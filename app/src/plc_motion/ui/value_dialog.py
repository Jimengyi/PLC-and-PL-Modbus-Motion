"""IO Monitor 數值點的寫入視窗（spec FR-39；多型別依 modbus-tags-migration FR-30、FR-31）。

輸入不合法時視窗不關閉並顯示原因。回傳給 `write_point` 的值：

- 必要 Tag：各軸 Speed 為 1 ~ 100 的 int；Position（Current／Target Position）以實際值輸入，
  依倍率換算成 PLC 原始值 int（四捨五入，換算後須在 I32 範圍內）。
- 其他有 `scale` 的點：工程值 float（函式庫寫入時乘上倍率）。
- 整數型別（U16、I16、U32、I32）：int；`F32`：float；`String`：str（ASCII，長度上限為
  Address Length × 2）。

範圍以函式庫的 `datatypes.encode` 檢查，與實際寫入的換算（倍率、四捨五入、型別範圍）一致。
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

from lib.modbus_tags import TagInfo, ValueOutOfRange
from lib.modbus_tags.datatypes import encode
from plc_motion.codec import I32_MAX, I32_MIN, to_raw
from plc_motion.settings.io_points import AXIS_COUNT, IoPoint, is_position, required_tag_types
from plc_motion.ui.jog_pad import SPEED_MAX, SPEED_MIN

_SPEED_TAGS = frozenset(f"Speed {n}" for n in range(1, AXIS_COUNT + 1))
_REQUIRED_TAGS = frozenset(required_tag_types())
_INTEGER_RANGES = {
    "U16": (0, 2**16 - 1),
    "I16": (-(2**15), 2**15 - 1),
    "U32": (0, 2**32 - 1),
    "I32": (I32_MIN, I32_MAX),
}
F32_DECIMALS = 4  # F32 顯示的小數位數（固定，重新整理時不跳動）
_MAX_DECIMALS = 4  # 倍率無法以有限小數表示解析度時（例如 3）的小數位數


def value_decimals(point: IoPoint) -> int | None:
    """顯示工程值的小數位數；不需小數（整數型別無倍率、String、Bool）時回傳 None。

    整數型別有倍率時，取能完整表示解析度 1 / scale 的最少位數（10000 → 4、2.5 → 1）。
    """
    if point.data_type == "F32":
        return F32_DECIMALS
    if point.data_type not in _INTEGER_RANGES or not point.scale:
        return None
    for d in range(_MAX_DECIMALS + 1):
        steps = 10**d / point.scale
        if abs(steps - round(steps)) < 1e-9:
            return d
    return _MAX_DECIMALS


def format_point_value(point: IoPoint, value: object) -> str:
    """數值頁「值」欄文字：有倍率或 F32 以固定小數位數顯示，有單位時加上單位。"""
    if point.data_type == "String":
        return str(value)
    decimals = value_decimals(point)
    text = str(int(value)) if decimals is None else f"{value:.{decimals}f}"
    return f"{text} {point.unit}" if point.unit else text


def _tag_info(point: IoPoint) -> TagInfo:
    """以 IoPoint 組出函式庫的 TagInfo，交給 `encode` 檢查範圍。"""
    return TagInfo(
        name=point.tag,
        display_name=point.display_name,
        description=point.description,
        data_model=point.data_model,
        data_type=point.data_type,
        access=point.access,
        address=point.start_address,
        bit=point.bit,
        length=point.length,
        unit_id=point.unit_id,
        scale=point.scale,
        unit=point.unit or "",
        invert=point.invert,
        word_order=point.word_order,
        byte_swap=point.byte_swap,
        extra={},
    )


def range_text(point: IoPoint) -> str:
    if point.tag in _SPEED_TAGS:
        return f"{SPEED_MIN} ~ {SPEED_MAX}"
    if point.data_type == "String":
        return f"最多 {point.length * 2} 個 ASCII 字元"
    if point.data_type == "F32":
        return "單精度浮點數"
    lo, hi = _INTEGER_RANGES[point.data_type]
    if point.scale:
        lo_text = format_point_value(point, lo / point.scale).removesuffix(f" {point.unit}")
        hi_text = format_point_value(point, hi / point.scale)
        return f"{lo_text} ~ {hi_text}"
    return f"{lo} ~ {hi}"


def _parse_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"「{text}」不是數字") from None
    if not math.isfinite(value):
        raise ValueError(f"「{text}」不是有限數值")
    return value


def _parse_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"「{text}」不是整數") from None


def _check_encode(point: IoPoint, value: object) -> None:
    try:
        encode(_tag_info(point), value)
    except ValueOutOfRange:
        raise ValueError(f"超出範圍 {range_text(point)}") from None


def parse_input(point: IoPoint, text: str) -> int | float | str:
    """輸入文字 → 要交給 `write_point` 的值；不合法時 raise ValueError（訊息給使用者看）。"""
    if point.data_type == "String":
        if not text.isascii():
            raise ValueError("只能輸入 ASCII 字元（英數與符號）")
        _check_encode(point, text)
        return text
    text = text.strip()
    if point.tag in _SPEED_TAGS:
        value = _parse_int(text)
        if not SPEED_MIN <= value <= SPEED_MAX:
            raise ValueError(f"超出範圍 {range_text(point)}")
        return value
    if is_position(point) and point.tag in _REQUIRED_TAGS:
        value = _parse_float(text)
        try:
            return to_raw(value, point.scale)
        except ValueError:
            raise ValueError(f"超出範圍 {range_text(point)}") from None
    if point.scale or point.data_type == "F32":
        value = _parse_float(text)
    else:
        value = _parse_int(text)
    _check_encode(point, value)
    return value


def current_text(point: IoPoint, current: object) -> str:
    if current is None:
        return "—"
    return format_point_value(point, current)


def edit_text(point: IoPoint, current: object) -> str:
    """輸入框的初始文字：與顯示相同的位數，但不帶單位；F32 取 7 位有效數字避免截掉精度。"""
    if point.data_type == "String":
        return str(current)
    if point.data_type == "F32":
        return f"{current:.7g}"
    decimals = value_decimals(point)
    return str(int(current)) if decimals is None else f"{current:.{decimals}f}"


class ValueDialog(QDialog):
    """`exec()` 回傳 Accepted 時，`value` 為要交給 `write_point` 的值。"""

    def __init__(self, point: IoPoint, current: object, parent: QWidget | None = None):
        super().__init__(parent)
        self._point = point
        self.value: int | float | str | None = None
        self.setWindowTitle(f"寫入 {point.label}")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        heading = QLabel(f"寫入 {point.label}")
        heading.setObjectName("heading")
        self.info_label = QLabel(
            f"Tag：{point.tag}\n目前值 {current_text(point, current)}　範圍 {range_text(point)}"
        )
        self.value_edit = QLineEdit()
        if current is not None:
            self.value_edit.setText(edit_text(point, current))
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
            self.value = parse_input(self._point, self.value_edit.text())
        except ValueError as e:
            self.error_label.setText(str(e))
            return
        self.accept()
