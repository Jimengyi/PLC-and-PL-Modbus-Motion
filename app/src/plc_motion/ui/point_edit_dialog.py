"""編輯此點：名稱、七軸座標與說明的大輸入框；視窗本身就是確認畫面。

改過的欄位標示原值；座標不是數字或超出該軸軟體極限時標紅，「確認存檔」不能按。
完全沒改也不能按。按下確認後由呼叫端寫回 positions.csv。
"""

import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import decimals_for
from plc_motion.settings.loader import AppConfig
from plc_motion.settings.positions import PositionRow
from plc_motion.ui.theme import set_state

AXIS_FIELDS = {1: "x", 2: "y", 3: "z", 4: "rx", 5: "ry", 6: "rz", 7: "lift"}


class PointEditDialog(QDialog):
    def __init__(self, config: AppConfig, row: PositionRow, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._row = row
        self.setWindowTitle(f"編輯 {row.point_id}")
        self.setModal(True)
        self.setMinimumWidth(600)

        header = QFrame()
        header.setObjectName("dialogHeader")
        header.setFixedHeight(44)
        head_layout = QHBoxLayout(header)
        head_layout.setContentsMargins(16, 0, 16, 0)
        head_layout.addWidget(QLabel(f"編輯 {row.point_id}"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.name_edit = self._text_field(row.name)
        grid.addWidget(_field_label("名稱"), 0, 0)
        grid.addWidget(self.name_edit, 0, 1)

        self.coord_edits: dict[int, QLineEdit] = {}
        self.coord_hints: dict[int, QLabel] = {}
        self._original: dict[int, str] = {}
        for i, n in enumerate(sorted(config.axes), start=1):
            point = config.points.axis_tag("Current Position", n)
            text = self._format(n, getattr(row, AXIS_FIELDS[n]))
            self._original[n] = text
            edit = QLineEdit(text)
            edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            edit.textChanged.connect(self._validate)
            edit.editingFinished.connect(lambda n=n: self._normalize(n))
            hint = QLabel()
            hint.setObjectName("fieldHint")
            hint.setMinimumWidth(170)
            grid.addWidget(_field_label(f"{config.axes[n].name}（{point.unit}）"), i, 0)
            grid.addWidget(edit, i, 1)
            grid.addWidget(hint, i, 2)
            self.coord_edits[n] = edit
            self.coord_hints[n] = hint
        last = len(config.axes) + 1
        self.description_edit = self._text_field(row.description)
        grid.addWidget(_field_label("說明"), last, 0)
        grid.addWidget(self.description_edit, last, 1)
        grid.setColumnStretch(1, 1)

        note = QLabel("直接輸入數值，Tab 跳下一格。改過的欄位會標示原值；超出軟體極限無法存檔。")
        note.setObjectName("hint")
        note.setWordWrap(True)
        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(10)
        body.addLayout(grid)
        body.addWidget(note)

        footer = QFrame()
        footer.setObjectName("dialogFooter")
        self.summary_label = QLabel()
        self.summary_label.setObjectName("hint")
        self.cancel_button = QPushButton("取消")
        self.save_button = QPushButton("確認存檔")
        self.save_button.setObjectName("primary")
        self.save_button.setAutoDefault(False)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        foot_layout = QHBoxLayout(footer)
        foot_layout.setContentsMargins(16, 10, 16, 12)
        foot_layout.addWidget(self.summary_label, 1)
        foot_layout.addWidget(self.cancel_button)
        foot_layout.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addLayout(body)
        layout.addWidget(footer)

        self._validate()
        first = self.coord_edits[min(self.coord_edits)]
        first.setFocus()
        first.selectAll()

    def _text_field(self, text: str) -> QLineEdit:
        edit = QLineEdit(text)
        edit.textChanged.connect(self._validate)
        return edit

    def _format(self, n: int, value: float) -> str:
        scale = self._config.points.axis_tag("Current Position", n).scale
        return f"{value:.{decimals_for(scale)}f}"

    def _parse(self, n: int) -> float | None:
        try:
            value = float(self.coord_edits[n].text().strip())
        except ValueError:
            return None
        return value if math.isfinite(value) else None

    def _normalize(self, n: int) -> None:
        value = self._parse(n)
        if value is not None:
            self.coord_edits[n].setText(self._format(n, value))

    def _validate(self) -> None:
        changed = bad = 0
        for n, edit in self.coord_edits.items():
            axis = self._config.axes[n]
            value = self._parse(n)
            hint = self.coord_hints[n]
            in_range = value is not None and axis.soft_limit_min <= value <= axis.soft_limit_max
            limits = f"{axis.soft_limit_min:g} ~ {axis.soft_limit_max:g}"
            if not in_range:
                bad += 1
                state = "fault"
                hint.setText("不是數字" if value is None else f"超出範圍 {limits}")
            elif self._format(n, value) != self._original[n]:
                changed += 1
                state = "warn"
                hint.setText(f"原值 {self._original[n]}")
            else:
                state = "off"
                hint.setText(f"範圍 {limits}")
            set_state(edit, state)
            set_state(hint, state)
        for edit, original in (
            (self.name_edit, self._row.name),
            (self.description_edit, self._row.description),
        ):
            is_changed = edit.text() != original
            changed += is_changed
            set_state(edit, "warn" if is_changed else "off")
        if bad:
            self.summary_label.setText(f"{bad} 格不是數字或超出範圍")
        else:
            self.summary_label.setText(f"已修改 {changed} 格" if changed else "尚未修改")
        self.save_button.setEnabled(bad == 0 and changed > 0)

    def values(self) -> dict[str, object]:
        """確認後的欄位值，給 `PositionTable.update(point_id, **values)`。"""
        out: dict[str, object] = {
            "name": self.name_edit.text(),
            "description": self.description_edit.text(),
        }
        for n in self.coord_edits:
            field = AXIS_FIELDS[n]
            value = self._parse(n)
            # 沒改的欄位沿用原值（原值可能比顯示的位數更精細，例如匯入的檔案）
            unchanged = value is not None and self._format(n, value) == self._original[n]
            out[field] = getattr(self._row, field) if unchanged else value
        return out


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label
