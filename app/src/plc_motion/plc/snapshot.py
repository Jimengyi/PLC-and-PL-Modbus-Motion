"""一次輪詢讀到的全部點位值（不可變），與逐軸狀態的判讀。"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from plc_motion.codec import from_raw
from plc_motion.settings.io_points import EMERGENCY_STOP, IoPoint, IoPointTable, SensorType


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


def is_triggered(point: IoPoint, raw: bool) -> bool:
    """依接點型式判讀輸入點是否觸發：B 接點讀到 0 為觸發，A 接點讀到 1 為觸發。"""
    return not raw if point.is_b_contact else bool(raw)


class Snapshot:
    def __init__(self, points: IoPointTable, values: Mapping[str, int | bool]):
        self._points = points
        self._values = MappingProxyType(dict(values))

    def raw(self, tag: str) -> int | bool:
        return self._values[tag]

    def value(self, tag: str) -> float | int | bool:
        """Position 點以倍率換算為實際值，其餘點同 raw。"""
        raw = self._values[tag]
        point = self._points.get(tag)
        if point.sensor_type == SensorType.POSITION:
            return from_raw(raw, point.scale)
        return raw

    def _triggered(self, tag: str) -> bool:
        return is_triggered(self._points.get(tag), self._values[tag])

    def axis(self, n: int) -> AxisState:
        def raw(name: str):
            return self._values[f"{name} {n}"]

        return AxisState(
            ready=raw("Ready"),
            busy=raw("Busy"),
            home_finish=raw("Find Home Finish"),
            reply=raw("Reply"),
            request=raw("Request"),
            alarm_code=raw("Alarm Code"),
            plc_action_code=raw("PLC Action Code"),
            pc_action_code=raw("PC Action Code"),
            position=self.value(f"Current Position {n}"),
            servo=raw("Servo Motor"),
            pos_limit_triggered=self._triggered(f"Positive Limit {n}"),
            neg_limit_triggered=self._triggered(f"Negative Limit {n}"),
            home_sensor=self._triggered(f"Home Sensor {n}"),
            jog_plus=raw("JOG Plus"),
            jog_minus=raw("JOG Minus"),
        )

    @property
    def estop_ok(self) -> bool:
        """Emergency Stop 未觸發。"""
        return not self._triggered(EMERGENCY_STOP)
