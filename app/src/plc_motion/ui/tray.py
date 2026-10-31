"""系統匣（spec FR-42、FR-43）：四態圖示、Alarm／急停通知、右鍵選單與雙擊叫回。

TrayController 只發 signal，接到哪裡由 `app.py` 決定；狀態由 `update(snapshot, status)` 餵入，
與 `PlcWorker.snapshot_ready` 的參數相同。
"""

from enum import Enum

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from plc_motion.plc.snapshot import Snapshot
from plc_motion.worker import EngineStatus

APP_TITLE = "PLC 七軸運動控制"


class TrayState(Enum):
    NORMAL = "正常"
    BUSY = "動作中"
    FAULT = "異常（Alarm 或急停）"
    DISCONNECTED = "未連線"


# 系統匣圖示不隨主題變化（工作列底色由 Windows 決定）
STATE_COLORS = {
    TrayState.NORMAL: "#30b050",
    TrayState.BUSY: "#2f7fe0",
    TrayState.FAULT: "#e53935",
    TrayState.DISCONNECTED: "#8a8a8e",
}


def tray_state(snapshot: Snapshot | None, status: EngineStatus, axes) -> TrayState:
    """優先序：未連線 > 異常 > 動作中 > 正常。"""
    if snapshot is None or not status.connected:
        return TrayState.DISCONNECTED
    states = [snapshot.axis(n) for n in axes]
    if not snapshot.estop_ok or any(s.alarm_code != 0 for s in states):
        return TrayState.FAULT
    if status.operating or any(s.busy for s in states):
        return TrayState.BUSY
    return TrayState.NORMAL


def state_icon(state: TrayState) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(STATE_COLORS[state])
        painter.setPen(QPen(color.darker(150), max(1.0, size / 16)))
        painter.setBrush(QBrush(color))
        r = size / 2 - max(1.0, size / 16)
        painter.drawEllipse(QPointF(size / 2, size / 2), r, r)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class TrayController(QObject):
    show_requested = Signal()
    reconnect_requested = Signal()
    quit_requested = Signal()

    def __init__(self, axes=range(1, 8), parent: QObject | None = None):
        super().__init__(parent)
        self._axes = tuple(axes)
        self._icons = {s: state_icon(s) for s in TrayState}
        self._prev: Snapshot | None = None
        self.state: TrayState | None = None

        self.menu = QMenu()
        self.action_show = QAction("顯示主視窗", self.menu)
        self.action_reconnect = QAction("重新連線", self.menu)
        self.action_quit = QAction("結束", self.menu)
        self.menu.addAction(self.action_show)
        self.menu.addAction(self.action_reconnect)
        self.menu.addSeparator()
        self.menu.addAction(self.action_quit)
        self.action_show.triggered.connect(self.show_requested)
        self.action_reconnect.triggered.connect(self.reconnect_requested)
        self.action_quit.triggered.connect(self.quit_requested)

        self.icon = QSystemTrayIcon(self)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._set_state(TrayState.DISCONNECTED)

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def update(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self._set_state(tray_state(snapshot, status, self._axes))
        prev, self._prev = self._prev, snapshot
        if snapshot is None or prev is None:
            return
        for n in self._axes:
            old, new = prev.axis(n).alarm_code, snapshot.axis(n).alarm_code
            if old == 0 and new != 0:
                self.notify("Alarm 發生", f"第 {n} 軸 Alarm Code = {new}")
        if prev.estop_ok and not snapshot.estop_ok:
            self.notify("急停觸發", "Emergency Stop 已觸發，所有軸停止")

    def notify(self, title: str, text: str) -> None:
        self.icon.showMessage(title, text, QSystemTrayIcon.MessageIcon.Warning, 5000)

    def _set_state(self, state: TrayState) -> None:
        if state is self.state:
            return
        self.state = state
        self.icon.setIcon(self._icons[state])
        self.icon.setToolTip(f"{APP_TITLE}：{state.value}")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_requested.emit()
