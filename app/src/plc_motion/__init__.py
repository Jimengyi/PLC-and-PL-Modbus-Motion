def main() -> None:
    """`plc-motion` 指令進入點；實作在 `plc_motion.app.main`。"""
    from plc_motion.app import main as app_main

    raise SystemExit(app_main())
