---
type: plan
created: 2026-11-07
tags: [modbus, modbus-tags, migration, jog]
slug: modbus-tags-migration
goal: "plc-motion 的 Modbus 通訊改由 modbus_tags 負責，交握與安全行為不變，JOG 放開不等輪詢週期，IO Monitor 支援函式庫全部型別"
status: approved
approved_by: user
related: ["[[spec-modbus-tags-migration]]", "[[design-modbus-tags-migration]]"]
---

# plc-motion 通訊層改用 modbus_tags 實作計畫

Goal: plc-motion 的 Modbus 通訊改由 modbus_tags 負責，交握與安全行為不變，JOG 放開不等輪詢週期，IO Monitor 支援函式庫全部型別

Architecture: 保留 `PlcPort`，以 `TagsLink` 包住 `modbus_tags.Bus` 取代 `ModbusLink`。`engine/` 不依賴 Qt，只透過 `PlcPort.write` 寫入、由 Worker 餵快照。Qt 只留在 `worker.py`、`app.py`、`ui/`、`sim/` 的視窗。程式判斷用邏輯值，`invert` 是接點型式的唯一來源。指令一律在 `app\` 下執行。

Verification: `cd app && uv run --system-certs pytest && uv run --system-certs pytest -m slow && uv run --system-certs ruff check . && uv run --system-certs ruff format --check .`

Base branch: main @ 59419e5

---

前置條件：基底 `AI Project\Python Library\modbus-tags` 的 `docs/plans/plan-write-wake.md` 已完成並合併，版本 0.7.0。

| FR／AC | Task |
|---|---|
| FR-1 ~ FR-4、AC-1 ~ AC-6 | 基底 plan-write-wake |
| FR-5、AC-7 | Task 1 |
| FR-6 ~ FR-14、AC-8、AC-9、AC-11、AC-12 | Task 2 |
| FR-26 ~ FR-28、AC-10、AC-27 ~ AC-29 | Task 3 |
| FR-33 的故障注入 | Task 4 |
| FR-20、FR-21、FR-24、FR-25、AC-24、AC-25 | Task 5 |
| FR-15 ~ FR-19、FR-22、FR-23、FR-32、AC-13 ~ AC-23、AC-26 | Task 6 |
| FR-13、FR-33 的多型別、AC-32 的 plc-sim 部分 | Task 7 |
| FR-29 ~ FR-31、AC-30 ~ AC-32 | Task 8 |
| FR-34 ~ FR-36、AC-33 ~ AC-36 | Task 9 |

## 檔案結構

| 檔案 | 職責 | 變更 |
|---|---|---|
| `app/src/lib/modbus_tags/` | 函式庫副本 | 更新為 0.7.0 |
| `app/config/`、`app/config/modbus/Channel01/` | 預設設定 | 改結構與格式 |
| `plc_motion/settings/` | 兩層啟動檢查、點位表、`AppConfig` | 修改；刪 `read_ranges.py` |
| `plc_motion/plc/snapshot.py`、`port.py` | 邏輯值快照、`ModelPort` | 修改 |
| `plc_motion/plc/tags_link.py` | 包住 `Bus` 的 adapter | 新增 |
| `plc_motion/plc/modbus_link.py`、`batching.py` | 舊通訊層 | Task 6 刪除 |
| `plc_motion/plc/model.py`、`sim/server.py` | 模擬 PLC | 修改 |
| `plc_motion/worker.py`、`app.py` | 計時器輪詢、停止流程 | 修改 |
| `plc_motion/ui/io_monitor.py`、`value_dialog.py`、`axis_page.py` | 顯示與寫入 | 修改 |

## Tasks

### Task 1: 相依、匯入路徑與副本 0.7.0 [x]

Files:
- Modify: `app/pyproject.toml`
- Modify: `app/uv.lock`
- Modify: `app/src/lib/modbus_tags/`（整個資料夾）
- Modify: `app/packaging/plc_motion.spec`（僅在匯入需要時）
- Test: `app/tests/test_smoke.py`

Difficulty: 簡單
Depends on: —
Related: `C:\Users\jimze\Desktop\Project\AI Project\Python Library\modbus-tags\src\modbus_tags` — 基底 0.7.0，副本的來源

介面契約：（契約層）

- 應用程式以 `lib.modbus_tags` 匯入：`[tool.uv.build-backend] module-name = ["plc_motion", "lib.modbus_tags"]`。
- 相依改為 `pymodbus[serial]>=3.16.2,<3.17`。
- `app/pyproject.toml` 設定 ruff format 排除 `*.md`，與基底相同：ruff 會格式化 Markdown 內的程式碼區塊，而副本不可手改。
- `app/src/lib/` 已在版控內，只更新內容。
- 副本內容與基底 0.7.0 的 `src/modbus_tags/` 相同。

測試 seam：`tests/test_smoke.py`，加上既有 `tests/sim` 對真實 TCP 的測試。

驗收：（契約層）
- [x] `from lib.modbus_tags import open, __version__` 成功，版本為 0.7.0。
- [x] 副本與基底 `src/modbus_tags/` 原始檔比對無差異（不含 `__pycache__`）。
- [x] `SimServer` 在新版 pymodbus 下，既有 sim 測試維持通過。
- [x] `uv run --system-certs ruff check .` 與 `ruff format --check .` 通過。
- [x] Run: `uv run --system-certs pytest tests/test_smoke.py tests/sim -q` → 全綠

禁區：
- 不手改副本內的檔案。

---

### Task 2: 設定結構與點位表 [x]

Files:
- Create: `app/config/app.toml`
- Create: `app/config/modbus/Channel01/device.toml`
- Create: `app/config/modbus/Channel01/io_points.csv`
- Create: `app/config/modbus/Channel01/read_ranges.toml`
- Delete: `app/config/device.toml`、`app/config/io_points.csv`、`app/config/read_ranges.toml`
- Modify: `app/src/plc_motion/settings/io_points.py`、`device.py`、`loader.py`、`__init__.py`（`device.py` 新增 `load_app`、`build_device`）
- Delete: `app/src/plc_motion/settings/read_ranges.py`、`app/tests/settings/test_read_ranges.py`
- Modify: `app/src/plc_motion/plc/batching.py`、`modbus_link.py`、`model.py`、`snapshot.py`、`sim/server.py`、`sim/main.py`、`app.py`、`engine/engine.py`（只改 `Access` 匯入）、`ui/io_monitor.py`、`ui/value_dialog.py`（機械式改名，行為不變）
- Modify: `app/tests/conftest.py`、`app/tests/e2e/conftest.py` 與 `tests/` 內引用舊欄位或舊路徑的測試
- Test: `app/tests/settings/test_io_points.py`、`test_loader.py`、`test_default_config.py`、`test_device_axes.py`、`app/tests/test_app_entry.py`

Difficulty: 複雜
Depends on: Task 1
Related: `docs/specs/spec-modbus-tags-migration.md` — FR-6 ~ FR-14、處置表；`app/src/lib/modbus_tags/config/__init__.py` — `load_config` 有錯就 raise

介面契約：（契約層）

- 設定資料夾、`device.toml`、`io_points.csv` 的格式依 spec FR-6 ~ FR-9。預設 Tag、位址、倍率、單位與轉換前相同。
- `AppConfig` 保留 `device`，引擎讀的屬性名不變（`handshake_timeout_s`、`reset_alarm_pulse_ms`，以及 `host`、`port`、`connect_timeout_ms`、`poll_interval_ms`、`log_retention_days`）；新增 `channel`（函式庫的 `ChannelConfig`），`read_ranges` 改用函式庫的 `ReadRange`。
- `IoPoint` 由函式庫的 `TagInfo` 建立，移除 `sensor_type`、`attributes`、`is_b_contact`，新增 `data_type: str`、`bit`、`invert`、`word_order`、`byte_swap`。`data_model`、`access` 改用函式庫的 enum。其餘欄位名不變；`visible`、`check_status` 從 `TagInfo.extra` 取，`line` 由 Check Status 寫回的 CSV 解析提供。`scale`：必要 Tag 為 `int`；其他點可為 float 或 None。
- 移除 app 自己的 `DataModel`、`Access`、`SensorType`，也不轉匯出；使用者一律從 `lib.modbus_tags` 匯入 `DataModel`、`Access`。`CheckStatus` 保留。
- 啟動檢查依 spec FR-11 的兩層。第一層直接以 `tomllib` 讀 `device.toml` 的 `[device] enabled` 與 `[connection] type`。`config/modbus/` 以 `{"PLC": "Channel01"}` 對照。
- Check Status 寫回行為依 spec FR-10。
- `--host`、`--port` 的值在這個 task 仍交給 `ModbusLink`；語意不變。

測試 seam：檢查規則以 `load_config(config_dir)` 與 `ConfigErrors.errors` 驗；錯誤視窗與結束碼在進入點實際啟動驗（`tests/test_app_entry.py`）；Check Status 從 `IoMonitorPage` 操作，重新載入後比對檔案位元組。

驗收：（契約層）
- [x] repo 的預設設定可啟動，連上 `plc-sim`；所有必要 Tag 的位址、Unit ID、Data Model、倍率、單位與轉換前相同。
- [x] 分別製造 spec FR-11 的每一種錯誤（每次一種），程式顯示列出該錯誤的視窗後結束，結束碼非 0。
- [x] 同層兩種錯誤同時列出；第一層與第二層同時有錯時先列第一層，排除後才出現第二層；`connection.type = "rtu"` 加上某列 Data Model 打錯時兩者都列出。
- [x] `byte_swap=false`、`invert=false` 用在必要 Tag 不算錯誤；`word_order=high_first` 用在 `Current Position 1` 報錯。
- [x] 在 IO Monitor 設定 Check Status 後重新啟動，狀態保留；比對檔案只有該欄改變。
- [x] `plc-motion --host --port` 實際連上 `plc-sim`，設定檔內容不變。
- [x] Run: `uv run --system-certs pytest tests/settings tests/plc tests/sim tests/engine tests/ui tests/test_worker.py tests/test_app_entry.py tests/e2e -q`（超過 120 秒，要加長 timeout）→ 全綠

禁區：
- 不動 `engine/engine.py`、`engine/handshake.py` 的邏輯。
- 不改輪詢、交握、畫面行為；這個 task 結束時 `ModbusLink` 仍是通訊層。

留給實作：
- `app.toml` 的鍵名；邊界：段落沿用 `[handshake]`、`[log]`。

---

### Task 3: 邏輯值快照與 B 接點顯示 [ ]

Files:
- Modify: `app/src/plc_motion/plc/snapshot.py`、`port.py`、`model.py`、`modbus_link.py`
- Modify: `app/src/plc_motion/ui/io_monitor.py`、`axis_page.py`
- Test: `app/tests/plc/test_snapshot.py`、`app/tests/ui/test_io_monitor.py`、`app/tests/ui/test_axis_page.py`、`app/tests/e2e/test_e2e_points_io.py`

Difficulty: 複雜
Depends on: Task 2

介面契約：（契約層）

- `Snapshot(points, values)` 的 `values` 是邏輯值與工程值，同函式庫的讀值。`value(tag)` 回傳工程值；Bool 為邏輯值。`raw(tag)` 回傳 PLC 原始值：Bool 為 `value != invert`，整數型別為未乘倍率前的整數。`is_triggered` 從 `snapshot.py` 移除；`AxisState` 的欄位不變。
- `PlcPort.write` 的 Bool 值是邏輯值。必要位置點寫 `int` 原始值。`PlcPort` 只保留 `write`；`read_all` 移到 `ModelPort` 自己的方法，測試繼續使用。
- `ModelPort` 與過渡中的 `ModbusLink` 在邊界換算，讓 `Snapshot` 看到相同的值。`PlcModel` 維持存原始值。
- IO Monitor 的 Bool 燈號用 `raw()`。雙擊切換寫入 `not snapshot.value(tag)`。
- `(B)`：只有 `invert=true` 時顯示。軸控頁的 `+Limit`、`−Limit`、`Home Sensor` 燈號也依此顯示。

測試 seam：引擎測試透過 `ModelPort`；畫面透過 `IoMonitorPage`、`AxisPage`，e2e 走 `plc-sim`。

驗收：（契約層）
- [ ] B 接點點位在 `plc-sim` 為 1 時：IO Monitor 亮燈、名稱有 `(B)`；軸控頁燈號名稱有 `(B)`、顯示未觸發。改為 0 時：IO Monitor 不亮；軸控頁顯示觸發，對應方向 JOG 被拒。
- [ ] 把 `Positive Limit 1` 的 `invert=true` 拿掉後可以啟動，`plc-sim` 把該點設為 1 時，軸控頁顯示正極限觸發、名稱沒有 `(B)`、正向 JOG 被拒。
- [ ] Emergency Stop 為 B 接點且在 `plc-sim` 為 0 時，移動命令被拒絕，理由是急停；改為 A 接點時，`plc-sim` 為 1 才拒絕。
- [ ] 測試設定新增 `invert=true` 的可寫 Coil，在 IO Monitor 雙擊：模擬器收到目前原始值的相反值，燈號跟著模擬器原始值變化。
- [ ] 引擎既有測試不改邏輯仍全部通過。
- [ ] Run: `uv run --system-certs pytest tests/plc tests/engine tests/ui tests/e2e/test_e2e_points_io.py -q` → 全綠

禁區：
- 不動 `engine/` 的判斷邏輯，也不在引擎出現依 B 接點判斷的寫法。

---

### Task 4: plc-sim 故障注入 [ ]

Files:
- Modify: `app/src/plc_motion/sim/server.py`
- Test: `app/tests/sim/test_server.py`

Difficulty: 簡單
Depends on: Task 2

介面契約：（契約層）

- `SimServer.set_delay(ms)`：每個 Modbus 請求回應前延遲。
- `SimServer.reject_writes(tags)`：只拒絕這些點的寫入，例外碼為 Server Device Failure；讀取照常。空集合解除。
- 這些能力不出現在 `plc-sim` 視窗，不影響一般操作。

測試 seam：`SimServer` 搭配 pymodbus client 走 TCP。

驗收：（契約層）
- [ ] 設延遲後，client 量到的回應時間增加；解除後回到原水準。
- [ ] 拒絕 `Speed 1` 寫入時，client 收到例外碼 4，讀取 `Speed 1` 與寫其他點正常。
- [ ] Run: `uv run --system-certs pytest tests/sim/test_server.py -q` → 全綠

禁區：
- 例外碼不可用 1：函式庫遇到它會改用 FC16 重試。

---

### Task 5: TagsLink [ ]

Files:
- Create: `app/src/plc_motion/plc/tags_link.py`
- Test: `app/tests/plc/test_tags_link.py`

Difficulty: 複雜
Depends on: Task 3、Task 4
Related: `app/src/lib/modbus_tags/bus.py`、`channel.py`、`model.py`；`docs/design/design-modbus-tags-migration.md` 的「執行流程」「失敗處理」

介面契約：（契約層）

`TagsLink` 實作 `PlcPort.write`，並提供：

| 方法 | 契約 |
|---|---|
| `connect()` | 建立並啟動新 `Bus`，不阻塞；套用 host、port，且 `auto_reconnect` 一律關閉 |
| `poll() -> Snapshot \| None` | 依序：先取最新快照、再查寫入失敗與連線狀態。有失敗或斷線 raise `PlcCommError`；狀態不是 CONNECTED 時回 `None`，第一輪快照要等 CONNECTED 才交出；已連線且有新快照時回傳；否則回 `None` |
| `connected`、`connecting` | 目前狀態 |
| `write(tag, value)` | 排入佇列後返回；未連線 raise `PlcCommError`；範圍或型別錯誤（`ValueOutOfRange`、`TypeError`、`ReadOnlyTag`）原樣拋出，不算通訊失敗 |
| `write_wait(tag, value, timeout)` | 等結果，失敗或逾時 raise `PlcCommError` |
| `drain(timeout)` | 等已排入的寫入有結果或逾時 |
| `close()` | 先 `drain`，再停止 Bus；可重複呼叫 |
| `request_count`、`read_ms`、`cycle_ms` | 通訊統計；`cycle_ms` 為相鄰快照時間差，第一輪為 `None` |

寫入值的規則：`int` 寫到有 `scale` 的點時視為原始值，先除以倍率再交給函式庫；`float` 視為工程值。Bool 為邏輯值。其餘照值傳入。

測試 seam：`TagsLink` 公開方法，對 `SimServer` 走真實 TCP；需要控制時序的驗收，以建構參數注入「建立 Bus 的方式」，不測 `Channel` 內部。

驗收：（契約層）
- [ ] 必要位置點寫入的 I32 與 PC 端換算值完全相同，涵蓋負值、三種倍率與 I32 邊界值。
- [ ] 同一批寫入在模擬器的順序與呼叫順序相同。
- [ ] `device.toml` 設 `auto_reconnect = true` 且 `reconnect_interval_ms` 極短時，連線中斷後 `poll()` raise，之後不會自動連上。
- [ ] 替身讓快照已更新且同時有寫入失敗：`poll()` raise，不回傳該快照。
- [ ] 通道未完成新的一輪時連續呼叫 `poll()`：同一輪只回傳一次快照，之後回 `None`（判斷依 `Snapshot.time`；`Bus.snapshot()` 每次都回新物件）。
- [ ] 替身讓第一輪快照已更新、狀態仍是 CONNECTING：`poll()` 回 `None`；狀態變成 CONNECTED 後才回傳該快照。
- [ ] 寫入被拒時訊息含 Tag Name 與位址；批次讀取被拒時訊息含 Data Model 與位址範圍。
- [ ] 佇列有寫入時呼叫 `close()`，模擬器收到這些寫入後連線才結束。
- [ ] Run: `uv run --system-certs pytest tests/plc/test_tags_link.py -q` → 全綠

禁區：
- 不匯入 PySide6。不使用函式庫底線開頭的成員。

留給實作：
- 等待上限；邊界：須有上限，且讓 `close()` 與 JOG 寫 0 的驗收成立。

---

### Task 6: Worker 改用 TagsLink [ ]

Files:
- Modify: `app/src/plc_motion/worker.py`、`app.py`
- Modify: `app/src/plc_motion/engine/engine.py`、`handshake.py`（只改時序敘述，並新增 JOG 查詢屬性）
- Delete: `app/src/plc_motion/plc/modbus_link.py`、`batching.py`、`app/tests/plc/test_modbus_link.py`、`test_batching.py`
- Modify: `app/src/plc_motion/plc/__init__.py`
- Test: `app/tests/test_worker.py`、`app/tests/e2e/`

Difficulty: 複雜
Depends on: Task 5
Related: `docs/design/design-modbus-tags-migration.md` 的「執行流程」「失敗處理」；`app/src/plc_motion/app.py` 的 `shutdown`；`docs/specs/spec-modbus-tags-migration.md`、`docs/specs/spec-modbus-master-app.md`（FR-18 與「主規格條文處置表」）

介面契約：（契約層）

- Worker 用短週期計時器呼叫 `TagsLink.poll()`，新快照才呼叫 `engine.tick`，同一份不重複。signal（`snapshot_ready`、`engine_event`、`connection_changed`、`poll_timed`）的名稱與語意不變。
- 第一輪完成才發 `connection_changed(True)`；連線失敗發 `connection_changed(False)`，不自動重試。
- 已連線或連線中按重新連線：先 `close()` 再 `connect()`，只留一條通道。
- 通訊中斷：寫入被拒且連線仍在時，先以 `write_wait` 對引擎按住中的 JOG 點寫 0；之後 `close()`、`engine.on_disconnected()`。提示依引擎現有事件。
- 引擎新增唯讀屬性 `jog_held_tag -> str | None`。
- `_do_stop` 走 `close()`，所以 `app.py` 結束流程的 JOG 放開不會被丟棄；`_WORKER_STOP_TIMEOUT_MS` 要涵蓋最壞等待。

測試 seam：`PlcWorker` 的 signal；e2e 用真 `MainWindow`／`PlcWorker` 走 TCP 連 `SimServer`，看模擬器寫入紀錄、畫面與日誌。

驗收：（契約層）
- [ ] `interval_ms = 1000` 時按住再放開 JOG，模擬器在放開後 100 ms 內記錄 JOG = 0；預設週期下也一樣。
- [ ] 按住 JOG 時結束程式，模擬器紀錄最終該 JOG 點為 0；按住 JOG 時按重新連線，也一樣。
- [ ] Code 2 多軸移動，每軸的寫入順序是 `Target Position`、`Speed`、`PC Action Code`、`Request`，寫入 PLC 的 I32 與 PC 端換算值完全相同。
- [ ] `plc-sim` 加上通訊延遲，使快照晚於寫入時，移動與原點復歸都正確完成，不提前結束也不逾時。
- [ ] PLC 值連續數輪不變時，交握逾時仍會在設定時間後觸發。
- [ ] 主規格中與移動、原點復歸、Reset Alarm、殘留交握、JOG 殘留有關的驗收條件全部維持通過（對應既有測試：`tests/engine`、`tests/e2e/test_e2e_motion.py`、`tests/e2e/test_e2e_connection.py`）。
- [ ] 啟動時連不上，主視窗照常開啟並顯示未連線；`plc-sim` 啟動後不會自動連上，按重新連線才連上。
- [ ] 連線中 `plc-sim` 中斷所有連線，畫面顯示未連線，操作以「通訊中斷」結束，之後不會自動重連。`device.toml` 設 `auto_reconnect = true` 且 `reconnect_interval_ms` 極短時也一樣，且下次手動連線仍執行殘留交握與 JOG 殘留檢查。
- [ ] `plc-sim` 拒絕 `Speed` 寫入時按住 JOG：模擬器紀錄最終該 JOG 點為 0，畫面顯示未連線與「軸可能仍在移動，請按急停」，日誌含該 Tag Name 與位址。
- [ ] `plc-sim` 拒絕 `Speed` 與 JOG 點的寫入時按住 JOG：畫面顯示未連線與同樣提示，日誌記錄寫 0 失敗。
- [ ] Code 2 移動中 `plc-sim` 拒絕 `Target Position`：模擬器沒有收到該軸的 `Request = 1`，操作以「通訊中斷」結束。
- [ ] 寫入失敗後重新連線，斷線前排隊的寫入在重新連線之後不會送出；殘留交握與 JOG 殘留的處理同主規格。
- [ ] 通訊品質畫面的讀取耗時、週期、請求數與連線後第一輪無週期值，都符合 spec FR-18。
- [ ] 以可注入建立 Bus 的方式製造「快照已更新且同時有寫入失敗」：引擎沒有收到 tick，判定為通訊中斷。
- [ ] Run: `uv run --system-certs pytest tests/test_worker.py tests/e2e tests/engine tests/ui -q`（超過 120 秒，要加長 timeout）→ 全綠

禁區：
- 不改引擎和交握的判斷邏輯。
- 不從 Channel 迴圈送 Qt signal 給 Worker。
- 事件函式內不呼叫 `write_wait`、`ticket.wait`。

留給實作：
- 檢查快照的計時器週期；邊界：JOG 放開 100 ms、交握逾時的驗收都成立，且不明顯增加 CPU 負載。

---

### Task 7: plc-sim 多型別 [ ]

Files:
- Modify: `app/src/plc_motion/plc/model.py`、`sim/server.py`
- Test: `app/tests/plc/test_model.py`、`app/tests/sim/test_server.py`

Difficulty: 複雜
Depends on: Task 6
Related: `app/src/lib/modbus_tags/datatypes.py`、`config/points.py`；`docs/specs/spec-modbus-tags-migration.md`、`docs/specs/spec-modbus-master-app.md`

介面契約：（契約層）

- `plc-sim` 服務 `Bool`（Coil、Discrete Input、暫存器位元 `N.bK`）、`U16`、`I16`、`U32`、`I32`、`F32`、`String`，以及 Holding Register、Input Register；含 FC4、FC22、`word_order`、`byte_swap`。
- 同一暫存器的多個位元點共用該暫存器，寫一個位元不影響其他位元。
- 輸入點以未觸發為初始狀態：`invert=true` 的 Bool 點（含暫存器位元）初始原始值為 1。
- 七軸必要 Tag 的 PLC 行為不變。

測試 seam：`SimServer` 搭配 `lib.modbus_tags.Bus` 走 TCP，透過 `Bus` 的 `read`、`write_wait` 與 `PlcModel` 觀測。

驗收：（契約層）
- [ ] 上述每種型別與 Data Model，`Bus` 讀寫的值都與模型相同；Input Register 寫入被拒。
- [ ] 非必要 `U32` 設 `word_order=high_first` 時，`Bus` 讀寫正確。
- [ ] 同一暫存器的兩個位元分別寫入，互不影響。
- [ ] `invert=true` 的暫存器位元點，啟動後 `Bus` 讀到未觸發。
- [ ] 主規格 FR-52、FR-53、FR-56、FR-62 的既有測試維持通過。
- [ ] Run: `uv run --system-certs pytest tests/plc/test_model.py tests/sim -q` → 全綠

---

### Task 8: IO Monitor 多型別 [ ]

Files:
- Modify: `app/src/plc_motion/ui/io_monitor.py`、`value_dialog.py`、`worker.py`（寫入日誌）、`engine/engine.py`（只放寬 `write_point` 型別註記）
- Test: `app/tests/ui/test_io_monitor.py`、`app/tests/e2e/test_e2e_points_io.py`

Difficulty: 複雜
Depends on: Task 7
Related: `docs/specs/spec-modbus-tags-migration.md`、`docs/specs/spec-modbus-master-app.md`（FR-29 ~ FR-31）

介面契約：（契約層）

- 分頁、顯示與寫入依 spec FR-29 ~ FR-31。
- `ValueDialog` 回傳要交給 `write_point` 的值：有 `scale` 的點回傳 `float` 工程值；必要位置點維持回傳 `int` 原始值；其餘依型別回傳 `int`、`float` 或 `str`。
- `lib.modbus_tags.datatypes.encode` 可用來驗證範圍。
- Worker 的 IO 寫入日誌能記錄 `float`、`str`，寫入 `String` 不會因記錄日誌而判定斷線。

測試 seam：`IoMonitorPage`、`ValueDialog`，e2e 用真 `MainWindow` 連 `plc-sim`。

驗收：（契約層）
- [ ] 測試設定新增下列非必要點，連上 `plc-sim` 後 IO Monitor 都顯示正確值，分頁符合 FR-29：`I16`、`U32`、`I32`（含 `scale`、`unit`）、`F32`、`String`、Holding Register 位元（`N.bK`）、Input Register。
- [ ] 上述可寫點從 IO Monitor 手動寫入，`plc-sim` 收到的值正確、其他位元或相鄰位址不受影響；超出範圍或格式錯誤時，視窗顯示原因且不寫入。
- [ ] `word_order=high_first` 的非必要 `U32` 能正確讀寫。
- [ ] `Speed` 維持 1 ~ 100。
- [ ] Run: `uv run --system-certs pytest tests/ui/test_io_monitor.py tests/e2e/test_e2e_points_io.py -q` → 全綠

禁區：
- 不改左右欄與整體版面。

留給實作：
- 非整數 `scale` 與 `F32` 的顯示位數；邊界：同一點重新整理時位數不跳動。
- 數值頁的分頁文字；邊界：須涵蓋 Holding Register 與 Input Register。

---

### Task 9: 打包、文件與整合驗收 [ ]

Files:
- Modify: `app/packaging/build.py`、`THIRD_PARTY_NOTICES.md`、`plc_motion.spec`
- Modify: `docs/specs/spec-modbus-master-app.md`、`docs/design/design-system-blueprint.md`、`app/README.md`、`CLAUDE.md`
- Create: `app/tests/test_no_qt_in_core.py`
- Test: `app/tests/test_packaging.py`

Difficulty: 簡單
Depends on: Task 8
Related: `docs/specs/spec-modbus-tags-migration.md` 的「主規格條文處置表」；`docs/design/design-modbus-tags-migration.md` 的 Blueprint Delta

介面契約：（契約層）

- pyserial 的發行套件沒有 LICENSE 檔，授權全文放進 `app/packaging/` 再由 `build.py` 複製；不要靠 `_copy_license` 從 metadata 取。
- 主規格依處置表逐條修訂；藍圖加入 Blueprint Delta 與變更紀錄；`app/README.md`、`CLAUDE.md` 的設定路徑與指令依新結構更新。
- `test_no_qt_in_core.py` 掃描 `engine/`、`plc/`、`settings/`、`codec.py` 的匯入。

測試 seam：`test_no_qt_in_core.py`、`pytest -m slow` 的打包測試。

驗收：（契約層）
- [ ] 自動測試掃描交握邏輯、通訊 adapter、快照與設定載入的匯入，出現 PySide6 就失敗；此測試在匯入 PySide6 的突變下會失敗。
- [ ] 打包後的 exe 不帶 `plc_motion.sim` 也能啟動，連上 `plc-sim` 後可以讀到快照；輸出資料夾含 pyserial 授權聲明與新的 config 結構。（觀測：`uv run pytest -m slow`）
- [ ] app 全套 `pytest`、`-m slow`、`ruff check`、`ruff format --check` 通過；已知的環境失敗（主規格 AC-33 前景鎖定）要列出並單獨確認，不靜默略過。
- [ ] 實際啟動 `plc-motion` 與 `plc-sim`，截圖確認 IO Monitor 與軸控頁顯示正確。
- [ ] Run: `uv run --system-certs pytest tests/test_no_qt_in_core.py -q && uv run --system-certs pytest -m slow -q` → 全綠

禁區：
- 不修改 `docs/PLC_PC_Modbus_通訊規格.md`、`七軸_Modbus位址表.md`、xlsx。

---

## Pre-Mortem

### 如果這個 plan 會失敗，最可能的原因

1. Task 2 太大，改名和格式變更同時打到大量測試（機率：high）
   - 早期訊號：Task 2 改了很多測試後仍有大批失敗，或 implementer 回報範圍失控。
   - 緩解：先改 fixture 和 `IoPoint`，用 `pytest` 分目錄收斂。超過兩輪無法收斂就拆成「設定檔與載入」「改名」兩個 task，重新經使用者確認。
2. 非同步寫入在 e2e 造成不穩定（機率：medium）
   - 早期訊號：`tests/e2e` 偶爾失敗，常見於連續寫入後立刻斷言模擬器狀態。
   - 緩解：斷言改等到條件成立且有上限；若是實際時序錯誤，例如快照早於寫入造成誤判，就回到 design 分析，不放寬條件。
3. pymodbus 3.16.2 改變 `SimServer` 依賴的行為（機率：low）
   - 早期訊號：Task 1 的 sim 測試失敗。
   - 緩解：這是 Task 1 的驗收，及早發現。只改 `SimServer`，不動函式庫副本。

## 審查紀錄

- 已知且接受：同一 Action Code 的操作提早結束後立刻重下，舊快照可能讓 `WAIT_COPY` 提早成立，`Request = 1` 提早排入。寫入依序送出，`PC Action Code` 仍先到 PLC；窗口約一輪讀取加計時器週期，人工操作幾乎碰不到。日後若加自動重試，要重新評估。
