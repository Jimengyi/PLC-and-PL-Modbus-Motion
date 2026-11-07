"""共用 fixture：在 tmp_path 產生一份完整、合法的七軸設定資料夾。

位址依 docs/七軸_Modbus位址表.md 的規律（PLC→PC、PC→PLC 每軸 +10，極限與原點每軸 +3），
型態依 spec FR-8 型態表。後續 task 一律沿用 `config_dir`。

結構：`app.toml`、`axes.toml` 在 `config_dir`；通道的 `device.toml`、`io_points.csv` 在
`channel_dir(config_dir)`（`modbus/Channel01`）。沒有 `read_ranges.toml`：走自動分組。
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
    "Data Type",
    "Access",
    "Visible",
    "Description",
    "Attributes",
    "Check Status",
]

AXIS_NAMES = {1: "X", 2: "Y", 3: "Z", 4: "RX", 5: "RY", 6: "RZ", 7: "升降"}
AXIS_POSITION_ATTR = {
    1: "scale=10000;unit=mm",
    2: "scale=10000;unit=mm",
    3: "scale=10000;unit=mm",
    4: "scale=100;unit=deg",
    5: "scale=100;unit=deg",
    6: "scale=100;unit=deg",
    7: "scale=100;unit=mm",
}

# (名稱, 第 1 軸位址, Address Length, Data Model, Data Type, Access, 中文顯示名稱, 位址步距)
_AXIS_POINTS = [
    ("Ready", 20, 1, "Coil", "Bool", "Read Only", "就緒", 10),
    ("Busy", 21, 1, "Coil", "Bool", "Read Only", "忙碌", 10),
    ("Find Home Finish", 22, 1, "Coil", "Bool", "Read Only", "原點復歸完成", 10),
    ("Reply", 24, 1, "Coil", "Bool", "Read Only", "回覆", 10),
    ("Alarm Code", 20, 1, "Holding Register", "U16", "Read Only", "異常碼", 10),
    ("PLC Action Code", 21, 1, "Holding Register", "U16", "Read Only", "PLC 動作碼", 10),
    ("Current Position", 22, 2, "Holding Register", "I32", "Read Only", "目前位置", 10),
    ("Positive Limit", 24592, 1, "Discrete Input", "Bool", "Read Only", "正極限", 3),
    ("Home Sensor", 24593, 1, "Discrete Input", "Bool", "Read Only", "原點", 3),
    ("Negative Limit", 24594, 1, "Discrete Input", "Bool", "Read Only", "負極限", 3),
    ("Servo Motor", 122, 1, "Coil", "Bool", "Read Write", "伺服啟動", 10),
    ("Speed", 123, 1, "Holding Register", "U16", "Read Write", "速度", 10),
    ("Target Position", 121, 2, "Holding Register", "I32", "Read Write", "目標位置", 10),
    ("PC Action Code", 120, 1, "Holding Register", "U16", "Read Write", "PC 動作碼", 10),
    ("Request", 120, 1, "Coil", "Bool", "Read Write", "請求", 10),
    ("Reset Alarm", 121, 1, "Coil", "Bool", "Read Write", "異常重置", 10),
    ("JOG Plus", 123, 1, "Coil", "Bool", "Read Write", "JOG 正轉", 10),
    ("JOG Minus", 124, 1, "Coil", "Bool", "Read Write", "JOG 反轉", 10),
]
_HIDDEN = {"JOG Plus", "JOG Minus", "Request"}
_B_CONTACT = {"Positive Limit", "Negative Limit"}


def default_io_rows() -> list[dict[str, str]]:
    rows = []
    for axis in range(1, 8):
        for name, base, length, model, data_type, access, zh, step in _AXIS_POINTS:
            if data_type == "I32":
                attr = AXIS_POSITION_ATTR[axis]
            elif name in _B_CONTACT:
                attr = "invert=true"
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
                    "Data Type": data_type,
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
            "Data Type": "Bool",
            "Access": "Read Only",
            "Visible": "O",
            "Description": "七軸共用",
            "Attributes": "invert=true",
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


CHANNEL = "Channel01"

DEVICE_TOML = """# 通道設定（modbus_tags 格式）
[device]
# 啟用
enabled = true

[connection]
# 連線方式
type = "tcp"

[connection.tcp]
# PLC IP
host = "192.168.1.105"
# Modbus TCP 埠號
port = 502

[protocol]
# 請求逾時（ms）
timeout_ms = 2000
# 不重試
retries = 0

[polling]
# 輪詢週期（ms）
interval_ms = 200
# 不自動重連
auto_reconnect = false
"""

APP_TOML = """[handshake]
# 交握逾時（秒）
timeout_s = 5
# Reset_Alarm 脈衝時間（ms）
reset_alarm_pulse_ms = 500

[log]
# 日誌保留天數
retention_days = 30
"""


def channel_dir(config_dir: Path) -> Path:
    """通道資料夾：`device.toml`、`io_points.csv`、`read_ranges.toml` 所在。"""
    return config_dir / "modbus" / CHANNEL


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
    """把列資料寫成通道資料夾的 io_points.csv（預設 UTF-8 無 BOM）。"""

    def _write(
        config_dir: Path,
        rows: list[dict[str, str]],
        encoding: str = "utf-8",
        header: list[str] | None = None,
    ) -> Path:
        path = channel_dir(config_dir) / "io_points.csv"
        path.write_bytes(io_csv_text(rows, header).encode(encoding))
        return path

    return _write


@pytest.fixture
def config_dir(tmp_path: Path, write_io_points) -> Path:
    d = tmp_path / "config"
    channel_dir(d).mkdir(parents=True)
    (d / "app.toml").write_bytes(APP_TOML.encode("utf-8"))
    (channel_dir(d) / "device.toml").write_bytes(DEVICE_TOML.encode("utf-8"))
    (d / "axes.toml").write_bytes(axes_toml_text().encode("utf-8"))
    write_io_points(d, default_io_rows())
    return d


class ConfirmRecorder:
    """取代 `confirm_dialog.confirm`：記錄每次確認視窗的內容並自動回答 `answer`。"""

    def __init__(self):
        self.answer = True
        self.calls: list[dict] = []

    def __call__(self, parent, title, **kwargs):
        self.calls.append({"title": title, **kwargs})
        return self.answer

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture(autouse=True)
def confirms(monkeypatch) -> ConfirmRecorder:
    """寫入前的確認視窗一律自動回答確認（測試可改 `confirms.answer = False`）。"""
    from plc_motion.ui import confirm_dialog

    recorder = ConfirmRecorder()
    monkeypatch.setattr(confirm_dialog, "confirm", recorder)
    return recorder
