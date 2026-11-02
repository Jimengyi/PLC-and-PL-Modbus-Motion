"""步進面板與按住式 JOG 鍵（spec FR-27、FR-32、FR-33）。

用語：「步進」＝按一次走一個步距（以 FR-19 換算成 Code 2）；「JOG」＝按住連續移動。

`JogButton` 是按住式按鈕：按下發 `jog_pressed`，放開發 `jog_released`。按住中滑鼠在鍵外放開、
所在視窗失去焦點、最小化或隱藏（含縮到系統匣、切換頁面）、按鈕被反灰，都視同放開（FR-27）。
沒有按住時以上情況都不發 signal——結束流程靠這點只對按住中的鍵寫 0。

`JogPad` 的步距是七軸各一個輸入框，OP 可直接輸入；長／中／短一鍵帶入 axes.toml 的值，改過任一格
即切到「自訂」。自訂值只在本次執行有效，不寫回 axes.toml。輸入範圍為大於 0（最小一個解析度）、
不超過該軸軟體極限行程。步進鍵直接呼叫 worker 的 `step`，不另外確認；速度與 JOG 共用。
"""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QHideEvent, QMouseEvent, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from plc_motion.codec import decimals_for
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.widgets import (
    SPEED_MAX,
    SPEED_MIN,
    Panel,
    SpeedStepper,
    hint,
    named_button,
    plain_spinbox,
    segment_button,
)
from plc_motion.worker import EngineStatus

__all__ = [
    "SPEED_MAX",
    "SPEED_MIN",
    "JogButton",
    "JogPad",
    "format_value",
    "lock_tooltip",
    "segment_button",
]

DISTANCES = (("long", "長"), ("medium", "中"), ("short", "短"))
CUSTOM = "custom"
VERTICAL_AXES = (3, 7)  # Z 與升降軸：鍵面用 ▲▼
LIFT_AXIS = 7


def lock_tooltip(snapshot: Snapshot | None, status: EngineStatus | None) -> str | None:
    """寫入控制項該反灰時回傳原因（給 tooltip），可寫入時回傳 None。"""
    if status is not None and status.locked:
        return "鎖定中：\n" + "\n".join(status.lock_reasons)
    if status is None or snapshot is None:
        return "尚未取得 PLC 狀態"
    return None


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


