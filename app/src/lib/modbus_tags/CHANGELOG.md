# Changelog

版本號與變更紀錄隨 `src/modbus_tags/` 一起複製到各專案，用來判斷副本從哪一版分出、源頭修正要補給哪些專案。

## [0.6.0] - 2026-11-07

- 移除 `device.toml` 的 `word_order`、`max_registers`、`max_bits`；還寫著這三項的設定檔會報 `unknown key`，刪掉即可
- `word_order` 只寫在點位的 Attributes，沒寫時為 `low_first`
- 單次讀取上限固定為 Modbus 標準（register 125、bit 2000）；上限較小的設備用 `read_ranges.toml` 把範圍切短
- `datatypes.decode`、`encode` 不再接受預設 word order 參數

## [0.5.0] - 2026-11-06

- 新增 `bus.stats(device)` 與 `PollStats`：連線後每輪讀取的最近、平均、最大耗時，以及每輪請求數與輪詢週期；輪詢與通訊行為不變

## [0.4.0] - 2026-11-06

- `DeviceInfo` 新增 `address`：TCP 為 `host:port`，RTU 為 COM port 名稱；`simulate=True` 時仍是設定檔的值

## [0.3.0] - 2026-11-05

- `open(create_missing=True)`：為 `devices` 宣告但尚未建立的通道產生停用的預設設定檔（`device.toml`、`io_points.csv`、`read_ranges.toml`），不覆寫既有檔案，寫入失敗會回滾
- 預設檔案內含啟用步驟說明與 4 個唯讀範例點位
- 不開啟時行為與 0.2.0 相同

## [0.2.0] - 2026-11-05

- 設定資料夾載入與檢查：TCP／RTU 通道、`devices` 開發者名稱對照、`display_name`
- 每條通道一個 thread 輪詢，自動分組讀取、連線狀態與自動重連
- `Bus`：`read`／`snapshot`／`write`／`write_wait`、`Ticket`、事件（`on_change`、`on_status`、`on_write_done`）
- register bit 寫入：FC22，設備不支援時改讀－改－寫
- 內建 `Simulator` 與 `open(simulate=True)`，含故障注入與請求記錄
- 使用說明補齊複製步驟、相依版本與使用限制

## [0.1.0] - 2026-11-04

- 專案骨架：uv、pytest、ruff，尚無功能
