"""SimServer：以 ModbusTcpClient 直接連線讀寫，斷言回應值與 SimLog（spec FR-51、FR-53、FR-56）。"""

import re
import time
from pathlib import Path

import pytest
from pymodbus.client import ModbusTcpClient

from plc_motion.codec import join_i32
from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.settings.io_points import DataModel, SensorType
from plc_motion.sim import SimServer

ILLEGAL_ADDRESS = 2


def wait_until(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "條件在時限內未成立"
        time.sleep(0.01)


def make_server(config_dir: Path) -> SimServer:
    points = load_config(config_dir).points
    return SimServer(
        PlcModel(points), points, port=0
    )  # 0：由系統指定，start() 後 server.port 為實際埠號


@pytest.fixture
def server(config_dir):
    srv = make_server(config_dir)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    c = ModbusTcpClient("127.0.0.1", port=server.port, timeout=2, retries=0)
    assert c.connect()
    yield c
    c.close()


def addr(server: SimServer, tag: str) -> int:
    return server.points.get(tag).start_address


def read_point(c: ModbusTcpClient, point, device_id: int = 0):
    if point.data_model == DataModel.COIL:
        rr = c.read_coils(point.start_address, count=point.length, device_id=device_id)
        return None if rr.isError() else rr.bits[0]
    if point.data_model == DataModel.DISCRETE_INPUT:
        rr = c.read_discrete_inputs(point.start_address, count=point.length, device_id=device_id)
        return None if rr.isError() else rr.bits[0]
    rr = c.read_holding_registers(point.start_address, count=point.length, device_id=device_id)
    if rr.isError():
        return None
    if point.sensor_type == SensorType.POSITION:
        return join_i32(rr.registers[0], rr.registers[1])
    return rr.registers[0]


def entries(server: SimServer, kind: str) -> list:
    return [e for e in server.log.entries() if e.kind == kind]


# ---- 讀取 ----


def test_every_defined_point_reads_model_value(server, client):
    with server.lock:
        server.model.set_position(1, -100.0)
        server.model.set_position(7, 12.34)
        server.model.set_limit(2, "pos", True)
        server.model.set_limit(3, "home", True)
        server.model.set_estop(True)
    for point in server.points.all():
        with server.lock:
            expected = server.model.read(point.tag)
        assert read_point(client, point) == expected, point.tag
    assert read_point(client, server.points.get("Current Position 1")) == -1000000
    assert read_point(client, server.points.get("Current Position 7")) == 1234


def test_multi_point_read_returns_each_value_in_order(server, client):
    # 第 1 軸 PLC→PC Coil 20 ~ 24 中 23 未定義；讀 20 ~ 22 三點
    with server.lock:
        server.model.master_write("Servo Motor 1", True)
    wait_until(lambda: read_point(client, server.points.get("Ready 1")) is True)
    rr = client.read_coils(20, count=3, device_id=0)
    assert not rr.isError()
    assert rr.bits[:3] == [True, False, False]


# ---- 寫入 ----


def test_write_coil_reaches_model_and_is_logged_with_ms_time(server, client):
    before = time.time()
    assert not client.write_coil(addr(server, "Servo Motor 1"), True, device_id=0).isError()
    with server.lock:
        assert server.model.read("Servo Motor 1") is True
    writes = entries(server, "write")
    assert [(e.tag, e.value) for e in writes] == [("Servo Motor 1", True)]
    assert before - 0.01 <= writes[0].time <= time.time() + 0.01
    assert re.search(r"\d{2}:\d{2}:\d{2}\.\d{3}", writes[0].format())
    assert "Servo Motor 1" in writes[0].format()


def test_write_register_reaches_model_and_is_logged(server, client):
    assert not client.write_register(addr(server, "Speed 3"), 80, device_id=0).isError()
    with server.lock:
        assert server.model.read("Speed 3") == 80
    assert [(e.tag, e.value) for e in entries(server, "write")] == [("Speed 3", 80)]


def test_write_multiple_coils_logs_each_point(server, client):
    # 第 1 軸 PC→PLC Coil：120 Request、121 Reset Alarm、122 Servo Motor
    assert not client.write_coils(120, [False, True, True], device_id=0).isError()
    with server.lock:
        assert server.model.read("Reset Alarm 1") is True
        assert server.model.read("Servo Motor 1") is True
    assert [(e.tag, e.value) for e in entries(server, "write")] == [
        ("Request 1", False),
        ("Reset Alarm 1", True),
        ("Servo Motor 1", True),
    ]


def test_position_two_words_low_first_join_to_signed_i32(server, client):
    a = addr(server, "Target Position 1")
    assert not client.write_registers(a, [48576, 65520], device_id=0).isError()
    with server.lock:
        assert server.model.read("Target Position 1") == -1000000
    assert [(e.tag, e.value) for e in entries(server, "write")] == [("Target Position 1", -1000000)]
    rr = client.read_holding_registers(a, count=2, device_id=0)
    assert rr.registers == [48576, 65520]


def test_position_words_written_separately_combine(server, client):
    a = addr(server, "Target Position 7")
    assert not client.write_register(a, 42080, device_id=0).isError()
    assert not client.write_register(a + 1, 65535, device_id=0).isError()
    with server.lock:
        assert server.model.read("Target Position 7") == -23456


def test_writing_position_with_neighbour_speed_in_one_request(server, client):
    # 第 1 軸 HR 121 ~ 123：Target Position（121、122）＋ Speed（123）
    assert not client.write_registers(121, [75000 & 0xFFFF, 75000 >> 16, 30], device_id=0).isError()
    with server.lock:
        assert server.model.read("Target Position 1") == 75000
        assert server.model.read("Speed 1") == 30


def test_write_to_read_only_point_is_illegal_address(server, client):
    rr = client.write_coil(addr(server, "Ready 1"), True, device_id=0)
    assert rr.isError() and rr.exception_code == ILLEGAL_ADDRESS
    rr = client.write_register(addr(server, "Alarm Code 1"), 5, device_id=0)
    assert rr.isError() and rr.exception_code == ILLEGAL_ADDRESS
    with server.lock:
        assert server.model.read("Ready 1") is False
        assert server.model.read("Alarm Code 1") == 0
    assert entries(server, "write") == []


# ---- 表外位址與強制例外 ----


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.read_coils(0, count=1, device_id=0),
        lambda c: c.read_coils(23, count=1, device_id=0),  # 第 1 軸 Coil 20 ~ 24 的空隙
        lambda c: c.read_coils(20, count=5, device_id=0),  # 範圍含空隙
        lambda c: c.read_discrete_inputs(100, count=1, device_id=0),
        lambda c: c.read_holding_registers(0, count=1, device_id=0),
        lambda c: c.read_holding_registers(20, count=10, device_id=0),
        lambda c: c.read_input_registers(20, count=1, device_id=0),
        lambda c: c.write_coil(500, True, device_id=0),
        lambda c: c.write_register(500, 1, device_id=0),
    ],
)
def test_undefined_address_returns_illegal_data_address(server, client, call):
    rr = call(client)
    assert rr.isError()
    assert rr.exception_code == ILLEGAL_ADDRESS


