"""交握引擎 MotionEngine：前置檢查、全域動作鎖、多軸操作的推進與結果彙整。

純邏輯：不 import Qt，時間由注入的 clock 提供，I/O 只經由 PlcPort。
呼叫端（PlcWorker）每輪 `port.read_all()` 後呼叫一次 `tick(snapshot)`；命令方法在兩次
tick 之間呼叫，命令內的寫入要在下一次 read_all 之前完成，tick 收到的快照才反映這些寫入。
PlcPort 寫入失敗的 PlcCommError 不在此攔截，由呼叫端視為通訊中斷並呼叫 `on_disconnected()`。
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from plc_motion.codec import decimals_for, to_raw
from plc_motion.engine.events import (
    AxisResult,
    CommandRejected,
    EngineEvent,
    OperationFinished,
    OperationKind,
    OperationStarted,
    ResultKind,
)
from plc_motion.engine.handshake import CODE_HOME, CODE_MOVE, AxisHandshake
from plc_motion.plc.port import PlcPort
from plc_motion.plc.snapshot import AxisState, Snapshot
from plc_motion.settings.loader import AppConfig

LIFT_AXIS = 7  # 原點復歸時先做的軸（FR-23）
SPEED_MIN = 1
SPEED_MAX = 100

REASON_NOT_CONNECTED = "未連線"
REASON_FIRST_POLL = "連線後第一次輪詢尚未完成"
REASON_OPERATION = "操作進行中"
REASON_ESTOP = "急停中（Emergency Stop 已觸發）"

Listener = Callable[[EngineEvent], None]


@dataclass
class _Operation:
    kind: OperationKind
    axes: tuple[int, ...]
    active: dict[int, AxisHandshake] = field(default_factory=dict)
    pending: list[AxisHandshake] = field(default_factory=list)  # 第 7 軸完成後才開始
    results: dict[int, AxisResult] = field(default_factory=dict)


class MotionEngine:
    def __init__(self, config: AppConfig, port: PlcPort, clock: Callable[[], float]):
        self._config = config
        self._port = port
        self._clock = clock
        self._listeners: list[Listener] = []
        self._connected = False
        self._first_poll_done = False
        self._snapshot: Snapshot | None = None
        self._op: _Operation | None = None

    # ---- 事件 ----

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def _emit(self, event: EngineEvent) -> None:
        for fn in self._listeners:
            fn(event)

    # ---- 連線與輪詢 ----

    def on_connected(self) -> None:
        self._connected = True
        self._first_poll_done = False
        self._snapshot = None

    def on_disconnected(self) -> None:
        """進行中操作的未完成軸一律以「通訊中斷」結束，不補寫（無法寫入，改由 FR-57 處理）。"""
        self._connected = False
        self._first_poll_done = False
        self._snapshot = None
        op = self._op
        if op is None:
            return
        lost = AxisResult(ResultKind.COMM_LOST)
        for hs in op.active.values():
            hs.abort(lost)
        for n in op.axes:
            op.results.setdefault(n, lost)
        op.pending = []
        self._finish_operation()

    def tick(self, snapshot: Snapshot) -> None:
        if not self._connected:
            return
        self._snapshot = snapshot
        self._first_poll_done = True
        if self._op is not None:
            self._advance(snapshot)

    def _advance(self, snapshot: Snapshot) -> None:
        op = self._op
        for n, hs in op.active.items():
            if n not in op.results:
                result = hs.advance(snapshot)
                if result is not None:
                    op.results[n] = result

        if op.pending and all(n in op.results for n in op.active):
            if all(r.kind == ResultKind.DONE for r in op.results.values()):
                # 第 7 軸完成：其餘軸同時開始；本輪快照早於這些寫入，下一輪才推進
                for hs in op.pending:
                    op.active[hs.axis] = hs
                    hs.start()
            else:
                for hs in op.pending:
                    op.results[hs.axis] = AxisResult(ResultKind.NOT_RUN)
            op.pending = []

        if not op.pending and len(op.results) == len(op.axes):
            self._finish_operation()

    def _finish_operation(self) -> None:
        op = self._op
        self._op = None
        results = MappingProxyType({n: op.results[n] for n in sorted(op.results)})
        self._emit(OperationFinished(op.kind, results))

    # ---- 鎖定 ----

    def _global_lock_reasons(self) -> list[str]:
        if not self._connected:
            return [REASON_NOT_CONNECTED]
        if not self._first_poll_done:
            return [REASON_FIRST_POLL]
        return [REASON_OPERATION] if self._op is not None else []

    def _busy_reasons(self, axes: Iterable[int]) -> list[str]:
        if self._snapshot is None:
            return []
        return [
            f"{self._label(n)}：Busy = 1（動作中）" for n in axes if self._snapshot.axis(n).busy
        ]

    @property
    def lock_reasons(self) -> list[str]:
        return self._global_lock_reasons() + self._busy_reasons(sorted(self._config.axes))

    @property
    def locked(self) -> bool:
        return bool(self.lock_reasons)

    # ---- 命令 ----

    def move(self, targets: dict[int, float], relative: bool, speed: int) -> list[str]:
        return self._move("移動", OperationKind.MOVE, targets, relative, speed)

    def step(self, axis: int, delta: float, speed: int) -> list[str]:
        return self._move("步進", OperationKind.STEP, {axis: delta}, True, speed)

    def home(self, axes: set[int]) -> list[str]:
        command = "原點復歸"
        order = sorted(set(axes))
        reasons = self._common_reasons(order)
        if reasons:
            return self._reject(command, reasons)
        snap = self._snapshot
        for n in order:
            reasons += self._axis_reasons(n, snap.axis(n), need_home=False)
        reasons += self._busy_reasons(n for n in sorted(self._config.axes) if n not in order)
        if reasons:
            return self._reject(command, reasons)

        handshakes = {
            n: AxisHandshake(
                n, CODE_HOME, self._port, self._clock, self._config.device.handshake_timeout_s
            )
            for n in order
        }
        first = [LIFT_AXIS] if LIFT_AXIS in handshakes and len(order) > 1 else order
        self._start_operation(
            OperationKind.HOME,
            order,
            {"axes": tuple(order)},
            first=[handshakes[n] for n in first],
            pending=[handshakes[n] for n in order if n not in first],
        )
        return []

    def _move(
        self,
        command: str,
        kind: OperationKind,
        targets: Mapping[int, float],
        relative: bool,
        speed: int,
    ) -> list[str]:
        order = sorted(targets)
        reasons = self._common_reasons(order)
        if reasons:
            return self._reject(command, reasons)
        snap = self._snapshot
        absolute: dict[int, float] = {}
        for n in order:
            st = snap.axis(n)
            reasons += self._axis_reasons(n, st, need_home=True)
            target = st.position + targets[n] if relative else float(targets[n])
            absolute[n] = target
            limit = self._config.axes[n]
            if not limit.soft_limit_min <= target <= limit.soft_limit_max:
                reasons.append(
                    f"{self._label(n)}：目標 {self._fmt(n, target)} 超出軟體極限 "
                    f"{self._fmt(n, limit.soft_limit_min)} ~ {self._fmt(n, limit.soft_limit_max)}"
                )
        reasons += self._busy_reasons(n for n in sorted(self._config.axes) if n not in order)
        if reasons:
            return self._reject(command, reasons)

        speed = min(max(int(speed), SPEED_MIN), SPEED_MAX)
        timeout = self._config.device.handshake_timeout_s
        handshakes = [
            AxisHandshake(
                n,
                CODE_MOVE,
                self._port,
                self._clock,
                timeout,
                target_raw=to_raw(absolute[n], self._scale(n)),
                speed=speed,
            )
            for n in order
        ]
        params = {
            "input": dict(targets),
            "relative": relative,
            "targets": absolute,
            "speed": speed,
        }
        self._start_operation(kind, order, params, first=handshakes, pending=[])
        return []

    def _common_reasons(self, order: list[int]) -> list[str]:
        """與個別軸狀態無關的拒絕原因；回傳非空時不再逐軸檢查。"""
        reasons = self._global_lock_reasons()
        if not order:
            reasons.append("沒有指定軸")
        reasons += [f"第 {n} 軸不存在" for n in order if n not in self._config.axes]
        if reasons:
            return reasons
        if not self._snapshot.estop_ok:
            return [REASON_ESTOP]
        return []

    def _axis_reasons(self, n: int, st: AxisState, need_home: bool) -> list[str]:
        items = []
        if not st.ready:
            items.append("Ready = 0（未就緒）")
        if st.busy:
            items.append("Busy = 1（動作中）")
        if st.alarm_code != 0:
            items.append(f"Alarm Code = {st.alarm_code}")
        if need_home and not st.home_finish:
            items.append("Find Home Finish = 0（尚未原點復歸）")
        return [f"{self._label(n)}：{item}" for item in items]

    def _reject(self, command: str, reasons: list[str]) -> list[str]:
        self._emit(CommandRejected(command, tuple(reasons)))
        return reasons

    def _start_operation(
        self,
        kind: OperationKind,
        axes: list[int],
        params: dict,
        first: list[AxisHandshake],
        pending: list[AxisHandshake],
    ) -> None:
        # 先建立操作再寫入：寫入途中通訊中斷時 on_disconnected 才能結束這個操作
        self._op = _Operation(kind, tuple(axes), {hs.axis: hs for hs in first}, pending)
        self._emit(OperationStarted(kind, tuple(axes), MappingProxyType(params)))
        for hs in first:
            hs.start()

    # ---- 顯示 ----

    def _label(self, n: int) -> str:
        axis = self._config.axes.get(n)
        return f"第 {n} 軸（{axis.name}）" if axis else f"第 {n} 軸"

    def _scale(self, n: int) -> int:
        return self._config.points.axis_tag("Current Position", n).scale

    def _fmt(self, n: int, value: float) -> str:
        point = self._config.points.axis_tag("Current Position", n)
        return f"{value:.{decimals_for(point.scale)}f} {point.unit}"
