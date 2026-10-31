"""程式日誌（spec FR-48）：`logs` 資料夾每天一檔，超過保留天數的檔案刪除。

全程式的 logger 都掛在 `plc_motion` 底下（`logging.getLogger("plc_motion.xxx")`）。
檔名 `plc-motion-YYYY-MM-DD.log`，日期取紀錄產生的時間；跨日時自動換檔，並順便清理舊檔。
保留天數以檔名日期判斷：比今天早超過 keep_days 天的刪除，檔名不符格式的檔案不動。
"""

import logging
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

LOGGER_NAME = "plc_motion"
_FILE_RE = re.compile(r"^plc-motion-(\d{4}-\d{2}-\d{2})\.log$")
_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def log_file_name(day: date) -> str:
    return f"plc-motion-{day.isoformat()}.log"


def delete_old_logs(logs_dir: Path, keep_days: int, today: date | None = None) -> None:
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    for path in logs_dir.iterdir():
        m = _FILE_RE.match(path.name)
        if not m or not path.is_file():
            continue
        try:
            day = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError:
                pass  # 被其他程式開著時下次再刪


class DailyFileHandler(logging.Handler):
    """依紀錄日期寫入當日檔案；跨日換檔並刪除超過保留天數的舊檔。"""

    def __init__(self, logs_dir: Path, keep_days: int):
        super().__init__()
        self.logs_dir = logs_dir
        self.keep_days = keep_days
        self._day: date | None = None
        self._stream = None
        self._io_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record) + "\n"
            day = datetime.fromtimestamp(record.created).date()
            with self._io_lock:
                if day != self._day:
                    self._open(day)
                self._stream.write(text)
                self._stream.flush()
        except Exception:
            self.handleError(record)

    def _open(self, day: date) -> None:
        self._close_stream()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.logs_dir / log_file_name(day)
        self._stream = path.open("a", encoding="utf-8")
        if self._day is not None:  # 跨日：以新日期清理
            delete_old_logs(self.logs_dir, self.keep_days, today=day)
        self._day = day

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        with self._io_lock:
            self._close_stream()
            self._day = None
        super().close()


def setup_logging(logs_dir: Path, keep_days: int) -> None:
    """建立 logs 資料夾、刪除舊檔、把 `plc_motion` logger 接到每日檔案。

    重複呼叫會換掉前一次加上的 handler。
    """
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    delete_old_logs(logs_dir, keep_days)
    close_logging()
    handler = DailyFileHandler(logs_dir, keep_days)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATE_FORMAT))
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def close_logging() -> None:
    """移除並關閉 setup_logging 加上的 handler（程式結束或測試清理用）。"""
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, DailyFileHandler):
            logger.removeHandler(h)
            h.close()
