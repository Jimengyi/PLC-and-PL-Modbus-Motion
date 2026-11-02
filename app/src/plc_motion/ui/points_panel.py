"""點位面板（spec FR-34 ~ FR-36）：P001 ~ P200 表格＋操作區。

資料與存檔都在 `PositionTable`（任何修改立即原子存檔）；面板只負責顯示與呼叫。
表格不能就地編輯：雙擊點位列或按「編輯此點」開 `PointEditDialog`，確認存檔才寫回。
存入目前位置、清除此點、移動到此點都先跳確認視窗（列出各軸 目前 → 存入後／清除後／目標）。
移動走 worker 的 `move(relative=False)`，只送選取的軸。座標超出該軸軟體極限的格子以異常色標示
（匯入只驗格式，極限檢查在這裡）。鎖定（`EngineStatus.locked`）時編輯、清除、存入、匯入、移動
皆不可用；尚未取得 PLC 狀態時仍可編輯、清除與匯入（不碰 PLC），但存入目前位置與移動需要快照。
"""

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from plc_motion.codec import decimals_for
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.loader import AppConfig
from plc_motion.settings.positions import HEADER, PositionRow, PositionTable
from plc_motion.ui import confirm_dialog
from plc_motion.ui.confirm_dialog import ConfirmRow
from plc_motion.ui.jog_pad import lock_tooltip
from plc_motion.ui.point_edit_dialog import AXIS_FIELDS, PointEditDialog
from plc_motion.ui.row_hover import enable_row_hover
from plc_motion.ui.theme import color
from plc_motion.ui.widgets import Panel, SpeedStepper, hint, named_button
from plc_motion.worker import EngineStatus

# 第 1 ~ 7 軸依序對應欄位 2 ~ 8；說明欄（9）不顯示，在編輯視窗修改
NAME_COL, FIRST_COORD_COL, DESC_COL = 1, 2, 9
_MAX_SHOWN_ERRORS = 12


