"""端到端：移動交握、逾時與提早結束、原點復歸、步進與 JOG、Servo 與 Reset Alarm
（spec AC-8 ~ AC-24、AC-37、AC-41）。

需要在動作途中操作模擬器的步驟（注入 Alarm、急停）由 `e2e.when()` 在另一條執行緒依模擬器
紀錄觸發：plc-sim 是獨立程序，模擬器操作員的時機不受 plc-motion 畫面影響。
"""

import time

import pytest
from pymodbus.client import ModbusTcpClient
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from plc_motion.engine import ResultKind, WaitPoint
from plc_motion.ui.tray import TrayState

pytestmark = pytest.mark.e2e

AXES = range(1, 8)
FREEZE_BUG = (
    "產品 bug：鎖定狀態切換時 PointsPanel._set_editable 對 200 列逐格 setFlags，觸發 itemChanged"
    " → _store 把 positions.csv 寫上千次，UI 執行緒凍結約 5 秒"
)


def lit(lamp) -> bool:
    return lamp.property("state") != "off"


def axis_of(tag: str) -> int:
    return int(tag.rsplit(" ", 1)[1])


def axis_writes(e2e, n: int, since: int) -> list[tuple[str, int | bool]]:
    return [w for w in e2e.writes(since) if axis_of(w[0]) == n]


def started(e2e) -> None:
    e2e.prepare()
    e2e.start()
    e2e.wait_unlocked()


def kinds(finished) -> dict[int, ResultKind]:
    return {n: r.kind for n, r in finished.results.items()}


# ---- 移動（AC-8 ~ AC-12） ----


def test_ac8_three_axes_absolute_move_handshake_order_and_lock(e2e):
    started(e2e)
    ev, since, first_sample = len(e2e.events), e2e.mark(), len(e2e.samples)
    targets = {1: 5.0, 2: 10.0, 3: 15.0}
    e2e.move(targets, speed=50)
    finished = e2e.wait_finished(ev, 30000)
    assert kinds(finished) == {1: ResultKind.DONE, 2: ResultKind.DONE, 3: ResultKind.DONE}

    for n, target in targets.items():
        raw = round(target * 10000)
        assert axis_writes(e2e, n, since) == [
            (f"Target Position {n}", raw),
            (f"Speed {n}", 50),
            (f"PC Action Code {n}", 2),
            (f"Request {n}", True),
            (f"Request {n}", False),
            (f"PC Action Code {n}", 0),
        ]
        # 交握的每一步都等到 PLC 端狀態成立後才寫下一步（FR-18）
        pc_on = e2e.find("write", f"PC Action Code {n}", 2, since)[0]
        copied = e2e.find("state", f"PLC Action Code {n}", 2, since)[0]
        req_on = e2e.find("write", f"Request {n}", True, since)[0]
        reply_on = e2e.find("state", f"Reply {n}", True, since)[0]
        req_off = e2e.find("write", f"Request {n}", False, since)[0]
        reply_off = e2e.find("state", f"Reply {n}", False, since)[0]
        assert pc_on < copied < req_on < reply_on < req_off < reply_off
        assert e2e.model(f"Current Position {n}") == raw  # 最終位置等於目標

    begin = [e2e.entry(e2e.find("state", f"Busy {n}", True, since)[0]).time for n in targets]
    end = [e2e.entry(e2e.find("state", f"Busy {n}", False, since)[-1]).time for n in targets]
    assert max(begin) < min(end)  # 三軸都開始移動後才有任一軸完成

    during = [
        s
        for s in e2e.samples[first_sample:]
        if s.status.operating or any(s.snapshot.axis(n).busy for n in targets)
    ]
    assert len(during) >= 3
    assert all(s.enabled == frozenset() for s in during)
    e2e.wait_unlocked()
    e2e.until(lambda: e2e.app.axis_page.move_button.isEnabled())
    assert {"執行勾選軸移動", "全部 Servo ON", "全部原點復歸", "步進 1+"} <= e2e.enabled_controls()


