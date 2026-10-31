"""plc-sim 的紀錄（spec FR-56）：收到的寫入、連線建立與中斷、讀取統計、模擬狀態變化。

每筆帶毫秒時間。讀取請求只累計次數與最近一次時間，不逐筆記錄（輪詢下會淹沒其他紀錄）；
匯出時以一行 `read` 摘要寫出。執行緒安全：server 執行緒寫入、視窗執行緒讀取。
"""

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

Kind = Literal["write", "connect", "disconnect", "read", "state"]

# 狀態變化紀錄的對象（FR-56）：各軸這些名稱的點與 Emergency Stop
STATE_NAMES = ("Ready", "Busy", "Reply", "Find Home Finish", "Alarm Code", "PLC Action Code")

_KIND_LABELS = {
    "write": "寫入",
    "connect": "連線",
    "disconnect": "斷線",
    "read": "讀取",
    "state": "狀態",
}

MAX_ENTRIES = 100_000


def format_time(t: float) -> str:
    dt = datetime.fromtimestamp(t)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


@dataclass(frozen=True)
class LogEntry:
    time: float  # time.time()，秒
    kind: Kind
    tag: str | None = None
    value: int | bool | None = None
    text: str = ""

    def format(self) -> str:
        parts = [format_time(self.time), f"[{_KIND_LABELS[self.kind]}]"]
        if self.tag is not None:
            value = int(self.value) if isinstance(self.value, bool) else self.value
            parts.append(f"{self.tag} = {value}")
        if self.text:
            parts.append(self.text)
        return " ".join(parts)


def is_state_tag(tag: str) -> bool:
    if tag == "Emergency Stop":
        return True
    name, _, axis = tag.rpartition(" ")
    return name in STATE_NAMES and axis.isdigit()


class SimLog:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: deque[LogEntry] = deque(maxlen=MAX_ENTRIES)
        self._read_count = 0
        self._last_read: float | None = None

    def entries(self) -> list[LogEntry]:
        with self._lock:
            return list(self._entries)

    def last_read_time(self) -> float | None:
        with self._lock:
            return self._last_read

    def read_count(self) -> int:
        with self._lock:
            return self._read_count

    def export(self, path: Path) -> None:
        lines = [e.format() for e in self.entries()]
        count, last = self.read_count(), self.last_read_time()
        last_text = "無" if last is None else format_time(last)
        summary = f"讀取請求累計 {count} 次，最近一次：{last_text}"
        lines.append(LogEntry(self._clock(), "read", text=summary).format())
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 寫入端（server 使用） ----

    def add(
        self,
        kind: Kind,
        tag: str | None = None,
        value: int | bool | None = None,
        text: str = "",
    ) -> None:
        entry = LogEntry(self._clock(), kind, tag, value, text)
        with self._lock:
            self._entries.append(entry)

    def count_read(self) -> None:
        now = self._clock()
        with self._lock:
            self._read_count += 1
            self._last_read = now

    def on_model_change(self, tag: str, value: int | bool) -> None:
        """PlcModel listener：只記 FR-56 的狀態點；Master 寫入的點由寫入紀錄負責。"""
        if is_state_tag(tag):
            self.add("state", tag, value)
