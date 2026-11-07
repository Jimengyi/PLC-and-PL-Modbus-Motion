"""端到端：連線、斷線、殘留交握與 JOG 殘留（spec AC-1、AC-6、AC-7、AC-39、AC-40、AC-42）。"""

import socket
import time

import pytest
from PySide6.QtCore import Qt

from plc_motion.engine import ResultKind
from plc_motion.plc import PlcModel
from plc_motion.sim import SimServer
from plc_motion.ui.tray import TrayState

pytestmark = pytest.mark.e2e


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lit(lamp) -> bool:
    return lamp.property("state") != "off"


# ---- AC-1 ----


def test_ac1_default_config_connects_and_mirrors_sim_state(e2e):
    e2e.prepare(servo=(1, 2, 3, 7), homed=(1, 7))  # Servo OFF 的軸模擬器會清 Find Home Finish
    with e2e.sim.lock:
        m = e2e.sim.model
        for n, value in {1: 12.3456, 2: -7.5, 4: 4.25, 5: -1.5, 6: 29.99, 7: 55.55}.items():
            m.set_position(n, value)
        m.set_limit(2, "pos", True)
        m.set_limit(3, "home", True)
        m.set_limit(5, "neg", True)
        m.inject_alarm(6, 4, persistent=False)

    app = e2e.start()
    e2e.wait_connected()
    e2e.until(lambda: app.axis_page.cards[2].position_label.text() == "-7.5000")

    port = e2e.sim.port
    assert f"已連線 127.0.0.1:{port}" in app.window.connection_label.text()
    page = app.axis_page
    cards = page.cards
    positions = {n: (cards[n].position_label.text(), cards[n].unit) for n in cards}
    assert positions == {
        1: ("12.3456", "mm"),
        2: ("-7.5000", "mm"),
        3: ("0.0000", "mm"),
        4: ("4.25", "deg"),
        5: ("-1.50", "deg"),
        6: ("29.99", "deg"),
        7: ("55.55", "mm"),
    }
    lamps: dict[str, set[int]] = {}
    alarms: dict[int, str] = {}
    for n in cards:  # 燈號在選定軸區塊：逐軸點卡片讀取
        e2e.click_row(n)
        for key, lamp in page.selected_lamps.items():
            if lit(lamp):
                lamps.setdefault(key, set()).add(n)
        alarms[n] = page.alarm_code_label.text()
    assert {k: v for k, v in lamps.items() if k not in ("request", "reply")} == {
        "servo": {1, 2, 3, 7},
        "ready": {1, 2, 3, 7},
        "home_finish": {1, 7},
        "pos_limit": {2},
        "home_sensor": {3},
        "neg_limit": {5},
    }
    assert alarms == {n: ("4" if n == 6 else "0") for n in cards}
    assert cards[6].property("state") == "fault"
    assert cards[6].reason_label.text() == "Alarm 4"
    assert cards[2].reason_label.text() == "+極限"


# ---- AC-6 ----


def test_ac6_start_without_sim_then_reconnect_manually(e2e, config):
    port = free_port()
    app = e2e.start(port=port)
    e2e.until(lambda: f"未連線 127.0.0.1:{port}" in app.window.connection_label.text())
    assert app.window.isVisible()
    assert e2e.enabled_controls() == frozenset()
    # 連線中也顯示未連線：等這次連線嘗試確定失敗（Windows 連到沒人聽的埠要等到逾時）
    e2e.until(lambda: "連線失敗" in e2e.log_text(), timeout=10000)

    server = SimServer(
        PlcModel(config.points), config.points, "127.0.0.1", port, config.read_ranges
    )
    server.start()
    try:
        e2e.settle(1500)
        # 期間程式不自行重連：模擬器沒有任何連線
        assert [e for e in server.log.entries() if e.kind == "connect"] == []
        assert f"未連線 127.0.0.1:{port}" in app.window.connection_label.text()

        e2e.click(app.window.reconnect_button)
        e2e.until(lambda: f"已連線 127.0.0.1:{port}" in app.window.connection_label.text())
        e2e.until(lambda: app.snapshot is not None)
        assert len([e for e in server.log.entries() if e.kind == "connect"]) == 1
    finally:
        e2e.close_all()
        server.stop()


# ---- AC-7 ----


def test_ac7_sim_stop_shows_disconnected_within_one_poll(e2e, config):
    app = e2e.start()
    e2e.wait_connected()
    e2e.until(lambda: app.tray.state == TrayState.NORMAL)

    t0 = time.monotonic()
    e2e.sim.stop()
    e2e.until(
        lambda: (
            "未連線" in app.window.connection_label.text()
            and app.tray.state == TrayState.DISCONNECTED
        )
    )
    elapsed = time.monotonic() - t0
    # 一個輪詢週期內偵測，加上 UI 執行緒處理事件的餘裕
    assert elapsed <= config.device.poll_interval_ms / 1000 + 0.1, elapsed
    assert "與 PLC 斷線" in e2e.log_text()


# ---- AC-39、AC-40：殘留交握 ----


