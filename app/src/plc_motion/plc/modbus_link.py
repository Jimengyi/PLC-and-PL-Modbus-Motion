"""PlcPort 的 Modbus TCP adapter（spec FR-12、FR-13 偵測面）。

讀取依 `batching.plan_reads` 合併成數筆請求：有 read_ranges.toml 時照設定的範圍讀，
否則自動分組、只讀 io_points 定義的位址；
Position 點兩個 Word 低位在前、有號 I32。請求的 device_id 為點的 Unit ID。

通訊失敗一律 raise `PlcCommError`：
- 連線中斷、逾時：關閉連線，`connected` 變為 False；之後要重新 `connect()`
  （不讓 pymodbus 在下一次請求時自動重連，重新連線由呼叫端決定）。
- Modbus 例外回應：連線保留；讀取時改逐點重讀找出是哪個點，訊息含 Tag Name 與位址。
"""

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ModbusPDU

from plc_motion.codec import join_i32, split_i32
from plc_motion.plc.batching import ReadBlock, plan_reads
from plc_motion.plc.port import PlcCommError, PlcPort
from plc_motion.plc.snapshot import Snapshot
from plc_motion.settings.io_points import DataModel, IoPointTable, SensorType
from plc_motion.settings.read_ranges import ReadRange


class ModbusLink(PlcPort):
    def __init__(
        self,
        points: IoPointTable,
        host: str,
        port: int,
        timeout_ms: int,
        read_ranges: tuple[ReadRange, ...] | None = None,
    ):
        self._points = points
        self._host = host
        self._port = port
        self._blocks = plan_reads(points.all(), read_ranges)
        self._client = ModbusTcpClient(host, port=port, timeout=timeout_ms / 1000, retries=0)
        self._connected = False

    # ---- 連線 ----

    def connect(self) -> None:
        self._client.close()
        if not self._client.connect():
            self._connected = False
            raise PlcCommError(f"無法連線到 PLC {self._host}:{self._port}")
        self._connected = True

    def close(self) -> None:
        self._connected = False
        self._client.close()

    @property
    def request_count(self) -> int:
        """`read_all` 一輪送出的讀取請求數（合併後的區塊數）。"""
        return len(self._blocks)

    @property
    def connected(self) -> bool:
        return self._connected and self._client.connected

    # ---- PlcPort ----

    def read_all(self) -> Snapshot:
        values: dict[str, int | bool] = {}
        for block in self._blocks:
            first = block.points[0] if block.points else None
            rr = self._request(
                lambda b=block: self._read(b.unit_id, b.data_model, b.start, b.count),
                first.tag if first else None,
                block.start,
            )
            if rr.isError():
                self._raise_for_block(block, rr)
            values.update(_decode(block, rr))
        return Snapshot(self._points, values)

    def write(self, tag: str, value: int | bool) -> None:
        try:
            p = self._points.get(tag)
        except KeyError:
            raise PlcCommError("io_points 中沒有這個點", tag=tag) from None
        c, dev = self._client, p.unit_id
        if p.data_model == DataModel.COIL:
            call = lambda: c.write_coil(p.start_address, bool(value), device_id=dev)  # noqa: E731
        elif p.data_model == DataModel.HOLDING_REGISTER and p.sensor_type == SensorType.POSITION:
            words = list(split_i32(int(value)))
            call = lambda: c.write_registers(p.start_address, words, device_id=dev)  # noqa: E731
        elif p.data_model == DataModel.HOLDING_REGISTER:
            call = lambda: c.write_register(p.start_address, int(value), device_id=dev)  # noqa: E731
        else:
            raise PlcCommError(f"{p.data_model} 無法寫入", tag=tag, address=p.start_address)
        rr = self._request(call, p.tag, p.start_address)
        if rr.isError():
            raise PlcCommError(_exception_text(rr), tag=tag, address=p.start_address)

    # ---- 內部 ----

    def _read(self, unit_id: int, model: DataModel, start: int, count: int) -> ModbusPDU:
        c = self._client
        if model == DataModel.COIL:
            return c.read_coils(start, count=count, device_id=unit_id)
        if model == DataModel.DISCRETE_INPUT:
            return c.read_discrete_inputs(start, count=count, device_id=unit_id)
        return c.read_holding_registers(start, count=count, device_id=unit_id)

    def _request(self, call, tag: str | None, address: int) -> ModbusPDU:
        """送出一筆請求；未連線、連線中斷或逾時時關閉連線並 raise。"""
        if not self._connected:
            raise PlcCommError("尚未連線到 PLC", tag=tag, address=address)
        try:
            return call()
        except (ModbusException, OSError) as e:
            self.close()
            raise PlcCommError(f"PLC 通訊失敗：{e}", tag=tag, address=address) from e

    def _raise_for_block(self, block: ReadBlock, rr: ModbusPDU) -> None:
        """整塊讀取回例外時逐點重讀，找出回例外的點。

        每個點單獨讀都正常時，表示 PLC 不接受範圍內的某個空號位址（read_ranges.toml 設太寬）。
        """
        for p in block.points:
            single = self._request(
                lambda p=p: self._read(p.unit_id, p.data_model, p.start_address, p.length),
                p.tag,
                p.start_address,
            )
            if single.isError():
                raise PlcCommError(_exception_text(single), tag=p.tag, address=p.start_address)
        raise PlcCommError(
            f"{_exception_text(rr)}（批次讀取 {block.data_model} "
            f"{block.start} ~ {block.start + block.count - 1}）",
            address=block.start,
        )


def _decode(block: ReadBlock, rr: ModbusPDU) -> dict[str, int | bool]:
    out: dict[str, int | bool] = {}
    for p in block.points:
        i = p.start_address - block.start
        if block.data_model == DataModel.HOLDING_REGISTER:
            if p.sensor_type == SensorType.POSITION:
                out[p.tag] = join_i32(rr.registers[i], rr.registers[i + 1])
            else:
                out[p.tag] = rr.registers[i]
        else:
            out[p.tag] = bool(rr.bits[i])
    return out


def _exception_text(rr: ModbusPDU) -> str:
    code = getattr(rr, "exception_code", None)
    return f"PLC 回應 Modbus 例外（例外碼 {code}）"
