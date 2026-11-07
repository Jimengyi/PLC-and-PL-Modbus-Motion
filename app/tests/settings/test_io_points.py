"""io_points.csv：欄位、第二層啟動檢查（spec FR-11）與 Check Status 寫回（FR-10）。"""

import os
from pathlib import Path

import pytest

from lib.modbus_tags import Access, DataModel
from plc_motion.settings import ConfigErrors, load_config

BOM = b"\xef\xbb\xbf"


def _errors(config_dir: Path) -> list[str]:
    with pytest.raises(ConfigErrors) as exc:
        load_config(config_dir)
    return exc.value.errors


def _row(rows, tag):
    return next(r for r in rows if r["Tag Name"] == tag)


def _channel(config_dir: Path) -> Path:
    return config_dir / "modbus" / "Channel01"


def _io_path(config_dir: Path) -> Path:
    return _channel(config_dir) / "io_points.csv"


# ---- 欄位 ----


def test_point_fields_are_parsed(config_dir):
    points = load_config(config_dir).points

    p = points.axis_tag("Current Position", 4)
    assert p.tag == "Current Position 4"
    assert p.display_name == "RX 軸目前位置"
    assert (p.start_address, p.length, p.unit_id) == (52, 2, 0)
    assert (p.data_model, p.data_type, p.access) == (
        DataModel.HOLDING_REGISTER,
        "I32",
        Access.READ_ONLY,
    )
    assert p.visible is True
    assert (p.scale, p.unit) == (100, "deg")
    assert type(p.scale) is int
    assert p.check_status == "Unchecked"
    assert (p.bit, p.invert, p.word_order, p.byte_swap) == (None, False, None, False)
    assert p.line == 2 + 18 * 3 + 6  # 表頭第 1 列；第 4 軸第 7 個點

    assert points.get("Positive Limit 1").invert is True
    assert points.get("Home Sensor 1").invert is False
    assert points.get("JOG Plus 3").visible is False
    assert points.get("Current Position 1").scale == 10000
    assert points.get("Ready 1").scale is None


def test_point_has_no_legacy_fields(config_dir):
    p = load_config(config_dir).points.get("Positive Limit 1")

    for name in ("sensor_type", "attributes", "is_b_contact"):
        assert not hasattr(p, name), name


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


def _free_row(io_rows, **values) -> dict[str, str]:
    return {**_row(io_rows, "Servo Motor 1"), "Display Name": "", **values}


def test_free_points_may_use_any_library_type(config_dir, io_rows, write_io_points):
    """FR-13：非必要點可用函式庫的全部 Data Model、Data Type 與 Attributes。"""
    io_rows += [
        _free_row(
            io_rows,
            **{
                "Tag Name": "Flow",
                "Start Address": "300",
                "Address Length": "2",
                "Data Model": "Input Register",
                "Data Type": "F32",
                "Access": "Read Only",
                "Attributes": "scale=2.5;unit=L;word_order=high_first;byte_swap=true",
            },
        ),
        _free_row(
            io_rows,
            **{
                "Tag Name": "Lamp",
                "Start Address": "400.b3",
                "Data Model": "Holding Register",
                "Data Type": "Bool",
                "Attributes": "invert=true",
            },
        ),
        _free_row(
            io_rows,
            **{
                "Tag Name": "Name",
                "Start Address": "410",
                "Address Length": "4",
                "Data Model": "Holding Register",
                "Data Type": "String",
            },
        ),
    ]
    write_io_points(config_dir, io_rows)

    points = load_config(config_dir).points

    flow = points.get("Flow")
    assert (flow.data_model, flow.data_type, flow.access) == (
        DataModel.INPUT_REGISTER,
        "F32",
        Access.READ_ONLY,
    )
    assert (flow.scale, flow.unit, flow.word_order, flow.byte_swap) == (
        2.5,
        "L",
        "high_first",
        True,
    )
    lamp = points.get("Lamp")
    assert (lamp.start_address, lamp.bit, lamp.invert) == (400, 3, True)
    assert points.get("Name").length == 4


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


# ---- 第二層：Visible、Check Status ----


@pytest.mark.parametrize("column", ["Visible", "Check Status"])
def test_missing_app_column_is_reported(config_dir, io_rows, write_io_points, column):
    header = [c for c in io_rows[0] if c != column]
    for r in io_rows:
        del r[column]
    write_io_points(config_dir, io_rows, header=header)

    assert any("io_points.csv" in e and column in e for e in _errors(config_dir))