def leave_residual_on_x(e2e) -> None:
    """X 開「不回 Reply」下移動，Request = 1 之後、逾時之前中斷所有連線。"""
    e2e.sim_call("set_fault", 1, no_reply=True, no_copy=False)
    since_event, since = len(e2e.events), e2e.mark()
    # 模擬器操作員在 Request = 1 後 0.3 秒按「中斷所有連線」
    action = e2e.when(
        e2e.logged("write", "Request 1", True, since),
        lambda: (time.sleep(0.3), e2e.sim.disconnect_all()),
    )
    e2e.move({1: 10.0})
    action.check()
    finished = e2e.wait_finished(since_event)
    assert finished.results[1].kind == ResultKind.COMM_LOST  # 交握逾時之前就斷線
    e2e.until(lambda: not e2e.app.last_status.connected)


def test_ac39_residual_dialog_keep_then_clear(e2e):
    e2e.prepare()
    app = e2e.start()
    e2e.wait_unlocked()
    leave_residual_on_x(e2e)

    since = e2e.mark()
    e2e.click(app.window.reconnect_button)
    dialog = app.window.residual_dialog
    e2e.until(dialog.isVisible)
    assert "第 1 軸（X）：Request = 1，PC Action Code = 2" in dialog.text()
    assert [e.kind for e in e2e.entries(since)] == ["connect"]

    e2e.settle(600)  # 數個輪詢週期：等操作員選擇的期間不寫入
    assert e2e.writes(since) == []
    e2e.click(dialog.keep_button)
    e2e.settle(600)
    assert not dialog.isVisible()
    assert app.last_status.locked
    assert any("殘留交握" in r for r in app.last_status.lock_reasons)
    assert e2e.writes(since) == []
    assert app.window.residual_button.isVisible()

    e2e.click(app.window.residual_button)
    e2e.until(dialog.isVisible)
    e2e.click(dialog.clear_button)
    e2e.until(lambda: len(e2e.writes(since)) >= 2)
    assert e2e.writes(since) == [("Request 1", False), ("PC Action Code 1", 0)]
    e2e.wait_unlocked()


def test_ac40_residual_survives_restart_and_no_copy_residual(e2e):
    e2e.prepare()
    e2e.start()
    e2e.wait_unlocked()
    leave_residual_on_x(e2e)
    e2e.click(e2e.app.window.reconnect_button)
    e2e.until(e2e.app.window.residual_dialog.isVisible)

    # 系統匣「結束」後重新啟動：同一個 SimServer
    e2e.quit_from_tray()
    assert not e2e.app.window.isVisible()
    app = e2e.start()
    dialog = app.window.residual_dialog
    e2e.until(dialog.isVisible)
    assert "第 1 軸（X）：Request = 1，PC Action Code = 2" in dialog.text()

    e2e.click(dialog.clear_button)
    e2e.wait_unlocked()
    e2e.sim_call("set_fault", 1, no_reply=False, no_copy=False)
    e2e.sim_call("set_fault", 2, no_reply=False, no_copy=True)
    since_event, since = len(e2e.events), e2e.mark()
    # 等待命令碼複製期間（PC Action Code = 2 之後 0.3 秒）中斷所有連線
    action = e2e.when(
        e2e.logged("write", "PC Action Code 2", 2, since),
        lambda: (time.sleep(0.3), e2e.sim.disconnect_all()),
    )
    e2e.move({2: 10.0})
    action.check()
    assert e2e.wait_finished(since_event).results[2].kind == ResultKind.COMM_LOST
    e2e.until(lambda: not app.last_status.connected)

    e2e.click(app.window.reconnect_button)
    e2e.until(dialog.isVisible)
    assert "第 2 軸（Y）：Request = 0，PC Action Code = 2" in dialog.text()
    assert "第 1 軸" not in dialog.text()


# ---- AC-42：斷線期間 JOG 未放開 ----


def test_ac42_jog_held_through_disconnect_is_cleared_after_reconnect(e2e, qtbot):
    e2e.prepare()
    app = e2e.start()
    e2e.wait_unlocked()
    jog_plus = app.axis_page.jog_plus
    assert app.axis_page.selected_axis == 1

    since = e2e.mark()
    qtbot.mousePress(jog_plus, Qt.MouseButton.LeftButton)
    e2e.wait_entry("write", "JOG Plus 1", True, since)
    e2e.until(lambda: e2e.model("Current Position 1") > 0)

    e2e.sim.disconnect_all()
    e2e.until(lambda: not app.last_status.connected)
    p1 = e2e.model("Current Position 1")
    e2e.settle(300)
    assert e2e.model("Current Position 1") > p1  # 模擬器的 X 持續移動
    assert e2e.model("JOG Plus 1") is True
    if jog_plus.is_held:
        qtbot.mouseRelease(jog_plus, Qt.MouseButton.LeftButton)
    e2e.settle(300)
    assert e2e.model("JOG Plus 1") is True  # 未連線時放開寫不出去

    since = e2e.mark()
    e2e.click(app.window.reconnect_button)
    cleared = e2e.wait_entry("write", "JOG Plus 1", False, since)
    connect = e2e.find("connect", None, None, since)[0]
    assert connect < cleared
    e2e.settle(200)
    p2 = e2e.model("Current Position 1")
    e2e.settle(300)
    assert e2e.model("Current Position 1") == p2  # X 停止
    assert any("第 1 軸 JOG 於斷線期間未放開，已寫回 0" in t for t in e2e.messages())
    assert "第 1 軸 JOG 於斷線期間未放開，已寫回 0" in e2e.log_text()
