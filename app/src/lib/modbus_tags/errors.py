"""Public exception types."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import ConfigIssue


class ModbusTagsError(Exception):
    """Base class for all modbus_tags errors."""


class ConfigError(ModbusTagsError):
    """Configuration is invalid; carries every issue found."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        super().__init__(
            "\n".join(f"{i.channel}/{i.file} [{i.location}]: {i.message}" for i in issues)
        )


class DeviceNotFound(ModbusTagsError): ...


class TagNotFound(ModbusTagsError): ...


class DeviceDisabled(ModbusTagsError): ...


class DeviceDisconnected(ModbusTagsError): ...


class ReadOnlyTag(ModbusTagsError): ...


class ValueOutOfRange(ModbusTagsError): ...


class WriteFailed(ModbusTagsError): ...


class WaitTimeout(ModbusTagsError): ...
