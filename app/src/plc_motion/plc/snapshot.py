"""一次輪詢讀到的全部點位值（不可變），與逐軸狀態的判讀。

`values` 與函式庫的讀值相同：Bool 為邏輯值（`invert=true` 已反相，True＝觸發），有 `scale`
的數值點為工程值。PLC 原始值由 `raw()` 依 `invert`、`scale` 還原。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from plc_motion.settings.io_points import EMERGENCY_STOP, IoPoint, IoPointTable

_INTEGER_TYPES = frozenset({"U16", "I16", "U32", "I32"})


@dataclass(frozen=True)
class AxisState:
    ready: bool
    busy: bool
    home_finish: bool
    reply: bool
    request: bool
    alarm_code: int
    plc_action_code: int
    pc_action_code: int
    position: float  # 以倍率換算後的實際值
    servo: bool
    pos_limit_triggered: bool
    neg_limit_triggered: bool
    home_sensor: bool  # 原點感測器觸發
    jog_plus: bool
    jog_minus: bool


def to_logical(point: IoPoint, raw: int | bool) -> float | int | bool:
    """PLC 原始值轉為快照的值：Bool 套用 `invert`，有 `scale` 的數值點除以倍率。"""
    if point.data_type == "Bool":
        return bool(raw) != point.invert
    if point.scale:
        return raw / point.scale
    return raw


def to_plc_raw(point: IoPoint, value: float | int | bool) -> int | float | bool:
    """快照的值還原為 PLC 原始值：Bool 為 `value != invert`，整數型別為未乘倍率前的整數。"""
    if point.data_type == "Bool":
        return bool(value) != point.invert
    if point.scale:
        scaled = value * point.scale
        return round(scaled) if point.data_type in _INTEGER_TYPES else scaled
    return value


class Snapshot:
    def __init__(self, points: IoPointTable, values: Mapping[str, float | int | bool]):
        self._points = points
        self._values = MappingProxyType(dict(values))

    def value(self, tag: str) -> float | int | bool:
        """工程值；Bool 為邏輯值（True＝觸發）。"""
        return self._values[tag]

    def raw(self, tag: str) -> int | float | bool:
        """PLC 原始值。"""
        return to_plc_raw(self._points.get(tag), self._values[tag])

    def axis(self, n: int) -> AxisState:
        def v(name: str):
            return self._values[f"{name} {n}"]

        return AxisState(
            ready=v("Ready"),
            busy=v("Busy"),
            home_finish=v("Find Home Finish"),
            reply=v("Reply"),
            request=v("Request"),
            alarm_code=v("Alarm Code"),
            plc_action_code=v("PLC Action Code"),
            pc_action_code=v("PC Action Code"),
            position=v("Current Position"),
            servo=v("Servo Motor"),
            pos_limit_triggered=v("Positive Limit"),
            neg_limit_triggered=v("Negative Limit"),
            home_sensor=v("Home Sensor"),
            jog_plus=v("JOG Plus"),
            jog_minus=v("JOG Minus"),
        )

    @property
    def estop_ok(self) -> bool:
        """Emergency Stop 未觸發。"""
        return not self._values[EMERGENCY_STOP]
