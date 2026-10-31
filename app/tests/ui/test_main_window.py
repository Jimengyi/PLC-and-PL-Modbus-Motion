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
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QWidget
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
from plc_motion.ui.dialogs import ExceptionReporter
from plc_motion.ui.main_window import MainWindow
from plc_motion.ui.points_panel import DESC_COL
from plc_motion.ui.theme import apply_theme, load_qss


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


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_points_layout_readable_at_minimum_size(window, config, fake_worker, qtbot, theme):
    """1600×900 下：點位操作區的七個軸勾選框標籤完整，點位表不捲動即可看到 點位 ~ 升降 欄。"""
    apply_theme(QApplication.instance(), theme)
    page = AxisPage(config, fake_worker)
    window.add_page("軸控", None, page)
    window.resize(1600, 900)
    qtbot.wait(50)
    panel = page.points_panel

    names = [box.text() for box in panel.axis_checks.values()]
    assert names == [config.axes[n].name for n in sorted(config.axes)]
    for box in panel.axis_checks.values():
        assert box.isVisible()
        assert box.width() >= box.sizeHint().width(), box.text()

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


def test_nav_collapse_widens_content(window, qtbot):
    window.add_page("軸控", None, QWidget())
    window.add_page("IO Monitor", None, QWidget())
    qtbot.waitUntil(lambda: window.pages.width() > 0)
    before = window.pages.width()
    window.nav.toggle_button.click()
    assert window.nav.collapsed
    until(qtbot, lambda: window.pages.width() > before)
    assert window.nav.width() < 80
    window.nav.toggle_button.click()
    assert not window.nav.collapsed
    until(qtbot, lambda: window.pages.width() == before)


def test_collapsed_nav_shows_name_as_tooltip(window):
    window.add_page("IO Monitor", None, QWidget())
    window.nav.set_collapsed(True)
    button = window.nav.buttons[0]
    assert button.text() == "" or button.toolButtonStyle().name == "ToolButtonIconOnly"
    assert button.toolTip() == "IO Monitor"
    assert not button.icon().isNull()


# ---- 工具列 ----


def test_toolbar_buttons_send_commands(window, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    window.reconnect_button.click()
    window.servo_on_button.click()
    window.servo_off_button.click()
    window.reset_button.click()
    assert fake_worker.calls == [
        ("connect_plc",),
        ("servo_all", True),
        ("servo_all", False),
        ("reset_all",),
    ]


def test_write_buttons_follow_lock(window, fake_worker, make_snapshot):
    buttons = [window.servo_on_button, window.servo_off_button, window.reset_button]
    assert not any(b.isEnabled() for b in buttons)  # 尚未收到任何狀態：視同鎖定

    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    assert all(b.isEnabled() for b in buttons)

    fake_worker.snapshot_ready.emit(
        make_snapshot(), status(locked=True, operating=True, reasons=("有操作進行中",))
    )
    assert not any(b.isEnabled() for b in buttons)
    assert "有操作進行中" in window.servo_on_button.toolTip()

    fake_worker.snapshot_ready.emit(None, status(locked=True, connected=False))
    assert not any(b.isEnabled() for b in buttons)
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


def test_status_bar_shows_poll_interval(window):
    assert "200 ms" in window.poll_label.text()


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


def test_theme_toggle_applies_and_saves(window, config_dir: Path):
    app = QApplication.instance()
    apply_theme(app, "dark")
    window.theme_button.click()
    assert app.styleSheet() == load_qss("light")
    assert load_config(config_dir).device.theme == "light"
    text = (config_dir / "device.toml").read_bytes().decode("utf-8")
    assert "# 主題：dark 或 light" in text and "# 預設深色" in text  # 註解保留
    window.theme_button.click()
    assert app.styleSheet() == load_qss("dark")
    assert load_config(config_dir).device.theme == "dark"


def test_themes_differ():
    assert load_qss("dark") != load_qss("light")


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
