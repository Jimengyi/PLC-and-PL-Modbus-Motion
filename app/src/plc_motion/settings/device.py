"""app.toml（交握與日誌）與 `DeviceSettings`（連線、輪詢、交握、日誌設定的合併結果）。

app.toml 格式：

    [handshake]   timeout_s（預設 5）、reset_alarm_pulse_ms（預設 500）
    [log]         retention_days（預設 30）

連線與輪詢設定在通道的 `modbus/Channel01/device.toml`（modbus_tags 格式），由函式庫載入；
`build_device` 把兩者合併成引擎與 Worker 使用的 `DeviceSettings`。
app.toml 的其他段落（例如舊版的 [ui] theme）不使用，留著也不影響載入。
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.modbus_tags.config import DeviceConfig

FILE_NAME = "app.toml"


@dataclass
class DeviceSettings:
    host: str
    port: int
    connect_timeout_ms: int
    poll_interval_ms: int = 200
    handshake_timeout_s: float = 5
    reset_alarm_pulse_ms: int = 500
    log_retention_days: int = 30


@dataclass(frozen=True)
class AppSettings:
    handshake_timeout_s: float = 5
    reset_alarm_pulse_ms: int = 500
    log_retention_days: int = 30


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_positive_int(v: Any) -> bool:
    return _is_int(v) and v > 0


def _is_positive_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def parse_app(data: dict[str, Any]) -> tuple[AppSettings | None, list[str]]:
    errors: list[str] = []

    def table(name: str) -> dict[str, Any]:
        t = data.get(name, {})
        if not isinstance(t, dict):
            errors.append(f"{FILE_NAME}：[{name}] 必須是一個段落")
            return {}
        return t

    handshake = table("handshake")
    log = table("log")

    def get(t: dict[str, Any], section: str, key: str, default: Any, check, rule: str):
        if key not in t:
            return default
        v = t[key]
        if not check(v):
            errors.append(f"{FILE_NAME}：[{section}] {key} = {v!r} 不合法，{rule}")
            return default
        return v

    pos_int, pos_int_rule = _is_positive_int, "須為正整數"
    hs_timeout = get(handshake, "handshake", "timeout_s", 5, _is_positive_number, "須為正數")
    pulse = get(handshake, "handshake", "reset_alarm_pulse_ms", 500, pos_int, pos_int_rule)
    retention = get(log, "log", "retention_days", 30, pos_int, pos_int_rule)
    if errors:
        return None, errors
    return AppSettings(hs_timeout, pulse, retention), []


def load_app(path: Path) -> tuple[AppSettings | None, list[str]]:
    data = load_toml(path)
    if isinstance(data, list):
        return None, data
    return parse_app(data)


def build_device(app: AppSettings, device: DeviceConfig) -> DeviceSettings:
    """通道 device.toml（已由函式庫檢查為 TCP）＋ app.toml → `DeviceSettings`。"""
    assert device.tcp_host is not None
    return DeviceSettings(
        host=device.tcp_host,
        port=device.tcp_port,
        connect_timeout_ms=device.timeout_ms,
        poll_interval_ms=device.interval_ms,
        handshake_timeout_s=app.handshake_timeout_s,
        reset_alarm_pulse_ms=app.reset_alarm_pulse_ms,
        log_retention_days=app.log_retention_days,
    )


def load_toml(path: Path) -> dict[str, Any] | list[str]:
    """回傳解析結果；失敗時回傳錯誤訊息清單。"""
    if not path.is_file():
        return [f"缺少設定檔 {path.name}"]
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [f"{path.name}：格式無法解析（{e}）"]
