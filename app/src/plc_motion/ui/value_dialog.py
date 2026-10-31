"""IO Monitor 數值點的寫入視窗（spec FR-39）。

範圍：U16 為 0 ~ 65535，各軸 Speed 為 1 ~ 100；Position 以實際值輸入，依倍率換算成 raw
（四捨五入，換算後須在 I32 範圍內）。輸入不合法時視窗不關閉並顯示原因。
"""

import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import I32_MAX, I32_MIN, decimals_for, to_raw
from plc_motion.settings.io_points import AXIS_COUNT, IoPoint, SensorType
from plc_motion.ui.jog_pad import SPEED_MAX, SPEED_MIN, format_value

U16_MIN, U16_MAX = 0, 65535
_SPEED_TAGS = frozenset(f"Speed {n}" for n in range(1, AXIS_COUNT + 1))


def integer_range(point: IoPoint) -> tuple[int, int]:
    """U16 點可寫入的範圍。"""
    if point.tag in _SPEED_TAGS:
        return SPEED_MIN, SPEED_MAX
    return U16_MIN, U16_MAX


def range_text(point: IoPoint) -> str:
    if point.sensor_type == SensorType.POSITION:
        lo = format_value(I32_MIN / point.scale, point.scale)
        hi = format_value(I32_MAX / point.scale, point.scale)
        return f"{lo} ~ {hi} {point.unit}"
    lo, hi = integer_range(point)
    return f"{lo} ~ {hi}"


def parse_input(point: IoPoint, text: str) -> int:
    """輸入文字 → 要寫入的 raw；不合法時 raise ValueError（訊息給使用者看）。"""
    text = text.strip()
    if point.sensor_type == SensorType.POSITION:
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"「{text}」不是數字") from None
        if not math.isfinite(value):
            raise ValueError(f"「{text}」不是有限數值")
        try:
            return to_raw(value, point.scale)
        except ValueError:
            raise ValueError(f"超出範圍 {range_text(point)}") from None
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"「{text}」不是整數") from None
    lo, hi = integer_range(point)
    if not lo <= value <= hi:
        raise ValueError(f"超出範圍 {range_text(point)}")
    return value


def current_text(point: IoPoint, current: float | int | None) -> str:
    if current is None:
        return "—"
    if point.sensor_type == SensorType.POSITION:
        return format_value(current, point.scale, point.unit)
    return str(current)


class ValueDialog(QDialog):
    """`exec()` 回傳 Accepted 時，`raw` 為要寫入的值。"""

    def __init__(self, point: IoPoint, current: float | int | None, parent: QWidget | None = None):
        super().__init__(parent)
        self._point = point
        self.raw: int | None = None
        self.setWindowTitle(f"寫入 {point.label}")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        heading = QLabel(f"寫入 {point.label}")
        heading.setObjectName("heading")
        self.info_label = QLabel(
            f"Tag：{point.tag}\n目前值 {current_text(point, current)}　範圍 {range_text(point)}"
        )
        self.value_edit = QLineEdit()
        if current is not None:
            if point.sensor_type == SensorType.POSITION:
                self.value_edit.setText(f"{current:.{decimals_for(point.scale)}f}")
            else:
                self.value_edit.setText(str(current))
        self.value_edit.selectAll()
        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("新值"))
        edit_row.addWidget(self.value_edit, 1)
        if point.unit:
            edit_row.addWidget(QLabel(point.unit))
        self.error_label = QLabel()
        self.error_label.setObjectName("lockHint")
        self.error_label.setWordWrap(True)

        self.write_button = QPushButton("寫入")
        self.write_button.setDefault(True)
        self.write_button.clicked.connect(self._on_write)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.write_button)
        buttons.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.info_label)
        layout.addLayout(edit_row)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self.setMinimumWidth(360)

    def _on_write(self) -> None:
        try:
            self.raw = parse_input(self._point, self.value_edit.text())
        except ValueError as e:
            self.error_label.setText(str(e))
            return
        self.accept()
