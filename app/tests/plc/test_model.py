"""PlcModel 行為：依 docs/PLC_PC_Modbus_通訊規格.md 與 spec FR-52、FR-53。

只透過 master_write／step／read 與 FR-53 的操作觀測。
"""

import pytest

from plc_motion.plc import PlcModel
from plc_motion.settings import load_config

DT = 0.01


@pytest.fixture
def model(config_dir) -> PlcModel:
    return PlcModel(load_config(config_dir).points)


def run(m: PlcModel, seconds: float) -> None:
    for _ in range(round(seconds / DT)):
        m.step(DT)


def run_until(m: PlcModel, cond, limit: float = 30.0) -> float:
    """逐步執行直到 cond() 成立，回傳經過秒數；超過 limit 視為失敗。"""
    t = 0.0
    while not cond():
        assert t < limit, "條件在時限內未成立"
        m.step(DT)
        t += DT
    return t


def r(m: PlcModel, name: str, axis: int = 1):
    return m.read(f"{name} {axis}")


def w(m: PlcModel, name: str, value, axis: int = 1) -> None:
    m.master_write(f"{name} {axis}", value)


def servo_on(m: PlcModel, axis: int = 1) -> None:
    w(m, "Servo Motor", True, axis)
    m.step(DT)


def handshake(m: PlcModel, code: int, axis: int = 1, target: int | None = None) -> None:
    """依規格書 2.1、2.2 完成交握（含 PC_Action_Code 清零），回傳時動作尚未開始。"""
    if target is not None:
        w(m, "Target Position", target, axis)
        w(m, "Speed", 100, axis)
    w(m, "PC Action Code", code, axis)
    m.step(DT)
    assert r(m, "PLC Action Code", axis) == code
    w(m, "Request", True, axis)
    m.step(DT)
    assert r(m, "Reply", axis) is True
    w(m, "Request", False, axis)
    w(m, "PC Action Code", 0, axis)
    m.step(DT)
    assert r(m, "Reply", axis) is False


def home(m: PlcModel, axis: int = 1) -> None:
    handshake(m, 1, axis)
    run_until(m, lambda: r(m, "Find Home Finish", axis))


def move(m: PlcModel, target: int, axis: int = 1) -> None:
    handshake(m, 2, axis, target)
    run_until(m, lambda: not r(m, "Busy", axis))


@pytest.fixture
def ready(model) -> PlcModel:
    """第 1 軸 Servo ON 且已原點復歸。"""
    servo_on(model)
    home(model)
    return model


# ---- Servo、Ready ----


def test_initial_state_is_not_ready(model):
    model.step(DT)
    assert r(model, "Ready") is False
    assert r(model, "Busy") is False
    assert r(model, "Find Home Finish") is False
    assert r(model, "Alarm Code") == 0


def test_servo_on_makes_ready(model):
    servo_on(model)
    assert r(model, "Ready") is True


def test_idle_servo_off_clears_ready_and_home_finish_without_alarm(ready):
    w(ready, "Servo Motor", False)
    ready.step(DT)

    assert r(ready, "Ready") is False
    assert r(ready, "Find Home Finish") is False
    assert r(ready, "Alarm Code") == 0


# ---- 命令碼複製 ----


def test_idle_copies_pc_action_code_to_plc_action_code(model):
    servo_on(model)
    w(model, "PC Action Code", 2)
    model.step(DT)
    assert r(model, "PLC Action Code") == 2

    w(model, "PC Action Code", 0)
    model.step(DT)
    assert r(model, "PLC Action Code") == 0


def test_no_copy_when_not_ready(model):
    w(model, "PC Action Code", 2)
    model.step(DT)
    assert r(model, "PLC Action Code") == 0


def test_busy_keeps_running_code_and_copies_zero_after_finish(ready):
    w(ready, "Target Position", 500000)
    w(ready, "Speed", 100)
    w(ready, "PC Action Code", 2)
    ready.step(DT)
    w(ready, "Request", True)
    ready.step(DT)
    w(ready, "Request", False)
    w(ready, "PC Action Code", 0)
    ready.step(DT)

    assert r(ready, "Busy") is True
    assert r(ready, "PLC Action Code") == 2  # 動作中不複製

    run_until(ready, lambda: not r(ready, "Busy"))
    ready.step(DT)
    assert r(ready, "PLC Action Code") == 0


