"""io_points.csv：IO 點表的解析、FR-8 啟動檢查與 Check Status 寫回。"""

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from plc_motion.settings.csvio import (
    CsvFormatError,
    CsvRecord,
    decode_csv_bytes,
    encode_csv_text,
    parse_csv,
    replace_spans,
)
from plc_motion.settings.paths import atomic_write_bytes

FILE_NAME = "io_points.csv"


class DataModel(StrEnum):
    COIL = "Coil"
    DISCRETE_INPUT = "Discrete Input"
    HOLDING_REGISTER = "Holding Register"


class SensorType(StrEnum):
    BOOLEAN = "Boolean"
    U16 = "U16"
    POSITION = "Position"


class Access(StrEnum):
    READ_ONLY = "Read Only"
    READ_WRITE = "Read Write"


class CheckStatus(StrEnum):
    OK = "OK"
    NG = "NG"
    UNCHECKED = "Unchecked"
    RECHECK = "Recheck"
    IGNORE = "Ignore"


VISIBLE_VALUES = ("O", "X")
B_CONTACT = "ON"

COLUMNS = (
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
    "Check Status",
)

# FR-8 型態表：(Data Model, Sensor Type, Access, Address Length, Attributes 規則)
# Attributes 規則："" 須空白、"ON" 須為 ON、"scale" 須為「倍率,單位」
_RO, _RW = Access.READ_ONLY, Access.READ_WRITE
_COIL, _DI, _HR = DataModel.COIL, DataModel.DISCRETE_INPUT, DataModel.HOLDING_REGISTER
_BOOL, _U16, _POS = SensorType.BOOLEAN, SensorType.U16, SensorType.POSITION
AXIS_TAG_TYPES: dict[str, tuple[DataModel, SensorType, Access, int, str]] = {
    "Ready": (_COIL, _BOOL, _RO, 1, ""),
    "Busy": (_COIL, _BOOL, _RO, 1, ""),
    "Find Home Finish": (_COIL, _BOOL, _RO, 1, ""),
    "Reply": (_COIL, _BOOL, _RO, 1, ""),
    "Alarm Code": (_HR, _U16, _RO, 1, ""),
    "PLC Action Code": (_HR, _U16, _RO, 1, ""),
    "Current Position": (_HR, _POS, _RO, 2, "scale"),
    "Positive Limit": (_DI, _BOOL, _RO, 1, "ON"),
    "Negative Limit": (_DI, _BOOL, _RO, 1, "ON"),
    "Home Sensor": (_DI, _BOOL, _RO, 1, ""),
    "Servo Motor": (_COIL, _BOOL, _RW, 1, ""),
    "Request": (_COIL, _BOOL, _RW, 1, ""),
    "Reset Alarm": (_COIL, _BOOL, _RW, 1, ""),
    "JOG Plus": (_COIL, _BOOL, _RW, 1, ""),
    "JOG Minus": (_COIL, _BOOL, _RW, 1, ""),
    "Speed": (_HR, _U16, _RW, 1, ""),
    "PC Action Code": (_HR, _U16, _RW, 1, ""),
    "Target Position": (_HR, _POS, _RW, 2, "scale"),
}
EMERGENCY_STOP = "Emergency Stop"
EMERGENCY_STOP_TYPE = (_DI, _BOOL, _RO, 1, "ON")
AXIS_COUNT = 7


def required_tag_types() -> dict[str, tuple[DataModel, SensorType, Access, int, str]]:
    """FR-5 的全部必要 Tag 與其 FR-8 型態。"""
    out = {
        f"{name} {axis}": spec
        for axis in range(1, AXIS_COUNT + 1)
        for name, spec in AXIS_TAG_TYPES.items()
    }
    out[EMERGENCY_STOP] = EMERGENCY_STOP_TYPE
    return out


@dataclass(frozen=True)
class IoPoint:
    tag: str
    display_name: str
    start_address: int
    length: int
    unit_id: int
    data_model: DataModel
    sensor_type: SensorType
    access: Access
    visible: bool
    description: str
    attributes: str
    check_status: CheckStatus
    line: int  # 在 io_points.csv 的列號（表頭為第 1 列）
    scale: int | None = None  # Position 才有
    unit: str | None = None  # Position 才有

    @property
    def label(self) -> str:
        """畫面顯示名稱：Display Name 空白時用 Tag Name。"""
        return self.display_name or self.tag

    @property
    def is_b_contact(self) -> bool:
        return self.sensor_type == SensorType.BOOLEAN and self.attributes == B_CONTACT

    @property
    def end_address(self) -> int:
        return self.start_address + self.length - 1


