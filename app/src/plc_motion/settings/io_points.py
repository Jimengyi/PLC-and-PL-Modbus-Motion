"""io_points.csv：點位表、第二層啟動檢查（spec FR-11）與 Check Status 寫回（FR-10）。

檔案格式與第一層檢查（欄位、型別、位址、Attributes 語法）由 modbus_tags 負責；這裡以函式庫
解析出的 `TagInfo` 建立 `IoPoint`，再檢查 app 自己的規則：

- `Visible`、`Check Status` 欄位與值
- 必要 Tag（主規格 FR-5）的 Data Model、Data Type、Access、Address Length（FR-8 型態表）
- 必要 Tag 的 Attributes：Position 須有正整數 `scale` 與 `unit`；極限、原點、急停可設 `invert`；
  其餘屬性只能是預設值
- 同一軸 Current／Target Position 的倍率與單位一致
- 同一 Unit ID、同一 Data Model 的位址不重疊（同一暫存器的不同位元不算）

列號與 Check Status 寫回用的欄位位置，來自本模組自己對同一份 CSV 的解析（`csvio`），
寫回時只替換該欄文字。
"""

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from lib.modbus_tags import Access, DataModel, TagInfo
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


class CheckStatus(StrEnum):
    OK = "OK"
    NG = "NG"
    UNCHECKED = "Unchecked"
    RECHECK = "Recheck"
    IGNORE = "Ignore"


VISIBLE_VALUES = ("O", "X")
APP_COLUMNS = ("Visible", "Check Status")

_MODEL_NAMES = {
    DataModel.COIL: "Coil",
    DataModel.DISCRETE_INPUT: "Discrete Input",
    DataModel.HOLDING_REGISTER: "Holding Register",
    DataModel.INPUT_REGISTER: "Input Register",
}
_ACCESS_NAMES = {Access.READ_ONLY: "Read Only", Access.READ_WRITE: "Read Write"}


def model_name(model: DataModel) -> str:
    """io_points.csv 的寫法：Coil、Discrete Input、Holding Register、Input Register。"""
    return _MODEL_NAMES[model]


def _name(value: object) -> str:
    if isinstance(value, DataModel):
        return _MODEL_NAMES[value]
    if isinstance(value, Access):
        return _ACCESS_NAMES[value]
    return str(value)


# FR-8 型態表：(Data Model, Data Type, Access, Address Length, Attributes 規則)
# Attributes 規則："" 不可設定任何屬性、"invert" 可設 invert、"scale" 須有正整數 scale 與 unit
_RO, _RW = Access.READ_ONLY, Access.READ_WRITE
_COIL, _DI, _HR = DataModel.COIL, DataModel.DISCRETE_INPUT, DataModel.HOLDING_REGISTER
TagType = tuple[DataModel, str, Access, int, str]
AXIS_TAG_TYPES: dict[str, TagType] = {
    "Ready": (_COIL, "Bool", _RO, 1, ""),
    "Busy": (_COIL, "Bool", _RO, 1, ""),
    "Find Home Finish": (_COIL, "Bool", _RO, 1, ""),
    "Reply": (_COIL, "Bool", _RO, 1, ""),
    "Alarm Code": (_HR, "U16", _RO, 1, ""),
    "PLC Action Code": (_HR, "U16", _RO, 1, ""),
    "Current Position": (_HR, "I32", _RO, 2, "scale"),
    "Positive Limit": (_DI, "Bool", _RO, 1, "invert"),
    "Negative Limit": (_DI, "Bool", _RO, 1, "invert"),
    "Home Sensor": (_DI, "Bool", _RO, 1, "invert"),
    "Servo Motor": (_COIL, "Bool", _RW, 1, ""),
    "Request": (_COIL, "Bool", _RW, 1, ""),
    "Reset Alarm": (_COIL, "Bool", _RW, 1, ""),
    "JOG Plus": (_COIL, "Bool", _RW, 1, ""),
    "JOG Minus": (_COIL, "Bool", _RW, 1, ""),
    "Speed": (_HR, "U16", _RW, 1, ""),
    "PC Action Code": (_HR, "U16", _RW, 1, ""),
    "Target Position": (_HR, "I32", _RW, 2, "scale"),
}
EMERGENCY_STOP = "Emergency Stop"
EMERGENCY_STOP_TYPE: TagType = (_DI, "Bool", _RO, 1, "invert")
AXIS_COUNT = 7


