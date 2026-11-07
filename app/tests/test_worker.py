"""PlcWorker：背景執行緒內的連線、輪詢、命令轉派與日誌（spec FR-10 ~ FR-14、FR-27、FR-49、FR-50）。

PLC 端為 SimServer（真 TCP、localhost）；斷言一律經由 worker 的 signal、SimServer 的紀錄與日誌。
"""

import logging
import socket
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from lib.modbus_tags import Bus, DeviceStatus, PollStats, WriteFailed
from lib.modbus_tags import Snapshot as LibSnapshot
from plc_motion.engine import (
    DisconnectedWhileMoving,
    MotionEngine,
    OperationFinished,
    ResultKind,
    WaitPoint,
)
from plc_motion.logging_setup import close_logging, log_file_name, setup_logging
from plc_motion.plc import PlcModel
from plc_motion.plc.tags_link import TagsLink
from plc_motion.settings import load_config
from plc_motion.sim import SimServer
from plc_motion.worker import EngineStatus, start_worker

WORKER_LOGGER = "plc_motion"


def set_poll_interval(config_dir: Path, interval_ms: int) -> None:
    path = config_dir / "modbus" / "Channel01" / "device.toml"
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(text.replace("interval_ms = 200", f"interval_ms = {interval_ms}").encode())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(config_dir: Path):
    points = load_config(config_dir).points
    srv = SimServer(PlcModel(points), points, port=0)
    srv.start()
    yield srv
    srv.stop()


class Probe:
    """在主執行緒收集 worker 的 signal。"""

    def __init__(self, worker):
        self.connection: list[bool] = []
        self.events: list = []
        self.snapshots: list = []
        worker.connection_changed.connect(self.connection.append)
        worker.engine_event.connect(self.events.append)
        worker.snapshot_ready.connect(lambda snap, status: self.snapshots.append((snap, status)))

    def finished(self) -> list[OperationFinished]:
        return [e for e in self.events if isinstance(e, OperationFinished)]

    @property
    def status(self) -> EngineStatus:
        return self.snapshots[-1][1]


@pytest.fixture
def make_worker(config_dir, qtbot):
    started = []

    def _make(host="127.0.0.1", port=None, interval_ms=None, server=None):
        if interval_ms is not None:
            set_poll_interval(config_dir, interval_ms)
        config = load_config(config_dir)
        thread, worker = start_worker(config, host, server.port if port is None else port)
        probe = Probe(worker)
        started.append((thread, worker))
        return thread, worker, probe

    yield _make
    for thread, worker in started:
        worker.stop()
        assert thread.wait(5000)


def until(qtbot, cond, timeout: int = 5000) -> None:
    qtbot.waitUntil(lambda: bool(cond()), timeout=timeout)


def wait_connected(qtbot, probe: Probe, value: bool = True, timeout: int = 3000) -> None:
    until(qtbot, lambda: probe.connection and probe.connection[-1] is value, timeout=timeout)


def writes(server: SimServer, tag: str) -> list:
    return [e for e in server.log.entries() if e.kind == "write" and e.tag == tag]


def log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


@pytest.fixture
def caplog_info(caplog):
    caplog.set_level(logging.INFO, logger=WORKER_LOGGER)
    return caplog


def prepare_servo(server: SimServer, axes=range(1, 8), homed: bool = True) -> None:
    """測試準備：直接在模擬 PLC 端 Servo ON，並可省略原點復歸。"""
    with server.lock:
        for n in axes:
            server.model.master_write(f"Servo Motor {n}", True)
            if homed:
                server.model.master_write(f"Find Home Finish {n}", True)


def wait_ready(qtbot, probe: Probe, axes=range(1, 8)) -> None:
    def ok():
        if not probe.snapshots or probe.snapshots[-1][0] is None:
            return False
        snap, status = probe.snapshots[-1]
        return all(snap.axis(n).ready for n in axes) and not status.locked

    until(qtbot, ok, timeout=3000)


# ---- 執行緒 ----


