"""設定資料夾位置與原子寫檔。"""

import os
import sys
import tempfile
from pathlib import Path


def resolve_config_dir() -> Path:
    """打包後：exe 同層的 config；原始碼執行：app/config。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config"
    # paths.py → settings → plc_motion → src → app
    return Path(__file__).resolve().parents[3] / "config"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """先寫同資料夾的暫存檔並 fsync，再以 os.replace 取代原檔；中途失敗時原檔不變、暫存檔移除。"""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
