---
type: spec
created: 2026-11-07
tags: [modbus, modbus-tags, migration, jog]
slug: modbus-tags-migration
status: approved
approved_by: user
related: ["[[design-modbus-tags-migration]]", "[[spec-modbus-master-app]]"]
---

# plc-motion 通訊層改用 modbus_tags Spec

本 spec 修訂 [[spec-modbus-master-app]]（以下稱主規格）的設定與通訊部分。受影響的主規格條文見「主規格條文處置表」，表內沒有列出的條文維持有效。

## Goal

plc-motion 的 Modbus 通訊改由 `modbus_tags` 負責，操作員可見的交握與安全行為不變，JOG 放開的寫入不必等到下一個輪詢週期；IO Monitor 可使用函式庫支援的全部型別。

## Non-Goals

- 不支援多台 PLC 或 RTU
- 不開啟自動重連
- 不用函式庫的模擬設備取代 `plc-sim`
- 不自動轉換舊版設定檔，只轉換 repo 內的預設設定
- 不翻譯函式庫的英文錯誤訊息
- 不恢復批次讀取失敗後逐點定位 Tag
- 不改交握流程與 PLC 通訊規格
- 不改 UI 版面，只改 B 接點燈號的資料來源、IO Monitor 的 `(B)` 標示條件、軸控頁新增 `(B)` 標示、IO Monitor 的分頁文字與多型別顯示和寫入
- 不把 app 改成事件驅動
- 不在本次抽出軸控 library

## Trigger Conditions

- When 操作員放開 JOG，then 寫 0 立刻送往 PLC，不等下一個輪詢週期。
- When 程式結束或操作員重新連線，then 已排入的寫入先有結果或逾時，才結束連線。
- When PLC 拒絕任何寫入，then 程式先盡力停止按住中的 JOG，再判定通訊中斷。
- When 現場確認接點型式，then 只改 `io_points.csv`，不改程式。

## Functional Requirements

### 基底函式庫

- FR-1：通道連線中，等待期間有寫入排入，或一輪結束時佇列已有寫入，都立刻開始新的一輪，順序是先寫入、再讀取。下一輪的時間從這一輪開始重新計算。
- FR-2：沒有寫入時，輪詢週期維持 `interval_ms`。
- FR-3：持續高速寫入時，每一輪仍完成讀取，快照持續更新。
- FR-4：通道停止時，不因等待被喚醒而多送請求。
- FR-5：基底版本升為 0.7.0，`CHANGELOG.md` 記錄此行為；本專案副本與基底 0.7.0 的原始檔內容相同。

### 設定

- FR-6：設定資料夾結構如下；`read_ranges.toml` 可省略，`positions.csv` 不存在時依主規格 FR-6 自動建立。

  ```text
  config/
  ├─ app.toml
  ├─ axes.toml
  ├─ positions.csv
  └─ modbus/Channel01/
     ├─ device.toml
     ├─ io_points.csv
     └─ read_ranges.toml
  ```

- FR-7：`app.toml` 包含交握逾時、Reset Alarm 脈衝、日誌保留天數，語意與預設值同主規格。
- FR-8：通道的 `device.toml` 依 `modbus_tags` 格式。預設設定為 Modbus TCP，連線目標與輪詢週期和舊版預設相同，請求逾時 2000 ms、請求不重試、`auto_reconnect = false`。
- FR-9：`io_points.csv` 依 `modbus_tags` 格式：`Data Type` 取代 `Sensor Type`；必要 Tag 的位置點為 `I32`、Attributes 寫 `scale=<倍率>;unit=<單位>`；B 接點寫 `invert=true`。預設設定的位址、倍率、單位與舊版相同，舊版 `ON` 的點轉成 `invert=true`。
- FR-10：`Visible`、`Check Status` 維持原語意；寫回 Check Status 時只改該欄，其餘文字與列順序不變，寫入過程中斷不得損毀檔案。
- FR-11：啟動檢查取代主規格 FR-8、FR-61 對舊格式的檢查，任一項不通過即顯示列出錯誤的視窗並結束。檢查分兩層，同一層的錯誤全部列出；第一層有錯時，第二層等第一層排除後才會出現（函式庫有錯時不提供點位資料）：
  - 第一層：不依賴點位資料
    - `modbus_tags` 回報的設定錯誤（訊息可為英文）
    - `axes.toml`、`app.toml` 缺少或無法解析；`axes.toml` 內容不符主規格 FR-8 的規則；`app.toml` 的交握逾時不是正數，或脈衝、日誌保留天數不是正整數
    - `positions.csv` 內容不符主規格的規則（檔案不存在時依主規格 FR-6 自動建立，不算錯誤）
    - `config/modbus/` 底下有 `Channel01` 以外的資料夾；`Channel01` 不存在、被停用或不是 TCP（與函式庫錯誤同時發生時也要一起列出）
  - 第二層：依賴點位資料
    - `Visible`、`Check Status` 欄位缺少或值不在主規格 FR-4 的列舉內
    - 主規格 FR-5 的必要 Tag 缺少，或其 Data Model、Data Type、Access、Address Length 與主規格 FR-8 型態表不符（Position 對應 `I32`）
    - 必要 Tag 使用了下列以外的 Attributes：`Current Position`、`Target Position` 的 `scale`、`unit`（兩者必填，倍率須為正整數）；Positive Limit、Negative Limit、Home Sensor、Emergency Stop 的 `invert`。設定後與預設值相同者（例如 `byte_swap=false`、`invert=false`）不算違規
    - 同一軸 `Current Position n`、`Target Position n` 的倍率或單位不一致
    - 同一 Unit ID、同一 Data Model 下位址範圍重疊；同一暫存器的不同位元不算重疊，同一位元算重疊