def test_ac9_relative_move_converts_to_absolute_code2(e2e):
    e2e.prepare(positions={1: 10.0, 7: 20.0})
    e2e.start()
    e2e.wait_unlocked()

    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: -2.5}, relative=True)
    assert kinds(e2e.wait_finished(ev)) == {1: ResultKind.DONE}
    writes = e2e.writes(since)
    assert ("Target Position 1", 75000) in writes
    assert ("PC Action Code 1", 2) in writes

    e2e.wait_unlocked()
    ev = len(e2e.events)
    e2e.move({7: 1.25}, relative=True)
    assert kinds(e2e.wait_finished(ev)) == {7: ResultKind.DONE}
    assert ("Target Position 7", 2000 + 125) in e2e.writes(since)
    assert [w for w in e2e.writes(since) if w[0].startswith("PC Action Code") and w[1] == 3] == []


def test_ac10_negative_target_words_and_negative_position_display(e2e):
    started(e2e)
    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: -100.0}, speed=100)
    e2e.wait_entry("write", "Target Position 1", -1000000, since)

    address = e2e.config.points.get("Target Position 1").start_address
    client = ModbusTcpClient("127.0.0.1", port=e2e.sim.port, timeout=2, retries=0)
    assert client.connect()
    try:
        rr = client.read_holding_registers(address, count=2, device_id=0)
    finally:
        client.close()
    assert rr.registers == [48576, 65520]  # 低位在前

    e2e.wait_finished(ev, 30000)
    e2e.sim_call("set_position", 1, -2.3456)
    row = e2e.app.axis_page.rows[1]
    e2e.until(lambda: row.position_label.text() == "-2.3456 mm")


def test_ac11_precheck_failure_writes_nothing_and_names_axis(e2e):
    e2e.prepare(homed=(1, 3, 4, 5, 6, 7))  # Y 未原點復歸
    app = e2e.start()
    e2e.wait_unlocked()

    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: 5.0, 2: 5.0})
    e2e.until(lambda: e2e.rejected(ev))
    reason = "第 2 軸（Y）：Find Home Finish = 0（尚未原點復歸）"
    assert reason in e2e.rejected(ev)[0].reasons
    e2e.until(app.window.event_dialog.isVisible)
    assert reason in app.window.event_dialog.text()
    e2e.settle(500)
    assert e2e.writes(since) == []

    e2e.prepare(servo=(), homed=(2,))
    e2e.until(lambda: lit(app.axis_page.rows[2].lamps["home_finish"]))
    ev = len(e2e.events)
    e2e.move({1: 150.0, 2: 5.0})
    e2e.until(lambda: e2e.rejected(ev))
    reasons = e2e.rejected(ev)[0].reasons
    assert [r for r in reasons if "超出軟體極限" in r] == [
        "第 1 軸（X）：目標 150.0000 mm 超出軟體極限 -100.0000 mm ~ 100.0000 mm"
    ]
    assert "第 1 軸（X）：目標 150.0000 mm 超出軟體極限" in app.window.event_dialog.text()
    e2e.settle(500)
    assert e2e.writes(since) == []


