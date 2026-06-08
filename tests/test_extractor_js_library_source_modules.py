"""Tests for Commit C — js-library source-submodule emission.

Covers the three changes:
  1. Flat source-module emission (root-level + one level into subdirs).
  2. Public-reachability gate for nested dirs (barrel re-export required).
  3. Median-outlier collapse guard for exploding flat dirs.

Synthetic neutral fixtures only (memory/rule-no-repo-specific-paths).
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.js_library import (
    JsLibraryExtractor,
    _collapse_oversplit_flat,
)


def _ctx(*, repo_path: Path, tracked_files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="js-generic",
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack="js-library",
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=0.9,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _names(anchors) -> set[str]:
    return {a.name for a in anchors}


# ── (a) root-flat lib: flat src/*.ts modules emit ──────────────────────────


def test_root_flat_modules_emit(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "flatlib", "version": "1.0.0",
        "exports": {".": "./src/index.ts"},
    }))
    _write(tmp_path / "src" / "index.ts", "export * from './alpha'\n")
    for mod in ("alpha", "beta", "gamma"):
        _write(tmp_path / "src" / f"{mod}.ts", f"export const {mod} = 1\n")

    anchors = JsLibraryExtractor().extract(_ctx(
        repo_path=tmp_path,
        tracked_files=[
            "package.json", "src/index.ts",
            "src/alpha.ts", "src/beta.ts", "src/gamma.ts",
        ],
    ))
    got = _names(anchors)
    assert {"alpha", "beta", "gamma"} <= got


# ── (b) nested public dir reachable via barrel star-export → emit ──────────


def test_nested_public_dir_emits(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "fetchlib", "version": "1.0.0",
        "exports": {".": "./source/index.ts"},
    }))
    # Source-root barrel re-exports THROUGH core/ — makes it public.
    _write(tmp_path / "source" / "index.ts",
           "export * from './core/options'\nexport * from './core/response'\n")
    _write(tmp_path / "source" / "core" / "options.ts", "export const o = 1\n")
    _write(tmp_path / "source" / "core" / "response.ts", "export const r = 1\n")
    _write(tmp_path / "source" / "core" / "errors.ts", "export const e = 1\n")

    anchors = JsLibraryExtractor().extract(_ctx(
        repo_path=tmp_path,
        tracked_files=[
            "package.json", "source/index.ts",
            "source/core/options.ts", "source/core/response.ts",
            "source/core/errors.ts",
        ],
    ))
    got = _names(anchors)
    # The nested public dir's flat leaves are caught.
    assert {"options", "response", "errors"} <= got


# ── (c) nested internal helper dir NOT reachable → must NOT emit ───────────


def test_nested_internal_helper_dir_not_emitted(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "helperlib", "version": "1.0.0",
        "exports": {".": "./src/index.ts"},
    }))
    # Barrel imports util internally but does NOT re-export through it.
    _write(tmp_path / "src" / "index.ts",
           "import { clamp } from './util/clamp'\nexport const main = clamp\n")
    _write(tmp_path / "src" / "util" / "clamp.ts", "export const clamp = 1\n")
    _write(tmp_path / "src" / "util" / "merge.ts", "export const merge = 1\n")

    anchors = JsLibraryExtractor().extract(_ctx(
        repo_path=tmp_path,
        tracked_files=[
            "package.json", "src/index.ts",
            "src/util/clamp.ts", "src/util/merge.ts",
        ],
    ))
    got = _names(anchors)
    # Internal helper leaves must NOT become per-file phantom features.
    assert "clamp" not in got
    assert "merge" not in got


# ── (d) giant flat dir WITH folder anchor → collapse ───────────────────────


def test_collapse_with_fold_target() -> None:
    # Sibling folder-modules have small file-counts (median 2).
    folder_file_counts = [2, 2, 1]
    # An exploding flat dir that IS also a folder anchor (foldable).
    flat_by_dir = {"src/big": {f"m{i}": f"src/big/m{i}.ts" for i in range(20)}}
    foldable = {"src/big"}
    collapsed = _collapse_oversplit_flat(flat_by_dir, folder_file_counts, foldable)
    assert collapsed == {"src/big"}


# ── (e) giant flat dir NO fold target → still emit (floor-only) ────────────


def test_no_collapse_without_fold_target() -> None:
    folder_file_counts = [2, 2, 1]
    flat_by_dir = {"src/core": {f"m{i}": f"src/core/m{i}.ts" for i in range(20)}}
    foldable: set[str] = set()  # no folder anchor for src/core
    collapsed = _collapse_oversplit_flat(flat_by_dir, folder_file_counts, foldable)
    # Nothing collapses — leaves are the feature surface, kept.
    assert collapsed == set()


def test_small_flat_dir_below_floor_not_collapsed() -> None:
    # A handful of flat modules IS the feature map — never collapse.
    flat_by_dir = {"src": {f"m{i}": f"src/m{i}.ts" for i in range(3)}}
    collapsed = _collapse_oversplit_flat(flat_by_dir, [], {"src"})
    assert collapsed == set()


def test_d_ts_declaration_files_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "typedlib", "version": "1.0.0",
        "exports": {".": "./src/index.ts"},
    }))
    _write(tmp_path / "src" / "index.ts", "export const x = 1\n")
    _write(tmp_path / "src" / "alpha.ts", "export const alpha = 1\n")
    _write(tmp_path / "src" / "alpha.d.ts", "export declare const alpha: number\n")

    anchors = JsLibraryExtractor().extract(_ctx(
        repo_path=tmp_path,
        tracked_files=[
            "package.json", "src/index.ts",
            "src/alpha.ts", "src/alpha.d.ts",
        ],
    ))
    # Exactly one "alpha" anchor; the .d.ts must not create a duplicate.
    assert sum(1 for a in anchors if a.name == "alpha") == 1
