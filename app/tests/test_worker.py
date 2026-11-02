"""PlcWorker：背景執行緒內的連線、輪詢、命令轉派與日誌（spec FR-10 ~ FR-14、FR-27、FR-49、FR-50）。

PLC 端為 SimServer（真 TCP、localhost）；斷言一律經由 worker 的 signal、SimServer 的紀錄與日誌。
"""

import logging
import socket
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from plc_motion.engine import MotionEngine, OperationFinished, ResultKind
from plc_motion.logging_setup import close_logging, log_file_name, setup_logging
from plc_motion.plc import PlcModel
from plc_motion.plc.modbus_link import ModbusLink
from plc_motion.settings import load_config
from plc_motion.sim import SimServer
from plc_motion.worker import EngineStatus, start_worker

WORKER_LOGGER = "plc_motion"


def set_poll_interval(config_dir: Path, interval_ms: int) -> None:
    path = config_dir / "device.toml"
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

    for name in ("connect", "read_all", "write", "close"):
        spy(ModbusLink, name)
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
    assert {"connect", "read_all", "write", "close", "tick", "on_connected"} <= names
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
    before = (config_dir / "device.toml").read_bytes()
    _, worker, probe = make_worker(host="127.0.0.1", server=server)
    wait_connected(qtbot, probe)
    assert (config_dir / "device.toml").read_bytes() == before
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
    text = log_text(caplog_info)
    assert "Alarm Code 3" in text
    assert "位址 40" in text
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
    original = ModbusLink.connect
    raised = []

    def connect(self):
        if not raised:
            raised.append(True)
            raise OSError("測試用的連線例外")
        return original(self)

    monkeypatch.setattr(ModbusLink, "connect", connect)
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
    from plc_motion.plc.batching import plan_reads

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
    expected = len(plan_reads(load_config(config_dir).points.all()))
    assert {t.requests for t in timings} == {expected}

    worker.connect_plc()  # 重新連線：第一輪又沒有間隔
    count = len(timings)  # 排到重新連線之前可能還有一輪，等到出現沒有間隔的那一輪
    until(qtbot, lambda: any(t.cycle_ms is None for t in timings[count:]))
