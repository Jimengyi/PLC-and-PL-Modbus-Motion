---
type: handoff
created: 2026-10-31
tags: [modbus, pyside6, motion]
session_goal: "依 craft 流程從零建立七軸 Modbus Master 桌面程式 plc-motion 與模擬 PLC plc-sim"
next_step: "在 app\\ 下手動試用 plc-sim＋plc-motion，再依實機確認清單接 PLC 測試"
related: ["[[plan-modbus-master-app]]", "[[spec-modbus-master-app]]", "[[design-modbus-master-app]]"]
---

# Handoff
模式：完整
日期：2026-10-31
專案：PLC-and-PL-Modbus-Motion
摘要：從空的文件 repo 建立 `app/`（Python 3.14 + uv + PySide6 + pymodbus），完成 design、spec、plan 與 19 個實作 task，準備 fast-forward 合併回 main。

## 0. 錨點
- 分支：modbus-master-app（worktree `.worktrees/modbus-master-app`，base main）
- HEAD：2cf6b63 chore: task 19 done
- 未提交：3 項，plan 翻 `status: done`、CLAUDE.md 補 app 執行規則、本交接檔；與本檔同一個 commit 提交
- 最後驗證：`cd app && uv run pytest -q && uv run ruff check . && uv run ruff format --check .` → 561 passed、8 deselected（slow）、ruff 全過，HEAD 2cf6b63 時執行

## 1. 核心目標
做一支 PC 端 Modbus TCP Master 桌面程式，依 `docs/PLC_PC_Modbus_通訊規格.md` 的交握控制七軸：IO Monitor、軸控（多軸同時移動、原點復歸、步進、JOG、點位表）、可縮到系統匣；裝置設定與點位表全部外部化；沒有實機 PLC，所以同時做模擬 PLC。

## 2. 已完成
- 做了什麼：Task 1 ~ 19 全部實作並各自通過獨立審查（codec、設定載入與啟動檢查、點位資料、預設設定、PLC 行為模型、模擬器伺服器、Modbus 連線、交握引擎兩段、背景 worker 與日誌、主視窗／主題／系統匣／單一實例、軸控頁、點位表、IO Monitor、殘留交握視窗、模擬器視窗、PyInstaller onedir 打包、兩批 e2e 驗收）
- 影響：`app/src/plc_motion/`、`app/tests/`、`app/config/`、`app/packaging/`
- 狀態：已驗證
- 驗證：見 §0；slow 測試（打包 7 項＋AC-36）在 Task 17、19 跑過全綠

- 做了什麼：spec 的 42 條 AC 全部有 e2e 測試覆蓋，經真 TCP 連 SimServer 驗證
- 影響：`app/tests/e2e/`
- 狀態：已驗證
- 驗證：`uv run pytest tests/e2e` 全綠（含在 §0 全套內）

- 做了什麼：e2e 階段抓到並修好兩個產品問題：點位表切換鎖定時誤觸存檔造成 UI 凍結約 5 秒（JOG 放開延遲 5 秒）；1600×900 下點位表軸勾選框被截斷
- 影響：`app/src/plc_motion/ui/points_panel.py`、`app/src/plc_motion/ui/axis_page.py`
- 狀態：已驗證
- 驗證：單元測試與版面測試含突變檢查；截圖 `.craft/modbus-master-app/screenshots/`（gitignored，只在主 repo）

- 做了什麼：修訂共用規格書：Request 寫回 0 時一併寫 PC_Action_Code = 0；交握提早異常結束一律寫回兩者
- 影響：`docs/PLC_PC_Modbus_通訊規格.md` 2.1、2.2、3.4 節
- 狀態：已決策
- 驗證：未跑，需實機確認 PLC 端行為（見 §4）

- 做了什麼：design、spec（四輪審查）、plan（三輪審查）、初版系統藍圖
- 影響：`docs/design/design-modbus-master-app.md`、`docs/design/design-system-blueprint.md`、`docs/specs/spec-modbus-master-app.md`、`docs/plans/plan-modbus-master-app.md`、`docs/bootstrap/bootstrap-plc-motion-master.md`
- 狀態：已決策

## 3. 關鍵決策
完整決策史在 `docs/design/design-modbus-master-app.md`，這裡只列容易被問起的：
- 決策：Servo ON 由人員觸發，程式啟動與結束都不寫 Servo
- 原因：使用者決定；刻意偏離規格書 2.0.4「啟動時自動寫入 1」
- 影響：spec FR-24 註明為刻意偏離

- 決策：JOG／Request 在 IO Monitor 的隱藏只由 `io_points.csv` 的 Visible 欄控制，程式不強制
- 原因：審查建議強制，使用者否決
- 影響：有人把 Visible 改成 O 時，從 IO Monitor 寫 JOG／Request 會卡在鎖定中寫不回 0

- 決策：重新連線讀到殘留交握（Request=1 或 PC Action Code≠0）時跳窗由操作員決定；JOG 殘留則自動寫 0
- 原因：自動補寫 Request=0 會讓 PLC 執行斷線前的舊命令；JOG 寫 0 只會停止
- 影響：spec FR-57、FR-58

- 決策：倍率放在 `io_points.csv` 的 Attributes，啟動時檢查同軸 Current／Target 一致
- 原因：使用者沿用 LabVIEW 習慣；我建議的單一來源（axes.toml）被否決
- 影響：FR-8 啟動檢查

