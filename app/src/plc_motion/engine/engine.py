"""交握引擎 MotionEngine：前置檢查、全域動作鎖、多軸操作的推進與結果彙整。

純邏輯：不 import Qt，時間由注入的 clock 提供，I/O 只經由 PlcPort。
呼叫端（PlcWorker）每拿到一份新快照呼叫一次 `tick(snapshot)`，同一份不重複；命令方法在
兩次 tick 之間呼叫。寫入只排入通訊佇列、不等結果，所以之後的快照可能仍早於這些寫入
（通道正好在讀取時）：交握等待的點都要等 PLC 反應才會變，舊值只會讓引擎多等一輪。
PlcPort 寫入失敗的 PlcCommError 不在此攔截，由呼叫端視為通訊中斷並呼叫 `on_disconnected()`。

名詞：
    操作（_Operation）：一次「移動／步進／原點復歸／Reset Alarm」命令，可能包含多軸；
        同一時間最多只有一個操作，進行中時其他命令一律拒絕（全域動作鎖）。
    交握（Handshake）：操作裡每一軸各一個狀態機物件（AxisHandshake 或 _ResetHandshake），
        由 tick 逐軸呼叫 advance() 推進；沒有額外的執行緒。

命令方法（move、home、jog_press…）的共同模式：
    1. 前置檢查，收集所有不通過的原因
    2. 有原因：發 CommandRejected 事件並回傳原因清單，不寫任何點
    3. 全部通過：寫入命令（或建立操作），回傳空清單
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from types import MappingProxyType

from lib.modbus_tags import Access
from plc_motion.codec import decimals_for, to_raw
from plc_motion.engine.events import (
    AxisResult,
    CommandRejected,
    DisconnectedWhileMoving,
    EngineEvent,
    JogResidualCleared,
    OperationFinished,
    OperationKind,
    OperationStarted,
    ResidualDetected,
    ResidualInfo,
    ResultKind,
)
from plc_motion.engine.handshake import CODE_HOME, CODE_MOVE, AxisHandshake
from plc_motion.plc.port import PlcPort
from plc_motion.plc.snapshot import AxisState, Snapshot
from plc_motion.settings.loader import AppConfig

LIFT_AXIS = 7  # 原點復歸時先做的軸（FR-23）
SPEED_MIN = 1  # 速度範圍（%）：超出時自動夾到範圍內，不拒絕
SPEED_MAX = 100

# 拒絕原因的固定文字；會顯示在畫面與日誌
REASON_NOT_CONNECTED = "未連線"
REASON_FIRST_POLL = "連線後第一次輪詢尚未完成"
REASON_OPERATION = "操作進行中"
REASON_ESTOP = "急停中（Emergency Stop 已觸發）"
REASON_NO_ALARM = "不需復歸：沒有任何軸 Alarm"

JOG_TAGS = {+1: "JOG Plus", -1: "JOG Minus"}  # JOG 方向 → 點位名稱（不含軸號）

# 事件接收函式；PlcWorker 註冊一個，收到後轉成 Qt signal 送給 UI
Listener = Callable[[EngineEvent], None]


class _ResetPhase(Enum):
    """Reset Alarm 交握的階段（相當於 LabVIEW 狀態機的 enum）。"""

    PULSING = auto()  # 已寫 Reset Alarm = 1，等脈衝時間到
    WAITING = auto()  # 已寫 Reset Alarm = 0，等 PLC 清掉 Alarm 並回到 Ready


class _ResetHandshake:
    """單軸異常復歸（FR-25；規格書 2.4）：Reset Alarm 1 → 脈衝時間後 0 → 等 Alarm = 0 且 Ready = 1。

    脈衝以 tick 計時：寫 1 之後第一次已過脈衝時間的 tick 寫 0，實際脈衝介於設定值與
    設定值＋一個輪詢週期之間。等待逾時從寫 0 起算；不重試。

    介面與 AxisHandshake 相同（start／advance／abort／axis／result），
    所以 MotionEngine 可以用同一套流程推進兩種交握。
    狀態只有兩段（_ResetPhase）：PULSING 為脈衝中（Reset Alarm = 1），WAITING 為等待 PLC 復歸。
    """

    def __init__(
        self,
        axis: int,
        port: PlcPort,
        clock: Callable[[], float],
        pulse_s: float,  # 脈衝寬度（秒），來自 device.toml 的 reset_alarm_pulse_ms
        timeout_s: float,  # 寫 0 之後等 PLC 復歸的逾時（秒）
    ):
        self.axis = axis
        self._port = port
        self._clock = clock
        self._pulse_s = pulse_s
        self._timeout_s = timeout_s
        self._tag = f"Reset Alarm {axis}"
        self.phase: _ResetPhase | None = None  # None：尚未 start()
        self._since = 0.0  # PULSING：寫 1 的時間；WAITING：寫 0 的時間
        self.result: AxisResult | None = None

    def start(self) -> None:
        self._port.write(self._tag, True)
        self.phase = _ResetPhase.PULSING
        self._since = self._clock()

    def abort(self, result: AxisResult) -> None:
        """通訊中斷用：不寫入，直接記下結果。"""
        if self.result is None:
            self.result = result

    def advance(self, snapshot: Snapshot) -> AxisResult | None:
        """每輪輪詢呼叫一次；回傳 None 表示還在進行，回傳 AxisResult 表示已結束。"""
        if self.phase is None or self.result is not None:
            return None
        elapsed = self._clock() - self._since
        match self.phase:  # 相當於 LabVIEW 的 Case Structure
            case _ResetPhase.PULSING:
                # 脈衝時間到才寫 0，並把計時改成從寫 0 起算
                if elapsed >= self._pulse_s:
                    self._port.write(self._tag, False)
                    self.phase = _ResetPhase.WAITING
                    self._since = self._clock()
                return None  # 本輪快照早於寫 0，下一輪才判斷
            case _ResetPhase.WAITING:
                # 等 PLC 清掉 Alarm 並回到 Ready；逾時就判定復歸失敗
                st = snapshot.axis(self.axis)
                if st.alarm_code == 0 and st.ready:
                    self.result = AxisResult(ResultKind.RESET_OK)
                elif elapsed >= self._timeout_s:
                    self.result = AxisResult(ResultKind.RESET_FAIL)
                return self.result


Handshake = AxisHandshake | _ResetHandshake  # 兩種交握共用的型別名稱


@dataclass
class _Operation:
    """進行中的一次操作；所有軸都有結果後由 _finish_operation 結束並清掉。"""

    kind: OperationKind  # 移動／步進／原點復歸／Reset Alarm
    axes: tuple[int, ...]  # 這次操作包含的全部軸
    active: dict[int, Handshake] = field(default_factory=dict)  # 已 start() 的交握，key 為軸號
    pending: list[Handshake] = field(default_factory=list)  # 第 7 軸完成後才開始
    results: dict[int, AxisResult] = field(default_factory=dict)  # 已結束的軸與結果


class MotionEngine:
    def __init__(self, config: AppConfig, port: PlcPort, clock: Callable[[], float]):
        self._config = config
        self._port = port
        self._clock = clock
        self._listeners: list[Listener] = []
        self._connected = False
        # 連線後第一次 tick 是否已執行；未執行前鎖定所有命令，因為還沒有快照可做前置檢查
        self._first_poll_done = False
        self._snapshot: Snapshot | None = None  # 最近一次 tick 的快照，命令的前置檢查用它
        self._op: _Operation | None = None  # 進行中的操作；None 表示沒有
        self._jog_held: tuple[int, int] | None = None  # (軸號, 方向)
        self._residual: list[ResidualInfo] = []  # 連線時偵測到、尚未處理的殘留交握

    # ---- 事件 ----

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def _emit(self, event: EngineEvent) -> None:
        for fn in self._listeners:
            fn(event)

    # ---- 連線與輪詢 ----

    def on_connected(self) -> None:
        """殘留交握（FR-57）與 JOG 殘留（FR-58）在之後的第一次 tick 檢查。"""
        self._connected = True
        self._first_poll_done = False
        self._snapshot = None
        self._residual = []

    def on_disconnected(self) -> None:
        """進行中操作的未完成軸一律以「通訊中斷」結束，不補寫（無法寫入，改由 FR-57 處理）。

        斷線前最後一次快照有軸 Busy = 1 或 JOG 按住中時，另發 `DisconnectedWhileMoving`。
        """
        moving = set()
        if self._snapshot is not None:
            moving = {n for n in self._config.axes if self._snapshot.axis(n).busy}
        if self._jog_held is not None:
            moving.add(self._jog_held[0])
        self._connected = False
        self._first_poll_done = False
        self._snapshot = None
        self._jog_held = None  # PLC 端若仍為 1，重新連線後由 FR-58 寫 0
        self._end_operation_comm_lost()
        if moving:
            self._emit(DisconnectedWhileMoving(tuple(sorted(moving))))

    def _end_operation_comm_lost(self) -> None:
        op = self._op
        if op is None:
            return
        lost = AxisResult(ResultKind.COMM_LOST)
        for hs in op.active.values():
            hs.abort(lost)
        # setdefault：已經有結果的軸保留原結果，其餘（含還在 pending 的軸）記為通訊中斷
        for n in op.axes:
            op.results.setdefault(n, lost)
        op.pending = []
        self._finish_operation()

    def tick(self, snapshot: Snapshot) -> None:
        """每輪輪詢呼叫一次：存下快照，連線後第一輪做殘留檢查，再推進進行中的操作。"""
        if not self._connected:
            return
        self._snapshot = snapshot
        if not self._first_poll_done:
            self._first_poll_done = True
            self._check_after_connect(snapshot)
        if self._op is not None:
            self._advance(snapshot)

    def _check_after_connect(self, snapshot: Snapshot) -> None:
        """連線後第一次輪詢：JOG = 1 立即寫 0（FR-58）；殘留交握只偵測、不寫入（FR-57）。"""
        residual = []
        for n in sorted(self._config.axes):
            st = snapshot.axis(n)
            # JOG 點仍為 1：可能是上次斷線時按住中，軸會一直動，所以不詢問直接寫 0
            held = [f"{JOG_TAGS[d]} {n}" for d, on in ((+1, st.jog_plus), (-1, st.jog_minus)) if on]
            for tag in held:
                self._port.write(tag, False)
            if held:
                self._emit(JogResidualCleared(n, tuple(held)))
            # Request 或 PC Action Code 不是 0：上次交握沒收完。只記錄並鎖定，
            # 由使用者在畫面上確認後呼叫 clear_residual() 歸零
            if st.request or st.pc_action_code != 0:
                residual.append(
                    ResidualInfo(
                        axis=n,
                        request=bool(st.request),
                        pc_action_code=st.pc_action_code,
                        target_position=snapshot.value(f"Target Position {n}"),
                    )
                )
        self._residual = residual
        if residual:
            self._emit(ResidualDetected(tuple(residual)))

    def _advance(self, snapshot: Snapshot) -> None:
        """推進進行中的操作，分三步：

        1. 逐軸推進：對還沒有結果的軸呼叫 advance()，有結果就記下
        2. 處理 pending（只有多軸原點復歸會有）：先做的第 7 軸結束後，
           成功就讓其餘軸開始，失敗則其餘軸記為「未執行」
        3. 所有軸都有結果：結束操作並發出 OperationFinished
        """
        op = self._op
        # 1. 逐軸推進（相當於 LabVIEW 用 For Loop 跑過每軸的狀態機一次）
        for n, hs in op.active.items():
            if n not in op.results:
                result = hs.advance(snapshot)
                if result is not None:
                    op.results[n] = result

        # 2. 有 pending 時，active 裡只有第 7 軸；它有結果後才決定 pending 的去向
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

        # 3. 全部軸都有結果才結束；任一軸還在跑，整個操作就還在進行中（維持全域鎖）
        if not op.pending and len(op.results) == len(op.axes):
            self._finish_operation()

    def _finish_operation(self) -> None:
        """清掉進行中的操作（解除全域鎖），並把各軸結果依軸號排序後發出。"""
        op = self._op
        self._op = None
        results = MappingProxyType({n: op.results[n] for n in sorted(op.results)})
        self._emit(OperationFinished(op.kind, results))

    # ---- 鎖定 ----

    def _global_lock_reasons(self) -> list[str]:
        """與軸狀態無關、會擋下所有命令的條件（全域動作鎖）。

        未連線、第一輪未完成時直接回傳：此時沒有快照，後面的檢查無從判斷。
        """
        if not self._connected:
            return [REASON_NOT_CONNECTED]
        if not self._first_poll_done:
            return [REASON_FIRST_POLL]
        reasons = []
        if self._residual:
            axes = "、".join(str(r.axis) for r in self._residual)
            reasons.append(f"有殘留交握尚未處理（第 {axes} 軸）")
        if self._op is not None:
            reasons.append(REASON_OPERATION)
        if self._jog_held is not None:
            reasons.append(f"{self._label(self._jog_held[0])}：JOG 按住中")
        return reasons

    def _busy_reasons(self, axes: Iterable[int]) -> list[str]:
        """列出指定軸中 Busy = 1 的軸。

        命令方法傳入「命令以外的軸」：任何一軸還在動時都不下新命令。
        命令本身的軸由 _axis_reasons 檢查 Busy，所以不重複列。
        """
        if self._snapshot is None:
            return []
        return [
            f"{self._label(n)}：Busy = 1（動作中）" for n in axes if self._snapshot.axis(n).busy
        ]

    @property
    def lock_reasons(self) -> list[str]:
        """目前的鎖定原因；PlcWorker 放進 EngineStatus 給畫面顯示與停用按鈕。"""
        return self._global_lock_reasons() + self._busy_reasons(sorted(self._config.axes))

    @property
    def locked(self) -> bool:
        return bool(self.lock_reasons)

    @property
    def jog_held_tag(self) -> str | None:
        """按住中的 JOG 點名稱（例如 "JOG Plus 3"）；沒有按住時 None。

        PlcWorker 在寫入被拒、斷線之前用它對該點寫 0（spec modbus-tags-migration FR-23）。
        """
        if self._jog_held is None:
            return None
        axis, direction = self._jog_held
        return f"{JOG_TAGS[direction]} {axis}"

    # ---- 命令 ----

    # 以下命令方法都回傳拒絕原因清單：空清單表示已接受

    def move(self, targets: dict[int, float], relative: bool, speed: int) -> list[str]:
        """多軸移動；targets 為 {軸號: 位置}，relative=True 時為相對目前位置的位移量。"""
        return self._move("移動", OperationKind.MOVE, targets, relative, speed)

    def step(self, axis: int, delta: float, speed: int) -> list[str]:
        """單軸步進：等同對一個軸做相對移動。"""
        return self._move("步進", OperationKind.STEP, {axis: delta}, True, speed)

    def home(self, axes: set[int]) -> list[str]:
        """原點復歸（Code 1）。多軸時第 7 軸（升降軸）先做，成功後其餘軸再一起做。"""
        command = "原點復歸"
        order = sorted(set(axes))
        reasons = self._common_reasons(order)
        if reasons:
            return self._reject(command, reasons)
        snap = self._snapshot
        for n in order:
            # 原點復歸本身不需要 Find Home Finish = 1
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
        # 包含第 7 軸且不只一軸：先只啟動第 7 軸，其餘放進 pending（由 _advance 接手）；
        # 否則全部同時啟動
        first = [LIFT_AXIS] if LIFT_AXIS in handshakes and len(order) > 1 else order
        self._start_operation(
            OperationKind.HOME,
            order,
            {"axes": tuple(order)},
            first=[handshakes[n] for n in first],
            pending=[handshakes[n] for n in order if n not in first],
        )
        return []

    def servo_all(self, on: bool) -> list[str]:
        """七軸 Servo Motor 同時寫 1／0（FR-24）；不屬於操作，不等結果。"""
        command = "全部 Servo ON" if on else "全部 Servo OFF"
        reasons = self.lock_reasons
        if reasons:
            return self._reject(command, reasons)
        for n in sorted(self._config.axes):
            self._port.write(f"Servo Motor {n}", bool(on))
        return []

    def reset_all(self) -> list[str]:
        """只對 Alarm Code ≠ 0 的軸做異常復歸（FR-25），以操作回報逐軸復歸成功／失敗。"""
        command = "全部 Reset Alarm"
        reasons = self.lock_reasons
        if reasons:
            return self._reject(command, reasons)
        order = [n for n in sorted(self._config.axes) if self._snapshot.axis(n).alarm_code != 0]
        if not order:
            return self._reject(command, [REASON_NO_ALARM])
        device = self._config.device
        handshakes = [
            _ResetHandshake(
                n,
                self._port,
                self._clock,
                device.reset_alarm_pulse_ms / 1000,
                device.handshake_timeout_s,
            )
            for n in order
        ]
        self._start_operation(OperationKind.RESET, order, {}, first=handshakes, pending=[])
        return []

    def jog_press(self, axis: int, direction: int, speed: int) -> list[str]:
        """FR-26 條件全部通過後寫 Speed、再寫 JOG = 1；到 jog_release 之前為鎖定。"""
        command = "JOG"
        reasons = self._global_lock_reasons()
        if axis not in self._config.axes:
            reasons.append(f"第 {axis} 軸不存在")
        if direction not in JOG_TAGS:
            reasons.append(f"JOG 方向只能是 +1 或 -1：{direction}")
        if reasons:
            return self._reject(command, reasons)
        snap = self._snapshot
        if not snap.estop_ok:
            reasons.append(REASON_ESTOP)
        st = snap.axis(axis)
        reasons += self._axis_reasons(axis, st, need_home=False)
        if direction > 0 and st.pos_limit_triggered:
            reasons.append(f"{self._label(axis)}：正極限已觸發")
        if direction < 0 and st.neg_limit_triggered:
            reasons.append(f"{self._label(axis)}：負極限已觸發")
        reasons += self._busy_reasons(n for n in sorted(self._config.axes) if n != axis)
        if reasons:
            return self._reject(command, reasons)

        speed = min(max(int(speed), SPEED_MIN), SPEED_MAX)
        # 先記錄按住中再寫入：寫入途中通訊中斷時 on_disconnected 才看得到
        self._jog_held = (axis, direction)
        self._port.write(f"Speed {axis}", speed)
        self._port.write(f"{JOG_TAGS[direction]} {axis}", True)
        return []

    def jog_release(self, axis: int, direction: int) -> None:
        """永遠執行（鎖定中也寫）：立即對該 JOG 點寫 0（FR-27）。

        未連線時無法寫入，只解除按住狀態；PLC 端殘留的 1 由重新連線後的 FR-58 處理。
        """
        if self._jog_held == (axis, direction):
            self._jog_held = None
        if self._connected:
            self._port.write(f"{JOG_TAGS[direction]} {axis}", False)

    def write_point(self, tag: str, value: int | float | bool | str) -> list[str]:
        """IO Monitor 的單點寫入；值的範圍檢查由畫面負責。"""
        command = "IO 寫入"
        reasons = self.lock_reasons
        try:
            point = self._config.points.get(tag)
        except KeyError:
            reasons.append(f"{tag}：點位不存在")
        else:
            if point.access != Access.READ_WRITE:
                reasons.append(f"{tag}：Read Only 點不可寫入")
        if reasons:
            return self._reject(command, reasons)
        self._port.write(tag, value)
        return []

    # ---- 殘留交握（FR-57） ----

    def residual_axes(self) -> list[ResidualInfo]:
        return list(self._residual)

    def clear_residual(self) -> None:
        """對殘留軸寫 Request = 0、PC Action Code = 0，全部寫完才解除該鎖定條件。"""
        for info in self._residual:
            self._port.write(f"Request {info.axis}", False)
            self._port.write(f"PC Action Code {info.axis}", 0)
        self._residual = []

    def _move(
        self,
        command: str,
        kind: OperationKind,
        targets: Mapping[int, float],
        relative: bool,
        speed: int,
    ) -> list[str]:
        """move 與 step 共用：前置檢查 → 換算絕對目標 → 每軸建一個 Code 2 交握並同時啟動。"""
        order = sorted(targets)
        # 全域條件不通過就不再逐軸檢查（例如未連線時沒有快照可查）
        reasons = self._common_reasons(order)
        if reasons:
            return self._reject(command, reasons)
        snap = self._snapshot
        absolute: dict[int, float] = {}  # 各軸的絕對目標（實際單位 mm／deg）
        for n in order:
            st = snap.axis(n)
            # 逐軸檢查會累加所有不通過的項目，讓使用者一次看到全部原因
            reasons += self._axis_reasons(n, st, need_home=True)
            # 相對移動以目前位置換算成絕對目標：PLC 只支援 Code 2 絕對移動
            target = st.position + targets[n] if relative else float(targets[n])
            absolute[n] = target
            limit = self._config.axes[n]  # axes.toml 的軟體極限
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
                target_raw=to_raw(absolute[n], self._scale(n)),  # 乘上該軸倍率轉成整數
                speed=speed,
            )
            for n in order
        ]
        # 放進 OperationStarted 事件，畫面與日誌用來顯示這次命令的內容
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
        """單軸能不能下命令；need_home=True 時另要求已原點復歸（移動、步進用）。"""
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
        """發出拒絕事件（畫面顯示原因）並回傳原因清單。"""
        self._emit(CommandRejected(command, tuple(reasons)))
        return reasons

    def _start_operation(
        self,
        kind: OperationKind,
        axes: list[int],
        params: dict,
        first: list[Handshake],
        pending: list[Handshake],
    ) -> None:
        """建立操作並啟動 first 裡的交握；pending 的交握等 _advance 決定何時啟動。"""
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
