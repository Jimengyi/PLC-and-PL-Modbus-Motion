"""Data Type definitions, Attributes parsing and value encode/decode.

Pure functions: no network, no pymodbus. Each Data Type is one ``DataTypeDef``
carrying its own codec, so adding a type means adding one definition.

Decode order (design "讀值解碼流程"): arrange registers by word_order and
byte_swap -> build the number for the Data Type -> divide by scale -> invert
(Bool). Encode is the reverse; checks happen at call time (FR-20).
"""

from __future__ import annotations

import math
import numbers
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .errors import ValueOutOfRange
from .model import DataModel, TagInfo

WORD_ORDERS = ("low_first", "high_first")

_REGISTER_MODELS = frozenset({DataModel.HOLDING_REGISTER, DataModel.INPUT_REGISTER})
_BIT_MODELS = frozenset({DataModel.COIL, DataModel.DISCRETE_INPUT})
_NUMERIC_ATTRS = frozenset({"scale", "unit"})
_WIDE_ATTRS = _NUMERIC_ATTRS | {"word_order", "byte_swap"}

Raw = Sequence[int] | Sequence[bool]
Decoder = Callable[[TagInfo, Raw], object]
Encoder = Callable[[TagInfo, object], "list[int] | bool"]


@dataclass(frozen=True)
class DataTypeDef:
    name: str
    length: int | None  # registers used; None for String (set by Address Length)
    data_models: frozenset[DataModel]
    attributes: frozenset[str]  # usable Attributes keys
    decode: Decoder
    encode: Encoder


# --- register helpers ---------------------------------------------------------


def _swap_bytes(reg: int) -> int:
    return ((reg & 0xFF) << 8) | ((reg >> 8) & 0xFF)


def _low_first(tag: TagInfo) -> bool:
    return tag.word_order != "high_first"  # a point without word_order is low_first


def _regs_to_u32(tag: TagInfo, raw: Raw) -> int:
    regs = [int(r) & 0xFFFF for r in raw[:2]]
    if tag.byte_swap:
        regs = [_swap_bytes(r) for r in regs]
    if _low_first(tag):
        regs.reverse()
    return (regs[0] << 16) | regs[1]


def _u32_to_regs(tag: TagInfo, value: int) -> list[int]:
    regs = [(value >> 16) & 0xFFFF, value & 0xFFFF]  # high word first
    if _low_first(tag):
        regs.reverse()
    if tag.byte_swap:
        regs = [_swap_bytes(r) for r in regs]
    return regs


def _round_half_away(x: float) -> int:
    # Built-in round() is banker's rounding; FR-19 wants .5 away from zero.
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _check_number(tag: TagInfo, value: object) -> numbers.Real:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(
            f"tag {tag.name!r} ({tag.data_type}) needs a number, got {type(value).__name__}"
        )
    return value


def _apply_scale(tag: TagInfo, value: numbers.Real) -> numbers.Real:
    """Scale for writing; a non-finite or overflowing result is out of range (FR-20)."""
    try:
        scaled = value * tag.scale if tag.scale else value
    except OverflowError:
        raise ValueOutOfRange(f"tag {tag.name!r}: {value!r} outside range") from None
    if not isinstance(scaled, numbers.Integral) and not math.isfinite(scaled):
        raise ValueOutOfRange(f"tag {tag.name!r}: {value!r} is not a finite number")
    return scaled


# --- integer types ------------------------------------------------------------


def _int_type(name: str, words: int, signed: bool) -> DataTypeDef:
    bits = 16 * words
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)

    def dec(tag: TagInfo, raw: Raw) -> object:
        n = _regs_to_u32(tag, raw) if words == 2 else int(raw[0]) & 0xFFFF
        if signed and n > hi:
            n -= 1 << bits
        return n / tag.scale if tag.scale else n

    def enc(tag: TagInfo, value: object) -> list[int]:
        scaled = _apply_scale(tag, _check_number(tag, value))
        n = int(scaled) if isinstance(scaled, numbers.Integral) else _round_half_away(float(scaled))
        if not lo <= n <= hi:
            raise ValueOutOfRange(
                f"tag {tag.name!r} ({name}): {value!r} -> raw {n} outside {lo} ~ {hi}"
            )
        n &= (1 << bits) - 1
        return _u32_to_regs(tag, n) if words == 2 else [n]

    attrs = _WIDE_ATTRS if words == 2 else _NUMERIC_ATTRS
    return DataTypeDef(name, words, _REGISTER_MODELS, attrs, dec, enc)


# --- F32 ------------------------------------------------------------------------


def _f32_decode(tag: TagInfo, raw: Raw) -> object:
    u = _regs_to_u32(tag, raw)
    (f,) = struct.unpack(">f", u.to_bytes(4, "big"))
    return f / tag.scale if tag.scale else f


def _f32_encode(tag: TagInfo, value: object) -> list[int]:
    scaled = _apply_scale(tag, _check_number(tag, value))
    try:
        packed = struct.pack(">f", float(scaled))
    except OverflowError:
        raise ValueOutOfRange(
            f"tag {tag.name!r} (F32): {value!r} outside single precision range"
        ) from None
    return _u32_to_regs(tag, int.from_bytes(packed, "big"))