def test_ac12_alarm_on_one_axis_others_continue(e2e):
    started(e2e)
    app = e2e.app
    ev, since, first_sample = len(e2e.events), e2e.mark(), len(e2e.samples)
    alarm = e2e.when(
        e2e.logged("state", "Busy 2", True, since),
        lambda: (time.sleep(0.2), e2e.sim_call("inject_alarm", 2, 2, False)),
    )
    e2e.move({1: 20.0, 2: 20.0})
    alarm.check()
    e2e.until(lambda: app.axis_page.rows[2].property("state") == "fault")
    finished = e2e.wait_finished(ev, 30000)

    assert finished.results[1].kind == ResultKind.DONE
    assert finished.results[2].kind == ResultKind.ALARM
    assert finished.results[2].alarm_code == 2
    assert e2e.model("Current Position 1") == 200000  # X 繼續移動到目標
    alarm_at = e2e.find("state", "Alarm Code 2", 2, since)[0]
    x_done = e2e.find("state", "Busy 1", False, since)[-1]
    assert alarm_at < x_done

    x_moving = [
        s for s in e2e.samples[first_sample:] if s.snapshot is not None and s.snapshot.axis(1).busy
    ]
    assert any(s.snapshot.axis(2).alarm_code == 2 for s in x_moving)
    assert all(s.enabled == frozenset() for s in x_moving)

    detail = app.window.messages[-1].detail
    assert "第 1 軸（X）：完成" in detail
    assert "第 2 軸（Y）：Alarm（2）" in detail
    assert app.window.event_dialog.isVisible()
    assert "第 2 軸（Y）：Alarm（2）" in app.window.event_dialog.text()


# ---- 逾時與提早結束（AC-13、AC-41、AC-14、AC-37） ----


def test_ac13_ac41_timeouts_write_back_zero_and_unlock(e2e):
    started(e2e)
    ev = len(e2e.events)
    e2e.move({1: 5.0})
    assert kinds(e2e.wait_finished(ev)) == {1: ResultKind.DONE}
    e2e.wait_unlocked()

    # 不複製命令碼：等命令碼複製逾時
    e2e.sim_call("set_fault", 1, no_reply=False, no_copy=True)
    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: 10.0})
    finished = e2e.wait_finished(ev, 20000)
    assert finished.results[1].kind == ResultKind.TIMEOUT
    assert finished.results[1].wait_point == WaitPoint.COPY
    writes = e2e.writes(since)
    assert ("Request 1", True) not in writes
    assert writes[-1] == ("PC Action Code 1", 0)  # AC-41
    start = e2e.entry(e2e.find("write", "PC Action Code 1", 2, since)[0]).time
    end = e2e.entry(e2e.find("write", "PC Action Code 1", 0, since)[-1]).time
    assert 5.0 <= end - start <= 5.0 + 0.5
    e2e.wait_unlocked()

    # 不回 Reply：等 Reply = 1 逾時
    e2e.sim_call("set_fault", 1, no_reply=True, no_copy=False)
    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: 10.0})
    finished = e2e.wait_finished(ev, 20000)
    assert finished.results[1].kind == ResultKind.TIMEOUT
    assert finished.results[1].wait_point == WaitPoint.REPLY_ON
    req_on = e2e.find("write", "Request 1", True, since)[0]
    req_off = e2e.find("write", "Request 1", False, since)
    assert req_off and req_off[0] > req_on
    assert 5.0 <= e2e.entry(req_off[0]).time - e2e.entry(req_on).time <= 5.0 + 0.5
    assert e2e.find("write", "PC Action Code 1", 0, req_on)  # AC-41
    e2e.wait_unlocked()


def test_ac41_no_copy_then_alarm_writes_back_without_request(e2e):
    started(e2e)
    e2e.sim_call("set_fault", 1, no_reply=False, no_copy=True)
    ev, since = len(e2e.events), e2e.mark()
    alarm = e2e.when(
        e2e.logged("write", "PC Action Code 1", 2, since),
        lambda: (time.sleep(0.5), e2e.sim_call("inject_alarm", 1, 5, False)),
    )
    e2e.move({1: 10.0})
    alarm.check()
    finished = e2e.wait_finished(ev)
    assert finished.results[1].kind == ResultKind.ALARM
    writes = e2e.writes(since)
    assert ("Request 1", True) not in writes
    assert writes[-2:] == [("Request 1", False), ("PC Action Code 1", 0)]


