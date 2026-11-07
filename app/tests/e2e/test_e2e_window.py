"""端到端：設定錯誤、主視窗與系統匣、主題、單一實例、畫面不凍結、日誌、打包
（spec AC-2、AC-30 ~ AC-36）。

「重新啟動」需要走進入點的（AC-2 錯誤視窗與結束碼、AC-32 主題、AC-35 舊日誌清理）在本程序
呼叫 `plc_motion.app.main()`；AC-33 以兩個真實程序驗證；AC-36 執行打包產物（`-m slow`）。
設定一律為 tmp 的 config 複本（`--config`），日誌在其同層的 `logs`（即 `e2e.logs_dir`）。
"""

import csv
import ctypes
import io
import os
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import date, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from plc_motion.app import main
from plc_motion.engine import ResultKind
from plc_motion.logging_setup import log_file_name
from plc_motion.settings.csvio import BOM
from plc_motion.ui.dialogs import ConfigErrorDialog
from plc_motion.ui.main_window import MainWindow
from plc_motion.ui.theme import load_qss
from plc_motion.ui.tray import STATE_COLORS, TrayState

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform != "win32", reason="以 Win32 API 觀察視窗"),
]

APP_DIR = Path(__file__).resolve().parents[2]
MAIN_TITLE = "PLC 七軸運動控制"
WM_CLOSE, WM_SYSCOMMAND, SC_MINIMIZE = 0x0010, 0x0112, 0xF020
user32 = ctypes.windll.user32 if sys.platform == "win32" else None


