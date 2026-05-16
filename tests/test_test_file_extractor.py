"""Tests for ``faultline.extractors.test_file.TestFileExtractor``
(Sprint 9a).

Covers applicability gate, the library safety gate, slug extraction,
the noise-slug filter, dedup across multiple matches, special path
characters, and walks under nested test directories.

Uses ``tmp_path`` to build synthetic tiny repos — does NOT depend on
any out-of-tree fixture corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faultline.extractors.test_file import TestFileExtractor


@dataclass
class _RepoStruct:
    """Minimal duck-typed stand-in for ``RepoStructure``."""
    is_library: bool


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# stub\n")


# ── applicable() ─────────────────────────────────────────────────────


def test_applicable_returns_true_unconditionally(tmp_path):
    """Per the docstring: gating happens at extract() time once
    repo_structure is available; applicable() is a cheap no-op.
    """
    assert TestFileExtractor().applicable(tmp_path) is True


# ── extract() — safety gate ──────────────────────────────────────────


def test_extract_returns_empty_without_repo_structure(tmp_path):
    _touch(tmp_path / "tests" / "test_security.py")
    assert TestFileExtractor().extract(tmp_path) == []


def test_extract_returns_empty_when_not_a_library(tmp_path):
    _touch(tmp_path / "tests" / "test_security.py")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=False),
    )
    assert out == []


# ── extract() — Python convention ────────────────────────────────────


def test_extract_python_test_file_emits_one_signal(tmp_path):
    _touch(tmp_path / "tests" / "test_security.py")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    assert len(out) == 1
    assert out[0].kind == "test-anchor"
    assert out[0].payload["slug"] == "security"
    assert out[0].payload["match_kind"] == "py-test"


def test_extract_python_trailing_test_suffix(tmp_path):
    """``billing_test.py`` should produce slug ``billing`` — the
    optional trailing ``_test`` group is matched and excluded from
    the capture.
    """
    _touch(tmp_path / "tests" / "billing_test.py")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    assert len(out) == 1
    assert out[0].payload["slug"] == "billing"


# ── extract() — JS/TS conventions ────────────────────────────────────


def test_extract_jest_dunder_tests(tmp_path):
    _touch(tmp_path / "__tests__" / "interceptors.test.ts")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    slugs = {s.payload["slug"] for s in out}
    assert "interceptors" in slugs


def test_extract_nested_tests_subdir(tmp_path):
    """axios uses ``tests/unit/<feature>.test.js``."""
    _touch(tmp_path / "tests" / "unit" / "cancel.test.js")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    slugs = {s.payload["slug"] for s in out}
    assert "cancel" in slugs


def test_extract_colocated_test(tmp_path):
    _touch(tmp_path / "src" / "billing.test.ts")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    slugs = {s.payload["slug"] for s in out}
    assert "billing" in slugs


# ── noise / dedup / empty ────────────────────────────────────────────


def test_noise_slugs_get_filtered(tmp_path):
    """``test_index.py``, ``test_helpers.py``, etc. are scaffolding."""
    for fn in ("test_index.py", "test_helpers.py",
               "test_utils.py", "test_setup.py", "conftest.py"):
        _touch(tmp_path / "tests" / fn)
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    assert out == []


def test_duplicate_slugs_across_paths_emit_once(tmp_path):
    _touch(tmp_path / "tests" / "test_security.py")
    _touch(tmp_path / "tests" / "unit" / "security.test.ts")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    slugs = [s.payload["slug"].lower() for s in out]
    assert slugs.count("security") == 1


def test_skip_dirs_are_not_walked(tmp_path):
    """node_modules / .venv contents must be ignored."""
    _touch(tmp_path / "node_modules" / "tests" / "test_evil.py")
    _touch(tmp_path / ".venv" / "lib" / "test_inner.py")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    assert out == []


def test_empty_repo_returns_empty(tmp_path):
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    assert out == []


def test_special_chars_in_slug_with_dash(tmp_path):
    _touch(tmp_path / "__tests__" / "cancel-token.test.ts")
    out = TestFileExtractor().extract(
        tmp_path, repo_structure=_RepoStruct(is_library=True),
    )
    slugs = {s.payload["slug"] for s in out}
    assert "cancel-token" in slugs


def test_extractor_name_attribute_present():
    assert TestFileExtractor.name == "test-file-extractor"
