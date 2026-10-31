"""模擬 PLC 的行為模型（plc-sim 與單元測試共用的唯一實作）。

依 docs/PLC_PC_Modbus_通訊規格.md 與 spec FR-52、FR-53。點位值一律存 raw：
Boolean 為 bool（輸入點依接點型式存電氣值）、U16 為 int、Position 為有號 I32。
PLC 邏輯只在 `step()` 掃描時執行；FR-53 的注入 Alarm 與急停立即生效。
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from plc_motion.codec import to_raw
from plc_motion.plc.snapshot import is_triggered
from plc_motion.settings.io_points import (
    AXIS_COUNT,
    EMERGENCY_STOP,
    IoPointTable,
    SensorType,
)

# 速度 100%、speed_factor 1 時的模擬最大速度（工程單位／秒：mm/s 或 deg/s）
MAX_SPEED = 50.0
# 原點復歸的移動速度（工程單位／秒，乘上 speed_factor）
HOME_SPEED = 25.0
# 原點復歸 Busy = 0 到 Find Home Finish = 1 的延遲（秒，不受 speed_factor 影響）；
# 須長於一個輪詢週期（200 ms），PC 才讀得到 Busy = 0 而 Find Home Finish = 0 的中間狀態
HOME_FINISH_DELAY = 0.5

ALARM_ESTOP = 1
ALARM_POS_LIMIT = 2
ALARM_NEG_LIMIT = 3
ALARM_SERVO_OFF = 4

CODE_HOME = 1
CODE_MOVE = 2

_LIMIT_NAMES = {"pos": "Positive Limit", "neg": "Negative Limit", "home": "Home Sensor"}

Listener = Callable[[str, int | bool], None]


class _Mode(Enum):
    IDLE = auto()
    HANDSHAKE = auto()  # Request 上升緣後、等 Request = 0
    MOVE = auto()
    HOME = auto()
    JOG = auto()


_BUSY_MODES = (_Mode.HANDSHAKE, _Mode.MOVE, _Mode.HOME, _Mode.JOG)


@dataclass
class _Axis:
    n: int
    scale: int
    pos: float = 0.0  # raw，移動中保留小數
    mode: _Mode = _Mode.IDLE
    action: int = 0
    jog_dir: int = 0
    home_wait: float | None = None  # 距離 Find Home Finish = 1 還剩幾秒
    persistent: bool = False
    no_reply: bool = False
    no_copy: bool = False
    prev_request: bool = False
    prev_reset: bool = False
    prev_jog_plus: bool = False
    prev_jog_minus: bool = False


class PlcModel:
    def __init__(self, points: IoPointTable):
        self._points = points
        self._values: dict[str, int | bool] = {}
        for p in points.all():
            if p.sensor_type == SensorType.BOOLEAN:
                # 輸入點以「未觸發」為初始狀態：B 接點未觸發時讀到 1
                self._values[p.tag] = p.is_b_contact
            else:
                self._values[p.tag] = 0
        self._axes = {
            n: _Axis(n, points.axis_tag("Current Position", n).scale)
            for n in range(1, AXIS_COUNT + 1)
        }
        self._listeners: list[Listener] = []
        self.speed_factor: float = 1.0

    # ---- 點位存取 ----

    @property
    def points(self) -> IoPointTable:
        return self._points

    def has_tag(self, tag: str) -> bool:
        return tag in self._values

    def read(self, tag: str) -> int | bool:
        return self._values[tag]

    def master_write(self, tag: str, value: int | bool) -> None:
        """模擬 Master 的寫入；PLC 端的反應在下一次 step() 發生。"""
        self._set(tag, self._coerce(tag, value))

    def add_listener(self, fn: Listener) -> None:
        """點位值有變化時呼叫 fn(tag, value)；來源含 Master 寫入、PLC 邏輯與 FR-53 操作。"""
        self._listeners.append(fn)

    def _coerce(self, tag: str, value: int | bool) -> int | bool:
        if self._points.get(tag).sensor_type == SensorType.BOOLEAN:
            return bool(value)
        return int(value)

    def _set(self, tag: str, value: int | bool) -> None:
        if self._values[tag] == value:
            return
        self._values[tag] = value
        for fn in self._listeners:
            fn(tag, value)

    def _get(self, ax: _Axis, name: str) -> int | bool:
        return self._values[f"{name} {ax.n}"]

    def _put(self, ax: _Axis, name: str, value: int | bool) -> None:
        self._set(f"{name} {ax.n}", value)

    def _triggered(self, tag: str) -> bool:
        return is_triggered(self._points.get(tag), self._values[tag])

    def _estop_pressed(self) -> bool:
        return self._triggered(EMERGENCY_STOP)

    def _limit_toward(self, ax: _Axis, direction: int) -> bool:
        name = "Positive Limit" if direction > 0 else "Negative Limit"
        return self._triggered(f"{name} {ax.n}")

    # ---- FR-53 操作 ----

    def set_limit(self, axis: int, which: str, triggered: bool) -> None:
        tag = f"{_LIMIT_NAMES[which]} {axis}"
        b_contact = self._points.get(tag).is_b_contact
        self._set(tag, not triggered if b_contact else triggered)

    def set_estop(self, pressed: bool) -> None:
        b_contact = self._points.get(EMERGENCY_STOP).is_b_contact
        self._set(EMERGENCY_STOP, not pressed if b_contact else pressed)
        if pressed:
            for ax in self._axes.values():
                self._apply_estop(ax)
                self._publish(ax)

    def set_position(self, axis: int, value: float) -> None:
        ax = self._axes[axis]
        ax.pos = float(to_raw(value, ax.scale))
        self._put(ax, "Current Position", round(ax.pos))

    def inject_alarm(self, axis: int, code: int, persistent: bool) -> None:
        ax = self._axes[axis]
        ax.persistent = persistent
        self._stop(ax)
        self._put(ax, "Alarm Code", code)
        self._publish(ax)

    def set_persistent(self, axis: int, persistent: bool) -> None:
        self._axes[axis].persistent = persistent

    def set_fault(self, axis: int, no_reply: bool, no_copy: bool) -> None:
        ax = self._axes[axis]
        ax.no_reply = no_reply
        ax.no_copy = no_copy

    # ---- 掃描 ----

    def step(self, dt: float) -> None:
        estop = self._estop_pressed()
        for ax in self._axes.values():
            self._step_axis(ax, dt, estop)

    def _step_axis(self, ax: _Axis, dt: float, estop: bool) -> None:
        servo = bool(self._get(ax, "Servo Motor"))
        request = bool(self._get(ax, "Request"))
        reset = bool(self._get(ax, "Reset Alarm"))
        jog_plus = bool(self._get(ax, "JOG Plus"))
        jog_minus = bool(self._get(ax, "JOG Minus"))

        if estop:
            self._apply_estop(ax)

        if not servo:
            if ax.mode in _BUSY_MODES:
                self._alarm(ax, ALARM_SERVO_OFF)
            ax.home_wait = None
            self._put(ax, "Find Home Finish", False)

        # 異常復歸：急停按住中或「原因持續」時不清除；極限異常停在極限上仍可清除
        if reset and not ax.prev_reset and not estop and not ax.persistent:
            self._put(ax, "Alarm Code", 0)

        # 動作在交握結束（Request = 0、Reply = 0）的下一次掃描才開始
        self._run_motion(ax, dt, jog_plus, jog_minus)

        if not request:
            self._put(ax, "Reply", False)
            if ax.mode == _Mode.HANDSHAKE:
                ax.mode = _Mode.HOME if ax.action == CODE_HOME else _Mode.MOVE

        if request and not ax.prev_request and self._can_start(ax, servo, estop):
            code = self._get(ax, "PLC Action Code")
            if not ax.no_reply and code in (CODE_HOME, CODE_MOVE):
                ax.mode = _Mode.HANDSHAKE
                ax.action = code
                ax.home_wait = None
                if code == CODE_HOME:
                    self._put(ax, "Find Home Finish", False)
                self._put(ax, "Reply", True)

        # JOG 只在上升緣檢查條件；朝已觸發的極限方向無效（不報異常）
        if self._can_start(ax, servo, estop):
            for pressed, prev, direction in (
                (jog_plus, ax.prev_jog_plus, 1),
                (jog_minus, ax.prev_jog_minus, -1),
            ):
                if pressed and not prev and not self._limit_toward(ax, direction):
                    ax.mode = _Mode.JOG
                    ax.jog_dir = direction
                    ax.home_wait = None
                    break

        ax.prev_request = request
        ax.prev_reset = reset
        ax.prev_jog_plus = jog_plus
        ax.prev_jog_minus = jog_minus
        self._publish(ax)

    def _can_start(self, ax: _Axis, servo: bool, estop: bool) -> bool:
        return servo and not estop and ax.mode == _Mode.IDLE and self._get(ax, "Alarm Code") == 0

    def _run_motion(self, ax: _Axis, dt: float, jog_plus: bool, jog_minus: bool) -> None:
        if ax.mode == _Mode.JOG:
            held = jog_plus if ax.jog_dir > 0 else jog_minus
            if not held:
                ax.mode = _Mode.IDLE
            elif not self._hit_limit(ax, ax.jog_dir):
                ax.pos += ax.jog_dir * self._speed(ax) * dt
        elif ax.mode == _Mode.MOVE:
            target = self._get(ax, "Target Position")
            direction = (target > ax.pos) - (target < ax.pos)
            if direction == 0:
                ax.mode = _Mode.IDLE
            elif not self._hit_limit(ax, direction):
                if self._approach(ax, target, self._speed(ax) * dt):
                    ax.mode = _Mode.IDLE
        elif ax.mode == _Mode.HOME:
            # 原點復歸中觸發極限不報異常
            if self._approach(ax, 0, HOME_SPEED * ax.scale * self.speed_factor * dt):
                ax.mode = _Mode.IDLE
                ax.home_wait = HOME_FINISH_DELAY
        elif ax.home_wait is not None:
            ax.home_wait -= dt
            if ax.home_wait <= 1e-9:
                ax.home_wait = None
                self._put(ax, "Find Home Finish", True)

    def _speed(self, ax: _Axis) -> float:
        """目前 Speed（1 ~ 100 %）對應的速度，raw／秒。"""
        percent = min(max(int(self._get(ax, "Speed")), 1), 100)
        return MAX_SPEED * ax.scale * percent / 100 * self.speed_factor

    @staticmethod
    def _approach(ax: _Axis, target: int, distance: float) -> bool:
        """朝 target 前進 distance，到達時回傳 True。"""
        if abs(target - ax.pos) <= distance:
            ax.pos = float(target)
            return True
        ax.pos += distance if target > ax.pos else -distance
        return False

    def _hit_limit(self, ax: _Axis, direction: int) -> bool:
        """移動方向的極限已觸發時停止並報 Alarm 2／3。"""
        if not self._limit_toward(ax, direction):
            return False
        self._alarm(ax, ALARM_POS_LIMIT if direction > 0 else ALARM_NEG_LIMIT)
        return True

    def _alarm(self, ax: _Axis, code: int) -> None:
        self._stop(ax)
        self._put(ax, "Alarm Code", code)

    def _apply_estop(self, ax: _Axis) -> None:
        self._alarm(ax, ALARM_ESTOP)
        self._put(ax, "Find Home Finish", False)

    @staticmethod
    def _stop(ax: _Axis) -> None:
        ax.mode = _Mode.IDLE
        ax.home_wait = None

    def _publish(self, ax: _Axis) -> None:
        """依內部狀態更新 Busy、Ready、Current Position 與命令碼複製。"""
        busy = ax.mode in _BUSY_MODES
        ready = (
            bool(self._get(ax, "Servo Motor"))
            and not busy
            and self._get(ax, "Alarm Code") == 0
            and not self._estop_pressed()
        )
        self._put(ax, "Busy", busy)
        self._put(ax, "Ready", ready)
        self._put(ax, "Current Position", round(ax.pos))
        if ready and not ax.no_copy:
            self._put(ax, "PLC Action Code", self._get(ax, "PC Action Code"))