def test_modbus_io_and_engine_calls_run_in_worker_thread(qtbot, server, make_worker, monkeypatch):
    calls: list[tuple[str, int]] = []

    def spy(cls, name):
        original = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            calls.append((name, threading.get_ident()))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, wrapper)

    for name in ("connect", "poll", "write", "write_wait", "close"):
        spy(TagsLink, name)
    for name in (
        "tick",
        "on_connected",
        "on_disconnected",
        "move",
        "step",
        "home",
        "servo_all",
        "reset_all",
        "jog_press",
        "jog_release",
        "write_point",
        "clear_residual",
    ):
        spy(MotionEngine, name)

    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.servo_all(True)
    worker.write_point("Speed 3", 40)
    worker.move({1: 1.0}, False, 100)
    until(qtbot, lambda: probe.finished(), timeout=5000)
    worker.step(2, 0.5, 100)
    until(qtbot, lambda: len(probe.finished()) == 2, timeout=5000)
    worker.home({4})
    until(qtbot, lambda: len(probe.finished()) == 3, timeout=10000)
    worker.reset_all()  # 沒有 Alarm：被拒絕，但仍在 worker 執行緒呼叫引擎
    worker.jog_press(5, +1, 10)
    until(qtbot, lambda: writes(server, "JOG Plus 5"), timeout=3000)
    worker.jog_release(5, +1)
    until(qtbot, lambda: len(writes(server, "JOG Plus 5")) == 2, timeout=3000)
    worker.clear_residual()
    worker.connect_plc()
    until(qtbot, lambda: probe.connection.count(True) == 2, timeout=3000)

    main = threading.get_ident()
    names = {n for n, _ in calls}
    assert {"connect", "poll", "write", "close", "tick", "on_connected"} <= names
    assert {"move", "step", "home", "servo_all", "reset_all", "jog_press", "jog_release"} <= names
    assert {"write_point", "clear_residual"} <= names
    idents = {ident for _, ident in calls}
    assert main not in idents
    assert len(idents) == 1


# ---- 連線（FR-10、FR-11、FR-14） ----


def test_auto_connect_once_on_start_without_retry(qtbot, config_dir, make_worker):
    port = free_port()
    _, worker, probe = make_worker(port=port, interval_ms=100)
    until(qtbot, lambda: probe.connection == [False], timeout=3000)

    points = load_config(config_dir).points
    srv = SimServer(PlcModel(points), points, port=port)
    srv.start()
    try:
        qtbot.wait(600)  # 數個輪詢週期
        assert not [e for e in srv.log.entries() if e.kind == "connect"]
        assert probe.connection == [False]

        worker.connect_plc()
        wait_connected(qtbot, probe)
        assert [e for e in srv.log.entries() if e.kind == "connect"]
    finally:
        worker.stop()
        srv.stop()


def test_host_port_override_is_not_written_back(qtbot, config_dir, server, make_worker):
    before = (config_dir / "modbus" / "Channel01" / "device.toml").read_bytes()
    _, worker, probe = make_worker(host="127.0.0.1", server=server)
    wait_connected(qtbot, probe)
    assert (config_dir / "modbus" / "Channel01" / "device.toml").read_bytes() == before
    assert load_config(config_dir).device.host == "192.168.1.105"


# ---- 輪詢（FR-12） ----


@pytest.mark.parametrize(("interval_ms", "low", "high"), [(100, 7, 12), (300, 2, 5)])
def test_poll_interval_follows_device_toml(qtbot, server, make_worker, interval_ms, low, high):
    _, worker, probe = make_worker(server=server, interval_ms=interval_ms)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots, timeout=3000)
    count = len(probe.snapshots)
    qtbot.wait(1000)
    assert low <= len(probe.snapshots) - count <= high


def test_snapshot_ready_carries_engine_status(qtbot, server, make_worker):
    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    snap, status = probe.snapshots[-1]
    assert snap.axis(1).servo
    assert status == EngineStatus(
        locked=False, lock_reasons=(), connected=True, operating=False, residual=()
    )

    worker.move({1: 2.0}, False, 10)
    until(qtbot, lambda: probe.status.operating, timeout=3000)
    assert probe.status.locked
    assert "操作進行中" in probe.status.lock_reasons
    until(qtbot, lambda: probe.finished(), timeout=5000)
    until(qtbot, lambda: not probe.status.operating, timeout=3000)


# ---- 通訊中斷（FR-13、FR-50） ----


