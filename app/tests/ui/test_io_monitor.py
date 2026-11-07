"""IoMonitorPage（spec FR-37 ~ FR-40）：斷言畫面項目、假 worker 收到的呼叫與 io_points.csv 內容。"""

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from ui_support import status

from lib.modbus_tags import Access
from plc_motion.app import MotionApp
from plc_motion.plc import ModelPort, PlcModel
from plc_motion.settings import load_config
from plc_motion.settings.io_points import CheckStatus
from plc_motion.sim import SimServer
from plc_motion.ui.io_monitor import DESC_COL, NAME_COL, STRIPE_COL, VALUE_COL, IoMonitorPage
from plc_motion.ui.theme import color
from plc_motion.ui.value_dialog import ValueDialog

RO, RW = Access.READ_ONLY, Access.READ_WRITE


def make_page(qtbot, config, worker):
    page = IoMonitorPage(config, worker)
    qtbot.addWidget(page)
    page.resize(1400, 800)
    page.show()
    qtbot.waitExposed(page)
    return page


@pytest.fixture
def page(qtbot, config, fake_worker):
    return make_page(qtbot, config, fake_worker)


def enable(fake_worker, snapshot, locked=False):
    fake_worker.snapshot_ready.emit(snapshot, status(locked))


def tags_in(page, kind, access) -> list[str]:
    table = page.tables[(kind, access)]
    return [page.tag_at(table, row) for row in range(table.rowCount())]


def visible_tags(page) -> set[str]:
    out = set()
    for table in page.tables.values():
        for row in range(table.rowCount()):
            if not table.isRowHidden(row):
                out.add(page.tag_at(table, row))
    return out


def cell(page, tag, col):
    table, row = page.locate(tag)
    return table.item(row, col)


def double_click(qtbot, page, tag):
    table, row = page.locate(tag)
    page.show_kind(page.kind_of(tag))
    table.scrollToItem(table.item(row, NAME_COL))
    rect = table.visualItemRect(table.item(row, NAME_COL))
    # 實際操作的順序是 press、release 再 double-click；QAbstractItemView 需要先有 press 的 index
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qtbot.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())


def answer_value_dialog(text: str, seen: dict) -> None:
    """下一個 ValueDialog 出現時輸入 `text` 按寫入；被拒（視窗仍開著）就記下錯誤並取消。

    `seen["stop"] = True` 停止等待（預期不會開視窗時用，免得輪詢延續到後面的測試）。
    """

    def poll():
        if seen.get("stop"):
            return
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, ValueDialog):
            QTimer.singleShot(20, poll)
            return
        seen["title"] = dialog.windowTitle()
        dialog.value_edit.setText(text)
        dialog.write_button.click()
        if dialog.isVisible():
            seen["error"] = dialog.error_label.text()
            dialog.cancel_button.click()

    QTimer.singleShot(0, poll)


def saved_status(config, tag) -> CheckStatus:
    return load_config(config.config_dir).points.get(tag).check_status


# ---- 版面與顯示 ----


def test_hidden_points_not_shown(page):
    shown = visible_tags(page) | {
        page.tag_at(t, r) for t in page.tables.values() for r in range(t.rowCount())
    }
    for n in range(1, 8):
        assert f"JOG Plus {n}" not in shown
        assert f"JOG Minus {n}" not in shown
        assert f"Request {n}" not in shown
    assert "Servo Motor 1" in shown


def test_points_split_by_kind_and_access(page):
    assert "Ready 1" in tags_in(page, "digital", RO)
    assert "Positive Limit 1" in tags_in(page, "digital", RO)
    assert "Emergency Stop" in tags_in(page, "digital", RO)
    assert "Servo Motor 1" in tags_in(page, "digital", RW)
    assert "Reset Alarm 7" in tags_in(page, "digital", RW)
    assert "Alarm Code 1" in tags_in(page, "numeric", RO)
    assert "Current Position 3" in tags_in(page, "numeric", RO)
    assert "Speed 1" in tags_in(page, "numeric", RW)
    assert "Target Position 7" in tags_in(page, "numeric", RW)
    assert "Speed 1" not in tags_in(page, "digital", RW)


