---
type: plan
created: 2026-10-31
tags: [modbus, pyside6, motion]
slug: modbus-master-app
goal: "交付七軸 Modbus TCP Master 桌面程式 plc-motion 與開發用模擬 PLC plc-sim，依規格書交握控制七軸，設定全部外部化"
status: approved
approved_by: user
related: ["[[spec-modbus-master-app]]"]
---

# 七軸 Modbus Master 程式 實作計畫

Goal: 交付七軸 Modbus TCP Master 桌面程式 plc-motion 與開發用模擬 PLC plc-sim，依規格書交握控制七軸，設定全部外部化

Architecture: 所有程式碼在 `app/`（uv 專案，套件 `plc_motion`，src layout），指令一律在 `app/` 下執行。主程式分四層：設定（`plc_motion.settings`）→ PLC 讀寫介面 `PlcPort`（兩個 adapter：`ModbusLink` 走 pymodbus TCP、`ModelPort` 直接包 `PlcModel` 給單元測試）→ 交握引擎 `MotionEngine`（純邏輯、時間由注入的 clock 提供、不碰 Qt）→ UI（PySide6，只透過 `PlcWorker` 的 signal／slot 與引擎往來）。所有 Modbus I/O 與 `MotionEngine` 呼叫都在 `PlcWorker` 的背景執行緒內，UI 執行緒不做任何 Modbus I/O。模擬 PLC 的行為只有一份實作 `PlcModel`，同時給 `plc-sim`（包上 pymodbus server）與單元測試使用。介面文字一律繁體中文。

Verification: `cd app && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

Base branch: main @ c3aa3a1

## Spec coverage

| FR／AC | Task |
|---|---|
| FR-1、FR-7、FR-9 | Task 2 |
| FR-2、FR-3、FR-4、FR-5、FR-8、FR-45（存檔面）、FR-40（存檔面） | Task 2 |
| FR-6、FR-35（匯入匯出驗證）、FR-36（存檔面） | Task 3 |
| FR-55 | Task 4 |
| FR-20（換算） | Task 1 |
| FR-52、FR-53（行為面） | Task 5 |
| FR-51、FR-56、FR-53（中斷所有連線） | Task 6 |
| FR-12、FR-13（偵測面） | Task 7 |
| FR-15 ~ FR-23、FR-19、FR-20（送出面） | Task 8 |
| FR-24 ~ FR-27（引擎面）、FR-57、FR-58（引擎面）、FR-13（操作結束面） | Task 9 |
| FR-10、FR-11、FR-12、FR-14、FR-27（立即寫）、FR-48、FR-49、FR-50（背景面） | Task 10 |
| FR-8（錯誤視窗）、FR-41 ~ FR-47、FR-50（UI 面） | Task 11 |
| FR-28 ~ FR-33、FR-27（UI 面）、FR-16（反灰） | Task 12 |
| FR-34 ~ FR-36 | Task 13 |
| FR-37 ~ FR-40 | Task 14 |
| FR-57、FR-58、FR-13（UI 提示） | Task 15 |
| FR-53（視窗） | Task 16 |
| FR-54 | Task 17 |
| AC-1、AC-6 ~ AC-24、AC-37、AC-39 ~ AC-42 | Task 18 |
| AC-2 ~ AC-5、AC-25 ~ AC-36、AC-38 | Task 19 |
| FR-21、FR-22、FR-25（畫面呈現）、FR-49（設定檔錯誤） | Task 11 |

---

## Tasks

### Task 1: 數值換算 codec [x]

Files:
- Create: `app/src/plc_motion/codec.py`
- Test: `app/tests/test_codec.py`

Difficulty: 簡單
Depends on: —
Related: `README.md`（repo 根）— I32 低位在前、有號、倍率表與範例

介面契約：（契約層）

```python
def split_i32(value: int) -> tuple[int, int]: ...   # 回傳 (low_word, high_word)，皆為 0~65535
def join_i32(low: int, high: int) -> int: ...        # 有號 I32
def to_raw(value: float, scale: int) -> int: ...     # 四捨五入為 I32；超出 I32 範圍 raise ValueError
def from_raw(raw: int, scale: int) -> float: ...
def decimals_for(scale: int) -> int: ...             # 10000 → 4、100 → 2
```

測試 seam：上列五個函式的回傳值

驗收：（契約層）
- [x] −100 mm（倍率 10000）→ `split_i32(to_raw(-100, 10000)) == (48576, 65520)`；`join_i32(48576, 65520) == -1000000`
- [x] README「I32 拆分範例」表的 7 列逐列成立（含 Z = −2.3456 mm → (42080, 65535)）
- [x] −2.5 mm 與 7.5 mm 的換算無浮點誤差造成的 ±1（例：`to_raw(7.5, 10000) == 75000`、`to_raw(150.25, 100) == 15025`）
- [x] Run: `cd app && uv run pytest tests/test_codec.py -v` → 全綠

---

### Task 2: 設定載入與啟動檢查（device、axes、io_points） [x]

Files:
- Create: `app/src/plc_motion/settings/__init__.py`
- Create: `app/src/plc_motion/settings/paths.py`
- Create: `app/src/plc_motion/settings/csvio.py`
- Create: `app/src/plc_motion/settings/device.py`
- Create: `app/src/plc_motion/settings/axes.py`
- Create: `app/src/plc_motion/settings/io_points.py`
- Create: `app/src/plc_motion/settings/loader.py`
- Modify: `app/pyproject.toml`（若需要保留註解的 TOML 寫入套件，例如 tomlkit；授權須為商用免費）
- Test: `app/tests/settings/test_io_points.py`、`app/tests/settings/test_device_axes.py`、`app/tests/settings/test_loader.py`
- Create: `app/tests/conftest.py`（共用 fixture：`config_dir` 產生一份完整、合法的七軸設定資料夾於 tmp_path，後續 task 一律沿用）

Difficulty: 複雜
Depends on: Task 1
Related: `docs/specs/spec-modbus-master-app.md` FR-1 ~ FR-9（含 FR-8 型態表）、FR-40、FR-45；`docs/七軸_Modbus位址表.md`

介面契約：（契約層）

```python
class ConfigErrors(Exception):
    errors: list[str]          # 全部錯誤，人看得懂的繁中訊息，含檔名與列號/Tag

def resolve_config_dir() -> Path: ...  # 打包後：exe 同層 config；原始碼執行：app/config
def load_config(config_dir: Path) -> AppConfig: ...   # 任一檢查不過 raise ConfigErrors（收集全部錯誤後才 raise）

@dataclass
class AppConfig:
    config_dir: Path
    device: DeviceSettings
    axes: dict[int, AxisSettings]        # key 1~7
    points: IoPointTable
    positions: PositionTable             # Task 3 提供；本 task 先以 Task 3 的介面名稱預留，Task 3 接上

class IoPointTable:
    def get(self, tag: str) -> IoPoint: ...
    def axis_tag(self, name: str, axis: int) -> IoPoint: ...   # FR-5 命名規則 "<名稱> <軸號>"
    def all(self) -> list[IoPoint]: ...                        # 依檔案列順序
    def save_check_status(self, tag: str, status: str) -> None: ...  # 只改該列該欄
    def reset_all_check_status(self) -> None: ...

class DeviceSettings:
    def save_theme(self, theme: str) -> None: ...   # 只改主題值，保留其餘內容與註解
