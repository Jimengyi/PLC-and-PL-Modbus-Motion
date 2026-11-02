"""PointsPanel 點位面板與 PointEditDialog（spec FR-34 ~ FR-36）。

以 positions.csv 內容與假 worker 呼叫為斷言；確認視窗由 `confirms` fixture 自動回答並記錄內容，
編輯視窗以 `answer_edit_dialog` 在出現時填值並按下確認或取消。
"""

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from ui_support import answer_next_message_box, status

from plc_motion.settings.positions import PositionTable, load_positions
from plc_motion.ui.axis_page import AxisPage
from plc_motion.ui.point_edit_dialog import PointEditDialog
from plc_motion.ui.points_panel import PointsPanel

NAME_COL, X_COL, Y_COL, DESC_COL = 1, 2, 3, 9
ALL_HOMED = {f"Find Home Finish {n}": True for n in range(1, 8)}


@pytest.fixture
def panel(qtbot, config, fake_worker):
    p = PointsPanel(config, fake_worker)
    qtbot.addWidget(p)
    p.resize(900, 500)
    p.show()
    qtbot.waitExposed(p)
    return p


def saved(config):
    table, errors = load_positions(config.positions.path)
    assert errors == []
    return {r.point_id: r for r in table.rows()}


def enable(fake_worker, make_snapshot, values=None, locked=False):
    fake_worker.snapshot_ready.emit(make_snapshot({**ALL_HOMED, **(values or {})}), status(locked))


def answer_edit_dialog(fill, accept: bool = True) -> None:
    """下一個 PointEditDialog 出現時呼叫 `fill(dialog)`，再按確認存檔（或取消）。"""

    def poll():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, PointEditDialog):
            fill(dialog)
            (dialog.save_button if accept else dialog.cancel_button).click()
        else:
            QTimer.singleShot(20, poll)

    QTimer.singleShot(0, poll)


def test_table_has_200_rows_and_hides_description(panel):
    assert panel.table.rowCount() == 200
    assert panel.table.item(0, 0).text() == "P001"
    assert panel.table.item(199, 0).text() == "P200"
    assert panel.table.isColumnHidden(DESC_COL)


def test_table_cells_are_not_editable_in_place(panel, fake_worker, make_snapshot):
    """點位只能在編輯視窗修改：格子雙擊不會就地編輯。"""
    enable(fake_worker, make_snapshot)
    assert panel.table.editTriggers() == panel.table.EditTrigger.NoEditTriggers
    assert not (panel.table.item(0, X_COL).flags() & Qt.ItemFlag.ItemIsEditable)


def test_edit_dialog_saves_name_coordinates_and_description(
    panel, config, fake_worker, make_snapshot
):
    enable(fake_worker, make_snapshot)
    panel.table.selectRow(2)

    def fill(d):
        d.name_edit.setText("取料位")
        d.coord_edits[1].setText("12.5")
        d.coord_edits[4].setText("-3")
        d.description_edit.setText("第一站")

    answer_edit_dialog(fill)
    panel.edit_button.click()
    row = saved(config)["P003"]
    assert (row.name, row.x, row.rx, row.description) == ("取料位", 12.5, -3.0, "第一站")
    assert panel.table.item(2, NAME_COL).text() == "取料位"
    assert panel.table.item(2, X_COL).text() == "12.5000"


