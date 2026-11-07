"""io_points.csv parsing and validation (FR-7, FR-8)."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..datatypes import DATA_TYPES, parse_attributes
from ..model import Access, ConfigIssue, DataModel, TagInfo
from .ranges import MAX_READ_REGISTERS

_FILE = "io_points.csv"

# The 12 known columns (design "io_points.csv").
REQUIRED_COLUMNS = (
    "Tag Name",
    "Start Address",
    "Address Length",
    "Unit ID",
    "Data Model",
    "Data Type",
    "Access",
)
_OPTIONAL_COLUMNS = ("Display Name", "Description", "Attributes")
_KEPT_COLUMNS = ("Visible", "Check Status")  # only stored, go to TagInfo.extra
KNOWN_COLUMNS = frozenset(REQUIRED_COLUMNS + _OPTIONAL_COLUMNS + _KEPT_COLUMNS)

_DATA_MODELS: Mapping[str, DataModel] = {
    "coil": DataModel.COIL,
    "discrete input": DataModel.DISCRETE_INPUT,
    "holding register": DataModel.HOLDING_REGISTER,
    "input register": DataModel.INPUT_REGISTER,
}
_ACCESS: Mapping[str, Access] = {"read only": Access.READ_ONLY, "read write": Access.READ_WRITE}
_DATA_TYPE_NAMES: Mapping[str, str] = {name.lower(): name for name in DATA_TYPES}

_REGISTER_MODELS = frozenset({DataModel.HOLDING_REGISTER, DataModel.INPUT_REGISTER})
_READ_ONLY_MODELS = frozenset({DataModel.DISCRETE_INPUT, DataModel.INPUT_REGISTER})
_MAX_ADDRESS = 65535
_FC16_MAX_REGISTERS = 123
_UNIT_RANGES = {"tcp": (0, 255), "rtu": (1, 247)}

_BIT_ADDRESS = re.compile(r"(\d+)\.[bB](\d+)")


@dataclass
class PointsResult:
    tags: list[TagInfo] = field(default_factory=list)  # file order
    issues: list[ConfigIssue] = field(default_factory=list)  # location "line N"
    warnings: list[str] = field(default_factory=list)  # caller writes the log


class _RowError(Exception):
    """One problem in a row; the row is dropped."""


# --- reading ------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    """UTF-8 (with or without BOM) first, Big5 as fallback (FR-7).

    Big5 is read as cp950: that is what Excel on Windows writes, and it has
    characters (e.g. 碁, 恒) that strict Big5 cannot decode.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp950")


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _plural_of(a: str, b: str) -> bool:
    return a in (b + "s", b + "es") or b in (a + "s", a + "es")


def near_known_column(column: str) -> str | None:
    """Return the known column ``column`` resembles but is not, else None.

    Near means: differs only in case, by one character (added, missing or
    changed), or by a singular/plural ending.
    """
    if column in KNOWN_COLUMNS:
        return None
    c = column.lower()
    for known in sorted(KNOWN_COLUMNS):
        k = known.lower()
        if c == k or _within_one_edit(c, k) or _plural_of(c, k):
            return known
    return None


# --- field parsing -------------------------------------------------------------------


def _int(text: str, what: str) -> int:
    if not text.isascii() or not text.isdigit():
        raise _RowError(f"{what} {text!r} is not a whole number")
    return int(text)


def _lookup(table: Mapping[str, object], text: str, what: str) -> object:
    value = table.get(text.lower())
    if value is None:
        allowed = ", ".join(repr(k.title()) for k in table)
        raise _RowError(f"unknown {what} {text!r}; use one of {allowed}")
    return value


