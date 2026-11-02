"""各頁共用的小元件：面板、燈號、速度增減器。樣式在 themes/dark.qss。"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from plc_motion.ui.theme import color, set_state

SPEED_MIN, SPEED_MAX = 1, 100
SPEED_STEP = 5


def set_flag(widget: QWidget, name: str, value: bool) -> None:
    """設定給 QSS 選擇器用的 bool property 並重新套用樣式。"""
    if widget.property(name) is value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class _IgnoreWheel(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        return event.type() == QEvent.Type.Wheel


_IGNORE_WHEEL = _IgnoreWheel()


def plain_spinbox(spin: QAbstractSpinBox) -> None:
    """不顯示上下鍵（QSS 把寬度設為 0）、不吃滑鼠滾輪，避免游標經過時誤改數值。

    不用 `NoButtons`：套用樣式表時它會讓內部文字框寬度變成 1 px，數字整個看不到。
    """
    spin.installEventFilter(_IGNORE_WHEEL)
    spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


def named_button(text: str, name: str) -> QPushButton:
    """以 objectName 指定樣式的按鈕（primary、danger、big、small…）。"""
    button = QPushButton(text)
    button.setObjectName(name)
    return button


def segment_button(text: str) -> QPushButton:
    """單選用的可勾選按鈕（放進 exclusive QButtonGroup）。"""
    button = QPushButton(text)
    button.setObjectName("segment")
    button.setCheckable(True)
    return button


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    return label


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


class Panel(QFrame):
    """有標題列的面板：`header_layout` 可再放按鈕，內容放 `body_layout`。"""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("panel")
        header = QFrame()
        header.setObjectName("panelHeader")
        header.setFixedHeight(38)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("panelTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("panelSub")
        self.header_layout = QHBoxLayout(header)
        self.header_layout.setContentsMargins(12, 0, 8, 0)
        self.header_layout.setSpacing(8)
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addWidget(self.subtitle_label)
        self.header_layout.addStretch(1)

        body = QWidget()
        body.setObjectName("panelBody")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(12, 12, 12, 12)
        self.body_layout.setSpacing(10)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(body, 1)


_LAMP_DOT = {"ok": "ok", "busy": "busy", "warn": "warn", "fault": "fault", "off": "off"}


class Lamp(QLabel):
    """燈號：色點加文字；`state` property 為亮燈時的狀態或 off。"""

    def __init__(self, text: str = "", lit_state: str = "ok", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("lamp")
        self.setTextFormat(Qt.TextFormat.RichText)
        self.caption = text
        self._lit_state = lit_state
        self._paint("off")

    @property
    def lit(self) -> bool:
        return self.property("state") != "off"

    def set_lit(self, on: bool) -> None:
        self._paint(self._lit_state if on else "off")

    def _paint(self, state: str) -> None:
        dot = color(_LAMP_DOT[state]).name()
        self.setText(f'<span style="color:{dot}">●</span>&nbsp;{self.caption}'.rstrip())
        set_state(self, state)


class SpeedStepper(QWidget):
    """速度：− 鍵、可直接輸入的數值、＋ 鍵；每按一次增減 5 %。數值在 `spin`。"""

    def __init__(self, value: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.spin = QSpinBox()
        self.spin.setObjectName("stepperValue")
        self.spin.setRange(SPEED_MIN, SPEED_MAX)
        self.spin.setSuffix(" %")
        self.spin.setSingleStep(SPEED_STEP)
        self.spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plain_spinbox(self.spin)
        self.spin.setValue(value)
        self.minus_button = named_button("−", "stepperButton")
        self.plus_button = named_button("+", "stepperButton")
        self.minus_button.setAutoRepeat(True)
        self.plus_button.setAutoRepeat(True)
        self.minus_button.clicked.connect(lambda: self.spin.stepBy(-1))
        self.plus_button.clicked.connect(lambda: self.spin.stepBy(1))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.minus_button)
        layout.addWidget(self.spin)
        layout.addWidget(self.plus_button)