def test_force_exception_on_tag_and_release(server, client):
    alarm = server.points.get("Alarm Code 1")
    servo = addr(server, "Servo Motor 1")
    server.force_exception("Alarm Code 1")
    assert client.read_holding_registers(alarm.start_address, count=1, device_id=0).isError()
    # 涵蓋該點的批次讀取也回例外
    assert client.read_holding_registers(20, count=4, device_id=0).isError()
    # 其他點不受影響
    assert not client.read_holding_registers(30, count=1, device_id=0).isError()

    server.force_exception("Servo Motor 1")
    assert client.write_coil(servo, True, device_id=0).isError()
    with server.lock:
        assert server.model.read("Servo Motor 1") is False

    server.force_exception(None)
    assert read_point(client, alarm) == 0
    assert not client.write_coil(servo, True, device_id=0).isError()


# ---- Unit ID ----


def test_unit_id_zero_points_read_and_write(server, client):
    assert not client.write_coil(addr(server, "Servo Motor 2"), True, device_id=0).isError()
    assert read_point(client, server.points.get("Servo Motor 2"), device_id=0) is True


def test_points_are_separated_by_unit_id(tmp_path, config_dir, io_rows, write_io_points):
    io_rows.append(
        {
            "Tag Name": "Cylinder1 Up",
            "Display Name": "汽缸 1 上升",
            "Start Address": "20",
            "Address Length": "1",
            "Unit ID": "2",
            "Data Model": "Coil",
            "Sensor Type": "Boolean",
            "Access": "Read Write",
            "Visible": "O",
            "Description": "",
            "Attributes": "",
            "Check Status": "Unchecked",
        }
    )
    write_io_points(config_dir, io_rows)
    srv = make_server(config_dir)
    srv.start()
    try:
        c = ModbusTcpClient("127.0.0.1", port=srv.port, timeout=2, retries=0)
        assert c.connect()
        assert not c.write_coil(20, True, device_id=2).isError()
        with srv.lock:
            assert srv.model.read("Cylinder1 Up") is True
            assert srv.model.read("Ready 1") is False
        # Unit ID 2 沒有 Coil 21；Unit ID 5 沒有任何點
        assert c.read_coils(21, count=1, device_id=2).exception_code == ILLEGAL_ADDRESS
        assert c.read_coils(20, count=1, device_id=5).exception_code == ILLEGAL_ADDRESS
        c.close()
    finally:
        srv.stop()


