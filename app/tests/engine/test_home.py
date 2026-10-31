"""MotionEngine 原點復歸（Code 1）：寫入順序、完成判斷、第 7 軸先做（FR-18、FR-21、FR-23）。"""

from plc_motion.engine import AxisResult, OperationKind, ResultKind


def has(reasons: list[str], axis: int, item: str) -> bool:
    return any(f"第 {axis} 軸" in r and item in r for r in reasons)


def test_code1_skips_target_and_speed(ready_rig):
    rig = ready_rig
    rig.model.set_position(1, 2.0)
    rig.tick()
    assert rig.engine.home({1}) == []
    fin = rig.run_until_finished()

    assert rig.writes() == [
        ("PC Action Code 1", 1),
        ("Request 1", True),
        ("Request 1", False),
        ("PC Action Code 1", 0),
    ]
    assert rig.port.writes[1].plc_action_code == 1
    assert fin.kind == OperationKind.HOME
    assert dict(fin.results) == {1: AxisResult(ResultKind.DONE)}
    assert rig.axis(1).home_finish


def test_busy_0_without_home_finish_is_not_done_and_stays_locked(ready_rig):
    rig = ready_rig
    rig.model.set_position(1, 2.0)
    rig.tick()
    rig.engine.home({1})

    seen_gap = False
    while not rig.finished():
        rig.tick()
        a = rig.axis(1)
        if a.ready and not a.busy and not a.home_finish:
            seen_gap = True
            assert rig.finished() == []
            assert rig.engine.locked
        assert rig.t < 30
    assert seen_gap
    assert rig.axis(1).home_finish


def test_home_precheck_does_not_require_home_finish(rig):
    rig.prepare(homed=False)
    rig.connect()
    assert rig.engine.home({1, 2}) == []
    assert ("PC Action Code 1", 1) in rig.writes()
    assert ("PC Action Code 2", 1) in rig.writes()


def test_home_precheck_lists_failing_axes_with_zero_writes(rig):
    rig.prepare(axes=[2, 3, 4, 5, 6, 7], homed=False)  # 第 1 軸 Servo OFF
    rig.model.inject_alarm(3, 6, persistent=True)
    rig.connect()
    reasons = rig.engine.home({1, 2, 3})
    assert has(reasons, 1, "Ready")
    assert has(reasons, 3, "Alarm")
    assert not any("第 2 軸" in r for r in reasons)
    assert not any("Find Home Finish" in r for r in reasons)
    assert rig.writes() == []
    assert rig.rejected()[-1].command == "原點復歸"


def test_home_with_axis7_runs_axis7_first_then_others_together(ready_rig):
    rig = ready_rig
    for n in (1, 2, 7):
        rig.model.set_position(n, 1.0)
    rig.tick()
    assert rig.engine.home({1, 2, 7}) == []
    fin = rig.run_until_finished()

    w = rig.port.writes
    first7 = next(i for i, x in enumerate(w) if x.tag == "Request 7" and x.value)
    others = [i for i, x in enumerate(w) if x.tag.endswith((" 1", " 2"))]
    assert others and first7 < min(others)
    # 其餘軸在第 7 軸完成（含 Find Home Finish = 1）後同一輪開始
    starts = {x.t for x in w if x.tag in ("PC Action Code 1", "PC Action Code 2") and x.value}
    assert len(starts) == 1
    start_t = starts.pop()
    last7 = max(x.t for x in w if x.tag.endswith(" 7"))
    assert last7 < start_t
    assert rig.model.read("Find Home Finish 7")
    assert dict(fin.results) == {n: AxisResult(ResultKind.DONE) for n in (1, 2, 7)}


def test_axis7_failure_marks_others_not_run_with_zero_writes(ready_rig):
    rig = ready_rig
    rig.model.set_position(7, 5.0)
    rig.tick()
    rig.engine.home({1, 2, 7})
    rig.run(0.5)
    rig.model.inject_alarm(7, 8, persistent=False)
    fin = rig.run_until_finished(limit=2.0)

    assert dict(fin.results) == {
        1: AxisResult(ResultKind.NOT_RUN),
        2: AxisResult(ResultKind.NOT_RUN),
        7: AxisResult(ResultKind.ALARM, alarm_code=8),
    }
    assert rig.writes(1) == []
    assert rig.writes(2) == []
    assert not rig.engine.locked


def test_home_without_axis7_runs_all_together(ready_rig):
    rig = ready_rig
    rig.engine.home({3, 1})
    starts = {x.t for x in rig.port.writes if x.tag.startswith("PC Action Code") and x.value}
    assert len(starts) == 1
    assert rig.started()[-1].axes == (1, 3)
    fin = rig.run_until_finished()
    assert dict(fin.results) == {n: AxisResult(ResultKind.DONE) for n in (1, 3)}
