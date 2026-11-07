"""device.toml parsing and validation (FR-3, FR-5, FR-6)."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import ConfigIssue

_FILE = "device.toml"


@dataclass(frozen=True)
class DeviceConfig:
    display_name: str | None
    enabled: bool
    transport: str | None  # "tcp" | "rtu"; None only for a disabled channel
    tcp_host: str | None
    tcp_port: int
    rtu_port: str | None
    baudrate: int | None
    parity: str | None
    stopbits: int | None
    timeout_ms: int
    retries: int
    inter_request_delay_ms: int
    interval_ms: int
    auto_reconnect: bool
    reconnect_interval_ms: int


@dataclass
class DeviceResult:
    config: DeviceConfig | None
    issues: list[ConfigIssue] = field(default_factory=list)


# FR-5 defaults: the single definition. Fields without a default start as None.
_DEFAULTS: dict[str, Any] = {
    "display_name": None,
    "enabled": True,
    "transport": None,
    "tcp_host": None,
    "tcp_port": 502,
    "rtu_port": None,
    "baudrate": None,
    "parity": None,
    "stopbits": None,
    "timeout_ms": 1000,
    "retries": 2,
    "inter_request_delay_ms": 0,
    "interval_ms": 200,
    "auto_reconnect": True,
    "reconnect_interval_ms": 3000,
}


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class _Reader:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.issues: list[ConfigIssue] = []

    def error(self, location: str, message: str) -> None:
        self.issues.append(ConfigIssue(self.channel, _FILE, location, message))

    def section(self, data: Mapping[str, Any], name: str, path: str) -> Mapping[str, Any]:
        """Return a sub-table, or {} (with an issue) when it is not a table."""
        value = data.get(name, {})
        if not isinstance(value, dict):
            self.error(f"{path}{name}", "must be a table")
            return {}
        return value

    def unknown_keys(self, table: Mapping[str, Any], allowed: set[str], path: str) -> None:
        for key in table:
            if key not in allowed:
                self.error(f"{path}{key}", "unknown key")

    def get(
        self,
        table: Mapping[str, Any],
        key: str,
        path: str,
        check: Callable[[Any], bool],
        expected: str,
        default: Any,
    ) -> Any:
        if key not in table:
            return default
        value = table[key]
        if not check(value):
            self.error(f"{path}{key}", f"must be {expected}")
            return default
        return value

    def required(
        self,
        table: Mapping[str, Any],
        key: str,
        path: str,
        check: Callable[[Any], bool],
        expected: str,
    ) -> Any:
        if key not in table:
            self.error(f"{path}{key}", "required")
            return None
        return self.get(table, key, path, check, expected, None)


def _str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _positive(v: Any) -> bool:
    return _is_int(v) and v >= 1


def _non_negative(v: Any) -> bool:
    return _is_int(v) and v >= 0


def _bool(v: Any) -> bool:
    return isinstance(v, bool)


def _range(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: _is_int(v) and lo <= v <= hi


def _one_of(*options: Any) -> Callable[[Any], bool]:
    # Match type as well as value, so 1.0 is not accepted where 1 is required.
    return lambda v: any(type(v) is type(o) and v == o for o in options)


def _enum_text(*options: object) -> str:
    return "one of " + ", ".join(repr(o) for o in options)


def parse_device_toml(path: Path) -> DeviceResult:
    channel = path.parent.name
    r = _Reader(channel)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        r.error("(file)", f"TOML syntax error: {e}")
        return DeviceResult(None, r.issues)
    except OSError as e:
        r.error("(file)", f"cannot read file: {e}")
        return DeviceResult(None, r.issues)

    device = r.section(data, "device", "")
    r.unknown_keys(device, {"display_name", "enabled"}, "device.")
    display_name = r.get(
        device, "display_name", "device.", _str, "a non-empty string", _DEFAULTS["display_name"]
    )
    enabled = r.get(device, "enabled", "device.", _bool, "true or false", _DEFAULTS["enabled"])

    d: dict[str, Any] = {**_DEFAULTS, "display_name": display_name, "enabled": enabled}

    if enabled:
        r.unknown_keys(data, {"device", "connection", "protocol", "polling"}, "")
        _read_connection(r, data, d)
        _read_protocol(r, data, d)
        _read_polling(r, data, d)

    if r.issues:
        return DeviceResult(None, r.issues)
    return DeviceResult(DeviceConfig(**d), [])


def _read_connection(r: _Reader, data: Mapping[str, Any], d: dict[str, Any]) -> None:
    if "connection" not in data:
        r.error("connection", "required section is missing")
        return
    conn = r.section(data, "connection", "")
    r.unknown_keys(conn, {"type", "tcp", "rtu"}, "connection.")
    transport = r.required(
        conn, "type", "connection.", _one_of("tcp", "rtu"), _enum_text("tcp", "rtu")
    )
    d["transport"] = transport
    if transport == "tcp":
        tcp = r.section(conn, "tcp", "connection.")
        r.unknown_keys(tcp, {"host", "port"}, "connection.tcp.")
        d["tcp_host"] = r.required(tcp, "host", "connection.tcp.", _str, "a non-empty string")
        d["tcp_port"] = r.get(
            tcp,
            "port",
            "connection.tcp.",
            _range(1, 65535),
            "an integer 1 ~ 65535",
            _DEFAULTS["tcp_port"],
        )
    elif transport == "rtu":
        rtu = r.section(conn, "rtu", "connection.")
        r.unknown_keys(rtu, {"port", "baudrate", "parity", "stopbits"}, "connection.rtu.")
        p = "connection.rtu."
        d["rtu_port"] = r.required(rtu, "port", p, _str, "a non-empty string")
        d["baudrate"] = r.required(rtu, "baudrate", p, _positive, "a positive integer")
        d["parity"] = r.required(
            rtu, "parity", p, _one_of("N", "E", "O"), _enum_text("N", "E", "O")
        )
        d["stopbits"] = r.required(rtu, "stopbits", p, _one_of(1, 2), _enum_text(1, 2))


def _read_protocol(r: _Reader, data: Mapping[str, Any], d: dict[str, Any]) -> None:
    t = r.section(data, "protocol", "")
    p = "protocol."
    r.unknown_keys(t, {"timeout_ms", "retries", "inter_request_delay_ms"}, p)
    d["timeout_ms"] = r.get(
        t, "timeout_ms", p, _positive, "a positive integer", _DEFAULTS["timeout_ms"]
    )
    d["retries"] = r.get(t, "retries", p, _non_negative, "an integer >= 0", _DEFAULTS["retries"])
    d["inter_request_delay_ms"] = r.get(
        t,
        "inter_request_delay_ms",
        p,
        _non_negative,
        "an integer >= 0",
        _DEFAULTS["inter_request_delay_ms"],
    )


def _read_polling(r: _Reader, data: Mapping[str, Any], d: dict[str, Any]) -> None:
    t = r.section(data, "polling", "")
    p = "polling."
    r.unknown_keys(t, {"interval_ms", "auto_reconnect", "reconnect_interval_ms"}, p)
    d["interval_ms"] = r.get(
        t, "interval_ms", p, _positive, "a positive integer", _DEFAULTS["interval_ms"]
    )
    d["auto_reconnect"] = r.get(
        t, "auto_reconnect", p, _bool, "true or false", _DEFAULTS["auto_reconnect"]
    )
    d["reconnect_interval_ms"] = r.get(
        t,
        "reconnect_interval_ms",
        p,
        _positive,
        "a positive integer",
        _DEFAULTS["reconnect_interval_ms"],
    )
