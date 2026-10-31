"""步進鍵盤與按住式 JOG 鍵（spec FR-27、FR-32、FR-33）。

用語：「步進」＝按一次走一個步距（以 FR-19 換算成 Code 2）；「JOG」＝按住連續移動。

`JogButton` 是按住式按鈕：按下發 `jog_pressed`，放開發 `jog_released`。按住中滑鼠在鍵外放開、
所在視窗失去焦點、最小化或隱藏（含縮到系統匣、切換頁面）、按鈕被反灰，都視同放開（FR-27）。
沒有按住時以上情況都不發 signal——結束流程靠這點只對按住中的鍵寫 0。

`JogPad` 的步進鍵直接呼叫 worker 的 `step`；速度欄與選定軸區塊的 JOG 鍵共用。
"""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QHideEvent, QMouseEvent, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import decimals_for
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.worker import EngineStatus

SPEED_MIN, SPEED_MAX = 1, 100
DISTANCES = (("long", "長"), ("medium", "中"), ("short", "短"))
VERTICAL_AXES = (3, 7)  # Z 與升降軸：鍵面用 ↑↓
LIFT_AXIS = 7


def lock_tooltip(snapshot: Snapshot | None, status: EngineStatus | None) -> str | None:
    """寫入控制項該反灰時回傳原因（給 tooltip），可寫入時回傳 None。"""
    if status is not None and status.locked:
        return "鎖定中：\n" + "\n".join(status.lock_reasons)
    if status is None or snapshot is None:
        return "尚未取得 PLC 狀態"
    return None


def segment_button(text: str) -> QPushButton:
    """單選用的可勾選按鈕（放進 exclusive QButtonGroup）。"""
    button = QPushButton(text)
    button.setObjectName("segment")
    button.setCheckable(True)
    return button


def format_value(value: float, scale: int, unit: str | None = None) -> str:
    text = f"{value:.{decimals_for(scale)}f}"
    return f"{text} {unit}" if unit else text


class JogButton(QPushButton):
    """按住式按鈕；不接受鍵盤焦點，避免以空白鍵按住後失焦卡住。"""

    jog_pressed = Signal()
    jog_released = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._held = False
        self._watched: QWidget | None = None

    @property
    def is_held(self) -> bool:
        return self._held

    def release(self) -> None:
        """按住中才放開並發 `jog_released`；沒有按住時什麼都不做。"""
        if not self._held:
            return
        self._held = False
        self.setDown(False)
        self.jog_released.emit()

    # ---- 滑鼠：不走 QAbstractButton 的 click 流程 ----

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._held:
            event.ignore()
            return
        self._held = True
        self.setDown(True)
        event.accept()
        self.jog_pressed.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # 按住後滑鼠被本鍵 grab，在鍵外放開也會送到這裡
        if event.button() == Qt.MouseButton.LeftButton:
            self.release()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # QAbstractButton 移出鍵外會發 released、移回來再發 pressed；JOG 不要這種行為
        event.accept()

    # ---- 視同放開 ----

    def hideEvent(self, event: QHideEvent) -> None:
        self.release()
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.release()
        super().changeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        window = self.window()
        if window is not self._watched:
            if self._watched is not None:
                self._watched.removeEventFilter(self)
            window.installEventFilter(self)
            self._watched = window

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._watched and self._held:
            kind = event.type()
            if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide):
                self.release()
            elif kind == QEvent.Type.WindowStateChange and self._watched.isMinimized():
                self.release()
        return False


