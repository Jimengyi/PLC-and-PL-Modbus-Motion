"""通訊計時：狀態列的讀取時間指示與「通訊品質」視窗。

讀取時間＝一輪輪詢依序送完所有讀取請求、讀完全部位址的耗時（worker 的 `PollTiming`）。
狀態列顯示本次／平均／最大、請求筆數與輪詢週期；左側圓點依本次讀取佔週期的比例變色
（> 80 % 警告、> 100 % 異常），百分比在滑鼠提示。
點狀態列開「通訊品質」視窗：最近 300 輪的曲線、統計、
建議週期（95 % 讀取時間 × 1.5，取整到 10 ms，最少 50 ms）。輪詢週期只在 device.toml 調整。
統計在每次連線時與按「重新統計」時歸零；斷線次數只在「重新統計」時歸零。
"""

import math
from collections import deque
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from plc_motion.ui.theme import color, set_state
from plc_motion.worker import PollTiming

HISTORY = 300
WARN_RATIO = 0.8
MIN_SUGGESTED_MS = 50


def load_state(read_ms: float, interval_ms: int) -> str:
    """讀取時間相對輪詢週期的狀態：ok、warn（> 80 %）、fault（> 100 %）。"""
    if read_ms > interval_ms:
        return "fault"
    if read_ms > interval_ms * WARN_RATIO:
        return "warn"
    return "ok"


class CommStats:
    """最近 `HISTORY` 輪的讀取時間與週期；平均、最小、最大為歸零後的全部輪數。"""

    def __init__(self):
        self.disconnects = 0
        self.reset()

    def reset(self) -> None:
        self.reads: deque[float] = deque(maxlen=HISTORY)
        self.cycles: deque[float] = deque(maxlen=HISTORY)
        self.count = 0
        self._sum = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.requests = 0
        self.since = datetime.now()

    def reset_all(self) -> None:
        self.disconnects = 0
        self.reset()

    def add(self, timing: PollTiming) -> None:
        self.reads.append(timing.read_ms)
        if timing.cycle_ms is not None:
            self.cycles.append(timing.cycle_ms)
        self.count += 1
        self._sum += timing.read_ms
        self.minimum = timing.read_ms if self.minimum is None else min(self.minimum, timing.read_ms)
        self.maximum = timing.read_ms if self.maximum is None else max(self.maximum, timing.read_ms)
        self.requests = timing.requests

    @property
    def last(self) -> float | None:
        return self.reads[-1] if self.reads else None

    @property
    def average(self) -> float | None:
        return self._sum / self.count if self.count else None

    @property
    def p95(self) -> float | None:
        """最近 `HISTORY` 輪中 95 % 的讀取時間在此以內。"""
        if not self.reads:
            return None
        ordered = sorted(self.reads)
        return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]

    @property
    def cycle_average(self) -> float | None:
        return sum(self.cycles) / len(self.cycles) if self.cycles else None

    def suggested_interval(self) -> int | None:
        if self.p95 is None:
            return None
        return max(MIN_SUGGESTED_MS, math.ceil(self.p95 * 1.5 / 10) * 10)

    def verdict(self, interval_ms: int) -> tuple[str, str]:
        if self.maximum is None:
            return "off", "尚無資料：連線後開始統計"
        if self.maximum > interval_ms:
            return "fault", "有讀取超過輪詢週期，畫面與交握判斷會延遲"
        if self.p95 > interval_ms * WARN_RATIO:
            return "warn", "讀取時間接近輪詢週期，餘裕不足"
        return "ok", "通訊正常，讀取時間在週期內有足夠餘裕"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ms"