class PointsPanel(Panel):
    def __init__(self, config: AppConfig, worker, parent: QWidget | None = None):
        super().__init__("點位", "點選一列後操作，雙擊編輯", parent)
        self._config = config
        self._positions = config.positions
        self._worker = worker
        self._snapshot: Snapshot | None = None
        self._status: EngineStatus | None = None
        self._fields = {n: AXIS_FIELDS[n] for n in sorted(config.axes)}

        self.import_button = named_button("匯入 CSV…", "small")
        self.import_button.clicked.connect(self._import)
        self.export_button = named_button("匯出 CSV…", "small")
        self.export_button.clicked.connect(self._export)
        self.header_layout.addWidget(self.import_button)
        self.header_layout.addWidget(self.export_button)

        self.table = QTableWidget(self._positions.POINT_COUNT, len(HEADER))
        headers = list(HEADER)
        headers[0] = "點位"
        headers[NAME_COL] = "名稱"
        headers[DESC_COL] = "說明"
        for n in self._fields:
            headers[FIRST_COORD_COL + n - 1] = config.axes[n].name
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnHidden(DESC_COL, True)
        self.row_hover = enable_row_hover(self.table, bar_column=0)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 52), (NAME_COL, 72)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(col, width)
        for col in range(FIRST_COORD_COL, DESC_COL):
            item = self.table.horizontalHeaderItem(col)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._populate()
        self.table.itemSelectionChanged.connect(self._update_controls)
        self.table.currentCellChanged.connect(lambda *_: self._update_controls())
        self.table.cellDoubleClicked.connect(lambda row, _col: self._edit_row(row))

        # 編輯列：已選、編輯此點、存入目前位置、清除此點
        self.selected_label = QLabel()
        self.selected_label.setObjectName("selectedTitle")
        self.edit_button = QPushButton("✎ 編輯此點")
        self.edit_button.clicked.connect(lambda: self._edit_row(self._selected_row()))
        self.save_button = QPushButton("存入目前位置")
        self.save_button.clicked.connect(self._save_current)
        self.clear_button = named_button("✕ 清除此點", "danger")
        self.clear_button.clicked.connect(self._clear_point)
        edit_row = QHBoxLayout()
        edit_row.addWidget(hint("已選"))
        edit_row.addWidget(self.selected_label)
        edit_row.addStretch(1)
        edit_row.addWidget(self.edit_button)
        edit_row.addWidget(self.save_button)
        edit_row.addWidget(self.clear_button)

        # 移動列：移動的軸（可切換按鈕）、速度、移動到此點
        self.axis_checks: dict[int, QPushButton] = {}
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(hint("移動的軸"))
        for n in self._fields:
            chip = named_button(config.axes[n].name, "chip")
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.toggled.connect(self._update_controls)
            chips.addWidget(chip)
            self.axis_checks[n] = chip
        chips.addStretch(1)
        self.speed_stepper = SpeedStepper(min(a.default_speed for a in config.axes.values()))
        self.speed = self.speed_stepper.spin
        self.move_button = named_button("▶ 移動到此點", "primary")
        self.move_button.setMinimumHeight(46)
        self.move_button.clicked.connect(self._move_to_point)
        move_row = QHBoxLayout()
        move_row.addWidget(hint("速度"))
        move_row.addWidget(self.speed_stepper)
        move_row.addStretch(1)
        move_row.addWidget(self.move_button)

        self.lock_label = QLabel()
        self.lock_label.setObjectName("lockHint")
        self.lock_label.setWordWrap(True)

        self.body_layout.addWidget(self.table, 1)
        self.body_layout.addLayout(edit_row)
        self.body_layout.addLayout(chips)
        self.body_layout.addLayout(move_row)
        self.body_layout.addWidget(self.lock_label)

        worker.snapshot_ready.connect(self.on_snapshot)
        self._update_controls()

    # ---- 表格內容 ----

    def reload(self) -> None:
        """從 `PositionTable` 重新填入全部列（匯入後、外部修改後呼叫）。"""
        self._populate()
        self._update_controls()

    def _populate(self) -> None:
        for i, row in enumerate(self._positions.rows()):
            self._fill_row(i, row)

    def _fill_row(self, i: int, row: PositionRow) -> None:
        self._set_item(i, 0, row.point_id)
        self._set_item(i, NAME_COL, row.name)
        self.table.item(i, NAME_COL).setToolTip(row.description)
        for n, field in self._fields.items():
            col = FIRST_COORD_COL + n - 1
            value = getattr(row, field)
            self._set_item(i, col, self._format(n, value))
            self._mark_limit(self.table.item(i, col), n, value)
        self._set_item(i, DESC_COL, row.description)

    def _set_item(self, i: int, col: int, text: str) -> None:
        item = self.table.item(i, col)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if FIRST_COORD_COL <= col < DESC_COL:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(i, col, item)
        item.setText(text)

    def _format(self, n: int, value: float) -> str:
        scale = self._config.points.axis_tag("Current Position", n).scale
        return f"{value:.{decimals_for(scale)}f}"

    def _unit(self, n: int) -> str:
        return self._config.points.axis_tag("Current Position", n).unit or ""

    def _mark_limit(self, item: QTableWidgetItem, n: int, value: float) -> None:
        axis = self._config.axes[n]
        if value < axis.soft_limit_min or value > axis.soft_limit_max:
            item.setBackground(QBrush(color("check_ng")))
            item.setToolTip(f"超出軟體極限 {axis.soft_limit_min} ~ {axis.soft_limit_max}")
        else:
            item.setBackground(QBrush())
            item.setToolTip("")

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            return rows[0].row()
        row = self.table.currentRow()
        return row if row >= 0 else None

    def _selected_point(self) -> PositionRow | None:
        i = self._selected_row()
        return self._positions.rows()[i] if i is not None else None

    @staticmethod
    def _label(row: PositionRow) -> str:
        return f"{row.point_id}（{row.name}）" if row.name else row.point_id

    # ---- 編輯 ----

    def _can_edit(self) -> bool:
        return not (self._status is not None and self._status.locked)

    def _edit_row(self, i: int | None) -> None:
        if i is None or not self._can_edit():
            return
        row = self._positions.rows()[i]
        dialog = PointEditDialog(self._config, row, self)
        try:
            if dialog.exec() == PointEditDialog.DialogCode.Accepted and self._can_edit():
                self._store(i, **dialog.values())
        finally:
            dialog.deleteLater()

    def _store(self, i: int, **fields) -> None:
        point_id = self._positions.rows()[i].point_id
        try:
            self._positions.update(point_id, **fields)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "點位存檔失敗", f"{point_id} 無法存檔：{e}")
        self._refresh_row(i)

    def _refresh_row(self, i: int) -> None:
        self._fill_row(i, self._positions.rows()[i])
        self._update_controls()

    def _clear_point(self) -> None:
        i = self._selected_row()
        if i is None or not self._can_edit():
            return
        row = self._positions.rows()[i]
        rows = tuple(
            ConfirmRow(
                self._config.axes[n].name,
                self._format(n, getattr(row, f)),
                self._format(n, 0.0),
                self._unit(n),
            )
            for n, f in self._fields.items()
        )
        if not confirm_dialog.confirm(
            self,
            f"確認清除 {self._label(row)}",
            lines=("名稱、七軸座標與說明會清空，並立即寫回 positions.csv。",),
            rows=rows,
            rows_head="清除後",
            note="清除後無法復原。",
            ok_text="✕ 確認清除",
            danger=True,
        ):
            return
        try:
            self._positions.clear(row.point_id)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "點位存檔失敗", f"{row.point_id} 無法存檔：{e}")
        self._refresh_row(i)

    # ---- 命令 ----

    def _save_current(self) -> None:
        i = self._selected_row()
        if i is None or self._snapshot is None:
            return
        row = self._positions.rows()[i]
        current = {n: self._snapshot.axis(n).position for n in self._fields}
        rows = tuple(
            ConfirmRow(
                self._config.axes[n].name,
                self._format(n, getattr(row, f)),
                self._format(n, current[n]),
                self._unit(n),
            )
            for n, f in self._fields.items()
        )
        if not confirm_dialog.confirm(
            self,
            f"確認存入 {self._label(row)}",
            lines=("用七軸目前位置覆蓋此點，並立即寫回 positions.csv。",),
            rows=rows,
            rows_head="存入後",
            ok_text="確認存入",
        ):
            return
        # 確認期間可能已鎖定或斷線：再檢查一次，存入的是確認視窗上顯示的值
        if lock_tooltip(self._snapshot, self._status) is not None:
            return
        self._store(i, **{f: current[n] for n, f in self._fields.items()})

    def _move_to_point(self) -> None:
        row = self._selected_point()
        if row is None or self._snapshot is None:
            return
        targets = {
            n: getattr(row, self._fields[n])
            for n, box in self.axis_checks.items()
            if box.isChecked()
        }
        if not targets:
            return
        speed = self.speed.value()
        rows = tuple(
            ConfirmRow(
                self._config.axes[n].name,
                self._format(n, self._snapshot.axis(n).position),
                self._format(n, t),
                self._unit(n),
            )
            for n, t in targets.items()
        )
        if not confirm_dialog.confirm(
            self,
            f"確認移動到 {self._label(row)}",
            lines=(f"移動 {len(targets)} 軸，同時出發　速度：{speed} %",),
            rows=rows,
            ok_text="▶ 確認移動",
        ):
            return
        if lock_tooltip(self._snapshot, self._status) is None:
            self._worker.move(targets, False, speed)

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
        locked = not self._can_edit()
        tip = lock_tooltip(self._snapshot, self._status)
        self.lock_label.setText(tip.replace("\n", " ") if tip else "")
        row = self._selected_point()
        self.selected_label.setText(
            "未選取點位" if row is None else f"{row.point_id}　{row.name}".rstrip()
        )
        no_point = None if row is not None else "請先選取點位"
        edit_reason = (tip if locked else None) or no_point
        for button in (self.edit_button, self.clear_button):
            button.setEnabled(edit_reason is None)
            button.setToolTip(edit_reason or "")
        save_reason = tip or no_point
        self.save_button.setEnabled(save_reason is None)
        self.save_button.setToolTip(save_reason or "")
        move_reason = tip or no_point
        if move_reason is None and not any(b.isChecked() for b in self.axis_checks.values()):
            move_reason = "請先選擇要移動的軸"
        self.move_button.setEnabled(move_reason is None)
        self.move_button.setToolTip(move_reason or "")
        self.import_button.setEnabled(not locked)
        self.import_button.setToolTip(tip if locked else "")
