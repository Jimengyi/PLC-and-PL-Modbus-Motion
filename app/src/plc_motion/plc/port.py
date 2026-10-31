"""PLC 讀寫介面 PlcPort，與直接包 PlcModel 的 adapter ModelPort。"""

from typing import Protocol, runtime_checkable

from plc_motion.plc.model import PlcModel
from plc_motion.plc.snapshot import Snapshot


class PlcCommError(Exception):
    """PLC 通訊失敗；訊息含 Tag Name 與位址（若可得）。"""

    def __init__(self, message: str, tag: str | None = None, address: int | None = None):
        self.tag = tag
        self.address = address
        where = []
        if tag is not None:
            where.append(f"Tag「{tag}」")
        if address is not None:
            where.append(f"位址 {address}")
        super().__init__(f"{message}（{'、'.join(where)}）" if where else message)


@runtime_checkable
class PlcPort(Protocol):
    def read_all(self) -> Snapshot: ...

    def write(self, tag: str, value: int | bool) -> None:
        """寫入單一點；Position 點傳 raw I32，adapter 負責拆 Word。"""
        ...


class ModelPort(PlcPort):
    """單元測試用：直接讀寫 PlcModel，不經 Modbus。"""

    def __init__(self, model: PlcModel):
        self._model = model

    def read_all(self) -> Snapshot:
        points = self._model.points
        return Snapshot(points, {p.tag: self._model.read(p.tag) for p in points.all()})

    def write(self, tag: str, value: int | bool) -> None:
        if not self._model.has_tag(tag):
            raise PlcCommError("io_points 中沒有這個點", tag=tag)
        self._model.master_write(tag, value)
