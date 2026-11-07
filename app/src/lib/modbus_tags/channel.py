"""One channel's thread: connection state, polling, snapshot and events.

FR-25 ~ FR-34, FR-32a, FR-37, FR-38 ~ FR-47, FR-51 ~ FR-53. Every request of a channel
is sent from its own thread; listeners run on that thread too.

The thread loop, per state:

- ``CONNECTED``: wait until ``interval_ms`` after the start of the previous round
  (no wait if that round overran, no make-up rounds), then run a round.
- ``RECONNECTING``: wait until ``reconnect_interval_ms`` after the start of the
  previous attempt (or after the connection was lost; no wait if the attempt overran),
  or until ``reconnect()``; then attempt to connect. A failed attempt stays silent.
  A ``reconnect_interval_ms`` shorter than one failed attempt (about the FR-29 judged
  timeout: 0.6 s TCP, 0.9 s RTU with the example settings) means attempts run back to
  back with no wait, the same no-wait rule as an overrunning poll round.
- ``DISCONNECTED``: wait for ``reconnect()``, then go ``CONNECTING`` and attempt once.

An attempt (FR-31) opens the transport and runs one round's reads; it succeeds only if
all of them succeed, and that round is the first poll round (FR-32a).

An unexpected error inside a round (anything but a ``TransportError``, e.g. from a
decoder) is logged and handled like FR-30, so the channel keeps reconnecting instead of
its thread dying.

Writes wait in a queue and go out at the start of a round, except the round of a
connection attempt: the queue is empty then, because only a CONNECTED channel accepts
writes. A write stays at the head of the queue until it has a result, so everything
that fails the queue (connection lost, stop) also fails the write in progress. Taking
the queue and leaving CONNECTED are one step under the lock, so no write can slip in
behind them and be sent after a reconnect (FR-33).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .config import ChannelConfig, ReadRange
from .datatypes import decode
from .errors import WriteFailed
from .model import (
    ChannelThread,
    DataModel,
    DeviceStatus,
    PollStats,
    Snapshot,
    TagInfo,
    Ticket,
    device_label,
)
from .planning import plan_reads
from .transport import ExceptionReply, Timeout, Transport, TransportError

__all__ = ["Channel"]

_log = logging.getLogger("modbus_tags.channel")

# The Snapshot passed to a change listener is that round's snapshot: its ``connected``
# is True (the round succeeded) and does not follow later status changes; call
# ``snapshot()`` for the state at the time of the call (FR-37).
ChangeListener = Callable[[str, dict[str, object], Snapshot], None]
StatusListener = Callable[[str, DeviceStatus, str], None]
WriteDoneListener = Callable[[Ticket], None]

_STOP_MARGIN = 2.0  # seconds stop() waits beyond the longest single request
_ILLEGAL_FUNCTION = 1  # the only exception code that means "use the alternative" (FR-44)


class _Stopping(Exception):
    """stop() was requested in the middle of a round."""


class _RoundFailed(Exception):
    """A request of the round met FR-30; the message is the on_status reason."""


@dataclass
class _Write:
    ticket: Ticket
    tag: TagInfo
    raw: list[int] | bool  # from datatypes.encode()


def _same(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


class Channel:
    """Polls one channel on its own thread (started by ``start()``)."""

    def __init__(self, config: ChannelConfig, target: tuple[str, int] | None = None) -> None:
        self._config = config
        self._device = config.device
        self._label = device_label(config.name, config.display_name)  # for log messages
        self._transport = Transport(config.device, target)
        self._ranges = plan_reads(config)

        self._cond = threading.Condition()  # guards everything below
        self._status = DeviceStatus.DISCONNECTED
        self._snapshot: Snapshot | None = None  # last completed round; connected=True
        # the read rounds since the last connection, in milliseconds (FR-63)
        self._rounds_read = 0
        self._read_total_ms = 0.0
        self._read_last_ms = 0.0
        self._read_max_ms = 0.0
        self._started = False
        self._stopping = False
        self._reconnect_requested = False
        self._change_listeners: list[tuple[object, ChangeListener]] = []
        self._status_listeners: list[tuple[object, StatusListener]] = []
        self._write_done_listeners: list[tuple[object, WriteDoneListener]] = []
        self._writes: deque[_Write] = deque()
        self._thread: threading.Thread | None = None

        # Unit IDs found not to support these function codes (FR-46); this object lives
        # as long as the Bus. Used only on the channel thread.
        self._no_fc06: set[int] = set()
        self._no_fc22: set[int] = set()

    # --- public ------------------------------------------------------------------------------

    @property
    def status(self) -> DeviceStatus:
        with self._cond:
            return self._status

    def start(self) -> None:
        with self._cond:
            if self._stopping:
                raise RuntimeError(f"channel {self._label} was stopped")
            if self._started:
                return
            self._started = True
            self._status = DeviceStatus.CONNECTING
            self._thread = ChannelThread(
                target=self._run, name=f"modbus_tags-{self._config.name}", daemon=True
            )
            # started under the lock, so stop() never sees a thread it cannot join; the
            # thread's first lock use waits for this block to end
            self._thread.start()

    def stop(self) -> None:
        """Stop polling and disconnect; returns once the thread has ended.

        The request in progress is allowed to finish (bounded by the FR-29 judged
        timeout); the thread then ends without sending further requests.
        """
        with self._cond:
            self._stopping = True
            thread = self._thread
            self._cond.notify_all()
        if thread is None or thread is threading.current_thread():
            # never started; or called from a listener: the thread ends (and goes
            # DISCONNECTED) once the listener returns
            return
        thread.join(self._longest_request() + _STOP_MARGIN)
        if thread.is_alive():
            _log.warning("%s: thread did not end within the stop timeout", self._label)
            self._fail_writes(self._take_writes(), "the channel was stopped")
        with self._cond:
            self._status = DeviceStatus.DISCONNECTED

    def reconnect(self) -> None:
        """DISCONNECTED: go CONNECTING and attempt now; RECONNECTING: attempt now."""
        with self._cond:
            if not self._started or self._stopping:
                raise RuntimeError(f"channel {self._label} is not running")
            if self._status in (DeviceStatus.DISCONNECTED, DeviceStatus.RECONNECTING):
                self._reconnect_requested = True
                self._cond.notify_all()

    def submit(self, ticket: Ticket, tag: TagInfo, raw: list[int] | bool) -> bool:
        """Queue a write (FR-38, FR-43); False if the channel is not CONNECTED right now."""
        with self._cond:
            if self._stopping or self._status is not DeviceStatus.CONNECTED:
                return False
            self._writes.append(_Write(ticket, tag, raw))
            return True

    def snapshot(self) -> Snapshot | None:
        with self._cond:
            snap, status = self._snapshot, self._status
        if snap is None:
            return None
        return snap._with_connected(status is DeviceStatus.CONNECTED)

    def stats(self) -> PollStats:
        requests, interval_ms = len(self._ranges), self._device.interval_ms
        with self._cond:
            if self._status is not DeviceStatus.CONNECTED or not self._rounds_read:
                return PollStats(None, None, None, requests, interval_ms)
            return PollStats(
                self._read_last_ms,
                self._read_total_ms / self._rounds_read,
                self._read_max_ms,
                requests,
                interval_ms,
            )

    def add_change_listener(self, func: ChangeListener) -> Callable[[], None]:
        return self._add(self._change_listeners, func)

    def add_status_listener(self, func: StatusListener) -> Callable[[], None]:
        return self._add(self._status_listeners, func)

    def add_write_done_listener(self, func: WriteDoneListener) -> Callable[[], None]:
        return self._add(self._write_done_listeners, func)

    def _add(self, listeners: list, func: Callable) -> Callable[[], None]:
        token = object()
        with self._cond:
            listeners.append((token, func))

        def cancel() -> None:
            # Once cancel() returns the listener is not called again; a call already
            # running on the channel thread is not interrupted.
            with self._cond:
                listeners[:] = [entry for entry in listeners if entry[0] is not token]

        return cancel

    # --- thread ----------------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._notify_status(DeviceStatus.CONNECTING, "starting")
            attempt_start = time.monotonic()  # RECONNECTING waits from here
            round_start = self._attempt(from_status=DeviceStatus.CONNECTING)
            while True:
                status = self.status
                if status is DeviceStatus.CONNECTED:
                    if not self._sleep(round_start + self._device.interval_ms / 1000):
                        break
                    round_start = time.monotonic()
                    try:
                        self._guarded_round(first=False)
                    except _RoundFailed as e:
                        attempt_start = time.monotonic()
                        self._lose_connection(str(e))
                elif status is DeviceStatus.RECONNECTING:
                    deadline = attempt_start + self._device.reconnect_interval_ms / 1000
                    if not self._sleep(deadline, wake_on_reconnect=True):
                        break
                    attempt_start = time.monotonic()
                    round_start = self._attempt(from_status=DeviceStatus.RECONNECTING)
                else:  # DISCONNECTED: only reconnect() leaves it
                    if not self._sleep(None, wake_on_reconnect=True):
                        break
                    self._set_status(DeviceStatus.CONNECTING, "reconnect requested")
                    attempt_start = time.monotonic()
                    round_start = self._attempt(from_status=DeviceStatus.CONNECTING)
        except _Stopping:
            pass
        except Exception:
            _log.exception("%s: channel thread failed", self._label)
        finally:
            with self._cond:
                # the thread is gone: reconnect() raises and start() refuses from now on
                self._stopping = True
                writes = self._take_writes()
            self._transport.close()
            self._fail_writes(writes, "the channel was stopped")
            self._set_status(DeviceStatus.DISCONNECTED, "stopped")

    def _attempt(self, from_status: DeviceStatus) -> float:
        """One connection attempt (FR-31); returns the start time of its read round."""
        self._raise_if_stopping()
        round_start = time.monotonic()
        try:
            try:
                self._transport.open()
            except TransportError as e:
                raise _RoundFailed(str(e)) from e
            round_start = time.monotonic()
            self._guarded_round(first=True)
        except _RoundFailed as e:
            self._transport.close()
            reason = str(e)
            if from_status is DeviceStatus.RECONNECTING:
                _log.debug("%s: reconnect attempt failed: %s", self._label, reason)
            elif self._device.auto_reconnect:
                self._set_status(DeviceStatus.RECONNECTING, reason)
            else:
                self._set_status(DeviceStatus.DISCONNECTED, reason)
        else:
            with self._cond:
                # a reconnect() made during this successful attempt is already served;
                # left set, it would skip the wait after the next connection loss
                self._reconnect_requested = False
        return round_start

    def _guarded_round(self, first: bool) -> None:
        """``_round``, with an unexpected error turned into ``_RoundFailed`` (logged)."""
        try:
            self._round(first)
        except _Stopping, _RoundFailed:
            raise
        except Exception as e:
            _log.exception("%s: unexpected error in a poll round", self._label)
            raise _RoundFailed(f"unexpected error: {e!r}") from e

    def _round(self, first: bool) -> None:
        """One poll round (FR-26). ``first``: the round of a connection attempt (FR-31)."""
        if not first:
            self._send_writes()
        read_start = time.monotonic()
        values = self._read_all()
        read_ms = (time.monotonic() - read_start) * 1000
        snap = Snapshot(
            self._config.name,
            datetime.now().astimezone(),
            True,
            values,
            _display_name=self._config.display_name,
        )
        with self._cond:
            previous = self._snapshot
            self._snapshot = snap
            self._count_read_round(read_ms, first)
        if first or previous is None:
            changed = dict(values)
        else:
            changed = {
                name: value for name, value in values.items() if not _same(value, previous[name])
            }
        if first:
            # FR-32a: snapshot -> CONNECTED -> on_status -> on_change
            self._set_status(DeviceStatus.CONNECTED, "connected")
        if changed:
            self._call(self._change_listeners, self._config.name, changed, snap)

    def _count_read_round(self, read_ms: float, first: bool) -> None:
        """Call under the lock. The round of a connection attempt starts the count over."""
        if first:
            self._rounds_read = 0
            self._read_total_ms = 0.0
            self._read_max_ms = 0.0
        self._rounds_read += 1
        self._read_total_ms += read_ms
        self._read_last_ms = read_ms
        self._read_max_ms = max(self._read_max_ms, read_ms)

    # --- writes -------------------------------------------------------------------------------

    def _send_writes(self) -> None:
        """Send the writes queued when the round starts; later ones wait for the next round."""
        with self._cond:
            count = len(self._writes)
        for _ in range(count):
            with self._cond:
                # one step: stop() sets _stopping before it takes the queue from another thread
                if self._stopping:
                    raise _Stopping
                write = self._writes[0]
            try:
                self._send(write)
            except ExceptionReply as e:  # FR-47: only this write fails
                self._write_finished(write, str(e))
            except TransportError as e:  # FR-30: _lose_connection() fails this write too
                raise _RoundFailed(str(e)) from e
            else:
                self._write_finished(write, None)

    def _send(self, write: _Write) -> None:
        """The function code choice of FR-44."""
        tag, raw = write.tag, write.raw
        if isinstance(raw, bool):
            if tag.bit is None:
                self._transport.write_coil(tag.unit_id, tag.address, raw)
            else:
                self._write_bit(tag, raw)
        elif len(raw) == 1:
            self._write_one_register(tag.unit_id, tag.address, raw[0])
        else:
            self._transport.write_registers(tag.unit_id, tag.address, raw)

    def _write_one_register(self, unit_id: int, address: int, value: int) -> None:
        if unit_id not in self._no_fc06:
            try:
                self._transport.write_register(unit_id, address, value)
                return
            except ExceptionReply as e:
                if e.code != _ILLEGAL_FUNCTION:
                    raise
            self._no_fc06.add(unit_id)
            self._log_unsupported(unit_id, 6, "FC16")
            self._raise_if_stopping()
        self._transport.write_registers(unit_id, address, [value])

    def _write_bit(self, tag: TagInfo, value: bool) -> None:
        mask = 1 << tag.bit  # type: ignore[operator]
        if tag.unit_id not in self._no_fc22:
            try:
                self._transport.mask_write(
                    tag.unit_id, tag.address, 0xFFFF ^ mask, mask if value else 0
                )
                return
            except ExceptionReply as e:
                if e.code != _ILLEGAL_FUNCTION:
                    raise
            self._no_fc22.add(tag.unit_id)
            self._log_unsupported(tag.unit_id, 22, "read-modify-write")
            self._raise_if_stopping()
        # FR-45
        rng = ReadRange(tag.unit_id, DataModel.HOLDING_REGISTER, tag.address, 1)
        register = int(self._transport.read(rng)[0])
        self._raise_if_stopping()
        self._write_one_register(
            tag.unit_id, tag.address, register | mask if value else register & ~mask & 0xFFFF
        )

    def _log_unsupported(self, unit_id: int, function_code: int, instead: str) -> None:
        _log.info(
            "%s: unit %d does not support FC%02d; using %s from now on",
            self._label,
            unit_id,
            function_code,
            instead,
        )

    def _write_finished(self, write: _Write, failure: str | None) -> None:
        """Remove the write sent by this thread from the queue and report its result."""
        with self._cond:
            # stop() may have taken the queue if this thread overran its stop timeout
            if self._writes and self._writes[0] is write:
                self._writes.popleft()
        self._report(write, failure)

    def _take_writes(self) -> list[_Write]:
        with self._cond:
            writes = list(self._writes)
            self._writes.clear()
        return writes

    def _fail_writes(self, writes: list[_Write], reason: str) -> None:
        for write in writes:
            self._report(write, reason)

    def _report(self, write: _Write, failure: str | None) -> None:
        ticket = write.ticket
        error = None
        if failure is not None:
            error = WriteFailed(
                f"write of {ticket.tag!r} to device {self._label} failed: {failure}"
            )
        if not ticket._finish(error):
            return
        if error is not None:
            _log.warning("%s: %s", self._label, error)
        self._call(self._write_done_listeners, ticket)

    # --- reads --------------------------------------------------------------------------------

    def _read_all(self) -> dict[str, object]:
        data: dict[tuple[int, DataModel], dict[int, int | bool]] = {}
        for rng in self._ranges:
            self._raise_if_stopping()
            try:
                result = self._transport.read(rng)
            except TransportError as e:
                raise _RoundFailed(self._reason(rng, e)) from e
            memory = data.setdefault((rng.unit_id, rng.data_model), {})
            for offset, value in enumerate(result):
                memory[rng.start + offset] = value
        self._raise_if_stopping()
        values: dict[str, object] = {}
        for tag in self._config.tags:
            memory = data[(tag.unit_id, tag.data_model)]
            raw = [memory[a] for a in range(tag.address, tag.address + tag.length)]
            values[tag.name] = decode(tag, raw)
        return values

    @staticmethod
    def _reason(rng: ReadRange, e: TransportError) -> str:
        what = (
            f"read unit {rng.unit_id} {rng.data_model.value} address {rng.start} count {rng.count}"
        )
        if isinstance(e, ExceptionReply):
            return f"{what}: exception response code {e.code}"
        if isinstance(e, Timeout):
            return f"{what}: no response ({e})"
        return f"{what}: {e}"

    def _lose_connection(self, reason: str) -> None:
        """FR-30 while CONNECTED: the queue fails before on_status reports the loss."""
        self._transport.close()
        with self._cond:
            self._status = DeviceStatus.DISCONNECTED
            writes = self._take_writes()
        self._fail_writes(writes, f"connection lost: {reason}")
        self._notify_status(DeviceStatus.DISCONNECTED, reason)
        if self._device.auto_reconnect:
            self._set_status(DeviceStatus.RECONNECTING, reason)

    # --- helpers ---------------------------------------------------------------------------

    def _sleep(self, deadline: float | None, wake_on_reconnect: bool = False) -> bool:
        """Wait until ``deadline`` (monotonic; None = forever); False when stopping.

        With ``wake_on_reconnect`` a reconnect() request ends the wait early.
        """
        with self._cond:
            while not self._stopping:
                if wake_on_reconnect and self._reconnect_requested:
                    self._reconnect_requested = False
                    return True
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._cond.wait(remaining)
            return False

    def _raise_if_stopping(self) -> None:
        with self._cond:
            if self._stopping:
                raise _Stopping

    def _set_status(self, status: DeviceStatus, reason: str) -> None:
        with self._cond:
            if self._status is status:
                return
            self._status = status
        self._notify_status(status, reason)

    def _notify_status(self, status: DeviceStatus, reason: str) -> None:
        level = logging.WARNING if status is DeviceStatus.DISCONNECTED else logging.INFO
        _log.log(level, "%s: %s (%s)", self._label, status.name, reason)
        self._call(self._status_listeners, self._config.name, status, reason)

    def _call(self, listeners: list, *args: object) -> None:
        with self._cond:
            entries = list(listeners)
        for token, func in entries:
            with self._cond:
                # skip a listener cancelled after the copy was taken
                if not any(t is token for t, _ in listeners):
                    continue
            try:
                func(*args)
            except Exception:
                _log.exception("%s: listener %r raised", self._label, func)

    def _longest_request(self) -> float:
        d = self._device
        attempts = 2 * d.retries + 1 if d.transport == "rtu" else d.retries + 1
        return (d.timeout_ms * attempts + d.inter_request_delay_ms) / 1000
