"""ModbusLink：對 SimServer（真 TCP、localhost）讀寫（spec FR-12、FR-13 偵測面）。"""

import socket
import threading
import time
from pathlib import Path

import pytest
from pymodbus.client import ModbusTcpClient

from plc_motion.plc import PlcCommError, PlcModel
from plc_motion.plc.modbus_link import ModbusLink
from plc_motion.settings import load_config
from plc_motion.sim import SimServer


def wait_until(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "條件在時限內未成立"
        time.sleep(0.01)


@pytest.fixture
def server(config_dir: Path):
    points = load_config(config_dir).points
    srv = SimServer(PlcModel(points), points, port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def link(server):
    lk = ModbusLink(server.points, "127.0.0.1", server.port, timeout_ms=1000)
    lk.connect()
    yield lk
    lk.close()


def model_values(server: SimServer) -> dict:
    with server.lock:
        return {p.tag: server.model.read(p.tag) for p in server.points.all()}


# ---- 連線 ----


def test_connect_sets_connected_and_close_clears_it(server):
    lk = ModbusLink(server.points, "127.0.0.1", server.port, timeout_ms=1000)
    assert not lk.connected
    lk.connect()
    assert lk.connected
    wait_until(lambda: any(e.kind == "connect" for e in server.log.entries()))
    lk.close()
    assert not lk.connected


def test_connect_failure_raises_plc_comm_error(config_dir):
    points = load_config(config_dir).points
    with socket.socket() as s:  # 取得一個沒人在聽的埠
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    lk = ModbusLink(points, "127.0.0.1", free_port, timeout_ms=500)
    with pytest.raises(PlcCommError):
        lk.connect()
    assert not lk.connected


def test_read_before_connect_raises(server):
    lk = ModbusLink(server.points, "127.0.0.1", server.port, timeout_ms=1000)
    with pytest.raises(PlcCommError):
        lk.read_all()
    with pytest.raises(PlcCommError):
        lk.write("Servo Motor 1", True)
    assert not any(e.kind == "connect" for e in server.log.entries())


# ---- 讀取 ----


def test_read_all_matches_model_for_every_point(server, link):
    with server.lock:
        server.model.set_position(1, -100.0)
        server.model.set_position(3, -2.3456)
        server.model.set_position(7, 12.34)
        server.model.set_limit(2, "pos", True)
        server.model.set_limit(4, "home", True)
        server.model.inject_alarm(5, 7, persistent=True)

    snap = link.read_all()
    expected = model_values(server)

    # 全部點（含 Visible = X 的 JOG Plus/Minus、Request）都在快照中且與 model 一致
    assert expected.keys() == {p.tag for p in server.points.all()}
    for tag, value in expected.items():
        assert snap.raw(tag) == value, tag
    assert snap.raw("Current Position 1") == -1000000
    assert snap.raw("Current Position 3") == -23456
    assert snap.value("Current Position 7") == pytest.approx(12.34)
    assert snap.axis(2).pos_limit_triggered
    assert snap.axis(5).alarm_code == 7


def test_read_all_merges_requests(server, link):
    before = server.log.read_count()
    link.read_all()
    wait_until(lambda: server.log.read_count() > before)
    requests = server.log.read_count() - before
    assert 0 < requests < len(server.points.all())


def test_read_all_does_not_touch_undefined_addresses(server, link):
    # SimServer 對表外位址回 Illegal Data Address；連讀數輪都不得失敗
    for _ in range(3):
        link.read_all()
    assert link.connected


# ---- 寫入 ----


def test_write_position_low_word_first(server, link):
    link.write("Target Position 1", -1000000)
    tgt = server.points.get("Target Position 1")
    c = ModbusTcpClient("127.0.0.1", port=server.port, timeout=2, retries=0)
    assert c.connect()
    try:
        rr = c.read_holding_registers(tgt.start_address, count=2, device_id=0)
        assert rr.registers == [48576, 65520]
    finally:
        c.close()
    with server.lock:
        assert server.model.read("Target Position 1") == -1000000


def test_write_coil_and_register(server, link):
    link.write("Servo Motor 3", True)
    link.write("Speed 3", 42)
    with server.lock:
        assert server.model.read("Servo Motor 3")
        assert server.model.read("Speed 3") == 42
    writes = [(e.tag, e.value) for e in server.log.entries() if e.kind == "write"]
    assert ("Servo Motor 3", True) in writes
    assert ("Speed 3", 42) in writes


def test_write_unknown_tag_raises(link):
    with pytest.raises(PlcCommError):
        link.write("No Such Tag", 1)


# ---- 通訊失敗 ----


def test_server_stopped_raises_on_read_and_write(server, link):
    server.stop()
    with pytest.raises(PlcCommError):
        link.read_all()
    assert not link.connected
    with pytest.raises(PlcCommError):
        link.write("Servo Motor 1", True)


def test_exception_response_on_read_names_tag_and_address(server, link):
    server.force_exception("Alarm Code 3")
    with pytest.raises(PlcCommError) as ei:
        link.read_all()
    msg = str(ei.value)
    assert "Alarm Code 3" in msg
    assert str(server.points.get("Alarm Code 3").start_address) in msg
    assert ei.value.tag == "Alarm Code 3"
    # 解除後恢復正常
    server.force_exception(None)
    link.read_all()


def test_exception_response_on_write_names_tag_and_address(server, link):
    server.force_exception("Speed 2")
    with pytest.raises(PlcCommError) as ei:
        link.write("Speed 2", 10)
    msg = str(ei.value)
    assert "Speed 2" in msg
    assert str(server.points.get("Speed 2").start_address) in msg


@pytest.fixture
def silent_server():
    """接受 TCP 連線但永不回應的 server，用來製造讀寫逾時。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    conns: list[socket.socket] = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conns.append(conn)
            except OSError:
                continue

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(1)
    for c in conns:
        c.close()
    srv.close()


def test_read_timeout_raises(config_dir, silent_server):
    points = load_config(config_dir).points
    lk = ModbusLink(points, "127.0.0.1", silent_server, timeout_ms=200)
    lk.connect()
    start = time.monotonic()
    with pytest.raises(PlcCommError):
        lk.read_all()
    assert time.monotonic() - start < 3
    assert not lk.connected
    lk.close()


def test_write_timeout_raises(config_dir, silent_server):
    points = load_config(config_dir).points
    lk = ModbusLink(points, "127.0.0.1", silent_server, timeout_ms=200)
    lk.connect()
    with pytest.raises(PlcCommError):
        lk.write("Servo Motor 1", True)
    lk.close()
