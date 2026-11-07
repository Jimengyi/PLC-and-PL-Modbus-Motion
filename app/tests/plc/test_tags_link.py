"""TagsLink：包住 modbus_tags.Bus 的 PlcPort adapter（spec FR-20、FR-21、FR-24、FR-25）。

大部分測試對 SimServer 走真實 TCP；需要控制快照、狀態與寫入結果時序的測試，以建構參數
`bus_factory` 注入替身 Bus，不碰 Channel 內部。
"""

import re
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lib.modbus_tags import DeviceStatus, PollStats, Snapshot, WriteFailed
from plc_motion.plc import PlcCommError, PlcModel
from plc_motion.plc import Snapshot as AppSnapshot
from plc_motion.plc.tags_link import TagsLink
from plc_motion.settings import load_config
from plc_motion.sim import SimServer


def wait_until(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "條件在時限內未成立"
        time.sleep(0.01)


def poll_snapshot(link: TagsLink, timeout: float = 3.0) -> AppSnapshot:
    """重複 poll 直到拿到快照。"""
    deadline = time.monotonic() + timeout
    while True:
        snap = link.poll()
        if snap is not None:
            return snap
        assert time.monotonic() < deadline, "時限內沒有快照"
        time.sleep(0.01)


def poll_error(link: TagsLink, timeout: float = 5.0) -> PlcCommError:
    """重複 poll 直到 raise PlcCommError，回傳該例外。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            link.poll()
        except PlcCommError as e:
            return e
        assert time.monotonic() < deadline, "時限內 poll 沒有 raise"
        time.sleep(0.01)


@pytest.fixture
def config(config_dir: Path):
    return load_config(config_dir)


@pytest.fixture
def server(config):
    srv = SimServer(PlcModel(config.points), config.points, port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def link(config, server):
    lk = TagsLink(config, "127.0.0.1", server.port)
    lk.connect()
    poll_snapshot(lk)
    yield lk
    lk.close()


def writes_seen(server: SimServer) -> list[tuple[str, int | bool]]:
    return [(e.tag, e.value) for e in server.log.entries() if e.kind == "write"]


# ---- 連線與讀取 ----


def test_first_snapshot_after_connect_matches_model(config, server):
    with server.lock:
        server.model.set_position(1, -2.3456)
        server.model.set_limit(2, "pos", True)
    lk = TagsLink(config, "127.0.0.1", server.port)
    try:
        assert not lk.connected
        lk.connect()
        snap = poll_snapshot(lk)
        assert lk.connected and not lk.connecting
        assert snap.raw("Current Position 1") == -23456
        assert snap.value("Current Position 1") == pytest.approx(-2.3456)
        assert snap.axis(2).pos_limit_triggered
        assert lk.request_count > 0
        assert lk.read_ms is not None
        assert lk.cycle_ms is None  # 連線後第一輪沒有週期
    finally:
        lk.close()


def test_connect_failure_makes_poll_raise(config):
    with socket.socket() as s:  # 取得一個沒人在聽的埠
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    lk = TagsLink(config, "127.0.0.1", free_port)
    try:
        lk.connect()
        err = poll_error(lk)
        assert f"127.0.0.1:{free_port}" in str(err)
        assert not lk.connected and not lk.connecting
    finally:
        lk.close()


def test_write_before_connect_raises(config, server):
    lk = TagsLink(config, "127.0.0.1", server.port)
    with pytest.raises(PlcCommError):
        lk.write("Servo Motor 1", True)


def test_cycle_ms_is_time_between_snapshots(link):
    poll_snapshot(link)
    assert link.cycle_ms is not None
    assert 100 < link.cycle_ms < 1000  # interval_ms = 200


# ---- 寫入值 ----


POSITION_RAWS = [0, 1, -1, 12345, -12345, 123456789, -123456789, 2**31 - 1, -(2**31), 99, -99]


@pytest.mark.parametrize("axis", [1, 4, 7])  # 倍率 10000、100、100
def test_position_raw_int_is_written_exactly(link, server, axis):
    tag = f"Target Position {axis}"
    for raw in POSITION_RAWS:
        link.write_wait(tag, raw, timeout=2)
        with server.lock:
            assert server.model.read(tag) == raw, raw


def test_float_to_scaled_point_is_engineering_value(link, server):
    link.write_wait("Target Position 1", 1.5, timeout=2)
    link.write_wait("Target Position 4", -2.25, timeout=2)
    with server.lock:
        assert server.model.read("Target Position 1") == 15000
        assert server.model.read("Target Position 4") == -225


def test_bool_is_logical_value(link, server):
    link.write_wait("Servo Motor 2", True, timeout=2)
    with server.lock:
        assert server.model.read("Servo Motor 2") is True


def test_writes_reach_plc_in_call_order(link, server):
    calls = [
        ("Target Position 3", 50000),
        ("Speed 3", 40),
        ("PC Action Code 3", 2),
        ("Servo Motor 5", True),
        ("Speed 5", 7),
        ("Target Position 5", -300),
    ]
    before = len(writes_seen(server))
    for tag, value in calls:
        link.write(tag, value)
    link.drain(3)
    assert writes_seen(server)[before:] == calls


def test_out_of_range_and_type_errors_are_not_comm_errors(link):
    from lib.modbus_tags import ReadOnlyTag, ValueOutOfRange

    with pytest.raises(ValueOutOfRange):
        link.write("Speed 1", 70000)
    with pytest.raises(TypeError):
        link.write("Servo Motor 1", "on")
    with pytest.raises(ReadOnlyTag):
        link.write("Ready 1", True)
    assert link.connected


# ---- 失敗 ----


def test_rejected_write_names_tag_and_address(link, server):
    server.reject_writes({"Speed 1"})
    link.write("Speed 1", 30)
    err = poll_error(link)
    msg = str(err)
    assert "Speed 1" in msg
    assert f"位址 {server.points.get('Speed 1').start_address}" in msg
    # 寫入被拒時連線仍在，仍可等待寫入結果（給 JOG 寫 0 用）
    assert link.connected
    link.write_wait("JOG Plus 1", False, timeout=2)


def test_rejected_write_wait_raises(link, server):
    server.reject_writes({"Speed 2"})
    with pytest.raises(PlcCommError) as ei:
        link.write_wait("Speed 2", 30, timeout=2)
    assert "Speed 2" in str(ei.value)


def test_rejected_batch_read_names_data_model_and_range(link, server):
    address = server.points.get("Alarm Code 1").start_address
    server.force_exception("Alarm Code 1")
    msg = str(poll_error(link))
    m = re.search(r"Holding Register (\d+) ~ (\d+)", msg)
    assert m, msg
    assert int(m.group(1)) <= address <= int(m.group(2))
    assert not link.connected


def test_connection_lost_raises(link, server):
    server.disconnect_all()
    poll_error(link)
    assert not link.connected


def test_auto_reconnect_in_device_toml_is_ignored(config_dir: Path):
    path = config_dir / "modbus" / "Channel01" / "device.toml"
    toml = path.read_text(encoding="utf-8")
    assert "auto_reconnect = false" in toml
    toml = toml.replace(
        "auto_reconnect = false", "auto_reconnect = true\nreconnect_interval_ms = 10"
    )
    path.write_bytes(toml.encode("utf-8"))
    config = load_config(config_dir)
    assert config.channel.device.auto_reconnect
    server = SimServer(PlcModel(config.points), config.points, port=0)
    server.start()
    lk = TagsLink(config, "127.0.0.1", server.port)
    try:
        lk.connect()
        poll_snapshot(lk)
        server.disconnect_all()
        poll_error(lk)
        connects = sum(e.kind == "connect" for e in server.log.entries())
        time.sleep(0.5)
        assert sum(e.kind == "connect" for e in server.log.entries()) == connects
        assert not lk.connected
        with pytest.raises(PlcCommError):
            lk.poll()
    finally:
        lk.close()
        server.stop()


# ---- 結束 ----


def test_close_sends_queued_writes_before_disconnecting(link, server):
    server.set_delay(30)
    calls = [(f"Speed {n}", 10 + n) for n in range(1, 8)]
    before = len(server.log.entries())
    for tag, value in calls:
        link.write(tag, value)
    link.close()
    wait_until(lambda: any(e.kind == "disconnect" for e in server.log.entries()[before:]))
    entries = server.log.entries()[before:]
    writes = [(e.tag, e.value) for e in entries if e.kind == "write"]
    assert writes == calls
    last_write = max(i for i, e in enumerate(entries) if e.kind == "write")
    disconnect = next(i for i, e in enumerate(entries) if e.kind == "disconnect")
    assert last_write < disconnect


def test_close_is_repeatable(link):
    link.close()
    link.close()
    assert not link.connected


# ---- 替身 Bus：時序控制 ----


class FakeTicket:
    def __init__(self, tag: str, value: object):
        self.tag = tag
        self.value = value
        self.error: WriteFailed | None = None
        self.finished = False

    def done(self) -> bool:
        return self.finished

    @property
    def ok(self) -> bool | None:
        return None if not self.finished else self.error is None

    def wait(self, timeout: float | None = None) -> None:
        if self.error is not None:
            raise self.error

    def fail(self, message: str) -> None:
        self.error = WriteFailed(message)
        self.finished = True


class FakeBus:
    """Bus 的公開方法替身：狀態、快照與寫入結果由測試直接設定。"""

    def __init__(self, points):
        self.points = points
        self.state = DeviceStatus.CONNECTING
        self.snap: Snapshot | None = None
        self.tickets: list[FakeTicket] = []
        self.started = False
        self.stopped = False
        self.t0 = datetime.now().astimezone()

    def set_round(self, n: int) -> None:
        """第 n 輪讀取完成（時間相差 n × 200 ms）。"""
        values = {p.tag: (False if p.data_type == "Bool" else 0) for p in self.points.all()}
        values["Speed 1"] = n
        self.snap = Snapshot("PLC", self.t0 + timedelta(milliseconds=200 * n), True, values)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def on_status(self, func):
        return lambda: None

    def status(self, device):
        return self.state

    def snapshot(self, device):
        return self.snap

    def stats(self, device):
        return PollStats(1.0, 1.0, 1.0, 3, 200)

    def write(self, ref, value):
        ticket = FakeTicket(ref, value)
        self.tickets.append(ticket)
        return ticket


@pytest.fixture
def fake(config):
    bus = FakeBus(config.points)
    lk = TagsLink(config, "127.0.0.1", 502, bus_factory=lambda channel: bus)
    lk.connect()
    assert bus.started
    yield lk, bus
    lk.close()


def test_snapshot_with_write_failure_raises(fake):
    lk, bus = fake
    bus.state = DeviceStatus.CONNECTED
    bus.set_round(1)
    assert lk.poll() is not None
    lk.write("Speed 1", 10)
    bus.set_round(2)  # 失敗那一輪的快照已經更新
    bus.tickets[-1].fail("exception response code 4")
    with pytest.raises(PlcCommError) as ei:
        lk.poll()
    assert "Speed 1" in str(ei.value)


def test_same_round_is_returned_once(fake):
    lk, bus = fake
    bus.state = DeviceStatus.CONNECTED
    bus.set_round(1)
    first = lk.poll()
    assert first is not None and first.value("Speed 1") == 1
    assert lk.poll() is None
    assert lk.poll() is None
    bus.set_round(2)
    second = lk.poll()
    assert second is not None and second.value("Speed 1") == 2
    assert lk.cycle_ms == pytest.approx(200)
    assert lk.poll() is None


def test_first_round_waits_for_connected(fake):
    lk, bus = fake
    bus.set_round(1)  # 第一輪快照先於狀態
    assert lk.connecting
    assert lk.poll() is None
    assert lk.poll() is None
    bus.state = DeviceStatus.CONNECTED
    snap = lk.poll()
    assert snap is not None and snap.value("Speed 1") == 1
    assert lk.cycle_ms is None


def test_int_to_scaled_point_is_raw_value(fake):
    lk, bus = fake
    bus.state = DeviceStatus.CONNECTED
    lk.write("Target Position 1", 12345)
    lk.write("Target Position 4", 1.5)
    lk.write("Speed 1", 30)
    assert [(t.tag, t.value) for t in bus.tickets] == [
        ("PLC/Target Position 1", pytest.approx(1.2345)),
        ("PLC/Target Position 4", 1.5),
        ("PLC/Speed 1", 30),
    ]


def test_close_stops_bus(fake):
    lk, bus = fake
    lk.close()
    assert bus.stopped
