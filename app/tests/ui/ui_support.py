"""UI 測試共用 helper：EngineStatus 產生器、假 worker、自動回答 QMessageBox。"""

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from plc_motion.worker import EngineStatus


def status(
    locked: bool = False,
    connected: bool = True,
    operating: bool = False,
    reasons: tuple[str, ...] = (),
) -> EngineStatus:
    if locked and not reasons:
        reasons = ("鎖定中",)
    return EngineStatus(
        locked=locked,
        lock_reasons=reasons if locked else (),
        connected=connected,
        operating=operating,
        residual=(),
    )


class FakeWorker(QObject):
    """與 PlcWorker 同樣的 signal；命令方法只記錄呼叫。"""

    snapshot_ready = Signal(object, object)
    engine_event = Signal(object)
    connection_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        if name in {
            "connect_plc",
            "move",
            "step",
            "home",
            "servo_all",
            "reset_all",
            "jog_press",
            "jog_release",
            "write_point",
            "clear_residual",
            "stop",
        }:
            return lambda *args: self.calls.append((name, *args))
        raise AttributeError(name)


def answer_next_message_box(button: QMessageBox.StandardButton, seen: list | None = None) -> None:
    """排程在下一個 modal QMessageBox 出現時按下 `button`；`seen` 收集其文字。"""

    def poll():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            if seen is not None:
                seen.append(box.text())
            box.button(button).click()
        else:
            QTimer.singleShot(20, poll)

    QTimer.singleShot(0, poll)
