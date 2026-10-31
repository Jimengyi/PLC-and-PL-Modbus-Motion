"""TrayController：四態圖示、Alarm／急停通知、右鍵選單與雙擊（spec FR-42、FR-43）。"""

import pytest
from PySide6.QtWidgets import QSystemTrayIcon
from ui_support import status

from plc_motion.ui.tray import TrayController, TrayState


@pytest.fixture
def tray(qtbot):
    controller = TrayController()
    controller.show()
    yield controller
    controller.hide()


@pytest.fixture
def notices(tray, monkeypatch):
    """收集送到 Windows 通知的 (標題, 內容)。"""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tray.icon, "showMessage", lambda title, text, *args: sent.append((title, text))
    )
    return sent


def center_color(tray: TrayController) -> str:
    image = tray.icon.icon().pixmap(32, 32).toImage()
    return image.pixelColor(16, 16).name()


def test_icon_is_visible_in_system_tray(tray):
    assert tray.icon.isVisible()


def test_initial_state_is_disconnected(tray):
    assert tray.state is TrayState.DISCONNECTED


def test_state_follows_snapshot_and_status(tray, make_snapshot):
    tray.update(None, status(locked=True, connected=False))
    assert tray.state is TrayState.DISCONNECTED

    tray.update(make_snapshot(), status())
    assert tray.state is TrayState.NORMAL

    tray.update(make_snapshot(), status(locked=True, operating=True))
    assert tray.state is TrayState.BUSY

    tray.update(make_snapshot({"Busy 3": True}), status(locked=True))
    assert tray.state is TrayState.BUSY

    tray.update(make_snapshot({"Alarm Code 2": 5}), status())
    assert tray.state is TrayState.FAULT

    tray.update(make_snapshot({"Emergency Stop": False}), status())
    assert tray.state is TrayState.FAULT


def test_fault_wins_over_busy(tray, make_snapshot):
    tray.update(make_snapshot({"Busy 1": True, "Alarm Code 2": 5}), status(operating=True))
    assert tray.state is TrayState.FAULT


def test_four_states_have_distinct_icons(tray, make_snapshot):
    colors = {}
    tray.update(None, status(locked=True, connected=False))
    colors["disconnected"] = center_color(tray)
    tray.update(make_snapshot(), status())
    colors["normal"] = center_color(tray)
    tray.update(make_snapshot(), status(operating=True))
    colors["busy"] = center_color(tray)
    tray.update(make_snapshot({"Alarm Code 1": 1}), status())
    colors["fault"] = center_color(tray)
    assert len(set(colors.values())) == 4, colors


def test_tooltip_describes_state(tray, make_snapshot):
    tray.update(None, status(locked=True, connected=False))
    assert "未連線" in tray.icon.toolTip()
    tray.update(make_snapshot({"Alarm Code 1": 1}), status())
    assert "異常" in tray.icon.toolTip()


def test_alarm_rising_edge_notifies_once(tray, notices, make_snapshot):
    tray.update(make_snapshot(), status())
    assert notices == []
    tray.update(make_snapshot({"Alarm Code 3": 7}), status())
    assert len(notices) == 1
    assert "第 3 軸" in notices[0][1] and "7" in notices[0][1]
    tray.update(make_snapshot({"Alarm Code 3": 7}), status())
    assert len(notices) == 1  # 維持非 0 不重複通知
    tray.update(make_snapshot({"Alarm Code 3": 9}), status())
    assert len(notices) == 1  # 非 0 → 非 0 不算 0 → 非 0


def test_alarm_cleared_then_raised_again_notifies_again(tray, notices, make_snapshot):
    tray.update(make_snapshot(), status())
    tray.update(make_snapshot({"Alarm Code 1": 2}), status())
    tray.update(make_snapshot(), status())
    tray.update(make_snapshot({"Alarm Code 1": 2}), status())
    assert len(notices) == 2


def test_estop_trigger_notifies(tray, notices, make_snapshot):
    tray.update(make_snapshot(), status())
    tray.update(make_snapshot({"Emergency Stop": False}), status())
    assert len(notices) == 1
    assert "急停" in notices[0][0] + notices[0][1]
    tray.update(make_snapshot({"Emergency Stop": False}), status())
    assert len(notices) == 1


def test_no_notice_for_values_already_abnormal_at_connect(tray, notices, make_snapshot):
    tray.update(None, status(locked=True, connected=False))
    tray.update(make_snapshot({"Alarm Code 1": 2, "Emergency Stop": False}), status())
    assert notices == []


def test_disconnect_resets_edge_detection(tray, notices, make_snapshot):
    tray.update(make_snapshot(), status())
    tray.update(None, status(locked=True, connected=False))
    tray.update(make_snapshot({"Alarm Code 1": 2}), status())
    assert notices == []  # 斷線後第一份快照沒有「前一次」可比較


def test_menu_actions_emit_signals(tray, qtbot):
    texts = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert texts == ["顯示主視窗", "重新連線", "結束"]
    with qtbot.waitSignal(tray.show_requested, timeout=500):
        tray.action_show.trigger()
    with qtbot.waitSignal(tray.reconnect_requested, timeout=500):
        tray.action_reconnect.trigger()
    with qtbot.waitSignal(tray.quit_requested, timeout=500):
        tray.action_quit.trigger()


def test_double_click_requests_show(tray, qtbot):
    with qtbot.waitSignal(tray.show_requested, timeout=500):
        tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)


def test_context_menu_activation_does_not_request_show(tray, qtbot):
    with qtbot.assertNotEmitted(tray.show_requested, wait=100):
        tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Context)