# --- Bool -------------------------------------------------------------------------


def _bool_decode(tag: TagInfo, raw: Raw) -> object:
    if tag.data_model in _BIT_MODELS:
        return bool(raw[0]) != tag.invert
    return bool((int(raw[0]) >> tag.bit) & 1) != tag.invert


def _bool_encode(tag: TagInfo, value: object) -> bool:
    # Register bit points also get the bit value; the write path builds the mask.
    if not isinstance(value, bool):
        raise TypeError(f"tag {tag.name!r} (Bool) needs a bool, got {type(value).__name__}")
    return value != tag.invert


# --- String -----------------------------------------------------------------------


def _string_decode(tag: TagInfo, raw: Raw) -> object:
    data = bytearray()
    for r in raw:
        r = int(r) & 0xFFFF
        if tag.byte_swap:
            r = _swap_bytes(r)
        data += r.to_bytes(2, "big")
    end = data.find(0)
    if end >= 0:
        del data[end:]
    return data.decode("ascii", errors="replace")  # bytes >= 0x80 -> U+FFFD


def _string_encode(tag: TagInfo, value: object) -> list[int]:
    if not isinstance(value, str):
        raise TypeError(f"tag {tag.name!r} (String) needs a str, got {type(value).__name__}")
    size = tag.length * 2
    if len(value) > size:
        raise ValueOutOfRange(f"tag {tag.name!r}: {len(value)} characters exceed {size}")
    if not value.isascii():
        raise ValueOutOfRange(f"tag {tag.name!r}: {value!r} contains non-ASCII characters")
    data = value.encode("ascii").ljust(size, b"\0")
    regs = [(data[i] << 8) | data[i + 1] for i in range(0, size, 2)]
    return [_swap_bytes(r) for r in regs] if tag.byte_swap else regs


# --- registry -----------------------------------------------------------------------

_DEFS = [
    DataTypeDef("Bool", 1, frozenset(DataModel), frozenset({"invert"}), _bool_decode, _bool_encode),
    _int_type("U16", 1, signed=False),
    _int_type("I16", 1, signed=True),
    _int_type("U32", 2, signed=False),
    _int_type("I32", 2, signed=True),
    DataTypeDef("F32", 2, _REGISTER_MODELS, _WIDE_ATTRS, _f32_decode, _f32_encode),
    DataTypeDef(
        "String", None, _REGISTER_MODELS, frozenset({"byte_swap"}), _string_decode, _string_encode
    ),
]

DATA_TYPES: Mapping[str, DataTypeDef] = MappingProxyType({d.name: d for d in _DEFS})


# --- Attributes -----------------------------------------------------------------------


def _parse_bool(text: str) -> bool:
    if text not in ("true", "false"):
        raise ValueError("must be true or false")
    return text == "true"


def _parse_scale(text: str) -> float:
    if "_" in text:  # float() accepts digit separators such as 1_0
        raise ValueError("must be a positive number")
    try:
        scale = float(text)
    except ValueError:
        raise ValueError("must be a positive number") from None
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("must be a positive number")
    return scale


def _parse_word_order(text: str) -> str:
    if text not in WORD_ORDERS:
        raise ValueError("must be low_first or high_first")
    return text


_ATTRIBUTE_PARSERS: Mapping[str, Callable[[str], object]] = MappingProxyType(
    {
        "scale": _parse_scale,
        "unit": str,
        "invert": _parse_bool,
        "word_order": _parse_word_order,
        "byte_swap": _parse_bool,
    }
)


def parse_attributes(text: str, data_type: str) -> tuple[dict[str, object], list[str]]:
    """Return (parsed attributes, error messages); one message per bad item.

    ``data_type`` is the canonical name. An unknown name skips the
    "not applicable to this type" check; the caller reports the bad type.
    """
    definition = DATA_TYPES.get(data_type)
    attrs: dict[str, object] = {}
    errors: list[str] = []
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        key, sep, raw_value = item.partition("=")
        key, raw_value = key.strip(), raw_value.strip()
        if not sep:
            errors.append(f"attribute {item!r} needs key=value")
            continue
        parser = _ATTRIBUTE_PARSERS.get(key)
        if parser is None:
            errors.append(f"unknown attribute {key!r}")
            continue
        if definition is not None and key not in definition.attributes:
            errors.append(f"attribute {key!r} does not apply to {data_type}")
            continue
        if key in attrs:
            errors.append(f"attribute {key!r} given more than once")
            continue
        try:
            attrs[key] = parser(raw_value)
        except ValueError as exc:
            errors.append(f"attribute {key}={raw_value!r}: {exc}")
    return attrs, errors


# --- public codec -----------------------------------------------------------------------


def decode(tag: TagInfo, raw: Sequence[int] | Sequence[bool]) -> object:
    """Raw registers or bits -> engineering value (FR-17, FR-18, FR-21)."""
    return DATA_TYPES[tag.data_type].decode(tag, raw)


def encode(tag: TagInfo, value: object) -> list[int] | bool:
    """Engineering value -> raw registers, or the bit value for Bool.

    Checked at call time; raises ValueOutOfRange or TypeError (FR-19, FR-20).
    """
    return DATA_TYPES[tag.data_type].encode(tag, value)
