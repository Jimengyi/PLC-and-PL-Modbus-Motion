---
type: handoff
created: 2026-11-07
tags: [modbus, modbus-tags, migration, jog]
session_goal: "plc-motion 的 Modbus 通訊改用 modbus_tags 函式庫；基底加上寫入立刻喚醒並升為 0.7.0"
next_step: "實機確認 JOG 放開延遲與讀取範圍空號；處理範圍外的小問題"
related: ["[[plan-modbus-tags-migration]]", "[[spec-modbus-tags-migration]]", "[[design-modbus-tags-migration]]"]
---

# Handoff：通訊層改用 modbus_tags

## 結果

| 範圍 | 狀態 |
|---|---|
| 基底 `Python Library\modbus-tags` 0.7.0（寫入立刻喚醒） | 完成，已合併回基底 main（`6e99d11`） |
| 本專案 Task 1～9 | 全部完成，每個 task 都經獨立審查 |
| 完整驗證（`b60ecf9`） | pytest 757 passed、`-m slow` 9 passed、ruff check／format 通過 |
| 畫面確認 | 軸控頁 `(B)` 標示、IO Monitor 原始值燈號與分頁，截圖檢查正確 |

- 通訊：Worker 以 20 ms 計時器呼叫 `TagsLink.poll()`；寫入排入函式庫佇列，JOG 放開立刻送出。
- 設定：`config/app.toml` 與 `config/modbus/Channel01/`，兩層啟動檢查，`auto_reconnect` 一律關閉。
- 結束程式最壞等待預設約 8.9 秒，只在 PLC 不回應時才會等到。
- 平行執行過 Task 3、4（獨立 worktree），合併後一起驗證。

## 實作中使用者裁決的事

- 結束程式的最壞等待從約 29 秒縮到約 8.9 秒，公式只保留一份。
- spec FR-23 補上：重新連線與未預期例外時，只要連線仍在且 JOG 按住中也先寫 0。

## 未解問題（範圍外，不擋合併）

- `sim/sim_log.py` 的值型別註記沒有涵蓋 float、str，不影響執行。
- `engine.write_point` 的說明仍寫「範圍檢查由畫面負責」，實際上 Worker 也攔截範圍錯誤。
- 主規格 FR-44 行尾寫死「約 8.9 秒」，預設逾時改動後會失真。
- e2e 偶發 Qt 物件回收時機崩潰：Task 8 在 teardown 加清理緩解，根因未找到。
- `PlcPort.read_all` 只剩 `ModelPort` 使用，介面是否收斂留待之後。

## 下一步

1. 實機確認：JOG 放開到 PLC 收到 0 的延遲；PLC 是否接受讀取範圍內的空號位址；`axes.toml` 暫定值。
2. 視需要處理上面的範圍外問題。
3. 之後另開一輪：把交握等業務邏輯抽成不含畫面的 Python 軸控 library（design「後續」一節）。
