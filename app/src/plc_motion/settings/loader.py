"""載入 config 資料夾並做兩層啟動檢查（spec FR-6、FR-11）；收集同一層的全部錯誤後才 raise。

    config/
    ├─ app.toml                    交握與日誌（`device.load_app`）
    ├─ axes.toml
    ├─ positions.csv               不存在時自動建立
    └─ modbus/Channel01/           modbus_tags 格式，由函式庫載入與檢查
       ├─ device.toml
       ├─ io_points.csv
       └─ read_ranges.toml         可省略

第一層不依賴點位資料：函式庫的設定錯誤、app.toml、axes.toml、positions.csv、通道資料夾的
規則（只能有 Channel01，且須啟用、為 TCP）。第一層有錯時不做第二層：函式庫有錯就不提供點位。
第二層是點位表的 app 規則（`io_points.build_points`）。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from lib.modbus_tags import ConfigError
from lib.modbus_tags.config import ChannelConfig, ReadRange
from lib.modbus_tags.config import load_config as load_channels
from plc_motion.settings.axes import AxisSettings, load_axes
from plc_motion.settings.device import DeviceSettings, build_device, load_app
from plc_motion.settings.io_points import IoPointTable, build_points
from plc_motion.settings.positions import PositionTable, load_positions

MODBUS_DIR = "modbus"
CHANNEL = "Channel01"
DEVICE_NAME = "PLC"
DEVICES = {DEVICE_NAME: CHANNEL}


class ConfigErrors(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class AppConfig:
    config_dir: Path
    device: DeviceSettings
    axes: dict[int, AxisSettings]  # key 1~7
    points: IoPointTable
    positions: PositionTable
    channel: ChannelConfig  # 函式庫載入的 Channel01
    # read_ranges.toml 的批次讀取範圍；None 表示沒有這個檔，依位址連續自動分組
    read_ranges: tuple[ReadRange, ...] | None = None


def channel_dir(config_dir: Path) -> Path:
    return Path(config_dir) / MODBUS_DIR / CHANNEL


def _channel_rules(config_dir: Path) -> list[str]:
    """`modbus/` 只能有 Channel01，且須啟用、為 TCP；直接讀 device.toml，不經函式庫。"""
    modbus = Path(config_dir) / MODBUS_DIR
    if not modbus.is_dir():
        return []  # 函式庫會報 modbus 資料夾不存在
    errors: list[str] = []
    others = sorted(p.name for p in modbus.iterdir() if p.is_dir() and p.name != CHANNEL)
    if others:
        errors.append(
            f"{MODBUS_DIR}/ 底下有 {CHANNEL} 以外的資料夾：{'、'.join(others)}"
            f"（本程式只使用 {CHANNEL}）"
        )
    folder = modbus / CHANNEL
    if not folder.is_dir():
        errors.append(f"缺少通道資料夾 {MODBUS_DIR}/{CHANNEL}")
        return errors
    try:
        data = tomllib.loads((folder / "device.toml").read_bytes().decode("utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        return errors  # 缺檔或無法解析：函式庫會報
    where = f"{MODBUS_DIR}/{CHANNEL}/device.toml"
    device = data.get("device", {})
    if isinstance(device, dict) and device.get("enabled", True) is False:
        errors.append(f"{where}：通道 {CHANNEL} 被停用（[device] enabled = false），須為 true")
    connection = data.get("connection", {})
    kind = connection.get("type") if isinstance(connection, dict) else None
    if kind != "tcp":
        errors.append(
            f'{where}：通道 {CHANNEL} 須為 TCP（[connection] type = "tcp"），實際為 {kind!r}'
        )
    return errors


def _library_errors(e: ConfigError) -> list[str]:
    return [f"{i.channel}/{i.file} [{i.location}]: {i.message}" for i in e.issues]


def load_config(config_dir: Path) -> AppConfig:
    config_dir = Path(config_dir)

    # ---- 第一層 ----
    errors: list[str] = []
    channel: ChannelConfig | None = None
    try:
        loaded = load_channels(config_dir, DEVICES)
    except ConfigError as e:
        errors += _library_errors(e)
    else:
        channel = next((c for c in loaded.channels if c.name == DEVICE_NAME), None)
    errors += _channel_rules(config_dir)
    app, app_errors = load_app(config_dir / "app.toml")
    axes, axes_errors = load_axes(config_dir / "axes.toml")
    positions, position_errors = load_positions(config_dir / "positions.csv")
    errors += app_errors + axes_errors + position_errors
    if errors:
        raise ConfigErrors(errors)
    assert channel is not None and app is not None  # 沒有錯誤時一定有

    # ---- 第二層 ----
    points, point_errors = build_points(channel_dir(config_dir) / "io_points.csv", channel.tags)
    if point_errors:
        raise ConfigErrors(point_errors)
    return AppConfig(
        config_dir=config_dir,
        device=build_device(app, channel.device),
        axes=axes,
        points=points,
        positions=positions,
        channel=channel,
        read_ranges=channel.read_ranges,
    )
