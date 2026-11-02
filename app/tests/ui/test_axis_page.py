"""AxisPage 軸控頁：位置卡、選定軸、目標位置移動、全軸操作、JOG。

spec FR-16 反灰、FR-27 ~ FR-31、FR-33。

顯示與命令以假 worker 送 signal、記錄呼叫；確認視窗由 `confirms` fixture 自動回答並記錄內容。
「縮到系統匣時放開 JOG」另以真 PlcWorker 連 SimServer 驗證 PLC 端 JOG 點確實寫回 0。
"""

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication
from ui_support import status

from plc_motion.app import MotionApp
from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.sim import SimServer
from plc_motion.ui.axis_page import AxisPage

ALL_HOMED = {f"Find Home Finish {n}": True for n in range(1, 8)}
SELECTED_LAMPS = (
    "servo",
    "ready",
    "busy",
    "request",
    "reply",
    "home_finish",
    "pos_limit",
    "home_sensor",
    "neg_limit",
)


@pytest.fixture
def page(qtbot, config, fake_worker):
    p = AxisPage(config, fake_worker)
    qtbot.addWidget(p)
    p.resize(1500, 820)
    p.show()
    qtbot.waitExposed(p)
    return p


def calls(fake_worker, name):
    return [c for c in fake_worker.calls if c[0] == name]


def is_on(lamp) -> bool:
    return lamp.property("state") != "off"


# ---- 位置卡 ----


def test_cards_show_name_unit_and_position(page, fake_worker, make_snapshot):
    snap = make_snapshot(
        {
            "Current Position 1": 123456,
            "Current Position 2": -32000,
            "Current Position 4": 125,
            "Current Position 7": 15025,
        }
    )
    fake_worker.snapshot_ready.emit(snap, status())
    assert [page.cards[n].name_label.text() for n in range(1, 8)] == [
        "X",
        "Y",
        "Z",
        "RX",
        "RY",
        "RZ",
        "升降",
    ]
    assert page.cards[1].position_label.text() == "12.3456"
    assert page.cards[2].position_label.text() == "-3.2000"
    assert page.cards[3].position_label.text() == "0.0000"
    assert page.cards[4].position_label.text() == "1.25"
    assert page.cards[7].position_label.text() == "150.25"
    assert page.cards[1].unit == "mm" and page.cards[4].unit == "deg"


def test_cards_show_only_position_and_fault_reason(page, fake_worker, make_snapshot):
    """總覽不再有燈號、勾選與目標值；異常時上緣變紅並顯示原因。"""
    assert not hasattr(page.cards[1], "check") and not hasattr(page.cards[1], "target")
    snap = make_snapshot(
        {
            "Alarm Code 5": 2,
            "Positive Limit 3": False,  # B 接點：0 為觸發
            "Negative Limit 6": False,
        }
    )
    fake_worker.snapshot_ready.emit(snap, status())
    assert page.cards[5].property("state") == "fault"
    assert page.cards[5].reason_label.text() == "Alarm 2"
    assert page.cards[3].reason_label.text() == "+極限"
    assert page.cards[6].reason_label.text() == "−極限"
    assert page.cards[4].property("state") == "ok"
    assert page.cards[4].reason_label.text() == ""
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    assert page.cards[5].property("state") == "ok"
    assert page.cards[5].reason_label.text() == ""


def test_no_snapshot_shows_placeholders(page, fake_worker):
    fake_worker.snapshot_ready.emit(None, status(locked=True, connected=False))
    assert page.cards[1].position_label.text() == "—"
    assert page.cards[1].property("state") == "off"
    assert page.plc_action_label.text() == "—"