def test_columns_are_stripe_name_description_value(page):
    """不顯示 Tag Name、Check Status 欄；色條之後依序為名稱、描述、值。"""
    for table in page.tables.values():
        headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
        assert headers == ["", "名稱", "描述", "值"]
    assert (STRIPE_COL, NAME_COL, DESC_COL, VALUE_COL) == (0, 1, 2, 3)


def test_no_reset_all_button(page):
    from PySide6.QtWidgets import QPushButton

    texts = [b.text() for b in page.findChildren(QPushButton)]
    assert not any("重設" in t for t in texts)


def test_plc_location_drops_leading_underscore(
    qtbot, config_dir, io_rows, write_io_points, fake_worker
):
    for row in io_rows:
        if row["Tag Name"] == "Ready 1":
            row["Description"] = "_M22"
        if row["Tag Name"] == "Busy 1":
            row["Description"] = "Y20"
    write_io_points(config_dir, io_rows)
    page = make_page(qtbot, load_config(config_dir), fake_worker)
    assert cell(page, "Ready 1", DESC_COL).text() == "M22"
    assert cell(page, "Busy 1", DESC_COL).text() == "Y20"


def test_rows_follow_file_order(page):
    rw = tags_in(page, "digital", RW)
    assert rw.index("Servo Motor 1") < rw.index("Reset Alarm 1") < rw.index("Servo Motor 2")


def test_b_contact_marked(page):
    assert cell(page, "Positive Limit 1", NAME_COL).text() == "X 軸正極限(B)"
    assert cell(page, "Emergency Stop", NAME_COL).text() == "緊急停止(B)"
    assert cell(page, "Home Sensor 1", NAME_COL).text() == "X 軸原點"


def test_display_name_blank_uses_tag(qtbot, config_dir, io_rows, write_io_points, fake_worker):
    for row in io_rows:
        if row["Tag Name"] == "Ready 1":
            row["Display Name"] = ""
    write_io_points(config_dir, io_rows)
    page = make_page(qtbot, load_config(config_dir), fake_worker)
    assert cell(page, "Ready 1", NAME_COL).text() == "Ready 1"


def test_position_shows_scaled_value_and_unit(page, fake_worker, make_snapshot):
    enable(
        fake_worker,
        make_snapshot({"Current Position 1": 15000, "Current Position 4": -3050, "Speed 2": 70}),
    )
    assert cell(page, "Current Position 1", VALUE_COL).text() == "1.5000 mm"
    assert cell(page, "Current Position 4", VALUE_COL).text() == "-30.50 deg"
    assert cell(page, "Speed 2", VALUE_COL).text() == "70"


def test_boolean_lamp_follows_raw(page, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot({"Servo Motor 1": True}))
    on, off = cell(page, "Servo Motor 1", VALUE_COL), cell(page, "Servo Motor 2", VALUE_COL)
    assert on.text().endswith("ON") and on.foreground().color() == color("ok")
    assert off.text().endswith("OFF") and off.foreground().color() == color("text_dim")


def test_no_snapshot_shows_placeholder(page, fake_worker):
    fake_worker.snapshot_ready.emit(None, status(connected=False))
    assert cell(page, "Current Position 1", VALUE_COL).text() == "—"


# ---- 寫入 ----


def test_rw_boolean_double_click_toggles(qtbot, page, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot({"Servo Motor 2": True}))
    double_click(qtbot, page, "Servo Motor 1")
    double_click(qtbot, page, "Servo Motor 2")
    assert fake_worker.calls == [
        ("write_point", "Servo Motor 1", True),
        ("write_point", "Servo Motor 2", False),
    ]


def test_ro_double_click_does_nothing(qtbot, page, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot())
    double_click(qtbot, page, "Ready 1")
    double_click(qtbot, page, "Alarm Code 1")
    assert fake_worker.calls == []


