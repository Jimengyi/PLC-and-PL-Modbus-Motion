"""模擬 PLC 的 Modbus TCP server（spec FR-51、FR-53「中斷所有連線」、FR-56）。

pymodbus 的 asyncio server 與 `model.step()` 的固定週期掃描跑在同一條背景執行緒；
所有對 `PlcModel` 的存取（Modbus 請求、step、視窗操作）都要持有 `SimServer.lock`，
因為 PlcModel 本身沒有鎖。點位只提供 io_points.csv 定義者，依 (Unit ID, Data Model, 位址) 對應。

暫存器內容由 PlcModel 的原始值以 modbus_tags 的 `encode`／`decode` 換算（spec FR-33）：多 Word 點
依 `word_order`、`byte_swap` 排列（未設定為低位在前），不套用 `scale` 與 `invert`。暫存器位元點
（`N.bK`）共用同一暫存器，讀取時由各位元點組成；寫入整個暫存器（FC6、FC16、FC22）時只更新可寫
的位元點，改到唯讀位元的寫入回 Illegal Data Address。

讀取 io_points 沒定義的位址回 Illegal Data Address；但有 read_ranges.toml 時（spec FR-62），
範圍內的空號位址讀到 0，模擬實機 PLC 的元件本來就存在。寫入空號位址一律回例外。
"""

import asyncio
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pymodbus.constants import ExcCodes
from pymodbus.pdu import ModbusPDU
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import DataType, SimData, SimDevice

from lib.modbus_tags import Access, DataModel, TagInfo
from lib.modbus_tags.config import ReadRange
from lib.modbus_tags.datatypes import decode, encode
from plc_motion.plc.model import PlcModel, RawValue
from plc_motion.settings.io_points import IoPoint, IoPointTable
from plc_motion.sim.sim_log import SimLog

# model.step() 的掃描週期（秒）
STEP_INTERVAL = 0.02
_START_TIMEOUT = 5.0

# Function Code → Data Model；寫入類的 FC 在寫完後也會讀回該位址組成回應。
# Input Register 只有讀取的 FC 4，沒有寫入的 FC
_MODELS = {fc: DataModel.COIL for fc in (1, 5, 15)}
_MODELS[2] = DataModel.DISCRETE_INPUT
_MODELS[4] = DataModel.INPUT_REGISTER
_MODELS |= {fc: DataModel.HOLDING_REGISTER for fc in (3, 6, 16, 22, 23)}
_READ_FUNCTIONS = frozenset((1, 2, 3, 4, 23))
_BIT_MODELS = frozenset((DataModel.COIL, DataModel.DISCRETE_INPUT))


@dataclass(frozen=True)
class _Cell:
    """一個位址上的點：數值點與 Coil、Discrete Input 為單一點；暫存器位元為共用它的全部位元點。"""

    points: tuple[IoPoint, ...]
    word: int = 0  # 多 Word 點的第幾個 Word（依位址順序）

    @property
    def register_bits(self) -> bool:
        return self.points[0].bit is not None


def _raw_tag(p: IoPoint) -> TagInfo:
    """換算暫存器用的 TagInfo：保留 Word／位元組排列，不套用 `scale`、`invert`（模型存原始值）。"""
    return TagInfo(
        name=p.tag,
        display_name=p.display_name,
        description=p.description,
        data_model=p.data_model,
        data_type=p.data_type,
        access=p.access,
        address=p.start_address,
        bit=p.bit,
        length=p.length,
        unit_id=p.unit_id,
        scale=None,
        unit="",
        invert=False,
        word_order=p.word_order,
        byte_swap=p.byte_swap,
        extra={},
    )


