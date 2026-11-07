"""批次讀取分組：把 io_points 合併成數筆 Modbus 讀取請求。

有 read_ranges.toml 時（spec FR-61）：完全照設定的範圍分組，每段一筆請求；範圍內
io_points 沒定義的位址也一起讀。設定載入時已檢查每個點都完整落在某一段範圍內。

沒有 read_ranges.toml 時自動分組：同一 (Unit ID, Data Model) 內位址相連的點合併為一塊；
位址有空隙就分塊，因此不會讀到 io_points 沒定義的位址（PLC 對表外位址可能回
Illegal Data Address）。一塊的長度不超過 Modbus 單筆讀取上限，且不把同一點
（Position 的兩個 Word）切到兩塊。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from lib.modbus_tags import DataModel
from lib.modbus_tags.config import MAX_READ_BITS, MAX_READ_REGISTERS, ReadRange
from plc_motion.settings.io_points import IoPoint

# Modbus 單筆讀取上限：FC 1/2 為 2000 個 bit，FC 3 為 125 個 Word
MAX_BITS = MAX_READ_BITS
MAX_REGISTERS = MAX_READ_REGISTERS

__all__ = ["MAX_BITS", "MAX_REGISTERS", "ReadBlock", "plan_reads"]


@dataclass(frozen=True)
class ReadBlock:
    unit_id: int
    data_model: DataModel
    start: int
    count: int
    points: tuple[IoPoint, ...]  # 依位址排序


def plan_reads(
    points: Iterable[IoPoint],
    ranges: Iterable[ReadRange] | None = None,
    max_bits: int = MAX_BITS,
    max_registers: int = MAX_REGISTERS,
) -> list[ReadBlock]:
    if ranges is not None:
        return _from_ranges(list(points), ranges)

    groups: dict[tuple[int, DataModel], list[IoPoint]] = {}
    for p in points:
        groups.setdefault((p.unit_id, p.data_model), []).append(p)

    blocks: list[ReadBlock] = []
    for (unit_id, model), group in groups.items():
        limit = max_registers if model == DataModel.HOLDING_REGISTER else max_bits
        group.sort(key=lambda p: p.start_address)
        current: list[IoPoint] = []
        for p in group:
            if current:
                start = current[0].start_address
                contiguous = p.start_address == current[-1].end_address + 1
                fits = p.end_address - start + 1 <= limit
                if not (contiguous and fits):
                    blocks.append(_block(unit_id, model, current))
                    current = []
            current.append(p)
        if current:
            blocks.append(_block(unit_id, model, current))
    return blocks


def _from_ranges(points: list[IoPoint], ranges: Iterable[ReadRange]) -> list[ReadBlock]:
    """每段範圍一塊，依設定檔的順序；範圍內沒有點的段落照樣讀。"""
    blocks = []
    for r in ranges:
        inside = sorted(
            (
                p
                for p in points
                if (p.unit_id, p.data_model) == (r.unit_id, r.data_model)
                and r.start <= p.start_address
                and p.end_address < r.start + r.count
            ),
            key=lambda p: p.start_address,
        )
        blocks.append(ReadBlock(r.unit_id, r.data_model, r.start, r.count, tuple(inside)))
    return blocks


def _block(unit_id: int, model: DataModel, points: list[IoPoint]) -> ReadBlock:
    start = points[0].start_address
    return ReadBlock(unit_id, model, start, points[-1].end_address - start + 1, tuple(points))
