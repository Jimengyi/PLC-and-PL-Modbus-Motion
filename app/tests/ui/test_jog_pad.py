"""JogPad 步進鍵盤與 JogButton 按住式按鈕（spec FR-27、FR-32、FR-33 步進面、FR-16 反灰）。"""

from dataclasses import replace

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QApplication, QWidget
from ui_support import status

from plc_motion.ui.jog_pad import JogButton, JogPad

ALL_HOMED = {f"Find Home Finish {n}": True for n in range(1, 8)}


@pytest.fixture
def pad(qtbot, config, fake_worker):
    # 第 4 軸（RX）步距與其他軸不同，用來抓「拿錯軸的步距」
    config.axes[4] = replace(config.axes[4], step_long=5.0, step_medium=0.5, step_short=0.05)
    p = JogPad(config, fake_worker)
    qtbot.addWidget(p)
    p.show()
    qtbot.waitExposed(p)
    return p


def steps(fake_worker):
    return [c for c in fake_worker.calls if c[0] == "step"]


# ---- 步進 ----


def test_step_key_sends_distance_times_direction(pad, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())
    pad.speed.setValue(30)
    pad.distance_buttons["medium"].click()
    pad.step_buttons[(1, +1)].click()
    pad.distance_buttons["long"].click()
    pad.step_buttons[(3, -1)].click()
    pad.step_buttons[(4, -1)].click()
    pad.distance_buttons["short"].click()
    pad.step_buttons[(7, +1)].click()
    assert steps(fake_worker) == [
        ("step", 1, 1.0, 30),
        ("step", 3, -10.0, 30),
        ("step", 4, -5.0, 30),
        ("step", 7, 0.1, 30),
    ]


def test_every_key_maps_to_its_axis_and_direction(pad, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())
    assert set(pad.step_buttons) == {(n, d) for n in range(1, 8) for d in (+1, -1)}
    pad.distance_buttons["long"].click()
    for (n, d), button in pad.step_buttons.items():
        fake_worker.calls.clear()
        button.click()
        expected = (5.0 if n == 4 else 10.0) * d
        assert steps(fake_worker) == [("step", n, expected, pad.speed.value())]


def test_each_click_sends_one_step(pad, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())
    for _ in range(3):
        pad.step_buttons[(2, +1)].click()
    assert len(steps(fake_worker)) == 3


def test_distance_label_shows_current_step(pad):
    pad.distance_buttons["long"].click()
    assert pad.distance_labels[1].text() == "10.0000 mm"
    assert pad.distance_labels[4].text() == "5.00 deg"
    assert pad.distance_labels[7].text() == "10.00 mm"
    pad.distance_buttons["short"].click()
    assert pad.distance_labels[1].text() == "0.1000 mm"
    assert pad.distance_labels[4].text() == "0.05 deg"


def test_step_keys_disabled_until_axis_homed(pad, fake_worker, make_snapshot):
    fake_worker.snapshot_ready.emit(make_snapshot({"Find Home Finish 3": True}), status())
    assert pad.step_buttons[(3, +1)].isEnabled()
    assert pad.step_buttons[(3, -1)].isEnabled()
    assert not pad.step_buttons[(1, +1)].isEnabled()
    assert not pad.step_buttons[(1, -1)].isEnabled()
    assert "原點" in pad.step_buttons[(1, +1)].toolTip()


def test_step_keys_disabled_while_locked_or_without_status(pad, fake_worker, make_snapshot):
    assert not any(b.isEnabled() for b in pad.step_buttons.values())  # 尚未收到狀態
    fake_worker.snapshot_ready.emit(make_snapshot(ALL_HOMED), status())
    assert all(b.isEnabled() for b in pad.step_buttons.values())
    fake_worker.snapshot_ready.emit(
        make_snapshot(ALL_HOMED), status(locked=True, reasons=("第 3 軸動作中",))
    )
    assert not any(b.isEnabled() for b in pad.step_buttons.values())
    assert "第 3 軸動作中" in pad.step_buttons[(1, +1)].toolTip()
    fake_worker.snapshot_ready.emit(None, status(locked=True, connected=False))
    assert not any(b.isEnabled() for b in pad.step_buttons.values())


def test_default_speed_comes_from_axes_settings(qtbot, config, fake_worker):
    for n in config.axes:
        config.axes[n] = replace(config.axes[n], default_speed=40 if n != 5 else 25)
    p = JogPad(config, fake_worker)
    qtbot.addWidget(p)
    assert p.speed.minimum() == 1 and p.speed.maximum() == 100
    assert p.speed.value() == 25  # 取各軸預設速度中最低者


# ---- JogButton ----


@pytest.fixture
def jog(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(300, 200)
    button = JogButton("JOG ＋", host)
    button.setGeometry(50, 50, 100, 40)
    host.show()
    qtbot.waitExposed(host)
    events: list[str] = []
    button.jog_pressed.connect(lambda: events.append("press"))
    button.jog_released.connect(lambda: events.append("release"))
    return host, button, events


def test_jog_button_press_and_release(qtbot, jog):
    _, button, events = jog
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    assert events == ["press"]
    assert button.is_held
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert events == ["press", "release"]
    assert not button.is_held


def test_jog_button_release_outside_still_releases(qtbot, jog):
    _, button, events = jog
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    qtbot.mouseMove(button, QPoint(-40, -40))
    assert events == ["press"]  # 移出鍵外不算放開，也不重新按下
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-40, -40))
    assert events == ["press", "release"]


def test_jog_button_releases_when_window_hidden(qtbot, jog):
    host, button, events = jog
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    host.hide()
    assert events == ["press", "release"]


def test_jog_button_releases_when_window_deactivated(qtbot, jog):
    host, button, events = jog
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    QApplication.sendEvent(host, QEvent(QEvent.Type.WindowDeactivate))
    assert events == ["press", "release"]


def test_jog_button_releases_when_window_minimized(qtbot, jog):
    host, button, events = jog
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    host.showMinimized()
    qtbot.waitUntil(lambda: events == ["press", "release"])


def test_jog_button_releases_when_disabled_while_held(qtbot, jog):
    _, button, events = jog
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    button.setEnabled(False)
    assert events == ["press", "release"]


def test_jog_button_hide_without_hold_sends_nothing(qtbot, jog):
    host, button, events = jog
    QApplication.sendEvent(host, QEvent(QEvent.Type.WindowDeactivate))
    host.hide()
    button.setEnabled(False)
    assert events == []


def test_disabled_jog_button_ignores_press(qtbot, jog):
    _, button, events = jog
    button.setEnabled(False)
    qtbot.mousePress(button, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert events == []