# ---- Win32 與子程序 ----


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def other_process_windows(title: str, visible_only: bool = True) -> list[int]:
    """其他程序中標題為 title 的頂層視窗（排除本測試程序自己建的主視窗）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title and window_pid(hwnd) != os.getpid():
            if not visible_only or user32.IsWindowVisible(hwnd):
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found


def wait_for(cond, timeout: float = 30.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待逾時")


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        proc.wait(10)


@pytest.fixture
def processes():
    started: list[subprocess.Popen] = []
    yield started
    for proc in started:
        kill_tree(proc)


@pytest.fixture
def no_running_instance():
    if other_process_windows(MAIN_TITLE, visible_only=False):
        pytest.skip("已有 plc-motion 在執行，單一實例的鎖是全機共用")


# ---- 本程序內執行進入點 ----


def run_main(args: list[str], inspect=None) -> int:
    """呼叫 `main(args)`：主視窗出現後呼叫 inspect(window) 再走結束流程。"""

    def poll():
        visible = [
            w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()
        ]
        if visible:
            if inspect is not None:
                inspect(visible[0])
            visible[0].request_quit()
        else:
            QTimer.singleShot(20, poll)

    QTimer.singleShot(0, poll)
    return main(args)


def run_main_with_config_errors(config_dir: Path) -> tuple[int, list[str]]:
    """以壞掉的設定啟動：錯誤視窗出現時記下內容並關閉。回傳 (結束碼, 視窗內容)。"""
    seen: list[str] = []
    deadline = time.monotonic() + 20

    def answer():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, ConfigErrorDialog):
            seen.append(dialog.text())
            dialog.reject()
        elif time.monotonic() < deadline:
            QTimer.singleShot(20, answer)

    QTimer.singleShot(0, answer)
    code = main(["--config", str(config_dir), "--host", "127.0.0.1", "--port", str(free_port())])
    return code, seen


@pytest.fixture
def restore_theme():
    app = QApplication.instance()
    sheet = app.styleSheet()
    yield
    app.setStyleSheet(sheet)


# ---- AC-2（spec FR-11）：設定錯誤 ----

CHANNEL = "modbus/Channel01"


def edit_io(config_dir: Path, tag: str, column: str, value: str | None) -> None:
    """改 io_points.csv 某點的某欄；value 為 None 時刪掉該列。"""
    path = config_dir / CHANNEL / "io_points.csv"
    records = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))
    col = records[0].index(column)
    out = []
    for r in records:
        if r and r[0] == tag:
            if value is None:
                continue
            r[col] = value
        out.append(r)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(out)
    path.write_bytes(BOM + buf.getvalue().encode("utf-8"))


def add_io(config_dir: Path, row: list[str]) -> None:
    path = config_dir / CHANNEL / "io_points.csv"
    text = path.read_bytes().decode("utf-8-sig")
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(row)
    path.write_bytes(BOM + (text + buf.getvalue()).encode("utf-8"))


def replace_text(config_dir: Path, name: str, old: str, new: str, count: int = 1) -> None:
    path = config_dir / name
    text = path.read_bytes().decode("utf-8-sig")
    assert old in text, (name, old)
    path.write_bytes(text.replace(old, new, count).encode("utf-8"))


def write_bytes(config_dir: Path, name: str, data: bytes) -> None:
    (config_dir / name).write_bytes(data)


def coil_row(tag: str, address: str, access: str = "Read Write") -> list[str]:
    return [tag, "", address, "1", "0", "Coil", "Bool", access, "O", "", "", "Unchecked"]


def positions_150_rows(config_dir: Path) -> None:
    path = config_dir / "positions.csv"
    lines = path.read_bytes().decode("utf-8-sig").splitlines(keepends=True)
    path.write_bytes(BOM + "".join(lines[:151]).encode("utf-8"))


def make_rtu(config_dir: Path) -> None:
    path = config_dir / CHANNEL / "device.toml"
    text = path.read_bytes().decode("utf-8")
    start, end = text.index("[connection]"), text.index("[protocol]")
    rtu = '[connection]\ntype = "rtu"\n\n[connection.rtu]\nport = "COM3"\nbaudrate = 9600\n'
    rtu += 'parity = "N"\nstopbits = 1\n\n'
    path.write_bytes((text[:start] + rtu + text[end:]).encode("utf-8"))


def remove_column(config_dir: Path, column: str) -> None:
    path = config_dir / CHANNEL / "io_points.csv"
    records = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))
    col = records[0].index(column)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows([r[:col] + r[col + 1 :] for r in records])
    path.write_bytes(BOM + buf.getvalue().encode("utf-8"))


DEVICE = f"{CHANNEL}/device.toml"
IO = f"{CHANNEL}/io_points.csv"
RANGES = f"{CHANNEL}/read_ranges.toml"

# (編號, 製造錯誤, 錯誤視窗應含的字串)；FR-11 第一層
CONFIG_ERRORS_FIRST = [
    # modbus_tags 回報的設定錯誤
    ("缺 device.toml", lambda d: (d / DEVICE).unlink(), ["device.toml", "missing"]),
    ("缺 io_points.csv", lambda d: (d / IO).unlink(), ["io_points.csv"]),
    (
        "device.toml 無法解析",
        lambda d: write_bytes(d, DEVICE, b"[connection\nhost = \n"),
        ["device.toml", "TOML syntax error"],
    ),
    (
        "device.toml 舊格式的 [handshake]",
        lambda d: replace_text(d, DEVICE, "[polling]", "[handshake]\ntimeout_s = 5\n\n[polling]"),
        ["device.toml", "handshake", "unknown key"],
    ),
    (
        "io_points.csv 無法解析",
        lambda d: write_bytes(d, IO, b"\xff\xfe\x00"),
        ["io_points.csv"],
    ),
    (
        "Tag Name 重複",
        lambda d: add_io(d, coil_row("Ready 1", "60")),
        ["'Ready 1'", "already used"],
    ),
    (
        "Data Model 打錯",
        lambda d: edit_io(d, "Busy 2", "Data Model", "Coils"),
        ["io_points.csv", "'Coils'"],
    ),
    (
        "Data Type 打錯",
        lambda d: edit_io(d, "Busy 2", "Data Type", "Boolean"),
        ["io_points.csv", "'Boolean'"],
    ),
    (
        "Access 打錯",
        lambda d: edit_io(d, "Busy 2", "Access", "RW"),
        ["io_points.csv", "'RW'"],
    ),
    (
        "Attributes 語法",
        lambda d: edit_io(d, "Positive Limit 1", "Attributes", "ON"),
        ["io_points.csv", "'ON'"],
    ),
    (
        "讀取範圍未涵蓋",
        lambda d: add_io(d, coil_row("Cylinder1 Up", "300")),
        ["read_ranges.toml", "'Cylinder1 Up'"],
    ),
    # app 的檔案
    ("缺 app.toml", lambda d: (d / "app.toml").unlink(), ["缺少設定檔 app.toml"]),
    ("缺 axes.toml", lambda d: (d / "axes.toml").unlink(), ["缺少設定檔 axes.toml"]),
    (
        "app.toml 無法解析",
        lambda d: write_bytes(d, "app.toml", b"[handshake\ntimeout_s = \n"),
        ["app.toml：格式無法解析"],
    ),
    (
        "axes.toml 無法解析",
        lambda d: write_bytes(d, "axes.toml", b"[[axes]\nnumber = = 1\n"),
        ["axes.toml：格式無法解析"],
    ),
    (
        "交握逾時",
        lambda d: replace_text(d, "app.toml", "timeout_s = 5", "timeout_s = 0"),
        ["app.toml", "timeout_s = 0", "須為正數"],
    ),
    (
        "脈衝",
        lambda d: replace_text(
            d, "app.toml", "reset_alarm_pulse_ms = 500", "reset_alarm_pulse_ms = 0.5"
        ),
        ["app.toml", "reset_alarm_pulse_ms = 0.5", "須為正整數"],
    ),
    (
        "日誌保留天數",
        lambda d: replace_text(d, "app.toml", "retention_days = 30", "retention_days = 0"),
        ["app.toml", "retention_days = 0", "須為正整數"],
    ),
    ("positions.csv 不是 200 列", positions_150_rows, ["positions.csv", "200 列", "150 列"]),
    (
        "軟體極限",
        lambda d: replace_text(d, "axes.toml", "soft_limit_max = 100.0", "soft_limit_max = -100.0"),
        ["軟體極限最小值 -100.0 不小於最大值 -100.0"],
    ),
    (
        "步距",
        lambda d: replace_text(d, "axes.toml", "step_short = ", "step_short = -", 1),
        ["step_short", "須為正數"],
    ),
    (
        "預設速度",
        lambda d: replace_text(d, "axes.toml", "default_speed = 50", "default_speed = 101"),
        ["default_speed = 101", "1 ~ 100"],
    ),
    # 通道資料夾
    (
        "Channel01 以外的資料夾",
        lambda d: shutil.copytree(d / CHANNEL, d / "modbus" / "Channel02"),
        ["Channel01 以外的資料夾", "Channel02"],
    ),
    (
        "缺 Channel01",
        lambda d: (d / CHANNEL).rename(d / "modbus" / "PLC"),
        ["缺少通道資料夾 modbus/Channel01"],
    ),
    (
        "Channel01 停用",
        lambda d: replace_text(d, DEVICE, "enabled = true", "enabled = false"),
        ["Channel01 被停用"],
    ),
    ("Channel01 不是 TCP", make_rtu, ["Channel01 須為 TCP", "'rtu'"]),
]

# FR-11 第二層
CONFIG_ERRORS_SECOND = [
    (
        "Visible 欄位缺少",
        lambda d: remove_column(d, "Visible"),
        ["io_points.csv：表頭缺少欄位 Visible"],
    ),
    (
        "Visible 列舉",
        lambda d: edit_io(d, "Busy 2", "Visible", "Y"),
        ["Busy 2", "Visible「Y」"],
    ),
    (
        "Check Status 列舉",
        lambda d: edit_io(d, "Busy 2", "Check Status", "Done"),
        ["Busy 2", "Check Status「Done」"],
    ),
    (
        "缺必要 Tag",
        lambda d: edit_io(d, "Reply 3", "Tag Name", None),
        ["缺少必要 Tag「Reply 3」"],
    ),
    (
        "型態表 Data Model",
        lambda d: (
            edit_io(d, "Home Sensor 1", "Data Model", "Coil"),
            replace_text(d, RANGES, '"20~84, 120~184"', '"20~84, 120~184, 24593"'),
        ),
        ["Home Sensor 1", "Data Model 應為「Discrete Input」，實際為「Coil」"],
    ),
    (
        "型態表 Data Type",
        lambda d: edit_io(d, "Alarm Code 1", "Data Type", "I16"),
        ["Alarm Code 1", "Data Type 應為「U16」，實際為「I16」"],
    ),
    (
        "型態表 Access",
        lambda d: edit_io(d, "Busy 1", "Access", "Read Write"),
        ["Busy 1", "Access 應為「Read Only」，實際為「Read Write」"],
    ),
    (
        "型態表 Address Length",
        lambda d: (
            edit_io(d, "Speed 1", "Data Type", "I32"),
            edit_io(d, "Speed 1", "Address Length", "2"),
        ),
        ["Speed 1", "Address Length 應為「1」，實際為「2」"],
    ),
    (
        "Position 的 word_order",
        lambda d: edit_io(
            d, "Current Position 1", "Attributes", "scale=10000;unit=mm;word_order=high_first"
        ),
        ["Current Position 1", "Attributes 不可設定 word_order=high_first"],
    ),
    (
        "Position 缺倍率",
        lambda d: edit_io(d, "Current Position 2", "Attributes", "unit=mm"),
        ["Current Position 2", "Attributes 缺少 scale"],
    ),
    (
        "倍率不是整數",
        lambda d: (
            edit_io(d, "Current Position 3", "Attributes", "scale=0.5;unit=mm"),
            edit_io(d, "Target Position 3", "Attributes", "scale=0.5;unit=mm"),
        ),
        ["Current Position 3", "倍率須為正整數"],
    ),
    (
        "非感測點設 invert",
        lambda d: edit_io(d, "Ready 1", "Attributes", "invert=true"),
        ["Ready 1", "Attributes 不可設定 invert=true"],
    ),
    (
        "Speed 設 scale",
        lambda d: edit_io(d, "Speed 2", "Attributes", "scale=10"),
        ["Speed 2", "Attributes 不可設定 scale=10"],
    ),
    (
        "倍率不一致",
        lambda d: edit_io(d, "Target Position 1", "Attributes", "scale=100;unit=mm"),
        ["Current Position 1", "Target Position 1", "倍率或單位不一致"],
    ),
    (
        "位址重疊",
        lambda d: add_io(d, coil_row("Cylinder1 Up", "20")),
        ["位址重疊", "Ready 1", "Cylinder1 Up"],
    ),
]


@pytest.mark.parametrize(
    ("make_error", "expected"),
    [
        pytest.param(make, expected, id=name)
        for name, make, expected in CONFIG_ERRORS_FIRST + CONFIG_ERRORS_SECOND
    ],
)
def test_ac2_each_config_error_is_listed_and_exits_nonzero(
    qtbot, e2e_config_dir, no_running_instance, make_error, expected
):
    make_error(e2e_config_dir)
    code, seen = run_main_with_config_errors(e2e_config_dir)
    assert code != 0
    assert len(seen) == 1, "錯誤視窗沒有出現"
    assert all(text in seen[0] for text in expected), seen[0]
    assert not [
        w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow) and w.isVisible()
    ]


@pytest.mark.parametrize(
    ("make_errors", "expected"),
    [
        pytest.param(
            (
                lambda d: (d / "axes.toml").unlink(),
                lambda d: edit_io(d, "Busy 2", "Data Model", "Coils"),
            ),
            ["缺少設定檔 axes.toml", "'Coils'"],
            id="第一層：axes.toml 缺少與 Data Model 打錯",
        ),
        pytest.param(
            (make_rtu, lambda d: edit_io(d, "Busy 2", "Data Model", "Coils")),
            ["Channel01 須為 TCP", "'Coils'"],
            id="第一層：rtu 與 Data Model 打錯",
        ),
        pytest.param(
            (
                lambda d: edit_io(d, "Speed 1", "Tag Name", None),
                lambda d: edit_io(d, "Busy 4", "Visible", "Z"),
            ),
            ["缺少必要 Tag「Speed 1」", "Busy 4", "Visible「Z」"],
            id="第二層：Speed 1 缺少與 Visible 值錯誤",
        ),
    ],
)
def test_ac2_two_errors_in_same_layer_are_both_listed(
    qtbot, e2e_config_dir, no_running_instance, make_errors, expected
):
    for make in make_errors:
        make(e2e_config_dir)
    code, seen = run_main_with_config_errors(e2e_config_dir)
    assert code != 0
    assert all(text in seen[0] for text in expected), seen[0]


def test_ac2_second_layer_is_listed_after_first_layer_is_fixed(
    qtbot, e2e_config_dir, no_running_instance
):
    axes = e2e_config_dir / "axes.toml"
    good = axes.read_bytes()
    axes.unlink()
    edit_io(e2e_config_dir, "Speed 1", "Tag Name", None)

    code, seen = run_main_with_config_errors(e2e_config_dir)
    assert code != 0
    assert "缺少設定檔 axes.toml" in seen[0] and "Speed 1" not in seen[0]

    axes.write_bytes(good)
    code, seen = run_main_with_config_errors(e2e_config_dir)
    assert code != 0
    assert "缺少必要 Tag「Speed 1」" in seen[0] and "axes.toml" not in seen[0]


def test_ac2_two_errors_are_both_listed_in_real_process(
    e2e_config_dir, no_running_instance, processes
):
    edit_io(e2e_config_dir, "Busy 2", "Data Model", "Coils")
    replace_text(e2e_config_dir, "axes.toml", "default_speed = 50", "default_speed = 101")
    code, seen = run_main_with_config_errors(e2e_config_dir)
    assert code != 0
    assert "'Coils'" in seen[0] and "default_speed = 101" in seen[0]
    assert "2 項錯誤" in seen[0]

    # 真實程序：錯誤視窗出現、主視窗沒開，關閉後結束碼非 0
    proc = subprocess.Popen(
        ["uv", "run", "plc-motion", "--config", str(e2e_config_dir)],
        cwd=APP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    processes.append(proc)
    hwnd = wait_for(lambda: other_process_windows("設定檔錯誤"))[0]
    assert not other_process_windows(MAIN_TITLE)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    assert proc.wait(15) != 0


# ---- AC-30、AC-31：系統匣 ----


def started(e2e) -> None:
    e2e.prepare()
    e2e.start()
    e2e.wait_unlocked()


def hwnd_of(window) -> int:
    return int(window.winId())


def double_click_tray(app) -> None:
    """系統匣圖示雙擊：Windows 送給 QSystemTrayIcon 的 activated(DoubleClick)。"""
    app.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)


def assert_polling_continues(e2e, seconds: float = 1.0) -> None:
    before = e2e.sim.log.read_count()
    e2e.settle(int(seconds * 1000))
    assert e2e.sim.log.read_count() >= before + 3  # 200 ms 週期：1 秒約 5 次


def test_ac30_minimize_and_close_go_to_tray_polling_continues(e2e):
    started(e2e)
    app = e2e.app
    window = app.window
    for message, wparam in ((WM_SYSCOMMAND, SC_MINIMIZE), (WM_CLOSE, 0)):  # 最小化鈕、關閉鈕
        user32.PostMessageW(hwnd_of(window), message, wparam, 0)
        e2e.until(lambda: not window.isVisible())
        e2e.settle(300)
        assert not window.isVisible()
        assert not user32.IsWindowVisible(hwnd_of(window))
        assert app.tray.icon.isVisible()
        assert_polling_continues(e2e)
        assert not app.thread.isFinished()

        double_click_tray(app)
        e2e.until(window.isVisible)
        assert not window.isMinimized()


def test_ac30_tray_quit_while_moving_asks_first(e2e):
    started(e2e)
    app = e2e.app
    ev = len(e2e.events)
    e2e.move({1: 20.0}, speed=10)
    e2e.until(lambda: app.snapshot.axis(1).busy)

    texts: list[str] = []

    def answer(button):
        def handle(box: QMessageBox) -> None:
            texts.append(box.text())
            box.button(button).click()

        e2e.answer_modal(QMessageBox, handle, timeout_ms=5000)

    answer(QMessageBox.StandardButton.No)
    app.tray.action_quit.trigger()
    assert len(texts) == 1 and "第 1 軸動作中" in texts[0]
    e2e.settle(500)
    assert not app.thread.isFinished()  # 取消：程式繼續、仍在輪詢
    assert_polling_continues(e2e, 0.6)
    assert e2e.finished(ev) == []

    answer(QMessageBox.StandardButton.Yes)
    since = e2e.mark()
    app.tray.action_quit.trigger()
    assert len(texts) == 2
    e2e.until(app.thread.isFinished)
    assert not app.tray.icon.isVisible()
    assert e2e.writes(since) == []  # 結束不寫入任何點


def test_ac31_alarm_while_in_tray_notifies_and_turns_icon_fault(e2e):
    started(e2e)
    app = e2e.app
    notices: list[tuple[str, str]] = []
    original = app.tray.icon.showMessage

    def spy(title, text, *args):
        notices.append((title, text))
        return original(title, text, *args)

    app.tray.icon.showMessage = spy  # 觀測真實系統匣圖示的通知呼叫，照常送出
    user32.PostMessageW(hwnd_of(app.window), WM_CLOSE, 0, 0)
    e2e.until(lambda: not app.window.isVisible())
    assert app.tray.state == TrayState.NORMAL

    e2e.sim_call("inject_alarm", 3, 5, False)
    e2e.until(lambda: app.tray.state == TrayState.FAULT)
    e2e.until(lambda: notices)
    assert notices == [("Alarm 發生", "第 3 軸 Alarm Code = 5")]
    assert QSystemTrayIcon.supportsMessages()
    pixel = app.tray.icon.icon().pixmap(32, 32).toImage().pixelColor(12, 16)  # 中心狀態點
    assert pixel.name() == STATE_COLORS[TrayState.FAULT]
    assert "異常" in app.tray.icon.toolTip()
    assert not app.window.isVisible()


# ---- AC-32：主題 ----


def test_ac32_dark_only_and_legacy_light_setting_ignored(
    e2e, e2e_config_dir, no_running_instance, restore_theme
):
    """只有深色主題：畫面上沒有主題切換；app.toml 寫了 [ui] theme = "light" 也照常以深色啟動。"""
    path = e2e_config_dir / "app.toml"
    text = path.read_bytes().decode("utf-8")
    path.write_bytes((text.rstrip() + '\n\n[ui]\ntheme = "light"\n').encode("utf-8"))
    qapp = QApplication.instance()
    qapp.setStyleSheet("")
    seen: list[tuple[str, bool]] = []
    code = run_main(
        ["--config", str(e2e_config_dir), "--host", "127.0.0.1", "--port", str(e2e.sim.port)],
        lambda w: seen.append((qapp.styleSheet(), hasattr(w, "theme_button"))),
    )
    assert code == 0
    assert seen == [(load_qss(), False)]


# ---- AC-33：單一實例（兩個真實程序） ----


def test_ac33_second_launch_raises_existing_window_to_foreground(
    e2e_config_dir, no_running_instance, processes
):
    args = ["--config", str(e2e_config_dir), "--host", "127.0.0.1", "--port", str(free_port())]
    first = subprocess.Popen(["uv", "run", "plc-motion", *args], cwd=APP_DIR)
    processes.append(first)
    hwnd = wait_for(lambda: other_process_windows(MAIN_TITLE))[0]
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # 縮到系統匣：不在前景
    wait_for(lambda: not user32.IsWindowVisible(hwnd), timeout=10)
    assert user32.GetForegroundWindow() != hwnd

    second = subprocess.Popen(["uv", "run", "plc-motion", *args], cwd=APP_DIR)
    processes.append(second)
    assert second.wait(30) == 0  # 新的那份結束
    wait_for(lambda: user32.IsWindowVisible(hwnd), timeout=10)
    assert other_process_windows(MAIN_TITLE) == [hwnd]  # 沒有第二個主視窗
    wait_for(lambda: user32.GetForegroundWindow() == hwnd, timeout=5)
    assert first.poll() is None


# ---- AC-34：畫面不凍結 ----


def test_ac34_drag_and_switch_pages_during_handshake_wait_stay_responsive(e2e):
    started(e2e)
    app = e2e.app
    window = app.window
    e2e.sim_call("set_fault", 1, True, False)  # 不回 Reply：交握等待 5 秒
    ev, since = len(e2e.events), e2e.mark()
    e2e.move({1: 10.0})
    e2e.wait_entry("write", "Request 1", True, since)

    ticks: list[float] = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    first_sample = len(e2e.samples)
    pages = []
    origin = window.pos()
    begin = time.monotonic()
    step = 0
    while time.monotonic() - begin < 3.0:  # 交握等待中拖動視窗、切換頁面
        step += 1
        window.move(origin.x() + (step % 40) * 4, origin.y() + (step % 20) * 2)
        if step % 10 == 0:
            e2e.click(window.nav.buttons[(step // 10) % 2])
            pages.append(window.pages.currentIndex())
        e2e.settle(20)
    timer.stop()
    window.move(origin)

    assert not e2e.finished(ev)  # 整段都在交握等待期間
    assert {0, 1} <= set(pages)
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) > 100 and max(gaps) < 0.2, max(gaps)
    sample_times = [s.time for s in e2e.samples[first_sample:]]
    assert len(sample_times) >= 10  # 輪詢結果持續送到畫面
    assert max(b - a for a, b in zip(sample_times, sample_times[1:], strict=False)) < 0.6

    finished = e2e.wait_finished(ev, 15000)
    assert finished.results[1].kind == ResultKind.TIMEOUT
    e2e.wait_unlocked()


# ---- AC-35：日誌 ----


def test_ac35_log_has_operations_and_alarm_and_old_log_is_removed(
    e2e, e2e_config_dir, no_running_instance
):
    started(e2e)
    assert e2e.logs_dir == e2e_config_dir.parent / "logs"  # 與 config 同層
    ev = len(e2e.events)
    e2e.move({1: 5.0, 2: 10.0, 3: 15.0})  # AC-8
    e2e.wait_finished(ev, 30000)
    e2e.wait_unlocked()

    ev, since = len(e2e.events), e2e.mark()
    alarm = e2e.when(
        e2e.logged("state", "Busy 2", True, since),
        lambda: (time.sleep(0.2), e2e.sim_call("inject_alarm", 2, 2, False)),
    )
    e2e.move({1: 20.0, 2: 20.0})  # AC-12
    alarm.check()
    e2e.wait_finished(ev, 30000)

    text = e2e.log_text()
    assert (
        "操作開始：移動，絕對，第 1 軸 輸入 5.0000 mm → 目標 5.0000 mm、"
        "第 2 軸 輸入 10.0000 mm → 目標 10.0000 mm、第 3 軸 輸入 15.0000 mm → 目標 15.0000 mm，"
        "速度 50"
    ) in text
    assert "操作結束：移動，第 1 軸：完成、第 2 軸：完成、第 3 軸：完成" in text
    assert (
        "操作開始：移動，絕對，第 1 軸 輸入 20.0000 mm → 目標 20.0000 mm、"
        "第 2 軸 輸入 20.0000 mm → 目標 20.0000 mm，速度 50"
    ) in text
    assert "操作結束：移動，第 1 軸：完成、第 2 軸：Alarm（2）" in text
    assert "Alarm Code 2：0 → 2" in text
    e2e.close_all()

    old = e2e.logs_dir / log_file_name(date.today() - timedelta(days=31))
    recent = e2e.logs_dir / log_file_name(date.today() - timedelta(days=29))
    for path in (old, recent):
        path.write_text("舊日誌\n", encoding="utf-8")
    code = run_main(
        ["--config", str(e2e_config_dir), "--host", "127.0.0.1", "--port", str(e2e.sim.port)]
    )
    assert code == 0
    assert not old.exists()  # 超過保留天數 30 天
    assert recent.exists()
    assert "Alarm Code 2：0 → 2" in e2e.log_text()  # 當日檔保留、續寫


# ---- AC-36：打包產物（slow） ----

DIST = APP_DIR / "dist" / "plc-motion"
_SOURCES = ("src", "config", "packaging", "pyproject.toml", "uv.lock")


def dist_is_fresh() -> bool:
    exe = DIST / "plc-motion.exe"
    if not exe.is_file():
        return False
    built = exe.stat().st_mtime
    for name in _SOURCES:
        root = APP_DIR / name
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        if any(p.stat().st_mtime > built for p in files):
            return False
    return True


@pytest.fixture(scope="module")
def dist() -> Path:
    if not dist_is_fresh():
        subprocess.run(
            [sys.executable, str(APP_DIR / "packaging" / "build.py")],
            cwd=APP_DIR,
            check=True,
            timeout=900,
        )
    return DIST


UIA_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                     [System.Windows.Automation.Condition]::TrueCondition)
foreach ($e in $all) {{ $e.Current.Name }}
"""