```

`IoPoint` 至少帶 FR-4 全部欄位，以及解析後的 `scale`、`unit`（Position）、`is_b_contact`（Attributes 為 `ON`）。

測試 seam：`load_config()` 的回傳值或 `ConfigErrors.errors`；寫回函式以檔案位元組為斷言對象

驗收：（契約層）
- [x] FR-8 每一種錯誤各一個案例都產生對應訊息；型態表的 Data Model、Sensor Type、Access、Address Length、Attributes 各至少一例；`Positive Limit 1` 缺 `ON` 被擋；同時兩種錯誤時兩條都在 `errors` 中
- [x] Big5 編碼、UTF-8 無 BOM、UTF-8 含 BOM 三種 `io_points.csv` 皆可載入且 Display Name 中文正確
- [x] `save_check_status` 之後檔案為 UTF-8 含 BOM；原檔為 UTF-8 時只有該列 Check Status 改變，其餘位元組（除 BOM 與該欄外）與列順序不變；原檔為 Big5 時重新載入後內容與列順序不變
- [x] `save_theme` 之後 `device.toml` 只有主題值改變，註解與其他內容逐字保留
- [x] 寫回採原子替換：模擬寫入途中例外時原檔內容不變
- [x] `tests/conftest.py` 的 `config_dir` fixture 產生的資料夾可被 `load_config` 載入（七軸、全部必要 Tag、Unit ID 0）
- [x] Run: `cd app && uv run pytest tests/settings -v` → 全綠

禁區：
- 不讀、不解析 `docs/` 下的 md 檔；FR-8 型態表寫成程式內的常數表

留給實作：
- TOML 保留註解的寫法（加套件或文字替換）；邊界：AC-32「其餘內容（含註解）不變」成立，新增套件須商用免費授權

---

### Task 3: 點位資料表 positions [x]

Files:
- Create: `app/src/plc_motion/settings/positions.py`
- Modify: `app/src/plc_motion/settings/loader.py`（接上 `AppConfig.positions`）
- Test: `app/tests/settings/test_positions.py`
- Modify: `app/tests/settings/test_device_axes.py`、`app/tests/settings/test_io_points.py`（資料夾檔案清單加入自動建立的 `positions.csv`）

Difficulty: 簡單
Depends on: Task 2
Related: spec FR-6、FR-34 ~ FR-36

介面契約：（契約層）

```python
class PositionTable:
    POINT_COUNT = 200
    def rows(self) -> list[PositionRow]: ...          # P001~P200
    def update(self, point_id: str, **fields) -> None: ...   # 立即存檔（原子替換）
    def import_file(self, path: Path) -> list[str]: ...      # 回傳錯誤；有錯誤時表與檔皆不變
    def export_file(self, path: Path) -> None: ...
```

`PositionRow` 欄位：point_id、name、x、y、z、rx、ry、rz、lift、description。

測試 seam：`PositionTable` 的方法回傳值與 `positions.csv` 檔案內容

驗收：（契約層）
- [x] `positions.csv` 不存在時 `load_config` 建立含 P001 ~ P200 的檔案，座標皆 0
- [x] 匯入：150 列、某座標為文字、缺欄、#Point 不是 P001 ~ P200 → 回傳錯誤、表與檔不變；合法檔 → 整份取代並存檔
- [x] 匯出檔可被 `import_file` 原樣匯回
- [x] Run: `cd app && uv run pytest tests/settings/test_positions.py -v` → 全綠

---

### Task 4: 預設設定檔 [x]

Files:
- Create: `app/config/device.toml`
- Create: `app/config/axes.toml`
- Create: `app/config/io_points.csv`
- Create: `app/config/positions.csv`
- Test: `app/tests/settings/test_default_config.py`

Difficulty: 簡單
Depends on: Task 3
Related: spec FR-55、FR-2、FR-3；`docs/七軸_Modbus位址表.md`；使用者參考檔 `C:\Users\jimze\Desktop\Project\勝品\Project\Settings\Modbus Master\IO_Data_Settings_01.conf`（Tab 分隔、UTF-8，只讀不改）

驗收：（契約層）
- [x] `load_config(app/config)` 成功
- [x] 7 軸 × 18 個必要 Tag ＋ `Emergency Stop` 皆在，位址與位址表逐一相符；倍率 X/Y/Z `10000,mm`、RX/RY/RZ `100,deg`、升降 `100,mm`
- [x] `JOG Plus n`、`JOG Minus n`、`Request n` 的 Visible 為 X，其餘為 O；Check Status 全為 Unchecked；Unit ID 全為 0（同參考檔）
- [x] `device.toml`：192.168.1.105、502、連線逾時 2000、其餘為 FR-2 預設值；每個鍵上方有繁中註解
- [x] `axes.toml` 名稱依序 X、Y、Z、RX、RY、RZ、升降；軟體極限與步進距離有「待依機構行程確認」註解
- [x] Run: `cd app && uv run pytest tests/settings/test_default_config.py -v` → 全綠

禁區：
- 不修改使用者的 LabVIEW 參考檔

---

### Task 5: PLC 讀寫介面、快照與 PLC 行為模型 [x]

Files:
- Create: `app/src/plc_motion/plc/__init__.py`
- Create: `app/src/plc_motion/plc/port.py`
- Create: `app/src/plc_motion/plc/snapshot.py`
- Create: `app/src/plc_motion/plc/model.py`
- Test: `app/tests/plc/test_model.py`、`app/tests/plc/test_snapshot.py`

Difficulty: 複雜
Depends on: Task 2
Related: `docs/PLC_PC_Modbus_通訊規格.md`（全部，PLC 端行為的依據；含 2.1、2.2 的 PC_Action_Code 清零修訂）；spec FR-52、FR-53

介面契約：（契約層）

```python
class PlcCommError(Exception): ...        # 訊息含 Tag Name 與位址（若可得）

class PlcPort(Protocol):
    def read_all(self) -> Snapshot: ...
    def write(self, tag: str, value: int | bool) -> None: ...   # Position 點傳 raw I32，adapter 負責拆 Word

class Snapshot:
    def raw(self, tag: str) -> int | bool: ...
    def value(self, tag: str) -> float | int | bool: ...   # Position 以倍率換算後的實際值
    def axis(self, n: int) -> AxisState: ...                # ready、busy、home_finish、reply、request、alarm_code、
                                                            # plc_action_code、pc_action_code、position、servo、
                                                            # pos_limit_triggered、neg_limit_triggered、home_sensor、
                                                            # jog_plus、jog_minus
    estop_ok: bool                                          # Emergency Stop 未觸發（依 B 接點判讀）

class PlcModel:
    def __init__(self, points: IoPointTable): ...
    def step(self, dt: float) -> None: ...
    def read(self, tag: str) -> int | bool: ...
    def master_write(self, tag: str, value: int | bool) -> None: ...   # 模擬 Master 的寫入
    # 視窗與測試用的操作（FR-53）
    def set_limit(self, axis: int, which: str, triggered: bool) -> None: ...   # which: "pos" | "neg" | "home"
    def set_estop(self, pressed: bool) -> None: ...
    def set_position(self, axis: int, value: float) -> None: ...
    def inject_alarm(self, axis: int, code: int, persistent: bool) -> None: ...
    def set_persistent(self, axis: int, persistent: bool) -> None: ...
    def set_fault(self, axis: int, no_reply: bool, no_copy: bool) -> None: ...
    speed_factor: float
    def add_listener(self, fn: Callable[[str, int | bool], None]) -> None: ...   # 狀態變化事件（FR-56 用）

class ModelPort(PlcPort):
    def __init__(self, model: PlcModel): ...