@pytest.mark.parametrize(("column", "value"), [("Visible", "Y"), ("Check Status", "Done")])
def test_app_column_value_out_of_range_is_reported(
    config_dir, io_rows, write_io_points, column, value
):
    _row(io_rows, "Home Sensor 2")[column] = value
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Home Sensor 2" in e and column in e and value in e for e in errors), errors


def test_error_message_includes_line_number(config_dir, io_rows, write_io_points):
    # 第 1 列為表頭，Find Home Finish 1 是第 1 軸第 3 個點：第 4 列
    _row(io_rows, "Find Home Finish 1")["Visible"] = "Z"
    write_io_points(config_dir, io_rows)

    assert any("第 4 列" in e and "Find Home Finish 1" in e for e in _errors(config_dir))


# ---- 第二層：必要 Tag ----


def test_missing_required_tag_is_reported(config_dir, io_rows, write_io_points):
    io_rows.remove(_row(io_rows, "Reply 3"))
    write_io_points(config_dir, io_rows)

    assert any("缺少必要 Tag「Reply 3」" in e for e in _errors(config_dir))


def test_missing_emergency_stop_is_reported(config_dir, io_rows, write_io_points):
    io_rows.remove(_row(io_rows, "Emergency Stop"))
    write_io_points(config_dir, io_rows)

    assert any("缺少必要 Tag「Emergency Stop」" in e for e in _errors(config_dir))


@pytest.mark.parametrize(
    ("tag", "changes", "expected"),
    [
        ("Ready 1", {"Data Model": "Discrete Input"}, "Data Model 應為「Coil」"),
        ("Home Sensor 1", {"Data Model": "Coil"}, "Data Model 應為「Discrete Input」"),
        ("Alarm Code 2", {"Data Type": "I16"}, "Data Type 應為「U16」，實際為「I16」"),
        (
            "Current Position 3",
            {"Data Type": "U32", "Attributes": "scale=10000;unit=mm"},
            "Data Type 應為「I32」，實際為「U32」",
        ),
        ("Speed 3", {"Access": "Read Only"}, "Access 應為「Read Write」，實際為「Read Only」"),
        (
            "Speed 4",
            {"Data Type": "I32", "Address Length": "2", "Start Address": "300"},
            "Address Length 應為「1」，實際為「2」",
        ),
    ],
)
def test_required_tag_type_mismatch_is_reported(
    config_dir, io_rows, write_io_points, tag, changes, expected
):
    _row(io_rows, tag).update(changes)
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any(tag in e and expected in e for e in errors), errors


@pytest.mark.parametrize(
    ("tag", "attributes", "expected"),
    [
        ("Current Position 1", "scale=10000;unit=mm;word_order=high_first", "word_order"),
        ("Target Position 2", "scale=10000;unit=mm;byte_swap=true", "byte_swap"),
        ("Current Position 3", "unit=mm", "scale"),
        ("Current Position 3", "scale=10000", "unit"),
        ("Target Position 4", "scale=0.5;unit=deg", "正整數"),
        ("Speed 1", "scale=10", "scale"),
        ("Alarm Code 1", "unit=%", "unit"),
        ("Ready 1", "invert=true", "invert"),
        ("Servo Motor 2", "invert=true", "invert"),
    ],
)
def test_required_tag_attribute_not_allowed_is_reported(
    config_dir, io_rows, write_io_points, tag, attributes, expected
):
    _row(io_rows, tag)["Attributes"] = attributes
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any(tag in e and "Attributes" in e and expected in e for e in errors), errors


@pytest.mark.parametrize(
    ("tag", "attributes"),
    [
        ("Current Position 1", "scale=10000;unit=mm;word_order=low_first;byte_swap=false"),
        ("Ready 1", "invert=false"),
        ("Positive Limit 1", ""),  # FR-12：極限可為 A 接點
        ("Home Sensor 1", "invert=true"),
        ("Emergency Stop", ""),
        ("Current Position 2", "scale=10000.0;unit=mm"),
    ],
)
def test_required_tag_default_valued_or_allowed_attributes_are_accepted(
    config_dir, io_rows, write_io_points, tag, attributes
):
    _row(io_rows, tag)["Attributes"] = attributes
    write_io_points(config_dir, io_rows)

    point = load_config(config_dir).points.get(tag)

    assert point.invert == ("invert=true" in attributes)


