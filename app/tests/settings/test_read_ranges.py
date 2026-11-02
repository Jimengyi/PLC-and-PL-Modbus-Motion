"""read_ranges.toml：解析、啟動檢查、與 load_config 的整合（spec FR-61）。"""

from pathlib import Path

import pytest

from plc_motion.settings import ConfigErrors, DataModel, load_config
from plc_motion.settings.read_ranges import ReadRange

_COIL, _DI, _HR = DataModel.COIL, DataModel.DISCRETE_INPUT, DataModel.HOLDING_REGISTER

# 涵蓋測試用預設 io_points（tests/conftest.py）全部點的範圍，與 app/config 的預設相同
FULL = """
[[unit]]
unit_id = 0
coil = "20~84, 120~184"
holding_register = "20~83, 120~183"
discrete_input = "24576~24612"
"""


def write_ranges(config_dir: Path, text: str) -> None:
    (config_dir / "read_ranges.toml").write_bytes(text.encode("utf-8"))


def errors_for(config_dir: Path, text: str) -> list[str]:
    write_ranges(config_dir, text)
    with pytest.raises(ConfigErrors) as ei:
        load_config(config_dir)
    return ei.value.errors


def test_missing_file_means_auto_grouping(config_dir):
    assert load_config(config_dir).read_ranges is None


def test_file_without_units_means_auto_grouping(config_dir):
    write_ranges(config_dir, "# 只有註解\n")
    assert load_config(config_dir).read_ranges is None


def test_ranges_parsed_in_file_order_inclusive(config_dir):
    write_ranges(config_dir, FULL)
    ranges = load_config(config_dir).read_ranges
    assert ranges == (
        ReadRange(0, _COIL, 20, 84),
        ReadRange(0, _COIL, 120, 184),
        ReadRange(0, _DI, 24576, 24612),
        ReadRange(0, _HR, 20, 83),
        ReadRange(0, _HR, 120, 183),
    )
    assert ranges[0].count == 65


def test_single_address_and_spaces_and_empty_pieces(config_dir):
    text = FULL.replace('"20~84, 120~184"', '" 20 ~ 84 ,, 120~184, 200 "')
    write_ranges(config_dir, text)
    coils = [r for r in load_config(config_dir).read_ranges if r.data_model == _COIL]
    assert [(r.start, r.end) for r in coils] == [(20, 84), (120, 184), (200, 200)]


@pytest.mark.parametrize(
    "piece, expected",
    [
        ("20-84", "格式不對"),
        ("abc", "格式不對"),
        ("20~30~40", "格式不對"),
        ("84~20", "起始位址大於結束位址"),
        ("20~65536", "超出位址上限"),
    ],
)
def test_bad_range_text_is_reported(config_dir, piece, expected):
    errors = errors_for(config_dir, FULL.replace('"24576~24612"', f'"{piece}"'))
    assert any(expected in e and piece in e and "discrete_input" in e for e in errors), errors


def test_range_over_request_limit_is_reported(config_dir):
    # holding_register 單筆上限 125：20~145 共 126 個
    errors = errors_for(config_dir, FULL.replace('"20~83, 120~183"', '"20~145, 150~183"'))
    assert any("126 個位址" in e and "上限 125" in e for e in errors), errors
    # coil 上限 2000：同樣 126 個不算超過
    write_ranges(config_dir, FULL.replace('"20~84, 120~184"', '"20~145, 150~184"'))
    load_config(config_dir)


def test_overlapping_ranges_are_reported(config_dir):
    errors = errors_for(config_dir, FULL.replace('"20~84, 120~184"', '"20~84, 80~184"'))
    assert any("重疊" in e and "Coil" in e for e in errors), errors


def test_unknown_keys_and_input_register_are_reported(config_dir):
    text = FULL + 'input_register = "1~2"\ncoils = "1"\n'
    errors = errors_for(config_dir, text + "[extra]\nx = 1\n")
    assert any("input_register" in e and "Input Register" in e for e in errors), errors
    assert any("「coils」" in e for e in errors), errors
    assert any("「extra」" in e for e in errors), errors


def test_bad_or_duplicate_unit_id_is_reported(config_dir):
    errors = errors_for(config_dir, FULL + FULL + '[[unit]]\nunit_id = "0"\n')
    assert any("unit_id 0 重複" in e for e in errors), errors
    assert any("unit_id 必須是非負整數" in e for e in errors), errors


def test_non_string_range_is_reported(config_dir):
    errors = errors_for(config_dir, FULL.replace('"24576~24612"', "24576"))
    assert any("discrete_input 必須是字串" in e for e in errors), errors


def test_point_outside_every_range_is_reported_with_line_and_address(config_dir):
    # Ready 7 在 Coil 80：範圍改成 20~79 後不在任何範圍內
    errors = errors_for(config_dir, FULL.replace('"20~84, 120~184"', '"20~79, 120~184"'))
    outside = [e for e in errors if "不在任何讀取範圍內" in e]
    assert any("「Ready 7」" in e and "Coil 80" in e and "第 " in e for e in outside), errors
    # 80~84 中有定義的四個點都被列出（Coil 83 是空號）
    assert len(outside) == 4


def test_empty_model_means_its_points_are_not_covered(config_dir):
    errors = errors_for(config_dir, FULL.replace('"24576~24612"', '""'))
    assert any("Emergency Stop" in e and "不在任何讀取範圍內" in e for e in errors), errors


def test_position_split_across_two_ranges_is_reported(config_dir):
    # Current Position 1 占 HR 22~23
    text = FULL.replace('"20~83, 120~183"', '"20~22, 23~83, 120~183"')
    errors = errors_for(config_dir, text)
    assert any("Current Position 1" in e and "跨越讀取範圍邊界" in e for e in errors), errors


def test_other_unit_id_does_not_cover_points(config_dir):
    errors = errors_for(config_dir, FULL.replace("unit_id = 0", "unit_id = 1"))
    assert any("不在任何讀取範圍內" in e for e in errors), errors


def test_range_errors_are_listed_with_other_config_errors(config_dir):
    (config_dir / "device.toml").write_bytes(b"[connection]\n")
    errors = errors_for(config_dir, FULL.replace('"24576~24612"', '"x"'))
    assert any("device.toml" in e for e in errors)
    assert any("read_ranges.toml" in e for e in errors)


def test_unparsable_file_is_reported(config_dir):
    errors = errors_for(config_dir, "[[unit]\n")
    assert any("read_ranges.toml" in e and "格式無法解析" in e for e in errors), errors