# ---- Request / Reply ----


def test_request_rising_edge_sets_busy_and_reply_in_same_scan(ready):
    w(ready, "Target Position", 500000)
    w(ready, "Speed", 100)
    w(ready, "PC Action Code", 2)
    ready.step(DT)
    w(ready, "Request", True)
    ready.step(DT)

    assert r(ready, "Busy") is True
    assert r(ready, "Ready") is False
    assert r(ready, "Reply") is True


def test_axis_does_not_move_until_request_cleared_and_reply_dropped(ready):
    w(ready, "Target Position", 500000)
    w(ready, "Speed", 100)
    w(ready, "PC Action Code", 2)
    ready.step(DT)
    w(ready, "Request", True)
    run(ready, 1.0)
    assert r(ready, "Current Position") == 0
    assert r(ready, "Reply") is True

    w(ready, "Request", False)
    ready.step(DT)
    assert r(ready, "Reply") is False

    run(ready, 0.2)
    assert r(ready, "Current Position") > 0


def test_request_ignored_when_not_ready(model):
    w(model, "PC Action Code", 1)
    w(model, "Request", True)
    run(model, 0.1)
    assert r(model, "Busy") is False
    assert r(model, "Reply") is False


# ---- Code 2 絕對移動 ----


def test_code2_moves_gradually_to_target_then_ready(ready):
    handshake(ready, 2, target=-1000000)  # −100 mm
    ready.step(DT)
    pos = r(ready, "Current Position")
    assert -1000000 < pos < 0  # 逐步移動，不是瞬間到位

    run_until(ready, lambda: not r(ready, "Busy"))
    assert r(ready, "Current Position") == -1000000
    assert r(ready, "Ready") is True
    assert r(ready, "Alarm Code") == 0


def _distance_in(m: PlcModel, speed: int, seconds: float) -> int:
    w(m, "Target Position", 100_000_000)
    w(m, "Speed", speed)
    w(m, "PC Action Code", 2)
    m.step(DT)
    w(m, "Request", True)
    m.step(DT)
    w(m, "Request", False)
    w(m, "PC Action Code", 0)
    m.step(DT)
    start = r(m, "Current Position")
    run(m, seconds)
    return r(m, "Current Position") - start


def test_code2_speed_percent_scales_motion(config_dir):
    distances = {}
    for speed in (100, 10):
        m = PlcModel(load_config(config_dir).points)
        servo_on(m)
        home(m)
        distances[speed] = _distance_in(m, speed, 0.5)

    assert distances[10] > 0
    assert distances[10] * 10 == pytest.approx(distances[100], rel=0.05)


def test_speed_factor_scales_motion(config_dir):
    distances = {}
    for factor in (1.0, 2.0):
        m = PlcModel(load_config(config_dir).points)
        m.speed_factor = factor
        servo_on(m)
        home(m)
        distances[factor] = _distance_in(m, 50, 0.5)

    assert distances[1.0] > 0
    assert distances[2.0] == pytest.approx(distances[1.0] * 2, rel=0.05)


# ---- Code 1 原點復歸 ----


def test_code1_clears_home_finish_at_start_and_returns_to_zero(ready):
    ready.set_position(1, 12.5)
    assert r(ready, "Find Home Finish") is True

    w(ready, "PC Action Code", 1)
    ready.step(DT)
    w(ready, "Request", True)
    ready.step(DT)
    assert r(ready, "Find Home Finish") is False

    w(ready, "Request", False)
    w(ready, "PC Action Code", 0)
    run_until(ready, lambda: not r(ready, "Busy"))
    assert r(ready, "Current Position") == 0


def test_code1_busy_drops_before_home_finish_with_gap_over_poll_period(model):
    servo_on(model)
    model.set_position(1, 3.0)
    handshake(model, 1)

    t_busy = run_until(model, lambda: not r(model, "Busy"))
    assert r(model, "Ready") is True
    assert r(model, "Find Home Finish") is False
    gap = run_until(model, lambda: r(model, "Find Home Finish"))

    assert t_busy > 0
    assert gap > 0.2


# ---- JOG ----


