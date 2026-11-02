"""批次讀取分組（純函式）：合併連續位址、不跨空隙、不切開 Position 點。"""

from plc_motion.plc.batching import plan_reads
from plc_motion.settings import load_config
from plc_motion.settings.io_points import (
    Access,
    CheckStatus,
    DataModel,
    IoPoint,
    SensorType,
)

_COIL, _DI, _HR = DataModel.COIL, DataModel.DISCRETE_INPUT, DataModel.HOLDING_REGISTER


def pt(tag: str, address: int, model: DataModel = _HR, length: int = 1, unit_id: int = 0):
    sensor = (
        SensorType.POSITION
        if length == 2
        else SensorType.U16
        if model == _HR
        else SensorType.BOOLEAN
    )
    return IoPoint(
        tag=tag,
        display_name="",
        start_address=address,
        length=length,
        unit_id=unit_id,
        data_model=model,
        sensor_type=sensor,
        access=Access.READ_ONLY,
        visible=True,
        description="",
        attributes="10000,mm" if length == 2 else "",
        check_status=CheckStatus.UNCHECKED,
        line=0,
        scale=10000 if length == 2 else None,
        unit="mm" if length == 2 else None,
    )


def spans(blocks) -> list[tuple[int, str, int, int, list[str]]]:
    return [
        (b.unit_id, b.data_model.value, b.start, b.count, [p.tag for p in b.points]) for b in blocks
    ]


def test_contiguous_points_merge_into_one_block():
    blocks = plan_reads([pt("a", 20), pt("b", 21), pt("pos", 22, length=2)])
    assert spans(blocks) == [(0, "Holding Register", 20, 4, ["a", "b", "pos"])]


def test_gap_splits_blocks():
    blocks = plan_reads(
        [pt("r", 20, _COIL), pt("b", 21, _COIL), pt("h", 22, _COIL), pt("y", 24, _COIL)]
    )
    assert spans(blocks) == [
        (0, "Coil", 20, 3, ["r", "b", "h"]),
        (0, "Coil", 24, 1, ["y"]),
    ]


def test_order_in_input_does_not_matter():
    blocks = plan_reads([pt("c", 22, _COIL), pt("a", 20, _COIL), pt("b", 21, _COIL)])
    assert spans(blocks) == [(0, "Coil", 20, 3, ["a", "b", "c"])]


def test_different_data_model_or_unit_id_never_merge():
    blocks = plan_reads(
        [pt("hr", 20), pt("coil", 21, _COIL), pt("di", 22, _DI), pt("u1", 21, unit_id=1)]
    )
    assert sorted(spans(blocks)) == [
        (0, "Coil", 21, 1, ["coil"]),
        (0, "Discrete Input", 22, 1, ["di"]),
        (0, "Holding Register", 20, 1, ["hr"]),
        (1, "Holding Register", 21, 1, ["u1"]),
    ]


def test_size_limit_splits_without_cutting_position_point():
    points = [pt("a", 20), pt("pos", 21, length=2), pt("b", 23)]
    assert spans(plan_reads(points, max_registers=3)) == [
        (0, "Holding Register", 20, 3, ["a", "pos"]),
        (0, "Holding Register", 23, 1, ["b"]),
    ]
    assert spans(plan_reads(points, max_registers=2)) == [
        (0, "Holding Register", 20, 1, ["a"]),
        (0, "Holding Register", 21, 2, ["pos"]),
        (0, "Holding Register", 23, 1, ["b"]),
    ]


def test_bit_limit_applies_to_coils():
    points = [pt(f"c{i}", i, _COIL) for i in range(5)]
    assert [b.count for b in plan_reads(points, max_bits=2)] == [2, 2, 1]


def test_default_config_covers_every_point_once_and_no_undefined_address(config_dir):
    points = load_config(config_dir).points.all()
    blocks = plan_reads(points)

    tags = [p.tag for b in blocks for p in b.points]
    assert sorted(tags) == sorted(p.tag for p in points)
    assert len(blocks) < len(points)

    defined = {
        (p.unit_id, p.data_model, p.start_address + i) for p in points for i in range(p.length)
    }
    for b in blocks:
        for a in range(b.start, b.start + b.count):
            assert (b.unit_id, b.data_model, a) in defined
    # 第 1 軸 Coil 23 未定義：Ready~Find Home Finish 與 Reply 分在兩塊
    coil_starts = {b.start for b in blocks if b.data_model == _COIL}
    assert {20, 24} <= coil_starts


def test_ranges_make_one_block_per_range_including_gaps():
    from plc_motion.settings.read_ranges import ReadRange

    points = [pt("a", 20, _COIL), pt("b", 24, _COIL), pt("pos", 22, _HR, length=2), pt("c", 20)]
    ranges = [ReadRange(0, _HR, 20, 30), ReadRange(0, _COIL, 20, 24), ReadRange(0, _DI, 5, 9)]
    assert spans(plan_reads(points, ranges)) == [
        (0, "Holding Register", 20, 11, ["c", "pos"]),
        (0, "Coil", 20, 5, ["a", "b"]),
        (0, "Discrete Input", 5, 5, []),
    ]
