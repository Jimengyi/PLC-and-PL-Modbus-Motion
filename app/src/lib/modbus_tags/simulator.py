"""Simulated devices built from the same config folder (FR-56 ~ FR-59).

Each enabled channel gets one pymodbus server on 127.0.0.1 with a free port:
Modbus TCP for tcp channels, RTU frames (with CRC) over TCP for rtu channels.
All servers share one asyncio loop running in a background thread.

pymodbus 3.16.2 cannot leave a request unanswered: unknown device ids get
exception 4, and SimDevice(id=0) answers for every undefined id. So the
servers' request handler is replaced: it records the request, drops it when
the unit is undefined or offline (nothing is sent, which matters for RTU
since a late frame would be taken as the next answer), applies the delay and
the rejected function codes, and runs the request PDU against this module's
own memory. Only pymodbus framing and PDU classes are used for the protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pymodbus import FramerType
from pymodbus.constants import ExcCodes
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import SimData, SimDevice

from .config import ChannelConfig, LoadedConfig, load_config
from .datatypes import decode, encode
from .errors import DeviceDisabled, DeviceNotFound, TagNotFound
from .model import DataModel, TagInfo, device_label

__all__ = ["RequestRecord", "Simulator"]

_log = logging.getLogger("modbus_tags.simulator")

_HOST = "127.0.0.1"
_LOOP_TIMEOUT = 5.0  # seconds to wait for the server thread to start or stop
MAX_REQUEST_RECORDS = 10000  # per channel; Simulator.requests() keeps the newest ones
_BIT_MODELS = (DataModel.COIL, DataModel.DISCRETE_INPUT)
_FC_MODEL: Mapping[int, DataModel] = {
    1: DataModel.COIL,
    5: DataModel.COIL,
    15: DataModel.COIL,
    2: DataModel.DISCRETE_INPUT,
    3: DataModel.HOLDING_REGISTER,
    6: DataModel.HOLDING_REGISTER,
    16: DataModel.HOLDING_REGISTER,
    22: DataModel.HOLDING_REGISTER,
    23: DataModel.HOLDING_REGISTER,
    4: DataModel.INPUT_REGISTER,
}
_SINGLE = (5, 6, 22)  # function codes addressing exactly one item


@dataclass(frozen=True)
class RequestRecord:
    function_code: int
    unit_id: int
    address: int
    count: int  # FC23: the read part


class _Unit:
    """One slave: its covered addresses (with values) and injected faults."""

    def __init__(self) -> None:
        self.online = True
        self.rejected: set[int] = set()
        self.memory: dict[DataModel, dict[int, int | bool]] = {m: {} for m in DataModel}


class _Channel:
    """State of one simulated channel; memory is guarded by ``lock``.

    Also serves as the datastore that pymodbus request PDUs run against
    (``async_getValues`` / ``async_setValues``).
    """

    def __init__(self, config: ChannelConfig) -> None:
        self.config = config
        self.label = device_label(config.name, config.display_name)  # for messages
        self.framer = FramerType.RTU if config.device.transport == "rtu" else FramerType.SOCKET
        self.tags = {tag.name: tag for tag in config.tags}
        self.lock = threading.Lock()
        self.units: dict[int, _Unit] = {}
        self.delay_ms = 0
        self.records: deque[RequestRecord] = deque(maxlen=MAX_REQUEST_RECORDS)
        self.port: int | None = None
        for tag in config.tags:
            unit = self.units.setdefault(tag.unit_id, _Unit())
            self._cover(unit, tag.data_model, tag.address, tag.length)
        for span in config.read_ranges or ():
            if span.unit_id in self.units:
                self._cover(self.units[span.unit_id], span.data_model, span.start, span.count)

    @staticmethod
    def _cover(unit: _Unit, model: DataModel, start: int, count: int) -> None:
        initial = False if model in _BIT_MODELS else 0
        memory = unit.memory[model]
        for address in range(start, start + count):
            memory.setdefault(address, initial)

    def unit(self, unit_id: int) -> _Unit:
        if unit_id not in self.units:
            raise ValueError(f"{self.label}: unit id {unit_id} is not defined in io_points.csv")
        return self.units[unit_id]

    def selected_units(self, unit_id: int | None) -> list[_Unit]:
        return list(self.units.values()) if unit_id is None else [self.unit(unit_id)]

    # --- datastore interface used by pymodbus request PDUs -------------------------

    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> list[int] | list[bool] | ExcCodes:
        model = _FC_MODEL.get(func_code)
        if model is None:
            return ExcCodes.ILLEGAL_FUNCTION
        with self.lock:
            memory = self.units[device_id].memory[model]
            addresses = range(address, address + count)
            if any(a not in memory for a in addresses):
                return ExcCodes.ILLEGAL_ADDRESS
            return [memory[a] for a in addresses]  # type: ignore[return-value]

    async def async_setValues(
        self, device_id: int, func_code: int, address: int, values: Sequence[int | bool]
    ) -> ExcCodes | None:
        model = _FC_MODEL.get(func_code)
        if model is None:
            return ExcCodes.ILLEGAL_FUNCTION
        with self.lock:
            memory = self.units[device_id].memory[model]
            if any(a not in memory for a in range(address, address + len(values))):
                return ExcCodes.ILLEGAL_ADDRESS
            convert = bool if model in _BIT_MODELS else int
            for offset, value in enumerate(values):
                memory[address + offset] = convert(value)
        return None

    # --- request handling ------------------------------------------------------------

    async def respond(self, pdu: ModbusPDU) -> ModbusPDU | None:
        """Response for one request; None means send nothing."""
        function_code = pdu.function_code
        unit_id = pdu.dev_id
        with self.lock:
            self.records.append(RequestRecord(function_code, unit_id, *_request_span(pdu)))
            unit = self.units.get(unit_id)
            if unit is None or not unit.online:
                return None
            delay_ms = self.delay_ms
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        with self.lock:
            if not unit.online:  # went offline while waiting
                return None
            rejected = function_code in unit.rejected
        if rejected:
            return ExceptionResponse(function_code, ExcCodes.ILLEGAL_FUNCTION)
        try:
            # pymodbus annotates the context as the deprecated ModbusServerContext, but
            # the PDUs only call async_getValues / async_setValues, which _Channel
            # implements; the cast states that duck typing without importing it.
            return await pdu.datastore_update(cast(Any, self), unit_id)
        except Exception:
            _log.exception("simulator %s: request %r failed", self.label, pdu)
            return ExceptionResponse(function_code, ExcCodes.DEVICE_FAILURE)


def _request_span(pdu: ModbusPDU) -> tuple[int, int]:
    if pdu.function_code in _SINGLE:
        return pdu.address, 1
    if pdu.function_code == 23:
        return pdu.read_address, pdu.read_count  # type: ignore[attr-defined]
    return getattr(pdu, "address", 0), getattr(pdu, "count", 0)


class _Handler(ServerRequestHandler):
    """Per-connection handler that lets ``_Channel.respond`` decide the answer."""

    async def handle_request(self, pdu: ModbusPDU) -> None:
        response = await self.server.sim_channel.respond(pdu)
        if response is None:
            return
        response.transaction_id = pdu.transaction_id
        response.dev_id = pdu.dev_id
        self.server_send(response, self.last_addr)


class _Server(ModbusTcpServer):
    def __init__(self, channel: _Channel) -> None:
        # The server requires a datastore; requests never reach it (see _Handler).
        placeholder = SimDevice(id=1, simdata=[SimData(0)])
        super().__init__(placeholder, framer=channel.framer, address=(_HOST, 0))
        self.sim_channel = channel

    def callback_new_connection(self) -> ServerRequestHandler:
        return _Handler(self, self.trace_packet, self.trace_pdu, self.trace_connect)


class Simulator:
    """Local Modbus servers simulating every enabled channel of a config folder."""

    def __init__(
        self, config_dir: str | os.PathLike[str], devices: Mapping[str, str] | None = None
    ) -> None:
        self._init(load_config(config_dir, devices))  # raises ConfigError

    @classmethod
    def _from_loaded(cls, loaded: LoadedConfig) -> Simulator:
        """A simulator for a config that is already loaded. Internal use only (``open()``)."""
        simulator = cls.__new__(cls)
        simulator._init(loaded)
        return simulator

    def _init(self, loaded: LoadedConfig) -> None:
        self._channels = {c.name: _Channel(c) for c in loaded.channels}
        self._disabled = dict(loaded.disabled)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: list[_Server] = []

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("simulator is already started")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="modbus_tags-simulator", daemon=True
        )
        thread.start()
        self._loop, self._thread = loop, thread
        try:
            asyncio.run_coroutine_threadsafe(self._open(), loop).result(_LOOP_TIMEOUT)
        except BaseException:
            self.stop()
            raise

    async def _open(self) -> None:
        for channel in self._channels.values():
            server = _Server(channel)
            self._servers.append(server)
            await server.serve_forever(background=True)
            channel.port = server.transport.sockets[0].getsockname()[1]

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), loop).result(_LOOP_TIMEOUT)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(_LOOP_TIMEOUT)
            if not thread.is_alive():
                loop.close()
            self._loop = self._thread = None
            self._servers = []
            for channel in self._channels.values():
                channel.port = None

    async def _close(self) -> None:
        for server in self._servers:
            await server.shutdown()  # also closes its client connections
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def __enter__(self) -> Simulator:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def address(self, device: str) -> tuple[str, int]:
        channel = self._channel(device)
        if channel.port is None:
            raise RuntimeError("simulator is not started")
        return _HOST, channel.port

    # --- values (FR-58) ------------------------------------------------------------------

    def set(self, ref: str, value: object) -> None:
        channel, tag = self._tag(ref)
        raw = encode(tag, value)
        with channel.lock:
            memory = channel.units[tag.unit_id].memory[tag.data_model]
            if isinstance(raw, bool):
                if tag.data_model in _BIT_MODELS:
                    memory[tag.address] = raw
                else:  # register bit point: rebuild the register
                    mask = 1 << tag.bit  # type: ignore[operator]
                    register = int(memory[tag.address])
                    memory[tag.address] = register | mask if raw else register & ~mask
            else:
                for offset, register in enumerate(raw):
                    memory[tag.address + offset] = register

    def get(self, ref: str) -> object:
        channel, tag = self._tag(ref)
        with channel.lock:
            memory = channel.units[tag.unit_id].memory[tag.data_model]
            raw = [memory[a] for a in range(tag.address, tag.address + tag.length)]
        return decode(tag, raw)

    def set_raw(
        self,
        device: str,
        data_model: DataModel,
        address: int,
        values: Sequence[int] | Sequence[bool],
        unit_id: int,
    ) -> None:
        channel = self._channel(device)
        bits = data_model in _BIT_MODELS
        for value in values:
            if bits and not isinstance(value, bool):
                raise TypeError(f"{data_model.value} values must be bool, got {value!r}")
            if not bits and not (
                isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFF
            ):
                raise ValueError(f"register values must be int 0~65535, got {value!r}")
        with channel.lock:
            memory = self._covered(channel, data_model, address, len(values), unit_id)
            for offset, value in enumerate(values):
                memory[address + offset] = value

    def get_raw(
        self, device: str, data_model: DataModel, address: int, count: int, unit_id: int
    ) -> list:
        channel = self._channel(device)
        with channel.lock:
            memory = self._covered(channel, data_model, address, count, unit_id)
            return [memory[a] for a in range(address, address + count)]

    # --- fault injection (FR-59) ---------------------------------------------------------

    def set_online(self, device: str, online: bool, unit_id: int | None = None) -> None:
        channel = self._channel(device)
        with channel.lock:
            for unit in channel.selected_units(unit_id):
                unit.online = online

    def reject_function(
        self, device: str, function_code: int, reject: bool = True, unit_id: int | None = None
    ) -> None:
        channel = self._channel(device)
        with channel.lock:
            for unit in channel.selected_units(unit_id):
                if reject:
                    unit.rejected.add(function_code)
                else:
                    unit.rejected.discard(function_code)

    def set_delay(self, device: str, ms: int) -> None:
        if ms < 0:
            raise ValueError(f"delay must be >= 0 ms, got {ms}")
        channel = self._channel(device)
        with channel.lock:
            channel.delay_ms = ms

    def requests(self, device: str) -> list[RequestRecord]:
        """Requests received by ``device``, oldest first.

        Each channel keeps only the newest ``MAX_REQUEST_RECORDS`` (10000)
        records; older ones are dropped as new requests arrive.
        """
        channel = self._channel(device)
        with channel.lock:
            return list(channel.records)

    # --- lookup ----------------------------------------------------------------------------

    def _channel(self, device: str) -> _Channel:
        if device in self._channels:
            return self._channels[device]
        disabled = self._disabled.get(device)
        if disabled is not None:
            raise DeviceDisabled(
                f"device {device_label(device, disabled.display_name)} is disabled"
            )
        raise DeviceNotFound(f"device {device!r} not found")

    def _tag(self, ref: str) -> tuple[_Channel, TagInfo]:
        device, _, name = ref.partition("/")  # same check order as Bus (FR-16)
        channel = self._channel(device)
        if name not in channel.tags:
            label = device_label(device, channel.config.display_name)
            raise TagNotFound(f"tag {name!r} not found in device {label}")
        return channel, channel.tags[name]

    @staticmethod
    def _covered(
        channel: _Channel, data_model: DataModel, address: int, count: int, unit_id: int
    ) -> dict[int, int | bool]:
        memory = channel.unit(unit_id).memory[data_model]
        missing = [a for a in range(address, address + count) if a not in memory]
        if missing:
            raise ValueError(
                f"{channel.label}: unit {unit_id} {data_model.value} "
                f"address {missing[0]} is not simulated"
            )
        return memory
