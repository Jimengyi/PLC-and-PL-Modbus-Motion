"""表格滑鼠停留整列標示（spec FR-45）：以滑鼠事件移動，截取 viewport 檢查像素顏色。"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QMouseEvent
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

from plc_motion.ui.io_monitor import NAME_COL, STRIPE_COL, IoMonitorPage
from plc_motion.ui.points_panel import PointsPanel
from plc_motion.ui.row_hover import BAR_WIDTH, RowHoverDelegate, enable_row_hover
from plc_motion.ui.theme import color, load_qss


@pytest.fixture(autouse=True)
def themed():
    app = QApplication.instance()
    original = app.styleSheet()
    app.setStyleSheet(load_qss())
    yield
    app.setStyleSheet(original)


@pytest.fixture
def table(qtbot):
    t = QTableWidget(5, 3)
    for row in range(5):
        for col in range(3):
            t.setItem(row, col, QTableWidgetItem(""))
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.resize(400, 300)
    qtbot.addWidget(t)
    t.show()
    qtbot.waitExposed(t)
    return t


def move_to(qtbot, view, row: int, col: int = 1) -> None:
    """直接送 MouseMove 給 viewport；qtbot.mouseMove 會被實際游標位置干擾。"""
    viewport = view.viewport()
    pos = QPointF(view.visualRect(view.model().index(row, col)).center())
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        pos,
        QPointF(viewport.mapToGlobal(pos.toPoint())),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, event)


def pixel(view, row: int, col: int, x_offset: int | None = None) -> QColor:
    rect = view.visualRect(view.model().index(row, col))
    x = rect.left() + x_offset if x_offset is not None else rect.center().x()
    return view.viewport().grab().toImage().pixelColor(QPoint(x, rect.center().y()))


def same(a: QColor, b: QColor) -> bool:
    return a.rgb() == b.rgb()


def test_mouse_move_sets_hover_row_and_leave_clears_it(qtbot, table):
    delegate = enable_row_hover(table)
    move_to(qtbot, table, 3)
    assert delegate.hover_row == 3
    move_to(qtbot, table, 1)
    assert delegate.hover_row == 1
    QApplication.sendEvent(table.viewport(), QEvent(QEvent.Type.Leave))
    assert delegate.hover_row == -1


def test_hovered_row_gets_hover_background_and_bar_others_unchanged(qtbot, table):
    enable_row_hover(table, bar_column=0)
    plain = pixel(table, 2, 1)
    move_to(qtbot, table, 2)
    for col in range(3):
        assert same(pixel(table, 2, col), color("hover")), col
    assert same(pixel(table, 2, 0, x_offset=1), color("accent"))
    assert same(pixel(table, 2, 1, x_offset=1), color("hover"))  # 邊條只在列頭
    assert same(pixel(table, 1, 1), plain)


def test_selected_row_is_dark_blue_and_brighter_when_hovered(qtbot, table):
    enable_row_hover(table)
    table.selectRow(2)
    assert same(pixel(table, 2, 1), color("accent_dim"))  # Windows 11 style 不吃 QSS 選取色
    move_to(qtbot, table, 2)
    assert same(pixel(table, 2, 1), color("hover_selected"))


def test_cell_with_own_background_keeps_it(qtbot, table):
    enable_row_hover(table, bar_column=1)
    table.item(2, 0).setBackground(QBrush(color("fault")))
    move_to(qtbot, table, 2)
    assert same(pixel(table, 2, 0), color("fault"))
    assert same(pixel(table, 2, 1, x_offset=1), color("accent"))


def test_points_table_bar_on_first_column(config, fake_worker, qtbot):
    panel = PointsPanel(config, fake_worker)
    qtbot.addWidget(panel)
    delegate = panel.table.itemDelegate()
    assert isinstance(delegate, RowHoverDelegate)
    assert delegate.bar_column == 0


def test_io_tables_bar_beside_status_stripe(config, fake_worker, qtbot):
    page = IoMonitorPage(config, fake_worker)
    qtbot.addWidget(page)
    page.resize(1400, 800)
    page.show()
    qtbot.waitExposed(page)
    table = next(t for t in page.tables.values() if t.rowCount() and t.isVisible())
    delegate = table.itemDelegate()
    assert isinstance(delegate, RowHoverDelegate)
    assert delegate.bar_column == NAME_COL
    stripe = pixel(table, 0, STRIPE_COL)
    move_to(qtbot, table, 0, NAME_COL)
    assert same(pixel(table, 0, STRIPE_COL), stripe)
    assert same(pixel(table, 0, NAME_COL, x_offset=BAR_WIDTH - 1), color("accent"))