class CommIndicator(QFrame):
    """狀態列右側的通訊計時；點選發 `clicked`。"""

    clicked = Signal()

    def __init__(self, interval_ms: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("commBar")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("點選查看通訊品質")
        self._interval = interval_ms
        self.dot = QLabel()
        self.dot.setTextFormat(Qt.TextFormat.RichText)
        self.read_value = _value_label()
        self.average_value = _value_label()
        self.max_value = _value_label()
        self.requests_value = _value_label()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(6)
        layout.addWidget(self.dot)
        layout.addWidget(QLabel("讀取"))
        layout.addWidget(self.read_value)
        for caption, value in (("平均", self.average_value), ("最大", self.max_value)):
            layout.addWidget(_separator())
            layout.addWidget(QLabel(caption))
            layout.addWidget(value)
        layout.addWidget(_separator())
        layout.addWidget(self.requests_value)
        layout.addWidget(QLabel("筆請求"))
        layout.addWidget(_separator())
        layout.addWidget(QLabel("輪詢週期"))
        layout.addWidget(_value_label(f"{interval_ms} ms"))
        self.update_stats(CommStats())

    def update_stats(self, stats: CommStats) -> None:
        last = stats.last
        state = "off" if last is None else load_state(last, self._interval)
        self.dot.setText(f'<span style="color:{color(state).name()}">●</span>')
        set_state(self.dot, state)
        self.read_value.setText(_ms(last))
        self.average_value.setText(_ms(stats.average))
        self.max_value.setText(_ms(stats.maximum))
        self.requests_value.setText(str(stats.requests) if stats.requests else "—")
        tip = "點選查看通訊品質"
        if last is not None:
            tip = f"本次讀取佔輪詢週期 {round(last / self._interval * 100)}%\n{tip}"
        self.setToolTip(tip)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class LoadChart(QWidget):
    """最近 `HISTORY` 輪的讀取時間折線；紅虛線＝輪詢週期，黃虛線＝週期的 80 %。"""

    def __init__(self, interval_ms: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._interval = interval_ms
        self._values: list[float] = []
        self.setMinimumHeight(130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_values(self, values) -> None:
        self._values = list(values)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillRect(rect, color("inset"))
        painter.setPen(QPen(color("border"), 1))
        painter.drawRect(rect)
        top = max(self._interval * 1.2, *self._values) if self._values else self._interval * 1.2

        def y(v: float) -> float:
            return rect.bottom() - v / top * (rect.height() - 4)

        for ratio, role in ((1.0, "fault"), (WARN_RATIO, "warn")):
            pen = QPen(color(role), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            line_y = y(self._interval * ratio)
            painter.drawLine(QPointF(rect.left(), line_y), QPointF(rect.right(), line_y))
        if len(self._values) >= 2:
            step = rect.width() / (HISTORY - 1)
            points = QPolygonF(
                [QPointF(rect.left() + i * step, y(v)) for i, v in enumerate(self._values)]
            )
            painter.setPen(QPen(color("accent"), 1.5))
            painter.drawPolyline(points)
        painter.end()


class CommDialog(QDialog):
    """通訊品質（非強制回應）：開著時每輪更新；「重新統計」發 `reset_requested`。"""

    reset_requested = Signal()

    def __init__(self, interval_ms: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("通訊品質")
        self.setMinimumWidth(680)
        self._interval = interval_ms

        self.verdict = QLabel()
        self.verdict.setObjectName("verdict")
        self.history_label = QLabel()
        self.history_label.setObjectName("hint")
        self.chart = LoadChart(interval_ms)
        legend = QLabel(
            f'<span style="color:{color("fault").name()}">- - -</span> 輪詢週期 {interval_ms} ms'
            f"（超過即延遲）　"
            f'<span style="color:{color("warn").name()}">- - -</span> 週期的 80%　'
            f'<span style="color:{color("accent").name()}">———</span> 每輪讀取時間'
        )
        legend.setObjectName("hint")

        self.tiles: dict[str, QLabel] = {}
        tiles = QGridLayout()
        tiles.setSpacing(6)
        for i, (key, caption) in enumerate(
            (
                ("last", "本次"),
                ("average", "平均"),
                ("minimum", "最小"),
                ("maximum", "最大"),
                ("p95", "95% 在此以內"),
                ("cycle", "實際週期（平均）"),
                ("disconnects", "斷線次數"),
                ("count", "統計輪數"),
            )
        ):
            tile = QFrame()
            tile.setObjectName("statTile")
            box = QVBoxLayout(tile)
            box.setContentsMargins(10, 6, 10, 6)
            box.setSpacing(0)
            title = QLabel(caption)
            title.setObjectName("hint")
            value = QLabel()
            value.setObjectName("statValue")
            box.addWidget(title)
            box.addWidget(value)
            tiles.addWidget(tile, i // 4, i % 4)
            self.tiles[key] = value

        info = QFrame()
        info.setObjectName("infoBox")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(10, 8, 10, 8)
        info_layout.addWidget(
            QLabel(f"設定輪詢週期 <b>{interval_ms} ms</b>（device.toml 的 [polling] interval_ms）")
        )
        self.suggest_label = QLabel()
        info_layout.addWidget(self.suggest_label)
        self.since_label = QLabel()
        self.since_label.setObjectName("hint")

        self.reset_button = QPushButton("重新統計")
        self.reset_button.clicked.connect(self.reset_requested)
        close_button = QPushButton("關閉")
        close_button.setObjectName("primary")
        close_button.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(self.reset_button)
        footer.addStretch(1)
        footer.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)
        layout.addWidget(self.verdict)
        layout.addWidget(self.history_label)
        layout.addWidget(self.chart)
        layout.addWidget(legend)
        layout.addLayout(tiles)
        layout.addWidget(info)
        layout.addWidget(self.since_label)
        layout.addLayout(footer)

    def update_stats(self, stats: CommStats) -> None:
        state, text = stats.verdict(self._interval)
        self.verdict.setText(f'<span style="color:{color(state).name()}">●</span>&nbsp;{text}')
        set_state(self.verdict, state)
        requests = stats.requests or "—"
        self.history_label.setText(
            f"最近 {len(stats.reads)} 輪的讀取時間（一輪＝依序送出 {requests} 筆讀取請求、"
            "讀完所有位址）"
        )
        self.chart.set_values(stats.reads)
        values = {
            "last": _ms(stats.last),
            "average": _ms(stats.average),
            "minimum": _ms(stats.minimum),
            "maximum": _ms(stats.maximum),
            "p95": _ms(stats.p95),
            "cycle": _ms(stats.cycle_average),
            "disconnects": str(stats.disconnects),
            "count": str(stats.count),
        }
        for key, text in values.items():
            self.tiles[key].setText(text)
        suggested = stats.suggested_interval()
        self.suggest_label.setText(
            "建議週期 —（尚無資料）"
            if suggested is None
            else f"建議週期 <b>≥ {suggested} ms</b>　"
            f'<span style="color:{color("text_dim").name()}">＝ 95% 讀取時間 × 1.5，'
            f"取整到 10 ms，最少 {MIN_SUGGESTED_MS} ms</span>"
        )
        self.since_label.setText(
            f"統計自 {stats.since:%H:%M:%S} 起（連線時或按「重新統計」時歸零；"
            "斷線次數只在按「重新統計」時歸零）"
        )

    def popup(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()


def _value_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("commValue")
    return label


def _separator() -> QFrame:
    line = QFrame()
    line.setObjectName("commSep")
    line.setFixedHeight(14)
    return line
