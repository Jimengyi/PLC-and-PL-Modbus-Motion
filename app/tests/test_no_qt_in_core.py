"""核心層（交握邏輯、通訊 adapter、快照、設定載入）不得匯入 Qt（FR-34）。"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "plc_motion"
CORE = [SRC / "engine", SRC / "plc", SRC / "settings", SRC / "codec.py"]
QT_ROOTS = {"PySide6", "shiboken6", "PyQt5", "PyQt6"}


def core_files() -> list[Path]:
    files: list[Path] = []
    for entry in CORE:
        files += sorted(entry.rglob("*.py")) if entry.is_dir() else [entry]
    return files


def qt_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        found += [n for n in names if n.split(".")[0] in QT_ROOTS]
    return found


def test_scanner_detects_qt_import():
    assert qt_imports("from PySide6.QtCore import QObject") == ["PySide6.QtCore"]
    assert qt_imports("def f():\n    import PySide6.QtGui") == ["PySide6.QtGui"]
    assert qt_imports("import json\nfrom pathlib import Path") == []


def test_core_files_are_found():
    names = {p.name for p in core_files()}
    assert {
        "engine.py",
        "handshake.py",
        "tags_link.py",
        "snapshot.py",
        "loader.py",
        "codec.py",
    } <= names


@pytest.mark.parametrize("path", core_files(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_core_module_does_not_import_qt(path: Path):
    assert qt_imports(path.read_text("utf-8")) == []
