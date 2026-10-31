"""MotionEngine 連線事件：殘留交握（FR-57）、JOG 殘留（FR-58）、斷線時軸可能仍在移動（FR-13）。"""

import pytest

from plc_motion.engine import (
    DisconnectedWhileMoving,
    JogResidualCleared,
    ResidualDetected,
    ResidualInfo,
)


def events_of(rig, cls) -> list:
    return [e for e in rig.events if isinstance(e, cls)]


# ---- FR-57 殘留交握 ----


def test_residual_detected_on_first_tick_keeps_lock_and_writes_nothing(rig):
    rig.prepare()
    rig.model.master_write("PC Action Code 3", 2)
    rig.model.master_write("Target Position 5", 1234)  # 第 5 軸 RY 倍率 100 → 12.34 deg
    rig.model.master_write("Request 5", True)
    rig.connect()

    detected = events_of(rig, ResidualDetected)
    assert len(detected) == 1
    infos = detected[0].axes
    assert [i.axis for i in infos] == [3, 5]
    assert infos[0] == ResidualInfo(axis=3, request=False, pc_action_code=2, target_position=0.0)
    assert infos[1].request is True
    assert infos[1].pc_action_code == 0
    assert infos[1].target_position == pytest.approx(12.34)
    assert rig.engine.residual_axes() == list(infos)

    assert rig.engine.locked
    assert any("殘留交握" in r for r in rig.engine.lock_reasons)
    rig.run(1.0)
    assert rig.engine.locked
    reasons = rig.engine.move({1: 1.0}, relative=False, speed=50)
    assert any("殘留交握" in r for r in reasons)
    assert rig.writes() == []
    assert len(events_of(rig, ResidualDetected)) == 1  # 只在連線後第一次輪詢判斷


def test_clear_residual_writes_0_and_releases_lock(rig):
    rig.prepare()
    rig.model.master_write("PC Action Code 3", 2)
    rig.model.master_write("Request 5", True)
    rig.connect()

    rig.engine.clear_residual()
    assert rig.writes() == [
        ("Request 3", False),
        ("PC Action Code 3", 0),
        ("Request 5", False),
        ("PC Action Code 5", 0),
    ]
    assert rig.engine.residual_axes() == []
    rig.tick()
    assert not rig.engine.locked


def test_normal_handshake_while_connected_is_not_residual(ready_rig):
    rig = ready_rig
    rig.engine.move({1: 5.0}, relative=False, speed=100)
    rig.run_until_finished()
    assert events_of(rig, ResidualDetected) == []
    assert rig.engine.residual_axes() == []


def test_reconnect_after_disconnect_mid_handshake_detects_residual(ready_rig):
    rig = ready_rig
    rig.model.set_fault(1, no_reply=True, no_copy=False)  # PLC 不回 Reply → Request 停在 1
    rig.engine.move({1: 5.0}, relative=False, speed=100)
    rig.run_until(lambda: ("Request 1", True) in rig.writes())
    rig.tick()
    rig.engine.on_disconnected()

    rig.connect()
    detected = events_of(rig, ResidualDetected)
    assert len(detected) == 1
    assert detected[0].axes[0].axis == 1
    assert detected[0].axes[0].request is True
    assert detected[0].axes[0].pc_action_code == 2
    assert detected[0].axes[0].target_position == pytest.approx(5.0)
    assert rig.engine.locked


def test_residual_rechecked_on_every_connection(rig):
    rig.prepare()
    rig.model.master_write("PC Action Code 2", 1)
    rig.connect()
    rig.engine.on_disconnected()
    rig.connect()
    assert len(events_of(rig, ResidualDetected)) == 2


# ---- FR-58 JOG 殘留 ----


def test_jog_residual_written_0_immediately_on_first_tick(rig):
    rig.prepare()
    rig.model.master_write("JOG Minus 4", True)
    rig.connect()

    assert rig.writes() == [("JOG Minus 4", False)]
    cleared = events_of(rig, JogResidualCleared)
    assert [e.axis for e in cleared] == [4]
    assert events_of(rig, ResidualDetected) == []

    rig.tick()
    rig.tick()
    assert not rig.axis(4).jog_minus
    assert not rig.engine.locked


def test_jog_residual_not_checked_after_first_tick(ready_rig):
    rig = ready_rig
    rig.model.master_write("JOG Plus 2", True)
    rig.run(0.5)
    assert events_of(rig, JogResidualCleared) == []
    assert rig.writes() == []


# ---- FR-13 斷線時軸可能仍在移動 ----


def test_disconnect_while_jog_held_emits_disconnected_while_moving(ready_rig):
    rig = ready_rig
    rig.engine.jog_press(1, +1, speed=40)
    rig.engine.on_disconnected()
    assert len(events_of(rig, DisconnectedWhileMoving)) == 1


def test_disconnect_while_axis_busy_emits_disconnected_while_moving(ready_rig):
    rig = ready_rig
    rig.engine.move({2: 50.0}, relative=False, speed=10)
    rig.run_until(lambda: rig.axis(2).busy)
    rig.tick()
    rig.engine.on_disconnected()
    events = events_of(rig, DisconnectedWhileMoving)
    assert len(events) == 1
    assert 2 in events[0].axes


def test_disconnect_while_idle_emits_nothing(ready_rig):
    rig = ready_rig
    rig.engine.on_disconnected()
    assert events_of(rig, DisconnectedWhileMoving) == []


def test_jog_hold_cleared_by_disconnect(ready_rig):
    rig = ready_rig
    rig.engine.jog_press(1, +1, speed=40)
    rig.engine.on_disconnected()
    rig.connect()  # PLC 端 JOG Plus 1 仍為 1 → FR-58 寫 0
    assert rig.writes()[-1] == ("JOG Plus 1", False)
    rig.tick()
    rig.tick()
    assert not any("JOG 按住中" in r for r in rig.engine.lock_reasons)
    assert not rig.engine.locked