```

已落地的額外公開介面：`PlcModel.points`、`PlcModel.has_tag(tag)`、`snapshot.is_triggered(point, raw)`；`PlcCommError(message, tag=None, address=None)`。

測試 seam：`PlcModel` 透過 `master_write`／`step`／`read` 觀測；`Snapshot` 以 `ModelPort.read_all()` 取得

驗收：（契約層）
- [x] FR-52 每一條各至少一個案例（Servo→Ready、待機複製命令碼含複製 0、Request 上升緣→Busy/Reply、Request=0→Reply=0 後才移動、Code 2 移到目標、Code 1 先 Busy=0 後 Find Home Finish=1 且中間間隔長於 0.2 秒、JOG 持續移動與停止、移動中碰極限 Alarm 2/3、原點復歸中碰極限不報、待機朝已觸發極限下命令報 Alarm、動作中 Servo OFF Alarm 4、急停、Reset Alarm 排除與未排除、停在極限上可清除）
- [x] 注入 Alarm：動作中立即停止、Busy=0、Ready=0；勾「原因持續」時 Reset Alarm 不清除，取消後可清除
- [x] 故障「不回 Reply」「不複製命令碼」逐軸生效
- [x] B 接點判讀依 `IoPoint.is_b_contact`：極限、急停的 raw 值與 `Snapshot` 的 triggered／estop_ok 相反
- [x] Run: `cd app && uv run pytest tests/plc -v` → 全綠

留給實作：
- 模擬最大速度與原點復歸延遲；邊界：Busy=0 到 Find Home Finish=1 的間隔長於一個輪詢週期（200 ms）

---

### Task 6: 模擬 PLC 伺服器與紀錄 [x]

Files:
- Create: `app/src/plc_motion/sim/__init__.py`
- Create: `app/src/plc_motion/sim/server.py`
- Create: `app/src/plc_motion/sim/sim_log.py`
- Test: `app/tests/sim/test_server.py`

Difficulty: 複雜
Depends on: Task 5
Related: spec FR-51、FR-53（中斷所有連線）、FR-56；pymodbus 3.16 server API

介面契約：（契約層）

```python
class SimLog:
    def entries(self) -> list[LogEntry]: ...     # kind: "write" | "connect" | "disconnect" | "state"（"read" 不逐筆入列，只出現在匯出摘要）
    def last_read_time(self) -> float | None: ...
    def read_count(self) -> int: ...
    def export(self, path: Path) -> None: ...

class SimServer:
    def __init__(self, model: PlcModel, points: IoPointTable, host: str = "127.0.0.1", port: int = 5020): ...
    def start(self) -> None: ...      # 背景執行緒：Modbus server ＋ 以固定週期 model.step()
    def stop(self) -> None: ...
    def disconnect_all(self) -> None: ...   # 關閉現有連線，保留狀態，仍接受新連線
    def force_exception(self, tag: str | None) -> None: ...   # 測試用：指定點讀寫回 Modbus 例外
    log: SimLog
```

已落地的額外公開介面：`SimServer.model`、`points`、`port`（傳 0 時 `start()` 後為實際埠號）、`lock`（RLock，直接存取 model 時須持有）、`forced_tag`；`LogEntry(time, kind, tag, value, text)` 與 `format()`。

測試 seam：以 pymodbus `ModbusTcpClient` 直接連 `SimServer` 讀寫，斷言回應值與 `SimLog` 內容

驗收：（契約層）
- [x] io_points 定義的每個點（依 Data Model 與 Unit ID）可讀；Read Write 點可寫，寫入轉成 `model.master_write` 並記入 log（毫秒時間、Tag Name、值）；Position 點兩個 Word 寫入後組成正確 I32
- [x] 連線、斷線、讀取次數與最近讀取時間、FR-56 列出的狀態變化（含 PLC Action Code）皆有紀錄，可匯出文字檔
- [x] `disconnect_all()` 後既有 client 斷線、model 狀態不變、新 client 可再連
- [x] 讀寫 io_points 未定義的位址回 Modbus 例外（Illegal Data Address）；提供測試用手段 `SimServer.force_exception(tag: str | None)` 讓指定點的讀寫回例外（None 解除）
- [x] Unit ID 0 的點可正常讀寫
- [x] Run: `cd app && uv run pytest tests/sim/test_server.py -v` → 全綠

---

### Task 7: Modbus 連線 adapter [x]

Files:
- Create: `app/src/plc_motion/plc/modbus_link.py`
- Create: `app/src/plc_motion/plc/batching.py`
- Test: `app/tests/plc/test_modbus_link.py`、`app/tests/plc/test_batching.py`

Difficulty: 複雜
Depends on: Task 6
Related: spec FR-12、FR-13、Open Questions「批次讀取如何分組」

介面契約：（契約層）

```python
class ModbusLink(PlcPort):
    def __init__(self, points: IoPointTable, host: str, port: int, timeout_ms: int): ...
    def connect(self) -> None: ...     # 失敗 raise PlcCommError
    def close(self) -> None: ...
    @property
    def connected(self) -> bool: ...
```

測試 seam：`ModbusLink` 對 Task 6 的 `SimServer`（真 TCP、localhost）讀寫；分組邏輯另以純函式測

驗收：（契約層）
- [x] `read_all()` 讀到 io_points 全部點（含 Visible = X），值與 `PlcModel.read` 一致；讀取請求次數少於點數（有合併）
- [x] 不讀取 io_points 沒定義的位址（SimServer 對表外位址回例外，`read_all` 不得因此失敗）
- [x] 寫 Position 點時低位 Word 在前；寫 −1000000 後 server 端兩個 Word 為 48576、65520
- [x] server 停止、讀寫逾時、Modbus 例外回應（以 `SimServer.force_exception` 製造）時 raise `PlcCommError`，例外回應的訊息含 Tag Name 與位址
- [x] Run: `cd app && uv run pytest tests/plc/test_modbus_link.py tests/plc/test_batching.py -v` → 全綠

留給實作：
- 批次分組方式；邊界：每輪讀到全部點，且不讀表外位址

---

### Task 8: 交握引擎（移動、原點復歸、鎖定、前置檢查） [x]

Files:
- Create: `app/src/plc_motion/engine/__init__.py`
- Create: `app/src/plc_motion/engine/handshake.py`
- Create: `app/src/plc_motion/engine/engine.py`
- Create: `app/src/plc_motion/engine/events.py`
- Test: `app/tests/engine/test_move.py`、`app/tests/engine/test_home.py`、`app/tests/engine/test_lock.py`

Difficulty: 複雜
Depends on: Task 5
Related: spec FR-15 ~ FR-23（FR-18 的寫回規則）；`docs/PLC_PC_Modbus_通訊規格.md` 2.1 ~ 2.3、3.4

介面契約：（契約層）

```python
class MotionEngine:
    def __init__(self, config: AppConfig, port: PlcPort, clock: Callable[[], float]): ...
    def tick(self, snapshot: Snapshot) -> None: ...     # 每輪輪詢後呼叫一次，推進各軸狀態機並寫入
    def on_connected(self) -> None: ...        # 初值為未連線；之後第一次 tick 完成前一律鎖定
    def on_disconnected(self) -> None: ...     # 進行中操作逐軸結果為「通訊中斷」
    # 命令：回傳空 list 表示受理；否則為拒絕原因（全部不符的軸與項目），且未寫入任何東西
    def move(self, targets: dict[int, float], relative: bool, speed: int) -> list[str]: ...
    def step(self, axis: int, delta: float, speed: int) -> list[str]: ...
    def home(self, axes: set[int]) -> list[str]: ...
    @property
    def locked(self) -> bool: ...
    @property
    def lock_reasons(self) -> list[str]: ...
    def add_listener(self, fn: Callable[[EngineEvent], None]) -> None: ...