def test_jog_plus_moves_while_held_and_stops_on_release(model):
    servo_on(model)
    w(model, "Speed", 50)
    w(model, "JOG Plus", True)
    model.step(DT)
    assert r(model, "Busy") is True
    assert r(model, "Ready") is False

    run(model, 0.5)
    p1 = r(model, "Current Position")
    run(model, 0.5)
    p2 = r(model, "Current Position")
    assert 0 < p1 < p2

    w(model, "JOG Plus", False)
    model.step(DT)
    assert r(model, "Busy") is False
    assert r(model, "Ready") is True
    stopped = r(model, "Current Position")
    run(model, 0.5)
    assert r(model, "Current Position") == stopped


def test_jog_minus_moves_negative(model):
    servo_on(model)
    w(model, "Speed", 50)
    w(model, "JOG Minus", True)
    run(model, 0.5)
    assert r(model, "Current Position") < 0


def test_jog_does_not_need_home_finish(model):
    servo_on(model)
    assert r(model, "Find Home Finish") is False
    w(model, "Speed", 50)
    w(model, "JOG Plus", True)
    run(model, 0.2)
    assert r(model, "Current Position") > 0


def test_jog_checks_condition_only_on_rising_edge(model):
    w(model, "Speed", 50)
    w(model, "JOG Plus", True)  # Servo OFF 時按下：不動
    model.step(DT)
    w(model, "Servo Motor", True)
    run(model, 0.5)  # 按住中 Servo ON，非上升緣仍不動
    assert r(model, "Current Position") == 0
    assert r(model, "Busy") is False


def test_jog_toward_triggered_limit_is_ignored_without_alarm(model):
    servo_on(model)
    model.set_limit(1, "pos", True)
    w(model, "Speed", 50)
    w(model, "JOG Plus", True)
    run(model, 0.5)

    assert r(model, "Current Position") == 0
    assert r(model, "Busy") is False
    assert r(model, "Alarm Code") == 0
    assert r(model, "Ready") is True


def test_jog_away_from_triggered_limit_is_allowed(model):
    servo_on(model)
    model.set_limit(1, "neg", True)
    w(model, "Speed", 50)
    w(model, "JOG Plus", True)
    run(model, 0.5)

    assert r(model, "Current Position") > 0
    assert r(model, "Alarm Code") == 0


def test_jog_into_limit_raises_alarm(model):
    servo_on(model)
    w(model, "Speed", 50)
    w(model, "JOG Plus", True)
    run(model, 0.2)
    model.set_limit(1, "pos", True)
    model.step(DT)
    stopped = r(model, "Current Position")
    run(model, 0.2)

    assert r(model, "Alarm Code") == 2
    assert r(model, "Busy") is False
    assert r(model, "Ready") is False
    assert r(model, "Current Position") == stopped


# ---- 極限 ----


@pytest.mark.parametrize(
    ("target", "which", "alarm"), [(10_000_000, "pos", 2), (-10_000_000, "neg", 3)]
)
def test_moving_into_limit_stops_with_alarm(ready, target, which, alarm):
    handshake(ready, 2, target=target)
    run(ready, 0.2)
    ready.set_limit(1, which, True)
    ready.step(DT)
    stopped = r(ready, "Current Position")
    run(ready, 0.2)

    assert r(ready, "Alarm Code") == alarm
    assert r(ready, "Busy") is False
    assert r(ready, "Ready") is False
    assert r(ready, "Current Position") == stopped


def test_homing_ignores_limits(model):
    servo_on(model)
    model.set_position(1, 5.0)
    handshake(model, 1)
    run(model, 0.05)
    model.set_limit(1, "neg", True)
    model.set_limit(1, "pos", True)
    run_until(model, lambda: r(model, "Find Home Finish"))

    assert r(model, "Alarm Code") == 0
    assert r(model, "Current Position") == 0


def test_idle_command_toward_triggered_limit_does_not_move_and_alarms(ready):
    ready.set_limit(1, "pos", True)
    handshake(ready, 2, target=500000)
    run(ready, 0.3)

    assert r(ready, "Current Position") == 0
    assert r(ready, "Alarm Code") == 2
    assert r(ready, "Busy") is False
    assert r(ready, "Ready") is False


def test_idle_command_away_from_triggered_limit_moves(ready):
    ready.set_limit(1, "pos", True)
    move(ready, -500000)

    assert r(ready, "Current Position") == -500000
    assert r(ready, "Alarm Code") == 0


