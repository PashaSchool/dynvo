"""Tests for the Go-stack extractors (Sprint 9c).

Covers all four extractors in ``faultline.extractors.go_module``:
  - GoTopLevelFileExtractor
  - GoSubpackageExtractor
  - GoTestFileExtractor
  - GoPerFileFolderExtractor

Synthetic repos via ``tmp_path`` only.
"""

from __future__ import annotations

from pathlib import Path

from faultline.extractors.go_module import (
    GoPerFileFolderExtractor,
    GoSubpackageExtractor,
    GoTestFileExtractor,
    GoTopLevelFileExtractor,
)


def _touch(p: Path, body: str = "package x\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _seed_go_mod(repo: Path) -> None:
    (repo / "go.mod").write_text("module example.com/x\n\ngo 1.22\n")


# ── GoTopLevelFileExtractor ──────────────────────────────────────────


def test_top_level_applicable_with_go_mod(tmp_path):
    _seed_go_mod(tmp_path)
    assert GoTopLevelFileExtractor().applicable(tmp_path) is True


def test_top_level_applicable_with_go_files_only(tmp_path):
    _touch(tmp_path / "mux.go")
    assert GoTopLevelFileExtractor().applicable(tmp_path) is True


def test_top_level_applicable_false_on_empty_repo(tmp_path):
    assert GoTopLevelFileExtractor().applicable(tmp_path) is False


def test_top_level_emits_one_signal_per_file(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "mux.go")
    _touch(tmp_path / "tree.go")
    out = GoTopLevelFileExtractor().extract(tmp_path)
    slugs = sorted(s.payload["slug"] for s in out)
    assert slugs == ["mux", "tree"]
    assert all(s.kind == "go-module" for s in out)


def test_top_level_skips_main_and_other_noise(tmp_path):
    for fn in ("main.go", "doc.go", "init.go", "version.go",
               "constants.go", "errors.go"):
        _touch(tmp_path / fn)
    out = GoTopLevelFileExtractor().extract(tmp_path)
    assert out == []


def test_top_level_skips_test_files(tmp_path):
    _touch(tmp_path / "mux_test.go")
    out = GoTopLevelFileExtractor().extract(tmp_path)
    assert out == []


# ── GoSubpackageExtractor ────────────────────────────────────────────


def test_subpackage_applicable_requires_go_mod(tmp_path):
    assert GoSubpackageExtractor().applicable(tmp_path) is False
    _seed_go_mod(tmp_path)
    assert GoSubpackageExtractor().applicable(tmp_path) is True


def test_subpackage_emits_per_directory(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "middleware" / "logger.go")
    _touch(tmp_path / "render" / "render.go")
    out = GoSubpackageExtractor().extract(tmp_path)
    slugs = sorted(s.payload["slug"] for s in out)
    assert slugs == ["middleware", "render"]
    assert all(s.kind == "go-subpackage" for s in out)


def test_subpackage_skips_root_dir(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "mux.go")  # root has go file but is skipped here
    out = GoSubpackageExtractor().extract(tmp_path)
    assert out == []


def test_subpackage_skips_directories_with_only_test_files(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "internal" / "foo_test.go")
    out = GoSubpackageExtractor().extract(tmp_path)
    assert out == []


def test_subpackage_skips_underscore_dirs(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "_examples" / "x.go")
    out = GoSubpackageExtractor().extract(tmp_path)
    # _examples is in _SKIP_DIRS; even if it weren't, slug filter
    # would drop ``_``-prefixed names.
    assert out == []


def test_subpackage_dedup_on_full_path_not_leaf(tmp_path):
    """Sprint 9c P2 fix — two ``logger`` directories at different
    paths are SEPARATE features (``middleware/logger`` vs
    ``transport/logger``). Dedup key is the full relative path,
    not just the leaf folder name.
    """
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "middleware" / "logger" / "logger.go")
    _touch(tmp_path / "transport" / "logger" / "logger.go")
    out = GoSubpackageExtractor().extract(tmp_path)
    slugs = [s.payload["slug"] for s in out]
    # Both should emit — different paths, same leaf name.
    assert slugs.count("logger") == 2
    dirs = sorted(s.payload["directory"] for s in out)
    assert dirs == ["middleware/logger", "transport/logger"]


