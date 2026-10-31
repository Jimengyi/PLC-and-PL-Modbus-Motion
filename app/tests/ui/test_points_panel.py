"""PointsPanel 點位表面板（spec FR-34 ~ FR-36）：以 positions.csv 內容與假 worker 呼叫為斷言。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox
from ui_support import answer_next_message_box, status

from plc_motion.settings.positions import PositionTable, load_positions
from plc_motion.ui.axis_page import AxisPage
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


def test_table_has_200_rows(panel):
    assert panel.table.rowCount() == 200
    assert panel.table.item(0, 0).text() == "P001"
    assert panel.table.item(199, 0).text() == "P200"


def test_edit_name_and_description_saved_immediately(panel, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    panel.table.item(2, NAME_COL).setText("取料位")
    panel.table.item(2, DESC_COL).setText("第一站")
    row = saved(config)["P003"]
    assert (row.name, row.description) == ("取料位", "第一站")


def test_edit_coordinate_saved_immediately(panel, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    panel.table.item(0, X_COL).setText("12.5")
    assert saved(config)["P001"].x == 12.5


def test_invalid_coordinate_is_reverted_and_not_saved(panel, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    panel.table.item(0, X_COL).setText("12.5")
    panel.table.item(0, X_COL).setText("abc")
    assert saved(config)["P001"].x == 12.5
    assert float(panel.table.item(0, X_COL).text()) == 12.5


def test_save_current_position_writes_all_seven_axes(panel, config, fake_worker, make_snapshot):
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


def test_move_sends_only_checked_axes_absolute(panel, config, fake_worker, make_snapshot):
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


def test_move_needs_checked_axis(panel, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    panel.table.selectRow(0)
    for check in panel.axis_checks.values():
        check.setChecked(False)
    assert not panel.move_button.isEnabled()


def test_out_of_soft_limit_cell_is_abnormal_color(panel, config, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot)
    panel.table.item(0, X_COL).setText("100")  # 上限 100 內
    panel.table.item(0, Y_COL).setText("100.5")  # 超過
    normal = panel.table.item(0, X_COL).background()
    abnormal = panel.table.item(0, Y_COL).background()
    assert abnormal.color() != normal.color()
    assert abnormal.style() == Qt.BrushStyle.SolidPattern
    panel.table.item(0, Y_COL).setText("-100")
    assert panel.table.item(0, Y_COL).background().color() == normal.color()


def test_theme_switch_remarks_out_of_limit_cell(qtbot, panel, fake_worker, make_snapshot):
    from PySide6.QtWidgets import QApplication

    from plc_motion.ui.theme import apply_theme, color

    enable(fake_worker, make_snapshot)
    app = QApplication.instance()
    try:
        apply_theme(app, "dark")
        panel.table.item(0, Y_COL).setText("100.5")
        assert panel.table.item(0, Y_COL).background().color() == color("check_ng", "dark")
        apply_theme(app, "light")
        assert panel.table.item(0, Y_COL).background().color() == color("check_ng", "light")
    finally:
        apply_theme(app, "dark")


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
    panel.table.item(0, NAME_COL).setText("甲")
    out = tmp_path / "out.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    panel.export_button.click()
    assert out.read_bytes() == config.positions.path.read_bytes()


def test_locked_disables_save_edit_import_move(panel, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot, locked=True)
    panel.table.selectRow(0)
    assert not panel.save_button.isEnabled()
    assert not panel.import_button.isEnabled()
    assert not panel.move_button.isEnabled()
    assert not (panel.table.item(0, NAME_COL).flags() & Qt.ItemFlag.ItemIsEditable)
    assert panel.export_button.isEnabled()
    enable(fake_worker, make_snapshot, locked=False)
    assert panel.save_button.isEnabled()
    assert panel.import_button.isEnabled()
    assert panel.table.item(0, NAME_COL).flags() & Qt.ItemFlag.ItemIsEditable


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


def test_axis_page_embeds_panel(qtbot, config, fake_worker):
    page = AxisPage(config, fake_worker)
    qtbot.addWidget(page)
    assert isinstance(page.points_panel, PointsPanel)
