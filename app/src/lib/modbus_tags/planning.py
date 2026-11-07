"""Read planning: turn a channel's tags into the requests sent each cycle (FR-22 ~ FR-24).

Pure functions; no communication happens here.
"""

from __future__ import annotations

from .config import MAX_READ_BITS, MAX_READ_REGISTERS, ChannelConfig, ReadRange
from .model import DataModel

__all__ = ["plan_reads"]

_BIT_MODELS = {DataModel.COIL, DataModel.DISCRETE_INPUT}
_MODEL_ORDER = list(DataModel)


def plan_reads(channel: ChannelConfig) -> list[ReadRange]:
    """Return read_ranges as configured; otherwise group tags automatically.

    Automatic grouping only merges addresses that are contiguous (no undefined address
    between them) within one unit and data model, splits by the Modbus read limits,
    and never splits a tag's registers across two requests. Register bit tags occupy
    their register (tag.length is 1 for them), so they need no special case.

    Overlapping tags merge into one atom that is never split. A single point never
    exceeds the limit (io_points validation guarantees it), so only deliberately
    overlapping points can merge into an atom larger than the limit.
    In that case one over-limit request is still emitted and the device is left to
    answer it; a failure then surfaces through the FR-30 disconnect rule, with the
    reason naming that range.
    """
    if channel.read_ranges is not None:
        return list(channel.read_ranges)

    # (unit_id, model) -> occupied (first, last) address spans, one per tag
    spans: dict[tuple[int, DataModel], list[tuple[int, int]]] = {}
    for tag in channel.tags:
        spans.setdefault((tag.unit_id, tag.data_model), []).append(
            (tag.address, tag.address + tag.length - 1)
        )

    result: list[ReadRange] = []
    for unit_id, model in sorted(spans, key=lambda k: (k[0], _MODEL_ORDER.index(k[1]))):
        limit = MAX_READ_BITS if model in _BIT_MODELS else MAX_READ_REGISTERS
        # Overlapping tags (e.g. an I32 and one of its registers) form one atom that
        # must be read together; atoms never split.
        atoms: list[list[int]] = []
        for first, last in sorted(spans[(unit_id, model)]):
            if atoms and first <= atoms[-1][1]:
                atoms[-1][1] = max(atoms[-1][1], last)
            else:
                atoms.append([first, last])
        start, end = atoms[0]
        for first, last in atoms[1:]:
            if first == end + 1 and last - start + 1 <= limit:
                end = last
            else:
                result.append(ReadRange(unit_id, model, start, end - start + 1))
                start, end = first, last
        result.append(ReadRange(unit_id, model, start, end - start + 1))
    return result
