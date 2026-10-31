import csv
import io
from pathlib import Path

import pytest

from plc_motion.settings import ConfigErrors, load_config
from plc_motion.settings.csvio import BOM
from plc_motion.settings.positions import PositionTable

HEADER = ["#Point", "Name", "X", "Y", "Z", "RX", "RY", "RZ", "Lift", "Description"]


def make_csv(rows: list[list[str]], header: list[str] | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header or HEADER)
    w.writerows(rows)
    return buf.getvalue()


def valid_rows(n: int = 200) -> list[list[str]]:
    return [
        [f"P{i:03d}", f"點{i}", "1.5", "0", "0", "0", "0", "0", "0", ""] for i in range(1, n + 1)
    ]


def test_missing_file_creates_200_zero_points(config_dir: Path):
    cfg = load_config(config_dir)
    rows = cfg.positions.rows()
    assert len(rows) == 200
    assert rows[0].point_id == "P001" and rows[-1].point_id == "P200"
    assert all(r.x == r.y == r.z == r.rx == r.ry == r.rz == r.lift == 0 for r in rows)
    data = (config_dir / "positions.csv").read_bytes()
    assert data.startswith(BOM)
    assert len(data.decode("utf-8-sig").strip().splitlines()) == 201


def test_update_saves_immediately_and_reloads(config_dir: Path):
    cfg = load_config(config_dir)
    cfg.positions.update("P003", name="取放點", x=12.5, description="說明")
    again = load_config(config_dir).positions.rows()[2]
    assert (again.name, again.x, again.description) == ("取放點", 12.5, "說明")


def test_update_unknown_point_or_field_raises(config_dir: Path):
    t = load_config(config_dir).positions
    with pytest.raises(KeyError):
        t.update("P201", name="x")
    with pytest.raises(ValueError):
        t.update("P001", bogus=1)


def test_update_failure_keeps_file_and_table(config_dir: Path, monkeypatch):
    t = load_config(config_dir).positions
    before = (config_dir / "positions.csv").read_bytes()

    def boom(path, data):
        raise OSError("disk")

    monkeypatch.setattr("plc_motion.settings.positions.atomic_write_bytes", boom)
    with pytest.raises(OSError):
        t.update("P001", x=9.0)
    assert (config_dir / "positions.csv").read_bytes() == before
    assert t.rows()[0].x == 0


def test_import_valid_replaces_and_saves(config_dir: Path, tmp_path: Path):
    t = load_config(config_dir).positions
    src = tmp_path / "in.csv"
    src.write_bytes(make_csv(valid_rows()).encode("big5", errors="replace"))
    assert t.import_file(src) == []
    assert t.rows()[4].x == 1.5
    assert t.rows()[0].name == "點1"
    assert load_config(config_dir).positions.rows()[0].name == "點1"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[:150],
        lambda rows: rows.__setitem__(4, rows[4][:2] + ["abc"] + rows[4][3:]) or rows,
        lambda rows: rows.__setitem__(4, rows[4][:5]) or rows,
        lambda rows: rows.__setitem__(4, ["P999"] + rows[4][1:]) or rows,
    ],
    ids=["150列", "座標為文字", "缺欄", "點號不合法"],
)
def test_import_invalid_leaves_table_and_file(config_dir: Path, tmp_path: Path, mutate):
    t = load_config(config_dir).positions
    before = (config_dir / "positions.csv").read_bytes()
    src = tmp_path / "bad.csv"
    src.write_bytes(make_csv(mutate(valid_rows())).encode("utf-8"))
    errors = t.import_file(src)
    assert errors
    assert (config_dir / "positions.csv").read_bytes() == before
    assert t.rows()[0].name == ""


def test_import_missing_header_column_reports_error(config_dir: Path, tmp_path: Path):
    t = load_config(config_dir).positions
    src = tmp_path / "bad.csv"
    src.write_bytes(make_csv(valid_rows(), header=HEADER[:-1]).encode("utf-8"))
    assert t.import_file(src)


def test_export_then_import_roundtrip(config_dir: Path, tmp_path: Path):
    t = load_config(config_dir).positions
    t.update("P010", name="含,逗號", x=-100.0, rz=0.0123, description='有"引號"')
    out = tmp_path / "out.csv"
    t.export_file(out)
    assert out.read_bytes().startswith(BOM)
    before = [r for r in t.rows()]
    t.update("P010", name="改掉", x=1.0)
    assert t.import_file(out) == []
    assert t.rows() == before


def test_load_config_reports_bad_positions_csv(config_dir: Path):
    (config_dir / "positions.csv").write_bytes(make_csv(valid_rows(10)).encode("utf-8"))
    with pytest.raises(ConfigErrors) as e:
        load_config(config_dir)
    assert any("positions.csv" in m for m in e.value.errors)


def test_class_constant():
    assert PositionTable.POINT_COUNT == 200
