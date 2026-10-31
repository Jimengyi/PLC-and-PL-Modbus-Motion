"""MotionEngine 移動交握（Code 2）、等待點逾時、前置檢查、相對移動與多軸並行。

依 spec FR-18 ~ FR-22、規格書 2.1 ~ 2.3、3.4。
"""

import pytest

from plc_motion.engine import (
    AxisResult,
    OperationKind,
    ResultKind,
    WaitPoint,
)

MOVE_SEQUENCE_X_10MM = [
    ("Target Position 1", 100000),
    ("Speed 1", 30),
    ("PC Action Code 1", 2),
    ("Request 1", True),
    ("Request 1", False),
    ("PC Action Code 1", 0),
]
WRITE_BACK_1 = [("Request 1", False), ("PC Action Code 1", 0)]


def has(reasons: list[str], axis: int, item: str) -> bool:
    return any(f"第 {axis} 軸" in r and item in r for r in reasons)


# ---- FR-18 寫入順序 ----


def test_single_axis_code2_writes_follow_fr18(ready_rig):
    rig = ready_rig
    assert rig.engine.move({1: 10.0}, relative=False, speed=30) == []
    fin = rig.run_until_finished()

    assert rig.writes() == MOVE_SEQUENCE_X_10MM
    w = rig.port.writes
    assert w[3].plc_action_code == 2  # Request = 1 只在 PLC 複製命令碼之後
    assert w[4].reply is True  # Request = 0 在讀到 Reply = 1 之後
    assert w[4].t == w[5].t  # Request = 0 與 PC Action Code = 0 同一輪寫
    assert fin.kind == OperationKind.MOVE
    assert dict(fin.results) == {1: AxisResult(ResultKind.DONE)}
    assert rig.axis(1).position == pytest.approx(10.0)


def test_operation_started_event_carries_axes_and_params(ready_rig):
    rig = ready_rig
    rig.engine.move({2: 3.0, 1: 1.0}, relative=False, speed=30)
    started = rig.started()[-1]
    assert started.kind == OperationKind.MOVE
    assert started.axes == (1, 2)
    assert started.params["speed"] == 30


def test_second_code2_waits_until_plc_action_code_goes_0_to_2(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 5.0}, relative=False, speed=100)
    rig.run_until_finished()
    assert rig.axis(1).plc_action_code == 0

    first_count = len(rig.port.writes)
    rig.engine.move({1: 8.0}, relative=False, speed=100)
    fin = rig.run_until_finished()
    second = rig.port.writes[first_count:]
    pc = next(w for w in second if w.tag == "PC Action Code 1" and w.value == 2)
    requests = [w for w in second if w.tag == "Request 1" and w.value]
    assert pc.plc_action_code == 0
    assert len(requests) == 1
    assert requests[0].plc_action_code == 2
    assert requests[0].t > pc.t
    assert fin.results[1].kind == ResultKind.DONE


@pytest.mark.parametrize(("speed", "written"), [(150, 100), (0, 1)])
def test_speed_is_clamped_to_1_100(ready_rig, speed, written):
    rig = ready_rig
    rig.engine.move({1: 1.0}, relative=False, speed=speed)
    assert ("Speed 1", written) in rig.writes()


# ---- 等待點逾時與提早結束 ----


def test_copy_timeout_writes_back_and_never_requests(ready_rig):
    rig = ready_rig
    rig.model.set_fault(1, no_reply=False, no_copy=True)
    t0 = rig.t
    rig.engine.move({1: 5.0}, relative=False, speed=50)
    fin = rig.run_until_finished()

    assert fin.results[1] == AxisResult(ResultKind.TIMEOUT, wait_point=WaitPoint.COPY)
    assert 5.0 - 1e-9 <= rig.t - t0 <= 5.0 + 2 * rig.DT
    assert ("Request 1", True) not in rig.writes()
    assert rig.writes()[-2:] == WRITE_BACK_1


