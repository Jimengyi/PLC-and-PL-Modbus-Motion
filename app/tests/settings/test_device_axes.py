from pathlib import Path

import pytest

from plc_motion.settings import ConfigErrors, load_config


def _errors(config_dir: Path) -> list[str]:
    with pytest.raises(ConfigErrors) as exc:
        load_config(config_dir)
    return exc.value.errors


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---- 通道 device.toml 與 app.toml ----


def _device_toml(config_dir: Path) -> Path:
    return config_dir / "modbus" / "Channel01" / "device.toml"


def test_device_values_are_loaded(config_dir):
    device = load_config(config_dir).device

    assert (device.host, device.port, device.connect_timeout_ms) == ("192.168.1.105", 502, 2000)
    assert device.poll_interval_ms == 200
    assert device.handshake_timeout_s == 5
    assert device.reset_alarm_pulse_ms == 500
    assert device.log_retention_days == 30


def test_connection_values_come_from_channel_device_toml(config_dir):
    path = _device_toml(config_dir)
    _edit(path, 'host = "192.168.1.105"', 'host = "10.0.0.1"')
    _edit(path, "port = 502", "port = 1502")
    _edit(path, "timeout_ms = 2000", "timeout_ms = 1500")
    _edit(path, "interval_ms = 200", "interval_ms = 300")

    device = load_config(config_dir).device

    assert (device.host, device.port, device.connect_timeout_ms) == ("10.0.0.1", 1502, 1500)
    assert device.poll_interval_ms == 300


def test_app_toml_values_are_loaded(config_dir):
    (config_dir / "app.toml").write_text(
        "[handshake]\ntimeout_s = 2.5\nreset_alarm_pulse_ms = 300\n[log]\nretention_days = 7\n",
        encoding="utf-8",
    )

    device = load_config(config_dir).device

    assert device.handshake_timeout_s == 2.5
    assert device.reset_alarm_pulse_ms == 300
    assert device.log_retention_days == 7


def test_app_toml_optional_keys_fall_back_to_defaults(config_dir):
    (config_dir / "app.toml").write_text("# 全部用預設值\n", encoding="utf-8")

    device = load_config(config_dir).device

    assert device.handshake_timeout_s == 5
    assert device.reset_alarm_pulse_ms == 500
    assert device.log_retention_days == 30


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("timeout_s = 5", "timeout_s = 0", "timeout_s"),
        ("timeout_s = 5", 'timeout_s = "5"', "timeout_s"),
        ("reset_alarm_pulse_ms = 500", "reset_alarm_pulse_ms = 0.5", "reset_alarm_pulse_ms"),
        ("retention_days = 30", "retention_days = 0", "retention_days"),
    ],
)
def test_app_toml_invalid_value_is_reported(config_dir, old, new, key):
    _edit(config_dir / "app.toml", old, new)

    assert any("app.toml" in e and key in e for e in _errors(config_dir))


def test_app_toml_two_errors_are_both_reported(config_dir):
    path = config_dir / "app.toml"
    _edit(path, "timeout_s = 5", "timeout_s = -1")
    _edit(path, "retention_days = 30", "retention_days = 1.5")

    errors = _errors(config_dir)

    assert any("timeout_s" in e for e in errors)
    assert any("retention_days" in e for e in errors)


def test_legacy_handshake_in_channel_device_toml_is_reported(config_dir):
    """[handshake] 已移到 app.toml：留在通道 device.toml 會被函式庫報未知鍵。"""
    path = _device_toml(config_dir)
    path.write_text(path.read_text(encoding="utf-8") + "\n[handshake]\ntimeout_s = 5\n", "utf-8")

    assert any("device.toml" in e and "handshake" in e for e in _errors(config_dir))


def test_axes_values_are_loaded(config_dir):
    axis = load_config(config_dir).axes[7]

    assert axis.number == 7
    assert axis.name == "升降"
    assert (axis.soft_limit_min, axis.soft_limit_max) == (-100.0, 100.0)
    assert (axis.step_long, axis.step_medium, axis.step_short) == (10.0, 1.0, 0.1)
    assert axis.default_speed == 50


def test_soft_limit_min_not_less_than_max_is_reported(config_dir):
    _edit(config_dir / "axes.toml", "soft_limit_min = -100.0", "soft_limit_min = 100.0")

    errors = _errors(config_dir)

    assert any("axes.toml" in e and "第 1 軸" in e and "軟體極限" in e for e in errors)


@pytest.mark.parametrize("value", ["0.0", "-1.0"])
def test_non_positive_step_is_reported(config_dir, value):
    _edit(config_dir / "axes.toml", "step_medium = 1.0", f"step_medium = {value}")

    assert any("第 1 軸" in e and "step_medium" in e for e in _errors(config_dir))


@pytest.mark.parametrize("value", ["0", "101"])
def test_default_speed_out_of_range_is_reported(config_dir, value):
    _edit(config_dir / "axes.toml", "default_speed = 50", f"default_speed = {value}")

    assert any("第 1 軸" in e and "預設速度" in e for e in _errors(config_dir))


def test_missing_axis_is_reported(config_dir):
    path = config_dir / "axes.toml"
    text = path.read_text(encoding="utf-8")
    sections = text.split("\n[[axes]]")
    path.write_text("\n[[axes]]".join(sections[:4] + sections[5:]), encoding="utf-8")

    assert any("axes.toml" in e and "缺少第 5 軸" in e for e in _errors(config_dir))


def test_axis_number_out_of_range_is_reported(config_dir):
    _edit(config_dir / "axes.toml", "number = 7", "number = 8")

    errors = _errors(config_dir)

    assert any("軸號" in e and "8" in e for e in errors)
    assert any("缺少第 7 軸" in e for e in errors)


@pytest.mark.parametrize("value", ["[1]", "{ a = 1 }"])
def test_unhashable_axis_number_is_reported(config_dir, value):
    _edit(config_dir / "axes.toml", "number = 1", f"number = {value}")

    errors = _errors(config_dir)

    assert any("軸號" in e and "不合法" in e for e in errors)


def test_two_axis_errors_are_both_reported(config_dir):
    path = config_dir / "axes.toml"
    _edit(path, "soft_limit_min = -100.0", "soft_limit_min = 200.0")
    _edit(path, "default_speed = 50", "default_speed = 0")

    errors = _errors(config_dir)

    assert any("軟體極限" in e for e in errors)
    assert any("預設速度" in e for e in errors)
