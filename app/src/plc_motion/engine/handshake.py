"""單軸 Code 1／Code 2 交握狀態機（spec FR-18；規格書 2.1 ~ 2.3、3.4）。

寫入順序：
    [Code 2] Target Position、Speed → PC Action Code
    → 等 PLC Action Code = PC Action Code → Request = 1
    → 等 Reply = 1 → Request = 0、PC Action Code = 0
    → 等 Reply = 0 → 等完成

前三個等待點有逾時；等完成無逾時。等完成之前因逾時、Alarm、急停提早結束時，
寫回 Request = 0、PC Action Code = 0 後才算結束。
"""

from collections.abc import Callable
from enum import Enum, auto

from plc_motion.engine.events import AxisResult, ResultKind, WaitPoint
from plc_motion.plc.port import PlcPort
from plc_motion.plc.snapshot import Snapshot

CODE_HOME = 1
CODE_MOVE = 2


class Phase(Enum):
    WAIT_COPY = auto()
    WAIT_REPLY_ON = auto()
    WAIT_REPLY_OFF = auto()
    WAIT_DONE = auto()
    FINISHED = auto()


_WAIT_POINTS = {
    Phase.WAIT_COPY: WaitPoint.COPY,
    Phase.WAIT_REPLY_ON: WaitPoint.REPLY_ON,
    Phase.WAIT_REPLY_OFF: WaitPoint.REPLY_OFF,
}


class AxisHandshake:
    def __init__(
        self,
        axis: int,
        code: int,
        port: PlcPort,
        clock: Callable[[], float],
        timeout_s: float,
        target_raw: int | None = None,
        speed: int | None = None,
    ):
        if code == CODE_MOVE and (target_raw is None or speed is None):
            raise ValueError("Code 2 需要 target_raw 與 speed")
        self.axis = axis
        self.code = code
        self._port = port
        self._clock = clock
        self._timeout_s = timeout_s
        self._target_raw = target_raw
        self._speed = speed
        self.phase: Phase | None = None
        self.result: AxisResult | None = None
        self._since = 0.0

    def _tag(self, name: str) -> str:
        return f"{name} {self.axis}"

    def _enter(self, phase: Phase) -> None:
        self.phase = phase
        self._since = self._clock()

    def start(self) -> None:
        if self.code == CODE_MOVE:
            self._port.write(self._tag("Target Position"), self._target_raw)
            self._port.write(self._tag("Speed"), self._speed)
        self._port.write(self._tag("PC Action Code"), self.code)
        self._enter(Phase.WAIT_COPY)

    def _clear_request(self) -> None:
        self._port.write(self._tag("Request"), False)
        self._port.write(self._tag("PC Action Code"), 0)

    def _finish(self, result: AxisResult, write_back: bool = False) -> AxisResult:
        if write_back:
            self._clear_request()
        self.phase = Phase.FINISHED
        self.result = result
        return result

    def abort(self, result: AxisResult) -> None:
        """不寫入、直接以 result 結束（通訊中斷用）。"""
        if self.phase != Phase.FINISHED:
            self._finish(result)

    def advance(self, snapshot: Snapshot) -> AxisResult | None:
        """依最新快照推進；有結果時回傳該結果（之後不再推進）。"""
        if self.phase in (None, Phase.FINISHED):
            return None
        st = snapshot.axis(self.axis)
        early = self.phase != Phase.WAIT_DONE

        if not snapshot.estop_ok:
            return self._finish(AxisResult(ResultKind.ESTOP), write_back=early)
        if st.alarm_code != 0:
            return self._finish(
                AxisResult(ResultKind.ALARM, alarm_code=st.alarm_code), write_back=early
            )

        if self.phase == Phase.WAIT_COPY and st.plc_action_code == self.code:
            self._port.write(self._tag("Request"), True)
            self._enter(Phase.WAIT_REPLY_ON)
        elif self.phase == Phase.WAIT_REPLY_ON and st.reply:
            self._clear_request()
            self._enter(Phase.WAIT_REPLY_OFF)
        elif self.phase == Phase.WAIT_REPLY_OFF and not st.reply:
            self._enter(Phase.WAIT_DONE)
        elif self.phase in _WAIT_POINTS and self._clock() - self._since >= self._timeout_s:
            return self._finish(
                AxisResult(ResultKind.TIMEOUT, wait_point=_WAIT_POINTS[self.phase]),
                write_back=True,
            )

        if self.phase == Phase.WAIT_DONE:
            done = st.ready and not st.busy
            if self.code == CODE_HOME:
                done = done and st.home_finish
            if done:
                return self._finish(AxisResult(ResultKind.DONE))
        return None
