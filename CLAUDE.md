# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案性質

- repo 根目錄是 Obsidian vault，`docs/` 放 PLC 與 PC 之間以 Modbus 控制七軸運動（六軸平台 X/Y/Z/RX/RY/RZ + 第七軸升降軸）的通訊規格。
- `app/` 是 PC 端 Modbus Master 程式：Python 3.14、uv、PySide6、pymodbus，打包用 PyInstaller onedir。技術棧決策見 `docs/bootstrap/bootstrap-plc-motion-master.md`。

## app/ 指令

- 一律在 `app\` 下執行：`uv sync`、`uv run plc-motion`、`uv run pytest`、`uv run ruff check .`、`uv run ruff format .`。
- 沒有實機 PLC：測試與手動驗證都對內建的模擬 PLC 跑。手動試用：`uv run plc-sim`（預設 127.0.0.1:5020）＋ `uv run plc-motion --host 127.0.0.1 --port 5020`；`--config DIR` 可指定設定資料夾。
- 商用授權約束：不改用 PyQt（GPL）或 Nuitka（AGPL）；打包維持 onedir，讓 Qt DLL 保持可替換，並附授權聲明（含 LGPL／GPL 全文）。
- 打包：`uv run python packaging/build.py`，產物在 `app/dist/plc-motion/`。打包測試標 `slow`，預設排除，用 `uv run pytest -m slow` 跑。
- 全套測試含 e2e 約 4 分鐘，超過 Bash 工具預設 120 秒：背景執行或加長 timeout。
- `plc-motion` 有全機共用的單一實例鎖：本機有 plc-motion 在跑時，進入點測試會 skip、打包 exe 測試會失敗。測完一律結束 plc-motion／plc-sim 行程；plc-sim 執行中 `uv run` 可能因檔案被鎖報 "Failed to spawn"。
- 測試要載入預設設定時，先把 `app/config` 複製到 tmp 再載入：`load_config` 會自動建立 `positions.csv`，切主題會寫回 `device.toml`。
- `uv sync` 遇 TLS `invalid peer certificate: UnknownIssuer` 時加 `--system-certs`。換 PySide6 套件後出現 QtCore「DLL load failed」：`uv sync --reinstall-package pyside6-essentials --reinstall-package shiboken6`。
- 要讓使用者看到 plc-motion／plc-sim 視窗，用 PowerShell `Start-Process` 啟動 `.venv\Scripts\*.exe`；從 Bash 背景執行時行程會跑但視窗不顯示。
- 套用樣式表時 SpinBox 不要設 `NoButtons`：內部文字框寬度會變 1 px、數字看不到。改用 `ui/widgets.py` 的 `plain_spinbox`（QSS 把上下鍵寬度設 0）。
- Windows 11 style 不採用 QSS 的 `selection-background-color`，`::item:hover` 也只亮單格：表格的選取與停留底色由 `ui/row_hover.py` 的 delegate 繪製，新表格用 `enable_row_hover` 接上。
- 寫入前的確認視窗一律經 `confirm_dialog.confirm(...)` 呼叫；測試由 `tests/conftest.py` 的 autouse fixture `confirms` 自動回答並記錄內容。
- PySide6 的 signal 接到 lambda 或非 QObject 方法時是 direct connection，會在發送端執行緒執行；要在 UI 執行緒處理必須接到 QObject 的 slot。
- 規格、設計與實作計畫在 `docs/specs/`、`docs/design/`、`docs/plans/`（slug `modbus-master-app`）；系統結構見 `docs/design/design-system-blueprint.md`。

## 文件分工與優先順序

- `docs/PLC Modbus軸控交握.xlsx`：位址與 Position 單位的唯一依據。
- `docs/PLC_PC_Modbus_通訊規格.md`：單軸的交握流程、狀態判斷、時序圖與 Action Code / Alarm_Code 定義。表內位址是單軸範例，與 xlsx 衝突時以 xlsx 為準。
- `docs/七軸_Modbus位址表.md`：七軸完整位址與各軸倍率。第 1、7 軸取自 xlsx，第 2 ~ 6 軸依規律計算即可（PLC→PC 每軸 +10、PC→PLC 每軸 +10、極限與原點每軸 +3）。
- `README.md`：各軸單位換算、I32（2 × U16，低位在前）資料格式、LabVIEW 讀寫接法與常見錯誤。

## 修改時的注意事項

- 通訊規格書只描述單一軸，不要寫入七軸內容；七軸資料放在位址表。
- 改位址或單位時，三份 md 要一起檢查是否一致（位址表、規格書的位址表格與範例數值、README 的倍率）。
- Position 倍率：X/Y/Z（第 1 ~ 3 軸）1 mm = 10000，RX/RY/RZ（第 4 ~ 6 軸）1 deg = 100，升降軸（第 7 軸）1 mm = 100。軸號對應已確認：第 1 ~ 6 軸為 X ~ RZ，第 7 軸為升降軸。
- 本專案 PLC 只支援 Action Code 1（原點復歸）、2（絕對移動）；相對移動由 PC 端換算成 Code 2。
- xlsx 的中文以 Big5 類編碼儲存，直接用終端機輸出會亂碼；讀取需用標準庫（zipfile + xml）解析，環境沒有 openpyxl。
- 文件中的圖表使用 mermaid（flowchart、sequenceDiagram），Obsidian 已裝 mermaid-popup 外掛。
- `.obsidian/workspace.json` 會因 Obsidian 開啟而自動變動，提交時不要一併加入。
