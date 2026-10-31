"""IO Monitor 頁（spec FR-37 ~ FR-40）。

「數位」（Coil、Discrete Input）與「數值」（Holding Register）兩個分頁，每頁左欄 Read Only、
右欄 Read Write；Visible = X 的點不顯示，列順序依 io_points.csv。

- 每列：Check Status 色條、目前值（Boolean 燈號、U16 數字、Position 換算值＋單位）、
  名稱（B 接點加註「(B)」）、Tag Name、PLC 位置（Description）、Check Status。
- Read Write 的點雙擊寫入：Boolean 切換，數值點開 `ValueDialog`。寫入走 worker 的
  `write_point`（Position 傳 raw）；鎖定或尚未取得 PLC 狀態時不寫入。
- 右鍵選單設定 Check Status，立即經 `IoPointTable.save_check_status` 存檔；
  「全部重設為 Unchecked」需確認。上方顯示各狀態數量（只算畫面上的點）與文字搜尋。
"""

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.io_points import Access, CheckStatus, DataModel, IoPoint, SensorType
from plc_motion.settings.loader import AppConfig
from plc_motion.ui.jog_pad import format_value, lock_tooltip, segment_button
from plc_motion.ui.theme import color
from plc_motion.ui.value_dialog import ValueDialog
from plc_motion.worker import EngineStatus

KINDS = (("digital", "數位（Coil / DI）"), ("numeric", "數值（Holding Register）"))
STRIPE_COL, VALUE_COL, NAME_COL, TAG_COL, DESC_COL, STATUS_COL = range(6)
_HEADERS = ("", "值", "名稱", "Tag Name", "PLC 位置", "Check Status")
_TAG_ROLE = Qt.ItemDataRole.UserRole
_PLACEHOLDER = "—"
_LAMP = "●"
# 色條與數量統計用的醒目色；整列底色用 theme 的 check_<status>
_STRIPE_ROLE = {
    CheckStatus.OK: "ok",
    CheckStatus.NG: "fault",
    CheckStatus.UNCHECKED: "off",
    CheckStatus.RECHECK: "warn",
    CheckStatus.IGNORE: "border",
}


def kind_of(point: IoPoint) -> str:
    return "numeric" if point.data_model == DataModel.HOLDING_REGISTER else "digital"


def row_name(point: IoPoint) -> str:
    return f"{point.label}(B)" if point.is_b_contact else point.label


def _row_color(status: CheckStatus) -> QColor:
    return color(f"check_{status.value.lower()}")


