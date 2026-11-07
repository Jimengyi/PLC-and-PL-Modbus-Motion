"""端到端：設定檔編碼與新增點、點位表、IO Monitor（spec AC-3 ~ AC-5、AC-25 ~ AC-29、AC-38）。

需要變更設定檔的 AC 先改 tmp 的 config 複本（`e2e_config_dir`），再以
`request.getfixturevalue("e2e")` 載入設定並啟動；repo 的 `app/config` 不動。
模擬器與 plc-motion 讀同一份設定資料夾。
"""

import csv
import io
from pathlib import Path

import pytest
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox

from lib.modbus_tags import Access
from plc_motion.engine import ResultKind
from plc_motion.engine.engine import REASON_ESTOP
from plc_motion.settings.csvio import BOM
from plc_motion.settings.io_points import CheckStatus
from plc_motion.ui.io_monitor import NAME_COL, STRIPE_COL
from plc_motion.ui.theme import color
from plc_motion.ui.value_dialog import ValueDialog

pytestmark = pytest.mark.e2e

AXES = range(1, 8)
Yes, No, Ok = (
    QMessageBox.StandardButton.Yes,
    QMessageBox.StandardButton.No,
    QMessageBox.StandardButton.Ok,
)


# ---- 檔案 helper ----


def read_csv(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))


def write_csv(path: Path, records: list[list[str]], encoding: str = "utf-8") -> None:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(records)
    data = buf.getvalue().encode(encoding)
    path.write_bytes(BOM + data if encoding == "utf-8" else data)


def csv_row(records: list[list[str]], key: str) -> list[str]:
    return next(r for r in records if r and r[0] == key)


def started(e2e) -> None:
    e2e.prepare()
    e2e.start()
    e2e.wait_unlocked()


def click_button(box: QMessageBox, button) -> None:
    box.button(button).click()


def answer_box(e2e, button, timeout_ms: int = 10000) -> list[QMessageBox]:
    """之後出現的 QMessageBox 記下並按 `button`；回傳的清單元素為 (標題, 內文)。"""
    texts: list[tuple[str, str]] = []

    def handle(box: QMessageBox) -> None:
        texts.append((box.windowTitle(), box.text()))
        click_button(box, button)

    e2e.answer_modal(QMessageBox, handle, timeout_ms)
    return texts


def right_click_set_status(e2e, tag: str, status: CheckStatus) -> None:
    """在該點的列上按右鍵（OS 送出的 context menu 事件），從選單選 `status`。"""
    table, row = e2e.show_io(tag)
    chosen: list[str] = []

    def handle(menu: QMenu) -> None:
        action = next(a for a in menu.actions() if a.text() == status.value)
        chosen.append(action.text())
        action.trigger()
        menu.close()

    e2e.answer_modal(QMenu, handle, popup=True)
    pos = table.visualItemRect(table.item(row, NAME_COL)).center()
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, pos, table.viewport().mapToGlobal(pos)
    )
    QApplication.sendEvent(table.viewport(), event)
    assert chosen == [status.value]


# ---- 設定檔（AC-3 ~ AC-5） ----


def test_ac3_big5_io_points_shows_chinese_and_check_status_writes_utf8_bom(request, e2e_config_dir):
    path = e2e_config_dir / "modbus" / "Channel01" / "io_points.csv"
    original = path.read_bytes().decode("utf-8-sig")
    path.write_bytes(original.encode("cp950"))  # Big5（含中文 Display Name）
    with pytest.raises(UnicodeDecodeError):
        path.read_bytes().decode("utf-8")

    e2e = request.getfixturevalue("e2e")
    started(e2e)
    page = e2e.app.io_page
    table, row = e2e.show_io("Servo Motor 1")
    assert table.item(row, NAME_COL).text() == "X 軸伺服啟動"
    table, row = e2e.show_io("Emergency Stop")
    assert table.item(row, NAME_COL).text() == "緊急停止(B)"

    right_click_set_status(e2e, "Servo Motor 1", CheckStatus.OK)
    table, row = page.locate("Servo Motor 1")
    assert table.item(row, STRIPE_COL).background().color() == color("ok")

    data = path.read_bytes()
    assert data.startswith(BOM)  # UTF-8 含 BOM：Excel 預設開啟即以 UTF-8 解讀
    text = data[len(BOM) :].decode("utf-8")
    expected = original.replace(
        "Servo Motor 1,X 軸伺服啟動,122,1,0,Coil,Bool,Read Write,O,_M122,,Unchecked",
        "Servo Motor 1,X 軸伺服啟動,122,1,0,Coil,Bool,Read Write,O,_M122,,OK",
    )
    assert expected != original
    assert text == expected  # 中文與其餘列逐字不變，只改該列 Check Status


