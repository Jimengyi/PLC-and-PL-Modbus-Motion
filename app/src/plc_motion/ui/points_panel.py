"""點位表面板（spec FR-34 ~ FR-36）：P001 ~ P200 表格＋操作面板。

資料與存檔都在 `PositionTable`（任何修改立即原子存檔）；面板只負責顯示與呼叫。
移動走 worker 的 `move(relative=False)`，只送勾選的軸。座標超出該軸軟體極限的格子以異常色標示
（匯入只驗格式，極限檢查在這裡）。鎖定（`EngineStatus.locked`）時存入、編輯、匯入、移動皆不可用；
尚未取得 PLC 狀態時仍可編輯與匯入（不碰 PLC），但存入目前位置與移動需要快照。
"""

import math
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from plc_motion.codec import decimals_for
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.settings.positions import HEADER, PositionRow, PositionTable
from plc_motion.ui.jog_pad import SPEED_MAX, SPEED_MIN, lock_tooltip
from plc_motion.ui.theme import color
from plc_motion.worker import EngineStatus

# 第 1 ~ 7 軸依序對應欄位 2 ~ 8
AXIS_FIELDS = {1: "x", 2: "y", 3: "z", 4: "rx", 5: "ry", 6: "rz", 7: "lift"}
NAME_COL, FIRST_COORD_COL, DESC_COL = 1, 2, 9
_MAX_SHOWN_ERRORS = 12


