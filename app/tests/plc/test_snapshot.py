"""Snapshot 與 ModelPort：以 ModelPort.read_all() 取得的快照為觀測點。"""

import pytest

from plc_motion.plc import ModelPort, PlcCommError, PlcModel, PlcPort, Snapshot
from plc_motion.settings import load_config


@pytest.fixture
def model(config_dir) -> PlcModel:
    return PlcModel(load_config(config_dir).points)


@pytest.fixture
def port(model) -> ModelPort:
    return ModelPort(model)


def test_model_port_satisfies_plc_port(port):
    assert isinstance(port, PlcPort)


def test_position_raw_and_scaled_value(model, port):
    model.set_position(1, -2.3456)  # X：1 mm = 10000
    model.set_position(4, 150.25)  # RX：1 deg = 100
    model.set_position(7, -3.5)  # 升降：1 mm = 100

    snap = port.read_all()

    assert snap.raw("Current Position 1") == -23456
    assert snap.value("Current Position 1") == pytest.approx(-2.3456)
    assert snap.raw("Current Position 4") == 15025
    assert snap.value("Current Position 4") == pytest.approx(150.25)
    assert snap.value("Current Position 7") == pytest.approx(-3.5)
    assert snap.axis(1).position == pytest.approx(-2.3456)


def test_non_position_value_equals_raw(port):
    port.write("Speed 3", 75)
    snap = port.read_all()
    assert snap.value("Speed 3") == 75
    assert snap.value("Servo Motor 3") is False


def test_limits_untriggered_read_raw_one_and_logical_false(port):
    snap = port.read_all()
    assert snap.raw("Positive Limit 1") is True
    assert snap.raw("Negative Limit 1") is True
    assert snap.value("Positive Limit 1") is False
    assert snap.axis(1).pos_limit_triggered is False
    assert snap.axis(1).neg_limit_triggered is False


def test_b_contact_limits_raw_zero_means_triggered(model, port):
    model.set_limit(2, "pos", True)
    model.set_limit(3, "neg", True)
    snap = port.read_all()

    assert snap.raw("Positive Limit 2") is False
    assert snap.value("Positive Limit 2") is True
    assert snap.axis(2).pos_limit_triggered is True
    assert snap.axis(2).neg_limit_triggered is False
    assert snap.raw("Negative Limit 3") is False
    assert snap.axis(3).neg_limit_triggered is True


def test_a_contact_home_sensor_raw_one_means_triggered(model, port):
    model.set_limit(1, "home", True)
    snap = port.read_all()
    assert snap.raw("Home Sensor 1") is True
    assert snap.value("Home Sensor 1") is True
    assert snap.axis(1).home_sensor is True
    assert port.read_all().axis(2).home_sensor is False


def test_estop_ok_is_inverse_of_logical_value(model, port):
    snap = port.read_all()
    assert snap.raw("Emergency Stop") is True
    assert snap.value("Emergency Stop") is False
    assert snap.estop_ok is True

    model.set_estop(True)
    snap = port.read_all()
    assert snap.raw("Emergency Stop") is False
    assert snap.value("Emergency Stop") is True
    assert snap.estop_ok is False


def logical_values(points) -> dict:
    """全部點的邏輯值：Bool 未觸發（False）、其他為 0。"""
    return {p.tag: False if p.data_type == "Bool" else 0 for p in points.all()}


def test_snapshot_values_are_logical_and_engineering(model):
    values = logical_values(model.points)
    values["Positive Limit 1"] = True  # B 接點觸發
    values["Home Sensor 2"] = True  # A 接點觸發
    values["Emergency Stop"] = True
    values["Current Position 1"] = -2.3456
    values["Current Position 4"] = 150.25
    snap = Snapshot(model.points, values)

    assert snap.axis(1).pos_limit_triggered is True
    assert snap.raw("Positive Limit 1") is False
    assert snap.axis(2).home_sensor is True
    assert snap.raw("Home Sensor 2") is True
    assert snap.estop_ok is False
    assert snap.raw("Emergency Stop") is False
    assert snap.raw("Negative Limit 1") is True  # B 接點未觸發
    assert snap.value("Current Position 1") == -2.3456
    assert snap.raw("Current Position 1") == -23456
    assert snap.raw("Current Position 4") == 15025
    assert snap.axis(1).position == -2.3456


def test_plc_port_requires_only_write():
    class WriteOnly:
        def write(self, tag, value):
            pass

    assert isinstance(WriteOnly(), PlcPort)


@pytest.fixture
def inverted_coil_config(config_dir, io_rows, write_io_points):
    io_rows.append(
        {
            **io_rows[0],
            "Tag Name": "Inv Coil",
            "Display Name": "反相線圈",
            "Start Address": "500",
            "Data Model": "Coil",
            "Data Type": "Bool",
            "Access": "Read Write",
            "Attributes": "invert=true",
        }
    )
    write_io_points(config_dir, io_rows)
    return load_config(config_dir)


def test_model_port_writes_bool_as_logical_value(inverted_coil_config):
    model = PlcModel(inverted_coil_config.points)
    port = ModelPort(model)

    port.write("Inv Coil", True)
    assert model.read("Inv Coil") is False
    assert port.read_all().value("Inv Coil") is True
    assert port.read_all().raw("Inv Coil") is False

    port.write("Inv Coil", False)
    assert model.read("Inv Coil") is True
    port.write("Servo Motor 1", True)
    assert model.read("Servo Motor 1") is True


def test_axis_state_maps_each_tag_of_that_axis(port):
    port.write("Servo Motor 5", True)
    port.write("PC Action Code 5", 1)
    port.write("JOG Minus 5", True)
    port.write("Request 6", True)
    port.write("JOG Plus 6", True)

    snap = port.read_all()
    a5, a6 = snap.axis(5), snap.axis(6)

    assert (a5.servo, a5.pc_action_code, a5.jog_minus, a5.jog_plus) == (True, 1, True, False)
    assert (a6.servo, a6.request, a6.jog_plus) == (False, True, True)
    assert a5.request is False
    for field in ("ready", "busy", "home_finish", "reply"):
        assert getattr(a5, field) is False
    assert (a5.alarm_code, a5.plc_action_code) == (0, 0)


def test_axis_state_reflects_model_outputs(model, port):
    port.write("Servo Motor 1", True)
    port.write("PC Action Code 1", 1)
    model.step(0.01)
    model.inject_alarm(2, 7, persistent=False)

    snap = port.read_all()
    assert snap.axis(1).ready is True
    assert snap.axis(1).plc_action_code == 1
    assert snap.axis(2).alarm_code == 7


def test_snapshot_is_frozen_copy(model, port):
    snap = port.read_all()
    model.set_position(1, 1.0)
    assert snap.raw("Current Position 1") == 0


def test_write_position_takes_raw_i32(model, port):
    port.write("Target Position 1", -1000000)
    assert model.read("Target Position 1") == -1000000
    assert port.read_all().value("Target Position 1") == pytest.approx(-100.0)


def test_write_unknown_tag_raises_comm_error_with_tag_name(port):
    with pytest.raises(PlcCommError) as exc:
        port.write("No Such Tag", 1)
    assert "No Such Tag" in str(exc.value)


def test_comm_error_message_contains_tag_and_address():
    err = PlcCommError("Modbus 例外", tag="Ready 1", address=20)
    assert "Ready 1" in str(err)
    assert "20" in str(err)
    assert (err.tag, err.address) == ("Ready 1", 20)
