"""MotionEngine JOG 按下／放開（FR-26、FR-27）與 JOG 按住中鎖定（FR-15）。

依規格書 2.0.2。
"""


def has(reasons: list[str], axis: int, item: str) -> bool:
    return any(f"第 {axis} 軸" in r and item in r for r in reasons)


def test_jog_press_writes_speed_then_jog_1(ready_rig):
    rig = ready_rig
    assert rig.engine.jog_press(1, +1, speed=40) == []
    assert rig.writes() == [("Speed 1", 40), ("JOG Plus 1", True)]

    start = rig.axis(1).position
    rig.run(0.5)
    assert rig.axis(1).position > start


def test_jog_minus_uses_jog_minus_tag(ready_rig):
    rig = ready_rig
    assert rig.engine.jog_press(4, -1, speed=20) == []
    assert rig.writes() == [("Speed 4", 20), ("JOG Minus 4", True)]
    rig.engine.jog_release(4, -1)
    assert rig.writes()[-1] == ("JOG Minus 4", False)


def test_jog_speed_clamped_to_1_100(ready_rig):
    rig = ready_rig
    rig.engine.jog_press(1, +1, speed=500)
    assert rig.writes()[0] == ("Speed 1", 100)


def test_locked_while_jog_held_and_released_after(ready_rig):
    rig = ready_rig
    rig.engine.jog_press(2, +1, speed=40)
    assert rig.engine.locked
    assert any("JOG 按住中" in r for r in rig.engine.lock_reasons)

    before = len(rig.port.writes)
    reasons = rig.engine.move({1: 1.0}, relative=False, speed=50)
    assert any("JOG 按住中" in r for r in reasons)
    assert any("JOG 按住中" in r for r in rig.engine.jog_press(3, +1, speed=40))
    assert len(rig.port.writes) == before

    rig.run(0.3)
    rig.engine.jog_release(2, +1)
    assert rig.writes()[-1] == ("JOG Plus 2", False)
    rig.tick()
    rig.tick()
    assert not rig.engine.locked


def test_jog_does_not_require_home_finish(rig):
    rig.prepare(homed=False)
    rig.connect()
    assert rig.engine.jog_press(1, +1, speed=40) == []


def test_jog_press_rejects_each_fr26_condition_with_zero_writes(ready_rig):
    rig = ready_rig
    rig.model.master_write("Servo Motor 1", False)  # Ready = 0
    rig.model.inject_alarm(2, 9, persistent=False)  # Alarm
    rig.tick()

    reasons = rig.engine.jog_press(1, +1, speed=40)
    assert has(reasons, 1, "Ready = 0")
    reasons = rig.engine.jog_press(2, +1, speed=40)
    assert has(reasons, 2, "Alarm Code = 9")
    assert rig.rejected()[-1].command == "JOG"
    assert rig.rejected()[-1].reasons == tuple(reasons)
    assert rig.writes() == []


def test_jog_press_rejected_during_estop(ready_rig):
    rig = ready_rig
    rig.model.set_estop(True)
    rig.tick()
    reasons = rig.engine.jog_press(1, +1, speed=40)
    assert any("急停" in r for r in reasons)
    assert rig.writes() == []


def test_jog_press_rejected_toward_triggered_b_contact_limit(ready_rig):
    rig = ready_rig
    rig.model.set_limit(3, "pos", True)
    rig.tick()
    assert rig.model.read("Positive Limit 3") is False  # B 接點：觸發時讀到 0

    reasons = rig.engine.jog_press(3, +1, speed=40)
    assert has(reasons, 3, "正極限")
    assert rig.writes() == []

    assert rig.engine.jog_press(3, -1, speed=40) == []  # 反方向可 JOG
    assert rig.writes()[-1] == ("JOG Minus 3", True)


def test_jog_press_rejected_toward_triggered_negative_limit(ready_rig):
    rig = ready_rig
    rig.model.set_limit(5, "neg", True)
    rig.tick()
    assert has(rig.engine.jog_press(5, -1, speed=40), 5, "負極限")
    assert rig.writes() == []


def test_jog_press_rejected_when_another_axis_busy(ready_rig):
    rig = ready_rig
    rig.model.master_write("JOG Plus 6", True)  # 由 PLC 端製造 Busy（不經引擎）
    rig.tick()
    reasons = rig.engine.jog_press(1, +1, speed=40)
    assert has(reasons, 6, "Busy")
    assert rig.writes() == []


def test_jog_press_rejected_when_not_connected(rig):
    rig.prepare()
    reasons = rig.engine.jog_press(1, +1, speed=40)
    assert any("未連線" in r for r in reasons)
    assert rig.writes() == []


def test_jog_release_writes_0_even_while_locked(ready_rig):
    rig = ready_rig
    rig.engine.move({2: 5.0}, relative=False, speed=100)
    assert rig.engine.locked
    rig.engine.jog_release(1, +1)
    assert rig.writes()[-1] == ("JOG Plus 1", False)


def test_jog_release_stops_axis(ready_rig):
    rig = ready_rig
    rig.engine.jog_press(1, +1, speed=40)
    rig.run(0.5)
    rig.engine.jog_release(1, +1)
    rig.tick()
    stopped = rig.axis(1).position
    rig.run(0.5)
    assert rig.axis(1).position == stopped
    assert not rig.axis(1).busy


def test_jog_held_tag_follows_press_release_and_disconnect(ready_rig):
    rig = ready_rig
    assert rig.engine.jog_held_tag is None
    rig.engine.jog_press(3, -1, speed=40)
    assert rig.engine.jog_held_tag == "JOG Minus 3"
    rig.engine.jog_release(3, -1)
    assert rig.engine.jog_held_tag is None

    rig.engine.jog_press(5, +1, speed=40)
    assert rig.engine.jog_held_tag == "JOG Plus 5"
    rig.engine.on_disconnected()
    assert rig.engine.jog_held_tag is None


def test_rejected_jog_press_holds_nothing(ready_rig):
    rig = ready_rig
    rig.engine.jog_press(9, +1, speed=40)
    assert rig.engine.jog_held_tag is None