class IoMonitorPage(QWidget):
    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__(parent)
        self._points = config.points
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self._where: dict[str, tuple[QTableWidget, int]] = {}
        self.tables: dict[tuple[str, Access], QTableWidget] = {}

        # ---- 上方列：分頁、搜尋、數量、重設 ----
        top = QHBoxLayout()
        self.kind_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key, text in KINDS:
            button = segment_button(text)
            button.toggled.connect(lambda on, k=key: on and self.show_kind(k))
            group.addButton(button)
            top.addWidget(button)
            self.kind_buttons[key] = button
        top.addSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋 Tag Name 或名稱")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search)
        top.addStretch(1)
        self.count_labels: dict[CheckStatus, QLabel] = {}
        self._swatches: dict[CheckStatus, QLabel] = {}
        for status in CheckStatus:
            swatch = QLabel()
            swatch.setObjectName("checkSwatch")
            label = QLabel()
            label.setObjectName("checkCount")
            top.addWidget(swatch)
            top.addWidget(label)
            top.addSpacing(8)
            self._swatches[status] = swatch
            self.count_labels[status] = label
        self.reset_button = QPushButton("全部重設為 Unchecked")
        self.reset_button.clicked.connect(self._reset_all)
        top.addWidget(self.reset_button)

        # ---- 兩個分頁 × 左右兩欄 ----
        self.pages = QStackedWidget()
        for key, _ in KINDS:
            page = QWidget()
            columns = QHBoxLayout(page)
            columns.setContentsMargins(0, 0, 0, 0)
            for access, title in (
                (Access.READ_ONLY, "輸入（PLC → PC，唯讀）"),
                (Access.READ_WRITE, "輸出（PC → PLC，雙擊寫入）"),
            ):
                box = QGroupBox(title)
                box_layout = QVBoxLayout(box)
                table = self._make_table(access)
                box_layout.addWidget(table)
                columns.addWidget(box, 1)
                self.tables[(key, access)] = table
            self.pages.addWidget(page)

        self.lock_label = QLabel()
        self.lock_label.setObjectName("lockHint")
        self.lock_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.lock_label)

        self._populate()
        self.kind_buttons["digital"].setChecked(True)
        self._update_counts()
        self._update_lock()
        worker.snapshot_ready.connect(self.on_snapshot)

    # ---- 建表 ----

    def _make_table(self, access: Access) -> QTableWidget:
        table = QTableWidget(0, len(_HEADERS))
        table.setHorizontalHeaderLabels(_HEADERS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(STRIPE_COL, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(STRIPE_COL, 6)
        table.setColumnWidth(VALUE_COL, 120)
        table.setColumnWidth(NAME_COL, 170)
        table.setColumnWidth(TAG_COL, 150)
        table.setColumnWidth(DESC_COL, 90)
        header.setStretchLastSection(True)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self._show_context_menu(t, pos)
        )
        if access == Access.READ_WRITE:
            table.cellDoubleClicked.connect(
                lambda row, _col, t=table: self._on_double_click(t, row)
            )
        return table

    def _populate(self) -> None:
        for point in self._points.all():
            if not point.visible:
                continue
            table = self.tables[(kind_of(point), point.access)]
            row = table.rowCount()
            table.insertRow(row)
            texts = {
                STRIPE_COL: "",
                VALUE_COL: _PLACEHOLDER,
                NAME_COL: row_name(point),
                TAG_COL: point.tag,
                DESC_COL: point.description,
                STATUS_COL: "",
            }
            for col, text in texts.items():
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(row, col, item)
            table.item(row, NAME_COL).setData(_TAG_ROLE, point.tag)
            table.item(row, VALUE_COL).setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                if point.sensor_type != SensorType.BOOLEAN
                else Qt.AlignmentFlag.AlignCenter
            )
            self._where[point.tag] = (table, row)
            self._paint_status(point)

    # ---- 查詢（測試與內部共用） ----

    def tag_at(self, table: QTableWidget, row: int) -> str:
        return table.item(row, NAME_COL).data(_TAG_ROLE)

    def locate(self, tag: str) -> tuple[QTableWidget, int]:
        return self._where[tag]

    def kind_of(self, tag: str) -> str:
        return kind_of(self._points.get(tag))

    def show_kind(self, kind: str) -> None:
        index = [k for k, _ in KINDS].index(kind)
        self.pages.setCurrentIndex(index)
        if not self.kind_buttons[kind].isChecked():
            self.kind_buttons[kind].setChecked(True)

    # ---- 值顯示 ----

    def on_snapshot(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self._snapshot = snapshot
        self._status = status
        for tag in self._where:
            self._paint_value(self._points.get(tag))
        self._update_lock()

    def _paint_value(self, point: IoPoint) -> None:
        table, row = self._where[point.tag]
        item = table.item(row, VALUE_COL)
        if self._snapshot is None:
            item.setText(_PLACEHOLDER)
            item.setForeground(QBrush())
            return
        if point.sensor_type == SensorType.BOOLEAN:
            on = bool(self._snapshot.raw(point.tag))
            item.setText(_LAMP)
            item.setForeground(QBrush(color("ok" if on else "off")))
        elif point.sensor_type == SensorType.POSITION:
            item.setText(format_value(self._snapshot.value(point.tag), point.scale, point.unit))
        else:
            item.setText(str(self._snapshot.raw(point.tag)))

    def _update_lock(self) -> None:
        tip = lock_tooltip(self._snapshot, self._status)
        self.lock_label.setText(f"不可寫入：{tip.replace(chr(10), ' ')}" if tip else "")
        for (_, access), table in self.tables.items():
            if access == Access.READ_WRITE:
                table.setToolTip(tip or "")

    # ---- 寫入 ----

    def _can_write(self) -> bool:
        return lock_tooltip(self._snapshot, self._status) is None

    def _on_double_click(self, table: QTableWidget, row: int) -> None:
        if not self._can_write():
            return
        point = self._points.get(self.tag_at(table, row))
        if point.sensor_type == SensorType.BOOLEAN:
            self._worker.write_point(point.tag, not bool(self._snapshot.raw(point.tag)))
            return
        dialog = ValueDialog(point, self._snapshot.value(point.tag), self)
        accepted = dialog.exec() == ValueDialog.DialogCode.Accepted
        # 視窗開著時可能已鎖定或斷線：確認後再檢查一次
        if accepted and dialog.raw is not None and self._can_write():
            self._worker.write_point(point.tag, dialog.raw)

    # ---- Check Status ----

    def check_status_menu(self, tag: str) -> QMenu:
        menu = QMenu(self)
        current = self._points.get(tag).check_status
        for status in CheckStatus:
            pixmap = QPixmap(12, 12)
            pixmap.fill(color(_STRIPE_ROLE[status]))
            action = menu.addAction(QIcon(pixmap), status.value)
            action.setCheckable(True)
            action.setChecked(status == current)
            action.triggered.connect(lambda _=False, s=status: self.set_check_status(tag, s))
        return menu

    def _show_context_menu(self, table: QTableWidget, pos: QPoint) -> None:
        item = table.itemAt(pos)
        if item is None:
            return
        menu = self.check_status_menu(self.tag_at(table, item.row()))
        menu.exec(table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def set_check_status(self, tag: str, status: CheckStatus) -> None:
        try:
            self._points.save_check_status(tag, status)
        except OSError as e:
            QMessageBox.warning(self, "Check Status 存檔失敗", f"{tag} 無法存檔：{e}")
            return
        self._paint_status(self._points.get(tag))
        self._update_counts()

    def _reset_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "全部重設為 Unchecked",
            "把 io_points.csv 全部點位的 Check Status 重設為 Unchecked？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._points.reset_all_check_status()
        except OSError as e:
            QMessageBox.warning(self, "Check Status 存檔失敗", f"無法存檔：{e}")
            return
        self._repaint_statuses()

    def _paint_status(self, point: IoPoint) -> None:
        table, row = self._where[point.tag]
        status = point.check_status
        table.item(row, STRIPE_COL).setBackground(QBrush(color(_STRIPE_ROLE[status])))
        background = QBrush(_row_color(status))
        for col in range(VALUE_COL, len(_HEADERS)):
            table.item(row, col).setBackground(background)
        table.item(row, STATUS_COL).setText(status.value)

    def _repaint_statuses(self) -> None:
        for tag in self._where:
            self._paint_status(self._points.get(tag))
        self._update_counts()

    def _update_counts(self) -> None:
        counts = dict.fromkeys(CheckStatus, 0)
        for tag in self._where:
            counts[self._points.get(tag).check_status] += 1
        for status, label in self.count_labels.items():
            label.setText(f"{status.value} {counts[status]}")
            hex_color = color(_STRIPE_ROLE[status]).name()
            self._swatches[status].setText(f'<span style="color:{hex_color}">■</span>')

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange) and hasattr(
            self, "count_labels"
        ):
            # 主題切換：依新主題色重畫 Check Status 與燈號
            self._repaint_statuses()
            for tag in self._where:
                self._paint_value(self._points.get(tag))

    # ---- 搜尋 ----

    def _apply_filter(self, text: str) -> None:
        query = text.strip().casefold()
        for tag, (table, row) in self._where.items():
            point = self._points.get(tag)
            match = not query or query in tag.casefold() or query in point.label.casefold()
            table.setRowHidden(row, not match)