def test_reply_on_timeout_writes_back(ready_rig):
    rig = ready_rig
    rig.model.set_fault(1, no_reply=True, no_copy=False)
    rig.engine.move({1: 5.0}, relative=False, speed=50)
    fin = rig.run_until_finished()

    assert fin.results[1] == AxisResult(ResultKind.TIMEOUT, wait_point=WaitPoint.REPLY_ON)
    request_t = next(w.t for w in rig.port.writes if w.tag == "Request 1" and w.value)
    assert 5.0 - 1e-9 <= rig.t - request_t <= 5.0 + 2 * rig.DT
    assert rig.writes() == [
        ("Target Position 1", 50000),
        ("Speed 1", 50),
        ("PC Action Code 1", 2),
        ("Request 1", True),
        *WRITE_BACK_1,
    ]


def test_reply_off_timeout_writes_back(ready_rig):
    rig = ready_rig
    rig.port.swallow.append(("Request 1", False))  # PLC 收不到 Request = 0，Reply 一直是 1
    rig.engine.move({1: 5.0}, relative=False, speed=50)
    fin = rig.run_until_finished()

    assert fin.results[1] == AxisResult(ResultKind.TIMEOUT, wait_point=WaitPoint.REPLY_OFF)
    first_clear_t = next(w.t for w in rig.port.writes if w.tag == "Request 1" and not w.value)
    assert 5.0 - 1e-9 <= rig.t - first_clear_t <= 5.0 + 2 * rig.DT
    assert rig.writes()[-4:] == WRITE_BACK_1 + WRITE_BACK_1  # 交握的清零＋逾時寫回


def test_alarm_while_waiting_ends_axis_and_writes_back(ready_rig):
    rig = ready_rig
    rig.model.set_fault(1, no_reply=True, no_copy=False)
    rig.engine.move({1: 5.0}, relative=False, speed=50)
    rig.run(1.0)
    rig.model.inject_alarm(1, 9, persistent=False)
    fin = rig.run_until_finished(limit=1.0)

    assert fin.results[1] == AxisResult(ResultKind.ALARM, alarm_code=9)
    assert rig.writes()[-2:] == WRITE_BACK_1


def test_estop_while_waiting_ends_axis_and_writes_back(ready_rig):
    rig = ready_rig
    rig.model.set_fault(1, no_reply=False, no_copy=True)
    rig.engine.move({1: 5.0}, relative=False, speed=50)
    rig.run(1.0)
    rig.model.set_estop(True)
    fin = rig.run_until_finished(limit=1.0)

    assert fin.results[1] == AxisResult(ResultKind.ESTOP)
    assert rig.writes()[-2:] == WRITE_BACK_1


def test_waiting_for_completion_has_no_timeout(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 90.0}, relative=False, speed=1)  # 0.5 mm/s，約 180 秒
    rig.run(20.0)
    assert rig.finished() == []
    assert rig.engine.locked
    assert rig.axis(1).busy


# ---- FR-21 前置檢查 ----


def test_precheck_lists_every_failing_axis_and_item_with_zero_writes(rig):
    rig.prepare(axes=[2, 3, 4, 5, 6, 7])  # 第 1 軸 Servo OFF → Ready = 0、未原點復歸
    rig.model.master_write("Find Home Finish 2", False)
    rig.model.inject_alarm(4, 7, persistent=True)
    rig.connect()

    reasons = rig.engine.move({1: 1.0, 2: 1.0, 3: 150.0, 4: 1.0, 5: 1.0}, relative=False, speed=50)

    assert has(reasons, 1, "Ready")
    assert has(reasons, 1, "Find Home Finish")
    assert has(reasons, 2, "Find Home Finish")
    assert has(reasons, 3, "軟體極限")
    assert has(reasons, 4, "Alarm")
    assert not any("第 5 軸" in r for r in reasons)
    assert rig.writes() == []
    assert rig.rejected()[-1].reasons == tuple(reasons)
    assert rig.started() == []


