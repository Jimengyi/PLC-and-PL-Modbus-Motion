"""SimServer：以 ModbusTcpClient 直接連線讀寫，斷言回應值與 SimLog（spec FR-51、FR-53、FR-56）。"""

import dataclasses
import re
import time
from pathlib import Path

import pytest
from pymodbus.client import ModbusTcpClient

from lib.modbus_tags import Bus, DataModel, DeviceStatus, ReadOnlyTag
from plc_motion.codec import join_i32
from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.settings.io_points import is_position
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
    if is_position(point):
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


# ---- 故障注入 ----

DEVICE_FAILURE = 4


def _median_read_seconds(client, address: int, n: int = 5) -> float:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        assert not client.read_holding_registers(address, count=1, device_id=0).isError()
        times.append(time.perf_counter() - t0)
    return sorted(times)[n // 2]


def test_set_delay_slows_responses_and_release_restores(server, client):
    a = addr(server, "Speed 1")
    base = _median_read_seconds(client, a)
    server.set_delay(300)
    assert _median_read_seconds(client, a, n=3) >= 0.28
    server.set_delay(0)
    assert _median_read_seconds(client, a) < 0.15
    assert base < 0.15


def test_reject_writes_only_blocks_writes_of_given_tags(server, client):
    speed = addr(server, "Speed 1")
    server.reject_writes({"Speed 1"})
    rr = client.write_register(speed, 50, device_id=0)
    assert rr.isError() and rr.exception_code == DEVICE_FAILURE
    with server.lock:
        assert server.model.read("Speed 1") != 50
    # 讀取同一點與寫其他點都正常
    assert read_point(client, server.points.get("Speed 1")) is not None
    assert not client.write_register(addr(server, "Speed 2"), 60, device_id=0).isError()
    # 空集合解除
    server.reject_writes(set())
    assert not client.write_register(speed, 50, device_id=0).isError()
    with server.lock:
        assert server.model.read("Speed 1") == 50


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
            "Data Type": "Bool",
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


# ---- 多型別（spec FR-13、FR-33）：以 modbus_tags.Bus 走 TCP ----


def _row(tag, address, length, model, data_type, access, attributes=""):
    return {
        "Tag Name": tag,
        "Display Name": "",
        "Start Address": str(address),
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


_HR, _IR = "Holding Register", "Input Register"
_RW, _RO = "Read Write", "Read Only"
EXTRA_ROWS = [
    _row("Aux U16", 300, 1, _HR, "U16", _RW),
    _row("Aux I16", 301, 1, _HR, "I16", _RW),
    _row("Aux U32", 302, 2, _HR, "U32", _RW),
    _row("Aux U32 HF", 304, 2, _HR, "U32", _RW, "word_order=high_first"),
    _row("Aux I32", 306, 2, _HR, "I32", _RW, "scale=100;unit=mm"),
    _row("Aux F32", 308, 2, _HR, "F32", _RW, "byte_swap=true"),
    _row("Aux String", 310, 4, _HR, "String", _RW),
    _row("Aux String Swap", 314, 2, _HR, "String", _RW, "byte_swap=true"),
    _row("Lamp A", "320.b0", 1, _HR, "Bool", _RW),
    _row("Lamp B", "320.b5", 1, _HR, "Bool", _RW),
    _row("Door Closed", "320.b9", 1, _HR, "Bool", _RO, "invert=true"),
    _row("Aux Coil", 400, 1, "Coil", "Bool", _RW),
    _row("Aux DI", 400, 1, "Discrete Input", "Bool", _RO),
    _row("IR U16", 0, 1, _IR, "U16", _RO),
    _row("IR Bit", "1.b3", 1, _IR, "Bool", _RO, "invert=true"),
    _row("IR F32", 2, 2, _IR, "F32", _RO),
]


@pytest.fixture
def multi(config_dir, io_rows, write_io_points):
    """加上多型別非必要點的 SimServer，與連到它的 Bus（已讀完第一輪）。"""
    write_io_points(config_dir, io_rows + EXTRA_ROWS)
    config = load_config(config_dir)
    srv = SimServer(PlcModel(config.points), config.points, port=0)
    srv.start()
    device = dataclasses.replace(config.channel.device, tcp_host="127.0.0.1", tcp_port=srv.port)
    bus = Bus([dataclasses.replace(config.channel, device=device)], {})
    bus.start()
    try:
        wait_until(lambda: bus.status("PLC") == DeviceStatus.CONNECTED and _has_round(bus))
        yield srv, bus
    finally:
        bus.stop()
        srv.stop()


def _has_round(bus) -> bool:
    snap = bus.snapshot("PLC")
    return snap is not None and snap.connected


def _fresh_read(bus, tag: str, expected) -> None:
    """等 Bus 的快照讀到 expected。"""
    wait_until(lambda: bus.read(f"PLC/{tag}") == expected)


def _set(srv: SimServer, tag: str, raw) -> None:
    with srv.lock:
        srv.model.master_write(tag, raw)


def _model(srv: SimServer, tag: str):
    with srv.lock:
        return srv.model.read(tag)


def _registers(srv: SimServer, address: int, count: int, func="read_holding_registers"):
    c = ModbusTcpClient("127.0.0.1", port=srv.port, timeout=2, retries=0)
    assert c.connect()
    try:
        rr = getattr(c, func)(address, count=count, device_id=0)
        assert not rr.isError()
        return rr.registers
    finally:
        c.close()


@pytest.mark.parametrize(
    ("tag", "plc_raw", "read_value", "write_value", "model_after"),
    [
        ("Aux U16", 65535, 65535, 1234, 1234),
        ("Aux I16", -5, -5, -32768, -32768),
        ("Aux U32", 4_000_000_000, 4_000_000_000, 70000, 70000),
        ("Aux U32 HF", 4_000_000_000, 4_000_000_000, 70000, 70000),
        ("Aux I32", -12345, -123.45, -1.5, -150),
        ("Aux F32", 1.5, 1.5, -2.25, -2.25),
        ("Aux String", "AB", "AB", "Hello12", "Hello12"),
        ("Aux String Swap", "xy", "xy", "abcd", "abcd"),
        ("Aux Coil", True, True, False, False),
    ],
)
def test_bus_reads_and_writes_match_model(
    multi, tag, plc_raw, read_value, write_value, model_after
):
    srv, bus = multi
    _set(srv, tag, plc_raw)
    _fresh_read(bus, tag, read_value)
    bus.write_wait(f"PLC/{tag}", write_value, timeout=3)
    assert _model(srv, tag) == model_after
    _fresh_read(bus, tag, write_value)


def test_read_only_types_read_model_values(multi):
    srv, bus = multi
    _set(srv, "Aux DI", True)
    _set(srv, "IR U16", 42)
    _set(srv, "IR F32", 3.25)
    _fresh_read(bus, "Aux DI", True)
    _fresh_read(bus, "IR U16", 42)
    _fresh_read(bus, "IR F32", 3.25)


def test_input_register_write_is_refused(multi):
    srv, bus = multi
    with pytest.raises(ReadOnlyTag):
        bus.write_wait("PLC/IR U16", 7, timeout=3)
    assert _model(srv, "IR U16") == 0
    # 寫入功能碼到同一位址是 Holding Register，沒有定義的點：回例外
    c = ModbusTcpClient("127.0.0.1", port=srv.port, timeout=2, retries=0)
    assert c.connect()
    rr = c.write_register(0, 7, device_id=0)
    c.close()
    assert rr.isError() and rr.exception_code == ILLEGAL_ADDRESS
    assert _model(srv, "IR U16") == 0


def test_word_order_and_byte_swap_register_layout(multi):
    srv, bus = multi
    bus.write_wait("PLC/Aux U32", 70000, timeout=3)  # 0x0001_1170
    bus.write_wait("PLC/Aux U32 HF", 70000, timeout=3)
    bus.write_wait("PLC/Aux F32", 1.5, timeout=3)  # 0x3FC0_0000
    bus.write_wait("PLC/Aux String Swap", "abcd", timeout=3)
    regs = _registers(srv, 302, 8)
    assert regs[0:2] == [0x1170, 0x0001]  # low_first
    assert regs[2:4] == [0x0001, 0x1170]  # high_first
    assert regs[6:8] == [0x0000, 0xC03F]  # low_first + byte_swap
    assert _registers(srv, 314, 2) == [0x6261, 0x6463]


def test_register_bits_are_independent(multi):
    srv, bus = multi
    bus.write_wait("PLC/Lamp A", True, timeout=3)
    assert (_model(srv, "Lamp A"), _model(srv, "Lamp B")) == (True, False)
    bus.write_wait("PLC/Lamp B", True, timeout=3)
    assert (_model(srv, "Lamp A"), _model(srv, "Lamp B")) == (True, True)
    bus.write_wait("PLC/Lamp A", False, timeout=3)
    assert (_model(srv, "Lamp A"), _model(srv, "Lamp B")) == (False, True)
    assert _model(srv, "Door Closed") is True  # 唯讀位元不受影響
    assert _registers(srv, 320, 1) == [(1 << 5) | (1 << 9)]
    _fresh_read(bus, "Lamp A", False)
    assert bus.read("PLC/Lamp B") is True


def test_register_bit_read_follows_model(multi):
    srv, bus = multi
    _set(srv, "Lamp B", True)
    _fresh_read(bus, "Lamp B", True)
    assert bus.read("PLC/Lamp A") is False


def test_inverted_register_bits_start_untriggered(multi):
    srv, bus = multi
    assert bus.read("PLC/Door Closed") is False
    assert bus.read("PLC/IR Bit") is False
    assert _registers(srv, 320, 1) == [1 << 9]
    assert _registers(srv, 1, 1, "read_input_registers") == [1 << 3]


def test_write_to_register_with_only_read_only_bits_is_illegal_address(multi):
    srv, _ = multi
    c = ModbusTcpClient("127.0.0.1", port=srv.port, timeout=2, retries=0)
    assert c.connect()
    # 改到唯讀位元（Door Closed，b9）的寫入被拒，其他位元也不變
    rr = c.write_register(320, 1, device_id=0)
    c.close()
    assert rr.isError() and rr.exception_code == ILLEGAL_ADDRESS
    assert (_model(srv, "Lamp A"), _model(srv, "Door Closed")) == (False, True)
