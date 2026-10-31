"""殘留交握視窗與連線相關提示（FR-13、FR-57、FR-58）：送入引擎事件，斷言畫面與假 worker 呼叫。"""

import pytest
from PySide6.QtWidgets import QApplication

from plc_motion.engine import (
    DisconnectedWhileMoving,
    JogResidualCleared,
    ResidualDetected,
    ResidualInfo,
)
from plc_motion.ui.main_window import MainWindow

WARNING = "寫回 0 後，PLC 可能開始執行斷線前的命令；若不要執行該命令，請先按急停"


@pytest.fixture
def window(qtbot, config, fake_worker):
    w = MainWindow(config, fake_worker, "127.0.0.1:5020")
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.residual_dialog.close()
    w.event_dialog.close()
    w.allow_close()
    w.close()


def detected() -> ResidualDetected:
    return ResidualDetected(
        (
            ResidualInfo(axis=1, request=True, pc_action_code=2, target_position=12.5),
            ResidualInfo(axis=4, request=False, pc_action_code=1, target_position=30.0),
        )
    )


def test_residual_detected_lists_axes_and_warning(window, fake_worker):
    fake_worker.engine_event.emit(detected())
    dialog = window.residual_dialog
    assert dialog.isVisible()
    text = dialog.text()
    assert WARNING in text
    assert "第 1 軸" in text and "第 4 軸" in text
    assert "12.5000 mm" in text  # 第 1 軸 1 mm = 10000，四位小數
    assert "30.00 deg" in text  # 第 4 軸 1 deg = 100，兩位小數
    assert dialog.clear_button.text() == "寫回 0"
    assert dialog.keep_button.text() == "先不處理，保持鎖定"
    assert fake_worker.calls == []


def test_keep_locked_closes_without_write_and_toolbar_reopens(qtbot, window, fake_worker):
    assert not window.residual_button.isVisible()
    fake_worker.engine_event.emit(detected())
    window.residual_dialog.keep_button.click()
    assert not window.residual_dialog.isVisible()
    assert fake_worker.calls == []
    qtbot.waitUntil(lambda: window.residual_button.isVisible(), timeout=2000)

    window.residual_button.click()
    assert window.residual_dialog.isVisible()
    window.residual_dialog.clear_button.click()
    assert fake_worker.calls == [("clear_residual",)]
    assert not window.residual_dialog.isVisible()
    qtbot.waitUntil(lambda: not window.residual_button.isVisible(), timeout=2000)


def test_snapshot_without_residual_hides_entry(qtbot, window, fake_worker):
    from ui_support import status

    fake_worker.engine_event.emit(detected())
    window.residual_dialog.keep_button.click()
    qtbot.waitUntil(lambda: window.residual_button.isVisible(), timeout=2000)
    fake_worker.snapshot_ready.emit(None, status())
    qtbot.waitUntil(lambda: not window.residual_button.isVisible(), timeout=2000)


def test_jog_residual_cleared_message(window, fake_worker):
    fake_worker.engine_event.emit(JogResidualCleared(axis=3, tags=("JOG Plus 3",)))
    assert "第 3 軸 JOG 於斷線期間未放開，已寫回 0" in window.messages[-1].summary


def test_disconnected_while_moving_message(window, fake_worker):
    fake_worker.engine_event.emit(DisconnectedWhileMoving(axes=(2, 5)))
    assert "軸可能仍在移動，請按急停" in window.messages[-1].summary
    assert window.event_dialog.isVisible()
    assert "軸可能仍在移動，請按急停" in window.event_dialog.text()
    assert "第 2、5 軸" in window.event_dialog.text()
    QApplication.processEvents()
