"""Tests for the Sprint 6 Python package sub-directory extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.python_package_subdir import (
    PythonPackageSubdirExtractor,
    collect_python_subpackages,
)
from faultline.protocols import Extractor


def _mk_pkg(root: Path, rel: str, modules: list[str] | None = None) -> Path:
    p = root / rel
    p.mkdir(parents=True, exist_ok=True)
    (p / "__init__.py").write_text("")
    for m in modules or []:
        (p / f"{m}.py").write_text("")
    return p


def test_extractor_conforms_to_protocol():
    assert isinstance(PythonPackageSubdirExtractor(), Extractor)


def test_finds_named_subpackages_under_main_package(tmp_path):
    """Apprise-shaped layout: main pkg with sub-packages."""
    _mk_pkg(tmp_path, "mylib", modules=["main", "common"])
    _mk_pkg(tmp_path, "mylib/notifications", modules=["base", "discord", "slack"])
    _mk_pkg(tmp_path, "mylib/storage", modules=["base", "s3", "local"])

    out = collect_python_subpackages(tmp_path)
    names = sorted(s.name for s in out)
    assert names == ["notifications", "storage"]
    by_name = {s.name: s for s in out}
    assert by_name["notifications"].module_count == 3
    assert by_name["storage"].parent_package == "mylib"


def test_skips_tooling_subpackage_names(tmp_path):
    _mk_pkg(tmp_path, "mylib")
    _mk_pkg(tmp_path, "mylib/utils", modules=["x", "y"])
    _mk_pkg(tmp_path, "mylib/helpers", modules=["a"])
    _mk_pkg(tmp_path, "mylib/internal", modules=["a"])
    _mk_pkg(tmp_path, "mylib/notifications", modules=["a"])

    out = collect_python_subpackages(tmp_path)
    names = {s.name for s in out}
    assert names == {"notifications"}


def test_skips_underscore_prefixed_subpackages(tmp_path):
    _mk_pkg(tmp_path, "mylib")
    _mk_pkg(tmp_path, "mylib/_private", modules=["x"])
    _mk_pkg(tmp_path, "mylib/public", modules=["x"])

    out = collect_python_subpackages(tmp_path)
    assert {s.name for s in out} == {"public"}


def test_skips_dirs_without_init_py(tmp_path):
    """A dir that's not a Python package (no __init__.py) is not
    a sub-package."""
    _mk_pkg(tmp_path, "mylib")
    (tmp_path / "mylib" / "data").mkdir()
    (tmp_path / "mylib" / "data" / "raw.csv").write_text("a,b,c")

    assert collect_python_subpackages(tmp_path) == []


def test_skips_test_dirs_and_node_modules(tmp_path):
    _mk_pkg(tmp_path, "tests/mylib")
    _mk_pkg(tmp_path, "tests/mylib/notifications", modules=["x"])
    _mk_pkg(tmp_path, "node_modules/somelib")
    _mk_pkg(tmp_path, "node_modules/somelib/notifications", modules=["x"])

    assert collect_python_subpackages(tmp_path) == []


def test_emits_one_signal_per_subpackage_with_sample(tmp_path):
    _mk_pkg(tmp_path, "mylib")
    _mk_pkg(tmp_path, "mylib/integrations",
            modules=["discord", "slack", "telegram", "email"])

    sigs = PythonPackageSubdirExtractor().extract(tmp_path, files=())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "python-subpackage"
    assert s.source == "python-package-subdir-extractor"
    assert s.payload["name"] == "integrations"
    assert s.payload["module_count"] == 4
    assert "discord" in s.payload["sample_modules"]


def test_extractor_applicable_false_on_non_python_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("")
    assert PythonPackageSubdirExtractor().applicable(tmp_path) is False


def test_extractor_applicable_true_on_any_python_pkg(tmp_path):
    _mk_pkg(tmp_path, "anyname")
    assert PythonPackageSubdirExtractor().applicable(tmp_path) is True
