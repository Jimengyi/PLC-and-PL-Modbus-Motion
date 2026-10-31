"""主視窗框架（spec FR-41、FR-42、FR-44、FR-45）：左側導覽、工具列、狀態列與事件顯示。

頁面由 `add_page(name, icon, widget)` 掛入。命令一律呼叫 worker 的命令方法；畫面狀態只依
`snapshot_ready`、`connection_changed`、`engine_event` 更新。關閉鈕與最小化都縮到系統匣，
真正結束走 `request_quit()` → `quit_confirmed`，由 `MotionApp.shutdown()` 收尾。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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
    EngineEvent,
    Notice,
    OperationFinished,
    ResultKind,
)
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.dialogs import TextDialog
from plc_motion.ui.theme import apply_theme, color, current_theme, set_state
from plc_motion.ui.tray import APP_TITLE
from plc_motion.worker import EngineStatus

log = logging.getLogger("plc_motion.ui")

MIN_WIDTH, MIN_HEIGHT = 1600, 900
NAV_EXPANDED_WIDTH = 180
NAV_COLLAPSED_WIDTH = 56
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


class NavBar(QFrame):
    """左側導覽：一頁一顆按鈕；收合後只剩圖示，名稱移到 tooltip。"""

    current_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("nav")
        self.collapsed = False
        self.buttons: list[QToolButton] = []
        self._names: list[str] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self.current_changed)

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("navToggle")
        self.toggle_button.setText("≡")
        self.toggle_button.setToolTip("收合導覽")
        self.toggle_button.clicked.connect(lambda: self.set_collapsed(not self.collapsed))

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 8, 6, 8)
        self._layout.setSpacing(4)
        self._layout.addWidget(self.toggle_button)
        self._layout.addStretch(1)
        self.setFixedWidth(NAV_EXPANDED_WIDTH)

    def add(self, name: str, icon: QIcon | None) -> int:
        index = len(self.buttons)
        button = QToolButton()
        button.setCheckable(True)
        button.setIcon(icon if icon is not None and not icon.isNull() else glyph_icon(name))
        button.setIconSize(QSize(22, 22))
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._group.addButton(button, index)
        self._layout.insertWidget(self._layout.count() - 1, button)
        self.buttons.append(button)
        self._names.append(name)
        self._apply_style(button, name)
        if index == 0:
            button.setChecked(True)
        return index

    def select(self, index: int) -> None:
        self.buttons[index].setChecked(True)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.setFixedWidth(NAV_COLLAPSED_WIDTH if collapsed else NAV_EXPANDED_WIDTH)
        self.toggle_button.setToolTip("展開導覽" if collapsed else "收合導覽")
        for button, name in zip(self.buttons, self._names, strict=True):
            self._apply_style(button, name)

    def _apply_style(self, button: QToolButton, name: str) -> None:
        if self.collapsed:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setText("")
            button.setToolTip(name)
        else:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setText(name)
            button.setToolTip("")


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

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        self._build_toolbar()
        self._build_body()
        self._build_status_bar()
        self.event_dialog = TextDialog("操作結果", self)
        self.history_dialog = TextDialog("訊息紀錄", self)

        worker.snapshot_ready.connect(self.on_snapshot)
        worker.connection_changed.connect(self.on_connection_changed)
        worker.engine_event.connect(self.on_engine_event)

        self._set_connection(False, initial=True)
        self._update_estop(None)
        self._update_write_buttons()

    # ---- 版面 ----

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("工具列")
        self.toolbar.setObjectName("toolbar")
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.toggleViewAction().setEnabled(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)

        self.connection_label = QLabel()
        self.reconnect_button = QPushButton("重新連線")
        self.reconnect_button.clicked.connect(self._worker.connect_plc)
        self.estop_label = QLabel()
        self.servo_on_button = QPushButton("全部 Servo ON")
        self.servo_on_button.clicked.connect(lambda: self._worker.servo_all(True))
        self.servo_off_button = QPushButton("全部 Servo OFF")
        self.servo_off_button.clicked.connect(lambda: self._worker.servo_all(False))
        self.reset_button = QPushButton("全部 Reset Alarm")
        self.reset_button.clicked.connect(self._worker.reset_all)
        self.write_buttons = [self.servo_on_button, self.servo_off_button, self.reset_button]
        self.theme_button = QPushButton()
        self.theme_button.clicked.connect(self.toggle_theme)
        self._update_theme_button()

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        spacer.setStyleSheet("background: transparent;")

        self.toolbar.addWidget(self.connection_label)
        self.toolbar.addWidget(self.reconnect_button)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(self.estop_label)
        self.toolbar.addSeparator()
        for button in self.write_buttons:
            self.toolbar.addWidget(button)
        self.toolbar.addWidget(spacer)
        self.toolbar.addWidget(self.theme_button)

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
        self.poll_label = QLabel(f"輪詢週期 {self._config.device.poll_interval_ms} ms")
        self.message_button = QPushButton("")
        self.message_button.setObjectName("message")
        self.message_button.setFlat(True)
        self.message_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.message_button.setToolTip("檢視訊息紀錄")
        self.message_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.message_button.clicked.connect(self.show_history)
        self.status_bar.addWidget(self.message_button, 1)
        self.status_bar.addPermanentWidget(self.poll_label)
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
        self._update_write_buttons()

    def on_connection_changed(self, connected: bool) -> None:
        self._set_connection(connected)
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

    # ---- 工具列狀態 ----

    def _set_connection(self, connected: bool, initial: bool = False) -> None:
        if connected:
            self.connection_label.setText(f"● 已連線 {self._endpoint}".rstrip())
            set_state(self.connection_label, "ok")
        else:
            text = "● 連線中…" if initial else "● 未連線"
            self.connection_label.setText(f"{text} {self._endpoint}".rstrip())
            set_state(self.connection_label, "off")

    def _update_estop(self, snapshot: Snapshot | None) -> None:
        if snapshot is None:
            self.estop_label.setText("急停：—")
            set_state(self.estop_label, "off")
        elif snapshot.estop_ok:
            self.estop_label.setText("急停：正常")
            set_state(self.estop_label, "ok")
        else:
            self.estop_label.setText("急停：觸發")
            set_state(self.estop_label, "fault")

    def _update_write_buttons(self) -> None:
        status = self.status
        enabled = status is not None and not status.locked
        if status is None:
            tip = "尚未取得 PLC 狀態"
        elif status.locked:
            tip = "鎖定中：\n" + "\n".join(status.lock_reasons)
        else:
            tip = ""
        for button in self.write_buttons:
            button.setEnabled(enabled)
            button.setToolTip(tip)

    # ---- 主題 ----

    def toggle_theme(self) -> None:
        name = "light" if current_theme() == "dark" else "dark"
        apply_theme(QApplication.instance(), name)
        self._update_theme_button()
        try:
            self._config.device.save_theme(name)
        except Exception as e:
            log.exception("主題寫回 device.toml 失敗")
            self.show_message(f"主題已切換，但寫回 device.toml 失敗：{e}", "fault")

    def _update_theme_button(self) -> None:
        self.theme_button.setText("切換淺色" if current_theme() == "dark" else "切換深色")

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
