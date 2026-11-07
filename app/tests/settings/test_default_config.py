"""repo 內建的預設 config（app/config）：能載入，且與 docs/七軸_Modbus位址表.md 相符。"""

import tomllib
from pathlib import Path

import pytest

from plc_motion.settings import load_config

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config"

# (名稱, 第 1 軸位址, 每軸位址步距)，依 docs/七軸_Modbus位址表.md
ADDRESS_RULES = [
    ("Ready", 20, 10),
    ("Busy", 21, 10),
    ("Find Home Finish", 22, 10),
    ("Reply", 24, 10),
    ("Alarm Code", 20, 10),
    ("PLC Action Code", 21, 10),
    ("Current Position", 22, 10),
    ("Positive Limit", 24592, 3),
    ("Home Sensor", 24593, 3),
    ("Negative Limit", 24594, 3),
    ("Servo Motor", 122, 10),
    ("Speed", 123, 10),
    ("Target Position", 121, 10),
    ("PC Action Code", 120, 10),
    ("Request", 120, 10),
    ("Reset Alarm", 121, 10),
    ("JOG Plus", 123, 10),
    ("JOG Minus", 124, 10),
]
SCALES = {1: (10000, "mm"), 2: (10000, "mm"), 3: (10000, "mm")}
SCALES |= {4: (100, "deg"), 5: (100, "deg"), 6: (100, "deg"), 7: (100, "mm")}
# 轉換前 Attributes 為 ON（B 接點）的點：轉成 invert=true
B_CONTACT = {"Positive Limit", "Negative Limit"}


@pytest.fixture(scope="module")
def default_config(tmp_path_factory):
    # 複製到暫存資料夾再載入：positions.csv 缺檔時 load_config 會自動建立，不能污染 repo
    import shutil

    work = tmp_path_factory.mktemp("default_config")
    shutil.copytree(DEFAULT_CONFIG, work, dirs_exist_ok=True)
    return load_config(work)


def test_default_config_loads(default_config):
    assert len(default_config.axes) == 7


def test_all_required_tags_present_with_addresses_from_address_table(default_config):
    points = default_config.points
    assert len(points.all()) == 7 * 18 + 1
    for name, base, step in ADDRESS_RULES:
        for axis in range(1, 8):
            assert points.axis_tag(name, axis).start_address == base + step * (axis - 1), (
                name,
                axis,
            )
    estop = points.get("Emergency Stop")
    assert estop.start_address == 24576
    assert estop.invert


def test_required_tags_data_model_type_and_contact(default_config):
    from lib.modbus_tags import DataModel
    from plc_motion.settings.io_points import required_tag_types

    for tag, spec in required_tag_types().items():
        p = default_config.points.get(tag)
        assert (p.data_model, p.data_type, p.access, p.length) == spec[:4], tag
        base = tag.rsplit(" ", 1)[0]
        assert p.invert == (base in B_CONTACT or tag == "Emergency Stop"), tag
        assert (p.word_order, p.byte_swap, p.bit) == (None, False, None), tag
        if p.data_model == DataModel.DISCRETE_INPUT:
            assert p.data_type == "Bool"


def test_position_scales_per_axis(default_config):
    for axis, attr in SCALES.items():
        for name in ("Current Position", "Target Position"):
            p = default_config.points.axis_tag(name, axis)
            assert (p.scale, p.unit) == attr
            assert type(p.scale) is int


def test_visible_unit_id_and_check_status(default_config):
    hidden = {"JOG Plus", "JOG Minus", "Request"}
    for p in default_config.points.all():
        assert p.unit_id == 0
        assert str(p.check_status) == "Unchecked"
        base_name = p.tag.rsplit(" ", 1)[0]
        assert p.visible == (base_name not in hidden), p.tag


CHANNEL = DEFAULT_CONFIG / "modbus" / "Channel01"


def test_config_folder_structure():
    """spec FR-6：app.toml、axes.toml 在 config；通道檔案在 modbus/Channel01。"""
    assert (DEFAULT_CONFIG / "app.toml").is_file()
    assert (DEFAULT_CONFIG / "axes.toml").is_file()
    assert sorted(p.name for p in (DEFAULT_CONFIG / "modbus").iterdir()) == ["Channel01"]
    for name in ("device.toml", "io_points.csv", "read_ranges.toml"):
        assert (CHANNEL / name).is_file(), name
    for name in ("device.toml", "io_points.csv", "read_ranges.toml"):
        assert not (DEFAULT_CONFIG / name).exists(), name


def test_device_toml_values():
    """spec FR-8：TCP，連線目標與輪詢週期同舊版預設；逾時 2000 ms、不重試、不自動重連。"""
    d = tomllib.loads((CHANNEL / "device.toml").read_text(encoding="utf-8"))
    assert d["device"]["enabled"] is True
    assert d["connection"] == {"type": "tcp", "tcp": {"host": "192.168.1.105", "port": 502}}
    assert d["protocol"] == {"timeout_ms": 2000, "retries": 0}
    assert d["polling"] == {"interval_ms": 200, "auto_reconnect": False}


def test_app_toml_values():
    d = tomllib.loads((DEFAULT_CONFIG / "app.toml").read_text(encoding="utf-8"))
    assert d == {
        "handshake": {"timeout_s": 5, "reset_alarm_pulse_ms": 500},
        "log": {"retention_days": 30},
    }


@pytest.mark.parametrize("path", [CHANNEL / "device.toml", DEFAULT_CONFIG / "app.toml"])
def test_toml_every_key_has_comment_above(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            assert lines[i - 1].lstrip().startswith("#"), f"{path.name} 第 {i + 1} 列缺註解：{line}"


def test_axes_names_and_pending_comment(default_config):
    assert [default_config.axes[n].name for n in range(1, 8)] == [
        "X",
        "Y",
        "Z",
        "RX",
        "RY",
        "RZ",
        "升降",
    ]
    text = (DEFAULT_CONFIG / "axes.toml").read_text(encoding="utf-8")
    # 每軸的軟體極限與步進距離上方各有一則註解
    assert text.count("待依機構行程確認") >= 7


def test_positions_csv_has_200_blank_points(default_config):
    rows = default_config.positions.rows()
    assert len(rows) == 200
    assert rows[0].point_id == "P001" and rows[-1].point_id == "P200"
    assert (DEFAULT_CONFIG / "positions.csv").is_file()


def test_default_read_ranges_cover_all_points_in_five_requests(default_config):
    from plc_motion.plc.batching import plan_reads

    assert default_config.read_ranges is not None
    blocks = plan_reads(default_config.points.all(), default_config.read_ranges)
    assert len(blocks) == 5
    tags = sorted(p.tag for b in blocks for p in b.points)
    assert tags == sorted(p.tag for p in default_config.points.all())
