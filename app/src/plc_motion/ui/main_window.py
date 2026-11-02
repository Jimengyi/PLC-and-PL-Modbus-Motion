"""主視窗框架（spec FR-41、FR-42、FR-44、FR-45）：左側導覽、頂列、狀態列與事件顯示。

頂列只有連線狀態、重新連線與急停狀態；Servo 與 Reset Alarm 在軸控頁的「全軸操作」。
狀態列左側是最新訊息，右側是通訊計時（`ui.comm`）。
頁面由 `add_page(name, icon, widget)` 掛入。命令一律呼叫 worker 的命令方法；畫面狀態只依
`snapshot_ready`、`connection_changed`、`engine_event` 更新。關閉鈕與最小化都縮到系統匣，
真正結束走 `request_quit()` → `quit_confirmed`，由 `MotionApp.shutdown()` 收尾。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from plc_motion.engine import (
    CommandRejected,
    DisconnectedWhileMoving,
    EngineEvent,
    JogResidualCleared,
    Notice,
    OperationFinished,
    ResidualDetected,
    ResultKind,
)
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.comm import CommDialog, CommIndicator, CommStats
from plc_motion.ui.dialogs import TextDialog
from plc_motion.ui.residual_dialog import ResidualDialog
from plc_motion.ui.theme import color, set_state
from plc_motion.ui.tray import APP_TITLE
from plc_motion.worker import EngineStatus, PollTiming

log = logging.getLogger("plc_motion.ui")

MIN_WIDTH, MIN_HEIGHT = 1600, 900
NAV_WIDTH = 92
MAX_MESSAGES = 500
_SUCCESS_RESULTS = (ResultKind.DONE, ResultKind.RESET_OK)


@dataclass(frozen=True)
class Message:
    time: datetime
    summary: str  # 狀態列顯示的一行
    detail: str  # 訊息紀錄中的完整內容
    level: str = "info"  # info、ok、fault


def glyph_icon(text: str, size: int = 32) -> QIcon:
    """沒有提供圖示時，以名稱首字畫一個圖示（導覽收合時仍可辨認）。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color("accent"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(0, 0, size, size, size / 4, size / 4)
    font = QFont()
    font.setPixelSize(int(size * 0.55))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text[:1].upper())
    painter.end()
    return QIcon(pixmap)


def _line_icon(draw, size: int = 56) -> QIcon:
    """以線條畫導覽圖示；`draw(painter, size)` 在透明底上畫。"""
    icon = QIcon()
    # 未選取（Off）用暗色、選取中（On）用亮色
    for state, role in ((QIcon.State.Off, "text_dim"), (QIcon.State.On, "text")):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color(role), size / 14)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        draw(painter, size)
        painter.end()
        icon.addPixmap(pixmap, QIcon.Mode.Normal, state)
        icon.addPixmap(pixmap, QIcon.Mode.Active, state)
    return icon


def axis_icon() -> QIcon:
    """軸控：十字箭頭。"""

    def draw(p: QPainter, s: int) -> None:
        m, c, a = s * 0.15, s / 2, s * 0.12
        p.drawLine(QPointF(c, m), QPointF(c, s - m))
        p.drawLine(QPointF(m, c), QPointF(s - m, c))
        for tip, d1, d2 in (
            (QPointF(c, m), QPointF(-a, a), QPointF(a, a)),
            (QPointF(c, s - m), QPointF(-a, -a), QPointF(a, -a)),
            (QPointF(m, c), QPointF(a, -a), QPointF(a, a)),
            (QPointF(s - m, c), QPointF(-a, -a), QPointF(-a, a)),
        ):
            p.drawLine(tip, tip + d1)
            p.drawLine(tip, tip + d2)

    return _line_icon(draw)


def io_icon() -> QIcon:
    """IO Monitor：面板上兩個燈號與兩條線。"""

    def draw(p: QPainter, s: int) -> None:
        m = s * 0.14
        p.drawRoundedRect(QRectF(m, m * 1.3, s - 2 * m, s - 2.6 * m), s * 0.06, s * 0.06)
        r = s * 0.06
        for y in (s * 0.4, s * 0.6):
            p.drawEllipse(QPointF(s * 0.34, y), r, r)
            p.drawLine(QPointF(s * 0.48, y), QPointF(s * 0.72, y))

    return _line_icon(draw)