class JogPad(QGroupBox):
    """步進鍵盤（FR-32）：X±、Y± 十字、Z 上下、升降上下、RX±、RY±、RZ±；長／中／短步距。"""

    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__("步進（按一次走一個步距）", parent)
        self._config = config
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self.step_buttons: dict[tuple[int, int], QPushButton] = {}
        self.distance_labels: dict[int, QLabel] = {}
        self.distance_buttons: dict[str, QPushButton] = {}

        self.speed = QSpinBox()
        self.speed.setRange(SPEED_MIN, SPEED_MAX)
        self.speed.setSuffix(" %")
        self.speed.setValue(min(a.default_speed for a in config.axes.values()))
        self.speed.setToolTip("步進與 JOG 共用的速度")

        options = QHBoxLayout()
        options.addWidget(QLabel("步距"))
        group = QButtonGroup(self)
        for key, text in DISTANCES:
            button = segment_button(text)
            button.toggled.connect(self._update_distance_labels)
            group.addButton(button)
            options.addWidget(button)
            self.distance_buttons[key] = button
        self.distance_buttons["short"].setChecked(True)
        options.addSpacing(16)
        options.addWidget(QLabel("速度（步進與 JOG 共用）"))
        options.addWidget(self.speed)
        options.addStretch(1)

        keys = QGridLayout()
        keys.setHorizontalSpacing(8)
        keys.setVerticalSpacing(6)
        # XY 十字
        keys.addWidget(self._key(2, +1), 0, 1)
        keys.addWidget(self._key(1, -1), 1, 0)
        keys.addLayout(self._labels(1, 2), 1, 1)
        keys.addWidget(self._key(1, +1), 1, 2)
        keys.addWidget(self._key(2, -1), 2, 1)
        keys.setColumnMinimumWidth(3, 16)
        # Z、升降：上下
        for col, n in ((4, 3), (5, LIFT_AXIS)):
            keys.addWidget(self._key(n, +1), 0, col)
            keys.addLayout(self._labels(n), 1, col)
            keys.addWidget(self._key(n, -1), 2, col)
        keys.setColumnMinimumWidth(6, 16)
        # RX、RY、RZ：一軸一列
        for row, n in enumerate((4, 5, 6)):
            keys.addWidget(self._key(n, +1), row, 7)
            keys.addLayout(self._labels(n), row, 8)
            keys.addWidget(self._key(n, -1), row, 9)
        keys.setColumnStretch(10, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(options)
        layout.addLayout(keys)
        layout.addStretch(1)

        worker.snapshot_ready.connect(self.on_snapshot)
        self._update_distance_labels()
        self._update_enabled()

    def _key(self, n: int, direction: int) -> QPushButton:
        name = self._config.axes[n].name
        if n in VERTICAL_AXES:
            text = f"{name}{'↑' if direction > 0 else '↓'}"
        else:
            text = f"{name}{'+' if direction > 0 else '−'}"
        button = QPushButton(text)
        button.setMinimumSize(64, 36)
        button.clicked.connect(lambda: self._step(n, direction))
        self.step_buttons[(n, direction)] = button
        return button

    def _labels(self, *axes: int) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(0)
        for n in axes:
            label = QLabel()
            label.setObjectName("stepDistance")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(f"{self._config.axes[n].name} 目前步距")
            box.addWidget(label)
            self.distance_labels[n] = label
        return box

    def distance_key(self) -> str:
        return next(k for k, b in self.distance_buttons.items() if b.isChecked())

    def distance(self, n: int) -> float:
        axis = self._config.axes[n]
        return {
            "long": axis.step_long,
            "medium": axis.step_medium,
            "short": axis.step_short,
        }[self.distance_key()]

    def _step(self, n: int, direction: int) -> None:
        self._worker.step(n, self.distance(n) * direction, self.speed.value())

    def _update_distance_labels(self) -> None:
        if not any(b.isChecked() for b in self.distance_buttons.values()):
            return  # 切換途中舊鍵先取消勾選
        for n, label in self.distance_labels.items():
            point = self._config.points.axis_tag("Current Position", n)
            label.setText(format_value(self.distance(n), point.scale, point.unit))

    def on_snapshot(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self._snapshot = snapshot
        self._status = status
        self._update_enabled()

    def _update_enabled(self) -> None:
        tip = lock_tooltip(self._snapshot, self._status)
        for (n, _), button in self.step_buttons.items():
            reason = tip
            if reason is None and not self._snapshot.axis(n).home_finish:
                name = self._config.axes[n].name
                reason = f"第 {n} 軸（{name}）尚未完成原點復歸（Find Home Finish = 0）"
            button.setEnabled(reason is None)
            button.setToolTip(reason or "")
