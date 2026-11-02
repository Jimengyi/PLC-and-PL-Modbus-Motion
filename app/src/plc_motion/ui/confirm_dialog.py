"""寫入前的確認視窗：列出各軸「目前 → 目標」，預設焦點在「取消」。

呼叫端一律經由 `confirm(...)`（模組屬性），測試以 monkeypatch 取代它來自動回答。
"""

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from plc_motion.ui.theme import set_state


@dataclass(frozen=True)
class ConfirmRow:
    axis: str
    current: str
    target: str
    unit: str = ""


class ConfirmDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        lines: tuple[str, ...] = (),
        rows: tuple[ConfirmRow, ...] = (),
        rows_head: str = "目標",
        note: str = "",
        ok_text: str = "確認",
        danger: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)

        header = QFrame()
        header.setObjectName("dialogHeader")
        header.setFixedHeight(44)
        heading = QLabel(("⚠ " if danger else "") + title)
        head_layout = QHBoxLayout(header)
        head_layout.setContentsMargins(16, 0, 16, 0)
        head_layout.addWidget(heading)
        set_state(header, "fault" if danger else "off")

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(10)
        for line in lines:
            label = QLabel(line)
            label.setWordWrap(True)
            body.addWidget(label)
        if rows:
            body.addWidget(self._rows_box(rows, rows_head))
        if note:
            note_label = QLabel(note)
            note_label.setObjectName("hint")
            note_label.setWordWrap(True)
            body.addWidget(note_label)

        footer = QFrame()
        footer.setObjectName("dialogFooter")
        self.cancel_button = QPushButton("取消")
        self.ok_button = QPushButton(ok_text)
        self.ok_button.setObjectName("dangerFill" if danger else "primary")
        self.ok_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)
        foot_layout = QHBoxLayout(footer)
        foot_layout.setContentsMargins(16, 10, 16, 12)
        foot_layout.addStretch(1)
        foot_layout.addWidget(self.cancel_button)
        foot_layout.addWidget(self.ok_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addLayout(body)
        layout.addWidget(footer)
        self.cancel_button.setFocus()

    @staticmethod
    def _rows_box(rows: tuple[ConfirmRow, ...], head: str) -> QFrame:
        box = QFrame()
        box.setObjectName("infoBox")
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 8, 12, 8)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for col, text in enumerate(("軸", "目前", "", head)):
            label = QLabel(text)
            label.setObjectName("hint")
            grid.addWidget(label, 0, col, right if col in (1, 3) else Qt.AlignmentFlag.AlignLeft)
        for i, row in enumerate(rows, start=1):
            grid.addWidget(QLabel(row.axis), i, 0)
            grid.addWidget(QLabel(row.current), i, 1, right)
            arrow = QLabel("→")
            arrow.setObjectName("hint")
            grid.addWidget(arrow, i, 2, Qt.AlignmentFlag.AlignCenter)
            target = QLabel(f"<b>{row.target}</b> {row.unit}".rstrip())
            grid.addWidget(target, i, 3, right)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return box


def confirm(
    parent: QWidget | None,
    title: str,
    *,
    lines: tuple[str, ...] = (),
    rows: tuple[ConfirmRow, ...] = (),
    rows_head: str = "目標",
    note: str = "",
    ok_text: str = "確認",
    danger: bool = False,
) -> bool:
    """顯示確認視窗；按確認回傳 True，取消、Esc 或關閉回傳 False。"""
    dialog = ConfirmDialog(parent, title, lines, rows, rows_head, note, ok_text, danger)
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
