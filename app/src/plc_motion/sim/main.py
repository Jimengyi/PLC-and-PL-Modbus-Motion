"""plc-sim 進入點（spec FR-51）。

    plc-sim [--host 127.0.0.1] [--port 5020] [--config DIR]

載入設定資料夾的 io_points.csv 與 read_ranges.toml，啟動模擬 PLC 的 Modbus TCP server 與操作視窗。
"""

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from plc_motion.plc import PlcModel
from plc_motion.settings import ConfigErrors, load_config, resolve_config_dir
from plc_motion.sim.server import SimServer
from plc_motion.sim.sim_window import SimWindow
from plc_motion.ui.theme import apply_theme


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"埠號須為整數：{text}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"埠號須為 0 ~ 65535：{text}")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="plc-sim", description="模擬 PLC")
    parser.add_argument("--host", default="127.0.0.1", help="監聽位址")
    parser.add_argument("--port", type=_port, default=5020, help="監聽埠號")
    parser.add_argument("--config", help="設定資料夾（預設為程式旁的 config）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config_dir = Path(args.config).resolve() if args.config else resolve_config_dir()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("plc-sim")
    try:
        config = load_config(config_dir)
    except ConfigErrors as e:
        print("\n".join(e.errors), file=sys.stderr)
        QMessageBox.critical(None, "模擬 PLC", "設定檔錯誤：\n" + "\n".join(e.errors))
        return 1
    apply_theme(app)

    server = SimServer(
        PlcModel(config.points), config.points, args.host, args.port, config.read_ranges
    )
    try:
        server.start()
    except Exception as e:
        print(f"無法啟動模擬 PLC：{e!r}", file=sys.stderr)
        QMessageBox.critical(None, "模擬 PLC", f"無法啟動（{args.host}:{args.port}）：{e!r}")
        return 1
    window = SimWindow(server)
    window.show()
    try:
        return app.exec()
    finally:
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