def test_server_stop_disconnects_within_one_poll(qtbot, server, make_worker, caplog_info):
    _, worker, probe = make_worker(server=server, interval_ms=200)
    wait_connected(qtbot, probe)
    server.stop()
    t0 = time.monotonic()
    wait_connected(qtbot, probe, value=False, timeout=200 + 100)
    assert time.monotonic() - t0 <= 0.3
    assert "斷線" in log_text(caplog_info)
    # connection_changed 與之後的 snapshot_ready 是兩個排隊的 signal：等斷線後的狀態也送到
    until(qtbot, lambda: probe.snapshots and probe.status.connected is False)
    assert probe.status.locked


def test_modbus_exception_is_comm_lost_and_logs_tag(qtbot, server, make_worker, caplog_info):
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    server.force_exception("Alarm Code 3")
    wait_connected(qtbot, probe, value=False)
    # 批次讀取失敗只指出範圍（Data Model 與位址），不逐點定位 Tag（遷移 spec FR-25）
    text = log_text(caplog_info)
    assert "Holding Register 40 ~" in text
    # worker 自行斷開連線
    until(qtbot, lambda: [e for e in server.log.entries() if e.kind == "disconnect"])

    server.force_exception(None)
    worker.connect_plc()
    wait_connected(qtbot, probe)


def test_disconnect_while_moving_ends_operation(qtbot, server, make_worker, caplog_info):
    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.move({1: 50.0}, False, 1)
    until(qtbot, lambda: probe.snapshots[-1][0].axis(1).busy, timeout=3000)
    server.disconnect_all()
    wait_connected(qtbot, probe, value=False)
    until(qtbot, lambda: probe.finished(), timeout=3000)
    assert probe.finished()[-1].results[1].kind == ResultKind.COMM_LOST
    text = log_text(caplog_info)
    assert "通訊中斷" in text
    assert "可能仍在移動" in text


def test_unexpected_exception_is_logged_and_worker_can_reconnect(
    qtbot, server, make_worker, monkeypatch, caplog_info
):
    original = MotionEngine.tick
    raised = []

    def tick(self, snapshot):
        if not raised:
            raised.append(True)
            raise RuntimeError("測試用的未預期例外")
        return original(self, snapshot)

    monkeypatch.setattr(MotionEngine, "tick", tick)
    _, worker, probe = make_worker(server=server)
    until(qtbot, lambda: probe.connection == [True, False], timeout=3000)
    records = [r for r in caplog_info.records if r.exc_info]
    assert records
    assert "測試用的未預期例外" in str(records[0].exc_info[1])
    assert probe.status.connected is False

    worker.connect_plc()
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.status.connected, timeout=3000)


def test_unexpected_exception_during_connect_reports_disconnected(
    qtbot, server, make_worker, monkeypatch, caplog_info
):
    original = TagsLink.connect
    raised = []

    def connect(self):
        if not raised:
            raised.append(True)
            raise OSError("測試用的連線例外")
        return original(self)

    monkeypatch.setattr(TagsLink, "connect", connect)
    _, worker, probe = make_worker(server=server)
    until(qtbot, lambda: probe.connection == [False], timeout=3000)
    records = [r for r in caplog_info.records if r.exc_info]
    assert records
    assert "測試用的連線例外" in str(records[0].exc_info[1])

    worker.connect_plc()
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.status.connected, timeout=3000)


# ---- JOG 放開插隊（FR-27） ----


def test_jog_release_writes_zero_without_waiting_for_poll(qtbot, server, make_worker):
    prepare_servo(server, homed=False)
    _, worker, probe = make_worker(server=server, interval_ms=1000)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.jog_press(1, +1, 10)
    until(qtbot, lambda: writes(server, "JOG Plus 1"), timeout=3000)
    qtbot.wait(300)  # 落在兩次輪詢之間

    t0 = time.time()
    worker.jog_release(1, +1)
    until(qtbot, lambda: len(writes(server, "JOG Plus 1")) == 2, timeout=1000)
    release = writes(server, "JOG Plus 1")[-1]
    assert release.value is False
    assert release.time - t0 <= 0.1


# ---- 殘留交握（FR-57、FR-58） ----


def test_residual_handshake_detected_logged_and_cleared(qtbot, server, make_worker, caplog_info):
    with server.lock:
        server.model.master_write("Request 2", True)
        server.model.master_write("PC Action Code 2", 2)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots and probe.status.residual, timeout=3000)
    assert [r.axis for r in probe.status.residual] == [2]
    assert probe.status.locked
    assert "殘留交握" in log_text(caplog_info)

    worker.clear_residual()
    until(qtbot, lambda: writes(server, "PC Action Code 2"), timeout=3000)
    assert writes(server, "Request 2")[-1].value is False
    assert writes(server, "PC Action Code 2")[-1].value == 0
    until(qtbot, lambda: not probe.status.residual, timeout=3000)
    assert "寫回 0" in log_text(caplog_info)


