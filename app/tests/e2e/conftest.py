"""端到端測試共用：在測試程序內啟動 SimServer＋真 MotionApp（主視窗、worker、系統匣），走 TCP。

觀測點（spec Acceptance Criteria 的觀測點說明）：
- 模擬器紀錄：`SimServer.log`（寫入、連線、斷線、狀態變化，各帶時間）
- 模擬器狀態：`PlcModel`（一律在 `server.lock` 內讀寫）
- 畫面：MainWindow 與各頁的控制項；操作以 pytest-qt 點擊
- 日誌檔：`setup_logging` 指到 tmp 的 logs 資料夾

設定一律使用 repo 預設 `app/config` 在 tmp 的複本。「重新啟動」以在同一程序內重建 MotionApp 模擬
（plan 允許），PLC 端是同一個保留狀態的 SimServer。
"""

import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Slot
from PySide6.QtWidgets import QApplication, QTableWidget, QWidget

from plc_motion.app import MotionApp
from plc_motion.engine import CommandRejected, OperationFinished
from plc_motion.logging_setup import close_logging, log_file_name, setup_logging
from plc_motion.plc import PlcModel, Snapshot
from plc_motion.settings import load_config
from plc_motion.settings.loader import AppConfig
from plc_motion.sim import LogEntry, SimServer
from plc_motion.worker import EngineStatus

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
HOST = "127.0.0.1"
AXES = tuple(range(1, 8))


@dataclass(frozen=True)
class Sample:
    """每次 snapshot_ready（畫面已依這次狀態更新之後）記下的畫面狀態。"""

    time: float
    snapshot: Snapshot | None
    status: EngineStatus
    enabled: frozenset[str]  # 可用的寫入控制項
    tray_state: object


class _Probe(QObject):
    """住在 UI 執行緒：worker signal 以 queued 送達，與畫面收到的順序相同。"""

    def __init__(self, harness: Harness):
        super().__init__()
        self._harness = harness

    @Slot(object, object)
    def on_snapshot(self, snapshot, status) -> None:
        self._harness._sample(snapshot, status)

    @Slot(object)
    def on_event(self, event) -> None:
        self._harness.events.append(event)


class SimAction:
    """模擬器操作員：另一條執行緒等條件成立後操作模擬器（plc-sim 是獨立程序，不受畫面影響）。"""

    def __init__(self, cond: Callable[[], bool], action: Callable[[], None], timeout: float):
        self.done_at: float | None = None
        self.error: BaseException | None = None
        self._cond, self._action, self._timeout = cond, action, timeout
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        deadline = time.monotonic() + self._timeout
        try:
            while not self._cond():
                if time.monotonic() > deadline:
                    raise TimeoutError("模擬器操作的條件在時限內未成立")
                time.sleep(0.002)
            self._action()
            self.done_at = time.time()
        except BaseException as e:
            self.error = e

    def done(self) -> bool:
        return not self._thread.is_alive()

    def check(self) -> float:
        self._thread.join(self._timeout + 1)
        if self.error is not None:
            raise self.error
        assert self.done_at is not None
        return self.done_at