def test_ac4_missing_positions_csv_is_created_with_200_points(request, e2e_config_dir):
    path = e2e_config_dir / "positions.csv"
    path.unlink()
    e2e = request.getfixturevalue("e2e")
    e2e.start()
    e2e.wait_connected()

    records = read_csv(path)
    assert records[0] == ["#Point", "Name", "X", "Y", "Z", "RX", "RY", "RZ", "Lift", "Description"]
    assert [r[0] for r in records[1:]] == [f"P{i:03d}" for i in range(1, 201)]
    assert all(r[2:9] == ["0"] * 7 for r in records[1:])
    table = e2e.app.axis_page.points_panel.table
    assert table.rowCount() == 200
    assert table.item(199, 0).text() == "P200"


def test_ac5_extra_tag_appears_in_io_monitor_and_writes_sim(request, e2e_config_dir):
    path = e2e_config_dir / "modbus" / "Channel01" / "io_points.csv"
    records = read_csv(path)
    records.append(
        [
            "Cylinder1 Up",
            "氣缸 1 上升",
            "300",
            "1",
            "0",
            "Coil",
            "Bool",
            "Read Write",
            "O",
            "_M300",
            "",
            "Unchecked",
        ]
    )
    write_csv(path, records)
    # 新增的點也要加進讀取範圍（FR-61），否則啟動檢查不通過
    ranges = e2e_config_dir / "modbus" / "Channel01" / "read_ranges.toml"
    text = ranges.read_text(encoding="utf-8")
    ranges.write_text(text.replace('"20~84, 120~184"', '"20~84, 120~184, 300"'), encoding="utf-8")

    e2e = request.getfixturevalue("e2e")  # 模擬器與 plc-motion 都讀這份檔
    assert e2e.sim.points.get("Cylinder1 Up").start_address == 300
    started(e2e)
    page = e2e.app.io_page
    table, row = e2e.show_io("Cylinder1 Up")
    assert table is page.tables[("digital", Access.READ_WRITE)]  # 數位分頁的右欄
    assert table.item(row, NAME_COL).text() == "氣缸 1 上升"
    assert table.isVisible()

    since = e2e.mark()
    assert e2e.model("Cylinder1 Up") is False or e2e.model("Cylinder1 Up") == 0
    e2e.click_cell(table, row, NAME_COL, double=True)
    e2e.wait_entry("write", "Cylinder1 Up", True, since)
    assert e2e.model("Cylinder1 Up") in (True, 1)


# ---- 點位表（AC-25、AC-26、AC-38） ----

P004 = {1: 12.3456, 2: -7.5, 3: 3.25, 4: 4.25, 5: -1.5, 6: 2.75, 7: 55.55}
P004_TEXT = ["12.3456", "-7.5", "3.25", "4.25", "-1.5", "2.75", "55.55"]
SCALES = {1: 10000, 2: 10000, 3: 10000, 4: 100, 5: 100, 6: 100, 7: 100}


