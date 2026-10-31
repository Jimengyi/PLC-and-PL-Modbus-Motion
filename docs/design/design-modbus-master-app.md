---
type: design
created: 2026-10-31
tags: [modbus, pyside6, motion]
slug: modbus-master-app
status: approved
approved_by: user
related: ["[[design-system-blueprint]]", "[[bootstrap-plc-motion-master]]"]
---

# 七軸 Modbus Master 程式 Design

> 當 plan / implementation 對 spec 的原意有疑義時，這份 design 檔就是原始理由的唯一真理來源。

## Context

- repo 原本只有通訊規格文件：`docs/PLC_PC_Modbus_通訊規格.md`（單軸交握）、`docs/七軸_Modbus位址表.md`、`README.md`（倍率與 I32 格式）。PC 端過去以 LabVIEW 實作。
- 2026-10-31 以 bootstrap 建立 `app/`（Python 3.14、uv、PySide6、pymodbus、PyInstaller onedir），技術棧理由見 [[bootstrap-plc-motion-master]]。
- 目前沒有實機 PLC，開發與測試都要靠模擬 PLC。
- 使用者提供既有 LabVIEW 專案 `勝品\Project\Settings` 當參考（只參考，不與 LabVIEW 共用檔案）：
  - `Modbus Master\IO_Data_Settings_01.conf`：Tab 分隔，欄位 Tag Name、Start Address、Address Length、Unit ID、Data Model、Sensor Type、Access、Visible、Localized Label、Description、Attributes、Check Status；7 軸 × 18 點加 `Emergency Stop`，位址與 repo 位址表一致。
  - `Modbus Master\Device_Settings_01.conf`：IP `192.168.1.105`、Port `502`、Unit_ID `0`、Timeout 2000 ms。
  - `Modbus Master.conf`：`Polling_Interval(ms) = "200"`。
  - `Point Table\Station Manager\7-Axis_Point_Data_01.txt`：200 列、11 欄（#Point、Name、X、Y、Z、RX、RY、RZ、Lift、Tool、Description）。
- 使用者另外確認的事實：
  - 軸號對應：第 1 ~ 6 軸為 X、Y、Z、RX、RY、RZ，第 7 軸為升降軸（已同步到 repo 文件）。
  - 本專案 PLC 只支援 Action Code 1（原點復歸）、2（絕對移動），不支援 3（相對移動）。
  - Attributes 的 `ON` 代表 B 接點；Visible 填 `X` 代表不顯示；Check Status 是點檢狀態（OK、NG、Unchecked、Recheck、Ignore）；Unit ID 以每一點自己的為準。

## Problem Statement

- 要一支 PC 端 Modbus TCP Master，依規格書的交握控制 PLC 七軸，提供軸控操作、點位表、IO 點檢，並可縮到系統匣常駐。
- 裝置設定、點位表、軸參數都不能寫死在程式裡。
- 沒有實機 PLC 時要能開發、測試完整交握，包括急停、極限、Alarm、逾時等異常路徑。

## Approaches Considered

### 程式結構

A：UI 按鈕直接呼叫 pymodbus（否決）
- 交握邏輯散在各按鈕處理函式；多軸同時動作與全域鎖難以做對，也無法脫離 UI 測試。

B：每軸一條執行緒跑交握（否決）
- 7 條執行緒共用一條 TCP 連線要加鎖，時序問題難追。

C：單一背景執行緒 + 每軸狀態機（採用）
- 所有 Modbus I/O 與交握在一條背景執行緒依序執行（LabVIEW Producer/Consumer 的 Consumer Loop）。每輪輪詢讀一次快照，交握引擎依快照推進各軸狀態機並寫入。
- 代價：每個等待點至少一輪輪詢，從按下到軸開始移動約 0.6 ~ 1 秒。先以 200 ms 實測，太慢再考慮交握期間縮短輪詢週期。

### 設定檔格式

- 第一輪選 TOML（裝置、軸參數）+ CSV（點位表）。
- 看到 LabVIEW 專案後曾提議沿用其 ini + Tab 格式；使用者說明那份只是參考、不與 LabVIEW 共用，所以維持 TOML + CSV，CSV 欄位參考 LabVIEW 版。
- CSV 讓點位表能用 Excel 對照 xlsx 編輯；代價是台灣 Windows Excel 存 CSV 預設 Big5，讀取要同時支援 UTF-8 與 Big5。

### 點位表逐點列出 vs 只寫位址規律

- 選逐點列出。第 2 ~ 6 軸位址是推算的，萬一某軸不照規律，改一列即可。

### 程式怎麼找到「第 n 軸的 Ready」

- 曾提議新增 Axis、Role 欄位。使用者否決：Tag Name 本來就是程式取用點位的唯一名稱，之後還會手動新增氣缸等非軸 IO。
- 改為命名規則：軸相關的 Tag 固定為「`<名稱> <軸號>`」（如 `Ready 1`），共用急停固定為 `Emergency Stop`；另加 Display Name 欄位供畫面顯示。
- 否決「在 axes.toml 逐軸寫 Tag 對應」：每軸多 18 行，而現有命名本來就規律。

### 倍率放哪裡

- 我建議只放在軸參數檔，避免 Current／Target 兩行倍率不一致造成 100 倍錯位。
- 使用者決定放在 io_points 的 Attributes（沿用 LabVIEW 版習慣）。補償措施：啟動時檢查同一軸 Current Position 與 Target Position 的倍率一致，不一致拒絕啟動。

