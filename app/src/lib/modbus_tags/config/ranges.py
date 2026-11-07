"""read_ranges.toml parsing and validation (FR-10)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import ConfigIssue, DataModel, TagInfo
from .device import DeviceConfig

_FILE = "read_ranges.toml"

# toml key -> data model
_KEYS: dict[str, DataModel] = {
    "coil": DataModel.COIL,
    "discrete_input": DataModel.DISCRETE_INPUT,
    "holding_register": DataModel.HOLDING_REGISTER,
    "input_register": DataModel.INPUT_REGISTER,
}
_BIT_MODELS = {DataModel.COIL, DataModel.DISCRETE_INPUT}
_MAX_ADDRESS = 65535
# What one read request may ask for by the Modbus standard. A device with a lower limit
# gets shorter segments in its read_ranges.toml.
MAX_READ_REGISTERS = 125
MAX_READ_BITS = 2000
_ITEM = re.compile(r"^(\d+)(?:\s*~\s*(\d+))?$")


@dataclass(frozen=True)
class ReadRange:
    unit_id: int
    data_model: DataModel
    start: int
    count: int


@dataclass
class RangesResult:
    ranges: list[ReadRange] = field(default_factory=list)
    issues: list[ConfigIssue] = field(default_factory=list)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_read_ranges(path: Path, device: DeviceConfig, tags: Sequence[TagInfo]) -> RangesResult:
    channel = path.parent.name
    result = RangesResult()

    def error(location: str, message: str) -> None:
        result.issues.append(ConfigIssue(channel, _FILE, location, message))

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        error("(file)", f"TOML syntax error: {e}")
        return result
    except OSError as e:
        error("(file)", f"cannot read file: {e}")
        return result

    for key in data:
        if key != "unit":
            error(key, "unknown key")
    units = data.get("unit", [])
    if not isinstance(units, list) or not all(isinstance(u, dict) for u in units):
        error("unit", "must be an array of tables ([[unit]])")
        return result

    max_unit = 247 if device.transport == "rtu" else 255
    min_unit = 1 if device.transport == "rtu" else 0
    # (unit_id, model) -> (toml location, list of (start, end)) used for coverage
    covered: dict[tuple[int, DataModel], tuple[str, list[tuple[int, int]]]] = {}
    seen_units: set[int] = set()

    for i, unit in enumerate(units):
        prefix = f"unit[{i}]."
        for key in unit:
            if key != "unit_id" and key not in _KEYS:
                error(f"{prefix}{key}", "unknown key")
        unit_id = unit.get("unit_id")
        if "unit_id" not in unit:
            error(f"{prefix}unit_id", "required")
            continue
        if not _is_int(unit_id) or not min_unit <= unit_id <= max_unit:
            error(
                f"{prefix}unit_id",
                f"must be an integer {min_unit} ~ {max_unit} for {device.transport}",
            )
            continue
        if unit_id in seen_units:
            error(f"{prefix}unit_id", f"duplicate unit_id {unit_id}")
            continue
        seen_units.add(unit_id)

        for key, model in _KEYS.items():
            if key not in unit:
                continue
            location = f"{prefix}{key}"
            spans = _parse_spans(unit[key], location, error)
            limit = MAX_READ_BITS if model in _BIT_MODELS else MAX_READ_REGISTERS
            for start, end in spans:
                if end - start + 1 > limit:
                    error(location, f"range {start}~{end} exceeds the limit of {limit}")
            spans_sorted = sorted(spans)
            for a, (s1, e1) in enumerate(spans_sorted):
                for s2, e2 in spans_sorted[a + 1 :]:
                    if s2 > e1:
                        break
                    error(location, f"range {s2}~{e2} overlaps range {s1}~{e1}")
            for start, end in spans:
                result.ranges.append(ReadRange(unit_id, model, start, end - start + 1))
            covered[(unit_id, model)] = (location, spans)

    if not result.issues:
        _check_coverage(tags, covered, error)
    return result


def _parse_spans(
    value: Any, location: str, error: Callable[[str, str], None]
) -> list[tuple[int, int]]:
    if not isinstance(value, str):
        error(location, 'must be a string such as "20~84, 120~184"')
        return []
    if value.strip() == "":
        return []
    spans: list[tuple[int, int]] = []
    for item in value.split(","):
        m = _ITEM.match(item.strip())
        if not m:
            error(location, f"invalid range {item.strip()!r}; use 'start~end' or a single number")
            continue
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if start > end:
            error(location, f"invalid range {item.strip()!r}; start is greater than end")
            continue
        if end > _MAX_ADDRESS:
            error(location, f"range {item.strip()!r} exceeds the maximum address {_MAX_ADDRESS}")
            continue
        spans.append((start, end))
    return spans


def _check_coverage(
    tags: Sequence[TagInfo],
    covered: dict[tuple[int, DataModel], tuple[str, list[tuple[int, int]]]],
    error: Callable[[str, str], None],
) -> None:
    for tag in tags:
        first, last = tag.address, tag.address + tag.length - 1
        entry = covered.get((tag.unit_id, tag.data_model))
        if entry is None:
            error(
                "(file)",
                f"tag {tag.name!r} (unit {tag.unit_id}, {tag.data_model.name}) "
                "is not covered by any range",
            )
            continue
        location, spans = entry
        if any(s <= first and last <= e for s, e in spans):
            continue
        if all(any(s <= reg <= e for s, e in spans) for reg in range(first, last + 1)):
            error(location, f"tag {tag.name!r} spans more than one range")
        else:
            error(location, f"tag {tag.name!r} is not covered by any range")
