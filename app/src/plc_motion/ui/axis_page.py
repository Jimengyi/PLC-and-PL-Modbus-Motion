"""軸控頁（spec FR-28 ~ FR-33、FR-16 反灰、FR-27 UI 面）。

上排：七張位置卡（只顯示目前位置；該軸 Alarm 或觸發極限時上緣變紅並顯示原因），點卡片＝選定軸。
下方三欄：選定軸（燈號、目標位置移動、JOG、單軸原點復歸、全軸操作）｜步進 `JogPad`｜點位
`PointsPanel`。

命令一律呼叫 worker 的命令方法；畫面只依 `snapshot_ready` 更新。目標位置移動、單軸與全部原點
復歸先跳確認視窗；JOG、步進、Servo、Reset Alarm 不確認。鎖定（`EngineStatus.locked`）或尚未
取得狀態時所有寫入控制項反灰，唯一例外是按住中的 JOG 鍵：JOG 按下本身就會造成鎖定，而反灰會
視同放開，所以按住中的鍵只在該方向極限觸發時反灰。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import I32_MAX, decimals_for
from plc_motion.plc.snapshot import AxisState, Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.ui import confirm_dialog
from plc_motion.ui.confirm_dialog import ConfirmRow
from plc_motion.ui.jog_pad import LIFT_AXIS, JogButton, JogPad, format_value, lock_tooltip
from plc_motion.ui.points_panel import PointsPanel
from plc_motion.ui.theme import set_state
from plc_motion.ui.widgets import (
    Lamp,
    Panel,
    divider,
    hint,
    named_button,
    plain_spinbox,
    segment_button,
    set_flag,
)
from plc_motion.worker import EngineStatus

# 燈號：key → (顯示文字, 亮燈時的 state, 由 AxisState 取值)；依 3 × 3 排列
_LAMPS = {
    "servo": ("Servo", "ok", lambda s: s.servo),
    "ready": ("Ready", "ok", lambda s: s.ready),
    "busy": ("Busy", "busy", lambda s: s.busy),
    "home_finish": ("原點完成", "ok", lambda s: s.home_finish),
    "home_sensor": ("Home Sensor", "ok", lambda s: s.home_sensor),
    "request": ("Request", "busy", lambda s: s.request),
    "pos_limit": ("+Limit", "fault", lambda s: s.pos_limit_triggered),
    "neg_limit": ("−Limit", "fault", lambda s: s.neg_limit_triggered),
    "reply": ("Reply", "busy", lambda s: s.reply),
}
SELECTED_LAMPS = tuple(_LAMPS)
# 感測點燈號 → Tag 名稱前綴；該軸的點 `invert=true` 時名稱加註「(B)」
_SENSOR_TAGS = {
    "pos_limit": "Positive Limit",
    "neg_limit": "Negative Limit",
    "home_sensor": "Home Sensor",
}
SELECTED_WIDTH, STEP_WIDTH = 396, 440
_PLACEHOLDER = "—"


def fault_reason(st: AxisState) -> str:
    """位置卡的異常原因：Alarm 與極限；正常時為空字串。"""
    parts = []
    if st.alarm_code:
        parts.append(f"Alarm {st.alarm_code}")
    if st.pos_limit_triggered:
        parts.append("+極限")
    if st.neg_limit_triggered:
        parts.append("−極限")
    return "、".join(parts)


class PositionCard(QFrame):
    """一軸的位置卡；點選發 `clicked`。"""

    clicked = Signal(int)

    def __init__(self, config: AppConfig, n: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("posCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.number = n
        point = config.points.axis_tag("Current Position", n)
        self.scale, self.unit = point.scale, point.unit

        self.name_label = QLabel(config.axes[n].name)
        self.name_label.setObjectName("posAxis")
        unit = QLabel(self.unit or "")
        unit.setObjectName("posUnit")
        self.position_label = QLabel(_PLACEHOLDER)
        self.position_label.setObjectName("posValue")
        self.position_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.reason_label = QLabel()
        self.reason_label.setObjectName("posReason")

        top = QHBoxLayout()
        top.addWidget(self.name_label)
        top.addStretch(1)
        top.addWidget(unit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 6)
        layout.setSpacing(2)
        layout.addLayout(top)
        layout.addWidget(self.position_label)
        layout.addWidget(self.reason_label)
        self.reason_label.setFixedHeight(18)
        set_state(self, "off")
        set_flag(self, "selected", False)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.number)
        super().mousePressEvent(event)

    def update_state(self, st: AxisState | None) -> None:
        if st is None:
            self.position_label.setText(_PLACEHOLDER)
            self.reason_label.setText("")
            set_state(self, "off")
            return
        self.position_label.setText(format_value(st.position, self.scale))
        reason = fault_reason(st)
        self.reason_label.setText(reason)
        set_state(self, "fault" if reason else "ok")


class AxisPage(QWidget):
    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__(parent)
        self._config = config
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self.selected_axis = min(config.axes)
        self._jog_axis = self.selected_axis  # 按下時的軸：放開送同一軸

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.cards: dict[int, PositionCard] = {}
        for n in sorted(config.axes):
            card = PositionCard(config, n)
            card.clicked.connect(self.select_axis)
            cards.addWidget(card, 1)
            self.cards[n] = card

        selected = self._build_selected()
        selected.setFixedWidth(SELECTED_WIDTH)
        self.jog_pad = JogPad(config, worker)
        self.jog_pad.setFixedWidth(STEP_WIDTH)
        self.points_panel = PointsPanel(config, worker)

        self.bottom_layout = QHBoxLayout()
        self.bottom_layout.setSpacing(12)
        self.bottom_layout.addWidget(selected)
        self.bottom_layout.addWidget(self.jog_pad)
        self.bottom_layout.addWidget(self.points_panel, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        layout.addLayout(cards)
        layout.addLayout(self.bottom_layout, 1)

        worker.snapshot_ready.connect(self.on_snapshot)
        self.select_axis(self.selected_axis)

    # ---- 版面 ----

    def _build_selected(self) -> Panel:
        panel = Panel("", "選定軸")
        self.selected_title = panel.title_label
        self.selected_state = QLabel()
        self.selected_state.setObjectName("stateChip")
        panel.header_layout.addWidget(self.selected_state)
        body = panel.body_layout

        lamps = QGridLayout()
        lamps.setHorizontalSpacing(6)
        lamps.setVerticalSpacing(6)
        self.selected_lamps: dict[str, Lamp] = {}
        for i, key in enumerate(SELECTED_LAMPS):
            text, lit, _ = _LAMPS[key]
            lamp = Lamp(text, lit)
            lamps.addWidget(lamp, i // 3, i % 3)
            self.selected_lamps[key] = lamp
        body.addLayout(lamps)

        self.plc_action_label = QLabel(_PLACEHOLDER)
        self.plc_action_label.setObjectName("value")
        self.alarm_code_label = QLabel(_PLACEHOLDER)
        self.alarm_code_label.setObjectName("value")
        codes = QHBoxLayout()
        codes.addWidget(hint("PLC Action Code"))
        codes.addWidget(self.plc_action_label)
        codes.addSpacing(16)
        codes.addWidget(hint("Alarm Code"))
        codes.addWidget(self.alarm_code_label)
        codes.addStretch(1)
        body.addLayout(codes)

        # 目標位置：單軸絕對／相對移動
        self.absolute_button = segment_button("絕對")
        self.relative_button = segment_button("相對")
        self.absolute_button.setChecked(True)
        mode = QButtonGroup(panel)
        mode.addButton(self.absolute_button)
        mode.addButton(self.relative_button)
        self.target = QDoubleSpinBox()
        plain_spinbox(self.target)
        self.target.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.target.setToolTip("目標值（絕對位置或相對移動量）")
        self.move_button = named_button("移動", "primary")
        self.move_button.clicked.connect(self._move_selected)
        target_row = QHBoxLayout()
        target_row.setSpacing(0)
        target_row.addWidget(self.absolute_button)
        target_row.addWidget(self.relative_button)
        target_row.addSpacing(8)
        target_row.addWidget(self.target, 1)
        target_row.addSpacing(8)
        target_row.addWidget(self.move_button)
        body.addWidget(hint("目標位置"))
        body.addLayout(target_row)

        self.jog_minus = JogButton("◀  JOG −")
        self.jog_plus = JogButton("JOG +  ▶")
        jog = QHBoxLayout()
        for button, direction in ((self.jog_minus, -1), (self.jog_plus, +1)):
            button.setObjectName("big")
            button.jog_pressed.connect(lambda d=direction: self._jog_press(d))
            button.jog_released.connect(lambda d=direction: self._jog_release(d))
            jog.addWidget(button, 1)
        body.addWidget(hint("JOG：按住移動，放開即停（速度與步進共用）"))
        body.addLayout(jog)
        self.home_one_button = QPushButton("⌂ 單軸原點復歸")
        self.home_one_button.clicked.connect(self._home_selected)
        body.addWidget(self.home_one_button)

        # 全軸操作
        body.addWidget(divider())
        body.addWidget(hint("全軸操作"))
        self.servo_on_button = QPushButton("Servo 全部 ON")
        self.servo_on_button.clicked.connect(lambda: self._worker.servo_all(True))
        self.servo_off_button = QPushButton("Servo 全部 OFF")
        self.servo_off_button.clicked.connect(lambda: self._worker.servo_all(False))
        self.reset_button = QPushButton("Reset Alarm")
        self.reset_button.clicked.connect(self._worker.reset_all)
        servo = QHBoxLayout()
        servo.setSpacing(6)
        for button in (self.servo_on_button, self.servo_off_button, self.reset_button):
            button.setObjectName("compact")
            servo.addWidget(button, 1)
        body.addLayout(servo)
        self.home_all_button = QPushButton("⌂ 全部原點復歸")
        self.home_all_button.clicked.connect(self._home_all)
        home_all = QHBoxLayout()
        home_all.addWidget(self.home_all_button)
        home_all.addWidget(hint("先做升降軸，完成後其餘軸同時做"))
        home_all.addStretch(1)
        body.addLayout(home_all)

        self.home_finish_lamps: dict[int, Lamp] = {}
        finished = QGridLayout()
        finished.setHorizontalSpacing(6)
        finished.setVerticalSpacing(6)
        finished.addWidget(hint("原點完成"), 0, 0)
        for i, n in enumerate(sorted(self._config.axes), start=1):
            lamp = Lamp(self._config.axes[n].name, "ok")
            finished.addWidget(lamp, i // 4, i % 4)
            self.home_finish_lamps[n] = lamp
        body.addLayout(finished)

        self.lock_label = QLabel()
        self.lock_label.setObjectName("lockHint")
        self.lock_label.setWordWrap(True)
        body.addWidget(self.lock_label)
        body.addStretch(1)
        return panel

    # ---- 選定軸 ----

    def select_axis(self, n: int) -> None:
        # 換軸前放開按住中的 JOG（正常操作下按住時無法點別張卡，保險起見）
        self.jog_minus.release()
        self.jog_plus.release()
        self.selected_axis = n
        for m, card in self.cards.items():
            set_flag(card, "selected", m == n)
        self.selected_title.setText(f"{self._config.axes[n].name} 軸")
        for key, prefix in _SENSOR_TAGS.items():
            b_contact = self._config.points.axis_tag(prefix, n).invert
            self.selected_lamps[key].caption = _LAMPS[key][0] + ("(B)" if b_contact else "")
        point = self._config.points.axis_tag("Current Position", n)
        limit = I32_MAX / point.scale
        self.target.setDecimals(decimals_for(point.scale))
        self.target.setRange(-limit, limit)
        self.target.setSuffix(f" {point.unit}")
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

    def _axis_text(self, n: int, value: float) -> str:
        point = self._config.points.axis_tag("Current Position", n)
        return format_value(value, point.scale)

    def _unit(self, n: int) -> str:
        return self._config.points.axis_tag("Current Position", n).unit or ""

    def _move_selected(self) -> None:
        if self._snapshot is None:
            return
        n = self.selected_axis
        value = self.target.value()
        relative = self.relative_button.isChecked()
        speed = self.jog_pad.speed.value()
        current = self._snapshot.axis(n).position
        target = current + value if relative else value
        name = self._config.axes[n].name
        if relative:
            mode = f"相對（{value:+.{self.target.decimals()}f} {self._unit(n)}）"
        else:
            mode = "絕對"
        if not confirm_dialog.confirm(
            self,
            f"確認移動 {name} 軸",
            lines=(f"模式：{mode}　速度：{speed} %",),
            rows=(
                ConfirmRow(
                    name, self._axis_text(n, current), self._axis_text(n, target), self._unit(n)
                ),
            ),
            ok_text="▶ 確認移動",
        ):
            return
        if lock_tooltip(self._snapshot, self._status) is None:
            self._worker.move({n: value}, relative, speed)

    def _home_selected(self) -> None:
        n = self.selected_axis
        name = self._config.axes[n].name
        lines = [f"{name} 軸會移動到原點。"]
        if self._snapshot is not None:
            position = self._axis_text(n, self._snapshot.axis(n).position)
            lines.append(f"目前位置 {position} {self._unit(n)}")
        if (
            confirm_dialog.confirm(
                self, f"確認 {name} 軸原點復歸", lines=tuple(lines), ok_text="⌂ 確認原點復歸"
            )
            and lock_tooltip(self._snapshot, self._status) is None
        ):
            self._worker.home([n])

    def _home_all(self) -> None:
        axes = sorted(self._config.axes)
        others = "、".join(self._config.axes[n].name for n in axes if n != LIFT_AXIS)
        if (
            confirm_dialog.confirm(
                self,
                "確認全部原點復歸",
                lines=(
                    "七軸都會移動到原點：",
                    "1. 先做升降軸",
                    f"2. 升降軸完成後，{others} 同時做",
                ),
                note="確認機台周圍淨空再執行。",
                ok_text="⌂ 確認全部原點復歸",
            )
            and lock_tooltip(self._snapshot, self._status) is None
        ):
            self._worker.home(axes)

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
        for n, card in self.cards.items():
            card.update_state(snapshot.axis(n) if snapshot is not None else None)
        for n, lamp in self.home_finish_lamps.items():
            lamp.set_lit(snapshot is not None and snapshot.axis(n).home_finish)
        self._update_selected()
        self._update_controls()

    def _update_controls(self) -> None:
        tip = lock_tooltip(self._snapshot, self._status)
        self.lock_label.setText(tip.replace("\n", " ") if tip else "")

        for button in (
            self.move_button,
            self.home_one_button,
            self.home_all_button,
            self.servo_on_button,
            self.servo_off_button,
            self.reset_button,
        ):
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
