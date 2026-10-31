"""I32（2 × U16，低位在前）與工程單位的換算。"""

import math
from decimal import ROUND_HALF_UP, Decimal

I32_MIN = -(2**31)
I32_MAX = 2**31 - 1


def split_i32(value: int) -> tuple[int, int]:
    """有號 I32 拆成 (low_word, high_word)，皆為 0~65535。"""
    if not I32_MIN <= value <= I32_MAX:
        raise ValueError(f"超出 I32 範圍：{value}")
    unsigned = value & 0xFFFFFFFF
    return unsigned & 0xFFFF, unsigned >> 16


def join_i32(low: int, high: int) -> int:
    """(low, high) 合成有號 I32。"""
    unsigned = ((high & 0xFFFF) << 16) | (low & 0xFFFF)
    return unsigned - (1 << 32) if unsigned & 0x80000000 else unsigned


def to_raw(value: float, scale: int) -> int:
    """工程單位轉 I32 原始值，四捨五入（0.5 進位）；超出 I32 範圍 raise ValueError。"""
    if not math.isfinite(value):
        raise ValueError(f"不是有限數值：{value}")
    # 經由 repr 轉 Decimal，避免 float 二進位誤差造成 ±1
    scaled = Decimal(repr(float(value))) * scale
    # 先擋極大值：quantize 超過 Decimal 精度會丟 InvalidOperation 而非 ValueError
    if abs(scaled) > 2**32:
        raise ValueError(f"換算後超出 I32 範圍：{value} × {scale}")
    raw = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if not I32_MIN <= raw <= I32_MAX:
        raise ValueError(f"換算後超出 I32 範圍：{value} × {scale}")
    return raw


def from_raw(raw: int, scale: int) -> float:
    return raw / scale


def decimals_for(scale: int) -> int:
    """倍率對應的小數位數：10000 → 4、100 → 2。"""
    return len(str(scale)) - 1