def test_precheck_rejects_when_estop_pressed(ready_rig):
    rig = ready_rig
    rig.model.set_estop(True)
    rig.tick()
    reasons = rig.engine.move({1: 1.0}, relative=False, speed=50)
    assert any("急停" in r for r in reasons)
    assert rig.writes() == []


def test_precheck_rejects_busy_axis(ready_rig):
    rig = ready_rig
    rig.model.master_write("JOG Plus 2", True)
    rig.tick()
    assert rig.axis(2).busy
    reasons = rig.engine.move({2: 1.0}, relative=False, speed=50)
    assert has(reasons, 2, "Busy")
    assert rig.writes() == []


def test_precheck_rejects_when_not_connected(rig):
    rig.prepare()
    rig.tick()
    reasons = rig.engine.move({1: 1.0}, relative=False, speed=50)
    assert any("未連線" in r for r in reasons)
    assert rig.writes() == []


def test_relative_target_outside_soft_limit_is_rejected(ready_rig):
    rig = ready_rig
    rig.model.set_position(1, 95.0)
    rig.tick()
    reasons = rig.engine.move({1: 10.0}, relative=True, speed=50)
    assert has(reasons, 1, "軟體極限")
    assert rig.writes() == []


# ---- FR-19、FR-20 相對移動與步進 ----


def test_relative_move_target_is_latest_position_plus_delta(ready_rig):
    rig = ready_rig
    rig.model.set_position(1, 10.0)
    rig.tick()
    assert rig.engine.move({1: -2.5}, relative=True, speed=50) == []
    fin = rig.run_until_finished()

    assert ("Target Position 1", 75000) in rig.writes()
    assert [v for t, v in rig.writes() if t == "PC Action Code 1"] == [2, 0]
    assert fin.results[1].kind == ResultKind.DONE
    assert rig.axis(1).position == pytest.approx(7.5)


def test_step_on_lift_axis_sends_code2_relative(ready_rig):
    rig = ready_rig
    assert rig.engine.step(7, 1.25, speed=40) == []
    fin = rig.run_until_finished()

    assert ("Target Position 7", 125) in rig.writes()
    assert [v for t, v in rig.writes() if t == "PC Action Code 7"] == [2, 0]
    assert rig.started()[-1].kind == OperationKind.STEP
    assert fin.kind == OperationKind.STEP
    assert rig.axis(7).position == pytest.approx(1.25)


# ---- FR-22 多軸並行 ----


def test_multi_axis_handshakes_run_concurrently(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 2.0, 2: 2.0, 3: 2.0}, relative=False, speed=100)
    rig.run_until_finished()
    w = rig.port.writes
    request_on = [i for i, x in enumerate(w) if x.tag.startswith("Request") and x.value]
    request_off = [i for i, x in enumerate(w) if x.tag.startswith("Request") and not x.value]
    assert len(request_on) == 3
    assert max(request_on) < min(request_off)  # 三軸都發出 Request 後才有任一軸清零


def test_one_axis_alarm_does_not_stop_others_and_finish_waits_for_all(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 20.0, 2: 20.0, 3: 20.0}, relative=False, speed=10)  # 5 mm/s，約 4 秒
    rig.run(1.0)
    rig.model.inject_alarm(2, 5, persistent=False)
    rig.tick()
    assert rig.finished() == []

    fin = rig.run_until_finished()
    assert len(rig.finished()) == 1
    assert dict(fin.results) == {
        1: AxisResult(ResultKind.DONE),
        2: AxisResult(ResultKind.ALARM, alarm_code=5),
        3: AxisResult(ResultKind.DONE),
    }
    assert rig.axis(1).position == pytest.approx(20.0)
    assert rig.axis(3).position == pytest.approx(20.0)
    assert len(rig.writes(2)) == 6  # 已進入等完成，Alarm 不再補寫