@pytest.mark.parametrize(
    ("tag", "text", "raw"),
    [
        ("Speed 1", "70", 70),
        ("Speed 3", "1", 1),
        ("Speed 3", "100", 100),
        ("PC Action Code 1", "0", 0),
        ("PC Action Code 1", "65535", 65535),
        ("Target Position 1", "1.5", 15000),
        ("Target Position 1", "-0.00005", -1),  # 四捨五入（0.5 進位）
        ("Target Position 4", "-2.25", -225),
        ("Target Position 7", "12.34", 1234),
    ],
)
def test_numeric_dialog_writes_raw(qtbot, page, fake_worker, make_snapshot, tag, text, raw):
    enable(fake_worker, make_snapshot())
    seen: dict = {}
    answer_value_dialog(text, seen)
    double_click(qtbot, page, tag)
    assert "error" not in seen
    assert fake_worker.calls == [("write_point", tag, raw)]


@pytest.mark.parametrize(
    ("tag", "text"),
    [
        ("Speed 1", "0"),
        ("Speed 1", "101"),
        ("Speed 1", "50.5"),
        ("PC Action Code 1", "65536"),
        ("PC Action Code 1", "-1"),
        ("PC Action Code 1", "abc"),
        ("Target Position 1", "300000"),  # × 10000 超出 I32
        ("Target Position 1", "nan"),
        ("Target Position 1", ""),
    ],
)
def test_numeric_dialog_rejects_out_of_range(qtbot, page, fake_worker, make_snapshot, tag, text):
    enable(fake_worker, make_snapshot())
    seen: dict = {}
    answer_value_dialog(text, seen)
    double_click(qtbot, page, tag)
    assert seen.get("error")
    assert fake_worker.calls == []


def test_numeric_dialog_shows_current_value_and_range(qtbot, config):
    dialog = ValueDialog(config.points.get("Speed 1"), 50)
    qtbot.addWidget(dialog)
    assert "50" in dialog.info_label.text()
    assert "1 ~ 100" in dialog.info_label.text()
    dialog = ValueDialog(config.points.get("Target Position 1"), 1.5)
    qtbot.addWidget(dialog)
    assert "1.5000 mm" in dialog.info_label.text()


def test_locked_double_click_does_not_write(qtbot, page, fake_worker, make_snapshot):
    enable(fake_worker, make_snapshot(), locked=True)
    seen: dict = {}
    answer_value_dialog("70", seen)  # 若誤開視窗會被填值寫入
    double_click(qtbot, page, "Servo Motor 1")
    double_click(qtbot, page, "Speed 1")
    qtbot.wait(100)
    seen["stop"] = True
    assert fake_worker.calls == []
    assert "title" not in seen
    assert "鎖定中" in page.lock_label.text()


def test_no_snapshot_double_click_does_not_write(qtbot, page, fake_worker):
    double_click(qtbot, page, "Servo Motor 1")
    assert fake_worker.calls == []


# ---- Check Status ----


def stripe(page, tag):
    return cell(page, tag, STRIPE_COL).background().color()


def test_set_check_status_colors_stripe_only_counts_and_file(page, config):
    total = len(visible_tags(page))
    assert page.count_labels[CheckStatus.UNCHECKED].text() == f"Unchecked {total}"
    plain = cell(page, "Ready 1", NAME_COL).background()
    page.check_status_menu("Ready 1").actions()[1].trigger()  # NG
    assert saved_status(config, "Ready 1") == CheckStatus.NG
    assert saved_status(config, "Busy 1") == CheckStatus.UNCHECKED
    assert stripe(page, "Ready 1") == color("fault")
    assert stripe(page, "Busy 1") == color("off")
    for col in (NAME_COL, DESC_COL, VALUE_COL):  # 整列不上底色
        assert cell(page, "Ready 1", col).background() == plain
    assert page.count_labels[CheckStatus.NG].text() == "NG 1"
    assert page.count_labels[CheckStatus.UNCHECKED].text() == f"Unchecked {total - 1}"


def test_check_status_menu_lists_five_states(page):
    texts = [a.text() for a in page.check_status_menu("Speed 1").actions()]
    assert texts == ["OK", "NG", "Unchecked", "Recheck", "Ignore"]


def test_initial_status_from_file_is_colored(
    qtbot, config_dir, io_rows, write_io_points, fake_worker
):
    for row in io_rows:
        if row["Tag Name"] == "Busy 2":
            row["Check Status"] = "Recheck"
    write_io_points(config_dir, io_rows)
    page = make_page(qtbot, load_config(config_dir), fake_worker)
    assert stripe(page, "Busy 2") == color("warn")
    assert page.count_labels[CheckStatus.RECHECK].text() == "Recheck 1"


