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

手動試用（沒有實機 PLC）：

```
uv run plc-sim                                     模擬 PLC（預設 127.0.0.1:5020）
uv run plc-motion --host 127.0.0.1 --port 5020     連到模擬 PLC
```

## 設定資料夾

```
config/
├─ app.toml                     交握逾時、Reset Alarm 脈衝寬度、日誌保留天數
├─ axes.toml                    軸參數
├─ positions.csv                點位表，不存在時自動建立
└─ modbus/Channel01/
   ├─ device.toml               連線與通訊參數（modbus_tags 格式）
   ├─ io_points.csv             IO 點位表
   └─ read_ranges.toml          批次讀取範圍
```

- `io_points.csv` 以 `Data Type`（Bool、U16、I32 等）指定型別，倍率與單位寫在 `scale=…;unit=…`，B 接點寫 `invert=true`。
- 預設設定新增 io 點時，要同步在 `read_ranges.toml` 加範圍。
- 通訊由 `src/lib/modbus_tags/`（函式庫副本）負責，不要手改。

## 打包

```
uv run python packaging/build.py     產物在 dist\plc-motion
uv run pytest -m slow                打包測試
```
