---
type: bootstrap
created: 2026-10-31
tags: []
slug: plc-motion-master
status: approved
approved_by: user
related: []
---

# Bootstrap：plc-motion-master

## Status

- 2026-10-31 使用者批准技術棧。

## 專案意圖

- PC 端 Modbus TCP Master 程式，依 `docs/` 規格控制 PLC 七軸運動：IO Monitor、軸控操作、可縮到系統匣。
- 裝置設定（IP、Port 等）與 Modbus 點位表放在外部設定檔，不寫死在程式。

## 限制

- Windows 11，單人開發，使用者對 Python／Qt 不熟（設計討論需多解釋）。
- 商業部署不能產生授權費用。
- 目前沒有實機 PLC，需要模擬 PLC 才能開發與測試。
- 最終要打包成 exe，現場電腦不裝 Python。

## 技術棧決策

| 決策 | 選擇 | 理由 | 翻案成本 |
|---|---|---|---|
| UI 框架 | PySide6 | 使用者指定；LGPL 商用免費。PyQt6 是 GPL，商用須買授權或開源 | 高：UI 層全部重寫 |
| 打包 | PyInstaller onedir | GPL 附例外條款，允許打包商業閉源程式；onedir 讓 Qt DLL 保持獨立可替換，符合 LGPL。否決 Nuitka：4.x 改為 AGPLv3 | 低：只影響打包腳本 |
| 程式位置 | `app/` 子資料夾 | 與 Obsidian vault 的文件分開，避免 `.venv` 被 Obsidian 索引 | 低 |

## Scaffolding 清單

- [x] `app/` 下 `uv init --package`，Python 3.14
- [x] 依賴：pyside6、pymodbus；dev：pytest、pytest-qt、ruff、pyinstaller
- [x] pytest 與 ruff 設定寫進 `pyproject.toml`
- [x] smoke test（匯入、Qt 視窗、系統匣可用）實際跑過
- [x] `.gitignore` 加 Python 項目
- [x] CLAUDE.md 更新專案性質與指令
- [ ] PyInstaller 打包腳本與授權聲明檔（功能完成後，轉入 plan）

## 未解問題

- `pyside6` 套件會連帶裝入 WebEngine、Addons 等大型模組，打包體積大。若設計確定只用 QtWidgets，可改成只依賴 `pyside6-essentials`；若要用 QtCharts 畫趨勢圖則需保留 Addons。設計階段決定。
- uv 曾出現一次 TLS `invalid peer certificate: UnknownIssuer`，重試後成功；若再發生，可加 `--system-certs`。
