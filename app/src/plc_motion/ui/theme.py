"""介面主題（spec FR-45）：工業風深色 QSS 與給程式碼著色用的語意色。

只有一套深色主題。顏色取向依 ISA-101 高效能 HMI：中性灰底，顏色只留給狀態——
ok／busy（運轉）、warn（警告）、fault（異常）；off 為未連線／無資料／未亮燈。

QSS 在 `themes/dark.qss`。控制項以 dynamic property `state` 標示語意狀態，QSS 依此著色；
改了 `state` 之後呼叫 `set_state(widget, state)` 讓樣式重新套用。
燈號、圖表等以程式繪製者用 `color(role)` 取色值。
"""

from functools import cache
from importlib import resources

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QWidget

FONT_FAMILIES = ("Bahnschrift", "Microsoft JhengHei UI", "Segoe UI")

# 與 themes/dark.qss 一致
COLORS = {
    "bg": "#16191d",  # 視窗底色
    "panel": "#1f2328",  # 面板
    "panel_alt": "#2a2f36",  # 面板標題列、表頭
    "inset": "#0f1215",  # 數值顯示、輸入框、表格內容
    "border": "#3a4048",
    "border_strong": "#4b525c",
    "text": "#e6e8eb",
    "text_dim": "#8b939e",
    "accent": "#2f9bff",  # 選取、主要按鈕
    "accent_dim": "#173a5c",  # 選取底色
    "ok": "#3ccf63",
    "busy": "#2f9bff",
    "warn": "#f2b21b",
    "fault": "#ff4b3e",
    "off": "#464d57",
    "button": "#2b3138",
    # 點位表超出軟體極限的格子底色
    "check_ng": "#5a211d",
}


@cache
def load_qss() -> str:
    return resources.files("plc_motion.ui").joinpath("themes", "dark.qss").read_text("utf-8")


def app_font() -> QFont:
    """英數用 Bahnschrift（DIN 風格、等寬數字），中文落到微軟正黑體。"""
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPointSizeF(10.5)
    return font


def apply_theme(app: QApplication) -> None:
    app.setFont(app_font())
    app.setStyleSheet(load_qss())


def color(role: str) -> QColor:
    return QColor(COLORS[role])


def set_state(widget: QWidget, state: str) -> None:
    if widget.property("state") == state:
        return
    widget.setProperty("state", state)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