def test_double_click_row_opens_edit_dialog(panel, qtbot, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    answer_edit_dialog(lambda d: d.coord_edits[2].setText("7"))
    rect = panel.table.visualItemRect(panel.table.item(1, X_COL))
    qtbot.mouseClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qtbot.mouseDClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert saved(config)["P002"].y == 7.0


def test_edit_dialog_cancel_keeps_file(panel, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    before = config.positions.path.read_bytes()
    panel.table.selectRow(0)
    answer_edit_dialog(lambda d: d.coord_edits[1].setText("5"), accept=False)
    panel.edit_button.click()
    assert config.positions.path.read_bytes() == before


def test_edit_dialog_validation(qtbot, config):
    config.positions.update("P004", x=1.23456, name="甲")
    row = config.positions.rows()[3]
    d = PointEditDialog(config, row)
    qtbot.addWidget(d)
    assert not d.save_button.isEnabled()  # 沒改不能存
    assert d.summary_label.text() == "尚未修改"
    d.coord_edits[2].setText("100.5")  # 超出軟體極限 ±100
    assert not d.save_button.isEnabled()
    assert d.coord_edits[2].property("state") == "fault"
    assert "超出範圍" in d.coord_hints[2].text()
    d.coord_edits[2].setText("abc")
    assert d.coord_hints[2].text() == "不是數字"
    assert not d.save_button.isEnabled()
    d.name_edit.setText("乙")  # 有別的有效修改，但仍有一格錯：不能存
    assert not d.save_button.isEnabled()
    d.name_edit.setText("甲")
    d.coord_edits[2].setText("-100")
    assert d.save_button.isEnabled()
    assert d.coord_edits[2].property("state") == "warn"
    assert d.coord_hints[2].text() == "原值 0.0000"
    assert d.summary_label.text() == "已修改 1 格"
    values = d.values()
    assert values["y"] == -100.0
    assert values["x"] == 1.23456  # 沒改的欄位保留原本較精細的值，不因顯示四捨五入
    assert values["name"] == "甲"


def test_clear_point_confirms_then_clears(panel, config, fake_worker, make_snapshot, confirms):
    enable(fake_worker, make_snapshot)
    config.positions.update("P003", name="取料位", x=12.5, rz=15.0, description="說明")
    panel.reload()
    panel.table.selectRow(2)
    panel.clear_button.click()
    row = saved(config)["P003"]
    assert (row.name, row.x, row.rz, row.description) == ("", 0.0, 0.0, "")
    assert panel.table.item(2, NAME_COL).text() == ""
    shown = confirms.last
    assert shown["title"] == "確認清除 P003（取料位）"
    assert shown["danger"] is True
    assert (shown["rows"][0].current, shown["rows"][0].target) == ("12.5000", "0.0000")


def test_clear_point_cancel_keeps_point(panel, config, fake_worker, make_snapshot, confirms):
    enable(fake_worker, make_snapshot)
    config.positions.update("P001", x=3.0)
    panel.reload()
    panel.table.selectRow(0)
    confirms.answer = False
    panel.clear_button.click()
    assert saved(config)["P001"].x == 3.0


def test_save_current_position_writes_all_seven_axes(
    panel, config, fake_worker, make_snapshot, confirms
):
    # 倍率：X/Y/Z 10000、RX/RY/RZ 100、升降 100（raw / scale = 實際值）
    raw = {
        "Current Position 1": 15000,
        "Current Position 2": 20000,
        "Current Position 3": 25000,
        "Current Position 4": 3000,
        "Current Position 5": 4000,
        "Current Position 6": 5000,
        "Current Position 7": 6000,
    }
    enable(fake_worker, make_snapshot, raw)
    panel.table.selectRow(4)
    panel.save_button.click()
    assert confirms.last["title"] == "確認存入 P005"
    assert [(r.axis, r.current, r.target) for r in confirms.last["rows"][:2]] == [
        ("X", "0.0000", "1.5000"),
        ("Y", "0.0000", "2.0000"),
    ]
    row = saved(config)["P005"]
    assert (row.x, row.y, row.z, row.rx, row.ry, row.rz, row.lift) == (
        1.5,
        2.0,
        2.5,
        30.0,
        40.0,
        50.0,
        60.0,
    )
    assert float(panel.table.item(4, X_COL).text()) == 1.5


def test_save_current_cancel_keeps_point(panel, config, fake_worker, make_snapshot, confirms):
    enable(fake_worker, make_snapshot, {"Current Position 1": 15000})
    panel.table.selectRow(0)
    confirms.answer = False
    panel.save_button.click()
    assert saved(config)["P001"].x == 0.0


def test_move_sends_only_checked_axes_absolute(panel, config, fake_worker, make_snapshot, confirms):
    enable(fake_worker, make_snapshot)
    table = config.positions
    table.update("P002", x=1.5, y=2.5, z=3.5, lift=7.0)
    panel.reload()
    panel.table.selectRow(1)
    for n, check in panel.axis_checks.items():
        check.setChecked(n in (1, 7))
    panel.speed.setValue(35)
    panel.move_button.click()
    assert [c for c in fake_worker.calls if c[0] == "move"] == [
        ("move", {1: 1.5, 7: 7.0}, False, 35)
    ]
    assert confirms.last["title"] == "確認移動到 P002"
    assert [(r.axis, r.target) for r in confirms.last["rows"]] == [
        ("X", "1.5000"),
        ("升降", "7.00"),
    ]
    assert "35 %" in confirms.last["lines"][0]


def test_move_cancel_sends_nothing(panel, fake_worker, make_snapshot, confirms):
    enable(fake_worker, make_snapshot)
    panel.table.selectRow(0)
    confirms.answer = False
    panel.move_button.click()
    assert [c for c in fake_worker.calls if c[0] == "move"] == []


def test_move_needs_checked_axis(panel, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    panel.table.selectRow(0)
    for check in panel.axis_checks.values():
        check.setChecked(False)
    assert not panel.move_button.isEnabled()


def test_out_of_soft_limit_cell_is_abnormal_color(panel, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    config.positions.update("P001", x=100.0, y=100.5)  # 上限 100：X 在範圍內、Y 超過
    panel.reload()
    normal = panel.table.item(0, X_COL).background()
    abnormal = panel.table.item(0, Y_COL).background()
    assert abnormal.color() != normal.color()
    assert abnormal.style() == Qt.BrushStyle.SolidPattern
    assert "超出軟體極限" in panel.table.item(0, Y_COL).toolTip()
    config.positions.update("P001", y=-100.0)
    panel.reload()
    assert panel.table.item(0, Y_COL).background().color() == normal.color()


def test_import_bad_file_shows_reason_and_keeps_table(
    panel, config, fake_worker, make_snapshot, tmp_path, monkeypatch
):
    enable(fake_worker, make_snapshot)
    before = config.positions.path.read_bytes()
    bad = tmp_path / "bad.csv"
    bad.write_text("#Point,Name\nP001,a\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(bad), ""))
    seen: list = []
    answer_next_message_box(QMessageBox.StandardButton.Ok, seen)
    panel.import_button.click()
    assert seen and "缺少欄位" in seen[0]
    assert config.positions.path.read_bytes() == before


def _good_file(config, tmp_path):
    other = PositionTable(tmp_path / "other.csv", config.positions.rows())
    other.update("P001", name="匯入點", x=9.0)
    other.export_file(tmp_path / "good.csv")
    return tmp_path / "good.csv"


def test_import_good_file_replaces_after_confirm(
    panel, config, fake_worker, make_snapshot, tmp_path, monkeypatch
):
    enable(fake_worker, make_snapshot)
    good = _good_file(config, tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(good), ""))
    answer_next_message_box(QMessageBox.StandardButton.Yes)
    panel.import_button.click()
    assert saved(config)["P001"].name == "匯入點"
    assert panel.table.item(0, NAME_COL).text() == "匯入點"
    assert float(panel.table.item(0, X_COL).text()) == 9.0


def test_import_cancel_keeps_table(
    panel, config, fake_worker, make_snapshot, tmp_path, monkeypatch
):
    enable(fake_worker, make_snapshot)
    good = _good_file(config, tmp_path)
    before = config.positions.path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(good), ""))
    answer_next_message_box(QMessageBox.StandardButton.No)
    panel.import_button.click()
    assert config.positions.path.read_bytes() == before
    assert panel.table.item(0, NAME_COL).text() == ""


def test_export_writes_csv(panel, config, fake_worker, make_snapshot, tmp_path, monkeypatch):
    enable(fake_worker, make_snapshot)
    config.positions.update("P001", name="甲")
    out = tmp_path / "out.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    panel.export_button.click()
    assert out.read_bytes() == config.positions.path.read_bytes()


def test_locked_disables_save_edit_clear_import_move(panel, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot, locked=True)
    panel.table.selectRow(0)
    for button in (
        panel.save_button,
        panel.edit_button,
        panel.clear_button,
        panel.import_button,
        panel.move_button,
    ):
        assert not button.isEnabled()
    assert panel.export_button.isEnabled()
    enable(fake_worker, make_snapshot, locked=False)
    for button in (panel.save_button, panel.edit_button, panel.clear_button, panel.import_button):
        assert button.isEnabled()


def test_lock_toggle_does_not_store_points(panel, config, fake_worker, make_snapshot, monkeypatch):
    calls = []
    monkeypatch.setattr(PositionTable, "update", lambda self, *a, **k: calls.append(a))
    path = config.positions.path
    before = path.read_bytes() if path.exists() else None
    enable(fake_worker, make_snapshot, locked=True)
    enable(fake_worker, make_snapshot, locked=False)
    assert calls == []
    assert (path.read_bytes() if path.exists() else None) == before


def test_no_snapshot_disables_plc_writes_only(panel):
    panel.table.selectRow(0)
    assert not panel.save_button.isEnabled()
    assert not panel.move_button.isEnabled()
    assert panel.import_button.isEnabled()
    assert panel.edit_button.isEnabled() and panel.clear_button.isEnabled()  # 不碰 PLC


def test_axis_page_embeds_panel(qtbot, config, fake_worker):
    page = AxisPage(config, fake_worker)
    qtbot.addWidget(page)
    assert isinstance(page.points_panel, PointsPanel)