def test_ac25_save_current_position_then_move_checked_axes_to_point(e2e):
    e2e.prepare(positions=P004)
    e2e.start()
    e2e.wait_unlocked()
    app = e2e.app
    e2e.until(lambda: all(app.snapshot.axis(n).position == v for n, v in P004.items()))
    panel = app.axis_page.points_panel
    path = e2e.config.positions.path

    e2e.click_cell(panel.table, 3, 0)
    assert panel.selected_label.text().startswith("P004")
    e2e.click(panel.save_button)
    assert csv_row(read_csv(path), "P004")[2:9] == P004_TEXT  # 立即存檔
    assert panel.table.item(3, 2).text() == "12.3456"

    with e2e.sim.lock:
        for n in AXES:
            e2e.sim.model.set_position(n, 1.0)
    e2e.until(lambda: all(app.snapshot.axis(n).position == 1.0 for n in AXES))
    for n in (4, 5, 6, 7):
        e2e.click(panel.axis_checks[n])
    assert [n for n, box in panel.axis_checks.items() if box.isChecked()] == [1, 2, 3]

    ev, since = len(e2e.events), e2e.mark()
    e2e.click(panel.move_button)
    finished = e2e.wait_finished(ev, 30000)
    assert {n: r.kind for n, r in finished.results.items()} == {
        1: ResultKind.DONE,
        2: ResultKind.DONE,
        3: ResultKind.DONE,
    }
    written_axes = {int(tag.rsplit(" ", 1)[1]) for tag, _ in e2e.writes(since)}
    assert written_axes == {1, 2, 3}  # 只有 X、Y、Z
    for n in (1, 2, 3):
        raw = round(P004[n] * SCALES[n])
        assert (f"Target Position {n}", raw) in e2e.writes(since)
        assert e2e.model(f"Current Position {n}") == raw
    for n in (4, 5, 6, 7):
        assert e2e.model(f"Current Position {n}") == SCALES[n]  # 仍在 1.0


def export_points(e2e, monkeypatch, target: Path) -> None:
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    e2e.click(e2e.app.axis_page.points_panel.export_button)
    assert target.is_file()


def import_points(e2e, monkeypatch, source: Path, button) -> list[tuple[str, str]]:
    """按匯入，選 source；之後跳出的 QMessageBox 按 `button`。回傳跳出的視窗內容。"""
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(source), ""))
    )
    texts = answer_box(e2e, button, timeout_ms=3000)
    e2e.click(e2e.app.axis_page.points_panel.import_button)
    e2e.settle(100)
    return texts


def table_snapshot(panel) -> list[list[str]]:
    t = panel.table
    return [[t.item(r, c).text() for c in range(t.columnCount())] for r in range(t.rowCount())]


def test_ac26_export_edit_import_confirm_replaces_cancel_keeps(e2e, monkeypatch, tmp_path):
    started(e2e)
    panel = e2e.app.axis_page.points_panel
    path = e2e.config.positions.path
    exported = tmp_path / "exported.csv"
    export_points(e2e, monkeypatch, exported)
    records = read_csv(exported)
    assert len(records) == 201

    records[10][1] = "取放點"  # P010 Name
    records[10][2] = "12.5"  # P010 X
    records[200][9] = "最後一點"  # P200 Description
    write_csv(exported, records)

    before_file, before_table = path.read_bytes(), table_snapshot(panel)
    texts = import_points(e2e, monkeypatch, exported, No)  # 取消確認
    assert texts and texts[0][0] == "匯入點位表" and "exported.csv" in texts[0][1]
    assert path.read_bytes() == before_file
    assert table_snapshot(panel) == before_table

    texts = import_points(e2e, monkeypatch, exported, Yes)
    assert [t[0] for t in texts] == ["匯入點位表"]
    assert read_csv(path) == records  # 整份換成匯入內容並存檔
    assert panel.table.item(9, 1).text() == "取放點"
    assert panel.table.item(9, 2).text() == "12.5000"
    assert panel.table.item(199, 9).text() == "最後一點"


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        ("150 列", ["200 列", "150 列"]),
        ("座標為文字", ["第 6 列", "Y 不是數字", "abc"]),
    ],
)
def test_ac38_invalid_import_shows_reason_and_changes_nothing(
    e2e, monkeypatch, tmp_path, broken, reason
):
    started(e2e)
    panel = e2e.app.axis_page.points_panel
    path = e2e.config.positions.path
    source = tmp_path / "bad.csv"
    export_points(e2e, monkeypatch, source)
    records = read_csv(source)
    if broken == "150 列":
        records = records[:151]
    else:
        records[5][3] = "abc"  # P005 的 Y
    write_csv(source, records)

    before_file, before_table = path.read_bytes(), table_snapshot(panel)
    texts = import_points(e2e, monkeypatch, source, Ok)
    assert [t[0] for t in texts] == ["匯入失敗"]  # 沒有跳確認，直接顯示原因
    assert all(r in texts[0][1] for r in reason), texts[0][1]
    assert path.read_bytes() == before_file
    assert table_snapshot(panel) == before_table