def test_ac41_no_reply_then_estop_writes_back_after_request(e2e):
    started(e2e)
    e2e.sim_call("set_fault", 1, no_reply=True, no_copy=False)
    ev, since = len(e2e.events), e2e.mark()
    estop = e2e.when(
        e2e.logged("write", "Request 1", True, since),
        lambda: (time.sleep(0.5), e2e.sim_call("set_estop", True)),
    )
    e2e.move({1: 10.0})
    estop.check()
    finished = e2e.wait_finished(ev)
    assert finished.results[1].kind == ResultKind.ESTOP
    req_on = e2e.find("write", "Request 1", True, since)[0]
    after = [(e.tag, e.value) for e in e2e.write_entries(req_on + 1)]
    assert after == [("Request 1", False), ("PC Action Code 1", 0)]


def test_ac14_estop_during_move_then_reset_requires_home(e2e):
    started(e2e)
    app = e2e.app
    ev, since = len(e2e.events), e2e.mark()
    estop = e2e.when(
        e2e.logged("state", "Busy 1", True, since),
        lambda: (time.sleep(0.3), e2e.sim_call("set_estop", True)),
    )
    e2e.move({1: 30.0})
    estop.check()
    assert kinds(e2e.wait_finished(ev)) == {1: ResultKind.ESTOP}

    e2e.sim_call("set_estop", False)
    e2e.until(lambda: app.window.estop_label.text() == "急停：正常")
    e2e.wait_unlocked()
    ev = len(e2e.events)
    e2e.click(app.window.reset_button)
    finished = e2e.wait_finished(ev, 20000)
    assert kinds(finished) == {n: ResultKind.RESET_OK for n in AXES}
    for n in AXES:
        assert e2e.model(f"Alarm Code {n}") == 0
        assert e2e.model(f"Ready {n}") is True

    e2e.wait_unlocked()
    page = app.axis_page
    e2e.until(lambda: all(page.rows[n].alarm_label.text() == "0" for n in AXES))
    assert not any(lit(lamp) for lamp in page.home_finish_lamps.values())
    assert not any(lit(page.rows[n].lamps["home_finish"]) for n in AXES)
    assert not any(b.isEnabled() for b in page.jog_pad.step_buttons.values())

    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: 5.0})
    e2e.until(lambda: e2e.rejected(ev))
    assert e2e.rejected(ev)[0].reasons == ("第 1 軸（X）：Find Home Finish = 0（尚未原點復歸）",)
    e2e.settle(300)
    assert e2e.writes(since) == []


def test_ac37_consecutive_moves_wait_for_copy_of_new_code(e2e):
    started(e2e)
    since = e2e.mark()
    for target in (5.0, 10.0):
        ev = len(e2e.events)
        e2e.move({1: target})
        assert kinds(e2e.wait_finished(ev)) == {1: ResultKind.DONE}
        e2e.wait_unlocked()

    writes = axis_writes(e2e, 1, since)
    offs = [i for i, w in enumerate(writes) if w == ("Request 1", False)]
    assert len(offs) == 2
    assert all(writes[i + 1] == ("PC Action Code 1", 0) for i in offs)

    first_off = e2e.find("write", "Request 1", False, since)[0]
    second_pc = e2e.find("write", "PC Action Code 1", 2, since)[1]
    second_req = e2e.find("write", "Request 1", True, since)[1]
    zero = [i for i in e2e.find("state", "PLC Action Code 1", 0, since) if i > first_off]
    copied = [i for i in e2e.find("state", "PLC Action Code 1", 2, since) if i > second_pc]
    assert zero and zero[0] < second_pc  # 第二次之前 PLC Action Code 已回到 0
    assert copied and copied[0] < second_req  # 由 0 變為 2 之後才送 Request = 1


# ---- 原點復歸（AC-15 ~ AC-18） ----


