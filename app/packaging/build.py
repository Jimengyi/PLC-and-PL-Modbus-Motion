"""產生 app/dist/plc-motion/（PyInstaller onedir）並補上 config、授權聲明。

cd app && uv run python packaging/build.py
"""

import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
APP_DIR = PACKAGING_DIR.parent
DIST_DIR = APP_DIR / "dist"
OUT_DIR = DIST_DIR / "plc-motion"
BUILD_DIR = APP_DIR / "build"

# 實際打包進去且帶授權檔的第三方套件：(發行套件名, 輸出檔名)
BUNDLED_LICENSES = [("pymodbus", "pymodbus-LICENSE.txt")]


def _copy_license(dist_name: str, target: Path) -> None:
    for f in metadata.files(dist_name) or []:
        if f.name.upper().startswith("LICENSE") and "licenses" in f.parts:
            shutil.copyfile(f.locate(), target)
            return
    raise FileNotFoundError(f"找不到 {dist_name} 的授權檔")


def main() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(PACKAGING_DIR / "plc_motion.spec"),
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(BUILD_DIR),
        ],
        cwd=APP_DIR,
        check=True,
    )

    shutil.copytree(APP_DIR / "config", OUT_DIR / "config", dirs_exist_ok=True)

    shutil.copyfile(PACKAGING_DIR / "THIRD_PARTY_NOTICES.md", OUT_DIR / "THIRD_PARTY_NOTICES.md")
    licenses = OUT_DIR / "licenses"
    licenses.mkdir(exist_ok=True)
    for dist_name, filename in BUNDLED_LICENSES:
        _copy_license(dist_name, licenses / filename)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    # Qt 的 LGPL v3 第 4 節要求隨附 LGPL v3 與其引用的 GPL v3 全文
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        shutil.copyfile(PACKAGING_DIR / name, licenses / name)
    print(f"完成：{OUT_DIR}")


if __name__ == "__main__":
    main()