# ---- 紀錄 ----


def test_connect_and_disconnect_are_logged(server):
    c = ModbusTcpClient("127.0.0.1", port=server.port, timeout=2, retries=0)
    assert c.connect()
    wait_until(lambda: len(entries(server, "connect")) == 1)
    assert entries(server, "disconnect") == []
    c.close()
    wait_until(lambda: len(entries(server, "disconnect")) == 1)


def test_read_count_and_last_read_time(server, client):
    assert server.log.read_count() == 0
    assert server.log.last_read_time() is None
    before = time.time()
    client.read_coils(20, count=1, device_id=0)
    client.read_discrete_inputs(24576, count=1, device_id=0)
    client.read_holding_registers(22, count=2, device_id=0)
    client.read_holding_registers(0, count=1, device_id=0)  # 例外回應也算一次讀取請求
    client.write_coil(addr(server, "Servo Motor 1"), True, device_id=0)  # 寫入不算讀取
    assert server.log.read_count() == 4
    last = server.log.last_read_time()
    assert last is not None and before - 0.01 <= last <= time.time() + 0.01


def test_state_changes_logged_including_plc_action_code(server, client):
    client.write_register(addr(server, "PC Action Code 1"), 2, device_id=0)
    client.write_coil(addr(server, "Servo Motor 1"), True, device_id=0)
    wait_until(
        lambda: ("PLC Action Code 1", 2) in [(e.tag, e.value) for e in entries(server, "state")]
    )
    state = [(e.tag, e.value) for e in entries(server, "state")]
    assert ("Ready 1", True) in state
    # Master 寫入的點只記在「寫入」，不重複記為狀態變化；位置不記
    tags = {t for t, _ in state}
    assert "Servo Motor 1" not in tags and "PC Action Code 1" not in tags
    assert not any(t.startswith("Current Position") for t in tags)


def test_state_changes_from_sim_operations_logged(server):
    with server.lock:
        server.model.master_write("Servo Motor 4", True)
    wait_until(lambda: ("Ready 4", True) in [(e.tag, e.value) for e in entries(server, "state")])
    with server.lock:
        server.model.inject_alarm(4, 7, persistent=False)
        server.model.set_estop(True)
    state = [(e.tag, e.value) for e in entries(server, "state")]
    assert ("Alarm Code 4", 7) in state
    assert ("Ready 4", False) in state
    assert any(t == "Emergency Stop" for t, _ in state)


def test_export_writes_text_file_with_all_kinds(server, client, tmp_path):
    client.write_coil(addr(server, "Servo Motor 1"), True, device_id=0)
    client.read_coils(20, count=1, device_id=0)
    wait_until(lambda: any(e.tag == "Ready 1" for e in entries(server, "state")))
    out = tmp_path / "sim_log.txt"
    server.log.export(out)
    text = out.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert any(
        "Servo Motor 1" in line and re.match(r".*\d{2}:\d{2}:\d{2}\.\d{3}", line) for line in lines
    )
    assert any("Ready 1" in line for line in lines)
    assert sum("127.0.0.1" in line for line in lines) >= 1  # 連線紀錄
    assert re.search(r"讀取請求累計 1 次", text)


# ---- 中斷所有連線 ----


def test_disconnect_all_keeps_state_and_accepts_new_clients(server, client):
    client.write_coil(addr(server, "Servo Motor 1"), True, device_id=0)
    client.write_register(addr(server, "Speed 1"), 55, device_id=0)
    wait_until(lambda: len(entries(server, "connect")) == 1)

    server.disconnect_all()

    # 既有連線被 server 端關閉：socket 讀到 EOF
    client.socket.settimeout(2)
    assert client.socket.recv(1) == b""
    wait_until(lambda: len(entries(server, "disconnect")) == 1)
    with server.lock:
        assert server.model.read("Servo Motor 1") is True
        assert server.model.read("Speed 1") == 55

    c2 = ModbusTcpClient("127.0.0.1", port=server.port, timeout=2, retries=0)
    assert c2.connect()
    assert read_point(c2, server.points.get("Speed 1")) == 55
    wait_until(lambda: len(entries(server, "connect")) == 2)
    c2.close()


def test_stop_releases_port(config_dir):
    srv = make_server(config_dir)
    srv.start()
    srv.stop()
    c = ModbusTcpClient("127.0.0.1", port=srv.port, timeout=0.5, retries=0)
    assert not c.connect()
    srv2 = SimServer(srv.model, srv.points, port=srv.port)
    srv2.start()
    srv2.stop()