def test_current_and_target_position_scale_mismatch_is_reported(
    config_dir, io_rows, write_io_points
):
    _row(io_rows, "Target Position 2")["Attributes"] = "scale=100;unit=mm"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any(
        "Current Position 2" in e and "Target Position 2" in e and "倍率或單位不一致" in e
        for e in errors
    ), errors


def test_current_and_target_position_unit_mismatch_is_reported(
    config_dir, io_rows, write_io_points
):
    _row(io_rows, "Current Position 7")["Attributes"] = "scale=100;unit=deg"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Current Position 7" in e and "Target Position 7" in e for e in errors)


# ---- 第二層：位址重疊 ----


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


def _bit_row(io_rows, tag: str, address: str) -> dict[str, str]:
    return _free_row(
        io_rows,
        **{
            "Tag Name": tag,
            "Start Address": address,
            "Data Model": "Holding Register",
            "Data Type": "Bool",
        },
    )


def test_different_bits_of_one_register_are_not_overlap(config_dir, io_rows, write_io_points):
    io_rows += [_bit_row(io_rows, "Bit A", "500.b0"), _bit_row(io_rows, "Bit B", "500.b1")]
    write_io_points(config_dir, io_rows)

    load_config(config_dir)


def test_same_bit_of_one_register_is_overlap(config_dir, io_rows, write_io_points):
    io_rows += [_bit_row(io_rows, "Bit A", "500.b2"), _bit_row(io_rows, "Bit B", "500.b2")]
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("重疊" in e and "Bit A" in e and "Bit B" in e for e in errors), errors


def test_two_errors_are_both_reported(config_dir, io_rows, write_io_points):
    io_rows.remove(_row(io_rows, "Speed 1"))
    _row(io_rows, "Busy 4")["Visible"] = "Z"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Speed 1" in e for e in errors)
    assert any("Busy 4" in e and "Visible" in e for e in errors)


# ---- 寫回 Check Status ----


def test_save_check_status_changes_only_that_field(config_dir):
    path = _io_path(config_dir)
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
    path = _io_path(config_dir)
    before = path.read_bytes()
    points = load_config(config_dir).points

    points.save_check_status("Ready 1", "Recheck")
    points.save_check_status("Ready 1", "Unchecked")

    assert path.read_bytes() == BOM + before


def test_save_check_status_keeps_quoted_fields(config_dir, io_rows, write_io_points):
    # 含逗號的欄位會被引號包住；寫回後引號必須原樣保留
    _row(io_rows, "Current Position 1")["Description"] = "_DD22,_DD23"
    path = write_io_points(config_dir, io_rows)
    before = path.read_bytes()
    assert b'"_DD22,_DD23",scale=10000;unit=mm,Unchecked\r\n' in before
    points = load_config(config_dir).points

    points.save_check_status("Current Position 1", "OK")

    assert path.read_bytes()[len(BOM) :] == before.replace(
        b'"_DD22,_DD23",scale=10000;unit=mm,Unchecked\r\n',
        b'"_DD22,_DD23",scale=10000;unit=mm,OK\r\n',
    )


def test_save_check_status_on_big5_file_writes_utf8_bom(config_dir, io_rows, write_io_points):
    write_io_points(config_dir, io_rows, encoding="big5")
    points = load_config(config_dir).points

    points.save_check_status("Emergency Stop", "OK")

    data = _io_path(config_dir).read_bytes()
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
    path = _io_path(config_dir)
    before = path.read_bytes()
    points = load_config(config_dir).points
    points.save_check_status("Busy 2", "NG")
    points.save_check_status("Emergency Stop", "Ignore")

    points.reset_all_check_status()

    assert path.read_bytes() == BOM + before
    assert {p.check_status for p in points.all()} == {"Unchecked"}


def test_save_check_status_failure_mid_write_keeps_original(config_dir, monkeypatch):
    path = _io_path(config_dir)
    before = path.read_bytes()
    points = load_config(config_dir).points

    def boom(*args, **kwargs):
        raise OSError("模擬寫入中斷")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        points.save_check_status("Busy 2", "NG")

    assert path.read_bytes() == before
    assert points.get("Busy 2").check_status == "Unchecked"
    assert sorted(p.name for p in _channel(config_dir).iterdir()) == [
        "device.toml",
        "io_points.csv",
    ]