class JogPad(Panel):
    """步進面板（FR-32）：步距輸入、速度、X±／Y± 十字、Z 與升降上下、RX±、RY±、RZ±。"""

    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__("步進", "按一次走一個步距", parent)
        self._config = config
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self._filling = False
        self.step_buttons: dict[tuple[int, int], QPushButton] = {}
        self.distance_fields: dict[int, QDoubleSpinBox] = {}
        self.distance_buttons: dict[str, QPushButton] = {}

        # 步距：長／中／短／自訂（自訂只在手動改過步距後亮起，不能直接點）
        distance_row = QHBoxLayout()
        distance_row.setSpacing(0)
        distance_row.addWidget(hint("步距"))
        distance_row.addSpacing(10)
        group = QButtonGroup(self)
        for key, text in (*DISTANCES, (CUSTOM, "自訂")):
            button = segment_button(text)
            group.addButton(button)
            distance_row.addWidget(button)
            self.distance_buttons[key] = button
            if key != CUSTOM:
                button.clicked.connect(lambda _=False, k=key: self.apply_preset(k))
        self.distance_buttons[CUSTOM].setEnabled(False)
        self.distance_buttons[CUSTOM].setToolTip("改過任一軸的步距後自動切到自訂")
        distance_row.addStretch(1)

        fields = QGridLayout()
        fields.setHorizontalSpacing(6)
        fields.setVerticalSpacing(6)
        for i, n in enumerate(sorted(config.axes)):
            fields.addWidget(self._distance_field(n), i // 4, i % 4)

        self.speed_stepper = SpeedStepper(min(a.default_speed for a in config.axes.values()))
        self.speed = self.speed_stepper.spin
        self.speed.setToolTip("步進與 JOG 共用的速度")
        speed_row = QHBoxLayout()
        speed_row.addWidget(hint("速度"))
        speed_row.addWidget(self.speed_stepper)
        speed_row.addWidget(hint("步進與 JOG 共用"))
        speed_row.addStretch(1)

        keys = QGridLayout()
        keys.setHorizontalSpacing(6)
        keys.setVerticalSpacing(6)
        # XY 十字
        keys.addWidget(self._key(2, +1), 0, 1)
        keys.addWidget(self._key(1, -1), 1, 0)
        keys.addWidget(_center_label("X / Y"), 1, 1)
        keys.addWidget(self._key(1, +1), 1, 2)
        keys.addWidget(self._key(2, -1), 2, 1)
        keys.setColumnMinimumWidth(3, 14)
        # Z、升降：上下
        for col, n in ((4, 3), (6, LIFT_AXIS)):
            keys.addWidget(self._key(n, +1), 0, col)
            keys.addWidget(_center_label(config.axes[n].name), 1, col)
            keys.addWidget(self._key(n, -1), 2, col)
        keys.setColumnMinimumWidth(5, 14)
        # RX、RY、RZ：一軸一列
        rotations = QGridLayout()
        rotations.setHorizontalSpacing(6)
        rotations.setVerticalSpacing(6)
        for row, n in enumerate((4, 5, 6)):
            rotations.addWidget(self._key(n, -1), row, 0)
            rotations.addWidget(_center_label(config.axes[n].name), row, 1)
            rotations.addWidget(self._key(n, +1), row, 2)
        rotations.setColumnStretch(1, 1)

        units = ", ".join(
            f"{config.axes[n].name} {config.points.axis_tag('Current Position', n).unit}"
            for n in sorted(config.axes)
        )
        fields_hint = hint(f"步距可直接輸入，按 Enter 套用（{units}）")
        fields_hint.setWordWrap(True)
        self.body_layout.addLayout(distance_row)
        self.body_layout.addLayout(fields)
        self.body_layout.addWidget(fields_hint)
        self.body_layout.addLayout(speed_row)
        self.body_layout.addLayout(keys)
        self.body_layout.addLayout(rotations)
        self.body_layout.addStretch(1)

        worker.snapshot_ready.connect(self.on_snapshot)
        self.apply_preset("short")
        self._update_enabled()

    # ---- 版面 ----

    def _distance_field(self, n: int) -> QWidget:
        axis = self._config.axes[n]
        point = self._config.points.axis_tag("Current Position", n)
        field = QDoubleSpinBox()
        field.setObjectName("stepField")
        plain_spinbox(field)
        field.setKeyboardTracking(False)  # Enter 或離開輸入框才套用
        field.setDecimals(decimals_for(point.scale))
        field.setRange(1 / point.scale, axis.soft_limit_max - axis.soft_limit_min)
        field.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        field.setToolTip(
            f"{axis.name} 步距（{point.unit}），範圍 {field.minimum():g} ~ {field.maximum():g}"
        )
        field.valueChanged.connect(self._on_distance_edited)
        self.distance_fields[n] = field
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        name = QLabel(axis.name)
        name.setObjectName("fieldLabel")
        layout.addWidget(name)
        layout.addWidget(field, 1)
        return box

    def _key(self, n: int, direction: int) -> QPushButton:
        name = self._config.axes[n].name
        if n in VERTICAL_AXES:
            text = f"{name} {'▲' if direction > 0 else '▼'}"
        else:
            text = f"{name}{'+' if direction > 0 else '−'}"
        button = named_button(text, "stepKey")
        button.clicked.connect(lambda: self._step(n, direction))
        self.step_buttons[(n, direction)] = button
        return button

    # ---- 步距 ----

    def apply_preset(self, key: str) -> None:
        """長／中／短：把 axes.toml 的步距填入七軸輸入框。"""
        self._filling = True
        try:
            for n, field in self.distance_fields.items():
                axis = self._config.axes[n]
                value = {"long": axis.step_long, "medium": axis.step_medium}.get(
                    key, axis.step_short
                )
                field.setValue(value)
        finally:
            self._filling = False
        self.distance_buttons[key].setChecked(True)

    def _on_distance_edited(self) -> None:
        if not self._filling:
            self.distance_buttons[CUSTOM].setChecked(True)

    def distance_key(self) -> str:
        return next(k for k, b in self.distance_buttons.items() if b.isChecked())

    def distance(self, n: int) -> float:
        return self.distance_fields[n].value()

    def _step(self, n: int, direction: int) -> None:
        self._worker.step(n, self.distance(n) * direction, self.speed.value())

    # ---- worker signal ----

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


def _center_label(text: str) -> QLabel:
    label = hint(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label