# ---- 搜尋 ----


def test_search_ignores_tag_name(page):
    page.search.setText("servo motor 1")
    assert visible_tags(page) == set()  # 只搜名稱；Tag Name 不顯示也不搜


def test_search_matches_tag_shown_as_name(qtbot, config_dir, io_rows, write_io_points, fake_worker):
    for row in io_rows:
        if row["Tag Name"] == "Ready 1":
            row["Display Name"] = ""  # 名稱欄顯示 Tag Name，就能用它搜到
    write_io_points(config_dir, io_rows)
    page = make_page(qtbot, load_config(config_dir), fake_worker)
    page.search.setText("ready 1")
    assert visible_tags(page) == {"Ready 1"}


def test_search_filters_display_name(page):
    page.search.setText("升降 軸就緒")
    assert visible_tags(page) == {"Ready 7"}
    page.search.setText("")
    assert "Ready 2" in visible_tags(page)


# ---- 自訂非軸 Tag ----


def test_custom_tags_in_correct_column(qtbot, config_dir, io_rows, write_io_points, fake_worker):
    base = {
        "Unit ID": "0",
        "Visible": "O",
        "Description": "",
        "Attributes": "",
        "Check Status": "Unchecked",
        "Address Length": "1",
    }
    io_rows += [
        {
            **base,
            "Tag Name": "Cylinder1 Up",
            "Display Name": "",
            "Start Address": "500",
            "Data Model": "Coil",
            "Data Type": "Bool",
            "Access": "Read Write",
            "Description": "Y20",
        },
        {
            **base,
            "Tag Name": "Door Sensor",
            "Display Name": "門檢",
            "Start Address": "30000",
            "Data Model": "Discrete Input",
            "Data Type": "Bool",
            "Access": "Read Only",
        },
        {
            **base,
            "Tag Name": "Counter",
            "Display Name": "計數",
            "Start Address": "600",
            "Data Model": "Holding Register",
            "Data Type": "U16",
            "Access": "Read Write",
        },
    ]
    write_io_points(config_dir, io_rows)
    config = load_config(config_dir)
    page = make_page(qtbot, config, fake_worker)
    assert "Cylinder1 Up" in tags_in(page, "digital", RW)
    assert "Door Sensor" in tags_in(page, "digital", RO)
    assert "Counter" in tags_in(page, "numeric", RW)
    assert cell(page, "Cylinder1 Up", NAME_COL).text() == "Cylinder1 Up"

    fake_worker.snapshot_ready.emit(ModelPort(PlcModel(config.points)).read_all(), status())
    double_click(qtbot, page, "Cylinder1 Up")
    seen: dict = {}
    answer_value_dialog("65535", seen)
    double_click(qtbot, page, "Counter")
    assert fake_worker.calls == [
        ("write_point", "Cylinder1 Up", True),
        ("write_point", "Counter", 65535),
    ]


# ---- 組裝：真 worker 連 SimServer ----


@pytest.fixture
def motion(qtbot, config):
    server = SimServer(PlcModel(config.points), config.points, port=0)
    server.start()
    m = MotionApp(config, "127.0.0.1", server.port)
    m.show()
    yield m, server
    m.shutdown()
    server.stop()


def test_motion_app_mounts_io_monitor_and_writes_to_plc(qtbot, motion):
    m, server = motion
    page = m.window.pages.widget(1)
    assert isinstance(page, IoMonitorPage)
    assert m.window.nav.buttons[1].text() == "IO Monitor"
    m.window.pages.setCurrentIndex(1)
    qtbot.waitUntil(lambda: m.snapshot is not None and not m.last_status.locked, timeout=5000)
    double_click(qtbot, page, "Servo Motor 3")

    def servo_on() -> bool:
        with server.lock:
            return bool(server.model.read("Servo Motor 3"))

    qtbot.waitUntil(servo_on, timeout=5000)
    qtbot.waitUntil(
        lambda: cell(page, "Servo Motor 3", VALUE_COL).foreground().color() == color("ok"),
        timeout=5000,
    )