- FR-12：接點型式不再是必要條件。Positive Limit、Negative Limit、Home Sensor、Emergency Stop 可為 A 或 B 接點。
- FR-13：非必要點可使用函式庫支援的全部 Data Model、Data Type 與 Attributes，規則由函式庫檢查。
- FR-14：`--host`、`--port` 暫時覆蓋連線目標，不修改設定檔。

### 連線與輪詢

- FR-15：連線成功以第一輪讀取完成為準；失敗時維持主規格 FR-10 的未連線畫面，不自動重試。無論 `device.toml` 如何設定，斷線後都只由操作員重新連線。
- FR-16：已連線或連線中按重新連線，先依 FR-19 結束現有連線，再建立新連線；任何時候都只有一條通道在通訊。
- FR-17：每次取得新快照推進一次交握，與值有沒有變動無關；同一份快照不重複推進。
- FR-18：通訊品質顯示維持主規格語意：讀取耗時、輪詢週期、每輪請求數。週期以相鄰快照的時間差計算，寫入喚醒的輪次照計。連線後第一輪沒有週期值。

### 寫入

- FR-19：程式結束與重新連線時，已排入的寫入先有結果或逾時，才結束連線。
- FR-20：同一次命令的寫入依呼叫順序送出，主規格的交握寫入順序不變。
- FR-21：必要 Tag 的位置點寫入的 I32 值與 PC 端換算出的原始值完全相同，不因經過工程值轉換而偏差。

### 失敗處理

- FR-22：讀取失敗、逾時、斷線，或任何寫入失敗（含 PLC 回 Modbus 例外）都判定為通訊中斷，後續行為同主規格 FR-13：斷開連線、進行中的操作以「通訊中斷」結束、畫面顯示未連線、有軸 Busy = 1 或 JOG 按住中時提示「軸可能仍在移動，請按急停」。
- FR-23：寫入被 PLC 拒絕且連線仍在時，先盡力對按住中的 JOG 點寫 0，有結果或逾時後才斷開連線。重新連線與未預期的例外也一樣：只要結束連線時連線仍在且 JOG 按住中，就先寫 0。（實作階段修訂，2026-11-07：Task 6 審查發現實作範圍比原條文寬，使用者確認採用；寫 0 只會讓軸停止，與主規格 FR-58 同理。）無論寫 0 是否成功，都走 FR-22 的提示；失敗時另外記錄日誌。
- FR-24：交握不得以寫入失敗那一輪或之後的快照繼續推進；通訊中斷判定優先。
- FR-25：寫入失敗的訊息包含 Tag Name 與位址。批次讀取失敗的訊息包含該段的 Data Model 與位址範圍，不要求 Tag Name。

### 接點與顯示

- FR-26：交握、前置檢查、急停與極限判斷、軸控頁狀態一律使用邏輯值（True＝觸發），接點型式只由 `invert` 決定。
- FR-27：IO Monitor 的 Bool 燈號顯示 PLC 原始值；雙擊切換時，PLC 收到的原始值是目前原始值的相反值，與 `invert` 無關。
- FR-28：IO Monitor 的點名稱，以及軸控頁正極限、負極限、原點感測器燈號的名稱，只有 `invert=true` 時才標示 `(B)`。
- FR-29：IO Monitor 依型別分頁：Bool（含暫存器位元）在數位頁，其他型別在數值頁；Input Register 與 Discrete Input 一樣只能唯讀。
- FR-30：IO Monitor 的數值顯示：有 `scale` 時顯示工程值與單位，整數型別沒有 `scale` 時顯示整數，`F32` 顯示浮點數，`String` 顯示文字。
- FR-31：IO Monitor 手動寫入可寫的數值點：依型別、`scale` 與 `word_order`、`byte_swap` 檢查範圍並寫入；輸入不合法時視窗不關閉並顯示原因。必要 Tag `Speed` 維持 1 ~ 100。

