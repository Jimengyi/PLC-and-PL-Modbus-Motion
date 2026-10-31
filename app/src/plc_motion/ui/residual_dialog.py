"""殘留交握視窗（FR-57）：連線後讀到上次斷線留下的交握時，讓使用者決定寫回 0 或維持鎖定。"""

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import decimals_for
from plc_motion.engine import ResidualInfo
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.theme import set_state

WARNING = "寫回 0 後，PLC 可能開始執行斷線前的命令；若不要執行該命令，請先按急停"


class ResidualDialog(QDialog):
    """非 modal。「寫回 0」呼叫 worker.clear_residual；「先不處理」只關閉視窗，維持鎖定。"""

    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._worker = worker
        self.setWindowTitle("偵測到殘留交握")
        self.resize(640, 400)
        self.heading = QLabel("連線時發現上次斷線留下的交握，所有寫入已鎖定")
        self.heading.setObjectName("heading")
        self.heading.setWordWrap(True)
        self.warning = QLabel(WARNING)
        self.warning.setWordWrap(True)
        set_state(self.warning, "fault")
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.clear_button = QPushButton("寫回 0")
        self.keep_button = QPushButton("先不處理，保持鎖定")
        self.clear_button.clicked.connect(self._clear)
        self.keep_button.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.clear_button)
        row.addWidget(self.keep_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.warning)
        layout.addWidget(self.body, 1)
        layout.addLayout(row)

    def set_residual(self, infos: tuple[ResidualInfo, ...]) -> None:
        self.body.setPlainText("\n".join(self._line(i) for i in infos))

    def _line(self, info: ResidualInfo) -> str:
        axis = self._config.axes.get(info.axis)
        name = f"（{axis.name}）" if axis is not None else ""
        tag = self._config.points.axis_tag("Target Position", info.axis)
        target = f"{info.target_position:.{decimals_for(tag.scale)}f}"
        unit = f" {tag.unit}" if tag.unit else ""
        return (
            f"第 {info.axis} 軸{name}：Request = {int(info.request)}，"
            f"PC Action Code = {info.pc_action_code}，Target Position = {target}{unit}"
        )

    def text(self) -> str:
        return f"{self.heading.text()}\n{self.warning.text()}\n{self.body.toPlainText()}"

    def popup(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _clear(self) -> None:
        self.accept()
        self._worker.clear_residual()