def required_tag_types() -> dict[str, TagType]:
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
    data_type: str  # 函式庫的 Data Type：Bool、U16、I16、U32、I32、F32、String
    access: Access
    visible: bool
    description: str
    check_status: CheckStatus
    line: int  # 在 io_points.csv 的列號（表頭為第 1 列）
    scale: int | float | None = None  # 必要 Tag 為 int；整數值的倍率一律轉成 int
    unit: str | None = None
    bit: int | None = None  # 暫存器位元點（N.bK）的 K
    invert: bool = False  # B 接點
    word_order: str | None = None
    byte_swap: bool = False

    @property
    def label(self) -> str:
        """畫面顯示名稱：Display Name 空白時用 Tag Name。"""
        return self.display_name or self.tag

    @property
    def end_address(self) -> int:
        return self.start_address + self.length - 1


def is_position(point: IoPoint) -> bool:
    """Position 點：有倍率的 I32（必要 Tag 的 Current／Target Position 即是）。"""
    return point.data_type == "I32" and point.scale is not None


class IoPointTable:
    def __init__(
        self,
        path: Path,
        text: str,
        records: list[CsvRecord],
        points: list[IoPoint],
        columns: tuple[int, int],
    ):
        self._path = path
        self._text = text
        self._records = records  # 與 points 一一對應
        self._points = points
        self._index = {p.tag: i for i, p in enumerate(points)}
        self._tag_col, self._status_col = columns  # Tag Name、Check Status 的欄位索引

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
        # 寫入成功後才更新記憶體內容；替換後各列的欄位位置會移動，以 Tag Name 重新對應
        by_tag = _records_by_tag(parse_csv(text)[1:], self._tag_col)
        self._text = text
        self._records = [by_tag[p.tag] for p in self._points]
        for i, status in changes.items():
            self._points[i] = dataclasses.replace(self._points[i], check_status=status)


def _records_by_tag(records: list[CsvRecord], tag_col: int) -> dict[str, CsvRecord]:
    out: dict[str, CsvRecord] = {}
    for r in records:
        if tag_col < len(r.values):
            out.setdefault(r.values[tag_col].strip(), r)
    return out


def _scale(value: float | None) -> int | float | None:
    if value is not None and float(value).is_integer():
        return int(value)
    return value


def _where(p: IoPoint) -> str:
    return f"{FILE_NAME} 第 {p.line} 列（{p.tag}）"


def _point(tag: TagInfo, line: int) -> tuple[IoPoint | None, list[str]]:
    where = f"{FILE_NAME} 第 {line} 列（{tag.name}）"
    errors: list[str] = []
    visible = tag.extra.get("Visible", "")
    if visible not in VISIBLE_VALUES:
        errors.append(f"{where}：Visible「{visible}」不是 {'、'.join(VISIBLE_VALUES)} 之一")
    status = tag.extra.get("Check Status", "")
    choices = tuple(s.value for s in CheckStatus)
    if status not in choices:
        errors.append(f"{where}：Check Status「{status}」不是 {'、'.join(choices)} 之一")
    if errors:
        return None, errors
    return (
        IoPoint(
            tag=tag.name,
            display_name=tag.display_name,
            start_address=tag.address,
            length=tag.length,
            unit_id=tag.unit_id,
            data_model=tag.data_model,
            data_type=tag.data_type,
            access=tag.access,
            visible=visible == "O",
            description=tag.description,
            check_status=CheckStatus(status),
            line=line,
            scale=_scale(tag.scale),
            unit=tag.unit or None,
            bit=tag.bit,
            invert=tag.invert,
            word_order=tag.word_order,
            byte_swap=tag.byte_swap,
        ),
        [],
    )


def _scale_text(scale: float) -> str:
    return str(_scale(scale))


def _check_attributes(p: IoPoint, rule: str) -> list[str]:
    errors: list[str] = []
    where = _where(p)
    extra: list[str] = []
    if rule == "scale":
        if p.scale is None:
            errors.append(f"{where}：Attributes 缺少 scale（倍率）")
        elif not isinstance(p.scale, int):
            errors.append(f"{where}：Attributes 的 scale={p.scale} 不合法，倍率須為正整數")
        if not p.unit:
            errors.append(f"{where}：Attributes 缺少 unit（單位）")
    else:
        if p.scale is not None:
            extra.append(f"scale={_scale_text(p.scale)}")
        if p.unit:
            extra.append(f"unit={p.unit}")
    if rule != "invert" and p.invert:
        extra.append("invert=true")
    if p.word_order == "high_first":
        extra.append("word_order=high_first")
    if p.byte_swap:
        extra.append("byte_swap=true")
    if extra:
        allowed = {
            "scale": "只允許 scale、unit",
            "invert": "只允許 invert",
            "": "不可設定屬性",
        }[rule]
        errors.append(f"{where}：Attributes 不可設定 {'、'.join(extra)}（此必要 Tag {allowed}）")
    return errors


