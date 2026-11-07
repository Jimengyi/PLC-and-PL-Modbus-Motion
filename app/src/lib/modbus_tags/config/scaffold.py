"""Default files for a channel folder created by ``open(create_missing=True)``.

The templates are plain text kept in one place. ``device.toml`` is disabled and carries
its examples as comments: lines starting with ``## `` are explanations, lines starting
with ``# `` are settings that become valid TOML once those two characters are removed.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

from ..errors import ConfigError
from ..model import ConfigIssue
from . import _DEVICES, _MODBUS, _resolve_names

_log = logging.getLogger("modbus_tags")

_NEW_FOLDER_NAME = re.compile(r"[A-Za-z0-9_]+")
# mkdir may succeed with these names on Windows and leave a folder that is hard to remove.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"{port}{n}" for port in ("COM", "LPT") for n in range(10)}
)

_DEVICE_TOML = """\
## 通道設定檔。這個通道由 open(create_missing=True) 自動產生，預設停用。
##
## 啟用步驟：
##   1. 在下方 TCP 與 RTU 兩段連線範例中選一種，把每行開頭的「# 」移除（取消註解）
##   2. 把 [device] 的 enabled 改成 true
##   3. 把 io_points.csv 的範例點位改成實際點位
##      範例點位的位址在實機上可能不存在，會被判定為斷線，連實機前一定要改
##   4. 需要調整通訊參數時，取消註解最下方的 [protocol]、[polling]（顯示的是預設值）

[device]
enabled = false
## 現場顯示用名稱，不需要時可以不設。取消註解即可使用。
# display_name = "左側 PLC"

## === TCP ===
## Modbus TCP：host 改成設備的 IP 位址。
# [connection]
# type = "tcp"
#
# [connection.tcp]
# host = "192.168.0.10"
# port = 502

## === RTU ===
## Modbus RTU（序列埠）：port 改成實際的 COM port。
## 每條通道要用不同的 COM port，兩條啟用中的 RTU 通道使用同一個 port 會報錯。
# [connection]
# type = "rtu"
#
# [connection.rtu]
# port = "COM3"
# baudrate = 9600
# parity = "N"
# stopbits = 1

## === COMMON ===
## 通訊與輪詢參數，數值為程式內建預設值；不需要調整就不用取消註解。
# [protocol]
# timeout_ms = 1000
# retries = 2
# inter_request_delay_ms = 0
#
# [polling]
# interval_ms = 200
# auto_reconnect = true
# reconnect_interval_ms = 3000
"""

_IO_POINTS_ROWS = (
    "Tag Name,Display Name,Start Address,Address Length,Unit ID,Data Model,Data Type,"
    "Access,Visible,Description,Attributes,Check Status",
    "Example Coil,範例線圈,0,1,1,Coil,Bool,Read Only,O,範例，請改成實際點位,,Unchecked",
    "Example U16,範例 U16,0,1,1,Holding Register,U16,Read Only,O,範例，請改成實際點位,,Unchecked",
    "Example I32,範例 I32,10,2,1,Holding Register,I32,Read Only,O,"
    "範例，請改成實際點位,scale=10; unit=mm,Unchecked",
    "Example Bit,範例位元,20.b3,1,1,Holding Register,Bool,Read Only,O,"
    "範例，請改成實際點位,,Unchecked",
)

# UTF-8 with BOM and CRLF so Excel opens the Chinese text correctly.
_IO_POINTS_CSV = ("\r\n".join(_IO_POINTS_ROWS) + "\r\n").encode("utf-8-sig")

_READ_RANGES_TOML = """\
## 讀取範圍設定，涵蓋 io_points.csv 的所有點位。
## 改了 io_points.csv 的位址，這裡的範圍要同步修改，否則載入時會報「not covered by any range」。
## 不想自己維護的話，可以直接刪除這個檔案，改由程式自動分組；
## 刪除後，open(create_missing=True) 不會再把它產生回來。

