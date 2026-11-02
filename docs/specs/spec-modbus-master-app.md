---
type: spec
created: 2026-10-31
tags: [modbus, pyside6, motion]
slug: modbus-master-app
status: approved
approved_by: user
related: ["[[design-modbus-master-app]]"]
---

# 七軸 Modbus Master 程式 Spec

## Goal

交付 PC 端 Modbus TCP Master 桌面程式 `plc-motion`（PySide6）與開發用模擬 PLC `plc-sim`：依 `docs/PLC_PC_Modbus_通訊規格.md` 的交握控制七軸，提供軸控、點位表、IO Monitor、系統匣常駐；裝置設定、點位表、軸參數全部外部化。設計理由見 [[design-modbus-master-app]]。

## 用語

- 步進：按一次移動一個固定距離（長／中／短），以 Action Code 2 送出。規格書稱 JOG 為「寸動」，本 spec 不用「寸動」一詞以免混淆。
- JOG：按住連續移動、放開停止，對應 JOG Plus、JOG Minus 兩個 Coil。
- 操作：使用者按一次按鈕觸發的整組命令（例如勾選三軸移動）。
- 鎖定：見 FR-15。

## Non-Goals

- 自動模式與流程編排
- 使用者登入與權限分級
- 斷線自動重連
- 軟體極限限制 JOG
- 單軸的 Servo 與 Reset Alarm 按鈕
- 軸位置趨勢圖
- 多國語系（介面只做繁體中文）
- 同時連線多台 PLC
- 在程式裡編輯 IO 點位表的位址等欄位（只能改 Check Status）
- 安裝精靈
- 點位表的 Tool 欄
- Alarm Code 的文字對照（只顯示數值）
- 模擬器打包給現場

## User Stories / Trigger Conditions

