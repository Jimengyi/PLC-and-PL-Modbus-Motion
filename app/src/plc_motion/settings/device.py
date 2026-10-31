"""device.toml：連線、輪詢、交握、日誌與主題設定。

檔案格式：

    [connection]  host、port、timeout_ms（必要）
    [polling]     interval_ms（預設 200）
    [handshake]   timeout_s（預設 5）、reset_alarm_pulse_ms（預設 500）
    [log]         retention_days（預設 30）
    [ui]          theme（dark 或 light，預設 dark）
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plc_motion.settings.paths import atomic_write_bytes

FILE_NAME = "device.toml"
THEMES = ("dark", "light")


@dataclass
class DeviceSettings:
    host: str
    port: int
    connect_timeout_ms: int
    poll_interval_ms: int = 200
    handshake_timeout_s: float = 5
    reset_alarm_pulse_ms: int = 500
    log_retention_days: int = 30
    theme: str = "dark"
    path: Path | None = field(default=None, repr=False)

    def save_theme(self, theme: str) -> None:
        """只改 [ui] 段的 theme 值，其餘內容與註解逐字保留；原子替換寫回。"""
        if theme not in THEMES:
            raise ValueError(f"主題只能是 dark 或 light：{theme}")
        if self.path is None:
            raise ValueError("此設定未對應到檔案")
        text = self.path.read_bytes().decode("utf-8")
        atomic_write_bytes(self.path, _replace_theme(text, theme).encode("utf-8"))
        self.theme = theme


_TABLE_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_\-]+)\s*\]\s*(#.*)?$")
_THEME_RE = re.compile(r"""^(\s*theme\s*=\s*)("[^"\n]*"|'[^'\n]*')""")


def _replace_theme(text: str, theme: str) -> str:
    lines = text.splitlines(keepends=True)
    section = ""
    ui_header_index = None
    for idx, line in enumerate(lines):
        m = _TABLE_RE.match(line.rstrip("\r\n"))
        if m:
            section = m.group(1)
            if section == "ui":
                ui_header_index = idx
            continue
        if section == "ui":
            m = _THEME_RE.match(line)
            if m:
                lines[idx] = m.group(1) + f'"{theme}"' + line[m.end() :]
                return "".join(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    if ui_header_index is not None:
        lines.insert(ui_header_index + 1, f'theme = "{theme}"{newline}')
        return "".join(lines)
    out = "".join(lines)
    if out and not out.endswith(("\n", "\r")):
        out += newline
    return out + f'{newline}[ui]{newline}theme = "{theme}"{newline}'


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_host(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _is_port(v: Any) -> bool:
    return _is_int(v) and 1 <= v <= 65535


def _is_positive_number(v: Any) -> bool:
    return _is_number(v) and v > 0


def parse_device(data: dict[str, Any], path: Path) -> tuple[DeviceSettings | None, list[str]]:
    errors: list[str] = []

    def table(name: str) -> dict[str, Any]:
        t = data.get(name, {})
        if not isinstance(t, dict):
            errors.append(f"{FILE_NAME}：[{name}] 必須是一個段落")
            return {}
        return t

    conn = table("connection")
    polling = table("polling")
    handshake = table("handshake")
    log = table("log")
    ui = table("ui")

    def get(t: dict[str, Any], section: str, key: str, default: Any, check, rule: str):
        if key not in t:
            if default is None:
                errors.append(f"{FILE_NAME}：缺少 [{section}] {key}")
            return default
        v = t[key]
        if not check(v):
            errors.append(f"{FILE_NAME}：[{section}] {key} = {v!r} 不合法，{rule}")
            return default
        return v

    pos_int, pos_int_rule = (lambda v: _is_int(v) and v > 0), "須為正整數"
    host = get(conn, "connection", "host", None, _is_host, "須為非空字串")
    port = get(conn, "connection", "port", None, _is_port, "須為 1 ~ 65535 的整數")
    timeout_ms = get(conn, "connection", "timeout_ms", None, pos_int, pos_int_rule)
    interval = get(polling, "polling", "interval_ms", 200, pos_int, pos_int_rule)
    hs_timeout = get(handshake, "handshake", "timeout_s", 5, _is_positive_number, "須為正數")
    pulse = get(handshake, "handshake", "reset_alarm_pulse_ms", 500, pos_int, pos_int_rule)
    retention = get(log, "log", "retention_days", 30, pos_int, pos_int_rule)
    theme = get(ui, "ui", "theme", "dark", lambda v: v in THEMES, "須為 dark 或 light")

    if errors:
        return None, errors
    return (
        DeviceSettings(
            host=host,
            port=port,
            connect_timeout_ms=timeout_ms,
            poll_interval_ms=interval,
            handshake_timeout_s=hs_timeout,
            reset_alarm_pulse_ms=pulse,
            log_retention_days=retention,
            theme=theme,
            path=path,
        ),
        [],
    )


def load_device(path: Path) -> tuple[DeviceSettings | None, list[str]]:
    data = load_toml(path)
    if isinstance(data, list):
        return None, data
    return parse_device(data, path)


def load_toml(path: Path) -> dict[str, Any] | list[str]:
    """回傳解析結果；失敗時回傳錯誤訊息清單。"""
    if not path.is_file():
        return [f"缺少設定檔 {path.name}"]
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [f"{path.name}：格式無法解析（{e}）"]