def test_jog_residual_cleared_is_logged(qtbot, server, make_worker, caplog_info):
    with server.lock:
        server.model.master_write("JOG Minus 4", True)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: writes(server, "JOG Minus 4"), timeout=3000)
    assert writes(server, "JOG Minus 4")[-1].value is False
    text = log_text(caplog_info)
    assert "JOG Minus 4" in text
    assert "未放開" in text


# ---- 日誌內容（FR-49） ----


def test_operation_params_and_results_are_logged(qtbot, server, make_worker, caplog_info):
    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.move({1: 1.5}, False, 100)
    until(qtbot, lambda: probe.finished(), timeout=5000)
    text = log_text(caplog_info)
    assert "操作開始：移動" in text
    assert "1.5" in text and "速度 100" in text
    assert "操作結束：移動" in text
    assert "第 1 軸：完成" in text


def test_rejected_command_is_logged(qtbot, server, make_worker, caplog_info):
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots, timeout=3000)
    worker.reset_all()
    until(qtbot, lambda: "命令被拒絕" in log_text(caplog_info), timeout=3000)
    assert "不需復歸" in log_text(caplog_info)


def test_alarm_and_estop_changes_are_logged(qtbot, server, make_worker, caplog_info):
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots, timeout=3000)
    with server.lock:
        server.model.inject_alarm(2, 7, persistent=True)
    until(qtbot, lambda: "Alarm Code 2：0 → 7" in log_text(caplog_info), timeout=3000)
    with server.lock:
        server.model.set_estop(True)
    until(qtbot, lambda: "Emergency Stop：觸發" in log_text(caplog_info), timeout=3000)
    with server.lock:
        server.model.set_estop(False)
    until(qtbot, lambda: "Emergency Stop：解除" in log_text(caplog_info), timeout=3000)


def test_io_write_is_logged(qtbot, server, make_worker, caplog_info):
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots, timeout=3000)
    worker.write_point("Speed 3", 40)
    until(qtbot, lambda: writes(server, "Speed 3"), timeout=3000)
    assert "IO 寫入：Speed 3 = 40" in log_text(caplog_info)


def test_events_are_written_to_daily_log_file(qtbot, tmp_path, server, make_worker):
    logs_dir = tmp_path / "logs"
    setup_logging(logs_dir, keep_days=30)
    try:
        _, worker, probe = make_worker(server=server)
        wait_connected(qtbot, probe)
        until(qtbot, lambda: probe.snapshots, timeout=3000)
        server.stop()
        wait_connected(qtbot, probe, value=False)
        text = (logs_dir / log_file_name(date.today())).read_text(encoding="utf-8")
        assert "已連線" in text
        assert "斷線" in text
    finally:
        close_logging()


# ---- 通訊計時 ----


def test_poll_timed_reports_read_time_cycle_and_request_count(
    qtbot, server, make_worker, config_dir
):
    _thread, worker, probe = make_worker(server=server, interval_ms=50)
    timings: list = []
    worker.poll_timed.connect(timings.append)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: len(timings) >= 6)
    first, *rest = timings
    assert first.cycle_ms is None  # 連線後第一輪沒有間隔
    assert all(t.read_ms > 0 for t in timings)
    # 間隔約等於 50 ms 的輪詢週期；個別一輪可能因計時器晚觸發而偏短或偏長，所以看平均
    assert all(t.cycle_ms > 0 for t in rest)
    assert 40 <= sum(t.cycle_ms for t in rest) / len(rest) <= 150
    channel = load_config(config_dir).channel
    expected = Bus([channel], {}).stats(channel.name).requests  # 函式庫規劃的讀取範圍數
    assert {t.requests for t in timings} == {expected}

    worker.connect_plc()  # 重新連線：第一輪又沒有間隔
    count = len(timings)  # 排到重新連線之前可能還有一輪，等到出現沒有間隔的那一輪
    until(qtbot, lambda: any(t.cycle_ms is None for t in timings[count:]))


