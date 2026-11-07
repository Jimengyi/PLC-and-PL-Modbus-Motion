"""PlcPort 的 modbus_tags adapter：包住 `lib.modbus_tags.Bus`（spec FR-15 ~ FR-25）。

通訊在函式庫的通道執行緒進行；這裡不送事件，由呼叫端定期 `poll()` 取結果
（design「執行流程」）。所有方法由同一條執行緒呼叫（正式為 Worker 執行緒）。

- 每次 `connect()` 建新 Bus（停止的 Bus 不能再啟動），host、port 依參數，
  `auto_reconnect` 一律關閉：斷線後只由操作員重新連線。
- `poll()` 先取最新快照、再查寫入失敗與連線狀態：拿到的快照若屬於寫入失敗那一輪，
  一定也看得到失敗（通道每輪先寫入、後讀取），交握不會用那一輪推進（FR-24）。
- 寫入值：`int` 寫到有 `scale` 的點視為 PLC 原始值，先除以倍率再交給函式庫，函式庫乘回並
  四捨五入，I32 範圍內結果與原始值完全相同（FR-21）；`float` 視為工程值；Bool 為邏輯值。
- 寫入依呼叫順序排入通道佇列，依序送出（FR-20）。
"""

import dataclasses
import re
import threading
import time
from collections.abc import Callable

from lib.modbus_tags import (
    Bus,
    DataModel,
    DeviceDisconnected,
    DeviceStatus,
    Ticket,
    WaitTimeout,
    WriteFailed,
)
from lib.modbus_tags.config import ChannelConfig
from plc_motion.plc.port import PlcCommError, PlcPort
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.io_points import IoPoint, model_name
from plc_motion.settings.loader import AppConfig

BusFactory = Callable[[ChannelConfig], Bus]

# 函式庫讀取失敗原因的格式（channel.py）：用來改寫成「批次讀取 <Data Model> <起> ~ <迄>」
_READ_REASON = re.compile(r"read unit (\d+) (\w+) address (\d+) count (\d+)")

# 通道先把狀態設成 DISCONNECTED、之後才通知原因：poll 在這個空檔看到斷線時等原因的上限
_REASON_WAIT_S = 0.1


def _default_bus(channel: ChannelConfig) -> Bus:
    return Bus([channel], {})


def _describe_reason(reason: str) -> str:
    m = _READ_REASON.search(reason)
    if m is None:
        return reason
    unit, model, start, count = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    try:
        name = model_name(DataModel(model))
    except ValueError:
        return reason
    return f"批次讀取 Unit {unit} {name} {start} ~ {start + count - 1} 失敗：{reason}"


