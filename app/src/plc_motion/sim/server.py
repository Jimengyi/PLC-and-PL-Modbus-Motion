"""模擬 PLC 的 Modbus TCP server（spec FR-51、FR-53「中斷所有連線」、FR-56）。

pymodbus 的 asyncio server 與 `model.step()` 的固定週期掃描跑在同一條背景執行緒；
所有對 `PlcModel` 的存取（Modbus 請求、step、視窗操作）都要持有 `SimServer.lock`，
因為 PlcModel 本身沒有鎖。點位只提供 io_points.csv 定義者，依 (Unit ID, Data Model, 位址) 對應；
Position 點占兩個 Word，低位在前。

讀取 io_points 沒定義的位址回 Illegal Data Address；但有 read_ranges.toml 時（spec FR-62），
範圍內的空號位址讀到 0，模擬實機 PLC 的元件本來就存在。寫入空號位址一律回例外。
"""

import asyncio
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

from pymodbus.constants import ExcCodes
from pymodbus.pdu import ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import DataType, SimData, SimDevice

from lib.modbus_tags import Access, DataModel
from lib.modbus_tags.config import ReadRange
from plc_motion.codec import from_raw, join_i32, split_i32
from plc_motion.plc.model import PlcModel
from plc_motion.settings.io_points import IoPoint, IoPointTable, is_position
from plc_motion.sim.sim_log import SimLog

# model.step() 的掃描週期（秒）
STEP_INTERVAL = 0.02
_START_TIMEOUT = 5.0

# Function Code → Data Model；寫入類的 FC 在寫完後也會讀回該位址組成回應。
# FC 4（Input Register）不在表內：io_points 沒有這種 Data Model，一律回 Illegal Data Address
_MODELS = {fc: DataModel.COIL for fc in (1, 5, 15)}
_MODELS[2] = DataModel.DISCRETE_INPUT
_MODELS |= {fc: DataModel.HOLDING_REGISTER for fc in (3, 6, 16, 22, 23)}
_READ_FUNCTIONS = frozenset((1, 2, 3, 4, 23))


@dataclass(frozen=True)
class _Cell:
    point: IoPoint
    word: int  # Position 的第幾個 Word（0 = 低位）；其餘型態為 0


class _Store:
    """pymodbus server 的 datastore：把位址對應到 io_points 的點，讀寫 PlcModel。"""

    def __init__(self, sim: SimServer):
        self._sim = sim
        self._cells: dict[tuple[int, DataModel, int], _Cell] = {}
        for p in sim.points.all():
            for i in range(p.length):
                self._cells[(p.unit_id, p.data_model, p.start_address + i)] = _Cell(p, i)
        # read_ranges.toml 範圍內的位址：io_points 沒定義也可以讀（讀到 0）
        self._readable: set[tuple[int, DataModel, int]] = set()
        for r in sim.read_ranges or ():
            for a in range(r.start, r.start + r.count):
                self._readable.add((r.unit_id, r.data_model, a))
        units = {p.unit_id for p in sim.points.all()} | {k[0] for k in self._readable}
        self._unit_ids = sorted(units)

    def device_ids(self) -> list[int]:
        return list(self._unit_ids)

    def _lookup(
        self, device_id: int, model: DataModel | None, address: int, count: int, reading: bool
    ) -> list[_Cell | None] | ExcCodes:
        """回傳各位址對應的點；None 為讀取範圍內的空號位址（只有讀取會出現）。"""
        if model is None:
            return ExcCodes.ILLEGAL_ADDRESS
        cells: list[_Cell | None] = []
        for a in range(address, address + count):
            key = (device_id, model, a)
            cell = self._cells.get(key)
            if cell is None and not (reading and key in self._readable):
                return ExcCodes.ILLEGAL_ADDRESS
            cells.append(cell)
        forced = self._sim.forced_tag
        if forced is not None and any(c is not None and c.point.tag == forced for c in cells):
            return ExcCodes.DEVICE_FAILURE
        return cells

    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> list[int] | list[bool] | ExcCodes:
        model = _MODELS.get(func_code)
        cells = self._lookup(device_id, model, address, count, reading=True)
        if isinstance(cells, ExcCodes):
            return cells
        with self._sim.lock:
            return [self._cell_value(c, model) for c in cells]

    def _cell_value(self, cell: _Cell | None, model: DataModel) -> int | bool:
        if cell is None:  # 讀取範圍內的空號位址
            return 0 if model == DataModel.HOLDING_REGISTER else False
        raw = self._sim.model.read(cell.point.tag)
        if is_position(cell.point):
            return split_i32(int(raw))[cell.word]
        if cell.point.data_model in (DataModel.COIL, DataModel.DISCRETE_INPUT):
            return bool(raw)
        return int(raw)

    async def async_setValues(
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: Sequence[int] | Sequence[bool],
    ) -> ExcCodes | None:
        cells = self._lookup(device_id, _MODELS.get(func_code), address, len(values), False)
        if isinstance(cells, ExcCodes):
            return cells
        if any(c.point.access != Access.READ_WRITE for c in cells):
            return ExcCodes.ILLEGAL_ADDRESS
        # 同一點的 Word 合併為一次寫入；依位址順序
        words: dict[str, dict[int, int | bool]] = {}
        for cell, v in zip(cells, values, strict=True):
            words.setdefault(cell.point.tag, {})[cell.word] = v
        sim = self._sim
        with sim.lock:
            for tag, parts in words.items():
                point = sim.points.get(tag)
                if is_position(point):
                    low, high = split_i32(int(sim.model.read(tag)))
                    value = join_i32(int(parts.get(0, low)), int(parts.get(1, high)))
                    text = f"（{from_raw(value, point.scale)} {point.unit}）"
                else:
                    value, text = parts[0], ""
                sim.model.master_write(tag, value)
                sim.log.add("write", tag, sim.model.read(tag), text)
        return None


