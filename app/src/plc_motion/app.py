"""plc-motion 程式進入點與組裝（spec FR-8、FR-14、FR-42 ~ FR-46、FR-48 ~ FR-50）。

    plc-motion [--host HOST] [--port PORT] [--config DIR]

`--config` 預設為 `resolve_config_dir()`；日誌在設定資料夾同層的 `logs`。`--host`／`--port`
只覆蓋本次連線，不寫回 device.toml。

啟動順序：單一實例檢查 → 日誌（先用預設保留天數，設定檔讀到後改用設定值）→ 載入設定
（錯誤時列出全部錯誤後以結束碼 1 結束）→ 主題 → MotionApp（worker、主視窗、系統匣）。
"""

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from plc_motion.logging_setup import close_logging, setup_logging
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings import ConfigErrors, load_config, resolve_config_dir
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.axis_page import AxisPage
from plc_motion.ui.dialogs import ConfigErrorDialog, ExceptionReporter
from plc_motion.ui.io_monitor import IoMonitorPage
from plc_motion.ui.main_window import MainWindow
from plc_motion.ui.single_instance import SingleInstance
from plc_motion.ui.theme import apply_theme
from plc_motion.ui.tray import APP_TITLE, TrayController
from plc_motion.worker import EngineStatus, start_worker

log = logging.getLogger("plc_motion.app")

DEFAULT_LOG_RETENTION_DAYS = 30
EXIT_CONFIG_ERROR = 1
_WORKER_STOP_TIMEOUT_MS = 5000


class MotionApp(QObject):
    """背景 worker、主視窗與系統匣的組裝；`shutdown()` 收尾後發 `finished`。"""

    finished = Signal()

    def __init__(self, config: AppConfig, host: str, port: int, parent: QObject | None = None):
        super().__init__(parent)
        self.config = config
        self.snapshot: Snapshot | None = None
        self.last_status: EngineStatus | None = None
        self._shut_down = False

        self.thread, self.worker = start_worker(config, host, port)
        # start_worker 的自動連線排在事件迴圈：以下接線都在回到事件迴圈之前完成
        self.window = MainWindow(config, self.worker, f"{host}:{port}")
        self.axis_page = AxisPage(config, self.worker)
        self.window.add_page("軸控", None, self.axis_page)
        self.io_page = IoMonitorPage(config, self.worker)
        self.window.add_page("IO Monitor", None, self.io_page)
        self.tray = TrayController(sorted(config.axes))
        self.worker.snapshot_ready.connect(self._on_snapshot)
        self.worker.snapshot_ready.connect(self.tray.update)
        self.tray.show_requested.connect(self.window.show_from_tray)
        self.tray.reconnect_requested.connect(self.worker.connect_plc)
        self.tray.quit_requested.connect(self.window.request_quit)
        self.window.quit_confirmed.connect(self.shutdown)

    def show(self) -> None:
        self.tray.show()
        self.window.show()

    def _on_snapshot(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self.snapshot = snapshot
        self.last_status = status

    def shutdown(self) -> None:
        """不寫入任何點（FR-44）：先藏起視窗（頁面依此放開按住中的 JOG），再停 worker。"""
        if self._shut_down:
            return
        self._shut_down = True
        log.info("程式結束")
        self.window.hide()
        self.worker.stop()
        if not self.thread.wait(_WORKER_STOP_TIMEOUT_MS):
            log.error("背景通訊執行緒未在 %s ms 內結束", _WORKER_STOP_TIMEOUT_MS)
        self.tray.hide()
        self.window.allow_close()
        self.window.close()
        self.finished.emit()


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"埠號須為整數：{text}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"埠號須為 1 ~ 65535：{text}")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="plc-motion", description=APP_TITLE)
    parser.add_argument("--host", help="PLC IP（暫時覆蓋 device.toml，不寫回）")
    parser.add_argument("--port", type=_port, help="Modbus TCP 埠號（暫時覆蓋 device.toml）")
    parser.add_argument("--config", help="設定資料夾（預設為程式旁的 config）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_dir = Path(args.config).resolve() if args.config else resolve_config_dir()
    logs_dir = config_dir.parent / "logs"

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("plc-motion")
    app.setQuitOnLastWindowClosed(False)  # 主視窗縮到系統匣時不結束

    instance = SingleInstance()
    if not instance.acquire():
        return 0
    try:
        setup_logging(logs_dir, DEFAULT_LOG_RETENTION_DAYS)
        log.info("程式啟動，設定資料夾：%s", config_dir)
        try:
            config = load_config(config_dir)
        except ConfigErrors as e:
            errors = e.errors
        except Exception as e:
            log.exception("讀取設定檔時發生未預期的例外")
            errors = [f"讀取設定檔時發生未預期的例外：{e!r}"]
        else:
            errors = []
        if errors:
            for error in errors:
                log.error("設定檔錯誤：%s", error)
            ConfigErrorDialog(errors, config_dir).exec()
            return EXIT_CONFIG_ERROR

        if config.device.log_retention_days != DEFAULT_LOG_RETENTION_DAYS:
            setup_logging(logs_dir, config.device.log_retention_days)
        apply_theme(app, config.device.theme)
        host = args.host or config.device.host
        port = args.port or config.device.port
        log.info("連線目標：%s:%s", host, port)

        motion = MotionApp(config, host, port)
        instance.activate_requested.connect(motion.window.show_from_tray)
        reporter = ExceptionReporter(motion.window)
        reporter.install()
        motion.finished.connect(app.quit)
        app.aboutToQuit.connect(motion.shutdown)  # 例如 Windows 登出：同樣先停 worker
        motion.show()
        try:
            return app.exec()
        finally:
            app.aboutToQuit.disconnect(motion.shutdown)
            motion.shutdown()
            reporter.uninstall()
    finally:
        instance.release()
        close_logging()
