"""系統匣（spec FR-42、FR-43）：四態圖示、Alarm／急停通知、右鍵選單與雙擊叫回。

圖示是六角形（六軸平台）：外框與中心點用狀態色，中心三條輻線代表平台支臂。狀態不只靠顏色：
動作中中心有 ▶、異常中心有「!」、未連線外框在六個角斷開。
「正常」版本同時當作程式圖示（`app_icon`）。

TrayController 只發 signal，接到哪裡由 `app.py` 決定；狀態由 `update(snapshot, status)` 餵入，
與 `PlcWorker.snapshot_ready` 的參數相同。
"""

import struct
from enum import Enum

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
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


ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
_HEX = ((32, 4), (56, 18), (56, 46), (32, 60), (8, 46), (8, 18))  # 64 × 64 座標
_SPOKES = ((32, 13), (48, 41), (16, 41))
_FACE, _SPOKE, _MARK = "#1f2328", "#8b939e", "#ffffff"


def draw_hex_icon(painter: QPainter, state: TrayState) -> None:
    """在 64 × 64 座標系畫六角形圖示（呼叫端先 `scale`）。"""
    color = QColor(STATE_COLORS[state])
    hexagon = QPolygonF([QPointF(x, y) for x, y in _HEX])
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(_FACE)))
    painter.drawPolygon(hexagon)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if state is TrayState.DISCONNECTED:
        # 斷開的外框：每條邊只畫中間一段，六個角留缺口（Qt 虛線在小尺寸下幾乎看不出來）
        painter.setPen(QPen(color, 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        for a, b in zip(_HEX, (*_HEX[1:], _HEX[0]), strict=True):
            pa, pb = QPointF(*a), QPointF(*b)
            painter.drawLine(pa + (pb - pa) * 0.25, pa + (pb - pa) * 0.75)
    else:
        outline = QPen(color, 6)
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(outline)
        painter.drawPolygon(hexagon)

    spoke = QPen(QColor(_SPOKE), 3)
    spoke.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(spoke)
    for x, y in _SPOKES:
        painter.drawLine(QPointF(32, 32), QPointF(x, y))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawEllipse(QPointF(32, 32), 11, 11)
    painter.setBrush(QBrush(QColor(_MARK)))
    if state is TrayState.FAULT:
        painter.drawRoundedRect(QRectF(30.5, 25.5, 3, 8.5), 1.5, 1.5)
        painter.drawEllipse(QPointF(32, 37.5), 1.8, 1.8)
    elif state is TrayState.BUSY:
        painter.drawPolygon(QPolygonF([QPointF(27, 27), QPointF(37.5, 32), QPointF(27, 37)]))


def state_icon(state: TrayState) -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(icon_image(state, size)))
    return icon


def icon_image(state: TrayState, size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)
    draw_hex_icon(painter, state)
    painter.end()
    return image


def ico_bytes(state: TrayState = TrayState.NORMAL) -> bytes:
    """Windows .ico（打包時給 exe 用）：ICON_SIZES 各一張，每張以 PNG 存放。"""
    pngs = []
    for size in ICON_SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        icon_image(state, size).save(buffer, "PNG")
        pngs.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(header) + 16 * len(pngs)
    entries, payload = b"", b""
    for size, png in pngs:
        side = size % 256  # 256 在目錄中記為 0
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        payload += png
    return header + entries + payload


def app_icon() -> QIcon:
    """程式圖示（主視窗標題列、工作列）：系統匣圖示的「正常」版本。"""
    return state_icon(TrayState.NORMAL)


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