# ── GoTestFileExtractor ──────────────────────────────────────────────


def test_test_extractor_applicable_with_go_mod(tmp_path):
    _seed_go_mod(tmp_path)
    assert GoTestFileExtractor().applicable(tmp_path) is True


def test_test_extractor_emits_one_per_test_file(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "mux_test.go")
    _touch(tmp_path / "middleware" / "logger_test.go")
    out = GoTestFileExtractor().extract(tmp_path)
    slugs = sorted(s.payload["slug"] for s in out)
    assert slugs == ["logger", "mux"]
    assert all(s.kind == "test-anchor" for s in out)


def test_test_extractor_dedup(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "mux_test.go")
    _touch(tmp_path / "internal" / "mux_test.go")
    out = GoTestFileExtractor().extract(tmp_path)
    slugs = [s.payload["slug"] for s in out]
    assert slugs.count("mux") == 1


def test_test_extractor_empty_repo(tmp_path):
    _seed_go_mod(tmp_path)
    assert GoTestFileExtractor().extract(tmp_path) == []


# ── GoPerFileFolderExtractor ─────────────────────────────────────────


def test_per_file_folder_emits_per_file_in_middleware(tmp_path):
    """The chi pattern: ``middleware/basic_auth.go`` is one feature."""
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "middleware" / "basic_auth.go")
    _touch(tmp_path / "middleware" / "heartbeat.go")
    out = GoPerFileFolderExtractor().extract(tmp_path)
    slugs = sorted(s.payload["slug"] for s in out)
    assert slugs == ["basic_auth", "heartbeat"]
    assert all(s.kind == "go-per-file" for s in out)
    assert all(s.payload["folder"] == "middleware" for s in out)


def test_per_file_folder_handles_handlers_folder(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "handlers" / "ping.go")
    out = GoPerFileFolderExtractor().extract(tmp_path)
    assert {s.payload["slug"] for s in out} == {"ping"}


def test_per_file_folder_ignores_non_canonical_folders(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "internal" / "auth.go")
    _touch(tmp_path / "pkg" / "x.go")
    out = GoPerFileFolderExtractor().extract(tmp_path)
    assert out == []


def test_per_file_folder_skips_test_files(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "middleware" / "basic_auth_test.go")
    out = GoPerFileFolderExtractor().extract(tmp_path)
    assert out == []


def test_per_file_folder_skips_noise_filenames(tmp_path):
    _seed_go_mod(tmp_path)
    for fn in ("main.go", "doc.go", "init.go", "version.go"):
        _touch(tmp_path / "middleware" / fn)
    out = GoPerFileFolderExtractor().extract(tmp_path)
    assert out == []


def test_per_file_folder_dedup_within_same_folder(tmp_path):
    _seed_go_mod(tmp_path)
    _touch(tmp_path / "middleware" / "basic_auth.go")
    # Same key (middleware/basic_auth) shouldn't appear twice if
    # the walker happens to revisit — defensive sanity check.
    out = GoPerFileFolderExtractor().extract(tmp_path)
    slugs = [s.payload["slug"] for s in out]
    assert slugs == ["basic_auth"]


# ── Empty / no go.mod ────────────────────────────────────────────────


def test_all_extractors_safe_on_repo_without_go_mod(tmp_path):
    """No go files at all — every extractor returns []."""
    assert GoTopLevelFileExtractor().extract(tmp_path) == []
    assert GoSubpackageExtractor().extract(tmp_path) == []
    assert GoTestFileExtractor().extract(tmp_path) == []
    assert GoPerFileFolderExtractor().extract(tmp_path) == []