def test_clicking_card_selects_axis(page, qtbot, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot({"Request 3": True}), status())
    assert page.selected_axis == 1
    qtbot.mouseClick(page.cards[3], Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
    assert page.selected_axis == 3
    assert page.selected_title.text() == "Z 軸"
    assert is_on(page.selected_lamps["request"])
    assert page.cards[3].property("selected") is True
    assert page.cards[1].property("selected") is False


# ---- 選定軸 ----


def test_selected_block_shows_nine_lamps_and_codes(page, fake_worker, make_snapshot):
    assert set(page.selected_lamps) == set(SELECTED_LAMPS)
    page.select_axis(6)
    snap = make_snapshot(
        {
            "Servo Motor 6": True,
            "Ready 6": True,
            "Reply 6": True,
            "Negative Limit 6": False,
            "PLC Action Code 6": 2,
            "Alarm Code 6": 9,
        }
    )
    fake_worker.snapshot_ready.emit(snap, status())
    on = {k for k, lamp in page.selected_lamps.items() if is_on(lamp)}
    assert on == {"servo", "ready", "reply", "neg_limit"}
    assert page.selected_lamps["neg_limit"].property("state") == "fault"
    assert page.plc_action_label.text() == "2"
    assert page.alarm_code_label.text() == "9"
    assert page.selected_state.text() == "Alarm 9"
    page.select_axis(1)
    assert page.plc_action_label.text() == "0"
    assert not any(is_on(lamp) for lamp in page.selected_lamps.values())


def test_target_input_follows_selected_axis_decimals_and_unit(page):
    assert page.target.decimals() == 4
    assert page.target.suffix() == " mm"
    page.select_axis(5)
    assert page.target.decimals() == 2
    assert page.target.suffix() == " deg"
    page.select_axis(7)
    assert page.target.decimals() == 2


def test_target_move_confirms_then_sends_single_axis(page, fake_worker, make_snapshot, confirms):
    snap = make_snapshot({**ALL_HOMED, "Current Position 3": 125000})  # 12.5 mm
    fake_worker.snapshot_ready.emit(snap, status())
    page.select_axis(3)
    page.jog_pad.speed.setValue(40)
    page.target.setValue(30.0)
    page.move_button.click()
    assert calls(fake_worker, "move") == [("move", {3: 30.0}, False, 40)]
    shown = confirms.last
    assert shown["title"] == "確認移動 Z 軸"
    assert [(r.axis, r.current, r.target, r.unit) for r in shown["rows"]] == [
        ("Z", "12.5000", "30.0000", "mm")
    ]
    assert "絕對" in shown["lines"][0] and "40 %" in shown["lines"][0]

    page.relative_button.click()
    page.target.setValue(-2.5)
    page.move_button.click()
    assert calls(fake_worker, "move")[-1] == ("move", {3: -2.5}, True, 40)
    assert confirms.last["rows"][0].target == "10.0000"  # 目前 12.5 + (−2.5)
    assert "相對" in confirms.last["lines"][0]


def test_cancelled_confirm_sends_nothing(page, fake_worker, make_snapshot, confirms):
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())
    confirms.answer = False
    page.move_button.click()
    page.home_one_button.click()
    page.home_all_button.click()
    assert len(confirms.calls) == 3
    assert calls(fake_worker, "move") == [] and calls(fake_worker, "home") == []


def test_lock_during_confirm_sends_nothing(page, fake_worker, make_snapshot, monkeypatch):
    """確認視窗開著時被鎖定（例如另一軸開始動作）：按確認後也不送命令。"""
    from plc_motion.ui import confirm_dialog

    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())

    def lock_then_confirm(parent, title, **kwargs):
        fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status(locked=True))
        return True

    monkeypatch.setattr(confirm_dialog, "confirm", lock_then_confirm)
    page.move_button.click()
    assert calls(fake_worker, "move") == []


# ---- 全軸操作 ----


def test_home_finish_overview(page, fake_worker, make_snapshot):
    snap = make_snapshot({"Find Home Finish 2": True, "Find Home Finish 7": True})
    fake_worker.snapshot_ready.emit(snap, status())
    on = {n for n, lamp in page.home_finish_lamps.items() if is_on(lamp)}
    assert on == {2, 7}
    assert page.home_finish_lamps[7].text().endswith("升降")


def test_home_commands_confirm_and_send_axis_sets(page, fake_worker, make_snapshot, confirms):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    page.select_axis(5)
    page.home_one_button.click()
    page.home_all_button.click()
    assert [sorted(c[1]) for c in calls(fake_worker, "home")] == [
        [5],
        [1, 2, 3, 4, 5, 6, 7],
    ]
    assert [c["title"] for c in confirms.calls] == ["確認 RY 軸原點復歸", "確認全部原點復歸"]
    assert any("先做升降軸" in line for line in confirms.last["lines"])


def test_servo_and_reset_buttons_send_without_confirm(page, fake_worker, make_snapshot, confirms):
    """Servo 與 Reset Alarm 從頂列移到全軸操作；不跳確認。"""
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    page.servo_on_button.click()
    page.servo_off_button.click()
    page.reset_button.click()
    assert [c for c in fake_worker.calls if c[0] in ("servo_all", "reset_all")] == [
        ("servo_all", True),
        ("servo_all", False),
        ("reset_all",),
    ]
    assert confirms.calls == []


# ---- JOG ----