class PointsPanel(QGroupBox):
    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__("點位表", parent)
        self._config = config
        self._positions = config.positions
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self._editable = True
        self._loading = False
        self._fields = {n: AXIS_FIELDS[n] for n in sorted(config.axes)}

        self.table = QTableWidget(self._positions.POINT_COUNT, len(HEADER))
        headers = list(HEADER)
        headers[0] = "點位"
        headers[NAME_COL] = "名稱"
        headers[DESC_COL] = "說明"
        for n in self._fields:
            unit = config.points.axis_tag("Current Position", n).unit
            headers[FIRST_COORD_COL + n - 1] = f"{config.axes[n].name} ({unit})"
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(NAME_COL, 90)
        for col in range(FIRST_COORD_COL, DESC_COL):
            self.table.setColumnWidth(col, 78)
        self._populate()
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_controls)
        self.table.currentCellChanged.connect(lambda *_: self._update_controls())

        self.selected_label = QLabel()
        self.selected_label.setObjectName("selectedTitle")
        self.axis_checks: dict[int, QCheckBox] = {}
        checks = QGridLayout()  # 4 + 3 兩列，標籤用完整軸名
        checks.setHorizontalSpacing(12)
        for i, n in enumerate(self._fields):
            box = QCheckBox(config.axes[n].name)
            box.setChecked(True)
            box.toggled.connect(self._update_controls)
            checks.addWidget(box, i // 4, i % 4)
            self.axis_checks[n] = box
        checks.setColumnStretch(4, 1)
        self.speed = QSpinBox()
        self.speed.setRange(SPEED_MIN, SPEED_MAX)
        self.speed.setSuffix(" %")
        self.speed.setValue(min(a.default_speed for a in config.axes.values()))
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("速度"))
        speed_row.addWidget(self.speed)
        speed_row.addStretch(1)

        self.move_button = QPushButton("▶ 移動到此點")
        self.move_button.clicked.connect(self._move_to_point)
        self.save_button = QPushButton("存入目前位置")
        self.save_button.clicked.connect(self._save_current)
        self.import_button = QPushButton("匯入 CSV…")
        self.import_button.clicked.connect(self._import)
        self.export_button = QPushButton("匯出 CSV…")
        self.export_button.clicked.connect(self._export)
        self.lock_label = QLabel()
        self.lock_label.setObjectName("lockHint")
        self.lock_label.setWordWrap(True)

        # 操作區：左欄選取點位、勾選軸、速度；右欄按鈕
        left = QVBoxLayout()
        left.addWidget(self.selected_label)
        left.addWidget(QLabel("移動的軸（勾選）"))
        left.addLayout(checks)
        left.addLayout(speed_row)
        left.addStretch(1)
        buttons = QGridLayout()
        buttons.addWidget(self.move_button, 0, 0)
        buttons.addWidget(self.save_button, 1, 0)
        buttons.addWidget(self.import_button, 0, 1)
        buttons.addWidget(self.export_button, 1, 1)
        buttons.setRowStretch(2, 1)
        ops_row = QHBoxLayout()
        ops_row.addLayout(left, 1)
        ops_row.addLayout(buttons, 1)
        ops = QVBoxLayout()
        ops.addLayout(ops_row)
        ops.addWidget(self.lock_label)
        self.ops_box = QGroupBox("點位操作")
        self.ops_box.setLayout(ops)

        # 預設表格在上、操作區在下；軸控頁會以 take_ops_box() 把操作區移到步進鍵盤下方
        self._layout = QVBoxLayout(self)
        self._layout.addWidget(self.table, 1)
        self._layout.addWidget(self.ops_box, 0)

        worker.snapshot_ready.connect(self.on_snapshot)
        self._update_controls()

    def take_ops_box(self) -> QGroupBox:
        """把操作區從本面板的版面移出，交給呼叫端擺放（面板只剩表格）。"""
        self._layout.removeWidget(self.ops_box)
        return self.ops_box

    # ---- 表格內容 ----

    def reload(self) -> None:
        """從 `PositionTable` 重新填入全部列（匯入後、外部修改後呼叫）。"""
        self._populate()
        self._update_controls()

    def _populate(self) -> None:
        self._loading = True
        try:
            for i, row in enumerate(self._positions.rows()):
                self._fill_row(i, row)
        finally:
            self._loading = False

    def _fill_row(self, i: int, row: PositionRow) -> None:
        self._set_item(i, 0, row.point_id, editable=False)
        self._set_item(i, NAME_COL, row.name)
        for n, field in self._fields.items():
            col = FIRST_COORD_COL + n - 1
            value = getattr(row, field)
            self._set_item(i, col, self._format(n, value))
            self._mark_limit(self.table.item(i, col), n, value)
        self._set_item(i, DESC_COL, row.description)

    def _set_item(self, i: int, col: int, text: str, editable: bool = True) -> None:
        item = self.table.item(i, col)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(i, col, item)
        item.setText(text)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if editable and self._editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        if FIRST_COORD_COL <= col < DESC_COL:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _format(self, n: int, value: float) -> str:
        scale = self._config.points.axis_tag("Current Position", n).scale
        return f"{value:.{decimals_for(scale)}f}"

    def _mark_limit(self, item: QTableWidgetItem, n: int, value: float) -> None:
        axis = self._config.axes[n]
        if value < axis.soft_limit_min or value > axis.soft_limit_max:
            item.setBackground(QBrush(QColor(color("check_ng"))))
            item.setToolTip(f"超出軟體極限 {axis.soft_limit_min} ~ {axis.soft_limit_max}")
        else:
            item.setBackground(QBrush())
            item.setToolTip("")

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange) and hasattr(
            self, "table"
        ):
            self._remark_limits()

    def _remark_limits(self) -> None:
        """主題切換後依新主題色重畫超限標記。"""
        self._loading = True
        try:
            for i, row in enumerate(self._positions.rows()):
                for n, field in self._fields.items():
                    item = self.table.item(i, FIRST_COORD_COL + n - 1)
                    if item is not None:
                        self._mark_limit(item, n, getattr(row, field))
        finally:
            self._loading = False

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            return rows[0].row()
        row = self.table.currentRow()
        return row if row >= 0 else None

    def _selected_point(self) -> PositionRow | None:
        i = self._selected_row()
        return self._positions.rows()[i] if i is not None else None

    # ---- 編輯 ----

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        i, col = item.row(), item.column()
        if col == NAME_COL:
            self._store(i, name=item.text())
        elif col == DESC_COL:
            self._store(i, description=item.text())
        elif FIRST_COORD_COL <= col < DESC_COL:
            field = self._fields[col - FIRST_COORD_COL + 1]
            try:
                value = float(item.text())
                if not math.isfinite(value):
                    raise ValueError
            except ValueError:
                self._refresh_row(i)  # 不是有限數字：還原顯示
                return
            self._store(i, **{field: value})
        else:
            self._refresh_row(i)

    def _store(self, i: int, **fields) -> None:
        point_id = self._positions.rows()[i].point_id
        try:
            self._positions.update(point_id, **fields)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "點位存檔失敗", f"{point_id} 無法存檔：{e}")
        self._refresh_row(i)

    def _refresh_row(self, i: int) -> None:
        self._loading = True
        try:
            self._fill_row(i, self._positions.rows()[i])
        finally:
            self._loading = False

    # ---- 命令 ----

    def _save_current(self) -> None:
        i = self._selected_row()
        if i is None or self._snapshot is None:
            return
        self._store(i, **{f: self._snapshot.axis(n).position for n, f in self._fields.items()})

    def _move_to_point(self) -> None:
        row = self._selected_point()
        if row is None:
            return
        targets = {
            n: getattr(row, self._fields[n])
            for n, box in self.axis_checks.items()
            if box.isChecked()
        }
        if targets:
            self._worker.move(targets, False, self.speed.value())

    def _import(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "匯入點位表", "", "CSV (*.csv);;所有檔案 (*)")
        if not name:
            return
        path = Path(name)
        errors = self._check_file(path)
        if errors:
            shown = errors[:_MAX_SHOWN_ERRORS]
            if len(errors) > len(shown):
                shown.append(f"…另有 {len(errors) - len(shown)} 項")
            QMessageBox.warning(self, "匯入失敗", "點位表未變動。\n\n" + "\n".join(shown))
            return
        answer = QMessageBox.question(
            self,
            "匯入點位表",
            f"以 {path.name} 取代目前全部 {self._positions.POINT_COUNT} 個點位？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors = self._positions.import_file(path)
        if errors:
            QMessageBox.warning(self, "匯入失敗", "點位表未變動。\n\n" + "\n".join(errors))
            return
        self.reload()

    def _check_file(self, path: Path) -> list[str]:
        """在暫存目錄的空表上試匯入，只為取得驗證錯誤；真正的表與檔不動。"""
        with tempfile.TemporaryDirectory() as tmp:
            return PositionTable(Path(tmp) / "check.csv", []).import_file(path)

    def _export(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "匯出點位表", "positions.csv", "CSV (*.csv);;所有檔案 (*)"
        )
        if not name:
            return
        try:
            self._positions.export_file(Path(name))
        except OSError as e:
            QMessageBox.warning(self, "匯出失敗", f"無法寫入 {name}：{e}")

    # ---- worker signal ----

    def on_snapshot(self, snapshot: Snapshot | None, status: EngineStatus) -> None:
        self._snapshot = snapshot
        self._status = status
        self._update_controls()

    def _update_controls(self) -> None:
        locked = self._status is not None and self._status.locked
        tip = lock_tooltip(self._snapshot, self._status)
        self.lock_label.setText(tip.replace("\n", " ") if tip else "")
        row = self._selected_point()
        self.selected_label.setText(
            "未選取點位" if row is None else f"{row.point_id}　{row.name}".rstrip()
        )
        no_point = None if row is not None else "請先選取點位"
        save_reason = tip or no_point
        self.save_button.setEnabled(save_reason is None)
        self.save_button.setToolTip(save_reason or "")
        move_reason = tip or no_point
        if move_reason is None and not any(b.isChecked() for b in self.axis_checks.values()):
            move_reason = "請先勾選軸"
        self.move_button.setEnabled(move_reason is None)
        self.move_button.setToolTip(move_reason or "")
        self.import_button.setEnabled(not locked)
        self.import_button.setToolTip(tip if locked else "")
        self._set_editable(not locked)

    def _set_editable(self, editable: bool) -> None:
        if editable == self._editable:
            return
        self._editable = editable
        self._loading = True  # setFlags 會發 itemChanged，不能被當成使用者編輯而存檔
        try:
            for i in range(self.table.rowCount()):
                for col in range(1, len(HEADER)):
                    item = self.table.item(i, col)
                    flags = item.flags()
                    if editable:
                        item.setFlags(flags | Qt.ItemFlag.ItemIsEditable)
                    else:
                        item.setFlags(flags & ~Qt.ItemFlag.ItemIsEditable)
        finally:
            self._loading = False
