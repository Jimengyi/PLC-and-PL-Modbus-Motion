# plc-motion

PLC 七軸運動的 Modbus TCP Master 程式（PySide6 + pymodbus）。通訊規格見上層 `docs/`。

## 常用指令

在 `app\` 目錄下執行：

```
uv sync                 安裝依賴
uv run plc-motion       啟動程式
uv run pytest           跑測試
uv run ruff check .     程式碼檢查
uv run ruff format .    格式化
```
