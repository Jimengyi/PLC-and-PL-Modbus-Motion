---
type: handoff
created: 2026-11-02
tags: [modbus, pyside6, motion, ui]
session_goal: "依使用者逐輪回饋改版 plc-motion 畫面（工業風、軸控重排、寫入前確認、通訊計時）並更換圖示"
next_step: "使用者手動試用新版畫面；依回饋續改，或接實機前請 PLC 工程師確認待調查項"
related: ["[[handoff-modbus-master-app]]", "[[spec-modbus-master-app]]"]
---

# Handoff
模式：完整
日期：2026-11-02
專案：PLC-and-PL-Modbus-Motion
承接自：handoff-modbus-master-app.md
摘要：依使用者六輪回饋（先本機網頁預覽定案、再實作）改版 plc-motion 全部畫面並更換六角形圖示，兩個 commit 已合併 main，使用者正在手動試用。

## 0. 錨點
- 分支：main（比 origin/main 多 1 個 commit：784624c，使用者自己推送，不要代推）
- HEAD：784624c 圖示：六角形（六軸平台）系統匣、程式與 exe 圖示
- 未提交：工作區乾淨（本交接檔除外）
- 最後驗證：圖示改動後 `uv run pytest -q tests/ui tests/test_app_entry.py tests/e2e/test_e2e_window.py::test_ac31_...` → 164 passed；`uv run pytest -q -m slow` → 9 passed（含 exe 圖示）；`ruff check`／`ruff format --check` 全過。完整套件最後一次在圖示改動前跑：562 passed、2 failed（皆為當時使用者試用改到 `app/config/io_points.csv`，已還原，之後未重跑）

## 1. 核心目標
把 plc-motion 改成自動化現場的工業風、精簡好辨別、OP 順手的介面；所有畫面修改先做本機網頁預覽給使用者確認，使用者說「改程式」才實作。

## 2. 已完成
- 做了什麼：系統匣、主視窗、工作列、exe 圖示改為六角形（六軸平台）；狀態以顏色＋形狀區分（動作中 ▶、異常 !、未連線外框在角上斷開）；啟動時設 AppUserModelID 讓工作列不顯示 python.exe 圖示；build.py 由同一份繪圖程式產生 `build/plc-motion.ico` 給 exe
- 影響：`app/src/plc_motion/ui/tray.py`（`draw_hex_icon`、`icon_image`、`ico_bytes`、`app_icon`）、`app/src/plc_motion/app.py`（`set_app_user_model_id`）、`app/packaging/build.py`、`app/packaging/plc_motion.spec`、spec FR-43
- 狀態：已驗證
- 驗證：見 §0；另以 Windows `ExtractAssociatedIcon` 取出 exe 圖示目視確認

- 做了什麼：畫面改版（commit f83ba22）：工業風深色主題（移除淺色與 device.toml `[ui]`）；頂列只剩連線／重新連線／急停，Servo 全部 ON／OFF、Reset Alarm 移到軸控「全軸操作」；軸控改為七張位置卡＋選定軸｜步進｜點位三欄，移除勾選多軸移動，選定軸加單軸目標位置移動；步距七軸可直接輸入（自訂只在本次執行有效）；點位取消格子就地編輯，改「編輯此點」視窗，新增「清除此點」；移動／原點復歸／存入／清除先跳確認視窗；IO Monitor 欄位為色條、名稱、描述、值，Check Status 只改色條，移除全部重設；狀態列通訊計時（讀取本次／平均／最大、請求筆數、輪詢週期，圓點變色，無進度條與百分比數字），點選開「通訊品質」視窗
- 影響：`app/src/plc_motion/ui/` 下 axis_page、jog_pad、points_panel、io_monitor、main_window、theme、themes/dark.qss，新檔 widgets.py、confirm_dialog.py、point_edit_dialog.py、comm.py；`app/src/plc_motion/worker.py`（`PollTiming`、`poll_timed`）；spec FR-28 ~ FR-36、FR-38、FR-40、FR-41、FR-45，新增 FR-59（確認）、FR-60（通訊計時）
- 狀態：已驗證
- 驗證：全套 562 passed（2 個失敗為試用設定檔，見 §0）；新測試做 7 處突變檢查全部抓到；實際啟動截圖檢查版面

- 做了什麼：修好上一輪記錄的不穩定測試 `test_server_stop_disconnects_within_one_poll`（等斷線後的 snapshot_ready 送到再斷言）
- 影響：`app/tests/test_worker.py`
- 狀態：已驗證
- 驗證：負載下（worker＋UI 測試同跑）連續 4 次全過

- 做了什麼：`app/config/positions.csv`、`io_points.csv` 還原為 repo 版本（使用者要求）
- 影響：工作區
- 狀態：已驗證
- 驗證：`git status` 乾淨

## 3. 關鍵決策
完整定案清單在 `.craft/ui-revisions/decisions.md`（gitignored），這裡只列容易被問起的：
- 決策：多軸同時移動只剩點位表「移動到此點」；選定軸只做單軸目標位置移動
- 原因：使用者要求七軸總覽只留目前位置
- 影響：e2e Harness 的多軸 `move` 改寫暫存點位 P200 後用點位表移動（`app/tests/e2e/conftest.py`）

