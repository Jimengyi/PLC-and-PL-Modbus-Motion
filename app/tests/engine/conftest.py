"""MotionEngine 測試台：ModelPort(PlcModel) 扮演 PLC，時間由測試推進。

每次 `tick()`：model.step(DT) → 時鐘 +DT → 讀快照 → engine.tick(快照)。
引擎的寫入在下一次 model.step() 才被 PLC 看到，與實機「寫入後下一次掃描」一致。
"""

from dataclasses import dataclass

import pytest

from plc_motion.engine import (
    CommandRejected,
    MotionEngine,
    OperationFinished,
    OperationStarted,
)
from plc_motion.plc import ModelPort, PlcModel
from plc_motion.settings import load_config

DT = 0.05


@dataclass(frozen=True)
class Write:
    t: float
    tag: str
    value: int | bool
    plc_action_code: int | None  # 寫入當下 model 該軸的 PLC Action Code
    reply: bool | None  # 寫入當下 model 該軸的 Reply


def axis_of(tag: str) -> int | None:
    last = tag.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else None


class RecordingPort:
    """記錄每次寫入（含寫入當下的 PLC 狀態）後轉給 ModelPort；可指定吞掉某次寫入。"""

    def __init__(self, model: PlcModel, rig: Rig):
        self._model = model
        self._inner = ModelPort(model)
        self._rig = rig
        self.writes: list[Write] = []
        self.swallow: list[tuple[str, int | bool]] = []  # 命中一次即移除，不轉給 PLC

    def read_all(self):
        return self._inner.read_all()

    def write(self, tag: str, value: int | bool) -> None:
        n = axis_of(tag)
        plc_action = reply = None
        if n is not None:
            plc_action = self._model.read(f"PLC Action Code {n}")
            reply = self._model.read(f"Reply {n}")
        self.writes.append(Write(self._rig.t, tag, value, plc_action, reply))
        if (tag, value) in self.swallow:
            self.swallow.remove((tag, value))
            return
        self._inner.write(tag, value)


class Rig:
    DT = DT

    def __init__(self, config):
        self.config = config
        self.t = 0.0
        self.model = PlcModel(config.points)
        self.port = RecordingPort(self.model, self)
        self.engine = MotionEngine(config, self.port, lambda: self.t)
        self.events: list = []
        self.engine.add_listener(self.events.append)

    # ---- 準備 ----

    def prepare(self, axes=range(1, 8), homed: bool = True) -> None:
        """Servo ON（直接操作 model，不經引擎），並可將 Find Home Finish 預設為 1。"""
        for n in axes:
            self.model.master_write(f"Servo Motor {n}", True)
        self.model.step(DT)
        if homed:
            for n in axes:
                # 測試準備：省略原點復歸，直接把 PLC 的 Find Home Finish 設為 1
                self.model.master_write(f"Find Home Finish {n}", True)

    def connect(self) -> None:
        self.engine.on_connected()
        self.tick()

    # ---- 推進 ----

    def tick(self) -> None:
        self.model.step(DT)
        self.t += DT
        self.engine.tick(self.port.read_all())

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / DT)):
            self.tick()

    def run_until(self, cond, limit: float = 60.0) -> None:
        start = self.t
        while not cond():
            assert self.t - start < limit, "條件在時限內未成立"
            self.tick()

    def run_until_finished(self, limit: float = 60.0) -> OperationFinished:
        count = len(self.finished())
        self.run_until(lambda: len(self.finished()) > count, limit)
        return self.finished()[-1]

    # ---- 觀測 ----

    def finished(self) -> list[OperationFinished]:
        return [e for e in self.events if isinstance(e, OperationFinished)]

    def started(self) -> list[OperationStarted]:
        return [e for e in self.events if isinstance(e, OperationStarted)]

    def rejected(self) -> list[CommandRejected]:
        return [e for e in self.events if isinstance(e, CommandRejected)]

    def writes(self, axis: int | None = None) -> list[tuple[str, int | bool]]:
        return [
            (w.tag, w.value) for w in self.port.writes if axis is None or axis_of(w.tag) == axis
        ]

    def axis(self, n: int):
        return self.port.read_all().axis(n)


@pytest.fixture
def config(config_dir):
    return load_config(config_dir)


@pytest.fixture
def rig(config) -> Rig:
    return Rig(config)


@pytest.fixture
def ready_rig(rig) -> Rig:
    """七軸 Servo ON、已原點復歸、已連線且完成第一次輪詢。"""
    rig.prepare()
    rig.connect()
    return rig
