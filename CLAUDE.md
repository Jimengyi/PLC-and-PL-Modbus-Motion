# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案性質

- repo 根目錄是 Obsidian vault，`docs/` 放 PLC 與 PC 之間以 Modbus 控制七軸運動（六軸平台 X/Y/Z/RX/RY/RZ + 第七軸升降軸）的通訊規格。
- `app/` 是 PC 端 Modbus Master 程式：Python 3.14、uv、PySide6、pymodbus，打包用 PyInstaller onedir。技術棧決策見 `docs/bootstrap/bootstrap-plc-motion-master.md`。

## app/ 指令

- 一律在 `app\` 下執行：`uv sync`、`uv run plc-motion`、`uv run pytest`、`uv run ruff check .`、`uv run ruff format .`。
- 沒有實機 PLC：測試與手動驗證都對內建的模擬 PLC（pymodbus server）跑。
- 商用授權約束：不改用 PyQt（GPL）或 Nuitka（AGPL）；打包維持 onedir，讓 Qt DLL 保持可替換，並附授權聲明。

## 文件分工與優先順序

- `docs/PLC Modbus軸控交握.xlsx`：位址與 Position 單位的唯一依據。
- `docs/PLC_PC_Modbus_通訊規格.md`：單軸的交握流程、狀態判斷、時序圖與 Action Code / Alarm_Code 定義。表內位址是單軸範例，與 xlsx 衝突時以 xlsx 為準。
- `docs/七軸_Modbus位址表.md`：七軸完整位址與各軸倍率。第 1、7 軸取自 xlsx，第 2 ~ 6 軸依規律計算即可（PLC→PC 每軸 +10、PC→PLC 每軸 +10、極限與原點每軸 +3）。
- `README.md`：各軸單位換算、I32（2 × U16，低位在前）資料格式、LabVIEW 讀寫接法與常見錯誤。

## 修改時的注意事項

- 通訊規格書只描述單一軸，不要寫入七軸內容；七軸資料放在位址表。
- 改位址或單位時，三份 md 要一起檢查是否一致（位址表、規格書的位址表格與範例數值、README 的倍率）。
- Position 倍率：X/Y/Z（第 1 ~ 3 軸）1 mm = 10000，RX/RY/RZ（第 4 ~ 6 軸）1 deg = 100，升降軸（第 7 軸）1 mm = 100。軸號與 X ~ RZ 的對應順序待確認。
- xlsx 的中文以 Big5 類編碼儲存，直接用終端機輸出會亂碼；讀取需用標準庫（zipfile + xml）解析，環境沒有 openpyxl。
- 文件中的圖表使用 mermaid（flowchart、sequenceDiagram），Obsidian 已裝 mermaid-popup 外掛。
- `.obsidian/workspace.json` 會因 Obsidian 開啟而自動變動，提交時不要一併加入。
