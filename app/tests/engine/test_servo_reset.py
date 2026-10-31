"""MotionEngine 全部 Servo ON／OFF（FR-24）、全部 Reset Alarm（FR-25）與 IO Monitor 寫入。

依 spec FR-15、FR-16、FR-24、FR-25；規格書 2.4。
"""

import pytest

from plc_motion.engine import AxisResult, OperationKind, ResultKind

SERVO_TAGS = [f"Servo Motor {n}" for n in range(1, 8)]


def servo_writes(rig) -> list[tuple[str, int | bool]]:
    return [w for w in rig.writes() if w[0].startswith("Servo Motor")]


# ---- FR-24 全部 Servo ON／OFF ----


def test_servo_all_on_writes_1_to_seven_axes(rig):
    rig.connect()
    assert rig.engine.servo_all(True) == []
    assert rig.writes() == [(tag, True) for tag in SERVO_TAGS]
    rig.tick()
    assert all(rig.axis(n).servo and rig.axis(n).ready for n in range(1, 8))


def test_servo_all_off_writes_0_to_seven_axes(ready_rig):
    rig = ready_rig
    assert rig.engine.servo_all(False) == []
    assert rig.writes() == [(tag, False) for tag in SERVO_TAGS]
    rig.tick()
    assert not any(rig.axis(n).servo for n in range(1, 8))


def test_engine_lifecycle_never_writes_servo(rig):
    rig.engine.on_connected()
    rig.tick()
    rig.run(0.5)
    rig.engine.on_disconnected()
    rig.connect()
    assert servo_writes(rig) == []


def test_servo_all_rejected_when_not_connected(rig):
    reasons = rig.engine.servo_all(True)
    assert any("未連線" in r for r in reasons)
    assert rig.rejected()[-1].command == "全部 Servo ON"
    assert rig.rejected()[-1].reasons == tuple(reasons)
    assert rig.writes() == []


def test_servo_all_rejected_while_operation_in_progress(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 5.0}, relative=False, speed=100)
    before = len(rig.port.writes)
    reasons = rig.engine.servo_all(False)
    assert any("操作進行中" in r for r in reasons)
    assert rig.rejected()[-1].command == "全部 Servo OFF"
    assert len(rig.port.writes) == before


# ---- FR-25 全部 Reset Alarm ----


def test_reset_all_writes_only_alarm_axes_and_reports_success(ready_rig):
    rig = ready_rig
    rig.model.inject_alarm(2, 9, persistent=False)
    rig.model.inject_alarm(5, 9, persistent=False)
    rig.tick()

    assert rig.engine.reset_all() == []
    started = rig.started()[-1]
    assert started.kind == OperationKind.RESET
    assert started.axes == (2, 5)
    assert rig.writes() == [("Reset Alarm 2", True), ("Reset Alarm 5", True)]

    fin = rig.run_until_finished()
    assert fin.kind == OperationKind.RESET
    assert dict(fin.results) == {
        2: AxisResult(ResultKind.RESET_OK),
        5: AxisResult(ResultKind.RESET_OK),
    }
    assert rig.writes() == [
        ("Reset Alarm 2", True),
        ("Reset Alarm 5", True),
        ("Reset Alarm 2", False),
        ("Reset Alarm 5", False),
    ]
    assert rig.axis(2).alarm_code == 0 and rig.axis(2).ready
    assert not rig.engine.locked


def test_reset_pulse_length_between_setting_and_one_poll_more(ready_rig):
    rig = ready_rig
    pulse = rig.config.device.reset_alarm_pulse_ms / 1000
    rig.model.inject_alarm(1, 9, persistent=False)
    rig.tick()
    rig.engine.reset_all()
    rig.run_until_finished()

    on = next(w for w in rig.port.writes if w.tag == "Reset Alarm 1" and w.value is True)
    off = next(w for w in rig.port.writes if w.tag == "Reset Alarm 1" and w.value is False)
    assert pulse - 1e-9 <= off.t - on.t <= pulse + rig.DT + 1e-9


