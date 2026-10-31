"""MotionEngine 全域動作鎖（FR-15 ~ FR-17）與通訊中斷結束操作（FR-13 操作面）。"""

from plc_motion.engine import AxisResult, ResultKind


def test_locked_before_on_connected_and_commands_rejected(rig):
    rig.prepare()
    rig.tick()
    assert rig.engine.locked
    assert any("未連線" in r for r in rig.engine.lock_reasons)

    for call, command in (
        (lambda: rig.engine.move({1: 1.0}, relative=False, speed=50), "移動"),
        (lambda: rig.engine.step(1, 1.0, speed=50), "步進"),
        (lambda: rig.engine.home({1}), "原點復歸"),
    ):
        reasons = call()
        assert reasons
        assert rig.rejected()[-1].command == command
        assert rig.rejected()[-1].reasons == tuple(reasons)
    assert rig.writes() == []


def test_locked_after_on_connected_until_first_tick(rig):
    rig.prepare()
    rig.engine.on_connected()
    assert rig.engine.locked
    assert any("第一次輪詢" in r for r in rig.engine.lock_reasons)
    reasons = rig.engine.move({1: 1.0}, relative=False, speed=50)
    assert any("第一次輪詢" in r for r in reasons)
    assert rig.writes() == []

    rig.tick()
    assert not rig.engine.locked
    assert rig.engine.lock_reasons == []


def test_locked_while_operation_in_progress_and_released_after(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 5.0}, relative=False, speed=100)
    assert rig.engine.locked
    assert any("操作進行中" in r for r in rig.engine.lock_reasons)

    writes_before = len(rig.port.writes)
    reasons = rig.engine.move({2: 1.0}, relative=False, speed=50)
    assert any("操作進行中" in r for r in reasons)
    assert rig.rejected()[-1].reasons == tuple(reasons)
    assert len(rig.port.writes) == writes_before

    rig.run_until_finished()
    assert not rig.engine.locked
    assert all(not rig.axis(n).busy for n in range(1, 8))


def test_locked_while_any_axis_busy(ready_rig):
    rig = ready_rig
    rig.model.master_write("JOG Plus 3", True)  # 由 PLC 端製造 Busy（不經引擎）
    rig.tick()
    assert rig.axis(3).busy
    assert rig.engine.locked
    assert any("第 3 軸" in r and "Busy" in r for r in rig.engine.lock_reasons)
    reasons = rig.engine.home({1})
    assert any("第 3 軸" in r and "Busy" in r for r in reasons)
    assert rig.writes() == []

    rig.model.master_write("JOG Plus 3", False)
    rig.tick()
    rig.tick()
    assert not rig.engine.locked


def test_on_disconnected_ends_operation_with_comm_lost(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 50.0, 2: 50.0}, relative=False, speed=10)
    rig.run(1.0)
    writes_before = len(rig.port.writes)
    rig.engine.on_disconnected()

    fin = rig.finished()
    assert len(fin) == 1
    assert dict(fin[0].results) == {
        1: AxisResult(ResultKind.COMM_LOST),
        2: AxisResult(ResultKind.COMM_LOST),
    }
    assert len(rig.port.writes) == writes_before  # 通訊中斷不補寫
    assert rig.engine.locked
    assert any("未連線" in r for r in rig.engine.lock_reasons)


def test_on_disconnected_during_home_marks_pending_axes_comm_lost(ready_rig):
    rig = ready_rig
    rig.model.set_position(7, 5.0)
    rig.tick()
    rig.engine.home({1, 7})
    rig.run(0.5)
    rig.engine.on_disconnected()
    assert dict(rig.finished()[-1].results) == {
        1: AxisResult(ResultKind.COMM_LOST),
        7: AxisResult(ResultKind.COMM_LOST),
    }


def test_ticks_after_disconnect_do_not_unlock_until_reconnected(ready_rig):
    rig = ready_rig
    rig.engine.on_disconnected()
    rig.tick()
    assert rig.engine.locked
    rig.connect()
    assert not rig.engine.locked