### 模擬與打包

- FR-32：`plc-sim` 讀同一份設定，維持軸運動、交握與故障注入行為，主程式與它之間走真實 Modbus TCP。
- FR-33：`plc-sim` 支援 FR-13 的全部 Data Model 與 Data Type，含 Input Register 與暫存器位元；輸入點依接點型式以未觸發為初始狀態。自動測試可注入通訊延遲與特定點的寫入拒絕，以重現 AC-16、AC-21 ～ AC-23；這些能力不改變 `plc-sim` 的一般操作行為。
- FR-34：交握邏輯、通訊 adapter、快照與設定載入不依賴 Qt，由自動測試強制。
- FR-35：相依 pymodbus 符合 `modbus_tags` 要求的版本範圍；打包產物包含 pyserial 授權聲明，onedir 形式不變。
- FR-36：主規格、系統藍圖、`app/README.md`、專案 `CLAUDE.md` 依「主規格條文處置表」同步。

## 主規格條文處置表

| 主規格條文 | 處置 | 內容 |
|---|---|---|
| FR-1、FR-2 | 修訂 | 設定檔改為 FR-6 的結構；`device.toml` 的 `[handshake]`、`[log]` 移到 `app.toml` |
| FR-2、AC-32 的舊版 `[ui] theme` | 廢止相容 | 舊設定檔不自動轉換；介面仍只有深色 |
| FR-4、FR-5 | 修訂 | 欄位與型別依本 spec FR-9、FR-13 |
| FR-8、AC-2 | 修訂 | 檢查項目依本 spec FR-11；拿掉 Attributes 的接點要求，改驗本 spec AC-9 |
| FR-12、FR-13 | 修訂 | 讀取由函式庫規劃；失敗訊息依本 spec FR-25 |
| FR-14 | 維持 | 語意相同，覆蓋的是通道設定 |
| FR-16、FR-27、AC-20 | 維持 | JOG 放開永遠執行，模擬器紀錄在 100 ms 內收到 0 |
| FR-30、AC-27 | 修訂 | `(B)` 依本 spec FR-28 |
| FR-37 ~ FR-40 | 修訂 | 型別與顯示依本 spec FR-27 ~ FR-31 |
| FR-18、FR-21、FR-26、FR-43、FR-52 | 修訂 | 急停與極限條件改讀為邏輯值（觸發／未觸發），依本 spec FR-26；`plc-sim` 依接點型式產生原始值 |
| FR-44 | 維持並補充 | 除主規格 FR-27 視同放開的 JOG 寫 0 外，結束不寫入；已排入的寫入依本 spec FR-19 |
| FR-52、FR-53、FR-56、FR-62 | 維持並補充 | `plc-sim` 多型別依本 spec FR-33 |
| FR-54 | 維持並補充 | 打包內容依本 spec FR-35、AC-34 |
| FR-55 | 修訂 | 預設設定依本 spec FR-6 ~ FR-9 |
| FR-60 | 維持並補充 | 週期計算依本 spec FR-18 |
| FR-61、FR-62、AC-43、AC-44 | 修訂 | 檔案改放通道資料夾，格式與檢查依函式庫：沒有 `[[unit]]` 不再改用自動分組（刪檔才會）；接受 `input_register`；涵蓋錯誤只列 Tag Name。`plc-sim` 仍允許讀範圍內的空號 |

## Constraints

### Hard Constraints

- 交握寫入順序：`Target Position`、`Speed` → `PC Action Code` → `Request`
- 位址、倍率與資料格式以 xlsx 與 `七軸_Modbus位址表.md` 為準，I32 低位在前
- 程式判斷一律用 A 接點邏輯值，接點型式只由 `invert=true` 決定
- IO Monitor 的 Bool 燈號顯示 PLC 原始值
- 斷線後不補寫，重新連線後維持殘留交握檢查，以及 JOG 殘留立即寫 0
- 只用可商用的套件（PySide6、pymodbus、pyserial），維持 onedir 打包
- 基底只能相依 pymodbus，不能依賴 PySide6；副本內不留基底以外的未記錄修改

### Avoidances

