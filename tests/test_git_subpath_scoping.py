"""Tests for git-level subpath scoping (monorepo-subprojects spec §3.2.3).

``scope_files_to_subpath`` / ``normalize_subpath`` are pure helpers;
``get_commits(..., src=)`` scopes the history pass so co-change /
bug-ratio / coverage don't compute over the whole monorepo.

A real git repo is built per test (the fast ``git log --name-only``
path needs real commits). Tests are independent of clone state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from faultline.analyzer.git import (
    get_commits,
    load_repo,
    normalize_subpath,
    scope_files_to_subpath,
)


# ── Pure helpers ─────────────────────────────────────────────────────


def test_normalize_subpath_variants() -> None:
    assert normalize_subpath("apps/web") == "apps/web"
    assert normalize_subpath("apps/web/") == "apps/web"
    assert normalize_subpath("./apps/web") == "apps/web"
    assert normalize_subpath("") is None
    assert normalize_subpath(".") is None
    assert normalize_subpath(None) is None


def test_scope_files_filters_and_relativizes() -> None:
    files = [
        "apps/web/app/page.tsx",
        "apps/web/lib/x.ts",
        "apps/worker/src/index.ts",
        "package.json",
    ]
    scoped = scope_files_to_subpath(files, "apps/web")
    assert sorted(scoped) == ["app/page.tsx", "lib/x.ts"]


def test_scope_files_segment_safe() -> None:
    # ``apps/web`` must NOT match the sibling ``apps/web-extra``.
    files = ["apps/web/a.ts", "apps/web-extra/b.ts"]
    scoped = scope_files_to_subpath(files, "apps/web")
    assert scoped == ["a.ts"]


def test_scope_files_none_is_passthrough() -> None:
    files = ["apps/web/a.ts", "b.ts"]
    assert scope_files_to_subpath(files, None) == files


# ── git history scoping ──────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def _init(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "config", "user.name", "Test")


def _commit(root: Path, rel: str, content: str, msg: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(root, "add", rel)
    _git(root, "commit", "-m", msg)


def test_get_commits_scoped_drops_other_subtrees(tmp_path: Path) -> None:
    _init(tmp_path)
    _commit(tmp_path, "apps/web/page.tsx", "a", "web: add page")
    _commit(tmp_path, "apps/worker/index.ts", "b", "worker: add index")
    _commit(tmp_path, "apps/web/lib.ts", "c", "web: add lib")

    repo = load_repo(str(tmp_path))

    web = get_commits(repo, days=3650, src="apps/web")
    # only the two web commits survive
    assert len(web) == 2
    msgs = {c.message for c in web}
    assert msgs == {"web: add page", "web: add lib"}
    # files_changed are relativized to the subpath
    all_files = {f for c in web for f in c.files_changed}
    assert all_files == {"page.tsx", "lib.ts"}
    assert not any("apps/" in f for f in all_files)


def test_get_commits_whole_repo_unchanged(tmp_path: Path) -> None:
    _init(tmp_path)
    _commit(tmp_path, "apps/web/page.tsx", "a", "web: add page")
    _commit(tmp_path, "apps/worker/index.ts", "b", "worker: add index")

    repo = load_repo(str(tmp_path))

    whole = get_commits(repo, days=3650)
    assert len(whole) == 2
    all_files = {f for c in whole for f in c.files_changed}
    # whole-repo: paths stay repo-root-relative (back-compat)
    assert "apps/web/page.tsx" in all_files
    assert "apps/worker/index.ts" in all_files


def test_get_commits_scoped_segment_safe(tmp_path: Path) -> None:
    _init(tmp_path)
    _commit(tmp_path, "apps/web/page.tsx", "a", "web: page")
    _commit(tmp_path, "apps/web-extra/x.ts", "b", "extra: x")

    repo = load_repo(str(tmp_path))

    web = get_commits(repo, days=3650, src="apps/web")
    assert len(web) == 1
    assert web[0].message == "web: page"