- 決策：JOG、步進、Servo、Reset Alarm 不跳確認；其餘寫入動作都確認
- 原因：JOG 按住移動、步進一次一小步，每次確認無法操作（使用者同意）
- 影響：spec FR-59

- 決策：輪詢週期只在 device.toml 調整，畫面只顯示通訊計時與建議週期；「逾時／錯誤次數」實作為「斷線次數」
- 原因：使用者決定；讀取 retries=0，任何逾時或錯誤都直接斷線
- 影響：spec FR-60

- 決策：系統匣與程式圖示用第 1 輪預覽的 C 方案（六角形）
- 原因：使用者在 AskUserQuestion 中明確選擇（雖然先前評第 1 輪偏 Q 版，第 2 輪 E ~ H 精簡方案未採用）
- 影響：模擬器圖示與畫面未改（使用者說模擬器不需要改），但模擬器共用 dark.qss，外觀跟著新主題

### 已排除方案
- QSpinBox／QDoubleSpinBox 設 `NoButtons` — 套樣式表時內部文字框寬度變 1 px，數字看不到；改用 `ui/widgets.py` 的 `plain_spinbox`
- Qt `setDashPattern` 畫未連線虛線外框 — 小尺寸幾乎看不出缺口；改為每條邊只畫中段
- 從 Bash 背景啟動 plc-motion／plc-sim 給使用者看 — 視窗不顯示；改用 PowerShell `Start-Process`
- 第 1 輪圖示 A／B／D、第 2 輪 E ~ H — 使用者未採用

## 4. 目前狀態

### 停留點
使用者正在手動試用新版；plc-motion 與 plc-sim 目前都已關閉。本機預覽伺服器（`.craft/ui-revisions/preview/` 的 http.server 8765）可能仍在背景，不影響任何東西。

### 已知限制
- 待調查（環境）：`test_ac33_second_launch_raises_existing_window_to_foreground` 偶爾失敗；以改版前程式碼在 worktree 對照同樣失敗，屬 Windows 前景鎖定（前景是終端機時），非程式問題
- 刻意跳過：完整測試套件在圖示改動與設定檔還原後未重跑（只跑 UI、進入點、系統匣 e2e、slow）
- 刻意跳過：上一輪 handoff §4 的範圍外觀察（Reset Alarm 斷線殘留、斷線提示判斷、event_dialog 互相覆蓋、THIRD_PARTY_NOTICES 未列 Qt 附帶元件）仍未處理；`axes.toml` 暫定值與實機待調查項仍在

## 5. 下一步
1. 等使用者手動試用回饋；畫面再改時照 feedback memory：先改 `.craft/ui-revisions/preview/` 預覽（`python -I -m http.server 8765 --bind 127.0.0.1`），使用者說「改程式」才實作
2. 關閉 plc-motion／plc-sim 後跑一次完整套件確認：`cd app && uv run pytest -q`（約 3.5 分鐘，背景或加長 timeout），預期全綠
3. 使用者要求才提交；在 main 上先開分支再 commit，合併由使用者指示；推送由使用者自己做

## 6. 關鍵檔案與環境
| 路徑 | 角色 | 備註 |
|---|---|---|
| `.craft/ui-revisions/decisions.md` | 畫面改版定案清單 | gitignored |
| `.craft/ui-revisions/preview/` | 本機預覽頁（index.html、tray.html、tray2.html） | gitignored |
| `app/src/plc_motion/ui/widgets.py` | Panel、Lamp、SpeedStepper、plain_spinbox | |
| `app/src/plc_motion/ui/confirm_dialog.py` | 確認視窗；呼叫端用模組屬性 `confirm_dialog.confirm` | 測試以 autouse fixture `confirms` 取代 |
| `app/src/plc_motion/ui/comm.py` | CommStats、CommIndicator、CommDialog | |
| `app/src/plc_motion/ui/tray.py` | 六角形圖示與 .ico 產生 | |
| `app/tests/conftest.py` | `confirms` fixture（ConfirmRecorder） | |

## 7. 連動變更（已執行）
- CLAUDE.md：專案「app/ 指令」新增 — 用 PowerShell `Start-Process` 讓使用者看到視窗；套樣式表時 SpinBox 不設 `NoButtons`；確認視窗經 `confirm_dialog.confirm` 與測試 fixture `confirms`
- Memory：modify project `project-plc-motion-status.md` — 改版與圖示已合併、指向本交接檔；移除已還原的試用設定檔說明
- Memory：modify feedback `feedback-ui-local-preview.md` — 加入視覺風格偏好（工業風、精簡、不要 Q 版，先給 3 ~ 4 方案比較）

## 8. 接手選項
- A. 直接執行：從 §5 第 1 項開始（等使用者試用回饋）
- B. 驗證既有成果：§5 第 2 項，跑完整套件
- D. 切換相鄰工作：上一輪的範圍外觀察或實機準備

建議優先：使用者正在手動試用，先問試用回饋（A）；動任何程式前先做 B 確認完整套件全綠。
