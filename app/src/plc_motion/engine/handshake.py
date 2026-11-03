"""單軸 Code 1／Code 2 交握狀態機（spec FR-18；規格書 2.1 ~ 2.3、3.4）。

寫入順序：
    [Code 2] Target Position、Speed → PC Action Code
    → 等 PLC Action Code = PC Action Code → Request = 1
    → 等 Reply = 1 → Request = 0、PC Action Code = 0
    → 等 Reply = 0 → 等完成

前三個等待點有逾時；等完成無逾時。等完成之前因逾時、Alarm、急停提早結束時，
寫回 Request = 0、PC Action Code = 0 後才算結束。

使用方式（由 MotionEngine 呼叫，本類別不自己跑迴圈、不開執行緒）：
    hs = AxisHandshake(...)   # 建立時不寫任何點
    hs.start()                # 寫入命令，進入 WAIT_COPY
    每輪輪詢：result = hs.advance(snapshot)
        回傳 None 表示還在進行；回傳 AxisResult 表示已結束，之後再呼叫也不會動作

狀態轉移（每次 advance 最多前進一格）：
    WAIT_COPY      --PLC Action Code = code，寫 Request = 1-->              WAIT_REPLY_ON
    WAIT_REPLY_ON  --Reply = 1，寫 Request = 0、PC Action Code = 0-->       WAIT_REPLY_OFF
    WAIT_REPLY_OFF --Reply = 0-->                                          WAIT_DONE
    WAIT_DONE      --Ready = 1 且 Busy = 0（Code 1 另需 Find Home Finish）--> FINISHED

時序注意：advance 拿到的快照是本輪開頭讀的，本輪寫入的點要下一輪讀取才看得到；
所以每一步至少隔一個輪詢週期。
"""

from collections.abc import Callable
from enum import Enum, auto

from plc_motion.engine.events import AxisResult, ResultKind, WaitPoint
from plc_motion.plc.port import PlcPort
from plc_motion.plc.snapshot import Snapshot

# PC Action Code：本專案 PLC 只支援這兩種；相對移動由 MotionEngine 換算成 Code 2
CODE_HOME = 1  # 原點復歸
CODE_MOVE = 2  # 絕對移動


class Phase(Enum):
    """交握的各個階段（相當於 LabVIEW 狀態機的 enum）。"""

    WAIT_COPY = auto()  # 已寫 PC Action Code，等 PLC 把它複製到 PLC Action Code
    WAIT_REPLY_ON = auto()  # 已寫 Request = 1，等 PLC 回 Reply = 1
    WAIT_REPLY_OFF = auto()  # 已把 Request、PC Action Code 清 0，等 PLC 回 Reply = 0
    WAIT_DONE = auto()  # 交握完成，等軸動作結束（無逾時，移動多久都等）
    FINISHED = auto()  # 已有結果（成功或失敗），不再推進


# 有逾時的等待點，及逾時時回報給使用者的等待點名稱；WAIT_DONE 不在其中，所以不會逾時
_WAIT_POINTS = {
    Phase.WAIT_COPY: WaitPoint.COPY,
    Phase.WAIT_REPLY_ON: WaitPoint.REPLY_ON,
    Phase.WAIT_REPLY_OFF: WaitPoint.REPLY_OFF,
}


