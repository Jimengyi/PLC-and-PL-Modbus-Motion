"""Modbus TCP/RTU master base library.

Copied into each application project as a plain folder, so the version is
kept here (not read from installed metadata) and must match pyproject.toml.
"""

from .bus import Bus, open
from .errors import (
    ConfigError,
    DeviceDisabled,
    DeviceDisconnected,
    DeviceNotFound,
    ModbusTagsError,
    ReadOnlyTag,
    TagNotFound,
    ValueOutOfRange,
    WaitTimeout,
    WriteFailed,
)
from .model import (
    Access,
    DataModel,
    DeviceInfo,
    DeviceStatus,
    PollStats,
    Snapshot,
    TagInfo,
    Ticket,
)
from .simulator import Simulator

__version__ = "0.7.0"

__all__ = [
    "Access",
    "Bus",
    "ConfigError",
    "DataModel",
    "DeviceDisabled",
    "DeviceDisconnected",
    "DeviceInfo",
    "DeviceNotFound",
    "DeviceStatus",
    "ModbusTagsError",
    "PollStats",
    "ReadOnlyTag",
    "Simulator",
    "Snapshot",
    "TagInfo",
    "TagNotFound",
    "Ticket",
    "ValueOutOfRange",
    "WaitTimeout",
    "WriteFailed",
    "__version__",
    "open",
]
