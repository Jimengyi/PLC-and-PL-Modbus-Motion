"""Public data types."""

from __future__ import annotations

import threading
from collections.abc import ItemsView, Mapping
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from .errors import TagNotFound, WaitTimeout, WriteFailed


def device_label(name: str, display_name: str) -> str:
    """How log and exception messages name a device (FR-11). Internal use only."""
    return f"{name!r} ({display_name})"


@dataclass(frozen=True)
class ConfigIssue:
    channel: str  # folder name; "modbus" for cross-channel errors
    file: str  # e.g. "io_points.csv"; "devices" for devices mapping errors
    location: str  # csv "line 5" or toml key path "protocol.timeout_ms"
    message: str


class DeviceStatus(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


class DataModel(Enum):
    COIL = "coil"
    DISCRETE_INPUT = "discrete_input"
    HOLDING_REGISTER = "holding_register"
    INPUT_REGISTER = "input_register"


class Access(Enum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    display_name: str
    channel: str
    transport: str  # "tcp" | "rtu"
    address: str  # TCP: "host:port"; RTU: COM port name (config value, even with simulate=True)


@dataclass(frozen=True)
class TagInfo:
    name: str
    display_name: str
    description: str
    data_model: DataModel
    data_type: str
    access: Access
    address: int
    bit: int | None
    length: int
    unit_id: int
    scale: float | None
    unit: str
    invert: bool
    word_order: str | None
    byte_swap: bool
    extra: Mapping[str, str]

    def __post_init__(self) -> None:
        # Copy so later edits to the caller's dict cannot leak in.
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))


@dataclass(frozen=True)
class Snapshot:
    """Values of one device from one completed read round (FR-37); cannot be modified.

    ``time`` is when that round's reads completed (aware local datetime);
    ``connected`` is whether the device was connected when ``snapshot()`` was called.
    ``values`` (constructor only) is copied, so later edits to the caller's dict do
    not leak in; read the values back with ``snap[tag]`` and ``items()``.
    """

    device: str
    time: datetime
    connected: bool
    values: InitVar[Mapping[str, object]]
    # hash=False: a mapping is unhashable; equal snapshots still hash equal
    _values: Mapping[str, object] = field(init=False, repr=False, hash=False)
    # only for messages: not part of equality, hash or repr
    _display_name: str = field(default="", repr=False, compare=False, kw_only=True)

    def __post_init__(self, values: Mapping[str, object]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, tag_name: str) -> object:
        try:
            return self._values[tag_name]
        except KeyError:
            device = (
                device_label(self.device, self._display_name)
                if self._display_name
                else repr(self.device)
            )
            raise TagNotFound(f"tag {tag_name!r} not found in device {device}") from None

    def items(self) -> ItemsView[str, object]:
        return self._values.items()

    def _with_connected(self, connected: bool) -> Snapshot:
        """The same device, time and values with ``connected`` replaced.

        Internal use only: lets ``Channel.snapshot()`` get a copy with a different
        ``connected``. Not part of the public API.
        """
        return Snapshot(
            self.device, self.time, connected, self._values, _display_name=self._display_name
        )


@dataclass(frozen=True)
class PollStats:
    """How the reads of one device are going (FR-63); cannot be modified.

    The three times cover the read requests of one poll round, in milliseconds, since the
    device last connected. They are None while it is not connected. ``requests`` and
    ``interval_ms`` come from the config: read requests per round, and the polling interval.
    """

    last_ms: float | None
    average_ms: float | None
    max_ms: float | None
    requests: int
    interval_ms: int


class ChannelThread(threading.Thread):
    """The thread type of a channel; waiting for a write on one is refused (FR-42)."""


def refuse_on_channel_thread(what: str) -> None:
    """FR-42: a channel thread waiting for a write could be waiting for itself."""
    if isinstance(threading.current_thread(), ChannelThread):
        raise RuntimeError(f"{what} cannot be called on a channel thread (in an event function)")


class Ticket:
    """The receipt of one write (FR-40); safe to read from any thread.

    ``tag`` is the reference given to ``write()`` ("device/Tag Name"), ``value`` the
    engineering value. ``ok`` and ``error`` stay None until the write has a result.
    """

    def __init__(self, tag: str, value: object) -> None:
        self.tag = tag
        self.value = value
        self._lock = threading.Lock()
        self._finished = threading.Event()
        # None until finished, then (error,): one assignment, so no reader sees half a result
        self._outcome: tuple[WriteFailed | None] | None = None

    def done(self) -> bool:
        return self._outcome is not None

    @property
    def ok(self) -> bool | None:
        outcome = self._outcome
        return None if outcome is None else outcome[0] is None

    @property
    def error(self) -> WriteFailed | None:
        outcome = self._outcome
        return None if outcome is None else outcome[0]

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the result; a timeout does not cancel the write (FR-41)."""
        refuse_on_channel_thread("Ticket.wait()")
        if not self._finished.wait(timeout):
            raise WaitTimeout(f"write of {self.tag!r} has no result after {timeout} s")
        error = self.error
        if error is not None:
            raise error

    def _finish(self, error: WriteFailed | None) -> bool:
        """Record the result; False if it already has one. Internal use only."""
        with self._lock:
            if self._outcome is not None:
                return False
            self._outcome = (error,)
        self._finished.set()
        return True