class AxisHandshake:
    """一個軸、一次命令的交握；命令結束後即丟棄，下一次命令建新的物件。"""

    def __init__(
        self,
        axis: int,  # 軸號 1 ~ 7，用來組出點位名稱（例如 "Request 3"）
        code: int,  # CODE_HOME 或 CODE_MOVE
        port: PlcPort,  # 寫入 PLC 的介面：正式為 ModbusLink，單元測試為 ModelPort
        clock: Callable[[], float],  # 取目前時間（秒）：正式為 time.monotonic，測試可換假時鐘
        timeout_s: float,  # 每個等待點的逾時秒數（device.toml 的 handshake_timeout_s）
        target_raw: int | None = None,  # Code 2 的目標位置，已乘上倍率的整數（I32）
        speed: int | None = None,  # Code 2 的速度，MotionEngine 已限制在 1 ~ 100
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
        self.phase: Phase | None = None  # None：尚未 start()
        self.result: AxisResult | None = None  # 結束後存放結果，供 MotionEngine 彙整
        self._since = 0.0  # 進入目前階段的時間，逾時從這裡起算

    def _tag(self, name: str) -> str:
        """組出本軸的點位名稱，對應 io_points.csv 的 Tag Name，例如 "Request 3"。"""
        return f"{name} {self.axis}"

    def _enter(self, phase: Phase) -> None:
        """切換階段並重設計時：每個等待點各自從進入時開始計算逾時。"""
        self.phase = phase
        self._since = self._clock()

    def start(self) -> None:
        """送出命令：Code 2 先寫目標位置與速度，最後寫 PC Action Code。

        PC Action Code 必須最後寫：PLC 看到它才會去讀目標與速度。
        """
        if self.code == CODE_MOVE:
            self._port.write(self._tag("Target Position"), self._target_raw)
            self._port.write(self._tag("Speed"), self._speed)
        self._port.write(self._tag("PC Action Code"), self.code)
        self._enter(Phase.WAIT_COPY)

    def _clear_request(self) -> None:
        """把 PC 端的交握點歸零（Request = 0、PC Action Code = 0）。

        兩種情況會呼叫：正常流程收到 Reply = 1 時；以及交握途中失敗、要收尾時，
        避免 PLC 留著半套交握。
        """
        self._port.write(self._tag("Request"), False)
        self._port.write(self._tag("PC Action Code"), 0)

    def _finish(self, result: AxisResult, write_back: bool = False) -> AxisResult:
        """結束交握並記下結果；write_back=True 時先把交握點歸零。"""
        if write_back:
            self._clear_request()
        self.phase = Phase.FINISHED
        self.result = result
        return result

    def abort(self, result: AxisResult) -> None:
        """不寫入、直接以 result 結束（通訊中斷用）。

        斷線時已經寫不到 PLC，所以不歸零；重新連線後由 MotionEngine 的殘留交握檢查處理。
        """
        if self.phase != Phase.FINISHED:
            self._finish(result)

    def advance(self, snapshot: Snapshot) -> AxisResult | None:
        """依最新快照推進；有結果時回傳該結果（之後不再推進）。

        每輪輪詢由 MotionEngine 呼叫一次，相當於 LabVIEW 狀態機的一次迴圈。判斷順序：
            1. 尚未開始或已結束：不動作
            2. 急停、Alarm：不論在哪個階段都立刻結束
            3. match／case（相當於 Case Structure）：依目前階段檢查 PLC 訊號，
               條件成立就寫下一個點並前進一格
            4. 三個等待點共用的逾時判斷
            5. 等完成階段：檢查軸是否動作完畢
        """
        # 1. 尚未 start() 或已有結果
        if self.phase in (None, Phase.FINISHED):
            return None
        st = snapshot.axis(self.axis)  # 本軸這一輪的狀態（Ready、Busy、Reply、Alarm Code…）
        # 進入 WAIT_DONE 之前，PC 端的 Request／PC Action Code 可能還不是 0，提早結束時要歸零；
        # 進入 WAIT_DONE 時已經歸零，不必再寫
        early = self.phase != Phase.WAIT_DONE

        # 2. 異常優先：急停、Alarm 不論在哪個階段都立刻結束
        if not snapshot.estop_ok:
            return self._finish(AxisResult(ResultKind.ESTOP), write_back=early)
        if st.alarm_code != 0:
            return self._finish(
                AxisResult(ResultKind.ALARM, alarm_code=st.alarm_code), write_back=early
            )

        # 3. 依目前階段處理（相當於 LabVIEW 的 Case Structure）；
        #    每個 case 只看自己等的訊號，條件成立才前進一格，所以每輪最多前進一格
        match self.phase:
            case Phase.WAIT_COPY:
                if st.plc_action_code == self.code:
                    # PLC 已把命令碼複製回來，表示收到命令 → 送出 Request
                    self._port.write(self._tag("Request"), True)
                    self._enter(Phase.WAIT_REPLY_ON)
            case Phase.WAIT_REPLY_ON:
                if st.reply:
                    # PLC 回 Reply = 1，表示已接受命令 → PC 端歸零
                    self._clear_request()
                    self._enter(Phase.WAIT_REPLY_OFF)
            case Phase.WAIT_REPLY_OFF:
                if not st.reply:
                    # PLC 看到 Request = 0 後把 Reply 也清 0 → 交握完成，開始等軸動作結束
                    self._enter(Phase.WAIT_DONE)
            case Phase.WAIT_DONE:
                pass  # 在下面第 5 步判斷，讓剛轉進 WAIT_DONE 的同一輪也能判斷

        # 4. 三個等待點共用的逾時判斷，不必在每個 case 各寫一份。
        #    本輪剛換到下一個階段時，_enter() 已重設計時，不會誤判逾時
        if self.phase in _WAIT_POINTS and self._clock() - self._since >= self._timeout_s:
            return self._finish(
                AxisResult(ResultKind.TIMEOUT, wait_point=_WAIT_POINTS[self.phase]),
                write_back=True,
            )

        # 5. 等完成：放在 match 外面。上面剛從 WAIT_REPLY_OFF 轉進 WAIT_DONE 時，
        #    同一輪就接著檢查是否已完成，不必多等一輪
        if self.phase == Phase.WAIT_DONE:
            done = st.ready and not st.busy
            if self.code == CODE_HOME:
                # 原點復歸完成時 PLC 先設 Busy = 0、之後才設 Find Home Finish = 1
                # （規格書 2.3），只看 Busy 會太早判定完成
                done = done and st.home_finish
            if done:
                return self._finish(AxisResult(ResultKind.DONE))
        return None