# ---- modbus_tags 遷移：寫入非同步後的時序（spec modbus-tags-migration AC-13 ~ AC-26） ----


def set_device_toml(config_dir: Path, old: str, new: str) -> None:
    path = config_dir / "modbus" / "Channel01" / "device.toml"
    text = path.read_bytes().decode("utf-8")
    assert old in text
    path.write_bytes(text.replace(old, new).encode())


def disconnected_while_moving(probe: Probe) -> list[DisconnectedWhileMoving]:
    return [e for e in probe.events if isinstance(e, DisconnectedWhileMoving)]


@pytest.mark.parametrize("interval_ms", [1000, 200])
def test_jog_release_reaches_plc_within_100ms_at_any_interval(
    qtbot, server, make_worker, interval_ms
):
    prepare_servo(server, homed=False)
    _, worker, probe = make_worker(server=server, interval_ms=interval_ms)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.jog_press(2, -1, 10)
    until(qtbot, lambda: writes(server, "JOG Minus 2"), timeout=3000)
    qtbot.wait(interval_ms // 3)  # 落在兩輪讀取之間

    t0 = time.time()
    worker.jog_release(2, -1)
    until(qtbot, lambda: len(writes(server, "JOG Minus 2")) == 2, timeout=1000)
    release = writes(server, "JOG Minus 2")[-1]
    assert release.value is False
    assert release.time - t0 <= 0.1


def test_stop_while_jog_held_sends_release_before_closing(qtbot, server, make_worker):
    prepare_servo(server, homed=False)
    thread, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.jog_press(1, +1, 10)
    until(qtbot, lambda: writes(server, "JOG Plus 1"), timeout=3000)
    # 每個請求延遲 100 ms：一輪讀取比輪詢週期長，通道一直在讀取，寫入要排隊等進行中的讀取，
    # 停止時不能丟掉
    server.set_delay(100)
    qtbot.wait(300)  # 讓延遲生效的那一輪已經開始
    worker.jog_release(1, +1)  # 程式結束時畫面放開 JOG，接著停 worker
    worker.stop()
    assert thread.wait(10000)
    assert writes(server, "JOG Plus 1")[-1].value is False
    with server.lock:
        assert server.model.read("JOG Plus 1") is False


def test_reconnect_while_jog_held_leaves_jog_zero(qtbot, server, make_worker):
    prepare_servo(server, homed=False)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.jog_press(3, +1, 10)
    until(qtbot, lambda: writes(server, "JOG Plus 3"), timeout=3000)
    worker.connect_plc()
    until(qtbot, lambda: probe.connection.count(True) == 2, timeout=5000)
    until(qtbot, lambda: writes(server, "JOG Plus 3")[-1].value is False, timeout=3000)
    with server.lock:
        assert server.model.read("JOG Plus 3") is False
    connects = [e for e in server.log.entries() if e.kind == "connect"]
    disconnects = [e for e in server.log.entries() if e.kind == "disconnect"]
    assert len(connects) == 2 and len(disconnects) == 1  # 只留一條通道


def test_multi_axis_code2_writes_in_order_with_exact_i32(qtbot, server, make_worker):
    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    targets = {1: -1.2345, 4: -12.34, 7: 3.21}  # 倍率 10000、100、100
    raws = {1: -12345, 4: -1234, 7: 321}
    since = len(server.log.entries())
    worker.move(targets, False, 30)
    until(qtbot, lambda: probe.finished(), timeout=15000)
    assert {n: r.kind for n, r in probe.finished()[-1].results.items()} == dict.fromkeys(
        targets, ResultKind.DONE
    )
    seen = [(e.tag, e.value) for e in server.log.entries()[since:] if e.kind == "write"]
    for n in targets:
        assert [w for w in seen if w[0].endswith(f" {n}")][:4] == [
            (f"Target Position {n}", raws[n]),
            (f"Speed {n}", 30),
            (f"PC Action Code {n}", 2),
            (f"Request {n}", True),
        ]
        with server.lock:
            assert server.model.read(f"Current Position {n}") == raws[n]


def test_handshake_completes_when_snapshots_lag_behind_writes(qtbot, server, make_worker):
    prepare_servo(server, homed=False)
    with server.lock:
        server.model.master_write("Find Home Finish 1", True)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    server.set_delay(30)  # 一輪讀取變長：寫入常落在讀取途中，之後的快照早於寫入
    worker.move({1: 0.5}, False, 100)
    until(qtbot, lambda: probe.finished(), timeout=20000)
    assert probe.finished()[-1].results[1].kind == ResultKind.DONE
    worker.home({4})
    until(qtbot, lambda: len(probe.finished()) == 2, timeout=30000)
    assert probe.finished()[-1].results[4].kind == ResultKind.DONE
    assert probe.status.connected


def test_handshake_timeout_fires_while_plc_values_stay_unchanged(
    qtbot, config_dir, server, make_worker
):
    path = config_dir / "app.toml"
    path.write_bytes(path.read_bytes().replace(b"timeout_s = 5", b"timeout_s = 1"))
    prepare_servo(server)
    with server.lock:
        server.model.set_fault(1, no_reply=False, no_copy=True)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.move({1: 1.0}, False, 50)
    until(qtbot, lambda: probe.finished(), timeout=5000)
    result = probe.finished()[-1].results[1]
    assert result.kind == ResultKind.TIMEOUT and result.wait_point == WaitPoint.COPY
    start = [e for e in writes(server, "PC Action Code 1") if e.value == 2][0].time
    until(qtbot, lambda: writes(server, "PC Action Code 1")[-1].value == 0, timeout=3000)
    end = writes(server, "PC Action Code 1")[-1].time
    assert 1.0 <= end - start <= 1.0 + 0.5


def test_disconnect_does_not_auto_reconnect_even_if_device_toml_allows(
    qtbot, config_dir, server, make_worker
):
    set_device_toml(
        config_dir,
        "auto_reconnect = false",
        "auto_reconnect = true\nreconnect_interval_ms = 50",
    )
    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    worker.move({1: 50.0}, False, 1)
    until(qtbot, lambda: probe.snapshots[-1][0] and probe.snapshots[-1][0].axis(1).busy)
    server.disconnect_all()
    wait_connected(qtbot, probe, value=False)
    until(qtbot, lambda: probe.finished(), timeout=3000)
    assert probe.finished()[-1].results[1].kind == ResultKind.COMM_LOST
    qtbot.wait(600)  # 遠多於 reconnect_interval_ms
    assert len([e for e in server.log.entries() if e.kind == "connect"]) == 1
    assert probe.connection == [True, False]

    # 手動重新連線仍做殘留交握與 JOG 殘留檢查
    with server.lock:
        server.model.master_write("JOG Minus 4", True)
        server.model.master_write("Request 2", True)
        server.model.master_write("PC Action Code 2", 2)
    worker.connect_plc()
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots and probe.status.residual, timeout=3000)
    assert [r.axis for r in probe.status.residual] == [2]
    until(qtbot, lambda: writes(server, "JOG Minus 4"), timeout=3000)
    assert writes(server, "JOG Minus 4")[-1].value is False