class TagsLink(PlcPort):
    def __init__(
        self,
        config: AppConfig,
        host: str,
        port: int,
        *,
        bus_factory: BusFactory | None = None,
    ):
        self._points = config.points
        self._device_name = config.channel.name
        self._endpoint = f"{host}:{port}"
        device = dataclasses.replace(
            config.channel.device, tcp_host=host, tcp_port=port, auto_reconnect=False
        )
        self._channel = dataclasses.replace(config.channel, device=device)
        self._bus_factory = bus_factory or _default_bus
        self._bus: Bus | None = None
        self._pending: list[tuple[Ticket, IoPoint]] = []
        self._reason = ""  # 通道最近一次回報的狀態原因（通道執行緒寫入）
        self._reason_event = threading.Event()  # 記下 _reason 後 set
        self._was_connected = False
        self._last_time = None  # 最近交出的快照的 Snapshot.time
        self._cycle_ms: float | None = None

    # ---- 連線 ----

    def connect(self) -> None:
        """建立並啟動新 Bus，不等連線結果；結果由 `poll()` 回報。既有的 Bus 先 `close()`。"""
        self.close()
        self._pending = []
        self._reason = ""
        self._reason_event.clear()
        self._was_connected = False
        self._last_time = None
        self._cycle_ms = None
        bus = self._bus_factory(self._channel)
        bus.on_status(self._on_status)
        self._bus = bus
        bus.start()

    def close(self, timeout: float | None = None) -> None:
        """等已排入的寫入有結果或逾時（`drain`），再停止 Bus；可重複呼叫。"""
        bus = self._bus
        if bus is None:
            return
        try:
            self.drain(self.drain_timeout_s if timeout is None else timeout)
        finally:
            self._bus = None
            self._pending = []
            bus.stop()

    @property
    def connected(self) -> bool:
        return self._status() is DeviceStatus.CONNECTED

    @property
    def connecting(self) -> bool:
        return self._status() is DeviceStatus.CONNECTING

    # ---- 讀取 ----

    def poll(self) -> Snapshot | None:
        """新的一輪快照；沒有新的一輪或尚未完成連線時回 None，寫入失敗或斷線 raise。"""
        bus = self._bus
        if bus is None:
            return None
        snap = bus.snapshot(self._device_name)  # 先取快照，再查失敗與狀態
        self._raise_failed_writes()
        status = bus.status(self._device_name)
        if status is DeviceStatus.CONNECTING:
            return None
        if status is not DeviceStatus.CONNECTED:
            raise self._lost_error()
        self._was_connected = True
        if snap is None or snap.time == self._last_time:
            return None
        previous, self._last_time = self._last_time, snap.time
        if previous is None:
            self._cycle_ms = None
        else:
            self._cycle_ms = (snap.time - previous).total_seconds() * 1000
        return Snapshot(self._points, {p.tag: snap[p.tag] for p in self._points.all()})

    @property
    def request_count(self) -> int:
        """一輪讀取的請求數（函式庫規劃的讀取範圍數）。"""
        if self._bus is None:
            return 0
        return self._bus.stats(self._device_name).requests

    @property
    def read_ms(self) -> float | None:
        """最近一輪讀取耗時（ms）；未連線時 None。"""
        if self._bus is None:
            return None
        return self._bus.stats(self._device_name).last_ms

    @property
    def cycle_ms(self) -> float | None:
        """最近交出的快照與前一份的時間差（ms）；連線後第一輪為 None。"""
        return self._cycle_ms

    # ---- 寫入 ----

    def write(self, tag: str, value: int | float | bool | str) -> None:
        """排入佇列後返回；結果由 `poll()` 檢查。範圍或型別錯誤原樣拋出。"""
        point = self._point(tag)
        ticket = self._submit(point, value)
        self._pending.append((ticket, point))

    def write_wait(self, tag: str, value: int | float | bool | str, timeout: float) -> None:
        """寫入並等結果；失敗或逾時 raise `PlcCommError`。"""
        point = self._point(tag)
        ticket = self._submit(point, value)
        try:
            ticket.wait(timeout)
        except WaitTimeout as e:
            raise PlcCommError(
                f"寫入在 {timeout} 秒內沒有結果", tag=tag, address=point.start_address
            ) from e
        except WriteFailed as e:
            raise PlcCommError(f"寫入失敗：{e}", tag=tag, address=point.start_address) from e

    def drain(self, timeout: float) -> bool:
        """等已排入的寫入都有結果，最多 `timeout` 秒；全部有結果回 True。不檢查成敗。"""
        deadline = time.monotonic() + timeout
        for ticket, _ in list(self._pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ticket.wait(remaining)
            except WaitTimeout, WriteFailed:
                pass
        return all(ticket.done() for ticket, _ in self._pending)

    @property
    def drain_timeout_s(self) -> float:
        """`close()` 等寫入的上限（秒）。

        寫入在下一輪開頭送出；最壞情況是先等進行中這一輪的讀取（每個請求最多一次判定逾時，
        逾時即斷線、佇列一起失敗），再等寫入本身，另加一個輪詢週期的餘裕。
        """
        d = self._channel.device
        judged_ms = d.timeout_ms * (d.retries + 1) + d.inter_request_delay_ms
        requests = self.request_count or 1
        return (judged_ms * (requests + 1) + d.interval_ms) / 1000

    # ---- 內部 ----

    def _on_status(self, device: str, status: DeviceStatus, reason: str) -> None:
        # 通道執行緒呼叫：只記原因，不做其他事
        if status is DeviceStatus.DISCONNECTED and reason != "stopped":
            self._reason = reason
            self._reason_event.set()

    def _status(self) -> DeviceStatus:
        if self._bus is None:
            return DeviceStatus.DISCONNECTED
        return self._bus.status(self._device_name)

    def _point(self, tag: str) -> IoPoint:
        try:
            return self._points.get(tag)
        except KeyError:
            raise PlcCommError("io_points 中沒有這個點", tag=tag) from None

    def _submit(self, point: IoPoint, value: int | float | bool | str) -> Ticket:
        bus = self._bus
        if bus is None:
            raise PlcCommError("尚未連線到 PLC", tag=point.tag, address=point.start_address)
        if isinstance(value, int) and not isinstance(value, bool) and point.scale:
            value = value / point.scale  # 原始值換成工程值；函式庫寫入時乘回
        try:
            return bus.write(f"{self._device_name}/{point.tag}", value)
        except DeviceDisconnected as e:
            raise PlcCommError(
                "未連線到 PLC，無法寫入", tag=point.tag, address=point.start_address
            ) from e

    def _raise_failed_writes(self) -> None:
        failed = [(t, p) for t, p in self._pending if t.done() and t.error is not None]
        self._pending = [(t, p) for t, p in self._pending if not t.done()]
        if failed:
            ticket, point = failed[0]
            raise PlcCommError(
                f"寫入失敗：{ticket.error}", tag=point.tag, address=point.start_address
            )

    def _lost_error(self) -> PlcCommError:
        if not self._reason:
            self._reason_event.wait(_REASON_WAIT_S)
        reason = _describe_reason(self._reason) if self._reason else "連線已中斷"
        if self._was_connected:
            return PlcCommError(f"與 PLC {self._endpoint} 的通訊中斷：{reason}")
        return PlcCommError(f"無法連線到 PLC {self._endpoint}：{reason}")