def _start_address(text: str, data_type: str, model: DataModel) -> tuple[int, int | None]:
    """Design "Start Address 檢查" flowchart -> (register, bit)."""
    register_bool = data_type == "Bool" and model in _REGISTER_MODELS
    if ".b" in text.lower():
        if not register_bool:
            raise _RowError(
                f"Start Address {text!r}: N.bK is only for Bool on Holding/Input Register"
            )
        m = _BIT_ADDRESS.fullmatch(text)
        if m is None or not text.isascii():
            raise _RowError(f"Start Address {text!r}: write register bits as N.bK, e.g. 20.b12")
        bit = int(m.group(2))
        if bit > 15:
            raise _RowError(f"Start Address {text!r}: bit must be 0 ~ 15")
        return int(m.group(1)), bit
    if "." in text or ":" in text:
        raise _RowError(f"Start Address {text!r}: write register bits as N.bK, e.g. 20.b12")
    if register_bool:
        raise _RowError(
            f"Start Address {text!r}: Bool on {model.name.replace('_', ' ').title()} "
            "must be written as N.bK"
        )
    return _int(text, "Start Address"), None


def _build_tag(values: Mapping[str, str], extra: Mapping[str, str], transport: str) -> TagInfo:
    empty = [c for c in REQUIRED_COLUMNS if not values.get(c)]
    if empty:
        raise _RowError("required value is empty: " + ", ".join(empty))

    name = values["Tag Name"]
    model = _lookup(_DATA_MODELS, values["Data Model"], "Data Model")
    data_type = _lookup(_DATA_TYPE_NAMES, values["Data Type"], "Data Type")
    access = _lookup(_ACCESS, values["Access"], "Access")
    assert isinstance(model, DataModel) and isinstance(access, Access)
    assert isinstance(data_type, str)
    definition = DATA_TYPES[data_type]

    if model not in definition.data_models:
        raise _RowError(f"Data Type {data_type} cannot be used with Data Model {model.name}")
    if access is Access.READ_WRITE and model in _READ_ONLY_MODELS:
        raise _RowError(f"{model.name} is read only by Modbus; Access cannot be Read Write")

    address, bit = _start_address(values["Start Address"], data_type, model)

    length = _int(values["Address Length"], "Address Length")
    if definition.length is None:
        if length < 1:
            raise _RowError(f"Address Length of {data_type} must be 1 or more")
    elif length != definition.length:
        raise _RowError(f"Address Length of {data_type} must be {definition.length}, got {length}")
    if address + length - 1 > _MAX_ADDRESS:
        raise _RowError(f"point at {address} with length {length} goes past {_MAX_ADDRESS}")
    if model in _REGISTER_MODELS:
        if length > MAX_READ_REGISTERS:
            raise _RowError(f"Address Length {length} exceeds the limit of {MAX_READ_REGISTERS}")
        if access is Access.READ_WRITE and length > _FC16_MAX_REGISTERS:
            raise _RowError(
                f"Read Write point uses {length} registers; "
                f"one write (FC16) allows at most {_FC16_MAX_REGISTERS}"
            )

    unit_id = _int(values["Unit ID"], "Unit ID")
    lo, hi = _UNIT_RANGES[transport]
    if not lo <= unit_id <= hi:
        raise _RowError(f"Unit ID {unit_id} outside {lo} ~ {hi} for {transport.upper()}")

    attrs, errors = parse_attributes(values.get("Attributes", ""), data_type)
    if errors:
        raise _RowError("; ".join(errors))

    return TagInfo(
        name=name,
        display_name=values.get("Display Name", ""),
        description=values.get("Description", ""),
        data_model=model,
        data_type=data_type,
        access=access,
        address=address,
        bit=bit,
        length=length,
        unit_id=unit_id,
        scale=attrs.get("scale"),  # type: ignore[arg-type]
        unit=attrs.get("unit", ""),  # type: ignore[arg-type]
        invert=attrs.get("invert", False),  # type: ignore[arg-type]
        word_order=attrs.get("word_order"),  # type: ignore[arg-type]
        byte_swap=attrs.get("byte_swap", False),  # type: ignore[arg-type]
        extra=extra,
    )


# --- cross-row checks ------------------------------------------------------------------