- 交握引擎和 UI 盡量不動
- 只使用函式庫的公開介面，例外是載入設定與編解碼
- 不為這次遷移順便重構無關的程式

## Acceptance Criteria

### 基底（觀測：基底測試透過 `Bus` 與內建模擬設備，TCP、RTU 各跑）

- AC-1：`interval_ms = 1000`，一輪讀完後等待期間呼叫 `write()`，模擬設備在 500 ms 內收到寫入，且該寫入之後收到該輪的讀取請求。
- AC-2：用 `set_delay` 拉長讀取，在一輪讀取途中呼叫 `write()`：該輪結束後不等待，下一輪立刻送出寫入。
- AC-3：接續 AC-1，下一輪在寫入那一輪開始後約 1000 ms 才開始，不連續補跑。
- AC-4：不寫入時，連續 10 輪的開始間隔約等於 `interval_ms`。
- AC-5：另一條 thread 持續高速寫入時，快照時間持續前進，每輪寫入都在該輪讀取之前。
- AC-6：`stop()` 與寫入同時發生時，`stop()` 返回後模擬設備不再收到新請求，未送出的寫入以失敗結束。
- AC-7：基底全套測試與 ruff 通過；`modbus_tags.__version__` 為 0.7.0；副本與基底的 `src/modbus_tags/` 原始檔比對無差異（不含 `__pycache__`）。

### 設定（觀測：實際啟動 `plc-motion`，看錯誤視窗與結束碼）

- AC-8：repo 的預設設定可啟動，連上 `plc-sim`；所有必要 Tag 的位址、Unit ID、Data Model、倍率、單位與轉換前相同。
- AC-9：分別製造 FR-11 的每一種錯誤（每次一種），程式顯示列出該錯誤的視窗後結束，結束碼非 0。同時製造同一層的兩種錯誤時兩者都被列出（例：`axes.toml` 缺少與某列 Data Model 打錯；`Speed 1` 缺少與 `Visible` 值錯誤）。同時製造第一層與第二層的錯誤時，先列出第一層，排除後再列出第二層。
- AC-10：把 `Positive Limit 1` 的 `invert=true` 拿掉後可以啟動，`plc-sim` 把該點設為 1 時，軸控頁顯示正極限觸發、名稱沒有 `(B)`、正向 JOG 被拒。
- AC-11：在 IO Monitor 設定 Check Status 後重新啟動，狀態保留；比對檔案只有該欄改變。
- AC-12：用 `--host`、`--port` 連上 `plc-sim`，設定檔內容不變。

### 運動與時序（觀測：真 MainWindow／Worker 走 TCP 連 `plc-sim`，看模擬器寫入紀錄）

- AC-13：`interval_ms = 1000` 時按住再放開 JOG，模擬器在放開後 100 ms 內記錄 JOG = 0；預設週期下也一樣。
- AC-14：按住 JOG 時結束程式，模擬器紀錄最終該 JOG 點為 0；按住 JOG 時按重新連線，也一樣。
- AC-15：Code 2 多軸移動，每軸的寫入順序是 `Target Position`、`Speed`、`PC Action Code`、`Request`，寫入 PLC 的 I32 與 PC 端換算值完全相同。
- AC-16：`plc-sim` 加上通訊延遲，使快照晚於寫入時，移動與原點復歸都正確完成，不提前結束也不逾時。
- AC-17：PLC 值連續數輪不變時，交握逾時仍會在設定時間後觸發。
- AC-18：主規格中與移動、原點復歸、Reset Alarm、殘留交握、JOG 殘留有關的驗收條件全部維持通過。

### 失敗（觀測：畫面、程式日誌與模擬器紀錄）

- AC-19：啟動時連不上，主視窗照常開啟並顯示未連線；`plc-sim` 啟動後不會自動連上，按重新連線才連上。
- AC-20：連線中 `plc-sim` 中斷所有連線，畫面顯示未連線，操作以「通訊中斷」結束，之後不會自動重連。`device.toml` 設 `auto_reconnect = true` 且 `reconnect_interval_ms` 極短時也一樣，且下次手動連線仍執行殘留交握與 JOG 殘留檢查。
- AC-21：`plc-sim` 拒絕 `Speed` 寫入時按住 JOG：模擬器紀錄最終該 JOG 點為 0，畫面顯示未連線與「軸可能仍在移動，請按急停」，日誌含該 Tag Name 與位址。
- AC-22：`plc-sim` 拒絕 `Speed` 與 JOG 點的寫入時按住 JOG：畫面顯示未連線與同樣提示，日誌記錄寫 0 失敗。
- AC-23：Code 2 移動中 `plc-sim` 拒絕 `Target Position`：模擬器沒有收到該軸的 `Request = 1`，操作以「通訊中斷」結束。
- AC-24：以可控時序的替身，讓快照已更新且同時有寫入失敗：交握不推進，判定為通訊中斷。（觀測：引擎收到的 tick 次數與事件）
- AC-25：批次讀取被拒絕時，訊息含 Data Model 與位址範圍。
- AC-26：寫入失敗後重新連線，斷線前排隊的寫入不會送出；殘留交握與 JOG 殘留的處理同主規格。

