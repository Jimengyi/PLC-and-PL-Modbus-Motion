"""文字型視窗：設定錯誤（FR-8）、操作事件與訊息紀錄、未預期例外（FR-50）。"""

import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from plc_motion.ui.theme import set_state

log = logging.getLogger("plc_motion.ui")


class TextDialog(QDialog):
    """標題列＋一行粗體標題＋唯讀多行內容＋關閉鈕。"""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(640, 360)
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        self.heading.setWordWrap(True)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("關閉")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.body, 1)
        layout.addWidget(buttons)

    def set_content(self, heading: str, text: str, state: str = "") -> None:
        self.heading.setText(heading)
        set_state(self.heading, state)
        self.body.setPlainText(text)

    def append(self, text: str) -> None:
        self.body.appendPlainText(text)

    def text(self) -> str:
        return f"{self.heading.text()}\n{self.body.toPlainText()}"

    def popup(self) -> None:
        """非 modal 顯示並帶到前面；已開著時只更新內容。"""
        self.show()
        self.raise_()
        self.activateWindow()


class ConfigErrorDialog(TextDialog):
    def __init__(self, errors: list[str], config_dir: Path, parent: QWidget | None = None):
        super().__init__("設定檔錯誤", parent)
        self.setModal(True)
        self.set_content(
            f"設定資料夾 {config_dir} 有 {len(errors)} 項錯誤，請修正後重新啟動。程式將結束。",
            "\n".join(f"{i}. {e}" for i, e in enumerate(errors, 1)),
            "fault",
        )


class ExceptionReporter:
    """接管 sys.excepthook：UI 執行緒的未預期例外寫日誌並顯示錯誤視窗，程式繼續執行。

    多次例外累加在同一個視窗，避免連續例外時視窗洗版。
    """

    def __init__(self, parent: QWidget | None = None):
        self._parent = parent
        self._previous = None
        self.dialog: TextDialog | None = None

    def install(self) -> None:
        self._previous = sys.excepthook
        sys.excepthook = self._hook

    def uninstall(self) -> None:
        if sys.excepthook is self._hook:
            sys.excepthook = self._previous or sys.__excepthook__

    def _hook(self, exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            (self._previous or sys.__excepthook__)(exc_type, exc, tb)
            return
        log.error("UI 執行緒發生未預期的例外", exc_info=(exc_type, exc, tb))
        try:
            self._show("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            log.exception("顯示例外視窗時發生例外")

    def _show(self, text: str) -> None:
        if self.dialog is None:
            self.dialog = TextDialog("未預期的錯誤", self._parent)
            self.dialog.set_content(
                "程式發生未預期的錯誤，已寫入日誌。程式仍可繼續操作；若狀況持續請重新啟動。",
                "",
                "fault",
            )
        elif self.dialog.body.toPlainText():
            self.dialog.append("-" * 40)
        self.dialog.append(text.rstrip())
        self.dialog.popup()
