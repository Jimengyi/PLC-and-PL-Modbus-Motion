---
type: handoff
created: 2026-11-02
tags: [modbus, pyside6, motion, ui]
session_goal: "依使用者試用回饋加入點位表與 IO Monitor 的選取／滑鼠停留標示，合併、打包、推送"
next_step: "使用者開新 session 問其他問題；無進行中工作"
related: ["[[handoff-modbus-master-app-2]]", "[[spec-modbus-master-app]]"]
---

# Handoff
模式：完整
日期：2026-11-02
專案：PLC-and-PL-Modbus-Motion
承接自：handoff-modbus-master-app-2.md
摘要：補跑完整套件後，依使用者回饋（表格滑鼠停留看不出在哪列）以預覽選定方案 B 實作，順帶修正選取列原本幾乎看不出來的問題；已合併 main、重新打包、使用者已推送。

## 0. 錨點
- 分支：main（與 origin/main 同步，使用者已推送）
- HEAD：51e1dde CLAUDE.md：補 Windows 11 style 表格選取與停留繪製規則
- 未提交：工作區乾淨（本交接檔除外）
- 最後驗證：4a3391e 上 `uv run pytest -q` → 574 passed；`uv run pytest -q tests/ui` → 162 passed；重新打包後 `uv run pytest -q -m slow` → 9 passed；ruff check／format 全過

## 1. 核心目標
依使用者試用新版畫面的回饋續改；本輪回饋只有一項（表格滑鼠停留），已完成。

## 2. 已完成
- 做了什麼：`app/config/io_points.csv` 還原（使用者試用改的 2 行）；`CLAUDE.md` 新增 Windows 11 style 選取／停留規則並提交 51e1dde
- 影響：`CLAUDE.md`「app/ 指令」
- 狀態：已驗證
- 驗證：`git status` 乾淨、與 origin/main 同步

- 做了什麼：重新打包 `app/dist/plc-motion/`
- 影響：`app/dist/plc-motion/plc-motion.exe`
- 狀態：已驗證
- 驗證：`uv run pytest -q -m slow` → 9 passed

- 做了什麼：點位表與 IO Monitor 的選取／滑鼠停留標示（commit 4a3391e，fast-forward 合併 main，分支已刪）。停留列淡灰底（`hover` #232930）＋列頭 4 px 亮藍邊條；已選取列深藍（`accent_dim`）白字，停留時再亮一階（`hover_selected` #1d4a75）；邊條欄文字一律右移 4 px；格子自訂底色（IO 狀態色條、點位超限）保留
- 影響：新檔 `app/src/plc_motion/ui/row_hover.py`（`RowHoverDelegate`、`enable_row_hover`）；`points_panel.py`（`self.row_hover`，邊條在第 0 欄）；`io_monitor.py` `_make_table`（邊條在 `NAME_COL`）；`theme.py` 新色 `hover`、`hover_selected`、`white`；spec FR-45；新測試 `app/tests/ui/test_row_hover.py`
- 狀態：已驗證
- 驗證：見 §0；5 處突變檢查，未抓到的那處（自訂底色判斷）確認為多餘程式碼已刪；直接建立兩個畫面截圖目視確認

- 做了什麼：補跑上一輪缺的完整套件
- 影響：無檔案變更
- 狀態：已驗證
- 驗證：568 passed（改動前）

## 3. 關鍵決策
- 決策：選取與停留底色都由 delegate 繪製，並清掉 style 的 Selected／MouseOver／HasFocus 狀態
- 原因：Windows 11 style 不採用 QSS `selection-background-color`（實測選取列只有 #1d2023），QSS `::item:hover` 只亮單格無法整列加邊條
- 影響：新增表格要有同樣外觀時用 `enable_row_hover(view, bar_column)`；規則已寫進 CLAUDE.md

- 決策：停留樣式採預覽 hover.html 方案 B
- 原因：使用者在 A（灰底）、B（灰底＋邊條）、C（外框）、D（灰底＋亮格）中選 B
- 影響：定案記在 `.craft/ui-revisions/decisions.md`「表格滑鼠停留」

### 已排除方案
- 測試用 `qtbot.mouseMove` 模擬停留 — 受實際游標位置干擾不穩定；改直接 `sendEvent` QMouseEvent 給 viewport
- 在 delegate 判斷「格子有自訂底色就不填」 — 多餘，`drawControl` 本來就把自訂底色畫在上層

## 4. 目前狀態

### 停留點
本輪工作全部完成並推送；使用者要開新 session 問其他問題。預覽伺服器已停，plc-motion／plc-sim 未執行。

### 已知限制
- 待調查（環境）：`test_ac33_second_launch_raises_existing_window_to_foreground` 偶爾失敗，屬 Windows 前景鎖定（沿自上一輪）
- 刻意跳過：`test_server_stop_disconnects_within_one_poll` 有 pymodbus「coroutine never awaited」RuntimeWarning，測試通過，未處理
- 刻意跳過：上一輪 handoff §4 的範圍外觀察（Reset Alarm 斷線殘留、斷線提示判斷、event_dialog 互相覆蓋、THIRD_PARTY_NOTICES 未列 Qt 附帶元件）、`axes.toml` 暫定值、實機待確認項仍未處理

## 5. 下一步
1. 聽使用者的新問題；畫面再改時照 feedback memory 先做 `.craft/ui-revisions/preview/` 預覽（`python -I -m http.server 8765 --bind 127.0.0.1`），使用者說「改程式」才實作
2. 動程式時在 main 先開分支；提交與合併等使用者指示，推送由使用者自己做

## 6. 關鍵檔案與環境
| 路徑 | 角色 | 備註 |
|---|---|---|
| `app/src/plc_motion/ui/row_hover.py` | 表格選取與停留繪製 | 兩張表共用 |
| `.craft/ui-revisions/preview/hover.html` | 停留方案 A ~ D 預覽 | gitignored |
| `.craft/ui-revisions/decisions.md` | 畫面改版定案清單 | gitignored |

## 7. 連動變更（已執行）
- CLAUDE.md：專案「app/ 指令」新增 — Windows 11 style 不採用 QSS 選取色、`::item:hover` 只亮單格，表格用 `ui/row_hover.py` 的 `enable_row_hover`（已在 51e1dde 提交）
- Memory：modify project `project-plc-motion-status.md` — 加入表格停留標示已合併、打包、推送，交接指向本檔；`MEMORY.md` 索引同步

## 8. 接手選項
- A. 直接執行：從 §5 第 1 項開始（等使用者的新問題）
- D. 切換相鄰工作：§4 已知限制中的範圍外觀察或實機準備

建議優先：沒有進行中的工作，直接回應使用者的新問題（A）。
