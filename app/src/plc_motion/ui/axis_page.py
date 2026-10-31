"""軸控頁（spec FR-28 ~ FR-33、FR-16 反灰、FR-27 UI 面）。

上半部：七軸總覽（FR-28、FR-29）｜選定軸區塊（FR-30）＋全軸操作區（FR-31）。
下半部 `bottom_layout`：步進鍵盤 `JogPad`（FR-32）與其下方的點位操作區｜點位表 `PointsPanel`
（FR-34 ~ FR-36）。

命令一律呼叫 worker 的命令方法；畫面只依 `snapshot_ready` 更新。鎖定（`EngineStatus.locked`）或
尚未取得狀態時所有寫入控制項反灰，唯一例外是按住中的 JOG 鍵：JOG 按下本身就會造成鎖定，
而反灰會視同放開，所以按住中的鍵只在該方向極限觸發時反灰。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import I32_MAX, decimals_for
from plc_motion.plc.snapshot import AxisState, Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.jog_pad import (
    SPEED_MAX,
    SPEED_MIN,
    JogButton,
    JogPad,
    format_value,
    lock_tooltip,
    segment_button,
)
from plc_motion.ui.points_panel import PointsPanel
from plc_motion.ui.theme import set_state
from plc_motion.worker import EngineStatus

# 燈號：key → (顯示文字, 亮燈時的 state, 由 AxisState 取值)
_LAMPS = {
    "servo": ("Servo", "ok", lambda s: s.servo),
    "ready": ("Ready", "ok", lambda s: s.ready),
    "busy": ("Busy", "busy", lambda s: s.busy),
    "request": ("Request", "busy", lambda s: s.request),
    "reply": ("Reply", "busy", lambda s: s.reply),
    "home_finish": ("原點完成", "ok", lambda s: s.home_finish),
    "pos_limit": ("+Limit(B)", "fault", lambda s: s.pos_limit_triggered),
    "home_sensor": ("Home Sensor", "ok", lambda s: s.home_sensor),
    "neg_limit": ("−Limit(B)", "fault", lambda s: s.neg_limit_triggered),
}
OVERVIEW_LAMPS = ("servo", "ready", "busy", "home_finish", "pos_limit", "home_sensor", "neg_limit")
SELECTED_LAMPS = tuple(_LAMPS)

# 總覽欄寬（表頭與每列共用）
_COLUMNS = {
    "check": 28,
    "name": 56,
    "position": 130,
    "lamp": 30,
    "alarm": 56,
    "target": 130,
}
_PLACEHOLDER = "—"


def set_flag(widget: QWidget, name: str, value: bool) -> None:
    """設定給 QSS 選擇器用的 bool property 並重新套用樣式。"""
    if widget.property(name) is value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class Lamp(QLabel):
    """燈號：`●` 加文字，依 `state` property 著色（off 為暗）。"""

    def __init__(self, text: str = "", lit_state: str = "ok", parent: QWidget | None = None):
        super().__init__(f"● {text}".rstrip(), parent)
        self.setObjectName("lamp")
        self._lit_state = lit_state
        set_state(self, "off")

    def set_lit(self, on: bool) -> None:
        set_state(self, self._lit_state if on else "off")


class AxisRow(QFrame):
    """總覽中的一軸；點選整列（勾選框與目標值以外的位置）發 `clicked`。"""

    clicked = Signal(int)

    def __init__(self, config: AppConfig, n: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("axisRow")
        self.number = n
        point = config.points.axis_tag("Current Position", n)
        self.scale, self.unit = point.scale, point.unit

        self.check = QCheckBox()
        self.check.setToolTip("勾選此軸")
        self.name_label = QLabel(config.axes[n].name)
        self.name_label.setObjectName("axisName")
        self.position_label = QLabel(_PLACEHOLDER)
        self.position_label.setObjectName("value")
        self.position_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.lamps = {key: Lamp("", _LAMPS[key][1]) for key in OVERVIEW_LAMPS}
        for key, lamp in self.lamps.items():
            lamp.setToolTip(_LAMPS[key][0])
            lamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.alarm_label = QLabel(_PLACEHOLDER)
        self.alarm_label.setObjectName("value")
        self.alarm_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.target = QDoubleSpinBox()
        self.target.setDecimals(decimals_for(self.scale))
        limit = I32_MAX / self.scale
        self.target.setRange(-limit, limit)
        self.target.setSuffix(f" {self.unit}")
        self.target.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.target.setToolTip("目標值（絕對位置或相對移動量）")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(4)
        _add(layout, self.check, "check")
        _add(layout, self.name_label, "name")
        _add(layout, self.position_label, "position")
        for lamp in self.lamps.values():
            _add(layout, lamp, "lamp")
        _add(layout, self.alarm_label, "alarm")
        _add(layout, self.target, "target")
        layout.addStretch(1)
        set_state(self, "off")
        set_flag(self, "selected", False)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.number)
        super().mousePressEvent(event)

    def update_state(self, st: AxisState | None) -> None:
        if st is None:
            self.position_label.setText(_PLACEHOLDER)
            self.alarm_label.setText(_PLACEHOLDER)
            for lamp in self.lamps.values():
                lamp.set_lit(False)
            set_state(self, "off")
            return
        self.position_label.setText(format_value(st.position, self.scale, self.unit))
        self.alarm_label.setText(str(st.alarm_code))
        for key, lamp in self.lamps.items():
            lamp.set_lit(bool(_LAMPS[key][2](st)))
        set_state(self, "fault" if st.alarm_code else "ok")


def _add(layout: QHBoxLayout, widget: QWidget, column: str) -> None:
    widget.setFixedWidth(_COLUMNS[column])
    layout.addWidget(widget)


def _overview_header() -> QWidget:
    header = QFrame()
    header.setObjectName("axisHeader")
    layout = QHBoxLayout(header)
    layout.setContentsMargins(6, 0, 6, 0)
    layout.setSpacing(4)
    titles = [("勾選", "check"), ("軸", "name"), ("目前位置", "position")]
    titles += [(_short_lamp_title(k), "lamp") for k in OVERVIEW_LAMPS]
    titles += [("Alarm", "alarm"), ("目標值", "target")]
    for text, column in titles:
        label = QLabel(text)
        label.setObjectName("columnTitle")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _add(layout, label, column)
    layout.addStretch(1)
    return header


def _short_lamp_title(key: str) -> str:
    return {
        "servo": "Svo",
        "ready": "Rdy",
        "busy": "Bsy",
        "home_finish": "原點",
        "pos_limit": "+L",
        "home_sensor": "HS",
        "neg_limit": "−L",
    }[key]


class AxisPage(QWidget):
    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self.selected_axis = min(config.axes)
        self._jog_axis = self.selected_axis  # 按下時的軸：放開送同一軸

        overview = self._build_overview()
        selected = self._build_selected()
        all_axes = self._build_all_axes()
        self.jog_pad = JogPad(config, worker)
        self.points_panel = PointsPanel(config, worker)

        right = QVBoxLayout()
        right.addWidget(selected)
        right.addWidget(all_axes)
        right.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(overview, 3)
        top.addLayout(right, 2)

        # 左欄：步進鍵盤＋點位操作區；右欄：點位表（佔滿剩餘寬度，讓七軸座標欄都看得到）
        left = QVBoxLayout()
        left.addWidget(self.jog_pad, 0)
        left.addWidget(self.points_panel.take_ops_box(), 0)
        left.addStretch(1)
        self.bottom_layout = QHBoxLayout()
        self.bottom_layout.addLayout(left, 0)
        self.bottom_layout.addWidget(self.points_panel, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(self.bottom_layout, 1)

        worker.snapshot_ready.connect(self.on_snapshot)
        self.select_axis(self.selected_axis)

    # ---- 版面 ----

    def _build_overview(self) -> QGroupBox:
        box = QGroupBox("七軸總覽（點選一列為選定軸）")
        layout = QVBoxLayout(box)
        layout.setSpacing(3)
        layout.addWidget(_overview_header())
        self.rows: dict[int, AxisRow] = {}
        for n in sorted(self._config.axes):
            row = AxisRow(self._config, n)
            row.clicked.connect(self.select_axis)
            row.check.toggled.connect(self._update_controls)
            layout.addWidget(row)
            self.rows[n] = row

        self.absolute_button = segment_button("絕對")
        self.relative_button = segment_button("相對")
        self.absolute_button.setChecked(True)
        mode = QButtonGroup(box)
        mode.addButton(self.absolute_button)
        mode.addButton(self.relative_button)
        self.move_speed = QSpinBox()
        self.move_speed.setRange(SPEED_MIN, SPEED_MAX)
        self.move_speed.setSuffix(" %")
        self.move_speed.setValue(min(a.default_speed for a in self._config.axes.values()))
        self.move_button = QPushButton("▶ 執行勾選軸移動")
        self.move_button.clicked.connect(self._move_checked)
        self.home_checked_button = QPushButton("⌂ 勾選軸原點復歸")
        self.home_checked_button.clicked.connect(lambda: self._worker.home(self._checked_axes()))

        controls = QHBoxLayout()
        controls.addWidget(self.absolute_button)
        controls.addWidget(self.relative_button)
        controls.addSpacing(16)
        controls.addWidget(QLabel("速度"))
        controls.addWidget(self.move_speed)
        controls.addStretch(1)
        controls.addWidget(self.move_button)
        controls.addWidget(self.home_checked_button)
        layout.addSpacing(6)
        layout.addLayout(controls)
        layout.addStretch(1)
        return box

    def _build_selected(self) -> QGroupBox:
        box = QGroupBox("選定軸")
        self.selected_title = QLabel()
        self.selected_title.setObjectName("selectedTitle")
        self.selected_state = QLabel()
        self.selected_state.setObjectName("lamp")
        title = QHBoxLayout()
        title.addWidget(self.selected_title)
        title.addWidget(self.selected_state)
        title.addStretch(1)

        lamps = QGridLayout()
        lamps.setHorizontalSpacing(14)
        self.selected_lamps: dict[str, Lamp] = {}
        for i, key in enumerate(SELECTED_LAMPS):
            text, lit, _ = _LAMPS[key]
            lamp = Lamp(text, lit)
            lamps.addWidget(lamp, i // 3, i % 3)
            self.selected_lamps[key] = lamp

        self.plc_action_label = QLabel(_PLACEHOLDER)
        self.plc_action_label.setObjectName("value")
        self.alarm_code_label = QLabel(_PLACEHOLDER)
        self.alarm_code_label.setObjectName("value")
        codes = QHBoxLayout()
        codes.addWidget(QLabel("PLC Action Code"))
        codes.addWidget(self.plc_action_label)
        codes.addSpacing(20)
        codes.addWidget(QLabel("Alarm Code"))
        codes.addWidget(self.alarm_code_label)
        codes.addStretch(1)

        self.jog_minus = JogButton("◀ JOG −")
        self.jog_plus = JogButton("JOG ＋ ▶")
        for button, direction in ((self.jog_minus, -1), (self.jog_plus, +1)):
            button.setMinimumHeight(44)
            button.jog_pressed.connect(lambda d=direction: self._jog_press(d))
            button.jog_released.connect(lambda d=direction: self._jog_release(d))
        self.home_one_button = QPushButton("⌂ 單軸原點復歸")
        self.home_one_button.clicked.connect(lambda: self._worker.home([self.selected_axis]))
        jog = QHBoxLayout()
        jog.addWidget(self.jog_minus, 1)
        jog.addWidget(self.jog_plus, 1)
        jog.addWidget(self.home_one_button)

        self.lock_label = QLabel()
        self.lock_label.setObjectName("lockHint")
        self.lock_label.setWordWrap(True)

        layout = QVBoxLayout(box)
        layout.addLayout(title)
        layout.addLayout(lamps)
        layout.addLayout(codes)
        layout.addWidget(QLabel("JOG：按住移動，放開即停（速度與步進共用）"))
        layout.addLayout(jog)
        layout.addWidget(self.lock_label)
        return box

    def _build_all_axes(self) -> QGroupBox:
        box = QGroupBox("全軸操作")
        self.home_all_button = QPushButton("⌂ 全部原點復歸")
        self.home_all_button.clicked.connect(lambda: self._worker.home(sorted(self._config.axes)))
        note = QLabel("先做升降軸，完成後其餘軸同時做")
        note.setObjectName("hint")
        top = QHBoxLayout()
        top.addWidget(self.home_all_button)
        top.addWidget(note)
        top.addStretch(1)

        self.home_finish_lamps: dict[int, Lamp] = {}
        lamps = QHBoxLayout()
        lamps.addWidget(QLabel("原點復歸完成"))
        for n in sorted(self._config.axes):
            lamp = Lamp(self._config.axes[n].name, "ok")
            lamps.addWidget(lamp)
            self.home_finish_lamps[n] = lamp
        lamps.addStretch(1)

        layout = QVBoxLayout(box)
        layout.addLayout(top)
        layout.addLayout(lamps)
        return box

    # ---- 選定軸 ----

    def select_axis(self, n: int) -> None:
        # 換軸前放開按住中的 JOG（正常操作下按住時無法點別列，保險起見）
        self.jog_minus.release()
        self.jog_plus.release()
        self.selected_axis = n
        for m, row in self.rows.items():
            set_flag(row, "selected", m == n)
        name = self._config.axes[n].name
        self.selected_title.setText(f"{name}（第 {n} 軸）")
        self._update_selected()
        self._update_controls()

    def _update_selected(self) -> None:
        st = self._snapshot.axis(self.selected_axis) if self._snapshot is not None else None
        for key, lamp in self.selected_lamps.items():
            lamp.set_lit(st is not None and bool(_LAMPS[key][2](st)))
        if st is None:
            self.plc_action_label.setText(_PLACEHOLDER)
            self.alarm_code_label.setText(_PLACEHOLDER)
            self.selected_state.setText("無資料")
            set_state(self.selected_state, "off")
            return
        self.plc_action_label.setText(str(st.plc_action_code))
        self.alarm_code_label.setText(str(st.alarm_code))
        if st.alarm_code:
            text, state = f"Alarm {st.alarm_code}", "fault"
        elif st.busy:
            text, state = "動作中", "busy"
        elif st.ready:
            text, state = "就緒", "ok"
        else:
            text, state = "未就緒", "off"
        self.selected_state.setText(text)
        set_state(self.selected_state, state)

    # ---- 命令 ----

    def _checked_axes(self) -> list[int]:
        return [n for n, row in self.rows.items() if row.check.isChecked()]

    def _move_checked(self) -> None:
        targets = {n: self.rows[n].target.value() for n in self._checked_axes()}
        if targets:
            self._worker.move(targets, self.relative_button.isChecked(), self.move_speed.value())

    def _jog_press(self, direction: int) -> None:
        self._jog_axis = self.selected_axis
        self._worker.jog_press(self._jog_axis, direction, self.jog_pad.speed.value())
        self._update_controls()

    def _jog_release(self, direction: int) -> None:
        self._worker.jog_release(self._jog_axis, direction)
        self._update_controls()

    # ---- worker signal ----

    def on_snapshot(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self._snapshot = snapshot
        self._status = status
        for n, row in self.rows.items():
            row.update_state(snapshot.axis(n) if snapshot is not None else None)
        for n, lamp in self.home_finish_lamps.items():
            lamp.set_lit(snapshot is not None and snapshot.axis(n).home_finish)
        self._update_selected()
        self._update_controls()

    def _update_controls(self) -> None:
        tip = lock_tooltip(self._snapshot, self._status)
        self.lock_label.setText(tip.replace("\n", " ") if tip else "")

        checked = bool(self._checked_axes())
        for button in (self.move_button, self.home_checked_button):
            reason = tip if tip is not None else (None if checked else "請先勾選軸")
            button.setEnabled(reason is None)
            button.setToolTip(reason or "")
        for button in (self.home_one_button, self.home_all_button):
            button.setEnabled(tip is None)
            button.setToolTip(tip or "")

        st = self._snapshot.axis(self.selected_axis) if self._snapshot is not None else None
        for button, direction in ((self.jog_minus, -1), (self.jog_plus, +1)):
            # 按住中的鍵不因鎖定反灰（反灰＝放開），只看極限
            reason = None if button.is_held else tip
            if reason is None and st is not None:
                if direction > 0 and st.pos_limit_triggered:
                    reason = "正極限已觸發"
                elif direction < 0 and st.neg_limit_triggered:
                    reason = "負極限已觸發"
            button.setEnabled(reason is None)
            button.setToolTip(reason or "")