class _Handler(ServerRequestHandler):
    """每條 client 連線一個；回報連線建立與中斷給 SimServer。"""

    def callback_connected(self) -> None:
        super().callback_connected()
        peer = self.transport.get_extra_info("peername") if self.transport else None
        self.server.sim._on_connect(self, peer)

    def callback_disconnected(self, exc: Exception | None) -> None:
        super().callback_disconnected(exc)
        self.server.sim._on_disconnect(self)


class _TcpServer(ModbusTcpServer):
    def __init__(self, sim: SimServer, address: tuple[str, int]):
        # 基底類別需要一個合法的 context；建好後換成自己的 _Store
        placeholder = SimDevice(0, simdata=[SimData(0, datatype=DataType.REGISTERS)])
        super().__init__(placeholder, address=address, trace_pdu=self._trace_pdu)
        self.sim = sim
        self.context = _Store(sim)

    def callback_new_connection(self):
        return _Handler(self, self.trace_packet, self.trace_pdu, self.trace_connect)

    def _trace_pdu(self, sending: bool, pdu: ModbusPDU) -> ModbusPDU:
        if not sending and pdu.function_code in _READ_FUNCTIONS:
            self.sim.log.count_read()
        return pdu


class SimServer:
    def __init__(
        self,
        model: PlcModel,
        points: IoPointTable,
        host: str = "127.0.0.1",
        port: int = 5020,
        read_ranges: tuple[ReadRange, ...] | None = None,
    ):
        self.model = model
        self.points = points
        self.read_ranges = read_ranges
        self.host = host
        self.port = port  # port 0 時 start() 後為實際監聽的埠號
        self.log = SimLog()
        self.lock = threading.RLock()
        self.forced_tag: str | None = None
        self._connections: dict[int, str] = {}
        self._conn_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._tcp: _TcpServer | None = None
        model.add_listener(self.log.on_model_change)

    # ---- 生命週期 ----

    def start(self) -> None:
        """啟動背景執行緒：Modbus server 開始監聽並以固定週期 model.step()；監聽失敗時 raise。"""
        if self._thread is not None:
            raise RuntimeError("SimServer 已啟動")
        ready = threading.Event()
        errors: list[BaseException] = []
        self._thread = threading.Thread(
            target=self._run, args=(ready, errors), name="plc-sim-server", daemon=True
        )
        self._thread.start()
        if not ready.wait(_START_TIMEOUT):
            errors.append(TimeoutError("模擬 PLC 啟動逾時"))
        if errors:
            self.stop()
            raise errors[0]

    def stop(self) -> None:
        thread, loop, stop_event = self._thread, self._loop, self._stop_event
        if thread is None:
            return
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        thread.join(_START_TIMEOUT)
        self._thread = self._loop = self._stop_event = self._tcp = None

    def disconnect_all(self) -> None:
        """關閉現有連線；點位與模擬狀態保留，仍接受新連線。"""
        loop = self._loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._disconnect_all(), loop).result(_START_TIMEOUT)

    def force_exception(self, tag: str | None) -> None:
        """測試用：指定點的讀寫回 Modbus 例外（Server Device Failure）；None 解除。"""
        if tag is not None:
            self.points.get(tag)  # 不存在的 Tag 直接 KeyError
        self.forced_tag = tag

    # ---- 背景執行緒 ----

    def _run(self, ready: threading.Event, errors: list[BaseException]) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._main(loop, ready, errors))
        finally:
            loop.close()

    async def _main(
        self,
        loop: asyncio.AbstractEventLoop,
        ready: threading.Event,
        errors: list[BaseException],
    ) -> None:
        stop_event = asyncio.Event()
        try:
            tcp = _TcpServer(self, (self.host, self.port))
            await tcp.serve_forever(background=True)
        except Exception as e:  # 交給 start() 的呼叫端
            errors.append(e)
            ready.set()
            return
        if self.port == 0 and tcp.transport is not None:
            self.port = tcp.transport.sockets[0].getsockname()[1]
        self._tcp, self._loop, self._stop_event = tcp, loop, stop_event
        ready.set()

        last = time.monotonic()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), STEP_INTERVAL)
            except TimeoutError:
                pass
            now = time.monotonic()
            with self.lock:
                self.model.step(now - last)
            last = now
        await tcp.shutdown()

    async def _disconnect_all(self) -> None:
        if self._tcp is None:
            return
        for handler in list(self._tcp.active_connections.values()):
            handler.close()
            # 由 server 端關閉時 pymodbus 不會呼叫 callback_disconnected
            self._on_disconnect(handler)

    def _on_connect(self, handler: object, peer) -> None:
        text = f"{peer[0]}:{peer[1]}" if peer else "未知位址"
        with self._conn_lock:
            self._connections[id(handler)] = text
        self.log.add("connect", text=text)

    def _on_disconnect(self, handler: object) -> None:
        with self._conn_lock:
            text = self._connections.pop(id(handler), None)
        if text is not None:
            self.log.add("disconnect", text=text)
