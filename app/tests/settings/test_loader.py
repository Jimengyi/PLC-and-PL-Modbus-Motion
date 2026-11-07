"""load_config：設定資料夾結構與兩層啟動檢查（spec FR-6、FR-11）。"""

import sys
from pathlib import Path

import pytest

from lib.modbus_tags import DataModel
from lib.modbus_tags.config import ChannelConfig, ReadRange
from plc_motion.settings import ConfigErrors, load_config, resolve_config_dir

APP_DIR = Path(__file__).resolve().parents[2]


def _errors(config_dir: Path) -> list[str]:
    with pytest.raises(ConfigErrors) as exc:
        load_config(config_dir)
    return exc.value.errors


def _channel(config_dir: Path) -> Path:
    return config_dir / "modbus" / "Channel01"


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _row(rows, tag):
    return next(r for r in rows if r["Tag Name"] == tag)


def test_fixture_config_dir_loads_seven_axes_all_required_tags_unit_id_0(config_dir):
    config = load_config(config_dir)

    assert config.config_dir == config_dir
    assert sorted(config.axes) == [1, 2, 3, 4, 5, 6, 7]
    assert [config.axes[n].name for n in range(1, 8)] == ["X", "Y", "Z", "RX", "RY", "RZ", "升降"]
    points = config.points.all()
    assert len(points) == 7 * 18 + 1
    assert {p.unit_id for p in points} == {0}
    assert config.points.get("Emergency Stop").start_address == 24576
    assert config.points.axis_tag("Target Position", 7).start_address == 181
    assert config.device.host == "192.168.1.105"


def test_channel_is_library_config_of_channel01(config_dir):
    config = load_config(config_dir)

    assert isinstance(config.channel, ChannelConfig)
    assert (config.channel.name, config.channel.channel) == ("PLC", "Channel01")
    assert config.channel.device.transport == "tcp"
    assert len(config.channel.tags) == 7 * 18 + 1
    assert config.read_ranges is None  # 沒有 read_ranges.toml


def test_read_ranges_come_from_channel_folder(config_dir):
    (_channel(config_dir) / "read_ranges.toml").write_text(
        '[[unit]]\nunit_id = 0\ncoil = "20~84, 120~184"\n'
        'holding_register = "20~83, 120~183"\ndiscrete_input = "24576~24612"\n',
        encoding="utf-8",
    )

    ranges = load_config(config_dir).read_ranges

    assert ranges is not None and all(isinstance(r, ReadRange) for r in ranges)
    assert ReadRange(0, DataModel.COIL, 20, 65) in ranges
    assert len(ranges) == 5


def test_missing_positions_csv_is_not_an_error(config_dir):
    assert not (config_dir / "positions.csv").exists()
    load_config(config_dir)
    assert (config_dir / "positions.csv").is_file()


# ---- 第一層 ----


@pytest.mark.parametrize("name", ["app.toml", "axes.toml"])
def test_missing_app_level_file_is_reported(config_dir, name):
    (config_dir / name).unlink()

    assert any("缺少設定檔" in e and name in e for e in _errors(config_dir))


@pytest.mark.parametrize("name", ["app.toml", "axes.toml"])
def test_unparsable_toml_is_reported(config_dir, name):
    (config_dir / name).write_text("[handshake\ntimeout_s = ", encoding="utf-8")

    assert any(name in e and "格式無法解析" in e for e in _errors(config_dir))


def test_library_config_error_is_reported(config_dir):
    _edit(_channel(config_dir) / "device.toml", "timeout_ms = 2000", "timeout_ms = 0")

    errors = _errors(config_dir)

    assert any("device.toml" in e and "protocol.timeout_ms" in e for e in errors), errors


@pytest.mark.parametrize("name", ["device.toml", "io_points.csv"])
def test_missing_channel_file_is_reported(config_dir, name):
    (_channel(config_dir) / name).unlink()

    assert any(name in e for e in _errors(config_dir))


def test_missing_modbus_folder_is_reported(config_dir):
    import shutil

    shutil.rmtree(config_dir / "modbus")

    assert any("modbus" in e for e in _errors(config_dir))


def test_missing_channel01_is_reported(config_dir):
    _channel(config_dir).rename(config_dir / "modbus" / "PLC1")

    errors = _errors(config_dir)

    assert any("Channel01" in e and "缺少" in e for e in errors), errors
    assert any("PLC1" in e and "Channel01 以外" in e for e in errors), errors


def test_extra_channel_folder_is_reported(config_dir):
    import shutil

    shutil.copytree(_channel(config_dir), config_dir / "modbus" / "Channel02")

    errors = _errors(config_dir)

    assert any("Channel02" in e and "Channel01 以外" in e for e in errors), errors


def test_disabled_channel_is_reported(config_dir):
    _edit(_channel(config_dir) / "device.toml", "enabled = true", "enabled = false")

    errors = _errors(config_dir)

    assert any("Channel01" in e and "停用" in e for e in errors), errors


def _make_rtu(config_dir: Path) -> None:
    path = _channel(config_dir) / "device.toml"
    text = path.read_text(encoding="utf-8")
    start = text.index("[connection]")
    end = text.index("[protocol]")
    rtu = '[connection]\ntype = "rtu"\n\n[connection.rtu]\nport = "COM3"\nbaudrate = 9600\n'
    rtu += 'parity = "N"\nstopbits = 1\n\n'
    path.write_text(text[:start] + rtu + text[end:], encoding="utf-8")


def test_rtu_channel_is_reported(config_dir):
    _make_rtu(config_dir)

    errors = _errors(config_dir)

    assert any("Channel01" in e and "TCP" in e for e in errors), errors


def test_rtu_type_and_library_error_are_both_listed(config_dir, io_rows, write_io_points):
    """`connection.type = "rtu"` 加上某列 Data Model 打錯，兩者都列出。"""
    _make_rtu(config_dir)
    _row(io_rows, "Busy 2")["Data Model"] = "Coils"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("Channel01" in e and "TCP" in e for e in errors), errors
    assert any("Coils" in e for e in errors), errors


def test_two_first_layer_errors_are_both_listed(config_dir, io_rows, write_io_points):
    (config_dir / "axes.toml").unlink()
    _row(io_rows, "Busy 2")["Data Model"] = "Coils"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("axes.toml" in e for e in errors)
    assert any("Coils" in e for e in errors), errors


def test_second_layer_waits_until_first_layer_is_clean(config_dir, io_rows, write_io_points):
    axes = config_dir / "axes.toml"
    good = axes.read_bytes()
    axes.write_text("[[axes]\n", encoding="utf-8")
    io_rows.remove(_row(io_rows, "Speed 1"))
    write_io_points(config_dir, io_rows)

    first = _errors(config_dir)
    assert any("axes.toml" in e for e in first)
    assert not any("Speed 1" in e for e in first)

    axes.write_bytes(good)
    second = _errors(config_dir)
    assert any("Speed 1" in e for e in second)
    assert not any("axes.toml" in e for e in second)


def test_resolve_config_dir_from_source_is_app_config(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert resolve_config_dir() == APP_DIR / "config"


def test_resolve_config_dir_when_frozen_is_next_to_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "plc-motion.exe"))

    assert resolve_config_dir() == tmp_path / "config"
