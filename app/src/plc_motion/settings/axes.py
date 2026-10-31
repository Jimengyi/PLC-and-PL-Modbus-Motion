"""axes.toml：每軸一段 [[axes]]，數值單位與該軸顯示單位相同（mm 或 deg）。

[[axes]]
number = 1              # 軸號 1 ~ 7
name = "X"
soft_limit_min = -100.0
soft_limit_max = 100.0
step_long = 10.0
step_medium = 1.0
step_short = 0.1
default_speed = 50      # 1 ~ 100
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plc_motion.settings.device import load_toml

FILE_NAME = "axes.toml"
AXIS_NUMBERS = range(1, 8)


@dataclass(frozen=True)
class AxisSettings:
    number: int
    name: str
    soft_limit_min: float
    soft_limit_max: float
    step_long: float
    step_medium: float
    step_short: float
    default_speed: int


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_axis_number(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v in AXIS_NUMBERS


def _parse_axis(index: int, raw: Any) -> tuple[AxisSettings | None, list[str]]:
    where = f"{FILE_NAME} 第 {index} 段"
    if not isinstance(raw, dict):
        return None, [f"{where}：格式錯誤"]
    errors: list[str] = []
    number = raw.get("number")
    if not _is_axis_number(number):
        errors.append(f"{where}：軸號 number = {number!r} 不合法，須為 1 ~ 7")
        return None, errors
    where = f"{FILE_NAME} 第 {number} 軸"

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{where}：顯示名稱 name 須為非空字串")

    nums: dict[str, float] = {}
    for key in ("soft_limit_min", "soft_limit_max", "step_long", "step_medium", "step_short"):
        v = raw.get(key)
        if not _is_number(v):
            errors.append(f"{where}：{key} = {v!r} 須為數字")
        else:
            nums[key] = float(v)
    if "soft_limit_min" in nums and "soft_limit_max" in nums:
        if not nums["soft_limit_min"] < nums["soft_limit_max"]:
            errors.append(
                f"{where}：軟體極限最小值 {nums['soft_limit_min']} 不小於最大值 "
                f"{nums['soft_limit_max']}"
            )
    for key in ("step_long", "step_medium", "step_short"):
        if key in nums and nums[key] <= 0:
            errors.append(f"{where}：步距 {key} = {nums[key]} 須為正數")

    speed = raw.get("default_speed")
    if not (isinstance(speed, int) and not isinstance(speed, bool) and 1 <= speed <= 100):
        errors.append(f"{where}：預設速度 default_speed = {speed!r} 須為 1 ~ 100 的整數")

    if errors:
        return None, errors
    return (
        AxisSettings(
            number=number,
            name=name,
            soft_limit_min=nums["soft_limit_min"],
            soft_limit_max=nums["soft_limit_max"],
            step_long=nums["step_long"],
            step_medium=nums["step_medium"],
            step_short=nums["step_short"],
            default_speed=speed,
        ),
        [],
    )


def load_axes(path: Path) -> tuple[dict[int, AxisSettings] | None, list[str]]:
    data = load_toml(path)
    if isinstance(data, list):
        return None, data
    raw_axes = data.get("axes")
    if not isinstance(raw_axes, list):
        return None, [f"{FILE_NAME}：找不到 [[axes]] 段落"]

    errors: list[str] = []
    axes: dict[int, AxisSettings] = {}
    seen: set[int] = set()
    for index, raw in enumerate(raw_axes, start=1):
        if isinstance(raw, dict):
            number = raw.get("number")
            if number in seen:
                errors.append(f"{FILE_NAME}：第 {number} 軸重複定義")
                continue
            if _is_axis_number(number):
                seen.add(number)
        axis, axis_errors = _parse_axis(index, raw)
        errors += axis_errors
        if axis is not None:
            axes[axis.number] = axis
    for n in AXIS_NUMBERS:
        if n not in seen:
            errors.append(f"{FILE_NAME}：缺少第 {n} 軸")
    if errors:
        return None, errors
    return axes, []