# ---- IO Monitor（AC-27 ~ AC-29） ----


def shown_tags(page) -> list[str]:
    return [
        page.tag_at(table, row) for table in page.tables.values() for row in range(table.rowCount())
    ]


def test_ac27_hidden_points_absent_and_b_contacts_marked(e2e):
    started(e2e)
    page = e2e.app.io_page
    tags = shown_tags(page)
    hidden = {f"{name} {n}" for n in AXES for name in ("JOG Plus", "JOG Minus", "Request")}
    assert hidden.isdisjoint(tags)
    assert len(tags) == len(e2e.config.points.all()) - len(hidden)

    b_tags = {f"{name} {n}" for n in AXES for name in ("Positive Limit", "Negative Limit")}
    b_tags.add("Emergency Stop")
    for tag in tags:
        table, row = page.locate(tag)
        name = table.item(row, NAME_COL).text()
        assert name.endswith("(B)") == (tag in b_tags), (tag, name)
    table, row = page.locate("Positive Limit 1")
    assert table.item(row, NAME_COL).text() == "X 軸正極限(B)"


def test_ac28_speed_dialog_range_servo_toggle_and_no_write_while_moving(e2e):
    started(e2e)
    assert e2e.config.points.get("Speed 1").start_address == 123  # D123
    table, row = e2e.show_io("Speed 1")
    since = e2e.mark()
    rejected: list[str] = []

    def enter(dialog: ValueDialog) -> None:
        dialog.value_edit.setText("150")
        dialog.write_button.click()
        rejected.append(dialog.error_label.text() if dialog.isVisible() else "（視窗已關閉）")
        dialog.value_edit.setText("70")
        dialog.write_button.click()

    seen = e2e.answer_modal(ValueDialog, enter)
    e2e.click_cell(table, row, NAME_COL, double=True)
    assert len(seen) == 1
    assert rejected == ["超出範圍 1 ~ 100"]
    e2e.wait_entry("write", "Speed 1", 70, since)
    assert e2e.model("Speed 1") == 70
    assert e2e.writes(since) == [("Speed 1", 70)]  # 150 沒有寫入

    table, row = e2e.show_io("Servo Motor 1")
    assert e2e.model("Servo Motor 1") in (True, 1)
    since = e2e.mark()
    e2e.click_cell(table, row, NAME_COL, double=True)
    e2e.wait_entry("write", "Servo Motor 1", False, since)
    assert e2e.model("Servo Motor 1") in (False, 0)

    # 移動中：雙擊不產生寫入、也不開輸入視窗
    e2e.prepare()
    e2e.wait_unlocked()
    ev = len(e2e.events)
    e2e.move({2: 20.0}, speed=10)
    e2e.until(lambda: e2e.app.snapshot.axis(2).busy)
    since = e2e.mark()
    seen = e2e.answer_modal(ValueDialog, lambda d: d.reject(), timeout_ms=1500)
    table, row = e2e.show_io("Speed 1")
    e2e.click_cell(table, row, NAME_COL, double=True)
    table, row = e2e.show_io("Servo Motor 1")
    e2e.click_cell(table, row, NAME_COL, double=True)
    e2e.settle(1600)
    assert seen == []
    assert e2e.app.snapshot.axis(2).busy  # 確實在移動中雙擊
    assert [w for w in e2e.writes(since) if w[0] in ("Speed 1", "Servo Motor 1")] == []
    e2e.wait_finished(ev, 30000)