def test_ac15_home_all_lift_axis_first_then_others_together(e2e):
    e2e.prepare(homed=(), positions={n: 5.0 for n in AXES})
    app = e2e.start()
    e2e.wait_unlocked()
    ev, since = len(e2e.events), e2e.mark()
    e2e.click(app.axis_page.home_all_button)
    finished = e2e.wait_finished(ev, 30000)
    assert kinds(finished) == {n: ResultKind.DONE for n in AXES}

    lift_request = e2e.find("write", "Request 7", True, since)[0]
    lift_done = e2e.find("state", "Find Home Finish 7", True, since)[0]
    others = [
        i
        for i, e in enumerate(e2e.entries())
        if i >= since and e.kind == "write" and axis_of(e.tag) != 7
    ]
    assert others and lift_request < lift_done < min(others)

    starts = [e2e.entry(e2e.find("write", f"PC Action Code {n}", 1, since)[0]).time for n in AXES]
    ends = [
        e2e.entry(e2e.find("state", f"Find Home Finish {n}", True, since)[0]).time for n in AXES
    ]
    assert max(starts[:6]) - min(starts[:6]) < 0.1  # 第 1 ~ 6 軸同一輪開始
    assert max(starts[:6]) < min(ends[:6])  # 彼此同時進行
    for n in AXES:
        assert e2e.model(f"Find Home Finish {n}") is True


def test_ac16_home_all_lift_alarm_skips_other_axes(e2e):
    e2e.prepare(homed=(), positions={7: 50.0})
    app = e2e.start()
    e2e.wait_unlocked()
    ev, since = len(e2e.events), e2e.mark()
    alarm = e2e.when(
        e2e.logged("write", "Request 7", True, since),
        lambda: (time.sleep(0.5), e2e.sim_call("inject_alarm", 7, 6, False)),
    )
    e2e.click(app.axis_page.home_all_button)
    alarm.check()
    finished = e2e.wait_finished(ev, 30000)
    assert finished.results[7].kind == ResultKind.ALARM
    assert {n: finished.results[n].kind for n in range(1, 7)} == {
        n: ResultKind.NOT_RUN for n in range(1, 7)
    }
    assert [w for w in e2e.writes(since) if axis_of(w[0]) != 7] == []
    e2e.until(app.window.event_dialog.isVisible)
    text = app.window.event_dialog.text()
    names = {1: "X", 2: "Y", 3: "Z", 4: "RX", 5: "RY", 6: "RZ"}
    for n, name in names.items():
        assert f"第 {n} 軸（{name}）：未執行" in text


def test_ac17_home_finish_delay_still_counts_as_operating(e2e):
    e2e.prepare(homed=(), positions={4: 5.0})
    app = e2e.start()
    e2e.wait_unlocked()
    e2e.click_row(4)
    ev, first_sample = len(e2e.events), len(e2e.samples)
    e2e.click(app.axis_page.home_one_button)
    assert kinds(e2e.wait_finished(ev)) == {4: ResultKind.DONE}

    samples = e2e.samples[first_sample:]
    busy_seen = next(i for i, s in enumerate(samples) if s.snapshot.axis(4).busy)
    gap = []
    for s in samples[busy_seen:]:
        st = s.snapshot.axis(4)
        if st.home_finish:
            break
        if not st.busy:
            gap.append(s)  # Busy = 0 且 Find Home Finish 尚未 = 1
    assert gap
    for s in gap:
        assert s.status.operating and s.status.locked
        assert s.enabled == frozenset()
        assert s.tray_state == TrayState.BUSY


def test_ac18_single_axis_home_writes_only_selected_axis(e2e):
    e2e.prepare(homed=(), positions={4: 2.0})
    app = e2e.start()
    e2e.wait_unlocked()
    e2e.click_row(4)
    assert app.axis_page.selected_axis == 4
    ev, since = len(e2e.events), e2e.mark()
    e2e.click(app.axis_page.home_one_button)
    assert kinds(e2e.wait_finished(ev)) == {4: ResultKind.DONE}
    writes = e2e.writes(since)
    assert writes and all(axis_of(tag) == 4 for tag, _ in writes)


