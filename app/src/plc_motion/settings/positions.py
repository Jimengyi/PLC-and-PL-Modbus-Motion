"""點位資料表（positions.csv）：固定 P001 ~ P200，任何修改立即原子存檔。"""

import csv
import dataclasses
import io
import math
from dataclasses import dataclass
from pathlib import Path

from plc_motion.settings.csvio import decode_csv_bytes, encode_csv_text
from plc_motion.settings.paths import atomic_write_bytes

HEADER = ["#Point", "Name", "X", "Y", "Z", "RX", "RY", "RZ", "Lift", "Description"]
_COORD_FIELDS = ("x", "y", "z", "rx", "ry", "rz", "lift")
_FIELDS = ("name", *_COORD_FIELDS, "description")
POINT_COUNT = 200


@dataclass(frozen=True)
class PositionRow:
    point_id: str
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    lift: float = 0.0
    description: str = ""


def _point_id(n: int) -> str:
    return f"P{n:03d}"


def _fmt(v: float) -> str:
    s = repr(float(v))
    return s[:-2] if s.endswith(".0") else s


def _serialize(rows: list[PositionRow]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow(
            [r.point_id, r.name, *(_fmt(getattr(r, f)) for f in _COORD_FIELDS), r.description]
        )
    return encode_csv_text(buf.getvalue())


def _parse(data: bytes, label: str) -> tuple[list[PositionRow], list[str]]:
    """解析並驗證整份點位檔；回傳 (rows, errors)，有錯誤時 rows 為空。"""
    try:
        text = decode_csv_bytes(data)
    except ValueError as e:
        return [], [f"{label}：{e}"]
    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as e:
        return [], [f"{label}：無法解析 CSV（{e}）"]
    records = [r for r in records if any(c.strip() for c in r)]
    if not records:
        return [], [f"{label}：檔案是空的"]
    header = [c.strip() for c in records[0]]
    missing = [c for c in HEADER if c not in header]
    if missing:
        return [], [f"{label}：缺少欄位 {', '.join(missing)}"]
    col = {name: header.index(name) for name in HEADER}
    body = records[1:]
    errors: list[str] = []
    if len(body) != POINT_COUNT:
        errors.append(f"{label}：資料須恰好 {POINT_COUNT} 列，實際為 {len(body)} 列")
    rows: list[PositionRow] = []
    for i, rec in enumerate(body):
        line = i + 2
        if len(rec) < len(header):
            errors.append(f"{label} 第 {line} 列：欄位數不足（{len(rec)}／{len(header)}）")
            continue
        pid = rec[col["#Point"]].strip()
        expected = _point_id(i + 1)
        if pid != expected:
            errors.append(f"{label} 第 {line} 列：#Point 應為 {expected}，實際為「{pid}」")
            continue
        values: dict[str, float] = {}
        for f, h in zip(_COORD_FIELDS, HEADER[2:9], strict=True):
            raw = rec[col[h]].strip()
            try:
                v = float(raw)
                if not math.isfinite(v):
                    raise ValueError
            except ValueError:
                errors.append(f"{label} 第 {line} 列：{h} 不是數字（「{raw}」）")
                break
            values[f] = v
        else:
            rows.append(
                PositionRow(
                    point_id=pid,
                    name=rec[col["Name"]],
                    description=rec[col["Description"]],
                    **values,
                )
            )
    if errors:
        return [], errors
    return rows, []


class PositionTable:
    POINT_COUNT = POINT_COUNT

    def __init__(self, path: Path, rows: list[PositionRow]):
        self._path = path
        self._rows = list(rows)

    @property
    def path(self) -> Path:
        return self._path

    def rows(self) -> list[PositionRow]:
        return list(self._rows)

    def update(self, point_id: str, **fields) -> None:
        idx = next((i for i, r in enumerate(self._rows) if r.point_id == point_id), None)
        if idx is None:
            raise KeyError(point_id)
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError(f"未知欄位：{', '.join(sorted(unknown))}")
        clean = dict(fields)
        for f in _COORD_FIELDS:
            if f in clean:
                v = float(clean[f])
                if not math.isfinite(v):
                    raise ValueError(f"{f} 必須是有限數字")
                clean[f] = v
        new_rows = list(self._rows)
        new_rows[idx] = dataclasses.replace(self._rows[idx], **clean)
        atomic_write_bytes(self._path, _serialize(new_rows))
        self._rows = new_rows

    def import_file(self, path: Path) -> list[str]:
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            return [f"無法讀取 {Path(path).name}：{e}"]
        rows, errors = _parse(data, Path(path).name)
        if errors:
            return errors
        try:
            atomic_write_bytes(self._path, _serialize(rows))
        except OSError as e:
            return [f"寫入 {self._path.name} 失敗：{e}"]
        self._rows = rows
        return []

    def export_file(self, path: Path) -> None:
        Path(path).write_bytes(_serialize(self._rows))


def load_positions(path: Path) -> tuple[PositionTable | None, list[str]]:
    """positions.csv 不存在時建立 200 個空白點；存在但無效則回報錯誤。"""
    if not path.exists():
        rows = [PositionRow(_point_id(i)) for i in range(1, POINT_COUNT + 1)]
        try:
            atomic_write_bytes(path, _serialize(rows))
        except OSError as e:
            return None, [f"無法建立 {path.name}：{e}"]
        return PositionTable(path, rows), []
    try:
        data = path.read_bytes()
    except OSError as e:
        return None, [f"無法讀取 {path.name}：{e}"]
    rows, errors = _parse(data, path.name)
    if errors:
        return None, errors
    return PositionTable(path, rows), []