### 已排除方案
- PyQt6 — GPL，商用需授權
- Nuitka — 4.x 改為 AGPLv3
- PyInstaller onefile — LGPL 要求 Qt DLL 可替換
- 主程式內建模擬模式 — 繞過通訊層，測不到通訊錯誤
- 每軸一條執行緒跑交握 — 共用一條 TCP 連線要加鎖，時序難追
- 沿用 LabVIEW 的 ini + Tab 設定檔格式 — 使用者說明那份只是參考，不與 LabVIEW 共用

## 4. 目前狀態

### 停留點
`craft:finishing` 合併流程中：使用者選了 merge 回 main（fast-forward，main 從開工後沒有新 commit）。本檔提交後要做：回主 repo checkout main、merge modbus-master-app、`git worktree remove .worktrees/modbus-master-app`、`git branch -d modbus-master-app`。

### 未解決問題
- 問題：`app/tests/test_worker.py::test_server_stop_disconnects_within_one_poll` 偶發失敗，約 15 次 1 次
- 已嘗試方案：確認與產品程式無關；失敗點是 `probe.status` 的 `snapshots[-1]` IndexError
- 推測方向：測試 race，第一筆 snapshot 未到就停伺服器。修法：`wait_connected` 之後再等到 `probe.snapshots` 非空才停伺服器

### 已知限制
- 待調查（需實機）：現場 PLC 必須以 PLC_Action_Code 決定動作、且待機時寫入 0 也會被複製；實體急停與極限；Unit ID 0；實際交握時序（設計估計從按下到開始移動約 0.6 ~ 1 秒）；現場電腦執行 `app/dist/plc-motion/plc-motion.exe`
- 待調查（需人工操作）：系統匣圖示真實雙擊叫回、標題列拖動不凍結、第二份程式啟動時第一份視窗確實到前景（自動測試以程式模擬或只驗可見）
- 刻意跳過：`app/config/axes.toml` 的軟體極限與步進距離是暫定值（X/Y/Z/升降 ±100 mm、步距 10/1/0.1；RX/RY/RZ ±30 deg、步距 1/0.1/0.01），待機構行程確認
- 刻意跳過（範圍外觀察，未處理）：Reset Alarm 脈衝中斷線時 PLC 端 Reset Alarm 可能殘留 1（FR-57 只檢查 Request 與 PC Action Code）；斷線提示「可能仍在移動」只看最後快照的 Busy 與 JOG，命令剛寫出、Busy 還沒拉起就斷線不會提示；`settings/positions.py` 缺公開驗證 API（點位表用暫存空表試匯入）；`DisconnectedWhileMoving` 與其他提示共用 `event_dialog` 會互相覆蓋；`THIRD_PARTY_NOTICES.md` 未列 Qt 附帶第三方元件（`opengl32sw.dll` 等）

## 5. 下一步
1. 完成合併：回主 repo `git checkout main`、`git merge modbus-master-app`（fast-forward）、`git worktree remove .worktrees/modbus-master-app`、`git branch -d modbus-master-app`
2. 手動試用：在 `app\` 下開兩個終端，`uv run plc-sim` 與 `uv run plc-motion --host 127.0.0.1 --port 5020`，用模擬器視窗觸發極限、急停、Alarm 看主程式反應
3. 修 `test_worker.py` 的不穩定測試（§4 的修法）
4. 接實機前請 PLC 工程師確認 PC_Action_Code 清零修訂與 §4 待調查項，並提供機構行程以更新 `axes.toml`

## 6. 關鍵檔案與環境
| 路徑 | 角色 | 備註 |
|---|---|---|
| `app/src/plc_motion/engine/engine.py` | 交握引擎 MotionEngine | 全部交握、鎖定、前置檢查規則 |
| `app/src/plc_motion/worker.py` | 背景執行緒 PlcWorker | 唯一做 Modbus I/O 與呼叫引擎的地方 |
| `app/src/plc_motion/plc/model.py` | PLC 行為模型 | 模擬器與單元測試共用 |
| `app/src/plc_motion/app.py` | MotionApp 組裝與 main | e2e 也用它 |
| `app/tests/e2e/conftest.py` | e2e Harness | 真 TCP 連 SimServer |
| `app/config/` | 預設設定檔 | io_points.csv 127 點、Unit ID 0 |
| `app/packaging/build.py` | onedir 打包 | 產物 `app/dist/plc-motion/` |

環境：Windows 11 + Git Bash；uv 0.11.15；Python 3.14.5；依賴 `pyside6-essentials`（非完整 pyside6）。

## 7. 連動變更（已執行）
- CLAUDE.md：專案「app/ 指令」節新增 — 手動試用指令、打包與 slow 測試、全套耗時、單一實例鎖、測試複製 config、uv TLS 與 PySide6 重裝修法、PySide6 signal 跨執行緒陷阱、文件位置
- Memory：add user `user-labview-background.md` — LabVIEW 背景、不熟 Python/Qt
- Memory：add feedback `feedback-ui-local-preview.md` — UI 先給本機網頁預覽，實作後截圖檢查
- Memory：add project `project-plc-motion-status.md` — 第一版完成、實機確認項與暫定值
- 本輪已 compact，未執行 delete

## 8. 接手選項
- A. 直接執行：從 §5 第 1 項開始
- D. 切換相鄰工作：§4 刻意跳過的範圍外觀察可各自開 issue 處理
- E. 深入未解：修 `test_worker.py` 不穩定測試

建議優先：若合併尚未完成先做 §5 第 1 項；之後使用者多半會先手動試用（§5 第 2 項），再決定是否處理不穩定測試與範圍外觀察。
