import sys
from pathlib import Path

import pytest

from plc_motion.settings import ConfigErrors, load_config, resolve_config_dir

APP_DIR = Path(__file__).resolve().parents[2]


def _errors(config_dir: Path) -> list[str]:
    with pytest.raises(ConfigErrors) as exc:
        load_config(config_dir)
    return exc.value.errors


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


@pytest.mark.parametrize("name", ["device.toml", "axes.toml", "io_points.csv"])
def test_missing_config_file_is_reported(config_dir, name):
    (config_dir / name).unlink()

    errors = _errors(config_dir)

    assert any("缺少設定檔" in e and name in e for e in errors)


def test_missing_positions_csv_is_not_an_error(config_dir):
    assert not (config_dir / "positions.csv").exists()
    load_config(config_dir)


@pytest.mark.parametrize("name", ["device.toml", "axes.toml"])
def test_unparsable_toml_is_reported(config_dir, name):
    (config_dir / name).write_text("[connection\nhost = ", encoding="utf-8")

    errors = _errors(config_dir)

    assert any(name in e and "格式無法解析" in e for e in errors)


def test_errors_from_different_files_are_all_collected(config_dir, io_rows, write_io_points):
    (config_dir / "device.toml").unlink()
    io_rows[0]["Data Model"] = "Register"
    write_io_points(config_dir, io_rows)

    errors = _errors(config_dir)

    assert any("device.toml" in e for e in errors)
    assert any("Ready 1" in e and "Data Model" in e for e in errors)


def test_resolve_config_dir_from_source_is_app_config(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert resolve_config_dir() == APP_DIR / "config"


def test_resolve_config_dir_when_frozen_is_next_to_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "plc-motion.exe"))

    assert resolve_config_dir() == tmp_path / "config"