def _bit_word_conflicts(tags: list[tuple[int, TagInfo]]) -> list[tuple[int, str]]:
    """Bit points and numeric points must not share a register (FR-8)."""
    found: list[tuple[int, str]] = []
    bits: dict[tuple[int, DataModel, int], int] = {}  # (unit, model, register) -> first line
    words: dict[tuple[int, DataModel, int], int] = {}
    for line, tag in tags:
        if tag.data_model not in _REGISTER_MODELS:
            continue
        is_bit = tag.bit is not None
        mine, other = (bits, words) if is_bit else (words, bits)
        for reg in range(tag.address, tag.address + tag.length):
            key = (tag.unit_id, tag.data_model, reg)
            if key in other:
                kind = "numeric" if is_bit else "bit"
                found.append(
                    (line, f"register {reg} is also used by the {kind} point on line {other[key]}")
                )
                break
        for reg in range(tag.address, tag.address + tag.length):
            mine.setdefault((tag.unit_id, tag.data_model, reg), line)
    return found


# --- entry point ----------------------------------------------------------------------


def parse_io_points(path: Path, transport: str) -> PointsResult:
    """Parse one channel's io_points.csv.

    ``transport`` is "tcp" or "rtu" (Unit ID range). ``ConfigIssue.channel``
    is the folder holding the file.
    """
    channel = path.parent.name
    result = PointsResult()

    def issue(location: str, message: str) -> None:
        result.issues.append(ConfigIssue(channel, _FILE, location, message))

    try:
        text = _decode(path.read_bytes())
    except OSError as e:
        issue("(file)", f"cannot read file: {e}")
        return result
    except UnicodeDecodeError:
        issue("(file)", "file is neither UTF-8 nor Big5")
        return result

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [c.strip() for c in next(reader)]
    except StopIteration:
        issue("line 1", "file is empty; the first row must be the column header")
        return result
    except csv.Error as e:
        issue("line 1", f"CSV format error: {e}")
        return result

    seen: set[str] = set()
    for column in header:
        if column and column in seen:
            issue("line 1", f"column {column!r} appears more than once")
        seen.add(column)
    missing = [c for c in REQUIRED_COLUMNS if c not in seen]
    if missing:
        near = {near_known_column(c): c for c in header if c}
        hints = [f"found {near[m]!r}, did you mean {m!r}?" for m in missing if m in near]
        message = "missing required column: " + ", ".join(missing)
        issue("line 1", message + (" (" + "; ".join(hints) + ")" if hints else ""))
    for column in header:
        known = near_known_column(column) if column else None
        if known is not None:
            result.warnings.append(
                f"{_FILE}: column {column!r} looks like known column {known!r}; kept as extra data"
            )
    if result.issues:
        return result

    known_used = KNOWN_COLUMNS - set(_KEPT_COLUMNS)
    names: dict[str, int] = {}
    valid: list[tuple[int, TagInfo]] = []
    line = reader.line_num
    while True:
        start = line + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as e:
            # The reader drops the bad record and goes on with the next line.
            issue(f"line {start}", f"CSV format error: {e}")
            line = reader.line_num
            continue
        line = reader.line_num
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        if len(cells) > len(header) and any(cells[len(header) :]):
            issue(f"line {start}", "row has more values than the header has columns")
            continue
        cells += [""] * (len(header) - len(cells))
        values = {c: v for c, v in zip(header, cells, strict=False) if c in known_used}
        extra = {c: v for c, v in zip(header, cells, strict=False) if c and c not in known_used}
        try:
            tag = _build_tag(values, extra, transport)
        except _RowError as e:
            issue(f"line {start}", str(e))
            continue
        if tag.name in names:
            issue(f"line {start}", f"Tag Name {tag.name!r} already used on line {names[tag.name]}")
            continue
        names[tag.name] = start
        valid.append((start, tag))

    for line_no, message in _bit_word_conflicts(valid):
        issue(f"line {line_no}", message)
    result.issues.sort(key=lambda i: int(i.location.removeprefix("line ")))
    result.tags = [t for _, t in valid]
    return result
