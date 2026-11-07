"""Synchronous Modbus master for one channel (FR-28, FR-29, FR-44).

Wraps a pymodbus sync client (TCP, or RTU over a serial port / ``socket://``).
Retries and timing are handled here, not by pymodbus: the client is created
with ``retries=0`` so every attempt is one request, timeouts are retried until
``retries + 1`` attempts have been made, exception replies are never retried,
and the next request waits ``inter_request_delay_ms`` after the last response.

The client never reconnects on its own: once the connection is lost every
request raises ``ConnectionLost`` until ``open()`` is called again.

Late answers on RTU: an RTU frame carries no transaction id, so the answer to a
request that timed out can arrive while the next request waits and be taken as
its answer. After a timeout on an RTU channel the next request is therefore
sent no earlier than ``timeout_ms`` after the timeout (a quiet period), and the
input buffer is discarded right before every RTU request. This covers answers
up to ``2 x timeout_ms`` late; later ones are caught only if they fail the
answer check below. The cost: on an unresponsive RTU device every retry is
preceded by ``timeout_ms`` of silence, so a request fails after about
``timeout_ms x (2 x retries + 1)``. Each attempt still waits at most
``timeout_ms`` for its answer, and the number of attempts is unchanged. TCP
matches answers by transaction id and has no quiet period.

Every answer is checked against the request (device id, function code, read
length, echoed write address); an answer that does not match counts as no
valid response and is retried like a timeout.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from pymodbus import FramerType
from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ModbusPDU

from .config import DeviceConfig, ReadRange
from .model import DataModel

__all__ = ["ConnectionLost", "ExceptionReply", "Timeout", "Transport", "TransportError"]

# data model -> (function code, pymodbus client method)
_READ = {
    DataModel.COIL: (1, "read_coils"),
    DataModel.DISCRETE_INPUT: (2, "read_discrete_inputs"),
    DataModel.HOLDING_REGISTER: (3, "read_holding_registers"),
    DataModel.INPUT_REGISTER: (4, "read_input_registers"),
}


def _answers(
    response: ModbusPDU,
    function_code: int,
    unit_id: int,
    address: int,
    matches: Callable[[ModbusPDU], bool] | None,
) -> bool:
    """True if ``response`` can be the answer to the request."""
    if response.dev_id != unit_id:
        return False
    if response.isError():
        return response.function_code in (function_code, function_code | 0x80)
    if response.function_code != function_code:
        return False
    if matches is not None:
        return matches(response)
    return getattr(response, "address", address) == address


class TransportError(Exception):
    """Communication failure; internal to the package."""


class Timeout(TransportError):
    """No response after ``retries + 1`` attempts."""


class ConnectionLost(TransportError):
    """Not connected, or the connection broke during a request."""


class ExceptionReply(TransportError):
    """The slave answered with a Modbus exception response."""

    def __init__(self, function_code: int, code: int, message: str) -> None:
        super().__init__(message)
        self.function_code = function_code
        self.code = code


class Transport:
    """One channel's synchronous Modbus client; used only on that channel's thread."""

    def __init__(self, device: DeviceConfig, target: tuple[str, int] | None = None) -> None:
        # target: connect there instead (simulate=True). TCP uses host:port,
        # RTU uses socket://host:port.
        if device.transport not in ("tcp", "rtu"):
            raise ValueError(f"unsupported transport {device.transport!r}")
        self._device = device
        self._target = target
        self._client: ModbusTcpClient | ModbusSerialClient | None = None
        self._last_reply: float | None = None  # monotonic time of the last response
        self._quiet_until: float | None = None  # RTU: no request before this (late answers)

    # --- connection ------------------------------------------------------------------------

    def open(self) -> None:
        self.close()
        client = self._new_client()
        if not client.connect():
            client.close()
            raise TransportError(f"cannot connect to {self._describe()}")
        self._client = client
        self._last_reply = None
        self._quiet_until = None

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def _new_client(self) -> ModbusTcpClient | ModbusSerialClient:
        d = self._device
        timeout = d.timeout_ms / 1000
        if d.transport == "tcp":
            host, port = self._target or (d.tcp_host, d.tcp_port)
            assert host is not None
            return ModbusTcpClient(host, port=port, timeout=timeout, retries=0)
        assert d.baudrate is not None and d.parity is not None and d.stopbits is not None
        return ModbusSerialClient(
            port=self._port_name(),
            framer=FramerType.RTU,
            baudrate=d.baudrate,
            parity=d.parity,
            stopbits=d.stopbits,
            timeout=timeout,
            retries=0,
        )

    def _port_name(self) -> str:
        if self._target is not None:
            host, port = self._target
            return f"socket://{host}:{port}"
        assert self._device.rtu_port is not None
        return self._device.rtu_port

    def _describe(self) -> str:
        if self._device.transport == "rtu":
            return self._port_name()
        host, port = self._target or (self._device.tcp_host, self._device.tcp_port)
        return f"{host}:{port}"

    # --- requests ------------------------------------------------------------------------

    def read(self, rng: ReadRange) -> list[int] | list[bool]:
        fc, method = _READ[rng.data_model]
        bits = rng.data_model in (DataModel.COIL, DataModel.DISCRETE_INPUT)

        def matches(r: ModbusPDU) -> bool:
            # bit answers are padded to whole bytes
            if bits:
                return len(r.bits) == (rng.count + 7) // 8 * 8
            return len(r.registers) == rng.count

        response = self._execute(
            fc,
            rng.unit_id,
            rng.start,
            lambda c: getattr(c, method)(rng.start, count=rng.count, device_id=rng.unit_id),
            matches,
        )
        if bits:
            return [bool(b) for b in response.bits[: rng.count]]
        return list(response.registers)

    def write_coil(self, unit_id: int, address: int, value: bool) -> None:
        self._execute(
            5, unit_id, address, lambda c: c.write_coil(address, bool(value), device_id=unit_id)
        )

    def write_register(self, unit_id: int, address: int, value: int) -> None:
        self._execute(
            6, unit_id, address, lambda c: c.write_register(address, value, device_id=unit_id)
        )

    def write_registers(self, unit_id: int, address: int, values: list[int]) -> None:
        self._execute(
            16,
            unit_id,
            address,
            lambda c: c.write_registers(address, list(values), device_id=unit_id),
        )

    def mask_write(self, unit_id: int, address: int, and_mask: int, or_mask: int) -> None:
        self._execute(
            22,
            unit_id,
            address,
            lambda c: c.mask_write_register(
                address=address, and_mask=and_mask, or_mask=or_mask, device_id=unit_id
            ),
        )

    def _execute(
        self,
        function_code: int,
        unit_id: int,
        address: int,
        call: Callable[[ModbusTcpClient | ModbusSerialClient], ModbusPDU],
        matches: Callable[[ModbusPDU], bool] | None = None,
    ) -> ModbusPDU:
        # matches: extra check of a normal (non-exception) answer; without it a
        # write answer must echo the address.
        what = f"unit {unit_id} FC{function_code:02d} address {address}"
        attempts = self._device.retries + 1
        last_exc: Exception | None = None
        for _ in range(attempts):
            client = self._connected_client(what)
            self._wait_inter_request_delay()
            try:
                self._prepare_rtu(client)
                response = call(client)
            except (ConnectionException, OSError) as e:
                # pymodbus lets some socket errors through as plain OSError
                # (e.g. ConnectionAbortedError from recv on Windows).
                self.close()
                raise ConnectionLost(f"{self._describe()}: connection lost ({what})") from e
            except ModbusIOException as e:
                # timeout, or a broken frame (CRC, length): retry
                last_exc = e
                self._start_quiet_period()
                continue
            if not _answers(response, function_code, unit_id, address, matches):
                last_exc = ModbusIOException(f"answer does not match the request: {response}")
                self._start_quiet_period()
                continue
            self._last_reply = time.monotonic()
            if response.isError():
                code = int(getattr(response, "exception_code", 0))
                raise ExceptionReply(
                    function_code,
                    code,
                    f"{self._describe()}: {what}: exception response, code {code}",
                )
            return response
        raise Timeout(
            f"{self._describe()}: {what}: no valid response after {attempts} attempt(s) "
            f"of {self._device.timeout_ms} ms (last: {last_exc})"
        ) from last_exc

    def _start_quiet_period(self) -> None:
        if self._device.transport == "rtu":
            self._quiet_until = time.monotonic() + self._device.timeout_ms / 1000

    def _prepare_rtu(self, client: ModbusTcpClient | ModbusSerialClient) -> None:
        # Let a late answer to a timed-out request land, then discard whatever
        # is waiting so that only the answer to this request is read.
        if self._device.transport != "rtu":
            return
        if self._quiet_until is not None:
            delay = self._quiet_until - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._quiet_until = None
        port = getattr(client, "socket", None)
        if port is not None:
            port.reset_input_buffer()

    def _connected_client(self, what: str) -> ModbusTcpClient | ModbusSerialClient:
        # pymodbus would silently reconnect on the next request; once the connection
        # is lost (close() was called) only open() connects again.
        client = self._client
        if client is None:
            raise ConnectionLost(f"{self._describe()}: not connected ({what})")
        return client

    def _wait_inter_request_delay(self) -> None:
        if self._last_reply is None:
            return
        remaining = self._last_reply + self._device.inter_request_delay_ms / 1000
        delay = remaining - time.monotonic()
        if delay > 0:
            time.sleep(delay)
