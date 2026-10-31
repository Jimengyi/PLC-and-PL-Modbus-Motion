"""骨架驗證：依賴可匯入、Qt 能建立視窗、系統匣可用。功能測試在設計完成後補上。"""

import pymodbus
from PySide6.QtWidgets import QSystemTrayIcon, QWidget


def test_pymodbus_importable():
    assert pymodbus.__version__.startswith("3.")


def test_qt_widget_shows(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    widget.show()
    assert widget.isVisible()


def test_system_tray_available(qtbot):
    assert QSystemTrayIcon.isSystemTrayAvailable()
