"""The application's entry point: name resolution and forwarding to the channels.

FR-1, FR-14 ~ FR-16, FR-25, FR-34 ~ FR-53, FR-60. The ``Bus`` does no
communication itself: each ``Channel`` polls on its own thread and runs the listeners
there.

Channels are built by ``start()``, because with ``simulate=True`` their target address
is known only once the simulator runs. Until then a device reads as DISCONNECTED and
has no snapshot. Listeners registered before ``start()`` wait in the ``Bus`` and are
attached to each channel, in registration order, before that channel starts.

Listeners may call any ``Bus`` method on a channel thread, so the lookups take no lock
and ``_lock`` is never held while waiting for a channel thread (``stop()``).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping

from .channel import Channel
from .config import ChannelConfig, DisabledChannel, load_config
from .config.scaffold import create_missing_channels
from .datatypes import encode
from .errors import (
    DeviceDisabled,
    DeviceDisconnected,
    DeviceNotFound,
    ReadOnlyTag,
    TagNotFound,
)
from .model import (
    Access,
    DeviceInfo,
    DeviceStatus,
    PollStats,
    Snapshot,
    TagInfo,
    Ticket,
    device_label,
    refuse_on_channel_thread,
)
from .planning import plan_reads
from .simulator import Simulator

__all__ = ["Bus", "open"]

_log = logging.getLogger("modbus_tags.bus")


def open(
    config_dir: str | os.PathLike[str],
    devices: Mapping[str, str] | None = None,
    simulate: bool = False,
    *,
    create_missing: bool = False,
) -> Bus:
    """Load and check the config folder (FR-1); no communication happens here.

    ``simulate=True`` also creates a ``Simulator`` from the same config; ``bus.start()``
    starts it and connects every channel to it, ``bus.stop()`` stops it (FR-60).

    ``create_missing=True`` first writes disabled default files for the channels in
    ``devices`` that do not exist yet, and logs a warning for each one.
    """
    if create_missing:
        create_missing_channels(config_dir, devices)
    loaded = load_config(config_dir, devices)  # raises ConfigError
    simulator = Simulator._from_loaded(loaded) if simulate else None
    return Bus(loaded.channels, loaded.disabled, simulator)


class _Listener:
    """A registered event function and how to remove it from the channels built so far."""

    def __init__(self, func: Callable) -> None:
        self.func = func
        self.cancels: list[Callable[[], None]] = []


class Bus:
    """Every channel of one config folder, addressed by identification name."""

    def __init__(
        self,
        channels: list[ChannelConfig],
        disabled: Mapping[str, DisabledChannel],
        simulator: Simulator | None = None,
    ) -> None:
        self.simulator = simulator
        self._configs = {config.name: config for config in channels}
        self._disabled = dict(disabled)
        self._tags = {c.name: {tag.name: tag for tag in c.tags} for c in channels}

        self._lock = threading.Lock()  # guards the lifecycle flags and the listener lists
        self._channels: dict[str, Channel] = {}  # replaced as a whole by start()
        self._started = False
        self._stopped = False
        self._simulator_running = False
        self._change_listeners: list[_Listener] = []
        self._status_listeners: list[_Listener] = []
        self._write_done_listeners: list[_Listener] = []

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start every enabled channel; a device that cannot connect does not raise (FR-25).

        If a channel cannot be built, the simulator is stopped again and the error
        propagates: ``__exit__`` does not run when ``__enter__`` raises. If a channel
        cannot be started, the bus is stopped (channels and simulator) and the error
        propagates; the bus cannot be started again. A failure of that cleanup is logged
        and never replaces the original error.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError("bus was stopped; open a new one to start again")
            if self._started:
                return
            if self.simulator is not None:
                self.simulator.start()
            try:
                channels = {name: self._build(config) for name, config in self._configs.items()}
            except BaseException:
                if self.simulator is not None:
                    self.simulator.stop()
                raise
            self._simulator_running = self.simulator is not None
            self._started = True
            self._channels = channels
            failure: BaseException | None = None
            try:
                for channel in channels.values():
                    self._attach(channel)
                    channel.start()
            except BaseException as e:
                self._stopped = True
                failure = e
        if failure is not None:
            # outside the lock: stop() waits for the channel threads
            try:
                self.stop()
            except Exception:
                _log.exception("bus: cleanup after a failed start also failed")
            raise failure

    def _build(self, config: ChannelConfig) -> Channel:
        target = None if self.simulator is None else self.simulator.address(config.name)
        return Channel(config, target)

    def _attach(self, channel: Channel) -> None:
        for listener in self._change_listeners:
            listener.cancels.append(channel.add_change_listener(listener.func))
        for listener in self._status_listeners:
            listener.cancels.append(channel.add_status_listener(listener.func))
        for listener in self._write_done_listeners:
            listener.cancels.append(channel.add_write_done_listener(listener.func))

    def stop(self) -> None:
        """Stop every channel, then the simulator (FR-34, FR-60); safe to repeat.

        Returns once all channel threads have ended. Called from a listener, the
        calling channel's thread ends right after that listener returns instead.
        """
        with self._lock:
            self._stopped = True
            channels = list(self._channels.values())
        try:
            for channel in channels:
                channel.stop()
        finally:
            with self._lock:
                stop_simulator = self._simulator_running
                self._simulator_running = False
            if stop_simulator:
                self.simulator.stop()  # type: ignore[union-attr]

    def reconnect(self, device: str) -> None:
        """Attempt to connect now (FR-32); no effect while CONNECTING or CONNECTED."""
        config = self._config(device)
        channel = self._channels.get(device)
        label = device_label(device, config.display_name)
        if channel is None:
            raise RuntimeError(f"device {label} is not running: bus is not started")
        try:
            channel.reconnect()
        except RuntimeError as e:
            raise RuntimeError(f"device {label} is not running: its channel has stopped") from e

    def __enter__(self) -> Bus:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- reading -------------------------------------------------------------------------------

    def read(self, ref: str) -> object:
        """The value in the latest snapshot; no communication, no waiting (FR-36)."""
        config, tag = self._resolve(ref)
        snap = self.snapshot(config.name)
        if snap is None or not snap.connected:
            label = device_label(config.name, config.display_name)
            raise DeviceDisconnected(
                f"device {label} is {self.status(config.name).name}, cannot read {tag.name!r}"
            )
        return snap[tag.name]

    def snapshot(self, device: str) -> Snapshot | None:
        """The last completed round, or None if nothing was ever read (FR-37)."""
        self._config(device)
        channel = self._channels.get(device)
        return None if channel is None else channel.snapshot()

    def stats(self, device: str) -> PollStats:
        """Read timing since the device connected; no communication, no waiting (FR-63)."""
        config = self._config(device)
        channel = self._channels.get(device)
        if channel is None:
            return PollStats(None, None, None, len(plan_reads(config)), config.device.interval_ms)
        return channel.stats()

    # --- writing -------------------------------------------------------------------------------

    def write(self, ref: str, value: object) -> Ticket:
        """Queue a write and return at once; the FR-39 checks raise here, in order (FR-38)."""
        config, tag = self._resolve(ref)
        label = device_label(config.name, config.display_name)
        if tag.access is not Access.READ_WRITE:
            raise ReadOnlyTag(f"tag {tag.name!r} of device {label} is read only")
        raw = encode(tag, value)  # ValueOutOfRange, TypeError
        ticket = Ticket(ref, value)
        channel = self._channels.get(config.name)
        if channel is None or not channel.submit(ticket, tag, raw):
            raise DeviceDisconnected(
                f"device {label} is {self.status(config.name).name}, cannot write {tag.name!r}"
            )
        return ticket

    def write_wait(self, ref: str, value: object, timeout: float | None = None) -> None:
        """Write and wait for the device's answer; on a channel thread nothing is queued."""
        refuse_on_channel_thread("write_wait()")
        self.write(ref, value).wait(timeout)

    # --- lookups -------------------------------------------------------------------------------

    def devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(
                c.name,
                c.display_name,
                c.channel,
                c.device.transport,
                f"{c.device.tcp_host}:{c.device.tcp_port}"
                if c.device.transport == "tcp"
                else str(c.device.rtu_port or ""),
            )
            for c in self._configs.values()
        ]

    def status(self, device: str) -> DeviceStatus:
        self._config(device)
        channel = self._channels.get(device)
        return DeviceStatus.DISCONNECTED if channel is None else channel.status

    def tags(self, device: str) -> list[TagInfo]:
        self._config(device)
        return list(self._tags[device].values())

    # --- events --------------------------------------------------------------------------------

    def on_change(self, func: Callable) -> Callable[[], None]:
        """Register ``func(device, changed, snapshot)``; returns its cancel function (FR-51)."""
        return self._register(self._change_listeners, Channel.add_change_listener, func)

    def on_status(self, func: Callable) -> Callable[[], None]:
        """Register ``func(device, status, reason)``; returns its cancel function (FR-51)."""
        return self._register(self._status_listeners, Channel.add_status_listener, func)

    def on_write_done(self, func: Callable) -> Callable[[], None]:
        """Register ``func(ticket)``; returns its cancel function (FR-40, FR-51)."""
        return self._register(self._write_done_listeners, Channel.add_write_done_listener, func)

    def _register(
        self, listeners: list[_Listener], add: Callable, func: Callable
    ) -> Callable[[], None]:
        listener = _Listener(func)
        with self._lock:
            listeners.append(listener)
            listener.cancels = [add(channel, func) for channel in self._channels.values()]

        def cancel() -> None:
            with self._lock:
                if listener in listeners:
                    listeners.remove(listener)
                for cancel_on_channel in listener.cancels:
                    cancel_on_channel()

        return cancel

    # --- name resolution -----------------------------------------------------------------------

    def _config(self, device: str) -> ChannelConfig:
        config = self._configs.get(device)
        if config is not None:
            return config
        disabled = self._disabled.get(device)
        if disabled is not None:
            raise DeviceDisabled(
                f"device {device_label(device, disabled.display_name)} is disabled"
            )
        raise DeviceNotFound(f"device {device!r} not found")

    def _resolve(self, ref: str) -> tuple[ChannelConfig, TagInfo]:
        """Split ``"device/Tag Name"`` at the first ``/`` (FR-14); check device, then tag."""
        device, _, name = ref.partition("/")
        config = self._config(device)
        tag = self._tags[device].get(name)
        if tag is None:
            raise TagNotFound(
                f"tag {name!r} not found in device {device_label(device, config.display_name)}"
            )
        return config, tag
