"""程式進入點 plc-motion（spec FR-8 錯誤視窗、FR-14、FR-45 重新啟動、FR-46、FR-48 日誌位置）。

子程序以 `uv run plc-motion --config <tmp>` 執行，視窗以 Win32 API 依標題觀察；
錯誤視窗內容與主題套用在同一程序內呼叫 `plc_motion.app.main()` 觀察。
單一實例的鎖是全機共用：測試期間不能有其他 plc-motion 在執行。
"""

import ctypes
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication

from plc_motion.app import main
from plc_motion.logging_setup import log_file_name
from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.sim import SimServer
from plc_motion.ui.dialogs import ConfigErrorDialog
from plc_motion.ui.main_window import MainWindow
from plc_motion.ui.single_instance import SingleInstance
from plc_motion.ui.theme import load_qss

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="以 Win32 API 觀察視窗")

APP_DIR = Path(__file__).resolve().parents[1]
MAIN_TITLE = "PLC 七軸運動控制"
ERROR_TITLE = "設定檔錯誤"
WM_CLOSE = 0x0010

user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def windows_titled(title: str) -> list[int]:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found


def visible_windows_titled(title: str) -> list[int]:
    return [h for h in windows_titled(title) if user32.IsWindowVisible(h)]


def wait_for(cond, timeout: float = 30.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待逾時")


def launch(*args: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["uv", "run", "plc-motion", *args],
        cwd=APP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(10)


@pytest.fixture
def processes():
    started: list[subprocess.Popen] = []
    yield started
    for proc in started:
        kill_tree(proc)


@pytest.fixture(autouse=True)
def no_running_instance():
    if windows_titled(MAIN_TITLE):
        pytest.skip("已有 plc-motion 在執行，單一實例測試無法進行")


def break_config(config_dir: Path) -> list[str]:
    """製造兩種錯誤：device.toml 缺 host、axes.toml 無法解析。回傳辨識用字串。"""
    device = config_dir / "device.toml"
    text = device.read_bytes().decode("utf-8").replace('host = "192.168.1.105"\n', "")
    device.write_bytes(text.encode("utf-8"))
    (config_dir / "axes.toml").write_bytes(b"[[axes]\nnumber = = 1\n")
    return ["缺少 [connection] host", "axes.toml"]


def log_path(config_dir: Path) -> Path:
    return config_dir.parent / "logs" / log_file_name(date.today())


# ---- 設定錯誤 ----


def test_config_errors_subprocess_shows_window_logs_and_exits_nonzero(config_dir, processes):
    markers = break_config(config_dir)
    proc = launch("--config", str(config_dir))
    processes.append(proc)

    hwnd = wait_for(lambda: visible_windows_titled(ERROR_TITLE))[0]
    assert not windows_titled(MAIN_TITLE)  # 主視窗沒有開
    text = log_path(config_dir).read_bytes().decode("utf-8")
    assert all(m in text for m in markers), text  # 日誌在 --config 資料夾同層的 logs

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    assert proc.wait(15) != 0


def test_config_error_dialog_lists_every_error(qtbot, config_dir):
    markers = break_config(config_dir)
    seen: list[str] = []

    def answer():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, ConfigErrorDialog):
            seen.append(dialog.text())
            dialog.reject()
        else:
            QTimer.singleShot(20, answer)

    QTimer.singleShot(0, answer)
    code = main(["--config", str(config_dir)])
    assert code != 0
    assert seen and all(m in seen[0] for m in markers)


# ---- 正常啟動 ----


def run_main_and_inspect(config_dir: Path, inspect, extra_args=()) -> int:
    """在本程序執行 main()；主視窗出現後呼叫 inspect(window)，再走結束流程。"""

    def poll():
        windows = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]
        visible = [w for w in windows if w.isVisible()]
        if visible:
            inspect(visible[0])
            visible[0].request_quit()
        else:
            QTimer.singleShot(20, poll)

    QTimer.singleShot(0, poll)
    args = ["--config", str(config_dir), "--host", "127.0.0.1", "--port", str(free_port())]
    return main([*args, *extra_args])


def test_dark_theme_is_applied_on_start(qtbot, config_dir):
    app = QApplication.instance()
    original = app.styleSheet()
    try:
        seen: list[str] = []
        code = run_main_and_inspect(config_dir, lambda w: seen.append(app.styleSheet()))
        assert code == 0
        assert seen == [load_qss()]
    finally:
        app.setStyleSheet(original)


def test_normal_start_writes_log_next_to_config(qtbot, config_dir):
    code = run_main_and_inspect(config_dir, lambda w: None)
    assert code == 0
    assert "程式啟動" in log_path(config_dir).read_bytes().decode("utf-8")


def test_host_and_port_override_without_touching_device_toml(config_dir, processes):
    points = load_config(config_dir).points
    server = SimServer(PlcModel(points), points, port=0)
    server.start()
    try:
        before = (config_dir / "device.toml").read_bytes()
        proc = launch(
            "--config", str(config_dir), "--host", "127.0.0.1", "--port", str(server.port)
        )
        processes.append(proc)
        wait_for(lambda: any(e.kind == "connect" for e in server.log.entries()))
        wait_for(lambda: server.log.read_count() > 0)
        assert (config_dir / "device.toml").read_bytes() == before
        assert [e for e in server.log.entries() if e.kind == "write"] == []  # 啟動不寫任何點
    finally:
        server.stop()


# ---- 單一實例 ----


def test_second_process_raises_first_window_and_exits(config_dir, processes):
    args = ("--config", str(config_dir), "--host", "127.0.0.1", "--port", str(free_port()))
    first = launch(*args)
    processes.append(first)
    hwnd = wait_for(lambda: visible_windows_titled(MAIN_TITLE))[0]

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # 關閉鈕：縮到系統匣
    wait_for(lambda: not user32.IsWindowVisible(hwnd), timeout=10)
    assert first.poll() is None

    second = launch(*args)
    processes.append(second)
    assert second.wait(30) == 0
    wait_for(lambda: user32.IsWindowVisible(hwnd), timeout=10)
    assert visible_windows_titled(MAIN_TITLE) == [hwnd]  # 沒有第二個主視窗
    assert first.poll() is None
    assert "另一個程序啟動" in log_path(config_dir).read_bytes().decode("utf-8")


def test_single_instance_lock_in_process(qtbot):
    key = f"plc-motion-test-{time.time_ns()}"
    first, second = SingleInstance(key), SingleInstance(key)
    try:
        assert first.acquire()
        assert not second.acquire()  # 同一程序內通知寫不出去也不能卡住
    finally:
        first.release()
    third = SingleInstance(key)
    try:
        assert third.acquire()  # 第一份放開後可再取得
    finally:
        third.release()


def test_primary_emits_activate_on_message(qtbot):
    key = f"plc-motion-test-{time.time_ns()}"
    primary = SingleInstance(key)
    assert primary.acquire()
    try:
        client = QLocalSocket()
        with qtbot.waitSignal(primary.activate_requested, timeout=3000):
            client.connectToServer(key)
            assert client.waitForConnected(1000)
            client.write(b"activate\n")
            client.flush()
        client.disconnectFromServer()
    finally:
        primary.release()