### 接點、型別與顯示（觀測：真 MainWindow 連 `plc-sim`，看畫面與模擬器狀態）

- AC-27：B 接點點位在 `plc-sim` 為 1 時：IO Monitor 亮燈、名稱有 `(B)`；軸控頁燈號名稱有 `(B)`、顯示未觸發。改為 0 時：IO Monitor 不亮；軸控頁顯示觸發，對應方向 JOG 被拒。
- AC-28：Emergency Stop 為 B 接點且在 `plc-sim` 為 0 時，移動命令被拒絕，理由是急停；改為 A 接點時，`plc-sim` 為 1 才拒絕。
- AC-29：測試設定新增 `invert=true` 的可寫 Coil，在 IO Monitor 雙擊：模擬器收到目前原始值的相反值，燈號跟著模擬器原始值變化。
- AC-30：測試設定新增下列非必要點，連上 `plc-sim` 後 IO Monitor 都顯示正確值，分頁符合 FR-29：`I16`、`U32`、`I32`（含 `scale`、`unit`）、`F32`、`String`、Holding Register 位元（`N.bK`）、Input Register。
- AC-31：上述可寫點從 IO Monitor 手動寫入，`plc-sim` 收到的值正確、其他位元或相鄰位址不受影響；超出範圍或格式錯誤時，視窗顯示原因且不寫入。
- AC-32：`word_order=high_first` 的非必要 `U32` 能正確讀寫；同樣屬性放在 `Current Position 1` 時，啟動檢查報錯。

### 結構與打包

- AC-33：自動測試掃描交握邏輯、通訊 adapter、快照與設定載入的匯入，出現 PySide6 就失敗；此測試在匯入 PySide6 的突變下會失敗。
- AC-34：打包後的 exe 不帶 `plc_motion.sim` 也能啟動，連上 `plc-sim` 後可以讀到快照；輸出資料夾含 pyserial 授權聲明與新的 config 結構。（觀測：`uv run pytest -m slow`）
- AC-35：app 全套 `pytest`、`-m slow`、`ruff check`、`ruff format --check` 通過；已知的環境失敗（主規格 AC-33 前景鎖定）要列出並單獨確認，不靜默略過。
- AC-36：實際啟動 `plc-motion` 與 `plc-sim`，截圖確認 IO Monitor 與軸控頁顯示正確。

## Open Questions

- Worker 檢查快照的計時器週期：留給實作決定，須讓 AC-13、AC-17 成立，且不明顯增加 CPU 負載。
- 停止前與 JOG 寫 0 的等待上限：留給實作決定，須有上限，且讓 AC-14、AC-21 成立。
- `plc-sim` 拒絕特定寫入的注入方式：留給實作決定，須能重現 AC-21 ～ AC-25；不能用例外碼 1，函式庫遇到它會改用 FC16 重試。
- `app.toml` 的鍵名：留給實作決定，段落沿用 `[handshake]`、`[log]`。
- 非整數 `scale` 與 `F32` 的顯示位數：留給實作決定，同一點重新整理時位數不跳動。
- IO Monitor 數值頁的分頁文字：留給實作決定，須涵蓋 Holding Register 與 Input Register。

## 影響範圍（參考，非契約）

- 基底：`AI Project\Python Library\modbus-tags` 的 `src/modbus_tags/channel.py` 與其開頭說明、`docs/specs/spec-modbus-tags.md` 的 FR-26、FR-27、README、CHANGELOG、版本三處。
- app 預計新增：`plc/tags_link.py`；移除：`plc/modbus_link.py`、`plc/batching.py`、`settings/read_ranges.py` 與其測試。
- app 預計修改：`worker.py`、`app.py` 的結束流程、`settings/`、`plc/snapshot.py`、`plc/port.py`、`plc/model.py`、`ui/io_monitor.py`、`ui/value_dialog.py`、軸控頁燈號、`sim/`、`pyproject.toml`、`packaging/`、`config/`、測試的設定 fixture。
- 引擎 docstring（`engine.py:4-5`）與 `handshake.py:24` 的時序敘述要同步。

## Past Gotchas (Reference)

無