class NavBar(QFrame):
    """左側導覽：一頁一顆按鈕，圖示在上、名稱在下。"""

    current_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("nav")
        self.buttons: list[QToolButton] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self.current_changed)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 8, 0, 8)
        self._layout.setSpacing(2)
        self._layout.addStretch(1)
        self.setFixedWidth(NAV_WIDTH)

    def add(self, name: str, icon: QIcon | None) -> int:
        index = len(self.buttons)
        button = QToolButton()
        button.setCheckable(True)
        button.setText(name)
        button.setIcon(icon if icon is not None and not icon.isNull() else glyph_icon(name))
        button.setIconSize(QSize(28, 28))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._group.addButton(button, index)
        self._layout.insertWidget(self._layout.count() - 1, button)
        self.buttons.append(button)
        if index == 0:
            button.setChecked(True)
        return index

    def select(self, index: int) -> None:
        self.buttons[index].setChecked(True)


class MainWindow(QMainWindow):
    quit_confirmed = Signal()

    def __init__(self, config: AppConfig, worker, endpoint: str = "", parent=None):
        super().__init__(parent)
        self._config = config
        self._worker = worker
        self._endpoint = endpoint
        self._allow_close = False
        self.snapshot: Snapshot | None = None
        self.status: EngineStatus | None = None
        self.messages: list[Message] = []
        self.comm_stats = CommStats()
        self._connected = False

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        self.residual_dialog = ResidualDialog(config, worker, self)
        self.residual_dialog.finished.connect(self._update_residual_entry)
        self._build_toolbar()
        self._build_body()
        self._build_status_bar()
        self.event_dialog = TextDialog("操作結果", self)
        self.history_dialog = TextDialog("訊息紀錄", self)

        worker.snapshot_ready.connect(self.on_snapshot)
        worker.connection_changed.connect(self.on_connection_changed)
        worker.engine_event.connect(self.on_engine_event)
        worker.poll_timed.connect(self.on_poll_timed)

        self._set_connection(False, initial=True)
        self._update_estop(None)

    # ---- 版面 ----

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("工具列")
        self.toolbar.setObjectName("toolbar")
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.toggleViewAction().setEnabled(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        brand = QLabel(APP_TITLE)
        brand.setObjectName("brand")
        self.connection_label = QLabel()
        self.connection_label.setObjectName("connection")
        self.connection_label.setTextFormat(Qt.TextFormat.RichText)
        self.reconnect_button = QPushButton("重新連線")
        self.reconnect_button.setObjectName("small")
        self.reconnect_button.clicked.connect(self._worker.connect_plc)
        self.estop_label = QLabel()
        self.estop_label.setObjectName("estop")
        self.residual_button = QPushButton("殘留交握…")
        self.residual_button.setToolTip("重新開啟殘留交握視窗")
        self.residual_button.clicked.connect(self.residual_dialog.popup)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        spacer.setStyleSheet("background: transparent;")

        self.toolbar.addWidget(brand)
        self.toolbar.addWidget(self.connection_label)
        self.toolbar.addWidget(self.reconnect_button)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.estop_label)
        self._residual_action = self.toolbar.addWidget(self.residual_button)
        self._residual_action.setVisible(False)
        self.toolbar.addWidget(spacer)

    def _build_body(self) -> None:
        self.nav = NavBar()
        self.pages = QStackedWidget()
        self.nav.current_changed.connect(self.pages.setCurrentIndex)
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.nav)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(body)

    def _build_status_bar(self) -> None:
        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        interval = self._config.device.poll_interval_ms
        self.comm_indicator = CommIndicator(interval)
        self.comm_indicator.clicked.connect(self.show_comm)
        self.comm_dialog = CommDialog(interval, self)
        self.comm_dialog.reset_requested.connect(self._reset_comm)
        self.message_button = QPushButton("")
        self.message_button.setObjectName("message")
        self.message_button.setFlat(True)
        self.message_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.message_button.setToolTip("檢視訊息紀錄")
        self.message_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.message_button.clicked.connect(self.show_history)
        self.status_bar.addWidget(self.message_button, 1)
        self.status_bar.addPermanentWidget(self.comm_indicator)
        self.setStatusBar(self.status_bar)

    def add_page(self, name: str, icon: QIcon | None, widget: QWidget) -> int:
        index = self.pages.addWidget(widget)
        self.nav.add(name, icon)
        if index == 0:
            self.pages.setCurrentIndex(0)
        return index

    # ---- worker signal ----

    def on_snapshot(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self.snapshot = snapshot
        self.status = status
        if not status.connected:
            self._set_connection(False)
        self._update_estop(snapshot)
        if not status.residual:
            self._residual_action.setVisible(False)
            self.residual_dialog.hide()

    def on_connection_changed(self, connected: bool) -> None:
        self._set_connection(connected)
        if connected:
            self.comm_stats.reset()
        elif self._connected:
            self.comm_stats.disconnects += 1
        self._connected = connected
        self._refresh_comm()
        if connected:
            self.show_message(f"已連線到 PLC {self._endpoint}", level="ok")
        else:
            self.show_message("未連線：連線失敗或通訊中斷，請確認 PLC 後按「重新連線」", "fault")

    def on_engine_event(self, event: EngineEvent) -> None:
        if isinstance(event, CommandRejected):
            self._show_rejected(event)
        elif isinstance(event, OperationFinished):
            self._show_finished(event)
        elif isinstance(event, Notice):
            self.show_message(event.text)
        elif isinstance(event, ResidualDetected):
            self._show_residual(event)
        elif isinstance(event, JogResidualCleared):
            self.show_message(f"第 {event.axis} 軸 JOG 於斷線期間未放開，已寫回 0", "fault")
        elif isinstance(event, DisconnectedWhileMoving):
            self._show_moving(event)

    def _show_residual(self, event: ResidualDetected) -> None:
        axes = "、".join(str(i.axis) for i in event.axes)
        self.show_message(
            f"偵測到殘留交握（第 {axes} 軸），寫入已鎖定",
            "fault",
            detail=self._residual_detail(event),
        )
        self.residual_dialog.set_residual(event.axes)
        self._residual_action.setVisible(True)
        self.residual_dialog.popup()

    @staticmethod
    def _residual_detail(event: ResidualDetected) -> str:
        return "偵測到殘留交握：" + "、".join(f"第 {i.axis} 軸" for i in event.axes)

    def _update_residual_entry(self, _result: int) -> None:
        # 寫回 0（accepted）後入口收起；保持鎖定（rejected）則保留入口
        if _result == ResidualDialog.DialogCode.Accepted:
            self._residual_action.setVisible(False)

    def _show_moving(self, event: DisconnectedWhileMoving) -> None:
        axes = "、".join(str(n) for n in event.axes)
        summary = f"第 {axes} 軸斷線時動作中，軸可能仍在移動，請按急停"
        self.show_message(summary, "fault")
        self.event_dialog.setWindowTitle("斷線時軸動作中")
        self.event_dialog.set_content("軸可能仍在移動，請按急停", f"第 {axes} 軸", "fault")
        self.event_dialog.popup()

    def _show_rejected(self, event: CommandRejected) -> None:
        heading = f"{event.command}未執行：共 {len(event.reasons)} 項不符"
        body = "\n".join(f"・{r}" for r in event.reasons)
        first = event.reasons[0] if event.reasons else ""
        more = f"（另 {len(event.reasons) - 1} 項）" if len(event.reasons) > 1 else ""
        self.show_message(
            f"{event.command}未執行：{first}{more}", "fault", detail=f"{heading}\n{body}"
        )
        self.event_dialog.setWindowTitle("命令未執行")
        self.event_dialog.set_content(heading, body, "fault")
        self.event_dialog.popup()

    def _show_finished(self, event: OperationFinished) -> None:
        items = [(n, r.text) for n, r in event.results.items()]
        ok = all(r.kind in _SUCCESS_RESULTS for r in event.results.values())
        summary = f"{event.kind.value}結束：" + "、".join(f"第 {n} 軸 {t}" for n, t in items)
        lines = "\n".join(f"第 {n} 軸（{self._axis_name(n)}）：{t}" for n, t in items)
        heading = f"{event.kind.value}結束" + ("" if ok else "：有軸未完成")
        self.show_message(summary, "ok" if ok else "fault", detail=f"{heading}\n{lines}")
        if not ok:
            self.event_dialog.setWindowTitle("操作結果")
            self.event_dialog.set_content(heading, lines, "fault")
            self.event_dialog.popup()

    def _axis_name(self, n: int) -> str:
        axis = self._config.axes.get(n)
        return axis.name if axis is not None else str(n)

    # ---- 通訊計時 ----

    def on_poll_timed(self, timing: PollTiming) -> None:
        self.comm_stats.add(timing)
        self._refresh_comm()

    def show_comm(self) -> None:
        self.comm_dialog.update_stats(self.comm_stats)
        self.comm_dialog.popup()

    def _reset_comm(self) -> None:
        self.comm_stats.reset_all()
        self._refresh_comm()

    def _refresh_comm(self) -> None:
        self.comm_indicator.update_stats(self.comm_stats)
        if self.comm_dialog.isVisible():
            self.comm_dialog.update_stats(self.comm_stats)

    # ---- 狀態列訊息 ----

    def show_message(self, summary: str, level: str = "info", detail: str | None = None) -> None:
        message = Message(datetime.now(), summary, detail or summary, level)
        self.messages.append(message)
        del self.messages[:-MAX_MESSAGES]
        self.message_button.setText(f"{message.time:%H:%M:%S}  {summary}")
        self.message_button.setToolTip(f"{message.detail}\n\n（點選檢視訊息紀錄）")
        set_state(self.message_button, level)
        if self.history_dialog.isVisible():
            self._fill_history()

    def show_history(self) -> None:
        self._fill_history()
        self.history_dialog.popup()

    def _fill_history(self) -> None:
        text = "\n\n".join(
            f"[{m.time:%Y-%m-%d %H:%M:%S}] {m.detail}" for m in reversed(self.messages)
        )
        self.history_dialog.set_content(f"最近 {len(self.messages)} 則訊息（新的在上）", text)

    # ---- 頂列狀態 ----

    def _set_connection(self, connected: bool, initial: bool = False) -> None:
        if connected:
            text, state = "已連線", "ok"
        else:
            text, state = ("連線中…" if initial else "未連線"), "off"
        dot = color(state).name()
        self.connection_label.setText(
            f'<span style="color:{dot}">●</span>&nbsp;{text} {self._endpoint}'.rstrip()
        )
        self.connection_label.setAccessibleName(f"{text} {self._endpoint}".rstrip())
        set_state(self.connection_label, state)

    def _update_estop(self, snapshot: Snapshot | None) -> None:
        if snapshot is None:
            self.estop_label.setText("急停：—")
            set_state(self.estop_label, "off")
        elif snapshot.estop_ok:
            self.estop_label.setText("急停：正常")
            set_state(self.estop_label, "ok")
        else:
            self.estop_label.setText("⚠ 急停觸發")
            set_state(self.estop_label, "fault")

    # ---- 系統匣與結束 ----

    def show_from_tray(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self) -> None:
        self.hide()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        self.hide_to_tray()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            # 在狀態切換的事件處理中直接 hide 會讓視窗還原時狀態錯亂，延到下一輪
            QTimer.singleShot(0, self._hide_if_minimized)

    def _hide_if_minimized(self) -> None:
        if self.isMinimized():
            self.hide()
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)

    def allow_close(self) -> None:
        """之後的 close() 真的關閉視窗（結束流程用）。"""
        self._allow_close = True

    def request_quit(self) -> bool:
        """結束前確認（FR-44）：有操作進行中或任一軸 Busy = 1 時先問。確認後發 quit_confirmed。"""
        busy_axes = []
        if self.snapshot is not None:
            busy_axes = [n for n in sorted(self._config.axes) if self.snapshot.axis(n).busy]
        operating = self.status is not None and self.status.operating
        if busy_axes or operating:
            parts = []
            if operating:
                parts.append("有操作進行中")
            if busy_axes:
                parts.append("第 " + "、".join(str(n) for n in busy_axes) + " 軸動作中")
            answer = QMessageBox.question(
                self if self.isVisible() else None,
                "確認結束",
                f"{'，'.join(parts)}。\n\n結束程式不會停止軸的動作，也不會寫入任何點。確定要結束嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.quit_confirmed.emit()
        return True
