"""MainWindow 與 MotionApp：導覽、工具列、狀態列、事件顯示、主題、縮到系統匣、結束、例外視窗
（spec FR-8 錯誤視窗以外的 FR-41 ~ FR-47、FR-50、FR-21／FR-22／FR-25 的畫面呈現）。

顯示面以假 worker 送 signal；「縮到系統匣仍輪詢」「結束不寫任何點」以真 PlcWorker 連 SimServer。
"""

import logging
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QProgressBar, QWidget
from ui_support import answer_next_message_box, status

from plc_motion.app import MotionApp
from plc_motion.engine import (
    AxisResult,
    CommandRejected,
    Notice,
    OperationFinished,
    OperationKind,
    ResultKind,
    WaitPoint,
)
from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.sim import SimServer
from plc_motion.ui.axis_page import AxisPage
from plc_motion.ui.comm import CommStats, load_state
from plc_motion.ui.dialogs import ExceptionReporter
from plc_motion.ui.main_window import MainWindow
from plc_motion.ui.points_panel import DESC_COL
from plc_motion.ui.theme import apply_theme, load_qss
from plc_motion.worker import PollTiming


def until(qtbot, cond, timeout: int = 5000) -> None:
    qtbot.waitUntil(lambda: bool(cond()), timeout=timeout)


@pytest.fixture
def window(qtbot, config, fake_worker):
    w = MainWindow(config, fake_worker, "127.0.0.1:5020")
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.allow_close()
    w.close()


@pytest.fixture(autouse=True)
def restore_stylesheet():
    app = QApplication.instance()
    sheet = app.styleSheet()
    yield
    app.setStyleSheet(sheet)


# ---- 版面 ----


def test_minimum_size_is_1600x900(window):
    assert window.minimumSize().width() == 1600
    assert window.minimumSize().height() == 900


def test_axis_page_layout_readable_at_minimum_size(window, config, fake_worker, qtbot):
    """1600×900 下：七張位置卡、三欄與點位操作都看得到；點位表不捲動即可看到 點位 ~ 升降 欄。"""
    apply_theme(QApplication.instance())
    page = AxisPage(config, fake_worker)
    window.add_page("軸控", None, page)
    window.resize(1600, 900)
    qtbot.wait(50)
    panel = page.points_panel

    names = [box.text() for box in panel.axis_checks.values()]
    assert names == [config.axes[n].name for n in sorted(config.axes)]
    for widget in (*panel.axis_checks.values(), panel.move_button, panel.clear_button):
        assert widget.isVisible()
        assert widget.width() >= widget.sizeHint().width(), widget.text()
    for button in (page.servo_on_button, page.servo_off_button, page.reset_button):
        assert button.width() >= button.sizeHint().width(), button.text()
    for widget in (page.reset_button, page.home_all_button, page.jog_pad.step_buttons[(6, 1)]):
        bottom = widget.mapTo(page, widget.rect().bottomLeft()).y()
        assert bottom <= page.height(), widget.text()  # 不被切掉
    assert all(card.isVisible() for card in page.cards.values())

    table = panel.table
    lift_col = DESC_COL - 1
    right = table.columnViewportPosition(lift_col) + table.columnWidth(lift_col)
    assert table.horizontalScrollBar().value() == 0
    assert right <= table.viewport().width()


def test_title_is_traditional_chinese(window):
    assert window.windowTitle() == "PLC 七軸運動控制"


def test_add_page_adds_nav_entry_and_switches(window, qtbot):
    a, b = QLabel("A"), QLabel("B")
    window.add_page("軸控", None, a)
    window.add_page("IO Monitor", None, b)
    assert [btn.text() for btn in window.nav.buttons] == ["軸控", "IO Monitor"]
    assert window.pages.currentWidget() is a
    window.nav.buttons[1].click()
    assert window.pages.currentWidget() is b


def test_nav_shows_icon_above_name(window):
    window.add_page("IO Monitor", None, QWidget())
    button = window.nav.buttons[0]
    assert button.text() == "IO Monitor"
    assert button.toolButtonStyle().name == "ToolButtonTextUnderIcon"
    assert not button.icon().isNull()
    assert window.nav.width() == 92


# ---- 工具列 ----


def test_toolbar_has_only_connection_and_estop(window, fake_worker, make_snapshot):
    """Servo、Reset Alarm 移到軸控頁；頂列只剩連線、重新連線、急停，且沒有主題切換。"""
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    window.reconnect_button.click()
    assert fake_worker.calls == [("connect_plc",)]
    for name in ("servo_on_button", "servo_off_button", "reset_button", "theme_button"):
        assert not hasattr(window, name)
    fake_worker.snapshot_ready.emit(None, status(locked=True, connected=False))
    assert window.reconnect_button.isEnabled()  # 重新連線不受鎖定影響