def parse_position_attributes(attributes: str) -> tuple[int, str] | None:
    """「倍率,單位」→ (倍率, 單位)；格式不符回傳 None。"""
    parts = attributes.split(",")
    if len(parts) != 2:
        return None
    scale_text, unit = parts[0].strip(), parts[1].strip()
    if not scale_text.isdigit() or int(scale_text) <= 0 or not unit:
        return None
    return int(scale_text), unit


class IoPointTable:
    def __init__(self, path: Path, text: str, records: list[CsvRecord], points: list[IoPoint]):
        self._path = path
        self._text = text
        self._records = records  # 與 points 一一對應（資料列，不含表頭）
        self._points = points
        self._index = {p.tag: i for i, p in enumerate(points)}
        self._status_col = _status_column(text)

    @property
    def path(self) -> Path:
        return self._path

    def get(self, tag: str) -> IoPoint:
        return self._points[self._index[tag]]

    def axis_tag(self, name: str, axis: int) -> IoPoint:
        return self.get(f"{name} {axis}")

    def all(self) -> list[IoPoint]:
        return list(self._points)

    def save_check_status(self, tag: str, status: str) -> None:
        """只改該列的 Check Status，其餘文字與列順序不變；檔案以 UTF-8 含 BOM 原子替換。"""
        new_status = CheckStatus(status)
        self._write({self._index[tag]: new_status})

    def reset_all_check_status(self) -> None:
        self._write({i: CheckStatus.UNCHECKED for i in range(len(self._points))})

    def _write(self, changes: dict[int, CheckStatus]) -> None:
        col = self._status_col
        replacements = [
            (self._records[i].spans[col], str(status))
            for i, status in changes.items()
            if self._points[i].check_status != status
        ]
        text = replace_spans(self._text, replacements)
        atomic_write_bytes(self._path, encode_csv_text(text))
        # 寫入成功後才更新記憶體內容
        records = parse_csv(text)[1:]
        self._text = text
        self._records = records
        for i, status in changes.items():
            self._points[i] = dataclasses.replace(self._points[i], check_status=status)


def _status_column(text: str) -> int:
    header = [v.strip() for v in parse_csv(text)[0].values]
    return header.index("Check Status")


def _parse_row(record: CsvRecord, col: dict[str, int]) -> tuple[IoPoint | None, list[str]]:
    def value(name: str) -> str:
        return record.values[col[name]].strip()

    tag = value("Tag Name")
    where = f"{FILE_NAME} 第 {record.line} 列（{tag}）"
    errors: list[str] = []

    ints: dict[str, int] = {}
    for name in ("Start Address", "Address Length", "Unit ID"):
        v = value(name)
        if not v.isdigit():
            errors.append(f"{where}：{name}「{v}」須為非負整數")
        else:
            ints[name] = int(v)
    if ints.get("Address Length") == 0:
        errors.append(f"{where}：Address Length 須大於 0")

    enums = {}
    for name, enum, allowed in (
        ("Data Model", DataModel, None),
        ("Sensor Type", SensorType, None),
        ("Access", Access, None),
        ("Check Status", CheckStatus, None),
        ("Visible", None, VISIBLE_VALUES),
    ):
        v = value(name)
        choices = allowed if enum is None else tuple(e.value for e in enum)
        if v not in choices:
            errors.append(f"{where}：{name}「{v}」不是 {'、'.join(choices)} 之一")
        else:
            enums[name] = v if enum is None else enum(v)

    attributes = value("Attributes")
    scale = unit = None
    if enums.get("Sensor Type") == SensorType.POSITION:
        parsed = parse_position_attributes(attributes)
        if parsed is None:
            errors.append(
                f"{where}：Position 的 Attributes「{attributes}」"
                "應為「倍率,單位」格式（如 10000,mm）"
            )
        else:
            scale, unit = parsed

    if errors:
        return None, errors
    return (
        IoPoint(
            tag=tag,
            display_name=value("Display Name"),
            start_address=ints["Start Address"],
            length=ints["Address Length"],
            unit_id=ints["Unit ID"],
            data_model=enums["Data Model"],
            sensor_type=enums["Sensor Type"],
            access=enums["Access"],
            visible=enums["Visible"] == "O",
            description=record.values[col["Description"]],
            attributes=attributes,
            check_status=enums["Check Status"],
            line=record.line,
            scale=scale,
            unit=unit,
        ),
        [],
    )


