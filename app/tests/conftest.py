"""共用 fixture：在 tmp_path 產生一份完整、合法的七軸設定資料夾。

位址依 docs/七軸_Modbus位址表.md 的規律（PLC→PC、PC→PLC 每軸 +10，極限與原點每軸 +3），
型態依 spec FR-8 型態表。後續 task 一律沿用 `config_dir`。
"""

import csv
import io
from pathlib import Path

import pytest

IO_HEADER = [
    "Tag Name",
    "Display Name",
    "Start Address",
    "Address Length",
    "Unit ID",
    "Data Model",
    "Sensor Type",
    "Access",
    "Visible",
    "Description",
    "Attributes",
    "Check Status",
]

AXIS_NAMES = {1: "X", 2: "Y", 3: "Z", 4: "RX", 5: "RY", 6: "RZ", 7: "升降"}
AXIS_POSITION_ATTR = {
    1: "10000,mm",
    2: "10000,mm",
    3: "10000,mm",
    4: "100,deg",
    5: "100,deg",
    6: "100,deg",
    7: "100,mm",
}

# (名稱, 第 1 軸位址, Address Length, Data Model, Sensor Type, Access, 中文顯示名稱, 位址步距)
_AXIS_POINTS = [
    ("Ready", 20, 1, "Coil", "Boolean", "Read Only", "就緒", 10),
    ("Busy", 21, 1, "Coil", "Boolean", "Read Only", "忙碌", 10),
    ("Find Home Finish", 22, 1, "Coil", "Boolean", "Read Only", "原點復歸完成", 10),
    ("Reply", 24, 1, "Coil", "Boolean", "Read Only", "回覆", 10),
    ("Alarm Code", 20, 1, "Holding Register", "U16", "Read Only", "異常碼", 10),
    ("PLC Action Code", 21, 1, "Holding Register", "U16", "Read Only", "PLC 動作碼", 10),
    ("Current Position", 22, 2, "Holding Register", "Position", "Read Only", "目前位置", 10),
    ("Positive Limit", 24592, 1, "Discrete Input", "Boolean", "Read Only", "正極限", 3),
    ("Home Sensor", 24593, 1, "Discrete Input", "Boolean", "Read Only", "原點", 3),
    ("Negative Limit", 24594, 1, "Discrete Input", "Boolean", "Read Only", "負極限", 3),
    ("Servo Motor", 122, 1, "Coil", "Boolean", "Read Write", "伺服啟動", 10),
    ("Speed", 123, 1, "Holding Register", "U16", "Read Write", "速度", 10),
    ("Target Position", 121, 2, "Holding Register", "Position", "Read Write", "目標位置", 10),
    ("PC Action Code", 120, 1, "Holding Register", "U16", "Read Write", "PC 動作碼", 10),
    ("Request", 120, 1, "Coil", "Boolean", "Read Write", "請求", 10),
    ("Reset Alarm", 121, 1, "Coil", "Boolean", "Read Write", "異常重置", 10),
    ("JOG Plus", 123, 1, "Coil", "Boolean", "Read Write", "JOG 正轉", 10),
    ("JOG Minus", 124, 1, "Coil", "Boolean", "Read Write", "JOG 反轉", 10),
]
_HIDDEN = {"JOG Plus", "JOG Minus", "Request"}
_B_CONTACT = {"Positive Limit", "Negative Limit"}


def default_io_rows() -> list[dict[str, str]]:
    rows = []
    for axis in range(1, 8):
        for name, base, length, model, sensor, access, zh, step in _AXIS_POINTS:
            if sensor == "Position":
                attr = AXIS_POSITION_ATTR[axis]
            elif name in _B_CONTACT:
                attr = "ON"
            else:
                attr = ""
            rows.append(
                {
                    "Tag Name": f"{name} {axis}",
                    "Display Name": f"{AXIS_NAMES[axis]} 軸{zh}",
                    "Start Address": str(base + step * (axis - 1)),
                    "Address Length": str(length),
                    "Unit ID": "0",
                    "Data Model": model,
                    "Sensor Type": sensor,
                    "Access": access,
                    "Visible": "X" if name in _HIDDEN else "O",
                    "Description": "",
                    "Attributes": attr,
                    "Check Status": "Unchecked",
                }
            )
    rows.append(
        {
            "Tag Name": "Emergency Stop",
            "Display Name": "緊急停止",
            "Start Address": "24576",
            "Address Length": "1",
            "Unit ID": "0",
            "Data Model": "Discrete Input",
            "Sensor Type": "Boolean",
            "Access": "Read Only",
            "Visible": "O",
            "Description": "七軸共用",
            "Attributes": "ON",
            "Check Status": "Unchecked",
        }
    )
    return rows


def io_csv_text(rows: list[dict[str, str]], header: list[str] | None = None) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header or IO_HEADER, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


DEVICE_TOML = """\
# 連線設定
[connection]
# PLC IP
host = "192.168.1.105"
# Modbus TCP 埠號
port = 502
# 連線逾時（ms）
timeout_ms = 2000

[polling]
# 輪詢週期（ms）
interval_ms = 200

[handshake]
# 交握逾時（秒）
timeout_s = 5
# Reset_Alarm 脈衝時間（ms）
reset_alarm_pulse_ms = 500

[log]
# 日誌保留天數
retention_days = 30

[ui]
# 主題：dark 或 light
theme = "dark"  # 預設深色
"""


def axes_toml_text() -> str:
    parts = []
    for axis, name in AXIS_NAMES.items():
        parts.append(
            f"""\
[[axes]]
number = {axis}
name = "{name}"
soft_limit_min = -100.0
soft_limit_max = 100.0
step_long = 10.0
step_medium = 1.0
step_short = 0.1
default_speed = 50
"""
        )
    return "\n".join(parts)


@pytest.fixture
def io_rows() -> list[dict[str, str]]:
    """`config_dir` 所用 io_points.csv 的列資料（每次呼叫為新副本，可自由修改）。"""
    return default_io_rows()


@pytest.fixture
def write_io_points():
    """把列資料寫成 io_points.csv（預設 UTF-8 無 BOM）。"""

    def _write(
        config_dir: Path,
        rows: list[dict[str, str]],
        encoding: str = "utf-8",
        header: list[str] | None = None,
    ) -> Path:
        path = config_dir / "io_points.csv"
        path.write_bytes(io_csv_text(rows, header).encode(encoding))
        return path

    return _write


@pytest.fixture
def config_dir(tmp_path: Path, write_io_points) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "device.toml").write_bytes(DEVICE_TOML.encode("utf-8"))
    (d / "axes.toml").write_bytes(axes_toml_text().encode("utf-8"))
    write_io_points(d, default_io_rows())
    return d
