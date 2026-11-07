"""端到端：通訊層改用 modbus_tags 後的停止、重新連線與寫入失敗
（spec modbus-tags-migration AC-14、AC-20、AC-21）。

寫入只排入通道佇列：結束與重新連線時要先等已排入的寫入；寫入被拒時要先放開 JOG 再斷線。
"""

import pytest
from PySide6.QtCore import Qt

from plc_motion.engine import ResultKind

pytestmark = pytest.mark.e2e


def connection_text(e2e) -> str:
    return e2e.app.window.connection_label.text()


def press_jog(e2e, qtbot) -> int:
    """在軸控頁按住第 1 軸 JOG ＋，等模擬器收到 JOG Plus 1 = 1；回傳按下前的紀錄位置。"""
    since = e2e.mark()
    qtbot.mousePress(e2e.app.axis_page.jog_plus, Qt.MouseButton.LeftButton)
    e2e.wait_entry("write", "JOG Plus 1", True, since)
    e2e.until(lambda: e2e.model("Current Position 1") > 0)
    return since


# ---- AC-14 ----


def test_quit_while_jog_held_leaves_jog_zero_in_plc(e2e, qtbot):
    e2e.prepare()
    e2e.start()
    e2e.wait_unlocked()
    press_jog(e2e, qtbot)
    # 每個請求延遲 100 ms：一輪讀取比輪詢週期長，通道一直在讀取，放開的寫入一定要排隊等
    # 進行中的讀取，結束時不能丟掉
    e2e.sim.set_delay(100)
    e2e.settle(300)  # 讓延遲生效的那一輪已經開始
    e2e.close_all()  # 等同系統匣「結束」確認後的 shutdown：先藏視窗（放開 JOG），再停 worker
    assert e2e.model("JOG Plus 1") is False
    p = e2e.model("Current Position 1")
    e2e.settle(300)
    assert e2e.model("Current Position 1") == p


def test_reconnect_while_jog_held_leaves_jog_zero_in_plc(e2e, qtbot):
    e2e.prepare()
    app = e2e.start()
    e2e.wait_unlocked()
    press_jog(e2e, qtbot)
    since = e2e.mark()
    app.tray.action_reconnect.trigger()  # 滑鼠仍按著 JOG：從系統匣選單重新連線
    e2e.wait_connected()
    e2e.until(lambda: e2e.find("connect", None, None, since))
    e2e.until(lambda: e2e.model("JOG Plus 1") is False)
    connects = [e for e in e2e.entries() if e.kind == "connect"]
    disconnects = [e for e in e2e.entries() if e.kind == "disconnect"]
    assert len(connects) == len(disconnects) + 1  # 舊連線關閉後才只剩一條


# ---- AC-20 ----


def test_sim_drops_connections_no_auto_reconnect_even_if_configured(e2e_config_dir, qtbot, e2e):
    # e2e fixture 的設定已載入；改 device.toml 後重新啟動（同程序內重建 MotionApp）
    path = e2e_config_dir / "modbus" / "Channel01" / "device.toml"
    text = path.read_bytes().decode("utf-8")
    assert "auto_reconnect = false" in text
    path.write_bytes(
        text.replace(
            "auto_reconnect = false", "auto_reconnect = true\nreconnect_interval_ms = 50"
        ).encode("utf-8")
    )
    e2e.prepare()
    e2e.restart()
    e2e.wait_unlocked()
    assert e2e.config.channel.device.auto_reconnect  # 設定確實允許自動重連

    ev = len(e2e.events)
    e2e.move({1: 50.0}, speed=1)
    e2e.until(lambda: e2e.app.snapshot is not None and e2e.app.snapshot.axis(1).busy)
    e2e.sim.disconnect_all()
    finished = e2e.wait_finished(ev)
    assert finished.results[1].kind == ResultKind.COMM_LOST
    e2e.until(lambda: "未連線" in connection_text(e2e))
    since = e2e.mark()
    e2e.settle(800)
    assert not e2e.find("connect", None, None, since)
    assert "未連線" in connection_text(e2e)

    # 手動重新連線仍做殘留交握與 JOG 殘留檢查
    e2e.sim_call("master_write", "JOG Minus 2", True)
    e2e.sim_call("master_write", "Request 3", True)
    e2e.sim_call("master_write", "PC Action Code 3", 2)
    e2e.click(e2e.app.window.reconnect_button)
    e2e.wait_entry("write", "JOG Minus 2", False, since)
    e2e.until(lambda: e2e.status is not None and e2e.status.residual)
    assert [r.axis for r in e2e.status.residual] == [3]


# ---- AC-21 ----


def test_rejected_speed_while_jog_releases_jog_and_shows_estop_hint(e2e, qtbot):
    e2e.prepare()
    app = e2e.start()
    e2e.wait_unlocked()
    e2e.sim.reject_writes({"Speed 1"})
    since = e2e.mark()
    qtbot.mousePress(app.axis_page.jog_plus, Qt.MouseButton.LeftButton)
    e2e.until(lambda: "未連線" in connection_text(e2e))
    e2e.until(lambda: any("軸可能仍在移動，請按急停" in t for t in e2e.messages()))
    if app.axis_page.jog_plus.is_held:
        qtbot.mouseRelease(app.axis_page.jog_plus, Qt.MouseButton.LeftButton)
    assert e2e.model("JOG Plus 1") is False
    jog = [v for t, v in e2e.writes(since) if t == "JOG Plus 1"]
    assert jog == [True, False]
    log = e2e.log_text()
    assert "Speed 1" in log and "位址 123" in log
    assert "可能仍在移動" in log