def test_jog_press_and_release_use_selected_axis_and_shared_speed(
    page, qtbot, fake_worker, make_snapshot
):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    page.select_axis(2)
    page.jog_pad.speed.setValue(35)
    qtbot.mousePress(page.jog_plus, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(page.jog_plus, Qt.MouseButton.LeftButton)
    qtbot.mousePress(page.jog_minus, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(page.jog_minus, Qt.MouseButton.LeftButton, pos=QPoint(-50, -50))
    assert [c for c in fake_worker.calls if c[0].startswith("jog")] == [
        ("jog_press", 2, 1, 35),
        ("jog_release", 2, 1),
        ("jog_press", 2, -1, 35),
        ("jog_release", 2, -1),
    ]


def test_held_jog_stays_enabled_while_own_jog_locks(page, qtbot, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    qtbot.mousePress(page.jog_plus, Qt.MouseButton.LeftButton)
    locked = status(locked=True, reasons=("JOG 按住中",))
    fake_worker.snapshot_ready.emit(make_snapshot({"JOG Plus 1": True}), locked)
    assert page.jog_plus.isEnabled()  # 反灰會視同放開，按住中的鍵不能反灰
    assert not page.jog_minus.isEnabled()
    qtbot.mouseRelease(page.jog_plus, Qt.MouseButton.LeftButton)
    assert calls(fake_worker, "jog_release") == [("jog_release", 1, 1)]
    assert not page.jog_plus.isEnabled()  # 放開後仍鎖定中


@pytest.mark.parametrize(
    "release",
    ["hide", "deactivate", "minimize"],
)
def test_held_jog_released_when_window_leaves(page, qtbot, fake_worker, make_snapshot, release):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    page.select_axis(4)
    qtbot.mousePress(page.jog_minus, Qt.MouseButton.LeftButton)
    if release == "hide":
        page.hide()
    elif release == "deactivate":
        QApplication.sendEvent(page, QEvent(QEvent.Type.WindowDeactivate))
    else:
        page.showMinimized()
    qtbot.waitUntil(lambda: calls(fake_worker, "jog_release") == [("jog_release", 4, -1)])


def test_hide_without_held_jog_sends_nothing(page, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(), status())
    page.hide()
    assert calls(fake_worker, "jog_release") == []


def test_jog_disabled_when_direction_limit_triggered(page, fake_worker, make_snapshot):
    page.select_axis(3)
    fake_worker.snapshot_ready.emit(make_snapshot({"Positive Limit 3": False}), status())
    assert not page.jog_plus.isEnabled()
    assert page.jog_minus.isEnabled()
    assert "正極限" in page.jog_plus.toolTip()
    fake_worker.snapshot_ready.emit(make_snapshot({"Negative Limit 3": False}), status())
    assert page.jog_plus.isEnabled()
    assert not page.jog_minus.isEnabled()
    page.select_axis(1)  # 換軸：依新選定軸的極限判斷
    assert page.jog_plus.isEnabled() and page.jog_minus.isEnabled()


# ---- 鎖定 ----


def write_controls(page):
    return [
        page.move_button,
        page.home_one_button,
        page.home_all_button,
        page.servo_on_button,
        page.servo_off_button,
        page.reset_button,
        page.jog_plus,
        page.jog_minus,
        *page.jog_pad.step_buttons.values(),
    ]


def test_locked_disables_all_write_controls(page, fake_worker, make_snapshot):
    assert not any(w.isEnabled() for w in write_controls(page))  # 尚未收到狀態
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())
    assert all(w.isEnabled() for w in write_controls(page))
    reasons = ("第 3 軸動作中",)
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status(locked=True, reasons=reasons))
    assert not any(w.isEnabled() for w in write_controls(page))
    assert "第 3 軸動作中" in page.home_all_button.toolTip()
    fake_worker.snapshot_ready.emit(None, status(locked=True, connected=False))
    assert not any(w.isEnabled() for w in write_controls(page))


# ---- 組裝：真 worker 連 SimServer ----


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


def test_motion_app_mounts_axis_page(motion):
    assert isinstance(motion.window.pages.widget(0), AxisPage)
    assert motion.window.nav.buttons[0].text() == "軸控"


def test_hiding_to_tray_releases_held_jog_on_plc(qtbot, motion, server):
    with server.lock:
        server.model.master_write("Servo Motor 1", True)
    qtbot.waitUntil(
        lambda: (
            motion.snapshot is not None
            and motion.snapshot.axis(1).ready
            and not motion.last_status.locked
        ),
        timeout=5000,
    )
    page = motion.window.pages.widget(0)
    qtbot.waitUntil(lambda: page.jog_plus.isEnabled(), timeout=5000)
    qtbot.mousePress(page.jog_plus, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(server.model.read("JOG Plus 1")), timeout=5000)
    motion.window.close()  # 縮到系統匣
    qtbot.waitUntil(lambda: not server.model.read("JOG Plus 1"), timeout=5000)
