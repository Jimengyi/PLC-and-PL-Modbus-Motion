"""批次讀取分組：把 io_points 合併成數筆 Modbus 讀取請求。

同一 (Unit ID, Data Model) 內位址相連的點合併為一塊；位址有空隙就分塊，
因此不會讀到 io_points 沒定義的位址（PLC 對表外位址可能回 Illegal Data Address）。
一塊的長度不超過 Modbus 單筆讀取上限，且不把同一點（Position 的兩個 Word）切到兩塊。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from plc_motion.settings.io_points import DataModel, IoPoint

# Modbus 單筆讀取上限：FC 1/2 為 2000 個 bit，FC 3 為 125 個 Word
MAX_BITS = 2000
MAX_REGISTERS = 125


@dataclass(frozen=True)
class ReadBlock:
    unit_id: int
    data_model: DataModel
    start: int
    count: int
    points: tuple[IoPoint, ...]  # 依位址排序


def plan_reads(
    points: Iterable[IoPoint],
    max_bits: int = MAX_BITS,
    max_registers: int = MAX_REGISTERS,
) -> list[ReadBlock]:
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


def _block(unit_id: int, model: DataModel, points: list[IoPoint]) -> ReadBlock:
    start = points[0].start_address
    return ReadBlock(unit_id, model, start, points[-1].end_address - start + 1, tuple(points))
