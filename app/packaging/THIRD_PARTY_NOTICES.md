# 第三方授權聲明

plc-motion 打包版（`plc-motion.exe` 與 `_internal/`）內含下列第三方元件。授權全文在 `licenses/`，或見各項所列網址。

## Qt 與 PySide6（LGPL v3）

- 元件：PySide6、shiboken6（Qt for Python）與其附帶的 Qt 6 函式庫（Qt6Core.dll、Qt6Gui.dll、Qt6Widgets.dll、Qt6Network.dll 等）。
- 授權：GNU LGPL v3（亦提供 GPL v3 與商業授權；本程式採用 LGPL v3）。全文：https://www.gnu.org/licenses/lgpl-3.0.html
- 版權：The Qt Company Ltd. 及各貢獻者。
- 原始碼：https://code.qt.io/ 、https://pypi.org/project/PySide6/
- 本程式以動態連結方式使用 Qt。打包採用 onedir，Qt DLL 與 PySide6 的 `.pyd` 為 `_internal/PySide6/` 下的獨立檔案，使用者可自行以修改過的相容版本取代。本程式未修改 Qt 或 PySide6 原始碼。

## pymodbus（BSD 3-Clause）

- 元件：pymodbus 3.x，Modbus TCP 通訊。
- 授權全文：`licenses/pymodbus-LICENSE.txt`
- 版權：Copyright 2008-2025 Pymodbus
- 來源：https://github.com/pymodbus-dev/pymodbus

## Python（PSF License）

- 元件：CPython 直譯器與標準函式庫（`python314.dll` 及隨附模組）。
- 授權全文：`licenses/Python-LICENSE.txt`
- 來源：https://www.python.org/

## PyInstaller 開機載入器（GPL v2 加例外條款）

- 元件：PyInstaller bootloader（`plc-motion.exe` 的啟動部分）。
- 授權：GPL v2 並附例外條款，允許以任何授權（含商業閉源）散布打包產物。
- 來源：https://github.com/pyinstaller/pyinstaller

## 其他

- Microsoft Visual C++ 執行階段 DLL（`VCRUNTIME140*.dll`、`msvcp140*.dll` 等）依 Microsoft 的再散布條款隨附。
