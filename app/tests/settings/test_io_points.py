import os
from pathlib import Path

import pytest

from plc_motion.settings import ConfigErrors, load_config

BOM = b"\xef\xbb\xbf"


def _errors(config_dir: Path) -> list[str]:
    with pytest.raises(ConfigErrors) as exc:
        load_config(config_dir)
    return exc.value.errors


def _row(rows, tag):
    return next(r for r in rows if r["Tag Name"] == tag)


# ---- 欄位解析 ----


def test_point_fields_are_parsed(config_dir):
    points = load_config(config_dir).points

    p = points.axis_tag("Current Position", 4)
    assert p.tag == "Current Position 4"
    assert p.display_name == "RX 軸目前位置"
    assert (p.start_address, p.length, p.unit_id) == (52, 2, 0)
    assert (p.data_model, p.sensor_type, p.access) == ("Holding Register", "Position", "Read Only")
    assert p.visible is True
    assert p.attributes == "100,deg"
    assert (p.scale, p.unit) == (100, "deg")
    assert p.check_status == "Unchecked"
    assert p.is_b_contact is False

    assert points.get("Positive Limit 1").is_b_contact is True
    assert points.get("Home Sensor 1").is_b_contact is False
    assert points.get("JOG Plus 3").visible is False
    assert points.get("Current Position 1").scale == 10000


def test_all_keeps_file_row_order(config_dir, io_rows):
    points = load_config(config_dir).points

    assert [p.tag for p in points.all()] == [r["Tag Name"] for r in io_rows]


def test_get_unknown_tag_raises_key_error(config_dir):
    with pytest.raises(KeyError):
        load_config(config_dir).points.get("No Such Tag")


def test_blank_display_name_falls_back_to_tag(config_dir, io_rows, write_io_points):
    _row(io_rows, "Busy 2")["Display Name"] = ""
    write_io_points(config_dir, io_rows)

    assert load_config(config_dir).points.get("Busy 2").label == "Busy 2"


def test_extra_free_tags_are_allowed(config_dir, io_rows, write_io_points):
    io_rows.append(
        {
            **_row(io_rows, "Servo Motor 1"),
            "Tag Name": "Cylinder 1 Up",
            "Display Name": "氣缸上",
            "Start Address": "300",
        }
    )
    write_io_points(config_dir, io_rows)

    assert load_config(config_dir).points.get("Cylinder 1 Up").start_address == 300


# ---- 編碼 ----


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "big5"],
    ids=["utf8-no-bom", "utf8-bom", "big5"],
)
def test_csv_encodings_load_with_correct_chinese(config_dir, io_rows, write_io_points, encoding):
    path = write_io_points(config_dir, io_rows, encoding=encoding)
    if encoding == "utf-8-sig":
        assert path.read_bytes().startswith(BOM)

    points = load_config(config_dir).points

    assert points.get("Emergency Stop").display_name == "緊急停止"
    assert points.get("Ready 7").display_name == "升降 軸就緒"


# ---- FR-8 錯誤 ----


def test_missing_column_is_reported(config_dir, io_rows, write_io_points):
    header = [
        "Tag Name",
        "Display Name",
        "Start Address",
        "Address Length",
        "Unit ID",
        "Data Model",
        "Sensor Type",
        "Access",
        "Visible",
        "Description",
        "Attributes",
    ]
    for r in io_rows:
        del r["Check Status"]
    write_io_points(config_dir, io_rows, header=header)

    assert any("io_points.csv" in e and "Check Status" in e for e in _errors(config_dir))


def test_non_integer_address_is_reported(config_dir, io_rows, write_io_points):
    _row(io_rows, "Busy 1")["Start Address"] = "abc"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Busy 1" in e and "Start Address" in e for e in errors)
    # 欄位有錯的必要 Tag 不另外被報成「缺少」
    assert not any("缺少" in e and "Busy 1" in e for e in errors)


