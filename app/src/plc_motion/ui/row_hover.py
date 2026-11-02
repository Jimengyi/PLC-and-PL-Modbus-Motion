"""表格的選取與滑鼠停留整列標示。

- 已選取列：深藍底（accent_dim）、白字
- 滑鼠停留列：淡灰底＋列頭亮藍邊條；已選取列停留時深藍再亮一階（hover_selected）

兩者都由 delegate 自行繪製，原因：
- QSS 的 `::item:hover` 只會亮滑鼠所在的那一格，無法整列加邊條
- Windows 11 style 不採用 QSS 的 `selection-background-color`，選取列只剩極淡的底色

格子自己設了底色（IO 狀態色條、點位超出極限）時保留原色；自己設了字色時保留原字色。
"""

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPersistentModelIndex, QRect, Qt
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from plc_motion.ui.theme import color

BAR_WIDTH = 4

_STYLE_STATES = (
    QStyle.StateFlag.State_Selected
    | QStyle.StateFlag.State_MouseOver
    | QStyle.StateFlag.State_HasFocus
)


class RowHoverDelegate(QStyledItemDelegate):
    def __init__(self, view: QAbstractItemView, bar_column: int) -> None:
        super().__init__(view)
        self._view = view
        self.bar_column = bar_column
        self.hover_row = -1
        view.setItemDelegate(self)
        view.setMouseTracking(True)
        view.viewport().installEventFilter(self)
        view.verticalScrollBar().valueChanged.connect(self._follow_cursor)

    def set_hover_row(self, row: int) -> None:
        if row == self.hover_row:
            return
        self.hover_row = row
        self._view.viewport().update()

    def _follow_cursor(self) -> None:
        """捲動時游標不動、底下的列換了，重新判斷停留列。"""
        viewport = self._view.viewport()
        pos = viewport.mapFromGlobal(viewport.cursor().pos())
        self.set_hover_row(self._view.rowAt(pos.y()) if viewport.rect().contains(pos) else -1)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            self.set_hover_row(self._view.rowAt(event.position().toPoint().y()))
        elif kind == QEvent.Type.Leave:
            self.set_hover_row(-1)
        return False

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hovered = index.row() == self.hover_row
        # 選取與停留的效果都自己畫，不交給 style
        opt.state &= ~_STYLE_STATES
        # 格子自己的底色由下面的 drawControl 畫在這層之上，所以會保留
        if selected or hovered:
            if selected:
                fill = color("hover_selected" if hovered else "accent_dim")
            else:
                fill = color("hover")
            painter.fillRect(opt.rect, fill)
        if selected and index.data(Qt.ItemDataRole.ForegroundRole) is None:
            opt.palette.setColor(QPalette.ColorRole.Text, color("white"))
        cell = QRect(opt.rect)
        if index.column() == self.bar_column:
            # 邊條欄的文字一律讓出邊條寬度，停留時文字不會被蓋住或跳動
            opt.rect = cell.adjusted(BAR_WIDTH, 0, 0, 0)
        widget = opt.widget or self._view
        widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        if hovered and index.column() == self.bar_column:
            bar = QRect(cell.left(), cell.top(), BAR_WIDTH, cell.height())
            painter.fillRect(bar, color("accent"))


def enable_row_hover(view: QAbstractItemView, bar_column: int = 0) -> RowHoverDelegate:
    return RowHoverDelegate(view, bar_column)