def test_ac29_set_ng_by_context_menu_saves_and_survives_restart(e2e):
    started(e2e)
    page = e2e.app.io_page
    path = e2e.config.points.path
    before = read_csv(path)
    unchecked = int(page.count_labels[CheckStatus.UNCHECKED].text().split()[-1])
    assert page.count_labels[CheckStatus.NG].text() == "NG 0"

    right_click_set_status(e2e, "Ready 1", CheckStatus.NG)
    table, row = page.locate("Ready 1")
    assert table.item(row, STRIPE_COL).background().color() == color("fault")
    assert page.count_labels[CheckStatus.NG].text() == "NG 1"
    assert page.count_labels[CheckStatus.UNCHECKED].text() == f"Unchecked {unchecked - 1}"

    after = read_csv(path)  # 立即存檔
    assert csv_row(after, "Ready 1")[11] == "NG"
    assert [r for r in after if r[0] != "Ready 1"] == [r for r in before if r[0] != "Ready 1"]

    app = e2e.restart()
    e2e.wait_connected()
    table, row = app.io_page.locate("Ready 1")
    assert table.item(row, STRIPE_COL).background().color() == color("fault")
    assert app.io_page.count_labels[CheckStatus.NG].text() == "NG 1"


# ---- 接點型式（migration spec AC-10、AC-27 ~ AC-29） ----

ATTR_COL = 10


def set_attributes(config_dir: Path, tag: str, attributes: str) -> None:
    path = config_dir / "modbus" / "Channel01" / "io_points.csv"
    records = read_csv(path)
    csv_row(records, tag)[ATTR_COL] = attributes
    write_csv(path, records)


def io_lamp_on(e2e, tag: str) -> bool:
    table, row = e2e.app.io_page.locate(tag)
    return table.item(row, 3).text().endswith("ON")


def jog_reasons(e2e, axis: int, direction: int) -> tuple[str, ...]:
    """直接對 worker 下 JOG（略過畫面反灰），回傳拒絕理由；沒被拒時放開並回傳空 tuple。"""
    ev = len(e2e.events)
    e2e.app.worker.jog_press(axis, direction, 10)
    e2e.settle(300)
    rejected = e2e.rejected(ev)
    if not rejected:
        e2e.app.worker.jog_release(axis, direction)
        e2e.settle(300)
        return ()
    return rejected[0].reasons


def test_migration_ac27_b_contact_raw_one_lit_in_io_and_untriggered_on_axis_page(e2e):
    started(e2e)
    app = e2e.app
    page = app.axis_page
    lamp = page.selected_lamps["pos_limit"]
    assert e2e.model("Positive Limit 1") is True  # B 接點未觸發：PLC 原始值 1
    table, row = e2e.show_io("Positive Limit 1")
    assert table.item(row, NAME_COL).text() == "X 軸正極限(B)"
    e2e.until(lambda: io_lamp_on(e2e, "Positive Limit 1"))
    assert lamp.caption == "+Limit(B)"
    assert page.selected_lamps["neg_limit"].caption == "−Limit(B)"
    assert page.selected_lamps["home_sensor"].caption == "Home Sensor"
    assert lamp.property("state") == "off"
    assert page.jog_plus.isEnabled()

    e2e.sim_call("master_write", "Positive Limit 1", False)  # 原始值 0：觸發
    e2e.until(lambda: not io_lamp_on(e2e, "Positive Limit 1"))
    e2e.until(lambda: lamp.property("state") == "fault")
    assert not page.jog_plus.isEnabled()
    assert page.jog_minus.isEnabled()
    assert jog_reasons(e2e, 1, 1) == ("第 1 軸（X）：正極限已觸發",)


