"""Configuration loading: scan config_dir/modbus/, resolve names, check everything.

FR-1 ~ FR-4, FR-11 ~ FR-13. No communication happens here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ConfigError
from ..model import ConfigIssue, TagInfo
from .device import DeviceConfig, parse_device_toml
from .points import parse_io_points
from .ranges import MAX_READ_BITS, MAX_READ_REGISTERS, ReadRange, parse_read_ranges

__all__ = [
    "ChannelConfig",
    "DeviceConfig",
    "DisabledChannel",
    "LoadedConfig",
    "MAX_READ_BITS",
    "MAX_READ_REGISTERS",
    "ReadRange",
    "load_config",
]

_log = logging.getLogger("modbus_tags")

_MODBUS = "modbus"  # ConfigIssue.channel for errors not tied to one folder
_DEVICES = "devices"  # ConfigIssue.file for devices mapping errors


@dataclass(frozen=True)
class DisabledChannel:
    name: str
    channel: str
    display_name: str  # equals name when omitted


@dataclass(frozen=True)
class ChannelConfig:
    name: str  # identification name (FR-11)
    channel: str  # folder name
    display_name: str  # equals name when omitted
    device: DeviceConfig
    tags: tuple[TagInfo, ...]
    read_ranges: tuple[ReadRange, ...] | None  # None: no read_ranges.toml


@dataclass
class LoadedConfig:
    channels: list[ChannelConfig] = field(default_factory=list)  # enabled only
    disabled: dict[str, DisabledChannel] = field(default_factory=dict)  # name -> channel


def load_config(
    config_dir: str | os.PathLike[str], devices: Mapping[str, str] | None
) -> LoadedConfig:
    """Load and check every channel; raise ConfigError with all issues at once.

    Warnings (e.g. column names close to known ones) go to logger "modbus_tags".
    """
    modbus_dir = Path(config_dir) / _MODBUS
    if not modbus_dir.is_dir():
        raise ConfigError(
            [
                ConfigIssue(
                    _MODBUS,
                    "(folder)",
                    str(modbus_dir),
                    f"config folder '{_MODBUS}' not found in {Path(config_dir)}",
                )
            ]
        )

    folders = sorted(p.name for p in modbus_dir.iterdir() if p.is_dir())
    issues: list[ConfigIssue] = []
    names = _resolve_names(folders, devices, issues)

    loaded = LoadedConfig()
    rtu_ports: dict[str, str] = {}  # casefolded port -> first folder using it
    for folder in folders:
        name = names[folder]
        device, result = _load_channel(modbus_dir / folder, name, issues)
        # FR-13: any enabled RTU channel whose device.toml parsed takes part, even if
        # its other files have errors, so port clashes surface in the same run.
        port = device.rtu_port if device is not None and device.enabled else None
        if device is not None and device.transport == "rtu" and port is not None:
            key = port.casefold()
            if key in rtu_ports:
                issues.append(
                    ConfigIssue(
                        _MODBUS,
                        "device.toml",
                        "connection.rtu.port",
                        f"{rtu_ports[key]} and {folder} both use port {port!r} "
                        "(port names are compared ignoring case)",
                    )
                )
            else:
                rtu_ports[key] = folder
        if isinstance(result, DisabledChannel):
            loaded.disabled[name] = result
        elif result is not None:
            loaded.channels.append(result)

    if issues:
        raise ConfigError(issues)
    return loaded


def _resolve_names(
    folders: list[str], devices: Mapping[str, str] | None, issues: list[ConfigIssue]
) -> dict[str, str]:
    """Return folder -> identification name (FR-11), adding FR-12 issues."""
    names = {folder: folder for folder in folders}
    mapped: dict[str, str] = {}  # folder -> developer name
    if devices is None:
        return names
    if not isinstance(devices, Mapping):
        message = (
            "devices must be a mapping of device name to channel folder, "
            f"got {type(devices).__name__}"
        )
        issues.append(ConfigIssue(_MODBUS, _DEVICES, "devices", message))
        return names

    def error(name: object, message: str) -> None:
        issues.append(ConfigIssue(_MODBUS, _DEVICES, f"devices[{name!r}]", message))

    for name, folder in devices.items():
        if not isinstance(name, str) or name.strip() == "":
            error(name, "device name must be a non-empty string")
            continue
        if not isinstance(folder, str):
            error(name, f"channel folder must be a string, got {type(folder).__name__}")
            continue
        if "/" in name:
            error(name, "device name must not contain '/'")
            continue
        if folder not in names:
            error(name, f"channel folder {folder!r} not found in '{_MODBUS}'")
            continue
        if folder in mapped:
            error(name, f"channel folder {folder!r} is already mapped to {mapped[folder]!r}")
            continue
        if name in names and name != folder:
            error(name, f"device name {name!r} is the folder name of another channel")
            continue
        mapped[folder] = name
        names[folder] = name
    return names


def _load_channel(
    folder: Path, name: str, issues: list[ConfigIssue]
) -> tuple[DeviceConfig | None, ChannelConfig | DisabledChannel | None]:
    """Parse one channel folder into (device, channel).

    device is None when device.toml has errors; channel is None when any file has
    errors. Errors are added to issues.
    """
    device_path = folder / "device.toml"
    if not device_path.is_file():
        issues.append(ConfigIssue(folder.name, "device.toml", "(file)", "required file is missing"))
        return None, None
    device_result = parse_device_toml(device_path)
    issues += device_result.issues
    device = device_result.config
    if device is None or device_result.issues:
        return None, None
    display_name = device.display_name or name
    if not device.enabled:
        return device, DisabledChannel(name, folder.name, display_name)
    assert device.transport is not None  # enabled channels always have one

    points = parse_io_points(folder / "io_points.csv", device.transport)
    issues += points.issues
    for warning in points.warnings:
        _log.warning("%s (%s), folder %s: %s", name, display_name, folder.name, warning)

    ranges_path = folder / "read_ranges.toml"
    read_ranges: tuple[ReadRange, ...] | None = None
    if ranges_path.exists():
        # With bad points, still report the file's own errors but skip coverage checks.
        ranges = parse_read_ranges(ranges_path, device, [] if points.issues else points.tags)
        issues += ranges.issues
        if ranges.issues:
            return device, None
        read_ranges = tuple(ranges.ranges)
    if points.issues:
        return device, None
    return device, ChannelConfig(
        name=name,
        channel=folder.name,
        display_name=display_name,
        device=device,
        tags=tuple(points.tags),
        read_ranges=read_ranges,
    )
