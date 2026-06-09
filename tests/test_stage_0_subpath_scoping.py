"""Tests for monorepo ``--subpath`` scoping in Stage 0 intake.

Covers the three blocker fixes + workspace emission for the
monorepo-subprojects feature (spec §3):

  - scope + relativize tracked files (extractors see ``app/page.tsx``,
    not ``apps/web/app/page.tsx``)
  - fail loud when a subpath can't be scoped (bogus path / empty filter)
  - effective-root stack detection (subtree is a real Next app root)

These run with ``skip_git=True`` so we don't have to ``git init`` each
fixture; the git-history-scoping path is covered in
``test_git_subpath_scoping.py`` against a real repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2 import ScanContext, stage_0_intake
from faultline.pipeline_v2.stage_0_intake import SubpathScopeError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2))


def _make_monorepo(root: Path) -> None:
    """A pnpm/turbo monorepo with a Next app + a backend worker."""
    _write_json(root / "package.json", {"name": "mono", "private": True})
    _write(root / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _write_json(root / "turbo.json", {"pipeline": {}})

    # apps/web — Next App Router
    _write_json(root / "apps" / "web" / "package.json", {
        "name": "@mono/web",
        "dependencies": {"next": "^15.0.0", "react": "^19.0.0"},
    })
    _write(root / "apps" / "web" / "next.config.ts", "export default {}")
    _write(root / "apps" / "web" / "app" / "page.tsx", "export default () => null")
    _write(root / "apps" / "web" / "app" / "dashboard" / "page.tsx", "export default () => null")

    # apps/worker — a plain backend
    _write_json(root / "apps" / "worker" / "package.json", {
        "name": "@mono/worker",
        "dependencies": {"express": "^4.0.0"},
    })
    _write(root / "apps" / "worker" / "src" / "index.ts", "console.log('hi')")

    # a sibling that must NOT be matched by a prefix bug
    _write_json(root / "apps" / "web-extra" / "package.json", {"name": "@mono/web-extra"})
    _write(root / "apps" / "web-extra" / "src" / "x.ts", "export const x = 1")


def test_subpath_scopes_and_relativizes_tracked_files(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    ctx = stage_0_intake(tmp_path, skip_git=True, subpath="apps/web")

    assert isinstance(ctx, ScanContext)
    assert ctx.subpath == "apps/web"
    # repo_path is now the subtree root
    assert ctx.repo_path == (tmp_path / "apps" / "web").resolve()
    # every tracked path is subpath-relative — extractors see app/page.tsx
    assert "app/page.tsx" in ctx.tracked_files
    assert "app/dashboard/page.tsx" in ctx.tracked_files
    # nothing leaks from sibling packages
    assert all(not f.startswith("apps/") for f in ctx.tracked_files)
    assert not any("worker" in f for f in ctx.tracked_files)
    # the sibling ``web-extra`` must not be folded in by a prefix bug
    assert not any("x.ts" in f for f in ctx.tracked_files)


def test_subpath_detects_stack_at_subtree_root(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    ctx = stage_0_intake(tmp_path, skip_git=True, subpath="apps/web")

    # the subtree is a real Next App Router root — stack must resolve
    # from apps/web/package.json + apps/web/next.config.ts + app/.
    assert ctx.stack == "next-app-router"
    # scoped to a single app, it should NOT look like a monorepo itself
    assert ctx.monorepo is False


def test_trailing_slash_and_dot_prefix_normalised(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    ctx1 = stage_0_intake(tmp_path, skip_git=True, subpath="apps/web/")
    ctx2 = stage_0_intake(tmp_path, skip_git=True, subpath="./apps/web")

    assert ctx1.subpath == "apps/web"
    assert ctx2.subpath == "apps/web"
    assert "app/page.tsx" in ctx1.tracked_files
    assert "app/page.tsx" in ctx2.tracked_files


def test_empty_subpath_is_whole_repo(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    ctx = stage_0_intake(tmp_path, skip_git=True, subpath="")

    assert ctx.subpath is None
    assert ctx.repo_path == tmp_path.resolve()
    # whole-repo: paths stay repo-root-relative
    assert any(f.startswith("apps/web/") for f in ctx.tracked_files)


def test_no_subpath_is_unchanged_whole_repo(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    ctx = stage_0_intake(tmp_path, skip_git=True)

    assert ctx.subpath is None
    assert ctx.monorepo is True
    assert any(f.startswith("apps/web/") for f in ctx.tracked_files)


# ── Fail-loud guard ──────────────────────────────────────────────────


def test_bogus_subpath_raises(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    with pytest.raises(SubpathScopeError):
        stage_0_intake(tmp_path, skip_git=True, subpath="apps/does-not-exist")


def test_subpath_escaping_repo_root_raises(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    with pytest.raises(SubpathScopeError):
        stage_0_intake(tmp_path, skip_git=True, subpath="../outside")


def test_subpath_with_no_tracked_files_raises(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    # An empty dir that exists but has no tracked source.
    (tmp_path / "apps" / "empty").mkdir(parents=True)

    with pytest.raises(SubpathScopeError):
        stage_0_intake(tmp_path, skip_git=True, subpath="apps/empty")


# ── Workspace emission ───────────────────────────────────────────────


def test_whole_repo_emits_workspaces(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)

    ctx = stage_0_intake(tmp_path, skip_git=True)

    assert ctx.workspaces is not None
    paths = {w.path for w in ctx.workspaces}
    # the workspace globs ``apps/*`` → at least web + worker enumerated
    assert "apps/web" in paths
    assert "apps/worker" in paths