def test_rejected_speed_while_jog_releases_jog_then_disconnects(
    qtbot, server, make_worker, caplog_info
):
    prepare_servo(server, homed=False)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    server.reject_writes({"Speed 1"})
    worker.jog_press(1, +1, 10)
    wait_connected(qtbot, probe, value=False)
    until(qtbot, lambda: disconnected_while_moving(probe), timeout=3000)
    assert disconnected_while_moving(probe)[-1].axes == (1,)
    jog = writes(server, "JOG Plus 1")
    assert [e.value for e in jog] == [True, False]  # 被拒之後 JOG = 1 仍送到，斷線前寫回 0
    with server.lock:
        assert server.model.read("JOG Plus 1") is False
    disconnect = [e for e in server.log.entries() if e.kind == "disconnect"][0]
    assert jog[-1].time <= disconnect.time
    text = log_text(caplog_info)
    assert "Speed 1" in text and "位址 123" in text
    assert "可能仍在移動" in text


def test_rejected_speed_and_jog_logs_failed_release(qtbot, server, make_worker, caplog_info):
    prepare_servo(server, homed=False)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    server.reject_writes({"Speed 1", "JOG Plus 1"})
    worker.jog_press(1, +1, 10)
    wait_connected(qtbot, probe, value=False)
    until(qtbot, lambda: disconnected_while_moving(probe), timeout=5000)
    text = log_text(caplog_info)
    assert "JOG Plus 1" in text and "寫 0 失敗" in text
    assert "可能仍在移動" in text
    until(qtbot, lambda: probe.snapshots and probe.status.connected is False)


