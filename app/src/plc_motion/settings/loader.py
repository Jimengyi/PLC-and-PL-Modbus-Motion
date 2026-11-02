"""載入 config 資料夾並做 FR-8 啟動檢查；收集全部錯誤後才 raise。"""

from dataclasses import dataclass
from pathlib import Path

from plc_motion.settings.axes import AxisSettings, load_axes
from plc_motion.settings.device import DeviceSettings, load_device
from plc_motion.settings.io_points import IoPointTable, load_io_points
from plc_motion.settings.positions import PositionTable, load_positions
from plc_motion.settings.read_ranges import ReadRange, check_coverage, load_read_ranges


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
    # read_ranges.toml 的批次讀取範圍；None 表示沒有這個檔，依位址連續自動分組
    read_ranges: tuple[ReadRange, ...] | None = None


def load_config(config_dir: Path) -> AppConfig:
    config_dir = Path(config_dir)
    device, device_errors = load_device(config_dir / "device.toml")
    axes, axes_errors = load_axes(config_dir / "axes.toml")
    points, point_errors = load_io_points(config_dir / "io_points.csv")
    positions, position_errors = load_positions(config_dir / "positions.csv")
    read_ranges, range_errors = load_read_ranges(config_dir / "read_ranges.toml")
    # 範圍與點位表都讀得到時，才檢查每個點是否都在範圍內
    if read_ranges is not None and points is not None:
        range_errors += check_coverage(read_ranges, points.all())
    errors = device_errors + axes_errors + point_errors + position_errors + range_errors
    if errors:
        raise ConfigErrors(errors)
    return AppConfig(
        config_dir=config_dir,
        device=device,
        axes=axes,
        points=points,
        positions=positions,
        read_ranges=read_ranges,
    )
