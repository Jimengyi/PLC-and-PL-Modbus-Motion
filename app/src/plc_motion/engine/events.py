"""MotionEngine 發出的事件與逐軸結果。"""

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class OperationKind(Enum):
    MOVE = "移動"
    STEP = "步進"
    HOME = "原點復歸"
    RESET = "Reset Alarm"


class ResultKind(Enum):
    DONE = "完成"
    ALARM = "Alarm"
    ESTOP = "急停"
    TIMEOUT = "逾時"
    COMM_LOST = "通訊中斷"
    NOT_RUN = "未執行"
    RESET_OK = "復歸成功"
    RESET_FAIL = "復歸失敗"


class WaitPoint(Enum):
    """交握中設有逾時的等待點（規格書 3.4）。"""

    COPY = "命令碼複製"
    REPLY_ON = "等待 Reply = 1"
    REPLY_OFF = "等待 Reply = 0"


@dataclass(frozen=True)
class AxisResult:
    kind: ResultKind
    alarm_code: int | None = None  # kind 為 ALARM 時
    wait_point: WaitPoint | None = None  # kind 為 TIMEOUT 時

    @property
    def text(self) -> str:
        if self.kind == ResultKind.ALARM:
            return f"Alarm（{self.alarm_code}）"
        if self.kind == ResultKind.TIMEOUT and self.wait_point is not None:
            return f"逾時（{self.wait_point.value}）"
        return self.kind.value


@dataclass(frozen=True)
class OperationStarted:
    kind: OperationKind
    axes: tuple[int, ...]
    params: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class OperationFinished:
    kind: OperationKind
    results: MappingProxyType[int, AxisResult]  # 軸號 → 結果，依軸號排序


@dataclass(frozen=True)
class CommandRejected:
    command: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Notice:
    text: str


@dataclass(frozen=True)
class ResidualInfo:
    """連線後第一次輪詢讀到的殘留交握（FR-57）。"""

    axis: int
    request: bool
    pc_action_code: int
    target_position: float  # 以倍率換算後的實際值


@dataclass(frozen=True)
class ResidualDetected:
    axes: tuple[ResidualInfo, ...]  # 依軸號排序


@dataclass(frozen=True)
class JogResidualCleared:
    """連線後第一次輪詢讀到 JOG = 1，已寫回 0（FR-58）。"""

    axis: int
    tags: tuple[str, ...]  # 寫回 0 的點（JOG Plus n／JOG Minus n）


@dataclass(frozen=True)
class DisconnectedWhileMoving:
    """斷線前有軸 Busy = 1 或 JOG 按住中（FR-13）：軸可能仍在移動。"""

    axes: tuple[int, ...]


EngineEvent = (
    OperationStarted
    | OperationFinished
    | CommandRejected
    | Notice
    | ResidualDetected
    | JogResidualCleared
    | DisconnectedWhileMoving
)