def test_rejected_target_position_never_sends_request(qtbot, server, make_worker):
    prepare_servo(server)
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    wait_ready(qtbot, probe)
    server.reject_writes({"Target Position 1"})
    worker.move({1: 5.0}, False, 50)
    wait_connected(qtbot, probe, value=False)
    until(qtbot, lambda: probe.finished(), timeout=3000)
    assert probe.finished()[-1].results[1].kind == ResultKind.COMM_LOST
    qtbot.wait(300)
    assert not [e for e in writes(server, "Request 1") if e.value is True]


def test_writes_queued_before_failure_are_not_sent_after_reconnect(qtbot, server, make_worker):
    _, worker, probe = make_worker(server=server)
    wait_connected(qtbot, probe)
    until(qtbot, lambda: probe.snapshots and not probe.status.locked, timeout=3000)
    server.set_delay(50)
    server.reject_writes({"Speed 1"})
    worker.write_point("Speed 1", 11)
    worker.write_point("Speed 2", 22)
    worker.write_point("Speed 3", 33)
    wait_connected(qtbot, probe, value=False)
    server.set_delay(0)
    server.reject_writes(())
    mark = len(server.log.entries())
    worker.connect_plc()
    wait_connected(qtbot, probe)
    qtbot.wait(600)
    after = [e.tag for e in server.log.entries()[mark:] if e.kind == "write"]
    assert not {"Speed 1", "Speed 2", "Speed 3"} & set(after)


# ---- 替身 Bus：快照與寫入失敗同時出現（FR-24） ----


class FakeTicket:
    def __init__(self):
        self.error: WriteFailed | None = None
        self.finished = False

    def done(self) -> bool:
        return self.finished

    def wait(self, timeout=None) -> None:
        if self.error is not None:
            raise self.error


class FakeBus:
    """Bus 公開方法的替身：狀態、快照與寫入結果由測試設定（測試執行緒與 worker 執行緒共用）。"""

    def __init__(self, points):
        self.points = points
        self.lock = threading.Lock()
        self.state = DeviceStatus.CONNECTING
        self.snap: LibSnapshot | None = None
        self.tickets: list[FakeTicket] = []
        self.t0 = datetime.now().astimezone()

    def set_round(self, n: int) -> None:
        values = {p.tag: (False if p.data_type == "Bool" else 0) for p in self.points.all()}
        values["Alarm Code 1"] = n  # 用來辨認快照屬於哪一輪
        self.snap = LibSnapshot("PLC", self.t0 + timedelta(milliseconds=200 * n), True, values)

    def start(self):
        pass

    def stop(self):
        pass

    def on_status(self, func):
        return lambda: None

    def status(self, device):
        with self.lock:
            return self.state

    def snapshot(self, device):
        with self.lock:
            return self.snap

    def stats(self, device):
        return PollStats(1.0, 1.0, 1.0, 3, 200)

    def write(self, ref, value):
        ticket = FakeTicket()
        with self.lock:
            self.tickets.append(ticket)
        return ticket


def test_snapshot_of_failed_write_round_is_not_ticked(qtbot, config_dir, monkeypatch):
    config = load_config(config_dir)
    bus = FakeBus(config.points)
    ticked: list[int] = []
    original = MotionEngine.tick

    def tick(self, snapshot):
        ticked.append(snapshot.value("Alarm Code 1"))
        return original(self, snapshot)

    monkeypatch.setattr(MotionEngine, "tick", tick)
    thread, worker = start_worker(config, "127.0.0.1", 502, bus_factory=lambda channel: bus)
    probe = Probe(worker)
    try:
        bus.set_round(1)
        with bus.lock:
            bus.state = DeviceStatus.CONNECTED
        wait_connected(qtbot, probe)
        until(qtbot, lambda: ticked == [1])
        worker.write_point("Speed 1", 10)
        until(qtbot, lambda: bus.tickets)
        with bus.lock:  # 同一輪：寫入失敗，快照也已更新
            bus.tickets[-1].error = WriteFailed("exception response code 4")
            bus.tickets[-1].finished = True
            bus.set_round(2)
        wait_connected(qtbot, probe, value=False)
        qtbot.wait(100)
        assert ticked == [1]
    finally:
        worker.stop()
        assert thread.wait(5000)