def _check_required(points: dict[str, IoPoint], invalid_tags: set[str]) -> list[str]:
    errors: list[str] = []
    for tag, (model, data_type, access, length, rule) in required_tag_types().items():
        p = points.get(tag)
        if p is None:
            if tag not in invalid_tags:
                errors.append(f"{FILE_NAME}：缺少必要 Tag「{tag}」")
            continue
        for name, actual, expected in (
            ("Data Model", p.data_model, model),
            ("Data Type", p.data_type, data_type),
            ("Access", p.access, access),
            ("Address Length", p.length, length),
        ):
            if actual != expected:
                errors.append(
                    f"{_where(p)}：{name} 應為「{_name(expected)}」，實際為「{_name(actual)}」"
                )
        errors += _check_attributes(p, rule)
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
                f"{FILE_NAME}：「{cur.tag}」（{cur.scale} {cur.unit}）與"
                f"「{tgt.tag}」（{tgt.scale} {tgt.unit}）的倍率或單位不一致"
            )
    return errors


def _check_overlap(points: list[IoPoint]) -> list[str]:
    """同一 (Unit ID, Data Model) 的位址範圍重疊；暫存器位元點只比同一位元。

    位元點與數值點共用暫存器由函式庫檢查（第一層）。
    """
    errors: list[str] = []
    groups: dict[tuple[int, DataModel], list[IoPoint]] = {}
    bits: dict[tuple[int, DataModel, int, int], IoPoint] = {}
    for p in points:
        if p.bit is not None:
            key = (p.unit_id, p.data_model, p.start_address, p.bit)
            first = bits.setdefault(key, p)
            if first is not p:
                errors.append(
                    f"{FILE_NAME}：Unit ID {p.unit_id} 的 {model_name(p.data_model)} 位址重疊："
                    f"「{first.tag}」與「{p.tag}」都是 {p.start_address}.b{p.bit}"
                )
            continue
        groups.setdefault((p.unit_id, p.data_model), []).append(p)
    for (unit_id, model), group in groups.items():
        group.sort(key=lambda p: (p.start_address, p.line))
        widest: IoPoint | None = None
        for p in group:
            if widest is not None and p.start_address <= widest.end_address:
                errors.append(
                    f"{FILE_NAME}：Unit ID {unit_id} 的 {model_name(model)} 位址重疊："
                    f"「{widest.tag}」（{widest.start_address} ~ {widest.end_address}）與"
                    f"「{p.tag}」（{p.start_address} ~ {p.end_address}）"
                )
            if widest is None or p.end_address > widest.end_address:
                widest = p
    return errors


def build_points(path: Path, tags: Sequence[TagInfo]) -> tuple[IoPointTable | None, list[str]]:
    """第二層檢查：以函式庫載入成功的 `tags` 建立點位表；有錯時回傳 (None, 錯誤清單)。"""
    try:
        text = decode_csv_bytes(path.read_bytes())
        records = parse_csv(text)
    except CsvFormatError as e:
        return None, [f"{FILE_NAME}：格式無法解析（第 {e.line} 列{e.reason}）"]
    except (OSError, ValueError) as e:
        return None, [f"{FILE_NAME}：格式無法解析（{e}）"]
    if not records:
        return None, [f"{FILE_NAME}：格式無法解析（檔案是空的）"]

    header = [v.strip() for v in records[0].values]
    missing = [c for c in APP_COLUMNS if c not in header]
    if missing:
        return None, [f"{FILE_NAME}：表頭缺少欄位 {'、'.join(missing)}"]
    tag_col, status_col = header.index("Tag Name"), header.index("Check Status")
    by_tag = _records_by_tag(records[1:], tag_col)

    errors: list[str] = []
    points: list[IoPoint] = []
    point_records: list[CsvRecord] = []
    invalid_tags: set[str] = set()
    for tag in tags:
        record = by_tag[tag.name]
        point, row_errors = _point(tag, record.line)
        if point is None:
            errors += row_errors
            invalid_tags.add(tag.name)
            continue
        if status_col >= len(record.spans):
            errors.append(f"{_where(point)}：缺少 Check Status 欄位的值")
            continue
        points.append(point)
        point_records.append(record)

    by_name = {p.tag: p for p in points}
    errors += _check_required(by_name, invalid_tags)
    errors += _check_position_pairs(by_name)
    errors += _check_overlap(points)
    if errors:
        return None, errors
    return IoPointTable(path, text, point_records, points, (tag_col, status_col)), []
