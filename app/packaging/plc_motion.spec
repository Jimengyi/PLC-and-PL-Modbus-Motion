# PyInstaller spec：onedir、視窗模式。plc-sim（plc_motion.sim）不進入打包產物。
# 用法見 packaging/build.py；onedir 讓 Qt DLL 保持獨立檔（LGPL 可替換）。
from pathlib import Path

APP_DIR = Path(SPECPATH).parent  # noqa: F821  (SPECPATH 由 PyInstaller 提供)

a = Analysis(  # noqa: F821
    [str(APP_DIR / "packaging" / "entry.py")],
    pathex=[str(APP_DIR / "src")],
    binaries=[],
    # QSS 以 importlib.resources 讀取
    datas=[
        (str(APP_DIR / "src" / "plc_motion" / "ui" / "themes" / "*.qss"), "plc_motion/ui/themes"),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["plc_motion.sim", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir
    name="plc-motion",
    icon=str(APP_DIR / "build" / "plc-motion.ico"),  # build.py 先產生
    console=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="plc-motion",
)