# ---- 步進與 JOG（AC-19 ~ AC-21） ----


def test_ac19_step_long_from_10mm_and_soft_limit(e2e):
    e2e.prepare(positions={1: 10.0})
    app = e2e.start()
    e2e.wait_unlocked()
    pad = app.axis_page.jog_pad
    e2e.click(pad.distance_buttons["long"])
    ev, since = len(e2e.events), e2e.mark()
    e2e.click(pad.step_buttons[(1, +1)])
    assert kinds(e2e.wait_finished(ev)) == {1: ResultKind.DONE}
    writes = e2e.writes(since)
    assert ("Target Position 1", 200000) in writes
    assert ("PC Action Code 1", 2) in writes

    e2e.wait_unlocked()
    e2e.sim_call("set_position", 1, 95.0)
    e2e.until(lambda: app.axis_page.rows[1].position_label.text() == "95.0000 mm")
    ev, since = len(e2e.events), e2e.mark()
    e2e.click(pad.step_buttons[(1, +1)])
    e2e.until(lambda: e2e.rejected(ev))
    assert any("超出軟體極限" in r for r in e2e.rejected(ev)[0].reasons)
    e2e.until(app.window.event_dialog.isVisible)
    assert "超出軟體極限" in app.window.event_dialog.text()
    e2e.settle(300)
    assert e2e.writes(since) == []


def post_release(button) -> None:
    """從另一條執行緒送出滑鼠放開（如同 OS 送進事件佇列的輸入）。"""
    pos = QPointF(button.rect().center())
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        pos,
        button.mapToGlobal(pos),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.postEvent(button, event)


@pytest.mark.xfail(strict=True, reason=FREEZE_BUG)
def test_ac20_jog_release_writes_zero_within_100ms(e2e, qtbot):
    started(e2e)
    jog_plus = e2e.app.axis_page.jog_plus
    since = e2e.mark()
    released: list[float] = []

    def release():
        time.sleep(0.6)  # 按住 0.6 秒
        released.append(time.time())
        post_release(jog_plus)

    hold = e2e.when(e2e.logged("write", "JOG Plus 1", True, since), release)
    qtbot.mousePress(jog_plus, Qt.MouseButton.LeftButton)
    p1 = e2e.model("Current Position 1")
    time.sleep(0.2)
    assert e2e.model("Current Position 1") > p1  # 按住期間持續增加
    hold.check()
    zero = e2e.wait_entry("write", "JOG Plus 1", False, since)
    assert e2e.entry(zero).time - released[0] <= 0.1
    p2 = e2e.model("Current Position 1")
    time.sleep(0.3)
    assert e2e.model("Current Position 1") == p2  # 位置停止


def test_ac20_focus_lost_while_jog_held_writes_zero_within_100ms(e2e, qtbot):
    started(e2e)
    window = e2e.app.window
    jog_plus = e2e.app.axis_page.jog_plus
    other = QWidget()
    qtbot.addWidget(other)
    other.setWindowTitle("其他視窗")
    other.show()
    qtbot.waitExposed(other)
    window.activateWindow()
    e2e.until(window.isActiveWindow)
    e2e.settle(500)  # other.show() 的啟用可能晚到：確認焦點穩定在主視窗
    if not window.isActiveWindow():
        window.activateWindow()
        e2e.until(window.isActiveWindow)
        e2e.settle(500)
    assert window.isActiveWindow()

    since = e2e.mark()
    qtbot.mousePress(jog_plus, Qt.MouseButton.LeftButton)
    e2e.wait_entry("write", "JOG Plus 1", True, since)
    e2e.until(lambda: e2e.model("Current Position 1") > 0)
    e2e.until(lambda: e2e.app.last_status.locked)  # 按住中
    assert jog_plus.is_held

    switched = time.time()
    other.activateWindow()
    zero = e2e.wait_entry("write", "JOG Plus 1", False, since)
    assert e2e.entry(zero).time - switched <= 0.1
    assert not jog_plus.is_held
    p = e2e.model("Current Position 1")
    time.sleep(0.3)
    assert e2e.model("Current Position 1") == p