def _check_required(points: dict[str, IoPoint], invalid_tags: set[str]) -> list[str]:
    errors: list[str] = []
    for tag, (model, sensor, access, length, attr_rule) in required_tag_types().items():
        p = points.get(tag)
        if p is None:
            if tag not in invalid_tags:
                errors.append(f"{FILE_NAME}：缺少必要 Tag「{tag}」")
            continue
        where = f"{FILE_NAME} 第 {p.line} 列（{tag}）"
        for name, actual, expected in (
            ("Data Model", p.data_model, model),
            ("Sensor Type", p.sensor_type, sensor),
            ("Access", p.access, access),
            ("Address Length", p.length, length),
        ):
            if actual != expected:
                errors.append(f"{where}：{name} 應為「{expected}」，實際為「{actual}」")
        if attr_rule == "" and p.attributes != "":
            errors.append(f"{where}：Attributes 應為空白，實際為「{p.attributes}」")
        elif attr_rule == "ON" and p.attributes != B_CONTACT:
            errors.append(f"{where}：Attributes 應為「ON」（B 接點），實際為「{p.attributes}」")
        elif attr_rule == "scale" and parse_position_attributes(p.attributes) is None:
            errors.append(f"{where}：Attributes 應為「倍率,單位」，實際為「{p.attributes}」")
    return errors


def _check_position_pairs(points: dict[str, IoPoint]) -> list[str]:
    errors: list[str] = []
    for axis in range(1, AXIS_COUNT + 1):
        cur = points.get(f"Current Position {axis}")
        tgt = points.get(f"Target Position {axis}")
        if cur is None or tgt is None or cur.scale is None or tgt.scale is None:
            continue
        if (cur.scale, cur.unit) != (tgt.scale, tgt.unit):
            errors.append(
                f"{FILE_NAME}：「{cur.tag}」（{cur.attributes}）與「{tgt.tag}」（{tgt.attributes}）"
                "的倍率或單位不一致"
            )
    return errors


def _check_overlap(points: list[IoPoint]) -> list[str]:
    errors: list[str] = []
    groups: dict[tuple[int, DataModel], list[IoPoint]] = {}
    for p in points:
        groups.setdefault((p.unit_id, p.data_model), []).append(p)
    for (unit_id, model), group in groups.items():
        group.sort(key=lambda p: (p.start_address, p.line))
        widest: IoPoint | None = None
        for p in group:
            if widest is not None and p.start_address <= widest.end_address:
                errors.append(
                    f"{FILE_NAME}：Unit ID {unit_id} 的 {model} 位址重疊："
                    f"「{widest.tag}」（{widest.start_address} ~ {widest.end_address}）與"
                    f"「{p.tag}」（{p.start_address} ~ {p.end_address}）"
                )
            if widest is None or p.end_address > widest.end_address:
                widest = p
    return errors


def load_io_points(path: Path) -> tuple[IoPointTable | None, list[str]]:
    if not path.is_file():
        return None, [f"缺少設定檔 {path.name}"]
    try:
        text = decode_csv_bytes(path.read_bytes())
        records = parse_csv(text)
    except CsvFormatError as e:
        return None, [f"{FILE_NAME}：格式無法解析（第 {e.line} 列{e.reason}）"]
    except ValueError as e:
        return None, [f"{FILE_NAME}：格式無法解析（{e}）"]
    if not records:
        return None, [f"{FILE_NAME}：格式無法解析（檔案是空的）"]

    header = [v.strip() for v in records[0].values]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        return None, [f"{FILE_NAME}：表頭缺少欄位 {'、'.join(missing)}"]
    col = {c: header.index(c) for c in COLUMNS}

    errors: list[str] = []
    points: list[IoPoint] = []
    data_records: list[CsvRecord] = []
    first_line: dict[str, int] = {}
    invalid_tags: set[str] = set()
    for record in records[1:]:
        if len(record.values) < len(header):
            tag = record.values[0].strip()
            errors.append(f"{FILE_NAME} 第 {record.line} 列（{tag}）：欄位數不足")
            invalid_tags.add(tag)
            continue
        tag = record.values[col["Tag Name"]].strip()
        if not tag:
            errors.append(f"{FILE_NAME} 第 {record.line} 列：Tag Name 空白")
            continue
        if tag in first_line:
            errors.append(
                f"{FILE_NAME} 第 {record.line} 列：Tag Name「{tag}」與第 {first_line[tag]} 列重複"
            )
            continue
        first_line[tag] = record.line
        point, row_errors = _parse_row(record, col)
        if point is None:
            errors += row_errors
            invalid_tags.add(tag)
            continue
        points.append(point)
        data_records.append(record)

    by_tag = {p.tag: p for p in points}
    errors += _check_required(by_tag, invalid_tags)
    errors += _check_position_pairs(by_tag)
    errors += _check_overlap(points)
    if errors:
        return None, errors
    return IoPointTable(path, text, data_records, points), []
