"""PyInstaller onedir 打包驗收（spec FR-54）。標記 slow，預設跳過；以 `-m slow` 執行。"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PyInstaller.archive.readers import CArchiveReader

from plc_motion.plc import PlcModel
from plc_motion.settings import load_config
from plc_motion.sim.server import SimServer

pytestmark = pytest.mark.slow

APP_DIR = Path(__file__).resolve().parents[1]
DIST = APP_DIR / "dist" / "plc-motion"


@pytest.fixture(scope="module")
def dist() -> Path:
    subprocess.run(
        [sys.executable, str(APP_DIR / "packaging" / "build.py")],
        cwd=APP_DIR,
        check=True,
        timeout=900,
    )
    return DIST


def test_exe_and_default_config_are_present(dist: Path):
    assert (dist / "plc-motion.exe").is_file()
    for name in (
        "app.toml",
        "axes.toml",
        "positions.csv",
        "modbus/Channel01/device.toml",
        "modbus/Channel01/io_points.csv",
        "modbus/Channel01/read_ranges.toml",
    ):
        assert (dist / "config" / name).read_bytes() == (APP_DIR / "config" / name).read_bytes()


def test_qt_dlls_are_separate_files(dist: Path):
    # onedir：Qt DLL 是獨立檔，LGPL 要求使用者可替換
    assert list(dist.rglob("Qt6Core.dll"))
    assert list(dist.rglob("Qt6Widgets.dll"))


def test_not_onefile(dist: Path):
    assert (dist / "_internal").is_dir()
    assert (dist / "plc-motion.exe").stat().st_size < 50 * 1024 * 1024


def test_theme_files_are_bundled(dist: Path):
    names = {p.name for p in dist.rglob("*.qss")}
    assert "dark.qss" in names


def test_license_notices_cover_bundled_packages(dist: Path):
    text = (dist / "THIRD_PARTY_NOTICES.md").read_text("utf-8")
    for needle in ("PySide6", "Qt", "LGPL", "pymodbus", "pyserial", "Python"):
        assert needle in text
    assert (dist / "licenses").is_dir()
    assert any((dist / "licenses").iterdir())
    pyserial = (dist / "licenses" / "pyserial-LICENSE.txt").read_text("utf-8")
    assert "Chris Liechti" in pyserial
    lgpl = (dist / "licenses" / "LGPL-3.0.txt").read_text("utf-8")
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in lgpl
    gpl = (dist / "licenses" / "GPL-3.0.txt").read_text("utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in gpl


def test_simulator_is_not_packaged(dist: Path):
    for p in dist.rglob("*"):
        low = p.name.lower()
        assert "sim_window" not in low and "plc-sim" not in low and "plc_sim" not in low, p
    assert not list(dist.rglob("sim"))
    # 純 Python 模組都壓在 exe 內的 PYZ：讀它的目錄確認沒有 sim 模組
    pyz = CArchiveReader(str(dist / "plc-motion.exe")).open_embedded_archive("PYZ.pyz")
    modules = [name for name in pyz.toc if name.startswith("plc_motion")]
    assert "plc_motion.app" in modules
    assert not [name for name in modules if name.startswith("plc_motion.sim")]


def test_copied_exe_connects_to_simulator(dist: Path, tmp_path: Path):
    config = load_config(APP_DIR / "config")
    copy = tmp_path / "plc-motion"
    shutil.copytree(dist, copy)
    server = SimServer(PlcModel(config.points), config.points, "127.0.0.1", 0, config.read_ranges)
    server.start()
    port = server.port
    proc = subprocess.Popen(
        [str(copy / "plc-motion.exe"), "--host", "127.0.0.1", "--port", str(port)]
    )
    try:
        log_dir = copy / "logs"
        deadline = time.monotonic() + 30
        connected = False
        while time.monotonic() < deadline and not connected:
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            for f in log_dir.glob("*.log*") if log_dir.is_dir() else []:
                if "已連線到 PLC" in f.read_text("utf-8", errors="replace"):
                    connected = True
        assert proc.poll() is None, "exe 提前結束（本機是否已有 plc-motion 在跑？）"
        assert connected
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        server.stop()


def test_exe_uses_app_icon(dist: Path):
    """exe 內嵌的圖示就是程式的六角形圖示（.ico 的 PNG 原樣存成 exe 的圖示資源）。"""
    import struct

    ico = (dist.parent.parent / "build" / "plc-motion.ico").read_bytes()
    exe = (dist / "plc-motion.exe").read_bytes()
    count = struct.unpack_from("<H", ico, 4)[0]
    length, offset = struct.unpack_from("<II", ico, 6 + 16 * (count - 1) + 8)
    assert ico[offset : offset + length] in exe  # 最大尺寸（256 px）那張