def test_ac21_positive_limit_disables_jog_plus_only(e2e):
    started(e2e)
    page = e2e.app.axis_page
    assert page.jog_plus.isEnabled() and page.jog_minus.isEnabled()
    e2e.sim_call("set_limit", 1, "pos", True)
    e2e.until(lambda: not page.jog_plus.isEnabled())
    assert page.jog_minus.isEnabled()
    assert page.jog_plus.toolTip() == "正極限已觸發"


# ---- Servo 與 Reset Alarm（AC-22 ~ AC-24） ----


def test_ac22_no_servo_write_on_start_and_quit_servo_on_writes_all(e2e):
    app = e2e.start()
    e2e.wait_unlocked()
    e2e.settle(600)
    assert [w for w in e2e.writes() if w[0].startswith("Servo Motor")] == []

    since = e2e.mark()
    e2e.click(app.window.servo_on_button)
    e2e.until(lambda: len(e2e.writes(since)) >= 7)
    e2e.settle(300)
    assert sorted(e2e.writes(since)) == sorted((f"Servo Motor {n}", True) for n in AXES)

    e2e.wait_unlocked()
    since = e2e.mark()
    e2e.quit_from_tray()
    e2e.settle(300)
    assert e2e.writes(since) == []  # 結束不寫任何點（含 Servo Motor）


def test_ac23_servo_off_and_reset_disabled_while_moving(e2e):
    started(e2e)
    window = e2e.app.window
    ev = len(e2e.events)
    e2e.move({1: 40.0}, speed=10)  # 5 mm/s，約 8 秒
    e2e.until(lambda: e2e.model("Busy 1") is True)
    e2e.until(
        lambda: not window.servo_off_button.isEnabled() and not window.reset_button.isEnabled()
    )

    since = e2e.mark()
    e2e.click(window.servo_off_button)
    e2e.click(window.reset_button)
    assert e2e.finished(ev) == []  # 點擊時仍在移動
    e2e.settle(300)
    assert [w for w in e2e.writes(since) if w[0].startswith(("Servo Motor", "Reset Alarm"))] == []
    assert e2e.model("Servo Motor 1") is True
    assert kinds(e2e.wait_finished(ev, 30000)) == {1: ResultKind.DONE}


def test_ac24_reset_only_alarm_axes_one_ok_one_fail_no_retry(e2e):
    e2e.prepare()
    e2e.sim_call("inject_alarm", 2, 5, False)
    e2e.sim_call("inject_alarm", 5, 5, True)  # 原因持續
    app = e2e.start()
    e2e.wait_unlocked()
    ev, since = len(e2e.events), e2e.mark()
    e2e.click(app.window.reset_button)
    finished = e2e.wait_finished(ev, 30000)
    assert kinds(finished) == {2: ResultKind.RESET_OK, 5: ResultKind.RESET_FAIL}

    entries = e2e.write_entries(since)
    assert {e.tag for e in entries} == {"Reset Alarm 2", "Reset Alarm 5"}
    for n in (2, 5):
        pulse = [e for e in entries if e.tag == f"Reset Alarm {n}"]
        assert [e.value for e in pulse] == [True, False]
        assert 0.5 <= pulse[1].time - pulse[0].time <= 0.5 + 0.3

    detail = app.window.messages[-1].detail
    assert "第 2 軸（Y）：復歸成功" in detail
    assert "第 5 軸（RY）：復歸失敗" in detail
    count = len(e2e.writes(since))
    e2e.settle(1500)
    assert len(e2e.writes(since)) == count  # 不自動再送