class _Store:
    """pymodbus server 的 datastore：把位址對應到 io_points 的點，讀寫 PlcModel。"""

    def __init__(self, sim: SimServer):
        self._sim = sim
        self._cells: dict[tuple[int, DataModel, int], _Cell] = {}
        bits: dict[tuple[int, DataModel, int], list[IoPoint]] = {}
        for p in sim.points.all():
            if p.bit is not None:
                bits.setdefault((p.unit_id, p.data_model, p.start_address), []).append(p)
                continue
            for i in range(p.length):
                self._cells[(p.unit_id, p.data_model, p.start_address + i)] = _Cell((p,), i)
        for key, group in bits.items():
            self._cells[key] = _Cell(tuple(group))
        self._raw_tags = {p.tag: _raw_tag(p) for p in sim.points.all()}
        # read_ranges.toml 範圍內的位址：io_points 沒定義也可以讀（讀到 0）
        self._readable: set[tuple[int, DataModel, int]] = set()
        for r in sim.read_ranges or ():
            for a in range(r.start, r.start + r.count):
                self._readable.add((r.unit_id, r.data_model, a))
        units = {p.unit_id for p in sim.points.all()} | {k[0] for k in self._readable}
        self._unit_ids = sorted(units)

    async def _delay(self) -> None:
        if self._sim.delay_ms > 0:
            await asyncio.sleep(self._sim.delay_ms / 1000)

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
        if forced is not None and any(
            p.tag == forced for c in cells if c is not None for p in c.points
        ):
            return ExcCodes.DEVICE_FAILURE
        return cells

    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> list[int] | list[bool] | ExcCodes:
        await self._delay()
        model = _MODELS.get(func_code)
        cells = self._lookup(device_id, model, address, count, reading=True)
        if isinstance(cells, ExcCodes):
            return cells
        with self._sim.lock:
            return [self._cell_value(c, model) for c in cells]

    def _cell_value(self, cell: _Cell | None, model: DataModel) -> int | bool:
        if cell is None:  # 讀取範圍內的空號位址
            return False if model in _BIT_MODELS else 0
        read = self._sim.model.read
        if cell.register_bits:
            return sum(1 << p.bit for p in cell.points if read(p.tag))
        point = cell.points[0]
        if model in _BIT_MODELS:
            return bool(read(point.tag))
        return self._words(point)[cell.word]

    def _words(self, point: IoPoint) -> list[int]:
        """數值點目前的暫存器內容（依位址順序）。"""
        return list(encode(self._raw_tags[point.tag], self._sim.model.read(point.tag)))

    async def async_setValues(
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: Sequence[int] | Sequence[bool],
    ) -> ExcCodes | None:
        await self._delay()
        cells = self._lookup(device_id, _MODELS.get(func_code), address, len(values), False)
        if isinstance(cells, ExcCodes):
            return cells
        rejected = self._sim.rejected_writes
        if rejected and any(p.tag in rejected for c in cells for p in c.points):
            return ExcCodes.DEVICE_FAILURE
        if any(
            all(p.access != Access.READ_WRITE for p in c.points) for c in cells if c is not None
        ):
            return ExcCodes.ILLEGAL_ADDRESS
        sim = self._sim
        with sim.lock:
            changes = self._changes(cells, values)
            if changes is None:
                return ExcCodes.ILLEGAL_ADDRESS
            for tag, value in changes.items():
                sim.model.master_write(tag, value)
                sim.log.add("write", tag, sim.model.read(tag), _engineering_text(sim, tag))
        return None

    def _changes(
        self, cells: list[_Cell | None], values: Sequence[int] | Sequence[bool]
    ) -> dict[str, RawValue] | None:
        """寫入內容換成各點的新原始值，依位址順序；改到唯讀位元時回 None。

        暫存器位元只收值有改變的位元點：FC22 與讀改寫都會帶上整個暫存器，無從得知目標是哪一點。

        同一點的 Word 合併為一次寫入，沒寫到的 Word 沿用目前內容。
        """
        read = self._sim.model.read
        out: dict[str, RawValue | list[int]] = {}
        for cell, v in zip(cells, values, strict=True):
            assert cell is not None  # 寫入不接受空號位址（_lookup）
            if cell.register_bits:
                for p in cell.points:
                    bit = bool((int(v) >> p.bit) & 1)
                    if bit == read(p.tag):
                        continue  # 只有值改變的位元算寫入：同一暫存器的其他位元點不記錄
                    if p.access != Access.READ_WRITE:
                        return None
                    out[p.tag] = bit
                continue
            point = cell.points[0]
            if point.data_model in _BIT_MODELS:
                out[point.tag] = bool(v)
                continue
            words = out.get(point.tag)
            if not isinstance(words, list):
                words = out[point.tag] = self._words(point)
            words[cell.word] = int(v)
        return {
            tag: decode(self._raw_tags[tag], v) if isinstance(v, list) else v
            for tag, v in out.items()
        }


def _engineering_text(sim: SimServer, tag: str) -> str:
    """有 `scale` 的點在寫入紀錄附上工程值。"""
    point = sim.points.get(tag)
    if not point.scale:
        return ""
    unit = f" {point.unit}" if point.unit else ""
    return f"（{sim.model.read(tag) / point.scale}{unit}）"


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
        self.delay_ms = 0
        self.rejected_writes: frozenset[str] = frozenset()
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

    def set_delay(self, ms: float) -> None:
        """測試用：每個 Modbus 請求回應前延遲 ms 毫秒；0 解除。"""
        self.delay_ms = max(0.0, ms)

    def reject_writes(self, tags: Iterable[str]) -> None:
        """測試用：只拒絕這些點的寫入（Server Device Failure），讀取照常；空集合解除。

        例外碼不可用 Illegal Function：函式庫遇到它會改用 FC16 重試。
        """
        names = frozenset(tags)
        for t in names:
            self.points.get(t)  # 不存在的 Tag 直接 KeyError
        self.rejected_writes = names

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
