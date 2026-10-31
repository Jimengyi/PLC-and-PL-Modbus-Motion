import pytest

from plc_motion.codec import decimals_for, from_raw, join_i32, split_i32, to_raw


def test_minus_100_mm_splits_into_low_high_words():
    assert split_i32(to_raw(-100, 10000)) == (48576, 65520)


def test_join_i32_is_signed():
    assert join_i32(48576, 65520) == -1000000


# README「I32 拆分範例」七列：(目標值, 倍率, I32 值, low, high)
README_ROWS = [
    (1, 10000, 10000, 10000, 0),
    (0.5, 10000, 5000, 5000, 0),
    (-2.3456, 10000, -23456, 42080, 65535),
    (1, 100, 100, 100, 0),
    (-0.25, 100, -25, 65511, 65535),
    (10, 100, 1000, 1000, 0),
    (150.25, 100, 15025, 15025, 0),
]


@pytest.mark.parametrize(("value", "scale", "raw", "low", "high"), README_ROWS)
def test_readme_split_examples(value, scale, raw, low, high):
    assert to_raw(value, scale) == raw
    assert split_i32(raw) == (low, high)
    assert join_i32(low, high) == raw


@pytest.mark.parametrize(
    ("value", "scale", "raw"),
    [(7.5, 10000, 75000), (-2.5, 10000, -25000), (150.25, 100, 15025), (-2.3456, 10000, -23456)],
)
def test_to_raw_has_no_float_error(value, scale, raw):
    assert to_raw(value, scale) == raw


def test_to_raw_rounds_to_nearest():
    assert to_raw(0.00004, 10000) == 0
    assert to_raw(0.00006, 10000) == 1
    assert to_raw(-0.00006, 10000) == -1


@pytest.mark.parametrize("value", [214749.3648, -214749.3649, 1e12, 1e300, -1e30])
def test_to_raw_out_of_i32_range_raises(value):
    with pytest.raises(ValueError):
        to_raw(value, 10000)


def test_to_raw_i32_bounds_accepted():
    assert to_raw(214748.3647, 10000) == 2147483647
    assert to_raw(-214748.3648, 10000) == -2147483648


def test_from_raw():
    assert from_raw(-23456, 10000) == pytest.approx(-2.3456)
    assert from_raw(15025, 100) == pytest.approx(150.25)


def test_decimals_for():
    assert decimals_for(10000) == 4
    assert decimals_for(100) == 2
