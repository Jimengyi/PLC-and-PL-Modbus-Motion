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


# ---- device.toml ----


def test_device_values_are_loaded(config_dir):
    device = load_config(config_dir).device

    assert (device.host, device.port, device.connect_timeout_ms) == ("192.168.1.105", 502, 2000)
    assert device.poll_interval_ms == 200
    assert device.handshake_timeout_s == 5
    assert device.reset_alarm_pulse_ms == 500
    assert device.log_retention_days == 30


def test_device_optional_keys_fall_back_to_fr2_defaults(config_dir):
    (config_dir / "device.toml").write_text(
        '[connection]\nhost = "10.0.0.1"\nport = 1502\ntimeout_ms = 1000\n', encoding="utf-8"
    )

    device = load_config(config_dir).device

    assert device.poll_interval_ms == 200
    assert device.handshake_timeout_s == 5
    assert device.reset_alarm_pulse_ms == 500
    assert device.log_retention_days == 30


def test_device_missing_host_is_reported(config_dir):
    _edit(config_dir / "device.toml", 'host = "192.168.1.105"\n', "")

    assert any("device.toml" in e and "host" in e for e in _errors(config_dir))


def test_device_invalid_port_is_reported(config_dir):
    _edit(config_dir / "device.toml", "port = 502", "port = 70000")

    assert any("device.toml" in e and "port" in e for e in _errors(config_dir))


def test_device_legacy_ui_theme_is_ignored(config_dir):
    """舊版 device.toml 的 [ui] theme（含已不支援的值）不影響載入：介面只有深色。"""
    _edit(config_dir / "device.toml", 'theme = "dark"', 'theme = "light"')
    device = load_config(config_dir).device
    assert not hasattr(device, "theme")


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