- US-1：操作員開啟程式後，程式自動連線 PLC，畫面即時顯示七軸狀態與位置。
- US-2：操作員勾選多軸、填目標值（絕對或相對）、按一次執行，各軸同時移動，全部停止後才能做下一個操作。
- US-3：操作員用步進鍵盤以長／中／短步距微調任一軸，或按住 JOG 連續移動。
- US-4：操作員對勾選軸、全部軸或單軸做原點復歸；含升降軸時升降軸先做。
- US-5：操作員把目前位置存成點位，之後選點位、勾軸，移動過去；點位表可匯入、匯出。
- US-6：工程人員在 IO Monitor 逐點確認 IO，標記 Check Status，必要時寫入 Output。
- US-7：程式縮到系統匣常駐，Alarm 或急停時跳通知。
- US-8：開發者沒有實機時，用 `plc-sim` 扮演 PLC，並能觸發極限、急停、Alarm 與交握故障。
- US-9：現場人員拿到 onedir 資料夾，修改 `config\` 內的設定檔後直接執行 exe。

## Functional Requirements

### 設定檔

- FR-1：設定檔放在 `config` 資料夾：打包後位於 exe 同層，從原始碼執行時位於 `app\config`。共五個檔：`device.toml`、`axes.toml`、`io_points.csv`、`positions.csv`、`read_ranges.toml`（可省略，見 FR-61）。
- FR-2：`device.toml` 至少包含：連線 host、port、連線逾時（ms）；輪詢週期（ms，預設 200）；交握逾時（秒，預設 5）；Reset_Alarm 脈衝時間（ms，預設 500）；日誌保留天數（預設 30）。（舊版的主題設定已移除，檔案裡留著不影響載入。）
- FR-3：`axes.toml` 每軸一段，包含：軸號（1 ~ 7）、顯示名稱、軟體極限最小值與最大值、步進長／中／短步距、預設速度（1 ~ 100）。數值單位與該軸的顯示單位相同（mm 或 deg）。
- FR-4：`io_points.csv` 每列一個點，欄位為 Tag Name、Display Name、Start Address、Address Length、Unit ID、Data Model（Coil、Discrete Input、Holding Register）、Sensor Type（Boolean、U16、Position）、Access（Read Only、Read Write）、Visible（O 或 X）、Description、Attributes、Check Status（OK、NG、Unchecked、Recheck、Ignore）。
  - Tag Name 是唯一名稱；Display Name 留空時畫面顯示 Tag Name。
  - Position 型的點佔 2 個 Holding Register，為有號 I32，低位 Word 在前；Attributes 寫 `倍率,單位`（如 `10000,mm`），實際值 = I32 ÷ 倍率。
  - Boolean 型的點 Attributes 寫 `ON` 代表 B 接點：讀到 0 為觸發。控制邏輯（極限、急停判斷）與畫面標示都依這個欄位。
  - 每一點以自己的 Unit ID 讀寫。
- FR-5：程式依固定 Tag Name 找到每軸的點：`<名稱> <軸號>`，名稱為 Ready、Busy、Find Home Finish、Reply、Alarm Code、PLC Action Code、Current Position、Positive Limit、Home Sensor、Negative Limit、Servo Motor、Speed、Target Position、PC Action Code、Request、Reset Alarm、JOG Plus、JOG Minus；七軸共用急停為 `Emergency Stop`。其他 Tag Name 可自由新增（例如氣缸 IO），只出現在 IO Monitor。
- FR-6：`positions.csv` 欄位為 #Point、Name、X、Y、Z、RX、RY、RZ、Lift、Description，固定 200 列（P001 ~ P200）。檔案不存在時自動建立 200 個空白點（座標為 0）。
- FR-7：CSV 讀取時接受 UTF-8（有無 BOM 皆可）與 Big5 編碼；程式寫出的 CSV 一律為 UTF-8 含 BOM。
- FR-8：啟動檢查，任一項不通過即顯示列出全部錯誤的視窗並結束程式：
  - 設定檔缺少（`positions.csv`、`read_ranges.toml` 除外）或格式無法解析
  - Tag Name 重複
  - 列舉欄位（Data Model、Sensor Type、Access、Visible、Check Status）不在 FR-4 的列舉內
  - FR-5 的必要 Tag 缺少，或其 Data Model、Sensor Type、Access、Address Length、Attributes 與下表不符
  - 同一軸 `Current Position n` 與 `Target Position n` 的倍率或單位不一致
  - 同一 Unit ID、同一 Data Model 下位址範圍重疊
  - 軟體極限最小值不小於最大值、步距不為正、預設速度不在 1 ~ 100
  - `read_ranges.toml` 的錯誤（見 FR-61）

  | 名稱 | Data Model | Sensor Type | Access | Address Length | Attributes |
  |---|---|---|---|---|---|
  | Ready、Busy、Find Home Finish、Reply | Coil | Boolean | Read Only | 1 | 空白 |
  | Alarm Code、PLC Action Code | Holding Register | U16 | Read Only | 1 | 空白 |
  | Current Position | Holding Register | Position | Read Only | 2 | `倍率,單位` |
  | Positive Limit、Negative Limit | Discrete Input | Boolean | Read Only | 1 | `ON` |
  | Home Sensor | Discrete Input | Boolean | Read Only | 1 | 空白 |
  | Servo Motor、Request、Reset Alarm、JOG Plus、JOG Minus | Coil | Boolean | Read Write | 1 | 空白 |
  | Speed、PC Action Code | Holding Register | U16 | Read Write | 1 | 空白 |
  | Target Position | Holding Register | Position | Read Write | 2 | `倍率,單位` |
  | Emergency Stop | Discrete Input | Boolean | Read Only | 1 | `ON` |

- FR-9：程式寫回 Check Status、點位資料時，寫入過程中斷不得留下損毀的檔案（原檔或新檔其一完整）。

### 連線與輪詢

- FR-10：程式啟動後自動連線。連不上時主視窗照常開啟，顯示未連線，不自動重試。
- FR-11：頂列與系統匣選單有「重新連線」，只由操作員觸發。
- FR-12：連線中依輪詢週期讀取 io_points 中的全部點（含 Visible = X 的點），以批次讀取完成：有 `read_ranges.toml` 時照 FR-61 的範圍；沒有時自動合併連續位址，只讀 io_points 定義的位址。
- FR-61：`read_ranges.toml` 讓使用者指定批次讀取範圍：
  - 每個 `[[unit]]` 段落一個 Unit ID（`unit_id`），`coil`、`discrete_input`、`holding_register` 各寫一串範圍，格式「起~迄」（含兩端，單一位址可只寫一個數字），多段以逗號分隔；留空或不寫表示該 Data Model 不讀。
  - 檔案不存在或沒有任何 `[[unit]]` 時改用自動分組。
  - 有範圍時每段一筆讀取請求、依檔案順序送出；範圍內 io_points 沒定義的位址也讀，值不使用；沒有任何點的範圍照樣讀。
  - 啟動檢查（併入 FR-8 的錯誤視窗）：範圍格式錯、起始大於結束、位址超過 65535、單段超過單筆上限（Coil、Discrete Input 2000，Holding Register 125）、同一 Unit ID 同一 Data Model 範圍重疊、`unit_id` 重複或不是非負整數、不認得的設定鍵（含 `input_register`）；io_points 有點不在任何範圍內，或跨越兩段範圍（例如 Position 的兩個 Word），列出該點的 Tag Name、列號與位址。
  - 批次讀取回 Modbus 例外、但範圍內每個點單獨重讀都正常時（PLC 不接受範圍內的空號位址），訊息改為該段批次讀取的 Data Model 與位址範圍，不帶 Tag Name（例外於 FR-13）。
- FR-13：任何讀寫失敗、逾時或 PLC 回傳 Modbus 例外時，判定為通訊中斷：斷開連線、進行中的操作以「通訊中斷」結束、畫面顯示未連線；斷線前有軸 Busy = 1 或 JOG 按住中時，另外提示「軸可能仍在移動，請按急停」；若為 Modbus 例外，訊息需包含 Tag Name 與位址。
- FR-14：命令列參數 `--host`、`--port` 可暫時覆蓋 `device.toml` 的連線設定，不修改設定檔。

### 全域動作鎖

- FR-15：下列任一成立時為「鎖定」：有操作進行中、任一 JOG 按住中、任一軸 Busy = 1、未連線、連線後第一次輪詢尚未完成、有 FR-57 的殘留交握尚未處理。
- FR-57：每次連線成功（含程式啟動時的自動連線）後，讀到任一軸 Request = 1 或 PC Action Code ≠ 0 即為「殘留交握」：
  - 程式不自動寫入，保持鎖定，跳出視窗列出這些軸與各自讀到的 Request、PC Action Code、Target Position，並警告「寫回 0 後，PLC 可能開始執行斷線前的命令；若不要執行該命令，請先按急停」
  - 視窗有「寫回 0」與「先不處理，保持鎖定」兩個選項。選寫回 0 時對列出的軸寫 Request = 0 與 PC Action Code = 0，並記錄日誌；選保持鎖定時，頂列提供重新開啟此視窗的入口
  - 判斷依據是連線時實際讀到的值，與程式是否重啟無關
  - 已連線期間不再觸發此檢查（正常交握中的 Request = 1、PC Action Code ≠ 0 不算殘留）
- FR-58：每次連線成功後的第一次輪詢讀到任一軸 JOG Plus 或 JOG Minus = 1 時，立即對該點寫 0，記錄日誌並在畫面提示「第 n 軸 JOG 於斷線期間未放開，已寫回 0」。寫 0 只會讓軸停止，不會觸發新動作，所以不比照 FR-57 等操作員決定（使用者決定，2026-10-31）。
- FR-16：鎖定時拒絕所有會寫入 PLC 的命令（移動、原點復歸、步進、JOG 按下、全部 Servo ON／OFF、全部 Reset Alarm、IO Monitor 寫入），也拒絕修改點位資料（存入目前位置、編輯、匯入）；對應的畫面控制項反灰。JOG 放開永遠執行。
- FR-17：操作結束且全部軸 Busy = 0 後解除鎖定。

### 交握

- FR-18：單軸的移動交握（Code 2）依序為：寫 Target Position（I32）與 Speed → 寫 PC Action Code → 等 PLC Action Code 等於 PC Action Code → Request = 1 → 等 Reply = 1 → Request = 0 且 PC Action Code = 0 → 等 Reply = 0 → 等完成。
  - 原點復歸（Code 1）不寫 Target Position 與 Speed，其餘相同。
  - 前三個等待各有交握逾時（預設 5 秒）；等待 Reply = 1 逾時時補寫 Request = 0 與 PC Action Code = 0。
  - 等完成不設逾時；Code 2 完成為 Ready = 1 且 Busy = 0；Code 1 完成還需 Find Home Finish = 1。
  - 任何等待中讀到 Alarm Code ≠ 0 或 Emergency Stop = 0，該軸以異常結束。
  - 交握在等完成之前因逾時、Alarm 或急停提早結束時，PC 寫 Request = 0 與 PC Action Code = 0 後才算結束。通訊中斷時無法寫入，程式不自動補寫，改由 FR-57 處理。
  - 「Request 寫回 0 時一併寫 PC Action Code = 0」是 2026-10-31 對規格書 2.2 第 9 步的修訂，目的是讓連續兩次相同命令碼時，等 PLC Action Code 一致的比對仍有效。
- FR-19：本專案只送 Action Code 1、2。相對移動與步進的目標值 = 最新讀到的目前位置 + 移動量，換算成 Code 2 送出。
- FR-20：寫入 PLC 的位置值 = 實際值 × 該軸倍率，四捨五入為 I32；速度限制在 1 ~ 100。
- FR-21：一次操作開始前逐軸檢查前置條件，任一軸不符則整個操作不寫入任何東西，並列出全部不符的軸與項目：
  - 移動與步進：已連線、Emergency Stop = 1、Ready = 1、Busy = 0、Alarm Code = 0、Find Home Finish = 1、目標值在該軸軟體極限內
  - 原點復歸：已連線、Emergency Stop = 1、Ready = 1、Busy = 0、Alarm Code = 0
- FR-22：多軸操作中各軸交握同時進行、各自結束；每軸結果為完成、Alarm、急停、逾時、通訊中斷、未執行（FR-23）其一；全部軸有結果時操作結束，結果逐軸顯示並記錄。
- FR-23：原點復歸的軸組含第 7 軸時，第 7 軸先做，完成後其餘軸同時做；第 7 軸未完成即不做其餘軸，其餘軸結果標示「未執行」。不含第 7 軸時直接同時做。
- FR-24：全部 Servo ON／OFF（軸控頁「全軸操作」）對七軸的 Servo Motor 同時寫 1／0。程式啟動與結束都不自動寫 Servo。這是對規格書 2.0.4「PC 程式啟動時自動寫入 1」的刻意偏離（使用者決定 Servo ON 由人員觸發）。
- FR-25：全部 Reset Alarm（軸控頁「全軸操作」）只對 Alarm Code ≠ 0 的軸：Reset Alarm 寫 1，經脈衝時間後寫 0，再於交握逾時內等 Alarm Code = 0 且 Ready = 1；逐軸回報成功或失敗，不自動重試。沒有任何軸 Alarm 時提示不需復歸。
- FR-26：JOG 按下時檢查：已連線、未鎖定、Ready = 1、Busy = 0、Alarm Code = 0、Emergency Stop = 1、該方向極限未觸發（B 接點讀到 0 為觸發）；通過後寫 Speed，再寫 JOG = 1。
- FR-27：JOG 放開時立即寫 0，不等輪詢週期。按住中滑鼠在按鈕外放開、視窗失去焦點、視窗最小化或縮到系統匣，都視同放開。

### 軸控頁

- FR-28：版面：上排七張位置卡，下方三欄依序為選定軸、步進、點位，1600×900 不需捲動。位置卡只顯示軸名、單位與目前位置（小數位數依倍率：10000 → 4 位、100 → 2 位）；該軸 Alarm Code ≠ 0 或任一方向極限觸發時，卡片上緣變為異常色並顯示原因（如「Alarm 2」「+極限」）。點選一張卡為選定軸。（2026-11-02 改版：原七軸總覽的勾選、燈號、Alarm Code、目標值與「執行勾選軸移動」「勾選軸原點復歸」移除，多軸同時移動改由點位表的「移動到此點」完成。）
- FR-29：選定軸的目標位置：絕對／相對切換、目標值輸入（單位與小數位數依該軸）、「移動」；以步進與 JOG 共用的速度做單軸移動。
- FR-30：選定軸區塊：Servo、Ready、Busy、Find Home Finish、原點感測器、Request、正極限(B)、負極限(B)、Reply 燈號（3 × 3）；PLC Action Code、Alarm Code 數值；FR-29 的目標位置；按住式 JOG− 與 JOG＋；單軸原點復歸。
- FR-31：全軸操作（選定軸區塊下方）：全部 Servo ON、全部 Servo OFF、全部 Reset Alarm、全部原點復歸（先升降軸，完成後其餘軸同時做）、七軸 Find Home Finish 燈號。
- FR-32：步進：步距為七軸各一個輸入框，OP 可直接輸入（大於 0、最小一個解析度，不超過該軸軟體極限行程，按 Enter 或離開輸入框套用）；長／中／短一鍵把 `axes.toml` 的步距填入七軸，改過任一格即顯示「自訂」。自訂步距只在本次執行有效，不寫回 `axes.toml`。速度以 −／＋ 鍵增減（每次 5 %，也可直接輸入），步進與 JOG 共用。鍵盤：X±、Y±（十字配置）、Z 上下、升降上下、RX±、RY±、RZ±。每按一次以 FR-19 換算送出一次移動。
- FR-33：JOG 鍵在該方向極限觸發時反灰；步進鍵在該軸 Find Home Finish = 0 時反灰。
- FR-34：點位表：200 列，顯示點位、名稱與七軸座標（說明不在表格顯示）；格子不能就地編輯。雙擊點位列或按「編輯此點」開編輯視窗：名稱、七軸座標、說明的輸入框；改過的欄位標示原值，座標不是數字或超出該軸軟體極限時標示且不能存檔，沒有修改也不能存檔，按「確認存檔」才寫回。表格中超出軟體極限的座標以異常色標示。
- FR-35：點位操作：選定點位、「編輯此點」、「存入目前位置」（寫入七軸目前位置）、「清除此點」（名稱、七軸座標、說明清空，座標為 0）、要移動的軸（可切換按鈕）、速度、「移動到此點」（以選取的軸做多軸絕對移動）、「匯入」（檔案須符合 FR-6 的欄位、恰好 200 列 P001 ~ P200、座標皆為數字、編碼符合 FR-7；不符合時顯示原因且點位表不變。符合時確認後整份取代並存檔；超出軟體極限的座標照收並依 FR-34 標示）、「匯出」（另存 CSV）。
- FR-36：點位資料任何修改確認後立即存檔。
- FR-59：寫入前確認：軸控頁的目標位置移動、單軸原點復歸、全部原點復歸、移動到此點、存入目前位置、清除此點，一律先跳確認視窗，列出各軸「目前 → 目標（或存入後、清除後）」與速度；預設焦點在「取消」，取消、Esc 或關閉都不送出。清除此點以異常色標示並註明無法復原。確認視窗開著時若變成鎖定，按確認也不送出。JOG、步進、Servo、Reset Alarm 不跳確認（JOG 是按住移動、步進一次一小步）。

### IO Monitor

- FR-37：分「數位」（Coil、Discrete Input）與「數值」（Holding Register）兩個分頁；每頁左欄為 Read Only 的點、右欄為 Read Write 的點；Visible = X 的點不顯示。
- FR-38：每列依序顯示 Check Status 色條、名稱（Display Name，B 接點加註「(B)」）、描述（Description 欄，去掉開頭底線：`_M22` 顯示為 `M22`）、目前值：Boolean 為燈號加 ON／OFF、U16 為數字、Position 為換算後數值與單位。
- FR-39：Read Write 的點：Boolean 雙擊切換並寫入；數值雙擊跳出輸入視窗，確認後寫入（U16 限 0 ~ 65535，Speed 限 1 ~ 100，Position 以實際值輸入並以倍率換算）。鎖定時不可寫入。
- FR-40：每列以左側色條標示 Check Status，整列不上底色；右鍵選單設定 Check Status，立即存回 `io_points.csv`（只改該列的 Check Status，其餘欄位、列順序不變）；顯示各狀態的數量；可用文字搜尋篩選名稱欄（Display Name，空白時為 Tag Name）。表格不顯示 Tag Name 與 Check Status 欄，也沒有「全部重設為 Unchecked」。

### 主視窗、系統匣、外觀

- FR-41：主視窗左側導覽（軸控、IO Monitor，圖示在上、名稱在下）；頂列：程式名稱、連線狀態與重新連線、急停狀態（觸發時以異常色填滿）；下方狀態列：最近訊息與 FR-60 的通訊計時。最小視窗尺寸 1600×900。
- FR-60：通訊計時：每輪輪詢量測讀完全部位址的時間（依序送出所有讀取請求）與相鄰兩輪開始的間隔。狀態列顯示本次、平均、最大讀取時間、每輪請求筆數、設定的輪詢週期；左側燈號依本次讀取佔週期的比例變色（> 80 % 警告色、> 100 % 異常色），百分比只在滑鼠提示，不顯示進度條與百分比數字。點狀態列開「通訊品質」視窗：結論、最近 300 輪讀取時間曲線（標出週期與 80 % 線）、本次／平均／最小／最大／95 % 落點、實際週期平均、斷線次數、統計輪數，以及建議週期（95 % 讀取時間 × 1.5，取整到 10 ms，最少 50 ms）。統計在每次連線與按「重新統計」時歸零，斷線次數只在「重新統計」時歸零。輪詢週期只在 `device.toml` 調整，畫面不提供修改。
- FR-42：最小化與按右上角關閉都縮到系統匣，輪詢繼續。系統匣右鍵選單：顯示主視窗、重新連線、結束；雙擊圖示叫回主視窗。
- FR-43：系統匣圖示為六角形（六軸平台），外框與中心點顏色依狀態：正常、有軸動作中、Alarm 或急停、未連線；狀態另以形狀區分（動作中中心 ▶、異常中心「!」、未連線外框在角上斷開）。「正常」版本同時是主視窗與工作列的程式圖示。任一軸 Alarm Code 由 0 變非 0、或 Emergency Stop 由 1 變 0 時跳 Windows 通知。
- FR-44：「結束」時若有軸 Busy = 1 或操作進行中，先確認；結束不寫入任何點。
- FR-45：只有一套工業風深色主題：中性灰底，顏色只用於狀態（運轉、警告、異常、選取）；數值使用等寬數字字型；按鈕高度至少約 34 px，主要操作更大。不提供主題切換。點位表與 IO Monitor 表格的已選取列為深藍底；滑鼠停留整列為淡灰底並在列頭加亮藍邊條，已選取列停留時深藍再亮一階。（2026-11-02 改版：原深淺色切換移除；同日加入表格停留標示。）
- FR-46：同一台電腦只能執行一份 `plc-motion`；再次啟動時叫出已執行的主視窗，新的那份結束。
- FR-47：介面文字為繁體中文。

### 日誌與錯誤

- FR-48：日誌每天一檔，放在 `logs` 資料夾（與 `config` 同層），超過保留天數的自動刪除。
- FR-49：日誌記錄：連線、斷線、通訊錯誤；每次操作的參數與逐軸結果；Alarm Code 與 Emergency Stop 的變化；IO Monitor 寫入；設定檔錯誤；未預期的例外。
- FR-50：未預期的例外寫入日誌並顯示錯誤視窗，程式不無聲結束；背景通訊發生未預期例外時視同通訊中斷，畫面保持可操作。

### 模擬 PLC

- FR-51：`plc-sim` 以 Modbus TCP server 提供 `io_points.csv` 定義的全部點，預設監聽 `127.0.0.1:5020`，可用參數改 host、port、設定資料夾。
- FR-52：模擬行為依規格書：
  - Servo Motor = 1 且無 Alarm、未動作時 Ready = 1；Servo OFF 時 Find Home Finish = 0
  - Ready = 1 且 Busy = 0 時把 PC Action Code 複製到 PLC Action Code
  - Request 上升緣 → Busy = 1、Ready = 0、Reply = 1；Request = 0 → Reply = 0，之後才開始動作
  - Code 2 以速度 % × 模擬最大速度逐步移動到 Target Position；完成 Busy = 0、Ready = 1
  - Code 1 移動到 0 後先 Busy = 0，稍後 Find Home Finish = 1；開始時 Find Home Finish = 0
  - JOG = 1（上升緣檢查條件）持續移動，= 0 停止
  - 移動中（原點復歸除外）觸發正／負極限：停止並 Alarm 2／3；待機時極限已觸發而目標朝向該極限：不動並報同樣的 Alarm
  - 動作中 Servo OFF：停止並 Alarm 4
  - 急停：Emergency Stop = 0、Ready = 0、Busy = 0、Alarm 1、Find Home Finish = 0；解除後 Emergency Stop = 1
  - Reset Alarm：原因已排除則 Alarm 0、Ready 1（軸停在極限上仍可清除）；未排除則維持
- FR-53：`plc-sim` 視窗可操作：
  - 每軸正極限、負極限、原點感測器切換；急停切換；模擬速度倍率
  - 直接設定任一軸目前位置（實際值）
  - 對指定軸注入 Alarm Code：動作中立即停止，Alarm Code 設為注入值、Busy = 0、Ready = 0。附「原因持續」勾選：勾選時 Reset Alarm 不清除，取消勾選後 Reset Alarm 才能清除
  - 故障注入「不回 Reply」「不複製命令碼」（逐軸）
  - 「中斷所有連線」：關閉目前的 Modbus 連線、所有點位與模擬狀態保留，之後仍接受新連線
- FR-62：`plc-sim` 讀同一份 `read_ranges.toml`：範圍內 io_points 沒定義的位址讀取回 0；範圍外的未定義位址，以及任何未定義位址的寫入，回 Illegal Data Address。
- FR-56：`plc-sim` 提供可檢視、可匯出為文字檔的紀錄，每筆帶毫秒時間：
  - 收到的寫入（Tag Name、值）
  - 連線建立與中斷
  - 讀取請求（至少可得知最近一次讀取時間與累計次數）
  - 模擬狀態變化（各軸 Ready、Busy、Reply、Find Home Finish、Alarm Code、PLC Action Code、Emergency Stop 的變化）

### 預設設定檔

- FR-55：repo 提供的預設 `config`：
  - `io_points.csv` 含 FR-5 全部必要 Tag，位址、型態依 `docs/七軸_Modbus位址表.md` 與 FR-8 型態表，Position 點的倍率與單位依該位址表（X/Y/Z `10000,mm`、RX/RY/RZ `100,deg`、升降軸 `100,mm`）
  - `JOG Plus n`、`JOG Minus n`、`Request n` 的 Visible 為 X，其餘為 O；Check Status 皆為 Unchecked
  - `device.toml` 連線值為 192.168.1.105:502、連線逾時 2000 ms，其餘依 FR-2 預設值
  - `read_ranges.toml` 取自使用者 LabVIEW 參考檔 `Range_Settings_01.conf`：Unit ID 0，Coil `20~84, 120~184`、Holding Register `20~83, 120~183`、Discrete Input `24576~24612`，每輪 5 筆請求（2026-11-02）
  - `axes.toml` 七軸名稱依序為 X、Y、Z、RX、RY、RZ、升降；軟體極限、步進距離為可運作的暫定值，並以註解標示「待依機構行程確認」
  - 不採納 review 建議「程式強制隱藏 JOG／Request」：使用者決定以 Visible 欄控制，程式不另行檢查（2026-10-31）

### 打包

- FR-54：以 PyInstaller onedir 打包 `plc-motion`：輸出資料夾含 exe、Qt DLL（獨立檔案）、`config`（預設設定檔）、第三方授權聲明檔；不含 `plc-sim`。

## Constraints

### Hard Constraints

- 商業部署不產生授權費：不使用 PyQt、Nuitka；打包維持 onedir，附授權聲明
- 交握流程與逾時依規格書；等動作完成不設逾時
- 該軸 Find Home Finish = 0 時不送 Code 2
- 任一軸動作中，所有寫入一律鎖住（含 IO Monitor）
- 不自動 Servo ON；結束程式不自動 Servo OFF
- 不要求 PLC 端修改規格

### Avoidances

- Modbus I/O 不在 UI 執行緒執行
- 主程式連模擬器與連實機走同一條 Modbus 路徑，不留假連線捷徑

## Acceptance Criteria

觀測點說明：「模擬器紀錄」指 FR-56 的紀錄，「模擬器寫入紀錄」「模擬器連線紀錄」分別指其中的寫入、連線與讀取部分；「模擬器狀態」指 `plc-sim` 端的點位值；「畫面」指 `plc-motion` 實際執行時的控制項狀態與顯示。

設定檔

- AC-1：以 repo 提供的預設 `config` 啟動 `plc-motion --host 127.0.0.1 --port 5020` 連上 `plc-sim`，畫面顯示已連線，七軸位置與燈號與模擬器狀態一致。（觀測：畫面 vs 模擬器狀態）
- AC-2：分別製造 FR-8 的每一種錯誤（每次一種；型態表逐欄至少各一例，含把 `Positive Limit 1` 的 `ON` 拿掉）啟動，程式顯示列出該錯誤的視窗後結束，結束碼非 0；同時製造兩種錯誤時兩者都被列出。（觀測：錯誤視窗內容、結束碼）
- AC-3：以 Big5 編碼存成的 `io_points.csv`（Display Name 含中文）啟動，畫面中文正確；程式寫回 Check Status 後，檔案為 UTF-8 含 BOM，再以 Excel 預設方式開啟中文正確。（觀測：畫面、檔案位元組）
- AC-4：刪除 `positions.csv` 後啟動，檔案被建立，含 P001 ~ P200 共 200 列。（觀測：檔案內容）
- AC-43：以預設 `config` 連上 `plc-sim`，主視窗的通訊狀態顯示每輪 5 筆請求，AC-1 成立；刪掉 `read_ranges.toml` 後重新啟動兩者，請求筆數變為自動分組的筆數，AC-1 仍成立。（觀測：畫面、模擬器狀態）
- AC-44：分別製造 FR-61 的每一種啟動檢查錯誤（每次一種），程式顯示列出該錯誤的視窗後結束，結束碼非 0；把 Coil 範圍改為 `20~79, 120~184` 時，錯誤列出第 7 軸 Coil 80 ~ 84 的四個點。（觀測：錯誤視窗內容、結束碼）
- AC-5：在 `io_points.csv` 新增一個非軸的 Tag（如 `Cylinder1 Up`，Coil、Read Write），並在 `read_ranges.toml` 加上它的位址，模擬器同樣讀這兩份檔；它出現在 IO Monitor 右欄，雙擊後模擬器狀態對應位址變為 1。（觀測：畫面、模擬器狀態）

連線

- AC-6：`plc-sim` 未啟動時開 `plc-motion`：主視窗出現、顯示未連線、所有寫入控制項反灰；之後啟動 `plc-sim` 再按重新連線，變成已連線。期間程式不自行重連。（觀測：畫面、模擬器連線紀錄）
- AC-7：已連線時關閉 `plc-sim`，一個輪詢週期內畫面顯示未連線、系統匣圖示變為未連線狀態、日誌記錄斷線。（觀測：畫面、日誌檔）
- AC-39：對 X 開啟「不回 Reply」並下移動，在 Request = 1 之後、5 秒逾時之前按模擬器「中斷所有連線」，再按重新連線：跳出殘留交握視窗列出 X（Request = 1、PC Action Code = 2）；連線後到操作員選擇前，模擬器寫入紀錄沒有任何寫入。選「先不處理」維持鎖定；再從工具列開啟視窗選「寫回 0」，紀錄出現 X 的 Request = 0 與 PC Action Code = 0，之後解鎖。（觀測：畫面、模擬器寫入紀錄）
- AC-40：重現 AC-39 的殘留狀態後，從系統匣選單「結束」`plc-motion` 再重新啟動，啟動連線後同樣跳出殘留交握視窗。對 Y 開「不複製命令碼」下移動，於等待期間「中斷所有連線」再重新連線（Request = 0、PC Action Code = 2），同樣跳出視窗列出 Y。（觀測：畫面）
- AC-42：按住 JOG+ 時按模擬器「中斷所有連線」，模擬器的 X 持續移動、JOG Plus 1 仍為 1；按重新連線後，第一次輪詢之後紀錄出現 JOG Plus 1 = 0、X 停止，畫面出現對應提示，日誌有紀錄。（觀測：模擬器紀錄、模擬器狀態、畫面、日誌檔）

交握與移動

- AC-8：點位表選一個點，移動的軸選 X、Y、Z（已原點復歸，三軸目標不同）並確認移動：模擬器寫入紀錄中每軸的寫入順序符合 FR-18；三軸都開始移動後才有任一軸完成；最後一軸停止前所有寫入控制項反灰，停止後解鎖；最終位置等於目標。（觀測：模擬器寫入紀錄、畫面）
- AC-9：相對模式對 X 輸入 −2.5 mm、目前位置 10 mm：模擬器收到的 Target Position 為 75000（7.5 × 10000），Action Code 為 2；全程沒有寫入 Action Code 3。升降軸相對 +1.25 mm 時收到值為目前值 + 125。（觀測：模擬器寫入紀錄）
- AC-10：負值目標 −100 mm（X 軸）寫入後，模擬器端兩個 Word 為低位 48576、高位 65520；在模擬器把 X 位置設為 −2.3456 mm（FR-53）後畫面顯示 −2.3456。（觀測：模擬器狀態、畫面）
- AC-11：以點位表移動 X、Y，其中 Y 的 Find Home Finish = 0：顯示 Y 未原點復歸、模擬器寫入紀錄中沒有任何一筆寫入。目標超出 X 軟體極限時同樣全部不寫入並指出 X。（觀測：畫面、模擬器寫入紀錄）
- AC-12：多軸移動中，對 Y 注入 Alarm 2：Y 的位置卡顯示異常、X 繼續移動到目標；X 停止前寫入控制項維持反灰；結束訊息逐軸顯示 X 完成、Y Alarm 2。（觀測：畫面、模擬器狀態）
- AC-13：先完成一次 X 軸 Code 2 移動，再開啟「不複製命令碼」故障並再下一次 X 軸移動：約 5 秒後該軸以逾時結束，模擬器寫入紀錄中沒有 Request = 1。開啟「不回 Reply」：約 5 秒後逾時，紀錄中 Request = 1 之後有 Request = 0。兩者之後皆解鎖。（觀測：畫面、模擬器寫入紀錄）
- AC-41：交握等待期間的提早結束都會寫回 0：
  - 開「不複製命令碼」下移動，於等待期間注入 Alarm：紀錄中沒有 Request = 1，有 PC Action Code = 0
  - 開「不回 Reply」下移動，於等待期間切換急停：紀錄中 Request = 1 之後有 Request = 0 與 PC Action Code = 0
  - AC-13 兩種逾時結束後，紀錄中都有 PC Action Code = 0
  （觀測：模擬器寫入紀錄）
- AC-14：移動中切換急停：該操作以急停結束；解除急停後按全部 Reset Alarm，Alarm 歸 0、Ready = 1，七軸 Find Home Finish 顯示未完成，步進鍵反灰，下移動被擋（原因為未原點復歸）。（觀測：畫面、模擬器狀態）
- AC-37：同一軸連續下兩次 Code 2 移動：模擬器紀錄中每次 Request = 0 都伴隨 PC Action Code = 0；第二次移動在 PLC Action Code 由 0 變為 2 之後才送 Request = 1。（觀測：模擬器紀錄）

原點復歸

- AC-15：全部原點復歸：模擬器寫入紀錄中第 7 軸的 Request = 1 早於第 1 ~ 6 軸任何一軸寫入 PC Action Code；第 1 ~ 6 軸在第 7 軸 Find Home Finish = 1 之後才開始，且彼此同時進行。（觀測：模擬器紀錄的寫入與狀態變化時間）
- AC-16：全部原點復歸時對第 7 軸注入 Alarm：第 1 ~ 6 軸無任何寫入，結果顯示「未執行」。（觀測：模擬器寫入紀錄、畫面）
- AC-17：原點復歸完成判定：模擬器在 Busy = 0 後延遲設 Find Home Finish = 1 的期間，畫面仍視為動作中、未解鎖。（觀測：畫面）
- AC-18：選定 RX 軸按單軸原點復歸，只有 RX 有寫入。（觀測：模擬器寫入紀錄）

步進與 JOG

- AC-19：X 目前 10 mm、步距「長」設定 10 mm，按 X+ 一次：模擬器收到 Target Position 200000、Action Code 2。目標超出軟體極限時不寫入並提示。（觀測：模擬器寫入紀錄、畫面）
- AC-20：按住 JOG+ 期間模擬器 X 位置持續增加；以自動化測試記錄放開的時間，模擬器紀錄中 JOG Plus = 0 的時間晚於放開不超過 100 ms，位置停止。按住期間把焦點切到其他視窗，同樣在 100 ms 內收到 0。（觀測：模擬器寫入紀錄、模擬器狀態）
- AC-21：正極限觸發（模擬器端切換）時 JOG+ 反灰、JOG− 可用。（觀測：畫面）

Servo 與 Reset

- AC-22：程式啟動與結束時，模擬器寫入紀錄中都沒有 Servo Motor 的寫入；按全部 Servo ON 時七軸 Servo Motor 都收到 1。（觀測：模擬器寫入紀錄）
- AC-23：移動中全部 Servo OFF、全部 Reset Alarm 按鈕為反灰，點擊不產生寫入。（觀測：畫面、模擬器寫入紀錄）
- AC-24：對兩軸注入 Alarm，其中一軸勾選「原因持續」，按全部 Reset Alarm：只有這兩軸收到 Reset Alarm 1 → 0（間隔約 0.5 秒）；結果顯示一軸成功、一軸失敗；程式不自動再送。（觀測：模擬器寫入紀錄、畫面）

點位表

- AC-25：選 P004，按存入目前位置並確認：`positions.csv` 的 P004 列立即更新為七軸目前位置；取消則不變。移動的軸只選 X、Y、Z，按移動到此點並確認：只有 X、Y、Z 移動，目標為 P004 的值。（觀測：檔案內容、模擬器寫入紀錄）
- AC-26：匯出後修改匯出檔再匯入：確認後點位表整份換成匯入內容並存檔；取消確認則不變。（觀測：畫面、檔案內容）
- AC-38：匯入只有 150 列、或某座標為文字的檔案：顯示原因，點位表與 `positions.csv` 皆不變。（觀測：畫面、檔案內容）

IO Monitor

- AC-27：JOG Plus、JOG Minus、Request（Visible = X）不出現在 IO Monitor；B 接點的點名稱帶「(B)」。（觀測：畫面）
- AC-28：雙擊 `Speed 1` 輸入 70，模擬器 D123 變為 70；輸入 150 被拒。雙擊 Servo Motor 1 切換後模擬器值改變。移動中雙擊不產生寫入。（觀測：模擬器狀態、模擬器寫入紀錄）
- AC-29：右鍵把某點設為 NG：該列左側色條變為 NG 的顏色（整列不上底色）、數量統計更新、`io_points.csv` 對應列的 Check Status 立即為 NG；重新啟動後仍為 NG。（觀測：畫面、檔案內容）

主視窗與系統匣

- AC-30：按最小化或右上角關閉，視窗消失、系統匣圖示存在、模擬器端仍持續收到輪詢；雙擊圖示叫回。系統匣選單「結束」在軸移動中先跳確認。（觀測：畫面、模擬器連線紀錄）
- AC-31：縮在系統匣時注入 Alarm，出現 Windows 通知、圖示變為異常顏色。（觀測：畫面）
- AC-32：介面為深色，畫面上沒有主題切換；`device.toml` 含舊版 `[ui] theme = "light"` 時照常啟動且仍為深色。（觀測：畫面）
- AC-33：已執行一份時再次啟動，不出現第二個主視窗，原本的主視窗被叫到前景。（觀測：畫面、程序列表）
- AC-34：通訊進行中（含交握等待期間）拖動視窗、切換頁面，畫面不凍結。（觀測：畫面）

日誌

- AC-35：完成 AC-8、AC-12 後，當日日誌檔含兩次操作的參數與逐軸結果，以及 Alarm 變化；把一個超過保留天數的舊日誌放進 `logs` 後重新啟動，該檔被刪除。（觀測：日誌檔）

打包

- AC-36：打包輸出的資料夾複製到另一個路徑，執行其中的 exe（不透過 uv、不需安裝 Python）以 `--host 127.0.0.1 --port 5020` 連上 `plc-sim`，AC-1 成立；資料夾內有授權聲明檔、Qt DLL 為獨立檔案、沒有 `plc-sim`。（觀測：畫面、資料夾內容）

## Open Questions

- 批次讀取如何分組：已由 FR-61 定案（2026-11-02）——有 `read_ranges.toml` 照設定，沒有則自動合併連續位址。實機是否接受範圍內的空號位址待接機確認；不接受時調整範圍或刪掉該檔。
- 交握期間是否縮短輪詢週期：本次不做；實作時若量到從按下到開始移動明顯超過 1 秒，回報而不自行加設定。
- 顏色、圖示、間距與字型：依 FR-45 的工業風深色主題，細節留給實作決定，邊界為 Check Status 五種狀態與「正常／動作中／異常／未連線」可互相區分，且版面結構符合 FR-28 ~ FR-41。
- 模擬器的最大速度與原點復歸延遲：留給實作決定，邊界為 AC-17 可被觀察到（延遲需長於一個輪詢週期）。
- 視窗大小與位置是否記憶：留給實作決定，不影響任何驗收。
- 預覽稿 v2 的 IO Monitor 有「依軸篩選」；因 Tag 不再有軸欄位（非軸 IO 也在同一表），改為 FR-40 的文字搜尋。

## 影響範圍（參考，非契約）

- 畫面預覽：`.craft/modbus-master-app/preview/layout-v3-page.html`（軸控頁）、`layout-v2-page.html`（IO Monitor）。`.craft/` 未納入 git，worktree 中看不到。
- 預設 `io_points.csv` 可由使用者的 LabVIEW 參考檔 `勝品\Project\Settings\Modbus Master\IO_Data_Settings_01.conf` 轉換：補 Display Name 欄、去掉 Localized Label 欄、Tab 改逗號（Visible 等內容要求見 FR-55）。
- 預設 `device.toml` 連線值取自 `Device_Settings_01.conf`（192.168.1.105:502、逾時 2000 ms）。
- 位址、倍率依據：`docs/七軸_Modbus位址表.md`、`README.md`。
- 接實機時要確認：PLC 以 PLC_Action_Code 決定動作（FR-18 會在 Request 寫回 0 時把 PC_Action_Code 清為 0），以及待機時寫入 0 也會被複製。

## Past Gotchas (Reference)

- I32 低位在前、有號，負值高位為 65535（`README.md`「資料格式」節）
- 升降軸倍率 100 與 X/Y/Z 的 10000 差 100 倍（`README.md`「注意事項」）
- Excel 另存 CSV 預設可能是 Big5
- 原點復歸時 PLC 先 Busy = 0 才 Find Home Finish = 1（規格書 2.0、2.3 節）