```

`EngineEvent` 至少含：`OperationStarted`（操作種類、軸組、參數）、`OperationFinished`（操作種類、逐軸結果：完成、Alarm(code)、急停、逾時(哪個等待點)、通訊中斷、未執行、復歸成功、復歸失敗）、`CommandRejected`（命令名稱、拒絕原因 list，與命令回傳值相同）、`Notice`（文字）。命令被拒時同時回傳原因並發 `CommandRejected`。

測試 seam：`MotionEngine` 公開方法＋事件；PLC 端以 `ModelPort(PlcModel)` 扮演，時間由測試推進（clock 與 `model.step` 同步）；寫入順序由測試端包一層記錄寫入的 `PlcPort` 觀測（不改 `plc/port.py`），或觀測 model 狀態

驗收：（契約層）
- [x] 單軸 Code 2 寫入順序完全符合 FR-18（含 Request=0 同時 PC Action Code=0）；Code 1 不寫 Target Position 與 Speed
- [x] 同一軸連續兩次 Code 2：第二次在 PLC Action Code 由 0 變 2 之後才寫 Request=1
- [x] 三個等待點逾時（預設 5 秒）各一案例；逾時、等待中 Alarm、等待中急停結束時都寫回 Request=0 與 PC Action Code=0；等完成不逾時
- [x] 前置檢查：任一軸不符 → 回傳全部不符的軸與項目、零寫入（含未連線、Ready=0、軟體極限、Find Home Finish=0、急停、Alarm、Busy）
- [x] 相對移動與步進：目標＝最新快照位置＋移動量，只送 Code 2；`to_raw` 換算（X −2.5 mm 自 10 mm → 75000；升降 +1.25 → +125）
- [x] 多軸同時：各軸交握並行；一軸 Alarm 其他軸繼續；全部軸有結果才 `OperationFinished`
- [x] 原點復歸含第 7 軸：第 7 軸 Request=1 早於其他軸任何寫入；第 7 軸完成（含 Find Home Finish=1）後其餘軸同時開始；第 7 軸失敗 → 其餘「未執行」且零寫入；Busy=0 但 Find Home Finish=0 期間不判完成、仍鎖定
- [x] 鎖定條件：引擎建立後未呼叫 `on_connected`、`on_connected` 後第一次 tick 前、操作進行中、任一軸 Busy=1 時 `locked` 為真且命令被拒（發 `CommandRejected`）；操作結束且全部 Busy=0 後解除
- [x] `on_disconnected` 時進行中操作逐軸結果為「通訊中斷」
- [x] Run: `cd app && uv run pytest tests/engine/test_move.py tests/engine/test_home.py tests/engine/test_lock.py -v` → 全綠

禁區：
- `plc_motion.engine` 不 import PySide6；除了經由 `PlcPort`，不做任何 I/O

---

### Task 9: 交握引擎（Servo、Reset、JOG、IO 寫入、連線事件與殘留交握） [x]

Files:
- Modify: `app/src/plc_motion/engine/engine.py`
- Modify: `app/src/plc_motion/engine/events.py`
- Modify: `app/src/plc_motion/engine/__init__.py`（匯出新事件）
- Test: `app/tests/engine/test_servo_reset.py`、`app/tests/engine/test_jog.py`、`app/tests/engine/test_connection.py`

Difficulty: 複雜
Depends on: Task 8
Related: spec FR-13、FR-15、FR-16、FR-24 ~ FR-27、FR-57、FR-58；規格書 2.0.2、2.4

介面契約：（契約層）在 Task 8 的 `MotionEngine` 上新增

```python
    def servo_all(self, on: bool) -> list[str]: ...
    def reset_all(self) -> list[str]: ...
    def jog_press(self, axis: int, direction: int, speed: int) -> list[str]: ...   # direction: +1 / -1
    def jog_release(self, axis: int, direction: int) -> None: ...                  # 永遠執行，立即寫 0
    def write_point(self, tag: str, value: int | bool) -> list[str]: ...           # IO Monitor 用
    def residual_axes(self) -> list[ResidualInfo]: ...   # 軸號、Request、PC Action Code、Target Position
    def clear_residual(self) -> None: ...      # 對殘留軸寫 Request=0、PC Action Code=0 並解除該鎖定條件
```

新增事件：`ResidualDetected`（殘留清單）、`JogResidualCleared`（軸號）、`DisconnectedWhileMoving`。Task 8 的 `on_connected`／`on_disconnected` 在本 task 擴充殘留與 JOG 檢查。

測試 seam：同 Task 8（`MotionEngine` 公開方法＋事件，`ModelPort(PlcModel)` 扮演 PLC）

驗收：（契約層）
- [x] `servo_all` 對七軸 Servo Motor 寫 1／0；引擎建立、`on_connected`、`on_disconnected` 都不寫 Servo
- [x] `reset_all`：只對 Alarm≠0 的軸寫 Reset Alarm 1，脈衝時間後寫 0，再於逾時內等 Alarm=0 且 Ready=1；以 `OperationStarted`／`OperationFinished` 回報，逐軸結果為「復歸成功」或「復歸失敗」；等待期間屬操作進行中而鎖定；不重試；無 Alarm 時以拒絕通道回傳「不需復歸」
- [x] `jog_press` 檢查 FR-26 全部條件（含 B 接點方向極限）後先寫 Speed 再寫 JOG=1；按住中 `locked` 為真；`jog_release` 在鎖定中也立即寫 0
- [x] `write_point`、`servo_all`、`reset_all` 鎖定時拒絕
- [x] 連線後第一次 tick 讀到 Request=1 或 PC Action Code≠0 → `ResidualDetected`、保持鎖定、除 FR-58 的 JOG 寫 0 外零寫入；`clear_residual` 寫回 0 後解除；已連線期間正常交握不觸發
- [x] 連線後第一次 tick 讀到 JOG=1 → 立即寫 0 並發 `JogResidualCleared`
- [x] `on_disconnected` 時，斷線前有軸 Busy 或 JOG 按住中 → `DisconnectedWhileMoving`
- [x] Run: `cd app && uv run pytest tests/engine/test_servo_reset.py tests/engine/test_jog.py tests/engine/test_connection.py -v` → 全綠

禁區：
- 不修改 Task 8 已通過的行為（`tests/engine/test_move.py` 等需維持全綠）

留給實作：
- Reset Alarm 脈衝計時方式；邊界：寫 1 到寫 0 的間隔不短於設定值、不長於設定值＋一個輪詢週期

---

### Task 10: 背景通訊 worker 與日誌 [x]

Files:
- Create: `app/src/plc_motion/worker.py`
- Create: `app/src/plc_motion/logging_setup.py`
- Test: `app/tests/test_worker.py`、`app/tests/test_logging.py`

Difficulty: 複雜
Depends on: Task 7、Task 9
Related: spec FR-10 ~ FR-14、FR-27、FR-48 ~ FR-50；`docs/design/design-modbus-master-app.md`「程式結構」節（方案 C）；`docs/design/design-system-blueprint.md`「介面與資料契約表」

介面契約：（契約層）

```python
class PlcWorker(QObject):
    # signals（跨執行緒，queued）
    snapshot_ready = Signal(object, object)   # (Snapshot | None, EngineStatus)；未連線時為 None；每輪輪詢、每個命令後、斷線時發出
    engine_event = Signal(object)              # EngineEvent
    connection_changed = Signal(bool)
    # 命令方法：UI 唯一入口，可從 UI 執行緒直接呼叫；方法內部一律以 queued 方式轉派到 worker 執行緒執行
    # （呼叫端不得阻塞、不得在呼叫端執行緒做 Modbus I/O）。對應 MotionEngine 同名命令；
    # 拒絕原因以 engine_event(CommandRejected) 回傳
    def connect_plc(self) -> None: ...
    def move(self, targets, relative, speed) -> None: ...
    def step(self, axis, delta, speed) -> None: ...
    def home(self, axes) -> None: ...
    def servo_all(self, on) -> None: ...
    def reset_all(self) -> None: ...
    def jog_press(self, axis, direction, speed) -> None: ...
    def jog_release(self, axis, direction) -> None: ...   # 插隊：不等輪詢週期
    def write_point(self, tag, value) -> None: ...
    def clear_residual(self) -> None: ...
    def stop(self) -> None: ...

