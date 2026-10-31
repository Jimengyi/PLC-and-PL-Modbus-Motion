"""SimWindow：以 pytest-qt 操作視窗，斷言 PlcModel 狀態與 SimServer 行為（spec FR-53、FR-56）。"""

import subprocess
import sys
import time

import pytest
from pymodbus.client import ModbusTcpClient

from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.sim import SimServer
from plc_motion.sim.sim_window import SimWindow


@pytest.fixture
def server(config_dir):
    points = load_config(config_dir).points
    return SimServer(PlcModel(points), points, port=0)


@pytest.fixture
def window(qtbot, server):
    w = SimWindow(server)
    qtbot.addWidget(w)
    w.show()
    yield w
    w.close()


def select_axis(window, axis: int) -> None:
    window.axis_combo.setCurrentIndex(axis - 1)


def test_limit_checkbox_triggers_negative_limit_of_selected_axis(window, server):
    select_axis(window, 3)
    assert server.model.read("Negative Limit 3") == 1  # B 接點：未觸發為 1
    window.neg_limit_check.setChecked(True)
    assert server.model.read("Negative Limit 3") == 0
    assert server.model.read("Negative Limit 1") == 1  # 其他軸不受影響
    window.neg_limit_check.setChecked(False)
    assert server.model.read("Negative Limit 3") == 1


def test_home_sensor_checkbox_sets_home_sensor(window, server):
    assert server.model.read("Home Sensor 1") == 0
    window.home_check.setChecked(True)
    assert server.model.read("Home Sensor 1") == 1


def test_positive_limit_checkbox(window, server):
    window.pos_limit_check.setChecked(True)
    assert server.model.read("Positive Limit 1") == 0


def test_estop_checkbox_presses_and_releases(window, server):
    assert server.model.read("Emergency Stop") == 1
    window.estop_check.setChecked(True)
    assert server.model.read("Emergency Stop") == 0
    window.estop_check.setChecked(False)
    assert server.model.read("Emergency Stop") == 1


def test_speed_factor_spin_sets_model_speed_factor(window, server):
    window.speed_spin.setValue(2.5)
    assert server.model.speed_factor == 2.5


def test_set_position_button_writes_current_position(window, server):
    select_axis(window, 4)
    window.position_spin.setValue(12.5)
    window.position_button.click()
    assert server.model.read("Current Position 4") == 1250  # 第 4 軸 1 deg = 100


def test_inject_alarm_button_sets_code_and_persistent(window, server):
    select_axis(window, 2)
    window.alarm_code_spin.setValue(7)
    window.alarm_persistent_check.setChecked(True)
    window.alarm_button.click()
    assert server.model.read("Alarm Code 2") == 7
    assert server.model.read("Alarm Code 1") == 0
    # 持續：Reset Alarm 之後 step() 也不會清掉
    server.model.master_write("Reset Alarm 2", True)
    server.model.step(0.05)
    assert server.model.read("Alarm Code 2") == 7


def test_inject_alarm_not_persistent_is_cleared_by_reset(window, server):
    window.alarm_code_spin.setValue(7)
    window.alarm_button.click()
    server.model.master_write("Reset Alarm 1", True)
    server.model.step(0.05)
    assert server.model.read("Alarm Code 1") == 0


def test_persistent_checkbox_toggle_after_injection_releases_alarm(window, server):
    window.alarm_code_spin.setValue(7)
    window.alarm_persistent_check.setChecked(True)
    window.alarm_button.click()
    window.alarm_persistent_check.setChecked(False)
    server.model.master_write("Reset Alarm 1", True)
    server.model.step(0.05)
    assert server.model.read("Alarm Code 1") == 0


def test_no_reply_fault_checkbox_stops_reply(window, server):
    window.no_reply_check.setChecked(True)
    m = server.model
    m.master_write("Servo Motor 1", True)
    m.step(0.05)
    m.master_write("Speed 1", 50)
    m.master_write("PC Action Code 1", 2)
    m.master_write("Target Position 1", 10000)
    m.master_write("Request 1", True)
    m.step(0.05)
    assert m.read("Reply 1") == 0


def test_no_copy_fault_checkbox_keeps_plc_action_code_zero(window, server):
    window.no_copy_check.setChecked(True)
    m = server.model
    m.master_write("Servo Motor 1", True)
    m.step(0.05)
    m.master_write("Speed 1", 50)
    m.master_write("PC Action Code 1", 2)
    m.master_write("Target Position 1", 10000)
    m.master_write("Request 1", True)
    m.step(0.05)
    assert m.read("PLC Action Code 1") == 0


def test_axis_switch_keeps_per_axis_checkbox_state(window, server):
    window.no_reply_check.setChecked(True)
    select_axis(window, 2)
    assert not window.no_reply_check.isChecked()
    select_axis(window, 1)
    assert window.no_reply_check.isChecked()


def test_disconnect_all_button_drops_client(window, server, qtbot):
    server.start()
    try:
        client = ModbusTcpClient("127.0.0.1", port=server.port, timeout=2, retries=0)
        assert client.connect()
        qtbot.waitUntil(lambda: any(e.kind == "connect" for e in server.log.entries()))
        window.disconnect_button.click()
        qtbot.waitUntil(lambda: any(e.kind == "disconnect" for e in server.log.entries()))
        client.close()
    finally:
        server.stop()


def test_log_view_shows_writes_and_state_changes(window, server, qtbot):
    with server.lock:
        server.model.master_write("Speed 1", 30)
    server.log.add("write", "Speed 1", 30)
    window.estop_check.setChecked(True)
    qtbot.waitUntil(lambda: "Speed 1 = 30" in window.log_view.toPlainText())
    qtbot.waitUntil(lambda: "Emergency Stop" in window.log_view.toPlainText())


def test_read_stats_label_shows_count(window, server, qtbot):
    assert "0" in window.read_stats_label.text()
    server.log.count_read()
    server.log.count_read()
    qtbot.waitUntil(lambda: "2" in window.read_stats_label.text())


def test_export_writes_log_file(window, server, tmp_path):
    server.log.add("write", "Speed 1", 30)
    path = tmp_path / "out.log"
    window.export_log(path)
    assert "Speed 1 = 30" in path.read_text(encoding="utf-8")


def test_plc_sim_entry_accepts_pymodbus_client(config_dir, tmp_path):
    """plc-sim 進入點：實際啟動子行程，pymodbus client 可連上。"""
    port = 5071
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "plc_motion.sim.main",
            "--port",
            str(port),
            "--config",
            str(config_dir),
        ],
        env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    try:
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=0)
        deadline = time.monotonic() + 15
        connected = False
        while time.monotonic() < deadline and not connected:
            connected = client.connect()
            if not connected:
                time.sleep(0.3)
        assert connected
        res = client.read_discrete_inputs(24576, count=1, device_id=0)
        assert not res.isError()
        client.close()
    finally:
        proc.terminate()
        proc.wait(10)