### 模擬 PLC

- A：獨立程式透過 TCP 扮演 PLC（採用）。主程式連模擬器與連實機走同一條 Modbus 路徑。
- B：主程式內建模擬模式（否決）：繞過通訊層，測不到通訊錯誤。
- PLC 行為模型寫成不含 I/O 的純邏輯，同時用在模擬器（包上 pymodbus server）與單元測試（直接當 PLC 讀寫介面的 adapter）。

### IO Monitor 的寫入與動作鎖

- 問題：在 IO Monitor 把 JOG_Plus 或 Request 寫 1 會讓軸 Busy，而 Busy 觸發全域鎖後就無法寫回 0，軸停不下或卡在交握。
- 否決「寫入不受鎖限制」與「工程模式」；採用使用者的做法：JOG Plus、JOG Minus、Request 在 IO Monitor 不顯示（Visible = X），其餘 Output 受動作鎖限制。

### 軸控畫面

- 預覽經三版（`.craft/modbus-master-app/preview/layout*-page.html`）：
  - 主框架：使用者選左側導覽且可收合（否決上方分頁）。
  - 軸控頁參考 LabVIEW 版 Axis Information／Move Position 的區塊配置，融合七軸總覽表與點位表；寸動採七軸方向鍵盤（否決只放選定軸的 −／＋）。
  - 拿掉軸控頁的 Digital Inputs／Outputs 清單，完整 IO 在 IO Monitor。
  - IO Monitor 改為輸入／輸出兩欄，Check Status 用顏色區分。
  - 深色主題為主，可切換淺色。

## Recommendation

採用單一背景執行緒 + 每軸狀態機的交握引擎，前後各切一個 seam：

- 交握引擎 ↔ PLC：「PLC 讀寫介面」有兩個 adapter——正式用的 Modbus 連線、單元測試用的 PLC 行為模型。交握規則（逾時、全域鎖、多軸、原點順序、軟體極限）全部在介面這一側以行為模型測完。
- 交握引擎 ↔ UI：UI 只送命令、收快照與事件，不碰 Modbus；引擎可以不開 UI 測試。

交握規則的要點（細節進 spec）：
- 一次操作先逐軸檢查全部前置條件，任一軸不符整個操作不執行（不會出現部分軸出發）。
- 相對移動與寸動在 PC 端以目前位置換算成 Code 2，再檢查軟體極限。
- 原點復歸含升降軸時先做升降軸，完成後其他軸同時做；升降軸失敗則不做第二階段。單軸原點復歸允許。
- 有操作進行中、任一軸 Busy、或通訊中斷時，所有寫入都拒絕；唯一例外是 JOG 放開寫 0。
- 多軸操作中一軸 Alarm，其他軸繼續；全部軸有結果、且全部 Busy = 0 才解鎖。

犧牲：
- 命令延遲約 0.6 ~ 1 秒（單執行緒輪詢推進）。
- 倍率放在 io_points 而非單一來源，靠啟動檢查補救。
- 設定檔錯誤時程式直接拒絕啟動，現場不能「先將就用」。

## Non-Goals 與 Hard Constraints

Non-Goals（刻意不做）：
- 自動模式與流程編排（下一步自動執行）
- 使用者登入與權限分級
- 斷線自動重連（只做手動重新連線）
- 軟體極限限制 JOG
- 單軸的 Servo 與 Reset Alarm 按鈕（只有全部軸各一顆）
- 軸位置趨勢圖
- 多國語系（介面只做繁體中文）
- 同時連線多台 PLC
- 在程式裡編輯 IO 點位表的位址（只能改 Check Status）
- 安裝精靈（onedir 資料夾複製即用）
- 點位表的 Tool 欄
- Alarm Code 的文字對照（只顯示數值）
- 模擬器打包給現場

Hard Constraints（絕對不能動）：
- 商業部署不產生授權費：不改用 PyQt、Nuitka；打包維持 onedir，附授權聲明
- 交握流程與逾時照規格書：等動作完成無逾時，其他等待 5 秒
- 該軸 Find_Home_Finish = 0 時不下 Code 2
- 任一軸動作中，所有寫入一律鎖住（含 IO Monitor）
- 不自動 Servo ON；結束程式不自動 Servo OFF
- 不要求 PLC 端改規格

Avoidances（偏好但可協商）：
- 介面文字用繁體中文
- Modbus I/O 不在 UI 執行緒
- 主程式與模擬器走同一條 Modbus 路徑，不留假連線捷徑

Past Gotchas（無 `docs/gotchas/`，以下取自 repo 文件）：
- I32 由兩個 Word 組成，低位在前，有號；負值高位為 65535（`README.md`「資料格式」節）
- 升降軸倍率 100、X/Y/Z 10000，差 100 倍（`README.md`「注意事項」）
- Excel 另存 CSV 可能是 Big5
- 原點復歸時 PLC 先 Busy = 0 才 Find_Home_Finish = 1，完成要等兩者（規格書 2.0、2.3 節）

## Blueprint Delta

藍圖連結：[[design-system-blueprint]]

本工作建立初版藍圖（專案原本沒有程式，也沒有藍圖），全部方塊皆為新增，結構見藍圖本體，此處不重畫。