class Harness:
    def __init__(self, qtbot, config: AppConfig, sim: SimServer, logs_dir: Path):
        self.qtbot = qtbot
        self.config = config
        self.sim = sim
        self.logs_dir = logs_dir
        self.apps: list[MotionApp] = []
        self.app: MotionApp | None = None
        self.events: list = []
        self.samples: list[Sample] = []
        self._probe = _Probe(self)

    # ---- 程式生命週期 ----

    def start(self, port: int | None = None) -> MotionApp:
        """建立並顯示 MotionApp（等同啟動 plc-motion --host 127.0.0.1 --port <port>）。"""
        app = MotionApp(self.config, HOST, self.sim.port if port is None else port)
        # 在回到事件迴圈之前接好 signal；接在畫面之後，取樣時畫面已更新
        app.worker.engine_event.connect(self._probe.on_event)
        app.worker.snapshot_ready.connect(self._probe.on_snapshot)
        app.show()
        self.qtbot.waitExposed(app.window)
        self.apps.append(app)
        self.app = app
        return app

    def quit_from_tray(self) -> None:
        """系統匣選單「結束」。"""
        app = self.app
        app.tray.action_quit.trigger()
        self.until(lambda: app.thread.isFinished())

    def close_all(self) -> None:
        apps, self.apps = self.apps, []
        for app in apps:
            app.shutdown()
            app.window.deleteLater()

    def restart(self) -> MotionApp:
        """結束目前的程式，從同一個設定資料夾重新載入設定後再啟動（同程序內模擬重新啟動）。"""
        self.close_all()
        self.config = load_config(self.config.config_dir)
        return self.start()

    def _sample(self, snapshot, status) -> None:
        self.samples.append(
            Sample(time.time(), snapshot, status, self.enabled_controls(), self.app.tray.state)
        )

    # ---- 等待 ----

    def until(self, cond, timeout: int = 15000) -> None:
        self.qtbot.waitUntil(lambda: bool(cond()), timeout=timeout)

    def settle(self, ms: int) -> None:
        self.qtbot.wait(ms)

    @property
    def status(self) -> EngineStatus | None:
        return self.app.last_status

    def connected(self) -> bool:
        s = self.app.last_status
        return s is not None and s.connected and self.app.snapshot is not None

    def wait_connected(self, timeout: int = 15000) -> None:
        self.until(self.connected, timeout)

    def wait_unlocked(self, timeout: int = 15000) -> None:
        self.until(lambda: self.connected() and not self.app.last_status.locked, timeout)

    def wait_finished(self, since: int, timeout: int = 20000) -> OperationFinished:
        self.until(lambda: self.finished(since), timeout)
        return self.finished(since)[0]

    def finished(self, since: int = 0) -> list[OperationFinished]:
        return [e for e in self.events[since:] if isinstance(e, OperationFinished)]

    def rejected(self, since: int = 0) -> list[CommandRejected]:
        return [e for e in self.events[since:] if isinstance(e, CommandRejected)]

    # ---- 模擬器 ----

    def prepare(self, servo=AXES, homed=AXES, positions: dict[int, float] | None = None) -> None:
        """測試前置：直接在模擬 PLC 端 Servo ON、設 Find Home Finish、設位置（不經 Modbus，
        不出現在寫入紀錄）。"""
        with self.sim.lock:
            m = self.sim.model
            for n in servo:
                m.master_write(f"Servo Motor {n}", True)
            for n in homed:
                m.master_write(f"Find Home Finish {n}", True)
            for n, value in (positions or {}).items():
                m.set_position(n, value)

    def when(self, cond, action, timeout: float = 15.0) -> SimAction:
        """條件成立時（另一條執行緒，每 2 ms 檢查）操作模擬器；回傳後以 `check()` 取完成時間。"""
        return SimAction(cond, action, timeout)

    def logged(self, kind: str, tag: str, value, since: int = 0) -> Callable[[], bool]:
        return lambda: bool(self.find(kind, tag, value, since))

    def sim_call(self, method: str, *args, **kwargs):
        """在 server.lock 內呼叫 PlcModel 的 FR-53 操作（等同在 plc-sim 視窗操作）。"""
        with self.sim.lock:
            return getattr(self.sim.model, method)(*args, **kwargs)

    def model(self, tag: str):
        with self.sim.lock:
            return self.sim.model.read(tag)

    def mark(self) -> int:
        return len(self.sim.log.entries())

    def entries(self, since: int = 0) -> list[LogEntry]:
        return self.sim.log.entries()[since:]

    def writes(self, since: int = 0) -> list[tuple[str, int | bool]]:
        return [(e.tag, e.value) for e in self.entries(since) if e.kind == "write"]

    def write_entries(self, since: int = 0) -> list[LogEntry]:
        return [e for e in self.entries(since) if e.kind == "write"]

    def find(self, kind: str, tag: str, value, since: int = 0) -> list[int]:
        """符合條件的紀錄在 entries() 中的絕對索引。"""
        return [
            i
            for i, e in enumerate(self.entries())
            if i >= since and e.kind == kind and e.tag == tag and e.value == value
        ]

    def wait_entry(self, kind: str, tag: str, value, since: int = 0, timeout: int = 15000) -> int:
        self.until(lambda: self.find(kind, tag, value, since), timeout)
        return self.find(kind, tag, value, since)[0]

    def entry(self, index: int) -> LogEntry:
        return self.entries()[index]

    # ---- 畫面操作 ----

    def click(self, widget: QWidget) -> None:
        self.qtbot.mouseClick(widget, Qt.MouseButton.LeftButton)

    def click_row(self, n: int) -> None:
        """點選第 n 軸的位置卡為選定軸。"""
        card = self.app.axis_page.cards[n]
        self.qtbot.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))

    SCRATCH_POINT = 199  # P200：多軸移動用的暫存點位

    def move(self, targets: dict[int, float], relative: bool = False, speed: int = 50) -> None:
        """單軸：選定軸的目標位置＋「移動」；多軸：寫進 P200 後用點位表「移動到此點」。

        確認視窗由 `confirms` fixture 自動回答確認。多軸只支援絕對移動（介面只有點位表能多軸）。
        """
        page = self.app.axis_page
        if len(targets) == 1:
            ((n, value),) = targets.items()
            self.click_row(n)
            self.click(page.relative_button if relative else page.absolute_button)
            page.target.setValue(value)
            page.jog_pad.speed.setValue(speed)
            self.click(page.move_button)
            return
        assert not relative, "多軸移動只有點位表，只能絕對"
        panel = page.points_panel
        fields = {1: "x", 2: "y", 3: "z", 4: "rx", 5: "ry", 6: "rz", 7: "lift"}
        point_id = panel._positions.rows()[self.SCRATCH_POINT].point_id
        panel._positions.update(point_id, **{fields[n]: v for n, v in targets.items()})
        panel.reload()
        panel.table.selectRow(self.SCRATCH_POINT)
        for n, chip in panel.axis_checks.items():
            if chip.isChecked() != (n in targets):
                self.click(chip)
        panel.speed.setValue(speed)
        self.click(panel.move_button)

    def write_controls(self) -> dict[str, QWidget]:
        page = self.app.axis_page
        panel = page.points_panel
        controls: dict[str, QWidget] = {
            "全部 Servo ON": page.servo_on_button,
            "全部 Servo OFF": page.servo_off_button,
            "全部 Reset Alarm": page.reset_button,
            "目標位置移動": page.move_button,
            "單軸原點復歸": page.home_one_button,
            "全部原點復歸": page.home_all_button,
            "JOG −": page.jog_minus,
            "JOG ＋": page.jog_plus,
            "點位移動": panel.move_button,
            "存入目前位置": panel.save_button,
            "編輯點位": panel.edit_button,
            "清除點位": panel.clear_button,
            "匯入點位": panel.import_button,
        }
        for (n, d), button in page.jog_pad.step_buttons.items():
            controls[f"步進 {n}{'+' if d > 0 else '-'}"] = button
        return controls

    def enabled_controls(self) -> frozenset[str]:
        names = {name for name, w in self.write_controls().items() if w.isEnabled()}
        if self.app.io_page._can_write():
            names.add("IO 寫入")
        return frozenset(names)

    def show_io(self, tag: str) -> tuple[QTableWidget, int]:
        """切到 IO Monitor 頁、該點所在分頁，回傳 (表格, 列)。"""
        window, page = self.app.window, self.app.io_page
        self.click(window.nav.buttons[window.pages.indexOf(page)])
        page.show_kind(page.kind_of(tag))
        table, row = page.locate(tag)
        table.scrollToItem(table.item(row, 0))
        return table, row

    def click_cell(self, table: QTableWidget, row: int, col: int, double: bool = False) -> None:
        """在表格的一格上點一下；double 時接著雙擊（pytest-qt 的雙擊前要先單擊）。"""
        table.scrollToItem(table.item(row, col))
        pos = table.visualItemRect(table.item(row, col)).center()
        self.qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        if double:
            self.qtbot.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=pos)

    def answer_modal(
        self, kind: type, handler: Callable, timeout_ms: int = 10000, popup: bool = False
    ) -> list:
        """之後出現 `kind` 型別的 modal 視窗（popup 時為彈出選單）時呼叫 handler(視窗)，
        回傳收到的視窗清單。

        以 QTimer 輪詢（modal 視窗與選單的 exec() 會跑巢狀事件迴圈，計時器照樣觸發）；
        時限內沒出現就停止輪詢，預期不開視窗的測試據此斷言清單為空。
        """
        seen: list = []
        deadline = time.monotonic() + timeout_ms / 1000

        def poll() -> None:
            if popup:
                widget = QApplication.activePopupWidget()
            else:
                widget = QApplication.activeModalWidget()
            if isinstance(widget, kind) and widget not in seen:
                seen.append(widget)
                handler(widget)
                return
            if time.monotonic() < deadline:
                QTimer.singleShot(20, poll)

        QTimer.singleShot(0, poll)
        return seen

    def messages(self) -> list[str]:
        return [m.detail for m in self.app.window.messages]

    def log_text(self) -> str:
        path = self.logs_dir / log_file_name(date.today())
        return path.read_bytes().decode("utf-8") if path.exists() else ""


@pytest.fixture
def e2e_config_dir(tmp_path: Path) -> Path:
    """repo 預設 config 的複本（不修改 repo 的 app/config）。"""
    d = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, d)
    return d


@pytest.fixture
def config(e2e_config_dir: Path) -> AppConfig:
    return load_config(e2e_config_dir)


@pytest.fixture
def logs_dir(tmp_path: Path):
    d = tmp_path / "logs"
    setup_logging(d, 30)
    yield d
    close_logging()


@pytest.fixture
def sim(config: AppConfig):
    server = SimServer(PlcModel(config.points), config.points, HOST, 0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def e2e(qtbot, config, sim, logs_dir):
    harness = Harness(qtbot, config, sim, logs_dir)
    yield harness
    harness.close_all()