[[unit]]
unit_id = 1
coil = "0"
holding_register = "0, 10~11, 20"
"""

CHANNEL_FILES: Mapping[str, bytes] = {
    "device.toml": _DEVICE_TOML.encode("utf-8"),
    "io_points.csv": _IO_POINTS_CSV,
    "read_ranges.toml": _READ_RANGES_TOML.encode("utf-8"),
}


def create_missing_channels(
    config_dir: str | os.PathLike[str], devices: Mapping[str, str] | None
) -> None:
    """Write the default files for channels in ``devices`` that do not exist yet.

    Every check runs before the first write. "Exists" means a direct subfolder of
    ``modbus/`` has exactly that name, so a value such as ``"../x"`` can never pass as an
    existing folder through path resolution. Raises ConfigError on any failure, leaving
    behind nothing this call created.
    """
    if not devices:
        return
    modbus_dir = Path(config_dir) / _MODBUS
    try:
        existing = [p.name for p in modbus_dir.iterdir() if p.is_dir()]
    except FileNotFoundError:  # also a plain file as config_dir on Windows; mkdir reports it
        existing = []
    except (OSError, ValueError) as error:  # ValueError: path with an embedded null
        issue = _path_issue(modbus_dir, f"cannot list folder: {error}", is_file=False)
        raise ConfigError([issue]) from error

    wanted: dict[str, object] = {}  # folder -> first device name using it, in devices order
    if isinstance(devices, Mapping):
        for name, folder in devices.items():
            if isinstance(folder, str):
                wanted.setdefault(folder, name)
    new = {folder: name for folder, name in wanted.items() if folder not in existing}
    issues: list[ConfigIssue] = []
    _resolve_names(existing + list(new), devices, issues)
    issues += _check_new_folders(existing, new)
    if issues:
        raise ConfigError(issues)

    targets = [
        folder
        for folder in wanted
        if not any(os.path.lexists(modbus_dir / folder / name) for name in CHANNEL_FILES)
    ]
    _write_channels(modbus_dir, targets)
    for folder in targets:
        _log.warning(
            "created default channel folder %s; it stays disabled until its device.toml is edited",
            os.path.abspath(modbus_dir / folder),
        )


def _write_channels(modbus_dir: Path, folders: list[str]) -> None:
    """Create the folders and their files, or remove all of it and raise.

    An OSError becomes ConfigError. Anything else (KeyboardInterrupt, MemoryError) is not a
    configuration problem, so it is raised unchanged and what is left only goes to the log.
    """
    created: list[Path] = []  # only what this call made; never removes anything else
    files: set[Path] = set()  # paths written as files; anything else here is a folder
    path = modbus_dir
    try:
        for folder in folders:
            channel_dir = modbus_dir / folder
            for path in (modbus_dir.parent, modbus_dir, channel_dir):
                if not path.is_dir():
                    path.mkdir()  # no parents=True: a missing parent of config_dir must fail
                    created.append(path)
            for name, data in CHANNEL_FILES.items():
                path = channel_dir / name
                files.add(path)
                with path.open("xb") as f:  # "x": an existing file fails, never overwritten
                    created.append(path)  # after open: a failed open made nothing to remove
                    f.write(data)
    except BaseException as error:
        left = _undo(created)
        if not isinstance(error, OSError):
            for p in left:
                _log.error("could not remove %s after an interrupted write", os.path.abspath(p))
            raise
        issues = [_path_issue(path, f"cannot create: {error}", is_file=path in files)]
        issues += [
            _path_issue(p, "could not be removed after the failure", is_file=p in files)
            for p in left
        ]
        raise ConfigError(issues) from error


def _undo(created: list[Path]) -> list[Path]:
    """Remove in reverse order (files are closed by now); returns what could not be removed.

    Removes one item at a time, not a tree: an existing folder can hold the user's files.
    """
    left: list[Path] = []
    for path in reversed(created):
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            left.append(path)
    return left


def _path_issue(path: Path, message: str, *, is_file: bool) -> ConfigIssue:
    """Fields as the base uses them: a channel's file, or a folder located by its path.

    The caller says which it is: the name cannot tell, since config_dir may be called
    ``device.toml``, and neither can the disk, since a plain file may sit where a folder goes.
    """
    where = os.path.abspath(path)
    if is_file:
        return ConfigIssue(path.parent.name, path.name, "(file)", f"{where}: {message}")
    return ConfigIssue(_MODBUS, "(folder)", where, f"{where}: {message}")


def _check_new_folders(existing: list[str], new: Mapping[str, object]) -> list[ConfigIssue]:
    """The name rules for folders about to be created (FR-8, FR-9); new is folder -> device name.

    Folders are quoted as written, not with ``repr``, so a Windows path stays recognizable.
    """
    issues: list[ConfigIssue] = []

    def error(name: object, message: str) -> None:
        issues.append(ConfigIssue(_MODBUS, _DEVICES, f"devices[{name!r}]", message))

    seen = {folder.casefold(): folder for folder in existing}
    for folder, name in new.items():
        if not _NEW_FOLDER_NAME.fullmatch(folder):
            error(name, f"channel folder '{folder}' must use only ASCII letters, digits and _")
        elif folder.upper() in _RESERVED_NAMES:
            error(name, f"channel folder '{folder}' is a reserved name on Windows")
        other = seen.setdefault(folder.casefold(), folder)
        if other != folder:
            error(name, f"channel folder '{folder}' differs only in case from '{other}'")
    return issues