def test_connection_status(window, fake_worker):
    fake_worker.connection_changed.emit(True)
    assert "已連線" in window.connection_label.text()
    assert "127.0.0.1:5020" in window.connection_label.text()
    fake_worker.connection_changed.emit(False)
    assert "未連線" in window.connection_label.text()


def test_estop_status(window, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    assert "正常" in window.estop_label.text()
    fake_worker.snapshot_ready.emit(make_snapshot({"Emergency Stop": False}), status())
    assert "觸發" in window.estop_label.text()
    fake_worker.snapshot_ready.emit(None, status(locked=True, connected=False))
    assert "正常" not in window.estop_label.text() and "觸發" not in window.estop_label.text()


# ---- 狀態列與事件 ----


def test_status_bar_comm_timing(window, fake_worker):
    bar = window.comm_indicator
    assert bar.read_value.text() == "—"  # 尚未輪詢
    fake_worker.connection_changed.emit(True)
    fake_worker.poll_timed.emit(PollTiming(18.4, None, 37))
    fake_worker.poll_timed.emit(PollTiming(42.0, 101.0, 37))
    fake_worker.poll_timed.emit(PollTiming(20.0, 100.0, 37))
    assert bar.read_value.text() == "20 ms"
    assert bar.average_value.text() == "27 ms"
    assert bar.max_value.text() == "42 ms"
    assert bar.requests_value.text() == "37"
    assert "10%" in bar.toolTip()  # 20 / 200 ms，只在滑鼠提示
    assert not any("%" in w.text() for w in bar.findChildren(QLabel))  # 不顯示百分比數字
    assert not bar.findChildren(QProgressBar)  # 不顯示進度條
    assert bar.dot.property("state") == "ok"
    fake_worker.poll_timed.emit(PollTiming(170.0, 200.0, 37))  # > 80 %
    assert bar.dot.property("state") == "warn"
    fake_worker.poll_timed.emit(PollTiming(230.0, 230.0, 37))  # > 週期
    assert bar.dot.property("state") == "fault"
    assert "115%" in bar.toolTip()


def test_comm_dialog_stats_and_reset(window, fake_worker):
    fake_worker.connection_changed.emit(True)
    for ms in (10, 20, 30):
        fake_worker.poll_timed.emit(PollTiming(float(ms), 200.0, 37))
    window.comm_indicator.clicked.emit()
    dialog = window.comm_dialog
    assert dialog.isVisible()
    assert dialog.tiles["average"].text() == "20 ms"
    assert dialog.tiles["minimum"].text() == "10 ms"
    assert dialog.tiles["maximum"].text() == "30 ms"
    assert dialog.tiles["count"].text() == "3"
    assert dialog.verdict.property("state") == "ok"
    assert "≥ 50 ms" in dialog.suggest_label.text()  # 30 × 1.5 = 45 → 最少 50
    fake_worker.poll_timed.emit(PollTiming(250.0, 250.0, 37))  # 開著時即時更新
    assert dialog.tiles["maximum"].text() == "250 ms"
    assert dialog.verdict.property("state") == "fault"
    fake_worker.connection_changed.emit(False)
    assert dialog.tiles["disconnects"].text() == "1"
    fake_worker.connection_changed.emit(True)  # 重新連線：時間統計歸零，斷線次數保留
    assert dialog.tiles["count"].text() == "0"
    assert dialog.tiles["disconnects"].text() == "1"
    dialog.reset_button.click()
    assert dialog.tiles["disconnects"].text() == "0"
    dialog.close()


def test_comm_stats_rules():
    stats = CommStats()
    assert stats.suggested_interval() is None
    assert stats.verdict(100)[0] == "off"
    for ms in range(1, 101):  # 1 ~ 100 ms
        stats.add(PollTiming(float(ms), 100.0, 5))
    assert stats.p95 == 95.0
    assert stats.suggested_interval() == 150  # 95 × 1.5 = 142.5 → 取整到 10 ms
    assert stats.verdict(200)[0] == "ok"
    assert stats.verdict(115)[0] == "warn"  # 95 > 115 × 0.8
    assert stats.verdict(99)[0] == "fault"  # 最大 100 > 99
    assert [load_state(v, 100) for v in (80, 81, 100, 101)] == ["ok", "warn", "warn", "fault"]


def test_command_rejected_shows_every_reason(window, fake_worker):
    reasons = ("Y：未原點復歸（Find Home Finish = 0）", "X：目標 150 超出軟體極限 -100 ~ 100")
    fake_worker.engine_event.emit(CommandRejected("移動", reasons))

    assert window.event_dialog.isVisible()
    body = window.event_dialog.text()
    assert all(r in body for r in reasons)
    assert "移動" in window.message_button.text()
    assert all(r in window.messages[-1].detail for r in reasons)


def test_operation_finished_shows_result_per_axis(window, fake_worker):
    results = MappingProxyType(
        {
            1: AxisResult(ResultKind.NOT_RUN),
            7: AxisResult(ResultKind.ALARM, alarm_code=3),
        }
    )
    fake_worker.engine_event.emit(OperationFinished(OperationKind.HOME, results))

    detail = window.messages[-1].detail
    assert "原點復歸" in detail
    assert "第 1 軸" in detail and "未執行" in detail
    assert "第 7 軸" in detail and "Alarm（3）" in detail
    assert "第 7 軸" in window.message_button.text()
    assert window.event_dialog.isVisible()  # 有軸未完成：跳出結果
    assert "Alarm（3）" in window.event_dialog.text()


def test_reset_results_show_success_and_failure(window, fake_worker):
    results = MappingProxyType(
        {2: AxisResult(ResultKind.RESET_OK), 5: AxisResult(ResultKind.RESET_FAIL)}
    )
    fake_worker.engine_event.emit(OperationFinished(OperationKind.RESET, results))
    detail = window.messages[-1].detail
    assert "第 2 軸" in detail and "復歸成功" in detail
    assert "第 5 軸" in detail and "復歸失敗" in detail


def test_all_done_result_goes_to_status_bar_without_popup(window, fake_worker):
    results = MappingProxyType({1: AxisResult(ResultKind.DONE), 2: AxisResult(ResultKind.DONE)})
    fake_worker.engine_event.emit(OperationFinished(OperationKind.MOVE, results))
    assert not window.event_dialog.isVisible()
    text = window.message_button.text()
    assert "移動" in text and "第 1 軸" in text and "第 2 軸" in text and "完成" in text


def test_timeout_result_shows_wait_point(window, fake_worker):
    results = MappingProxyType({1: AxisResult(ResultKind.TIMEOUT, wait_point=WaitPoint.REPLY_ON)})
    fake_worker.engine_event.emit(OperationFinished(OperationKind.MOVE, results))
    assert "逾時（等待 Reply = 1）" in window.messages[-1].detail


def test_notice_goes_to_status_bar(window, fake_worker):
    fake_worker.engine_event.emit(Notice("沒有任何軸 Alarm，不需復歸"))
    assert "不需復歸" in window.message_button.text()


def test_message_history_shows_full_content(window, fake_worker):
    fake_worker.engine_event.emit(CommandRejected("步進", ("理由甲", "理由乙")))
    fake_worker.engine_event.emit(Notice("第二則訊息"))
    window.message_button.click()
    assert window.history_dialog.isVisible()
    text = window.history_dialog.text()
    assert "理由甲" in text and "理由乙" in text and "第二則訊息" in text


# ---- 主題 ----


def test_single_dark_theme():
    """只有深色主題：QSS 只有一份，沒有淺色。"""
    from importlib import resources

    files = {p.name for p in resources.files("plc_motion.ui").joinpath("themes").iterdir()}
    assert "light.qss" not in files
    assert "#16191d" in load_qss()


# ---- 縮到系統匣 ----


def test_close_button_hides_to_tray(window, qtbot):
    window.close()
    assert not window.isVisible()
    window.show_from_tray()
    assert window.isVisible()


def test_minimize_hides_to_tray(window, qtbot):
    window.showMinimized()
    until(qtbot, lambda: not window.isVisible(), timeout=2000)
    window.show_from_tray()
    assert window.isVisible() and not window.isMinimized()


# ---- 結束確認（假 worker） ----


def test_quit_when_idle_needs_no_confirmation(window, fake_worker, make_snapshot, qtbot):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    with qtbot.waitSignal(window.quit_confirmed, timeout=500):
        assert window.request_quit() is True


def test_quit_during_operation_asks_first(window, fake_worker, make_snapshot, qtbot):
    fake_worker.snapshot_ready.emit(make_snapshot(), status(locked=True, operating=True))
    seen: list[str] = []
    answer_next_message_box(QMessageBox.StandardButton.No, seen)
    with qtbot.assertNotEmitted(window.quit_confirmed):
        assert window.request_quit() is False
    assert seen and "結束" in seen[0]

    answer_next_message_box(QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.quit_confirmed, timeout=500):
        assert window.request_quit() is True


def test_quit_with_axis_busy_asks_first(window, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot({"Busy 4": True}), status(locked=True))
    seen: list[str] = []
    answer_next_message_box(QMessageBox.StandardButton.No, seen)
    assert window.request_quit() is False
    assert seen


# ---- MotionApp：真 worker 連 SimServer ----


@pytest.fixture
def server(config_dir: Path):
    points = load_config(config_dir).points
    srv = SimServer(PlcModel(points), points, port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def motion(qtbot, config, server):
    m = MotionApp(config, "127.0.0.1", server.port)
    m.show()
    yield m
    m.shutdown()


def wait_connected(qtbot, motion: MotionApp) -> None:
    until(
        qtbot,
        lambda: (
            motion.last_status is not None
            and motion.last_status.connected
            and motion.snapshot is not None
        ),
    )


def test_hidden_to_tray_keeps_polling(qtbot, motion, server):
    wait_connected(qtbot, motion)
    motion.window.close()
    assert not motion.window.isVisible()
    assert motion.tray.icon.isVisible()
    count = server.log.read_count()
    until(qtbot, lambda: server.log.read_count() >= count + 3, timeout=3000)


def test_tray_double_click_and_menu_show_window(qtbot, motion):
    from PySide6.QtWidgets import QSystemTrayIcon

    motion.window.close()
    motion.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
    assert motion.window.isVisible()
    motion.window.close()
    motion.tray.action_show.trigger()
    assert motion.window.isVisible()


def test_tray_icon_follows_connection(qtbot, motion, server):
    from plc_motion.ui.tray import TrayState

    wait_connected(qtbot, motion)
    until(qtbot, lambda: motion.tray.state is TrayState.NORMAL)
    server.stop()
    until(qtbot, lambda: motion.tray.state is TrayState.DISCONNECTED)
    assert "未連線" in motion.window.connection_label.text()


def test_tray_reconnect(qtbot, motion, server):
    wait_connected(qtbot, motion)
    connects = len([e for e in server.log.entries() if e.kind == "connect"])
    motion.tray.action_reconnect.trigger()
    until(
        qtbot,
        lambda: len([e for e in server.log.entries() if e.kind == "connect"]) > connects,
    )


def test_tray_quit_writes_nothing_and_stops_worker(qtbot, motion, server):
    wait_connected(qtbot, motion)
    with qtbot.waitSignal(motion.finished, timeout=5000):
        motion.tray.action_quit.trigger()
    assert motion.thread.isFinished()
    assert not motion.tray.icon.isVisible()
    assert [e for e in server.log.entries() if e.kind == "write"] == []


def test_tray_quit_during_motion_asks_and_can_cancel(qtbot, motion, server):
    wait_connected(qtbot, motion)
    with server.lock:
        server.model.master_write("Servo Motor 1", True)
        server.model.master_write("Find Home Finish 1", True)
    until(qtbot, lambda: motion.snapshot.axis(1).ready and not motion.last_status.locked)
    motion.worker.move({1: 90.0}, False, 1)  # 低速長距離，確認期間仍在動作中
    until(qtbot, lambda: motion.snapshot.axis(1).busy)
    seen: list[str] = []
    answer_next_message_box(QMessageBox.StandardButton.No, seen)
    with qtbot.assertNotEmitted(motion.finished, wait=200):
        motion.tray.action_quit.trigger()
    assert seen
    assert motion.thread.isRunning()


# ---- 未預期例外 ----


@pytest.mark.qt_no_exception_capture
def test_unexpected_exception_logs_and_shows_dialog(qtbot, window, caplog):
    caplog.set_level(logging.ERROR, logger="plc_motion")
    reporter = ExceptionReporter(window)
    reporter.install()
    try:

        def boom():
            raise RuntimeError("測試用的未預期例外")

        QTimer.singleShot(0, boom)
        until(qtbot, lambda: reporter.dialog is not None and reporter.dialog.isVisible())
        assert "測試用的未預期例外" in reporter.dialog.text()
        assert any(
            r.exc_info and "測試用的未預期例外" in str(r.exc_info[1]) for r in caplog.records
        )
        assert window.isVisible()  # 程式不結束

        QTimer.singleShot(0, boom)
        until(qtbot, lambda: reporter.dialog.text().count("測試用的未預期例外") >= 2)
    finally:
        reporter.uninstall()
    assert sys.excepthook is not reporter._hook
