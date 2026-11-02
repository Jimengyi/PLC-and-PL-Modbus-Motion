"""read_ranges.toml：批次讀取範圍（spec FR-61）。

檔案格式（每個 [[unit]] 一個 Unit ID）：

    [[unit]]
    unit_id = 0
    coil = "20~84, 120~184"
    holding_register = "20~83, 120~183"
    discrete_input = "24576~24612"

- 範圍寫「起~迄」（含兩端），只有一個位址時可寫單一數字；多段用逗號分隔。
- 某種 Data Model 留空或不寫，表示這種 Data Model 不讀。
- 檔案不存在、或沒有任何 [[unit]]：不使用本檔，改由 `plc.batching` 依位址連續自動分組。

本檔存在時，每一輪完全照這裡的範圍讀取，每段一筆請求。範圍內 io_points 沒定義的位址
也會讀（值不使用），所以 PLC 必須允許讀這些位址。
"""

from dataclasses import dataclass
from pathlib import Path

from plc_motion.settings.device import load_toml
from plc_motion.settings.io_points import DataModel, IoPoint

FILE_NAME = "read_ranges.toml"

# Modbus 單筆讀取上限：FC 1/2 為 2000 個 bit，FC 3 為 125 個 Word
MAX_BITS = 2000
MAX_REGISTERS = 125
MAX_ADDRESS = 65535

# 設定鍵 → Data Model
_KEYS = {
    "coil": DataModel.COIL,
    "discrete_input": DataModel.DISCRETE_INPUT,
    "holding_register": DataModel.HOLDING_REGISTER,
}


def read_limit(model: DataModel) -> int:
    return MAX_REGISTERS if model == DataModel.HOLDING_REGISTER else MAX_BITS


@dataclass(frozen=True)
class ReadRange:
    unit_id: int
    data_model: DataModel
    start: int
    end: int  # 含

    @property
    def count(self) -> int:
        return self.end - self.start + 1

    def contains(self, p: IoPoint) -> bool:
        return self.start <= p.start_address and p.end_address <= self.end

    def overlaps(self, start: int, end: int) -> bool:
        return start <= self.end and self.start <= end

    @property
    def text(self) -> str:
        return f"Unit ID {self.unit_id} {self.data_model} {self.start}~{self.end}"


def load_read_ranges(path: Path) -> tuple[tuple[ReadRange, ...] | None, list[str]]:
    """回傳 (範圍, 錯誤)；檔案不存在或沒有 [[unit]] 時範圍為 None（改用自動分組）。"""
    if not path.is_file():
        return None, []
    data = load_toml(path)
    if isinstance(data, list):
        return None, data
    return parse_read_ranges(data)


def parse_read_ranges(data: dict) -> tuple[tuple[ReadRange, ...] | None, list[str]]:
    errors: list[str] = []
    errors += [f"{FILE_NAME}：不認得的設定「{k}」（只接受 [[unit]]）" for k in data if k != "unit"]
    units = data.get("unit", [])
    if not isinstance(units, list) or not all(isinstance(u, dict) for u in units):
        return None, [*errors, f"{FILE_NAME}：unit 必須寫成 [[unit]] 段落"]

    ranges: list[ReadRange] = []
    seen_units: set[int] = set()
    for i, unit in enumerate(units, start=1):
        where = f"{FILE_NAME} 第 {i} 個 [[unit]]"
        unit_id = unit.get("unit_id")
        if not (isinstance(unit_id, int) and not isinstance(unit_id, bool) and unit_id >= 0):
            errors.append(f"{where}：unit_id 必須是非負整數，實際為 {unit_id!r}")
            continue
        if unit_id in seen_units:
            errors.append(f"{where}：unit_id {unit_id} 重複")
            continue
        seen_units.add(unit_id)
        where = f"{FILE_NAME} unit_id {unit_id}"
        for key in unit:
            if key == "unit_id" or key in _KEYS:
                continue
            hint = "（io_points 沒有 Input Register）" if key == "input_register" else ""
            errors.append(f"{where}：不認得的設定「{key}」{hint}")
        for key, model in _KEYS.items():
            text = unit.get(key, "")
            if not isinstance(text, str):
                errors.append(f'{where}：{key} 必須是字串，例如 "20~84, 120~184"')
                continue
            parsed, errs = _parse_spans(text, f"{where} {key}", limit=read_limit(model))
            errors += errs
            ranges += [ReadRange(unit_id, model, s, e) for s, e in parsed]

    errors += _overlap_errors(ranges)
    if errors:
        return None, errors
    if not units:
        return None, []
    return tuple(ranges), []


def _parse_spans(text: str, where: str, limit: int) -> tuple[list[tuple[int, int]], list[str]]:
    spans, errors = [], []
    for piece in (s.strip() for s in text.split(",")):
        if not piece:
            continue
        parts = [s.strip() for s in piece.split("~")]
        if len(parts) == 1:
            parts *= 2
        if len(parts) != 2 or not all(s.isdigit() for s in parts):
            errors.append(f"{where}：「{piece}」格式不對，應寫成「起~迄」，例如 20~84")
            continue
        start, end = int(parts[0]), int(parts[1])
        if end > MAX_ADDRESS:
            errors.append(f"{where}：「{piece}」超出位址上限 {MAX_ADDRESS}")
        elif start > end:
            errors.append(f"{where}：「{piece}」起始位址大於結束位址")
        elif end - start + 1 > limit:
            errors.append(
                f"{where}：「{piece}」共 {end - start + 1} 個位址，超過單筆讀取上限 {limit}"
            )
        else:
            spans.append((start, end))
    return spans, errors


def _overlap_errors(ranges: list[ReadRange]) -> list[str]:
    errors = []
    for i, a in enumerate(ranges):
        for b in ranges[i + 1 :]:
            same = (a.unit_id, a.data_model) == (b.unit_id, b.data_model)
            if same and a.overlaps(b.start, b.end):
                errors.append(f"{FILE_NAME}：{a.text} 與 {b.start}~{b.end} 重疊")
    return errors


def check_coverage(ranges: tuple[ReadRange, ...], points: list[IoPoint]) -> list[str]:
    """每個點都要完整落在某一段範圍內；Position 的兩個 Word 不能跨兩段。"""
    errors = []
    for p in points:
        same = [r for r in ranges if (r.unit_id, r.data_model) == (p.unit_id, p.data_model)]
        if any(r.contains(p) for r in same):
            continue
        where = (
            f"{FILE_NAME}：io_points.csv 第 {p.line} 列「{p.tag}」"
            f"（Unit ID {p.unit_id} {p.data_model} {_span(p)}）"
        )
        if any(r.overlaps(p.start_address, p.end_address) for r in same):
            errors.append(f"{where}跨越讀取範圍邊界，必須整個落在同一段範圍內")
        else:
            errors.append(f"{where}不在任何讀取範圍內")
    return errors


def _span(p: IoPoint) -> str:
    return str(p.start_address) if p.length == 1 else f"{p.start_address}~{p.end_address}"
