"""UI 測試共用：載入 `config_dir`、以 PlcModel 造快照、記錄命令呼叫的假 worker。"""

from pathlib import Path

import pytest
from ui_support import FakeWorker

from plc_motion.plc import ModelPort, PlcModel
from plc_motion.settings import load_config


@pytest.fixture
def config(config_dir: Path):
    return load_config(config_dir)


@pytest.fixture
def make_snapshot(config):
    """snapshot 工廠：在 PlcModel 初始值上套用 `values`（tag → raw）後讀出快照。"""

    def make(values: dict[str, int | bool] | None = None):
        model = PlcModel(config.points)
        for tag, value in (values or {}).items():
            model.master_write(tag, value)  # 不呼叫 step()，值原樣留在快照裡
        return ModelPort(model).read_all()

    return make


@pytest.fixture
def fake_worker():
    return FakeWorker()