def test_duplicate_tag_is_reported(config_dir, io_rows, write_io_points):
    dup = {**_row(io_rows, "Ready 1"), "Start Address": "999"}
    io_rows.append(dup)
    write_io_points(config_dir, io_rows)

    assert any("Ready 1" in e and "重複" in e for e in _errors(config_dir))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("Data Model", "Input Register"),
        ("Sensor Type", "Float"),
        ("Access", "Write Only"),
        ("Visible", "Y"),
        ("Check Status", "Done"),
    ],
)
def test_enum_value_out_of_range_is_reported(config_dir, io_rows, write_io_points, column, value):
    _row(io_rows, "Home Sensor 2")[column] = value
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Home Sensor 2" in e and column in e and value in e for e in errors)


def test_missing_required_tag_is_reported(config_dir, io_rows, write_io_points):
    io_rows.remove(_row(io_rows, "Reply 3"))
    write_io_points(config_dir, io_rows)

    assert any("缺少" in e and "Reply 3" in e for e in _errors(config_dir))


def test_missing_emergency_stop_is_reported(config_dir, io_rows, write_io_points):
    io_rows.remove(_row(io_rows, "Emergency Stop"))
    write_io_points(config_dir, io_rows)

    assert any("缺少" in e and "Emergency Stop" in e for e in _errors(config_dir))


@pytest.mark.parametrize(
    ("tag", "column", "value", "expected"),
    [
        ("Ready 1", "Data Model", "Discrete Input", "Coil"),
        ("Alarm Code 2", "Sensor Type", "Boolean", "U16"),
        ("Speed 3", "Access", "Read Only", "Read Write"),
        ("Current Position 4", "Address Length", "1", "2"),
        ("Positive Limit 1", "Attributes", "", "ON"),
        ("Emergency Stop", "Attributes", "", "ON"),
        ("Home Sensor 5", "Attributes", "ON", "空白"),
        ("Target Position 6", "Attributes", "100", "倍率,單位"),
    ],
)
def test_required_tag_type_mismatch_is_reported(
    config_dir, io_rows, write_io_points, tag, column, value, expected
):
    _row(io_rows, tag)[column] = value
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any(tag in e and column in e and expected in e for e in errors), errors


def test_current_and_target_position_scale_mismatch_is_reported(
    config_dir, io_rows, write_io_points
):
    _row(io_rows, "Target Position 2")["Attributes"] = "100,mm"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Current Position 2" in e and "Target Position 2" in e for e in errors)


def test_current_and_target_position_unit_mismatch_is_reported(
    config_dir, io_rows, write_io_points
):
    _row(io_rows, "Current Position 7")["Attributes"] = "100,deg"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Current Position 7" in e and "Target Position 7" in e for e in errors)


def test_address_overlap_in_same_unit_and_model_is_reported(config_dir, io_rows, write_io_points):
    # Current Position 1 佔 22~23；把 Alarm Code 1 放到 23 造成重疊
    _row(io_rows, "Alarm Code 1")["Start Address"] = "23"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("重疊" in e and "Alarm Code 1" in e and "Current Position 1" in e for e in errors)


def test_same_address_in_different_model_or_unit_is_not_overlap(
    config_dir, io_rows, write_io_points
):
    # Ready 1（Coil 20）與 Alarm Code 1（Holding Register 20）本就同位址不同 Data Model
    extra = {**_row(io_rows, "Ready 1"), "Tag Name": "Other Unit Ready", "Unit ID": "1"}
    io_rows.append(extra)
    write_io_points(config_dir, io_rows)

    load_config(config_dir)


def test_two_errors_are_both_reported(config_dir, io_rows, write_io_points):
    _row(io_rows, "Positive Limit 1")["Attributes"] = ""
    _row(io_rows, "Busy 4")["Visible"] = "Z"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Positive Limit 1" in e for e in errors)
    assert any("Busy 4" in e and "Visible" in e for e in errors)