def start_worker(config: AppConfig, host: str, port: int) -> tuple[QThread, PlcWorker]: ...
def setup_logging(logs_dir: Path, keep_days: int) -> None: ...
```

`EngineStatus` 帶 `locked`、`lock_reasons`、`connected`、`operating`（worker 依 OperationStarted／OperationFinished 追蹤）、`residual`。

測試 seam：`PlcWorker` 的 signal 與 slot（pytest-qt `qtbot.waitSignal`），PLC 端為 Task 6 的 `SimServer`（真 TCP）

驗收：（契約層）
- [x] 從 UI 執行緒呼叫任一命令方法時，實際的 Modbus 寫入與 `MotionEngine` 呼叫發生在 worker 執行緒（以記錄執行緒 id 的測試驗證）
- [x] 啟動即自動連線；先在 SimServer 未啟動時啟動 worker（`connection_changed(False)`），再啟動 SimServer、等待數個輪詢週期，連線紀錄為零；呼叫 `connect_plc` 後才出現連線
- [x] 輪詢週期依 `device.toml`；SimServer 停止後一個輪詢週期內 `connection_changed(False)` 且日誌有斷線紀錄
- [x] `jog_release` 從呼叫到 SimServer 寫入紀錄出現 JOG=0 不超過 100 ms（即使正在等待下一輪）
- [x] `--host`/`--port` 覆寫由 `start_worker` 參數傳入，不寫回 `device.toml`
- [x] 背景執行緒內未預期例外 → 日誌記錄、視同通訊中斷、worker 仍可再 `connect_plc`
- [x] 日誌每日一檔於 `logs`；FR-49 列出的事件皆寫入，另含殘留交握偵測與寫回 0（FR-57）、JOG 殘留寫 0（FR-58）；超過保留天數的檔案在 `setup_logging` 時刪除（以檔名日期判斷）
- [x] Run: `cd app && uv run pytest tests/test_worker.py tests/test_logging.py -v` → 全綠

禁區：
- Modbus I/O 與 `MotionEngine` 呼叫只能在 worker 執行緒

留給實作：
- 交握期間不縮短輪詢週期；邊界：量到從按下到開始移動明顯超過 1 秒時在 dev-log 回報，不自行新增設定

---

### Task 11: 主視窗框架、主題、系統匣、單一實例、程式進入點 [x]

Files:
- Create: `app/src/plc_motion/app.py`
- Create: `app/src/plc_motion/ui/__init__.py`
- Create: `app/src/plc_motion/ui/main_window.py`
- Create: `app/src/plc_motion/ui/theme.py`
- Create: `app/src/plc_motion/ui/themes/dark.qss`、`app/src/plc_motion/ui/themes/light.qss`
- Create: `app/src/plc_motion/ui/tray.py`
- Create: `app/src/plc_motion/ui/single_instance.py`
- Create: `app/src/plc_motion/ui/dialogs.py`（設定錯誤視窗、例外視窗）
- Modify: `app/src/plc_motion/__init__.py`（`main` 指向 `app.main`）
- Modify: `.gitignore`（repo 根，加入 `app/logs/`）
- Test: `app/tests/ui/test_main_window.py`、`app/tests/ui/test_tray.py`、`app/tests/test_app_entry.py`

Difficulty: 複雜
Depends on: Task 4、Task 10
Related: spec FR-8（錯誤視窗）、FR-14、FR-41 ~ FR-47、FR-50；預覽 `.craft/modbus-master-app/preview/layout-v3-page.html`（主 repo 內才有，worktree 中不存在；版面依本 task 描述即可）

介面契約：（契約層）
- 命令列：`plc-motion [--host HOST] [--port PORT] [--config DIR]`；`--config` 預設為 `resolve_config_dir()`，`logs` 位於該資料夾的同層（開發與測試用，現場不需指定）
- `MainWindow` 公開：`nav`（可收合）、`toolbar` 上的連線狀態與「重新連線」、主題切換、急停狀態、「全部 Servo ON」「全部 Servo OFF」「全部 Reset Alarm」；`status_bar` 顯示輪詢週期與最近訊息；`add_page(name, icon, widget)` 逐頁掛入（Task 12、14 各自呼叫）
- 鎖定時工具列的寫入按鈕反灰（依 `EngineStatus.locked`）
- 已落地：`MainWindow(config, worker, endpoint="")`；`app.py` 的 `MotionApp(config, host, port)` 組裝 worker、主視窗與系統匣（提供 `.window`、`.tray`、`.worker`、`.thread`），供 Task 18 重用

測試 seam：pytest-qt 操作 `MainWindow` 與 `TrayController`；進入點以 subprocess 執行 `uv run plc-motion --config <tmp 設定資料夾>` 觀察結束碼與視窗（不得修改 repo 的 `app/config`）

驗收：（契約層）
- [x] 設定錯誤時顯示列出全部錯誤的視窗，關閉後結束碼非 0；同時寫入日誌（位置為所用設定資料夾——`--config` 指定者或預設的 `resolve_config_dir()`——同層的 `logs`，保留天數用預設 30）
- [x] 收到 `CommandRejected` 時顯示全部拒絕原因；收到 `OperationFinished` 時顯示逐軸結果（含復歸成功／失敗、未執行）；兩者都進狀態列最近訊息並可檢視完整內容
- [x] 最小化與關閉鈕都縮到系統匣、worker 繼續輪詢；系統匣選單「顯示主視窗／重新連線／結束」與雙擊叫回可用；「結束」在操作進行中或有軸 Busy 時先確認；結束不寫任何點
- [x] 系統匣圖示依「正常／動作中／Alarm 或急停／未連線」四態切換；Alarm 0→非 0、急停 1→0 時 `showMessage` 通知
- [x] 主題切換即時套用並存回 `device.toml`，重新啟動仍為該主題
- [x] 第二個 `plc-motion` 程序啟動時，第一個視窗被叫到前景，第二個結束
- [x] UI 執行緒未預期例外 → 日誌＋錯誤視窗，程式不結束
- [x] 最小尺寸 1600×900；左側導覽收合後內容區變寬
- [x] Run: `cd app && uv run pytest tests/ui/test_main_window.py tests/ui/test_tray.py tests/test_app_entry.py -v` → 全綠

留給實作：
- 顏色、圖示、間距與字型；邊界：深淺兩主題下 Check Status 五種狀態與「正常／動作中／異常／未連線」可互相區分
- 視窗大小與位置是否記憶；邊界：不影響任何驗收

---

### Task 12: 軸控頁（總覽、選定軸、全軸操作、步進鍵盤、JOG） [ ]

Files:
- Create: `app/src/plc_motion/ui/axis_page.py`
- Create: `app/src/plc_motion/ui/jog_pad.py`
- Modify: `app/src/plc_motion/app.py`（以 `add_page` 掛入軸控頁）
- Test: `app/tests/ui/test_axis_page.py`、`app/tests/ui/test_jog_pad.py`

Difficulty: 複雜
Depends on: Task 11
Related: spec FR-16、FR-27 ~ FR-33；預覽 `layout-v3-page.html`（主 repo 的 `.craft/`）

介面契約：（契約層）
- `AxisPage(config, worker)`：顯示 FR-28 ~ FR-31 的區塊，命令一律呼叫 `PlcWorker` 的命令方法（Task 10 定義的唯一入口），不直接碰 engine
- `JogPad`：FR-32 的鍵位；長／中／短距離取自 `axes.toml`；步進與 JOG 共用速度欄
- 位置小數位數用 `codec.decimals_for`

測試 seam：pytest-qt 操作 `AxisPage`／`JogPad`，以假的 worker（記錄被呼叫的 slot 與參數）與手動送入的 `snapshot_ready` 斷言

驗收：（契約層）
- [ ] 七軸總覽每列顯示 FR-28 的欄位；Alarm≠0 的列為異常色；點選一列更新選定軸區塊
- [ ] 選定軸區塊顯示 FR-30 的 9 個燈號與 PLC Action Code、Alarm Code；全軸操作區顯示七軸 Find Home Finish 總覽（FR-31）
- [ ] 勾選＋目標＋絕對/相對＋速度 → 呼叫 `move` 的參數正確；「勾選軸原點復歸」「單軸原點復歸」「全部原點復歸」呼叫 `home` 的軸組正確
- [ ] 步進鍵按一次呼叫一次 `step`，delta＝所選長/中/短距離×方向；該軸 Find Home Finish=0 時鍵反灰；鍵旁顯示目前距離
- [ ] JOG 按下呼叫 `jog_press`、放開呼叫 `jog_release`；按住中滑鼠在鍵外放開、視窗失焦、最小化、縮到系統匣都呼叫 `jog_release`
- [ ] 方向極限觸發時對應 JOG 鍵反灰
- [ ] `EngineStatus.locked` 為真時所有寫入控制項反灰（JOG 放開除外）
- [ ] Run: `cd app && uv run pytest tests/ui/test_axis_page.py tests/ui/test_jog_pad.py -v` → 全綠

---

### Task 13: 點位表面板 [ ]

Files:
- Create: `app/src/plc_motion/ui/points_panel.py`
- Modify: `app/src/plc_motion/ui/axis_page.py`（嵌入面板）
- Test: `app/tests/ui/test_points_panel.py`

Difficulty: 簡單
Depends on: Task 12
Related: spec FR-34 ~ FR-36；Task 3 的 `PositionTable`

介面契約：（契約層）
- `PointsPanel(config, worker)`：200 列表格＋操作面板；檔案操作走 `PositionTable`，移動走 worker 的 `move`（絕對、勾選軸）

測試 seam：pytest-qt 操作 `PointsPanel`，斷言 `positions.csv` 內容與假 worker 收到的呼叫

驗收：（契約層）
- [ ] Name、Description 雙擊編輯後立即存檔；座標手動輸入立即存檔
- [ ] 「存入目前位置」把最新快照的七軸位置寫入選定點位並存檔
- [ ] 「移動到此點」只對勾選軸呼叫 `move(relative=False)`，目標為該點位值、速度取面板速度
- [ ] 超出軟體極限的座標格為異常色
- [ ] 匯入：錯誤檔顯示原因且表不變；合法檔確認後取代，取消不變。匯出另存 CSV
- [ ] 鎖定時存入、編輯、匯入皆不可用
- [ ] Run: `cd app && uv run pytest tests/ui/test_points_panel.py -v` → 全綠

---

### Task 14: IO Monitor 頁 [ ]

Files:
- Create: `app/src/plc_motion/ui/io_monitor.py`
- Create: `app/src/plc_motion/ui/value_dialog.py`
- Modify: `app/src/plc_motion/app.py`（以 `add_page` 掛入 IO Monitor）
- Test: `app/tests/ui/test_io_monitor.py`

Difficulty: 複雜
Depends on: Task 11
Related: spec FR-37 ~ FR-40；Task 2 的 `IoPointTable.save_check_status`

介面契約：（契約層）
- `IoMonitorPage(config, worker)`：「數位」「數值」分頁、左 Read Only 右 Read Write、Visible=X 不顯示；寫入走 worker 的 `write_point`

測試 seam：pytest-qt 操作 `IoMonitorPage`，斷言畫面項目、假 worker 收到的呼叫與 `io_points.csv` 內容

驗收：（契約層）
- [ ] 預設 config 下 JOG Plus、JOG Minus、Request 不出現；B 接點顯示「(B)」；Position 顯示換算值與單位
- [ ] RW Boolean 雙擊切換呼叫 `write_point`；數值雙擊開輸入視窗，U16 0~65535、Speed 1~100、Position 以實際值換算 raw；超出範圍被拒
- [ ] 鎖定時雙擊不呼叫 `write_point`
- [ ] 右鍵設定 Check Status → 該列顏色、數量統計更新，檔案該欄立即改變；「全部重設為 Unchecked」經確認後執行
- [ ] 文字搜尋篩選 Tag Name 與 Display Name
- [ ] 自訂非軸 Tag（例：`Cylinder1 Up`）出現在正確欄
- [ ] Run: `cd app && uv run pytest tests/ui/test_io_monitor.py -v` → 全綠

---

### Task 15: 殘留交握視窗與連線相關提示 [ ]

Files:
- Create: `app/src/plc_motion/ui/residual_dialog.py`
- Modify: `app/src/plc_motion/ui/main_window.py`（工具列重新開啟入口、提示顯示）
- Test: `app/tests/ui/test_residual_dialog.py`

Difficulty: 簡單
Depends on: Task 11
Related: spec FR-13、FR-57、FR-58；Task 9 的 `ResidualDetected`、`JogResidualCleared`、`DisconnectedWhileMoving` 事件

介面契約：（契約層）
- 收到 `ResidualDetected` 跳出視窗：列出軸號與讀到的 Request、PC Action Code、Target Position，警告文字照 FR-57；「寫回 0」呼叫 worker `clear_residual`；「先不處理，保持鎖定」關閉視窗，工具列出現重新開啟入口
- `JogResidualCleared` 顯示「第 n 軸 JOG 於斷線期間未放開，已寫回 0」；`DisconnectedWhileMoving` 顯示「軸可能仍在移動，請按急停」

測試 seam：pytest-qt 送入事件，斷言視窗內容與假 worker 呼叫

驗收：（契約層）
- [ ] 三種事件的畫面文字與 FR 一致
- [ ] 選「先不處理」不呼叫任何 worker 寫入；從工具列重開後選「寫回 0」才呼叫 `clear_residual`
- [ ] Run: `cd app && uv run pytest tests/ui/test_residual_dialog.py -v` → 全綠

---

### Task 16: 模擬 PLC 視窗與 plc-sim 進入點 [ ]

Files:
- Create: `app/src/plc_motion/sim/sim_window.py`
- Create: `app/src/plc_motion/sim/main.py`
- Modify: `app/pyproject.toml`（`[project.scripts]` 加 `plc-sim = "plc_motion.sim.main:main"`）
- Test: `app/tests/sim/test_sim_window.py`

Difficulty: 簡單
Depends on: Task 4、Task 6
Related: spec FR-51、FR-53、FR-56

介面契約：（契約層）
- 命令列：`plc-sim [--host 127.0.0.1] [--port 5020] [--config DIR]`；`--config` 預設同 `resolve_config_dir()`
- 視窗操作對應 `PlcModel` 的 FR-53 方法與 `SimServer.disconnect_all`；紀錄區顯示 `SimLog` 並可匯出

測試 seam：pytest-qt 操作 `SimWindow`，斷言 `PlcModel` 狀態與 `SimServer` 行為

驗收：（契約層）
- [ ] FR-53 每一項操作在視窗上可用且生效（極限、原點感測器、急停、速度倍率、設定位置、注入 Alarm 含原因持續、兩種故障、中斷所有連線）
- [ ] 紀錄區即時顯示寫入、連線、讀取統計、狀態變化，可匯出
- [ ] `uv run plc-sim --port 5021` 啟動後可用 pymodbus client 連上
- [ ] Run: `cd app && uv run pytest tests/sim/test_sim_window.py -v` → 全綠

---

### Task 17: PyInstaller onedir 打包與授權聲明 [ ]

Files:
- Create: `app/packaging/plc_motion.spec`
- Create: `app/packaging/build.py`
- Create: `app/packaging/THIRD_PARTY_NOTICES.md`（或由 build.py 產生）
- Modify: `docs/bootstrap/bootstrap-plc-motion-master.md`（勾掉「PyInstaller 打包腳本與授權聲明檔」）
- Modify: `app/pyproject.toml`（註冊 `slow` marker；`addopts` 預設排除 `slow`）
- Test: `app/tests/test_packaging.py`

Difficulty: 簡單
Depends on: Task 15、Task 13、Task 14
Related: spec FR-54、Hard Constraints；`docs/bootstrap/bootstrap-plc-motion-master.md`

介面契約：（契約層）
- `cd app && uv run python packaging/build.py` 產出 `app/dist/plc-motion/`：`plc-motion.exe`、Qt DLL 為獨立檔、`config/`（Task 4 預設檔複本）、`logs/` 可由程式建立、第三方授權聲明（PySide6／Qt LGPL、pymodbus、Python 等實際打包進去的套件）
- 不含 `plc_motion.sim` 與 `plc-sim`

測試 seam：`build.py` 產出的資料夾內容；打包測試標記 `@pytest.mark.slow`，預設全套跳過，以 `-m slow` 執行

驗收：（契約層）
- [ ] 產出資料夾存在上述內容；搜尋不到 `sim_window`、`plc_sim` 相關模組
- [ ] 把資料夾複製到另一路徑，執行 exe（不經 uv）加 `--host 127.0.0.1 --port 5020`，連上 `uv run plc-sim` 啟動的模擬器
- [ ] Run: `cd app && uv run pytest tests/test_packaging.py -m slow -v` → 全綠

禁區：
- 不使用 onefile 模式

留給實作：
- 是否把依賴從 `pyside6` 改為 `pyside6-essentials` 以縮小體積；邊界：程式只用到的 Qt 模組都在，全套測試仍綠，結果寫進 bootstrap 紀錄的未解問題

---

### Task 18: 端到端驗收（交握、連線、原點、步進與 JOG、Servo 與 Reset） [ ]

Files:
- Create: `app/tests/e2e/conftest.py`（在測試內啟動 `SimServer`＋真 `MainWindow`＋`PlcWorker`，走 TCP）
- Create: `app/tests/e2e/test_e2e_motion.py`
- Create: `app/tests/e2e/test_e2e_connection.py`
- Modify: `app/pyproject.toml`（註冊 `e2e` marker）

Difficulty: 複雜
Depends on: Task 12、Task 14、Task 15、Task 16
Related: `docs/specs/spec-modbus-master-app.md` Acceptance Criteria（觀測點說明）

測試 seam：真實 `MainWindow`（pytest-qt 點擊）＋真 TCP 連到 `SimServer`；觀測 `SimLog`、`PlcModel` 狀態、畫面控制項、日誌檔。e2e 測試標記 `@pytest.mark.e2e`，預設全套也要跑

驗收：（契約層，逐字抄 spec）
- [ ] AC-1：以 repo 提供的預設 `config` 啟動 `plc-motion --host 127.0.0.1 --port 5020` 連上 `plc-sim`，畫面顯示已連線，七軸位置與燈號與模擬器狀態一致。
- [ ] AC-6：`plc-sim` 未啟動時開 `plc-motion`：主視窗出現、顯示未連線、所有寫入控制項反灰；之後啟動 `plc-sim` 再按重新連線，變成已連線。期間程式不自行重連。
- [ ] AC-7：已連線時關閉 `plc-sim`，一個輪詢週期內畫面顯示未連線、系統匣圖示變為未連線狀態、日誌記錄斷線。
- [ ] AC-39：對 X 開啟「不回 Reply」並下移動，在 Request = 1 之後、5 秒逾時之前按模擬器「中斷所有連線」，再按重新連線：跳出殘留交握視窗列出 X（Request = 1、PC Action Code = 2）；連線後到操作員選擇前，模擬器寫入紀錄沒有任何寫入。選「先不處理」維持鎖定；再從工具列開啟視窗選「寫回 0」，紀錄出現 X 的 Request = 0 與 PC Action Code = 0，之後解鎖。
- [ ] AC-40：重現 AC-39 的殘留狀態後，從系統匣選單「結束」`plc-motion` 再重新啟動，啟動連線後同樣跳出殘留交握視窗。對 Y 開「不複製命令碼」下移動，於等待期間「中斷所有連線」再重新連線（Request = 0、PC Action Code = 2），同樣跳出視窗列出 Y。
- [ ] AC-42：按住 JOG+ 時按模擬器「中斷所有連線」，模擬器的 X 持續移動、JOG Plus 1 仍為 1；按重新連線後，第一次輪詢之後紀錄出現 JOG Plus 1 = 0、X 停止，畫面出現對應提示，日誌有紀錄。
- [ ] AC-8：三軸（X、Y、Z，已原點復歸）勾選、填不同目標、絕對模式執行：模擬器寫入紀錄中每軸的寫入順序符合 FR-18；三軸都開始移動後才有任一軸完成；最後一軸停止前所有寫入控制項反灰，停止後解鎖；最終位置等於目標。
- [ ] AC-9：相對模式對 X 輸入 −2.5 mm、目前位置 10 mm：模擬器收到的 Target Position 為 75000（7.5 × 10000），Action Code 為 2；全程沒有寫入 Action Code 3。升降軸相對 +1.25 mm 時收到值為目前值 + 125。
- [ ] AC-10：負值目標 −100 mm（X 軸）寫入後，模擬器端兩個 Word 為低位 48576、高位 65520；在模擬器把 X 位置設為 −2.3456 mm（FR-53）後畫面顯示 −2.3456。
- [ ] AC-11：勾選 X、Y 移動，其中 Y 的 Find Home Finish = 0：顯示 Y 未原點復歸、模擬器寫入紀錄中沒有任何一筆寫入。目標超出 X 軟體極限時同樣全部不寫入並指出 X。
- [ ] AC-12：多軸移動中，對 Y 注入 Alarm 2：Y 列顯示異常、X 繼續移動到目標；X 停止前寫入控制項維持反灰；結束訊息逐軸顯示 X 完成、Y Alarm 2。
- [ ] AC-13：先完成一次 X 軸 Code 2 移動，再開啟「不複製命令碼」故障並再下一次 X 軸移動：約 5 秒後該軸以逾時結束，模擬器寫入紀錄中沒有 Request = 1。開啟「不回 Reply」：約 5 秒後逾時，紀錄中 Request = 1 之後有 Request = 0。兩者之後皆解鎖。
- [ ] AC-41：交握等待期間的提早結束都會寫回 0：開「不複製命令碼」下移動，於等待期間注入 Alarm：紀錄中沒有 Request = 1，有 PC Action Code = 0；開「不回 Reply」下移動，於等待期間切換急停：紀錄中 Request = 1 之後有 Request = 0 與 PC Action Code = 0；AC-13 兩種逾時結束後，紀錄中都有 PC Action Code = 0。
- [ ] AC-14：移動中切換急停：該操作以急停結束；解除急停後按全部 Reset Alarm，Alarm 歸 0、Ready = 1，七軸 Find Home Finish 顯示未完成，步進鍵反灰，下移動被擋（原因為未原點復歸）。
- [ ] AC-37：同一軸連續下兩次 Code 2 移動：模擬器紀錄中每次 Request = 0 都伴隨 PC Action Code = 0；第二次移動在 PLC Action Code 由 0 變為 2 之後才送 Request = 1。
- [ ] AC-15：全部原點復歸：模擬器寫入紀錄中第 7 軸的 Request = 1 早於第 1 ~ 6 軸任何一軸寫入 PC Action Code；第 1 ~ 6 軸在第 7 軸 Find Home Finish = 1 之後才開始，且彼此同時進行。
- [ ] AC-16：全部原點復歸時對第 7 軸注入 Alarm：第 1 ~ 6 軸無任何寫入，結果顯示「未執行」。
- [ ] AC-17：原點復歸完成判定：模擬器在 Busy = 0 後延遲設 Find Home Finish = 1 的期間，畫面仍視為動作中、未解鎖。
- [ ] AC-18：選定 RX 軸按單軸原點復歸，只有 RX 有寫入。
- [ ] AC-19：X 目前 10 mm、步距「長」設定 10 mm，按 X+ 一次：模擬器收到 Target Position 200000、Action Code 2。目標超出軟體極限時不寫入並提示。
- [ ] AC-20：按住 JOG+ 期間模擬器 X 位置持續增加；以自動化測試記錄放開的時間，模擬器紀錄中 JOG Plus = 0 的時間晚於放開不超過 100 ms，位置停止。按住期間把焦點切到其他視窗，同樣在 100 ms 內收到 0。
- [ ] AC-21：正極限觸發（模擬器端切換）時 JOG+ 反灰、JOG− 可用。
- [ ] AC-22：程式啟動與結束時，模擬器寫入紀錄中都沒有 Servo Motor 的寫入；按全部 Servo ON 時七軸 Servo Motor 都收到 1。
- [ ] AC-23：移動中全部 Servo OFF、全部 Reset Alarm 按鈕為反灰，點擊不產生寫入。
- [ ] AC-24：對兩軸注入 Alarm，其中一軸勾選「原因持續」，按全部 Reset Alarm：只有這兩軸收到 Reset Alarm 1 → 0（間隔約 0.5 秒）；結果顯示一軸成功、一軸失敗；程式不自動再送。
- [ ] Run: `cd app && uv run pytest tests/e2e/test_e2e_motion.py tests/e2e/test_e2e_connection.py -v` → 全綠

留給實作：
- AC-40 的「結束再重新啟動」與 AC-6 的「未啟動時開程式」可在測試內以重建 `MainWindow`＋worker 模擬；邊界：PLC 端必須是同一個（狀態保留的）`SimServer`，且經由真 TCP

---

### Task 19: 端到端驗收（點位表、IO Monitor、主視窗、日誌、打包）與實機前截圖檢查 [ ]

Files:
- Create: `app/tests/e2e/test_e2e_points_io.py`
- Create: `app/tests/e2e/test_e2e_window.py`

Difficulty: 複雜
Depends on: Task 17、Task 18
Related: spec Acceptance Criteria

測試 seam：同 Task 18；需要變更設定檔的 AC（AC-2 ~ AC-5、AC-29、AC-32）一律以 `--config` 指向 tmp 資料夾的複本，不修改 repo 的 `app/config`；AC-33 以兩個真實程序驗證；AC-36 使用 Task 17 的打包產物（`-m slow`）

驗收：（契約層，逐字抄 spec）
- [ ] AC-2：分別製造 FR-8 的每一種錯誤（每次一種；型態表逐欄至少各一例，含把 `Positive Limit 1` 的 `ON` 拿掉）啟動，程式顯示列出該錯誤的視窗後結束，結束碼非 0；同時製造兩種錯誤時兩者都被列出。
- [ ] AC-3：以 Big5 編碼存成的 `io_points.csv`（Display Name 含中文）啟動，畫面中文正確；程式寫回 Check Status 後，檔案為 UTF-8 含 BOM，再以 Excel 預設方式開啟中文正確。
- [ ] AC-4：刪除 `positions.csv` 後啟動，檔案被建立，含 P001 ~ P200 共 200 列。
- [ ] AC-5：在 `io_points.csv` 新增一個非軸的 Tag（如 `Cylinder1 Up`，Coil、Read Write），模擬器同樣讀這份檔；它出現在 IO Monitor 右欄，雙擊後模擬器狀態對應位址變為 1。
- [ ] AC-25：選 P004，按存入目前位置：`positions.csv` 的 P004 列立即更新為七軸目前位置。勾選 X、Y、Z 按移動到此點：只有 X、Y、Z 移動，目標為 P004 的值。
- [ ] AC-26：匯出後修改匯出檔再匯入：確認後點位表整份換成匯入內容並存檔；取消確認則不變。
- [ ] AC-38：匯入只有 150 列、或某座標為文字的檔案：顯示原因，點位表與 `positions.csv` 皆不變。
- [ ] AC-27：JOG Plus、JOG Minus、Request（Visible = X）不出現在 IO Monitor；B 接點的點名稱帶「(B)」。
- [ ] AC-28：雙擊 `Speed 1` 輸入 70，模擬器 D123 變為 70；輸入 150 被拒。雙擊 Servo Motor 1 切換後模擬器值改變。移動中雙擊不產生寫入。
- [ ] AC-29：右鍵把某點設為 NG：該列變為 NG 的顏色、數量統計更新、`io_points.csv` 對應列的 Check Status 立即為 NG；重新啟動後仍為 NG。
- [ ] AC-30：按最小化或右上角關閉，視窗消失、系統匣圖示存在、模擬器端仍持續收到輪詢；雙擊圖示叫回。系統匣選單「結束」在軸移動中先跳確認。
- [ ] AC-31：縮在系統匣時注入 Alarm，出現 Windows 通知、圖示變為異常顏色。
- [ ] AC-32：切換為淺色主題後重新啟動，仍為淺色；`device.toml` 的主題值已變更，其餘內容（含註解）不變。
- [ ] AC-33：已執行一份時再次啟動，不出現第二個主視窗，原本的主視窗被叫到前景。
- [ ] AC-34：通訊進行中（含交握等待期間）拖動視窗、切換頁面，畫面不凍結。
- [ ] AC-35：完成 AC-8、AC-12 後，當日日誌檔含兩次操作的參數與逐軸結果，以及 Alarm 變化；把一個超過保留天數的舊日誌放進 `logs` 後重新啟動，該檔被刪除。
- [ ] AC-36：打包輸出的資料夾複製到另一個路徑，執行其中的 exe（不透過 uv、不需安裝 Python）以 `--host 127.0.0.1 --port 5020` 連上 `plc-sim`，AC-1 成立；資料夾內有授權聲明檔、Qt DLL 為獨立檔案、沒有 `plc-sim`。
- [ ] 實際啟動 `uv run plc-sim` 與 `uv run plc-motion --host 127.0.0.1 --port 5020`，深色與淺色各截一張軸控頁與 IO Monitor 頁的圖存到主 repo 的 `C:\Users\jimze\Desktop\Project\AI Project\PLC-and-PL-Modbus-Motion\.craft\modbus-master-app\screenshots\`（絕對路徑，worktree 清除後仍保留），版面結構符合 FR-28 ~ FR-41
- [ ] Run: `cd app && uv run pytest tests/e2e/test_e2e_points_io.py tests/e2e/test_e2e_window.py -v` → 全綠；`cd app && uv run pytest -m slow -v` → 全綠
- [ ] Run: `cd app && uv run pytest -q` → 收集到的測試數大於 0 且全綠

留給實作：
- AC-31 的 Windows 通知與 AC-33 的前景判定在自動化測試中的觀測方式（例如攔截 `QSystemTrayIcon.showMessage` 呼叫、檢查視窗 `isActiveWindow`）；邊界：必須在真實執行的程式上觀測，不可只測呼叫端假物件

---

## Pre-Mortem

### 如果這個 plan 會失敗，最可能的原因

1. 交握引擎與模擬器出自同一份理解，兩邊一起錯而測試全綠（機率：medium）
   - 早期訊號：接實機時第一個命令就卡在「等 PLC Action Code 一致」或「等 Reply」逾時
   - 緩解：模擬器行為逐條對照規格書寫測試（Task 5 驗收），不從引擎反推；spec 已列實機確認項（PLC 以 PLC_Action_Code 決定動作、待機複製 0）；完成時把這些列入給使用者的實機測試清單

2. Qt 執行緒與 pytest-qt 的 e2e 測試不穩定（偶發逾時、Windows 系統匣在測試環境不可用）（機率：medium）
   - 早期訊號：Task 10、Task 18 的測試偶爾失敗，重跑又過
   - 緩解：等待一律用 `qtbot.waitUntil`／`waitSignal` 帶明確逾時，不用 sleep；模擬器以固定步長推進；仍不穩時依 `craft:testing` 的 flaky 處置查根因，不加重試

3. PyInstaller 對 PySide6 6.12＋Python 3.14 的支援缺口，打包後執行失敗（機率：low）
   - 早期訊號：Task 17 打包成功但 exe 啟動即閃退或缺 Qt plugin
   - 緩解：缺 plugin 以 spec 檔 hiddenimports／datas 補；仍不行回報使用者，不改用 onefile 或 Nuitka（授權硬約束）
