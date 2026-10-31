"""plc-sim 的操作視窗（spec FR-51、FR-53、FR-56）。

左側是 FR-53 的操作（極限、原點感測器、急停、速度倍率、設定位置、注入 Alarm、兩種故障、
中斷所有連線），右側是 SimLog。對 PlcModel 的呼叫都包在 `server.lock` 內；SimLog 由
QTimer 在 UI 執行緒定時拉取，不在 server 執行緒的通知裡碰 UI。
"""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from plc_motion.sim.server import SimServer
from plc_motion.sim.sim_log import format_time

AXIS_COUNT = 7
REFRESH_MS = 200
_PER_AXIS = ("pos", "neg", "home", "no_reply", "no_copy", "persistent")


class SimWindow(QWidget):
    def __init__(self, server: SimServer, parent: QWidget | None = None):
        super().__init__(parent)
        self._server = server
        self._shown = 0
        self._state = {a: dict.fromkeys(_PER_AXIS, False) for a in range(1, AXIS_COUNT + 1)}
        self.setWindowTitle(f"模擬 PLC（{server.host}:{server.port}）")
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_log)
        self._timer.start(REFRESH_MS)

    # ---- 版面 ----

    def _build(self) -> None:
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        root.addLayout(left, 0)

        self.axis_combo = QComboBox()
        for a in range(1, AXIS_COUNT + 1):
            self.axis_combo.addItem(f"第 {a} 軸", a)
        self.axis_combo.currentIndexChanged.connect(self._load_axis_state)
        left.addWidget(self.axis_combo)

        axis_box = QGroupBox("選定軸")
        axis_lay = QVBoxLayout(axis_box)
        self.pos_limit_check = QCheckBox("正極限觸發")
        self.neg_limit_check = QCheckBox("負極限觸發")
        self.home_check = QCheckBox("原點感測器觸發")
        self.pos_limit_check.toggled.connect(lambda on: self._limit("pos", on))
        self.neg_limit_check.toggled.connect(lambda on: self._limit("neg", on))
        self.home_check.toggled.connect(lambda on: self._limit("home", on))
        for w in (self.pos_limit_check, self.neg_limit_check, self.home_check):
            axis_lay.addWidget(w)

        row = QHBoxLayout()
        self.position_spin = QDoubleSpinBox()
        self.position_spin.setRange(-1e6, 1e6)
        self.position_spin.setDecimals(4)
        self.position_button = QPushButton("設定位置")
        self.position_button.clicked.connect(self._set_position)
        row.addWidget(self.position_spin)
        row.addWidget(self.position_button)
        axis_lay.addLayout(row)

        row = QHBoxLayout()
        self.alarm_code_spin = QSpinBox()
        self.alarm_code_spin.setRange(0, 65535)
        self.alarm_persistent_check = QCheckBox("原因持續")
        self.alarm_persistent_check.toggled.connect(self._persistent_toggled)
        self.alarm_button = QPushButton("注入 Alarm")
        self.alarm_button.clicked.connect(self._inject_alarm)
        row.addWidget(self.alarm_code_spin)
        row.addWidget(self.alarm_persistent_check)
        row.addWidget(self.alarm_button)
        axis_lay.addLayout(row)

        self.no_reply_check = QCheckBox("故障：不回 Reply")
        self.no_copy_check = QCheckBox("故障：不複製 PLC Action Code")
        self.no_reply_check.toggled.connect(lambda _: self._fault_changed())
        self.no_copy_check.toggled.connect(lambda _: self._fault_changed())
        axis_lay.addWidget(self.no_reply_check)
        axis_lay.addWidget(self.no_copy_check)
        left.addWidget(axis_box)

        common = QGroupBox("全機")
        common_lay = QVBoxLayout(common)
        self.estop_check = QCheckBox("急停按下")
        self.estop_check.toggled.connect(self._estop)
        common_lay.addWidget(self.estop_check)
        row = QHBoxLayout()
        row.addWidget(QLabel("速度倍率"))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 100.0)
        self.speed_spin.setSingleStep(0.5)
        self.speed_spin.setValue(self._server.model.speed_factor)
        self.speed_spin.valueChanged.connect(self._speed)
        row.addWidget(self.speed_spin)
        common_lay.addLayout(row)
        self.disconnect_button = QPushButton("中斷所有連線")
        self.disconnect_button.clicked.connect(self._server.disconnect_all)
        common_lay.addWidget(self.disconnect_button)
        left.addWidget(common)
        left.addStretch()

        right = QVBoxLayout()
        root.addLayout(right, 1)
        self.read_stats_label = QLabel()
        right.addWidget(self.read_stats_label)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        right.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        export = QPushButton("匯出紀錄")
        export.clicked.connect(self._export_clicked)
        clear = QPushButton("清除畫面")
        clear.clicked.connect(self.log_view.clear)
        row.addStretch()
        row.addWidget(clear)
        row.addWidget(export)
        right.addLayout(row)
        self.resize(900, 520)
        self.refresh_log()

    # ---- 操作 ----

    @property
    def axis(self) -> int:
        return self.axis_combo.currentData()

    def _load_axis_state(self) -> None:
        st = self._state[self.axis]
        for check, key in (
            (self.pos_limit_check, "pos"),
            (self.neg_limit_check, "neg"),
            (self.home_check, "home"),
            (self.no_reply_check, "no_reply"),
            (self.no_copy_check, "no_copy"),
            (self.alarm_persistent_check, "persistent"),
        ):
            check.blockSignals(True)
            check.setChecked(st[key])
            check.blockSignals(False)

    def _limit(self, which: str, on: bool) -> None:
        self._state[self.axis][which] = on
        with self._server.lock:
            self._server.model.set_limit(self.axis, which, on)

    def _estop(self, on: bool) -> None:
        with self._server.lock:
            self._server.model.set_estop(on)

    def _speed(self, value: float) -> None:
        with self._server.lock:
            self._server.model.speed_factor = value

    def _set_position(self) -> None:
        with self._server.lock:
            self._server.model.set_position(self.axis, self.position_spin.value())

    def _inject_alarm(self) -> None:
        persistent = self.alarm_persistent_check.isChecked()
        with self._server.lock:
            self._server.model.inject_alarm(self.axis, self.alarm_code_spin.value(), persistent)

    def _persistent_toggled(self, on: bool) -> None:
        self._state[self.axis]["persistent"] = on
        with self._server.lock:
            self._server.model.set_persistent(self.axis, on)

    def _fault_changed(self) -> None:
        no_reply, no_copy = self.no_reply_check.isChecked(), self.no_copy_check.isChecked()
        self._state[self.axis].update(no_reply=no_reply, no_copy=no_copy)
        with self._server.lock:
            self._server.model.set_fault(self.axis, no_reply, no_copy)

    # ---- 紀錄 ----

    def refresh_log(self) -> None:
        log = self._server.log
        entries = log.entries()
        if len(entries) < self._shown:  # 上限截斷或重置後重新顯示
            self._shown = 0
            self.log_view.clear()
        for entry in entries[self._shown :]:
            self.log_view.appendPlainText(entry.format())
        self._shown = len(entries)
        last = log.last_read_time()
        last_text = "無" if last is None else format_time(last)
        self.read_stats_label.setText(f"讀取請求累計 {log.read_count()} 次，最近一次：{last_text}")

    def export_log(self, path: Path) -> None:
        self._server.log.export(path)

    def _export_clicked(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "匯出紀錄", "plc-sim.log", "紀錄 (*.log *.txt)")
        if name:
            self.export_log(Path(name))

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)