def test_error_message_includes_line_number(config_dir, io_rows, write_io_points):
    # 第 1 列為表頭，Ready 1 在第 2 列
    _row(io_rows, "Ready 1")["Visible"] = "Z"
    write_io_points(config_dir, io_rows)

    assert any("第 2 列" in e and "Ready 1" in e for e in _errors(config_dir))


# ---- 寫回 Check Status ----


def test_save_check_status_changes_only_that_field(config_dir):
    path = config_dir / "io_points.csv"
    before = path.read_bytes()
    points = load_config(config_dir).points

    points.save_check_status("Busy 2", "NG")

    after = path.read_bytes()
    assert after.startswith(BOM)
    old_line = next(ln for ln in before.split(b"\r\n") if ln.startswith(b"Busy 2,"))
    assert old_line.endswith(b",Unchecked")
    new_line = old_line[: -len(b"Unchecked")] + b"NG"
    assert after[len(BOM) :] == before.replace(old_line + b"\r\n", new_line + b"\r\n")
    assert points.get("Busy 2").check_status == "NG"
    assert load_config(config_dir).points.get("Busy 2").check_status == "NG"


def test_save_check_status_twice_keeps_other_bytes(config_dir):
    path = config_dir / "io_points.csv"
    before = path.read_bytes()
    points = load_config(config_dir).points

    points.save_check_status("Ready 1", "Recheck")
    points.save_check_status("Ready 1", "Unchecked")

    assert path.read_bytes() == BOM + before


def test_save_check_status_keeps_quoted_fields(config_dir):
    # Position 的 Attributes 含逗號會被引號包住；寫回後引號必須原樣保留
    path = config_dir / "io_points.csv"
    before = path.read_bytes()
    assert b'"10000,mm",Unchecked' in before
    points = load_config(config_dir).points

    points.save_check_status("Current Position 1", "OK")

    old_line = "Current Position 1,X 軸目前位置,22,2,0,Holding Register,Position,Read Only,O,,"
    old_line = old_line.encode("utf-8") + b'"10000,mm",'
    assert old_line + b"Unchecked\r\n" in before
    assert path.read_bytes()[len(BOM) :] == before.replace(
        old_line + b"Unchecked\r\n", old_line + b"OK\r\n"
    )


def test_save_check_status_on_big5_file_writes_utf8_bom(config_dir, io_rows, write_io_points):
    write_io_points(config_dir, io_rows, encoding="big5")
    points = load_config(config_dir).points

    points.save_check_status("Emergency Stop", "OK")

    data = (config_dir / "io_points.csv").read_bytes()
    assert data.startswith(BOM)
    reloaded = load_config(config_dir).points
    assert reloaded.get("Emergency Stop").check_status == "OK"
    assert reloaded.get("Emergency Stop").display_name == "緊急停止"
    assert [p.tag for p in reloaded.all()] == [r["Tag Name"] for r in io_rows]


def test_save_check_status_rejects_unknown_status(config_dir):
    points = load_config(config_dir).points

    with pytest.raises(ValueError):
        points.save_check_status("Busy 2", "Done")


def test_reset_all_check_status(config_dir):
    path = config_dir / "io_points.csv"
    before = path.read_bytes()
    points = load_config(config_dir).points
    points.save_check_status("Busy 2", "NG")
    points.save_check_status("Emergency Stop", "Ignore")

    points.reset_all_check_status()

    assert path.read_bytes() == BOM + before
    assert {p.check_status for p in points.all()} == {"Unchecked"}


def test_save_check_status_failure_mid_write_keeps_original(config_dir, monkeypatch):
    path = config_dir / "io_points.csv"
    before = path.read_bytes()
    points = load_config(config_dir).points

    def boom(*args, **kwargs):
        raise OSError("模擬寫入中斷")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        points.save_check_status("Busy 2", "NG")

    assert path.read_bytes() == before
    assert points.get("Busy 2").check_status == "Unchecked"
    assert sorted(p.name for p in config_dir.iterdir()) == [
        "axes.toml",
        "device.toml",
        "io_points.csv",
    ]
