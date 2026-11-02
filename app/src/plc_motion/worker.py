"""背景通訊 worker（spec FR-10 ~ FR-14、FR-27、FR-49、FR-50）。

`PlcWorker` 住在自己的 QThread：`ModbusLink` 的讀寫、`MotionEngine` 的建立與每一次呼叫
都在這條執行緒內。UI 只呼叫命令方法（任何執行緒皆可，內部以 queued signal 轉派）並接收
signal。一切在 worker 的事件迴圈裡依序執行，所以引擎命令必定落在某次 tick 之後、下一次
read_all 之前；JOG 放開等命令不必等輪詢計時器，排到的下一個事件就執行。

每輪輪詢量測 `read_all` 的耗時（依序送完所有讀取請求）與相鄰兩輪開始的間隔，以 `poll_timed` 發出，
供狀態列判斷通訊品質與調整輪詢週期；連線後第一輪沒有間隔值。

通訊中斷：`PlcCommError`（含 Modbus 例外回應）與任何未預期例外都視為中斷——關閉連線、
`engine.on_disconnected()`、發 `connection_changed(False)`；不自動重連，等 `connect_plc()`。
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from plc_motion.codec import decimals_for
from plc_motion.engine import (
    CommandRejected,
    DisconnectedWhileMoving,
    EngineEvent,
    JogResidualCleared,
    MotionEngine,
    Notice,
    OperationFinished,
    OperationKind,
    OperationStarted,
    ResidualDetected,
    ResidualInfo,
)
from plc_motion.plc.modbus_link import ModbusLink
from plc_motion.plc.port import PlcCommError
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig

log = logging.getLogger("plc_motion.worker")


@dataclass(frozen=True)
class EngineStatus:
    locked: bool
    lock_reasons: tuple[str, ...]
    connected: bool
    operating: bool  # 有操作（移動、步進、原點復歸、Reset Alarm）進行中
    residual: tuple[ResidualInfo, ...]  # 尚未處理的殘留交握（FR-57）


@dataclass(frozen=True)
class PollTiming:
    read_ms: float  # 本輪讀完全部位址花的時間
    cycle_ms: float | None  # 與上一輪開始的間隔；連線後第一輪為 None
    requests: int  # 本輪送出的讀取請求數


class PlcWorker(QObject):
    # 跨執行緒 signal；接收端在 UI 執行緒時為 queued。
    # snapshot_ready 每次輪詢與每個命令之後發出；未連線時 Snapshot 為 None。
    snapshot_ready = Signal(object, object)  # (Snapshot | None, EngineStatus)
    engine_event = Signal(object)  # EngineEvent
    connection_changed = Signal(bool)
    poll_timed = Signal(object)  # PollTiming，每次輪詢成功後發出

    _call = Signal(object)  # 內部：把命令轉派到 worker 執行緒

    def __init__(self, config: AppConfig, host: str, port: int):
        super().__init__()
        self._config = config
        self._host = host
        self._port = port
        self._link: ModbusLink | None = None
        self._engine: MotionEngine | None = None
        self._timer: QTimer | None = None
        self._connected = False
        self._operating = False
        self._stopped = False
        self._snapshot: Snapshot | None = None
        self._prev: Snapshot | None = None  # 比較 Alarm Code 與 Emergency Stop 變化用
        self._last_poll_start: float | None = None
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    # ---- 命令方法（UI 入口） ----

    def connect_plc(self) -> None:
        self._post(self._do_connect)

    def move(self, targets, relative, speed) -> None:
        self._post(lambda: self._engine.move(dict(targets), bool(relative), speed))

    def step(self, axis, delta, speed) -> None:
        self._post(lambda: self._engine.step(axis, delta, speed))

    def home(self, axes) -> None:
        self._post(lambda: self._engine.home(set(axes)))

    def servo_all(self, on) -> None:
        self._post(lambda: self._do_servo_all(bool(on)))

    def reset_all(self) -> None:
        self._post(lambda: self._engine.reset_all())

    def jog_press(self, axis, direction, speed) -> None:
        self._post(lambda: self._do_jog_press(axis, direction, speed))

    def jog_release(self, axis, direction) -> None:
        self._post(lambda: self._do_jog_release(axis, direction))

    def write_point(self, tag, value) -> None:
        self._post(lambda: self._do_write_point(tag, value))

    def clear_residual(self) -> None:
        self._post(self._do_clear_residual)

    def stop(self) -> None:
        """關閉連線並結束 worker 執行緒；呼叫端之後 `thread.wait()`。"""
        self._post(self._do_stop)

    # ---- worker 執行緒 ----

    def _post(self, fn: Callable[[], object]) -> None:
        self._call.emit(fn)

    @Slot()
    def _on_started(self) -> None:
        self._link = ModbusLink(
            self._config.points,
            self._host,
            self._port,
            self._config.device.connect_timeout_ms,
            self._config.read_ranges,
        )
        self._engine = MotionEngine(self._config, self._link, time.monotonic)
        self._engine.add_listener(self._on_engine_event)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self._config.device.poll_interval_ms)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

    @Slot(object)
    def _run(self, fn: Callable[[], object]) -> None:
        if self._stopped:
            return
        self._guard(fn)
        if not self._stopped:
            self._emit_status()

    @Slot()
    def _on_timer(self) -> None:
        if not self._stopped:
            self._guard(self._poll)

    def _guard(self, fn: Callable[[], object]) -> None:
        try:
            fn()
        except PlcCommError as e:
            log.error("通訊錯誤：%s", e)
            self._disconnect(f"通訊錯誤：{e}")
        except Exception as e:
            log.exception("背景通訊發生未預期的例外，視同通訊中斷")
            self._disconnect(f"未預期的例外：{e!r}")

    def _poll(self) -> None:
        if not self._connected:
            return
        start = time.perf_counter()
        snapshot = self._link.read_all()
        read_ms = (time.perf_counter() - start) * 1000
        last, self._last_poll_start = self._last_poll_start, start
        cycle_ms = (start - last) * 1000 if last is not None else None
        self._snapshot = snapshot
        self._log_changes(snapshot)
        self._engine.tick(snapshot)
        self._emit_status()
        self.poll_timed.emit(PollTiming(read_ms, cycle_ms, self._link.request_count))

    def _do_connect(self) -> None:
        if self._connected:
            self._disconnect("重新連線")
        try:
            self._link.connect()
        except Exception as e:
            if isinstance(e, PlcCommError):
                log.warning("連線失敗：%s", e)
            else:
                log.exception("連線時發生未預期的例外")
            try:
                self._link.close()
            except Exception:
                log.exception("關閉連線時發生例外")
            self.connection_changed.emit(False)
            return
        log.info("已連線到 PLC %s:%s", self._host, self._port)
        self._connected = True
        self._prev = None
        self._last_poll_start = None
        self._engine.on_connected()
        self.connection_changed.emit(True)
        self._poll()  # 不等計時器：連線後第一次輪詢立即完成

    def _disconnect(self, reason: str) -> None:
        if not self._connected:
            return
        self._connected = False
        self._snapshot = None
        self._prev = None
        try:
            self._link.close()
        except Exception:
            log.exception("關閉連線時發生例外")
        log.warning("與 PLC 斷線：%s", reason)
        try:
            self._engine.on_disconnected()
        except Exception:
            log.exception("處理斷線時發生未預期的例外")
        self.connection_changed.emit(False)
        self._emit_status()

    def _do_stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer.stop()
        if self._connected:
            self._connected = False
            log.info("程式結束，關閉 PLC 連線")
        if self._link is not None:
            self._link.close()
        QThread.currentThread().quit()

    def _do_servo_all(self, on: bool) -> None:
        if not self._engine.servo_all(on):
            log.info("全部 Servo %s", "ON" if on else "OFF")

    def _do_jog_press(self, axis: int, direction: int, speed: int) -> None:
        if not self._engine.jog_press(axis, direction, speed):
            log.info("JOG 按下：第 %s 軸 %s，速度 %s", axis, _dir_text(direction), speed)

    def _do_jog_release(self, axis: int, direction: int) -> None:
        self._engine.jog_release(axis, direction)
        log.info("JOG 放開：第 %s 軸 %s", axis, _dir_text(direction))

    def _do_write_point(self, tag: str, value: int | bool) -> None:
        if not self._engine.write_point(tag, value):
            log.info("IO 寫入：%s = %s", tag, int(value))

    def _do_clear_residual(self) -> None:
        if not self._connected:
            log.warning("未連線，無法處理殘留交握")
            return
        axes = [r.axis for r in self._engine.residual_axes()]
        self._engine.clear_residual()
        if axes:
            text = "、".join(str(n) for n in axes)
            log.info("殘留交握已寫回 0（Request、PC Action Code）：第 %s 軸", text)

    def _emit_status(self) -> None:
        reasons = tuple(self._engine.lock_reasons)
        status = EngineStatus(
            locked=bool(reasons),
            lock_reasons=reasons,
            connected=self._connected,
            operating=self._operating,
            residual=tuple(self._engine.residual_axes()),
        )
        self.snapshot_ready.emit(self._snapshot, status)

    # ---- 日誌（FR-49） ----

    def _log_changes(self, snap: Snapshot) -> None:
        """Alarm Code 與 Emergency Stop 的變化；連線後第一次輪詢記錄非正常的初始值。"""
        prev, self._prev = self._prev, snap
        for n in sorted(self._config.axes):
            code = snap.axis(n).alarm_code
            if prev is None:
                if code != 0:
                    log.warning("連線時 Alarm Code %s = %s", n, code)
            elif (old := prev.axis(n).alarm_code) != code:
                level = logging.WARNING if code != 0 else logging.INFO
                log.log(level, "Alarm Code %s：%s → %s", n, old, code)
        if prev is None:
            if not snap.estop_ok:
                log.warning("連線時 Emergency Stop：觸發")
        elif prev.estop_ok != snap.estop_ok:
            if snap.estop_ok:
                log.info("Emergency Stop：解除")
            else:
                log.warning("Emergency Stop：觸發")

    def _on_engine_event(self, event: EngineEvent) -> None:
        if isinstance(event, OperationStarted):
            self._operating = True
            log.info("操作開始：%s，%s", event.kind.value, self._params_text(event))
        elif isinstance(event, OperationFinished):
            self._operating = False
            results = "、".join(f"第 {n} 軸：{r.text}" for n, r in event.results.items())
            log.info("操作結束：%s，%s", event.kind.value, results)
        elif isinstance(event, CommandRejected):
            log.warning("命令被拒絕：%s：%s", event.command, "；".join(event.reasons))
        elif isinstance(event, ResidualDetected):
            items = "；".join(
                f"第 {r.axis} 軸 Request = {int(r.request)}、PC Action Code = "
                f"{r.pc_action_code}、Target Position = {self._fmt(r.axis, r.target_position)}"
                for r in event.axes
            )
            log.warning("偵測到殘留交握：%s", items)
        elif isinstance(event, JogResidualCleared):
            log.warning(
                "第 %s 軸 JOG 於斷線期間未放開，已寫回 0（%s）", event.axis, "、".join(event.tags)
            )
        elif isinstance(event, DisconnectedWhileMoving):
            axes = "、".join(str(n) for n in event.axes)
            log.warning("斷線前第 %s 軸動作中，軸可能仍在移動，請按急停", axes)
        elif isinstance(event, Notice):
            log.info("%s", event.text)
        self.engine_event.emit(event)

    def _params_text(self, event: OperationStarted) -> str:
        p = event.params
        if event.kind in (OperationKind.MOVE, OperationKind.STEP):
            mode = "相對" if p["relative"] else "絕對"
            items = "、".join(
                f"第 {n} 軸 輸入 {self._fmt(n, p['input'][n])} → 目標 {self._fmt(n, t)}"
                for n, t in sorted(p["targets"].items())
            )
            return f"{mode}，{items}，速度 {p['speed']}"
        return "軸 " + "、".join(str(n) for n in event.axes)

    def _fmt(self, n: int, value: float) -> str:
        point = self._config.points.axis_tag("Current Position", n)
        return f"{value:.{decimals_for(point.scale)}f} {point.unit}"


def _dir_text(direction: int) -> str:
    return "+" if direction > 0 else "-"


def start_worker(config: AppConfig, host: str, port: int) -> tuple[QThread, PlcWorker]:
    """建立並啟動 worker 執行緒；host／port 為本次連線用（命令列覆寫），不寫回 device.toml。

    自動連線（FR-10）排在呼叫端執行緒的事件迴圈：呼叫端在回到事件迴圈之前接好的 signal
    都收得到第一次的 `connection_changed`。
    """
    thread = QThread()
    thread.setObjectName("plc-worker")
    worker = PlcWorker(config, host, port)
    worker.moveToThread(thread)
    thread.started.connect(worker._on_started)
    thread.start()
    QTimer.singleShot(0, lambda: worker.connect_plc())
    return thread, worker
