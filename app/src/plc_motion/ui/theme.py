"""深色／淺色主題（spec FR-45）：QSS 檔與給程式碼著色用的語意色。

QSS 在 `themes/<name>.qss`。控制項以 dynamic property `state` 標示語意狀態，QSS 依此著色：

    ok（正常）、busy（動作中）、fault（異常）、off（未連線／無資料）

改了 `state` 之後呼叫 `set_state(widget, state)` 讓樣式重新套用。
燈號、表格列底色等以程式繪製者用 `color(role)` 取目前主題的色值；Check Status 五態
以 `check_<status 小寫>` 為 role（例：`check_ng`）。
"""

from functools import cache
from importlib import resources

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

THEMES = ("dark", "light")

_COLORS = {
    "dark": {
        "bg": "#1d1d1f",
        "panel": "#2d2d2f",
        "panel_alt": "#3d3d3f",
        "border": "#48484a",
        "text": "#f5f5f7",
        "text_dim": "#98989d",
        "accent": "#0a84ff",
        "ok": "#32d74b",
        "busy": "#0a84ff",
        "fault": "#ff453a",
        "warn": "#ffb340",
        "off": "#636366",
        "check_ok": "#1f5130",
        "check_ng": "#6b2420",
        "check_unchecked": "#2d2d2f",
        "check_recheck": "#6b4f12",
        "check_ignore": "#3a3a52",
    },
    "light": {
        "bg": "#f5f5f7",
        "panel": "#ffffff",
        "panel_alt": "#e5e5e7",
        "border": "#c7c7cc",
        "text": "#1d1d1f",
        "text_dim": "#6e6e73",
        "accent": "#0071e3",
        "ok": "#1f9d3a",
        "busy": "#0071e3",
        "fault": "#d70015",
        "warn": "#b25000",
        "off": "#8e8e93",
        "check_ok": "#d4f5dc",
        "check_ng": "#ffd6d3",
        "check_unchecked": "#ffffff",
        "check_recheck": "#ffecc2",
        "check_ignore": "#e3e3f0",
    },
}

_current = "dark"


@cache
def load_qss(name: str) -> str:
    if name not in THEMES:
        raise ValueError(f"未知的主題：{name}")
    return resources.files("plc_motion.ui").joinpath("themes", f"{name}.qss").read_text("utf-8")


def apply_theme(app: QApplication, name: str) -> None:
    global _current
    app.setStyleSheet(load_qss(name))
    _current = name


def current_theme() -> str:
    return _current


def color(role: str, theme: str | None = None) -> QColor:
    return QColor(_COLORS[theme or _current][role])


def set_state(widget: QWidget, state: str) -> None:
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