def window_texts(hwnd: int) -> list[str]:
    """以 Windows UI Automation 讀出視窗內所有控制項的名稱（Qt 以 accessibility 提供文字）。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", UIA_SCRIPT.format(hwnd=hwnd)],
        capture_output=True,
        timeout=60,
    )
    return [line.strip() for line in out.stdout.decode("utf-8", "replace").splitlines()]


@pytest.mark.slow
def test_ac36_copied_package_runs_without_uv_and_mirrors_sim(
    dist, tmp_path, no_running_instance, processes
):
    from plc_motion.plc import PlcModel
    from plc_motion.settings import load_config
    from plc_motion.sim import SimServer

    copy = tmp_path / "另一個路徑" / "plc-motion"
    shutil.copytree(dist, copy)
    assert (copy / "THIRD_PARTY_NOTICES.md").is_file()
    assert (copy / "licenses" / "LGPL-3.0.txt").is_file()
    assert list(copy.rglob("Qt6Core.dll")) and list(copy.rglob("Qt6Widgets.dll"))
    assert not [p for p in copy.rglob("*") if "plc-sim" in p.name.lower() or p.name == "sim"]

    config = load_config(copy / "config")
    server = SimServer(
        PlcModel(config.points), config.points, "127.0.0.1", 5020, config.read_ranges
    )
    server.start()
    try:
        with server.lock:
            m = server.model
            for n in (1, 2, 3, 7):
                m.master_write(f"Servo Motor {n}", True)
            for n, value in {1: 12.3456, 2: -7.5, 4: 4.25, 7: 55.55}.items():
                m.set_position(n, value)
            m.set_limit(2, "pos", True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "UV_"))}
        env["PATH"] = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32"  # 不經 uv／Python
        proc = subprocess.Popen(
            [str(copy / "plc-motion.exe"), "--host", "127.0.0.1", "--port", "5020"],
            cwd=tmp_path,
            env=env,
        )
        processes.append(proc)
        hwnd = wait_for(lambda: other_process_windows(MAIN_TITLE), timeout=60)[0]
        wait_for(lambda: server.log.read_count() > 3)
        expected = ["已連線 127.0.0.1:5020", "12.3456", "-7.5000", "4.25", "55.55"]
        texts = wait_for(
            lambda: (t := window_texts(hwnd)) and all(e in t for e in expected) and t,
            timeout=30,
            interval=1,
        )
        assert "急停：正常" in texts
        assert proc.poll() is None
        assert [e for e in server.log.entries() if e.kind == "write"] == []
    finally:
        for proc in processes:
            kill_tree(proc)
        server.stop()