def test_idle_with_triggered_limit_stays_ready(ready):
    ready.set_limit(1, "neg", True)
    run(ready, 0.1)
    assert r(ready, "Ready") is True
    assert r(ready, "Alarm Code") == 0


def test_limit_alarm_can_be_reset_while_still_on_limit(ready):
    handshake(ready, 2, target=10_000_000)
    run(ready, 0.1)
    ready.set_limit(1, "pos", True)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 2

    w(ready, "Reset Alarm", True)
    ready.step(DT)
    w(ready, "Reset Alarm", False)
    ready.step(DT)

    assert r(ready, "Alarm Code") == 0
    assert r(ready, "Ready") is True


# ---- Servo OFF 動作中 ----


def test_servo_off_during_motion_alarms_4(ready):
    handshake(ready, 2, target=10_000_000)
    run(ready, 0.1)
    w(ready, "Servo Motor", False)
    ready.step(DT)
    stopped = r(ready, "Current Position")
    run(ready, 0.2)

    assert r(ready, "Alarm Code") == 4
    assert r(ready, "Busy") is False
    assert r(ready, "Ready") is False
    assert r(ready, "Find Home Finish") is False
    assert r(ready, "Current Position") == stopped


def test_servo_off_during_jog_alarms_4(model):
    servo_on(model)
    w(model, "Speed", 50)
    w(model, "JOG Minus", True)
    run(model, 0.1)
    w(model, "Servo Motor", False)
    model.step(DT)

    assert r(model, "Alarm Code") == 4
    assert r(model, "Busy") is False


# ---- 急停 ----


def test_estop_stops_all_axes_and_clears_home_finish(ready):
    servo_on(ready, 2)
    handshake(ready, 2, target=10_000_000)
    run(ready, 0.1)

    ready.set_estop(True)
    ready.step(DT)
    stopped = r(ready, "Current Position")
    run(ready, 0.2)

    assert ready.read("Emergency Stop") is False  # B 接點：急停時為 0
    for axis in (1, 2):
        assert r(ready, "Alarm Code", axis) == 1
        assert r(ready, "Ready", axis) is False
        assert r(ready, "Busy", axis) is False
        assert r(ready, "Find Home Finish", axis) is False
    assert r(ready, "Current Position") == stopped


def test_estop_alarm_not_cleared_while_pressed_then_cleared_after_release(ready):
    ready.set_estop(True)
    ready.step(DT)

    w(ready, "Reset Alarm", True)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 1  # 讀到 Reset Alarm 的那次掃描也不可短暫清為 0
    w(ready, "Reset Alarm", False)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 1
    assert r(ready, "Ready") is False

    ready.set_estop(False)
    ready.step(DT)
    assert ready.read("Emergency Stop") is True
    assert r(ready, "Alarm Code") == 1  # 解除急停不會自動清 Alarm

    w(ready, "Reset Alarm", True)
    ready.step(DT)
    w(ready, "Reset Alarm", False)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 0
    assert r(ready, "Ready") is True
    assert r(ready, "Find Home Finish") is False


# ---- 注入 Alarm（FR-53） ----


def test_injected_alarm_stops_motion_immediately(ready):
    handshake(ready, 2, target=10_000_000)
    run(ready, 0.1)

    ready.inject_alarm(1, 42, persistent=False)
    assert r(ready, "Alarm Code") == 42
    assert r(ready, "Busy") is False
    assert r(ready, "Ready") is False

    stopped = r(ready, "Current Position")
    run(ready, 0.2)
    assert r(ready, "Current Position") == stopped


def test_injected_alarm_cleared_by_reset_when_not_persistent(ready):
    ready.inject_alarm(1, 42, persistent=False)
    w(ready, "Reset Alarm", True)
    ready.step(DT)
    w(ready, "Reset Alarm", False)
    ready.step(DT)

    assert r(ready, "Alarm Code") == 0
    assert r(ready, "Ready") is True


def test_persistent_alarm_survives_reset_until_unchecked(ready):
    ready.inject_alarm(1, 42, persistent=True)
    w(ready, "Reset Alarm", True)
    ready.step(DT)
    w(ready, "Reset Alarm", False)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 42
    assert r(ready, "Ready") is False

    ready.set_persistent(1, False)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 42  # 取消勾選本身不清除

    w(ready, "Reset Alarm", True)
    ready.step(DT)
    w(ready, "Reset Alarm", False)
    ready.step(DT)
    assert r(ready, "Alarm Code") == 0
    assert r(ready, "Ready") is True


