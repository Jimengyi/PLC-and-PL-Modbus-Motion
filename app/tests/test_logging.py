"""setup_logging：每日一檔於 logs、超過保留天數的檔案刪除（spec FR-48）。"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from plc_motion.logging_setup import LOGGER_NAME, close_logging, log_file_name, setup_logging


@pytest.fixture
def logs_dir(tmp_path: Path):
    d = tmp_path / "logs"
    yield d
    close_logging()


def test_log_file_name_contains_date():
    assert log_file_name(date(2026, 10, 31)) == "plc-motion-2026-10-31.log"


def test_creates_logs_dir_and_writes_today_file(logs_dir):
    setup_logging(logs_dir, keep_days=30)
    logging.getLogger(f"{LOGGER_NAME}.test").info("連線成功 127.0.0.1:502")
    path = logs_dir / log_file_name(date.today())
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "連線成功 127.0.0.1:502" in text
    assert "[INFO]" in text


def test_exception_traceback_is_written(logs_dir):
    setup_logging(logs_dir, keep_days=30)
    log = logging.getLogger(f"{LOGGER_NAME}.test")
    try:
        raise RuntimeError("測試例外")
    except RuntimeError:
        log.exception("未預期的例外")
    text = (logs_dir / log_file_name(date.today())).read_text(encoding="utf-8")
    assert "未預期的例外" in text
    assert "Traceback" in text
    assert "RuntimeError: 測試例外" in text


def test_record_on_next_day_goes_to_new_file(logs_dir):
    setup_logging(logs_dir, keep_days=30)
    log = logging.getLogger(f"{LOGGER_NAME}.test")
    log.info("今天")
    tomorrow = datetime.now() + timedelta(days=1)
    record = log.makeRecord(log.name, logging.INFO, __file__, 0, "明天", (), None)
    record.created = tomorrow.timestamp()
    log.handle(record)
    today_text = (logs_dir / log_file_name(date.today())).read_text(encoding="utf-8")
    tomorrow_text = (logs_dir / log_file_name(tomorrow.date())).read_text(encoding="utf-8")
    assert "今天" in today_text and "明天" not in today_text
    assert "明天" in tomorrow_text


def test_deletes_files_older_than_keep_days(logs_dir):
    logs_dir.mkdir()
    today = date.today()
    old = logs_dir / log_file_name(today - timedelta(days=31))
    edge = logs_dir / log_file_name(today - timedelta(days=30))
    other = logs_dir / "notes-2000-01-01.txt"
    bad = logs_dir / "plc-motion-not-a-date.log"
    for p in (old, edge, other, bad):
        p.write_text("x", encoding="utf-8")
    setup_logging(logs_dir, keep_days=30)
    assert not old.exists()
    assert edge.exists()
    assert other.exists()
    assert bad.exists()


def test_setup_twice_does_not_duplicate_lines(logs_dir):
    setup_logging(logs_dir, keep_days=30)
    setup_logging(logs_dir, keep_days=30)
    logging.getLogger(f"{LOGGER_NAME}.test").info("只寫一次")
    text = (logs_dir / log_file_name(date.today())).read_text(encoding="utf-8")
    assert text.count("只寫一次") == 1