def test_migration_ac10_a_contact_limit_raw_one_triggers(request, e2e_config_dir):
    set_attributes(e2e_config_dir, "Positive Limit 1", "")
    e2e = request.getfixturevalue("e2e")
    assert e2e.config.points.get("Positive Limit 1").invert is False
    started(e2e)
    page = e2e.app.axis_page
    lamp = page.selected_lamps["pos_limit"]
    assert e2e.model("Positive Limit 1") is False  # A 接點未觸發：原始值 0
    assert lamp.caption == "+Limit"
    assert "(B)" not in lamp.text()
    assert lamp.property("state") == "off"
    table, row = e2e.show_io("Positive Limit 1")
    assert table.item(row, NAME_COL).text() == "X 軸正極限"

    e2e.sim_call("master_write", "Positive Limit 1", True)
    e2e.until(lambda: lamp.property("state") == "fault")
    e2e.until(lambda: io_lamp_on(e2e, "Positive Limit 1"))
    assert not page.jog_plus.isEnabled()
    assert jog_reasons(e2e, 1, 1) == ("第 1 軸（X）：正極限已觸發",)


def move_reasons(e2e) -> tuple[str, ...] | None:
    """直接對 worker 下單軸移動；被拒回傳理由，否則等操作結束後回傳 None。"""
    ev = len(e2e.events)
    target = e2e.app.snapshot.axis(1).position + 1.0
    e2e.app.worker.move({1: target}, False, 50)
    e2e.until(lambda: e2e.rejected(ev) or e2e.finished(ev), 20000)
    rejected = e2e.rejected(ev)
    return rejected[0].reasons if rejected else None


def test_migration_ac28_b_contact_estop_raw_zero_rejects_move(e2e):
    started(e2e)
    assert move_reasons(e2e) is None  # 原始值 1：未觸發，可以移動
    e2e.sim_call("master_write", "Emergency Stop", False)
    e2e.until(lambda: not e2e.app.snapshot.estop_ok)
    assert move_reasons(e2e) == (REASON_ESTOP,)


def test_migration_ac28_a_contact_estop_rejects_only_at_raw_one(request, e2e_config_dir):
    set_attributes(e2e_config_dir, "Emergency Stop", "")
    e2e = request.getfixturevalue("e2e")
    started(e2e)
    assert e2e.model("Emergency Stop") is False
    assert move_reasons(e2e) is None
    e2e.sim_call("master_write", "Emergency Stop", True)
    e2e.until(lambda: not e2e.app.snapshot.estop_ok)
    assert move_reasons(e2e) == (REASON_ESTOP,)


def test_migration_ac29_inverted_coil_double_click_writes_inverse_raw(request, e2e_config_dir):
    path = e2e_config_dir / "modbus" / "Channel01" / "io_points.csv"
    records = read_csv(path)
    records.append(
        [
            "Inv Coil",
            "反相線圈",
            "300",
            "1",
            "0",
            "Coil",
            "Bool",
            "Read Write",
            "O",
            "_M300",
            "invert=true",
            "Unchecked",
        ]
    )
    write_csv(path, records)
    ranges = e2e_config_dir / "modbus" / "Channel01" / "read_ranges.toml"
    text = ranges.read_text(encoding="utf-8")
    ranges.write_text(text.replace('"20~84, 120~184"', '"20~84, 120~184, 300"'), encoding="utf-8")

    e2e = request.getfixturevalue("e2e")
    started(e2e)
    table, row = e2e.show_io("Inv Coil")
    assert table.item(row, NAME_COL).text() == "反相線圈(B)"
    assert e2e.model("Inv Coil") is True  # 模擬器初始原始值 1
    e2e.until(lambda: io_lamp_on(e2e, "Inv Coil"))

    since = e2e.mark()
    e2e.click_cell(table, row, NAME_COL, double=True)
    e2e.wait_entry("write", "Inv Coil", False, since)  # 模擬器收到原始值 0
    assert e2e.model("Inv Coil") is False
    e2e.until(lambda: not io_lamp_on(e2e, "Inv Coil"))

    since = e2e.mark()
    e2e.click_cell(table, row, NAME_COL, double=True)
    e2e.wait_entry("write", "Inv Coil", True, since)
    assert e2e.model("Inv Coil") is True
    e2e.until(lambda: io_lamp_on(e2e, "Inv Coil"))