# ---- 故障注入（FR-53，逐軸） ----


def test_fault_no_reply_only_affects_that_axis(model):
    servo_on(model, 1)
    servo_on(model, 2)
    model.set_fault(1, no_reply=True, no_copy=False)
    for axis in (1, 2):
        w(model, "PC Action Code", 1, axis)
    model.step(DT)
    for axis in (1, 2):
        w(model, "Request", True, axis)
    run(model, 0.5)

    assert r(model, "Reply", 1) is False
    assert r(model, "Reply", 2) is True


def test_fault_no_copy_only_affects_that_axis(model):
    servo_on(model, 1)
    servo_on(model, 2)
    model.set_fault(1, no_reply=False, no_copy=True)
    for axis in (1, 2):
        w(model, "PC Action Code", 2, axis)
    run(model, 0.5)

    assert r(model, "PLC Action Code", 1) == 0
    assert r(model, "PLC Action Code", 2) == 2


def test_fault_can_be_cleared(model):
    servo_on(model)
    model.set_fault(1, no_reply=True, no_copy=True)
    model.set_fault(1, no_reply=False, no_copy=False)
    handshake(model, 1)  # 交握內的斷言全部成立


# ---- 其他 FR-53 操作與事件 ----


def test_set_position_uses_engineering_value(model):
    model.set_position(1, -2.3456)  # X 軸 1 mm = 10000
    model.set_position(4, 12.5)  # RX 軸 1 deg = 100
    assert r(model, "Current Position", 1) == -23456
    assert r(model, "Current Position", 4) == 1250


def test_set_limit_writes_b_and_a_contact_raw_values(model):
    model.set_limit(1, "pos", True)
    model.set_limit(1, "home", True)
    assert r(model, "Positive Limit") is False  # B 接點：觸發為 0
    assert r(model, "Negative Limit") is True  # 未觸發為 1
    assert r(model, "Home Sensor") is True  # A 接點：觸發為 1

    model.set_limit(1, "pos", False)
    assert r(model, "Positive Limit") is True


def test_listener_receives_state_changes(model):
    events = []
    model.add_listener(lambda tag, value: events.append((tag, value)))
    servo_on(model)

    assert ("Servo Motor 1", True) in events
    assert ("Ready 1", True) in events
    assert ("Ready 2", True) not in events  # 只送有變化的點


# ---- 多型別的非必要點（spec FR-33） ----


def _extra(tag, address, length, model, data_type, access, attributes=""):
    return {
        "Tag Name": tag,
        "Display Name": "",
        "Start Address": address,
        "Address Length": str(length),
        "Unit ID": "0",
        "Data Model": model,
        "Data Type": data_type,
        "Access": access,
        "Visible": "O",
        "Description": "",
        "Attributes": attributes,
        "Check Status": "Unchecked",
    }


@pytest.fixture
def multi_model(config_dir, io_rows, write_io_points) -> PlcModel:
    hr = "Holding Register"
    io_rows += [
        _extra("Aux F32", "300", 2, hr, "F32", "Read Write"),
        _extra("Aux String", "302", 2, hr, "String", "Read Write"),
        _extra("Aux U32", "304", 2, hr, "U32", "Read Write"),
        _extra("Lamp", "310.b0", 1, hr, "Bool", "Read Write"),
        _extra("Door Closed", "310.b1", 1, hr, "Bool", "Read Only", "invert=true"),
    ]
    write_io_points(config_dir, io_rows)
    return PlcModel(load_config(config_dir).points)


def test_initial_raw_values_by_data_type(multi_model):
    assert multi_model.read("Aux F32") == 0.0
    assert multi_model.read("Aux String") == ""
    assert multi_model.read("Aux U32") == 0
    assert multi_model.read("Lamp") is False
    assert multi_model.read("Door Closed") is True  # B 接點未觸發為 1


def test_master_write_keeps_float_and_text_values(multi_model):
    multi_model.master_write("Aux F32", 1.25)
    multi_model.master_write("Aux String", "AB")
    multi_model.master_write("Aux U32", 4_000_000_000)
    assert multi_model.read("Aux F32") == 1.25
    assert multi_model.read("Aux String") == "AB"
    assert multi_model.read("Aux U32") == 4_000_000_000