def test_reset_persistent_cause_fails_after_timeout_without_retry(ready_rig):
    rig = ready_rig
    rig.model.inject_alarm(3, 9, persistent=True)
    rig.model.inject_alarm(4, 9, persistent=False)
    rig.tick()
    t0 = rig.t
    rig.engine.reset_all()

    fin = rig.run_until_finished()
    assert dict(fin.results) == {
        3: AxisResult(ResultKind.RESET_FAIL),
        4: AxisResult(ResultKind.RESET_OK),
    }
    pulse = rig.config.device.reset_alarm_pulse_ms / 1000
    timeout = rig.config.device.handshake_timeout_s
    assert rig.t - t0 == pytest.approx(pulse + timeout, abs=2 * rig.DT + 1e-9)
    assert rig.writes(3) == [("Reset Alarm 3", True), ("Reset Alarm 3", False)]  # 不重試

    rig.run(1.0)
    assert rig.writes(3) == [("Reset Alarm 3", True), ("Reset Alarm 3", False)]


def test_reset_fails_when_alarm_cleared_but_ready_stays_0(ready_rig):
    rig = ready_rig
    rig.model.inject_alarm(1, 9, persistent=False)
    rig.model.master_write("Servo Motor 1", False)  # Servo OFF：Alarm 清除後 Ready 仍為 0
    rig.tick()
    rig.engine.reset_all()
    fin = rig.run_until_finished()
    assert rig.axis(1).alarm_code == 0
    assert dict(fin.results) == {1: AxisResult(ResultKind.RESET_FAIL)}


def test_reset_waiting_counts_as_operation_in_progress(ready_rig):
    rig = ready_rig
    rig.model.inject_alarm(1, 9, persistent=True)
    rig.tick()
    rig.engine.reset_all()
    rig.run(1.0)
    assert rig.engine.locked
    assert any("操作進行中" in r for r in rig.engine.lock_reasons)
    before = len(rig.port.writes)
    assert rig.engine.move({2: 1.0}, relative=False, speed=50)
    assert rig.engine.reset_all()
    assert len(rig.port.writes) == before


def test_reset_all_without_alarm_rejected_as_not_needed(ready_rig):
    rig = ready_rig
    reasons = rig.engine.reset_all()
    assert any("不需復歸" in r for r in reasons)
    assert rig.rejected()[-1].command == "全部 Reset Alarm"
    assert rig.rejected()[-1].reasons == tuple(reasons)
    assert rig.started() == []
    assert rig.writes() == []


def test_reset_all_rejected_when_not_connected(rig):
    rig.prepare()
    rig.model.inject_alarm(1, 9, persistent=False)
    reasons = rig.engine.reset_all()
    assert any("未連線" in r for r in reasons)
    assert rig.writes() == []


def test_disconnect_during_reset_ends_with_comm_lost(ready_rig):
    rig = ready_rig
    rig.model.inject_alarm(1, 9, persistent=True)
    rig.tick()
    rig.engine.reset_all()
    rig.tick()
    rig.engine.on_disconnected()
    assert dict(rig.finished()[-1].results) == {1: AxisResult(ResultKind.COMM_LOST)}


# ---- IO Monitor 寫入 ----


def test_write_point_writes_value(ready_rig):
    rig = ready_rig
    assert rig.engine.write_point("Speed 3", 42) == []
    assert rig.writes() == [("Speed 3", 42)]
    rig.tick()
    assert rig.port.read_all().raw("Speed 3") == 42


def test_write_point_rejected_when_locked(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 5.0}, relative=False, speed=100)
    before = len(rig.port.writes)
    reasons = rig.engine.write_point("Speed 3", 42)
    assert any("操作進行中" in r for r in reasons)
    assert rig.rejected()[-1].command == "IO 寫入"
    assert len(rig.port.writes) == before


def test_write_point_rejects_read_only_and_unknown_tag(ready_rig):
    rig = ready_rig
    assert any("Read Only" in r for r in rig.engine.write_point("Ready 1", True))
    assert any("不存在" in r for r in rig.engine.write_point("No Such Tag", 1))
    assert rig.writes() == []
