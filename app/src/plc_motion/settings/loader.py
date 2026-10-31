"""載入 config 資料夾並做 FR-8 啟動檢查；收集全部錯誤後才 raise。"""

from dataclasses import dataclass
from pathlib import Path

from plc_motion.settings.axes import AxisSettings, load_axes
from plc_motion.settings.device import DeviceSettings, load_device
from plc_motion.settings.io_points import IoPointTable, load_io_points


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
    positions: object | None = None  # PositionTable，由 Task 3 接上


def load_config(config_dir: Path) -> AppConfig:
    config_dir = Path(config_dir)
    device, device_errors = load_device(config_dir / "device.toml")
    axes, axes_errors = load_axes(config_dir / "axes.toml")
    points, point_errors = load_io_points(config_dir / "io_points.csv")
    errors = device_errors + axes_errors + point_errors
    if errors:
        raise ConfigErrors(errors)
    return AppConfig(config_dir=config_dir, device=device, axes=axes, points=points)
