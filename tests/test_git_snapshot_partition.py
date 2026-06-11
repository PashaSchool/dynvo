"""Equivalence gate for the shared single git pass (multi-subpath Phase 1).

The hard requirement: ``partition_snapshot(fetch_git_snapshot(...), sp)``
must reproduce EXACTLY what the legacy per-subpath calls return today —
``get_tracked_files(repo, src=sp)`` (+ Stage 0's relativization) and
``get_commits(repo, days, src=sp)`` — field by field, order-sensitive.

Fixture monorepo: 3 subdirs (two of them with nested files), several
commits touching different subsets, one commit touching TWO subpaths,
one renamed file, one feature-branch merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from faultline.analyzer.git import (
    get_commits,
    get_tracked_files,
    load_repo,
    scope_files_to_subpath,
)
from faultline.pipeline_v2.git_snapshot import (
    GitSnapshot,
    SnapshotNotPartitionable,
    fetch_git_snapshot,
    partition_snapshot,
)

DAYS = 3650
SUBPATHS = ("apps/web", "apps/worker", "packages/shared")


# ── Fixture repo ─────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True,
    )


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit_files(root: Path, files: dict[str, str], msg: str) -> None:
    for rel, content in files.items():
        _write(root, rel, content)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", msg)


@pytest.fixture()
def monorepo(tmp_path: Path) -> Path:
    """Git monorepo with 3 subtrees + cross-subpath commit + rename + merge."""
    root = tmp_path / "mono"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")

    _commit_files(
        root,
        {
            "package.json": '{"name": "mono"}',
            "apps/web/app/page.tsx": "export default 1\n",
            "apps/web/lib/util.ts": "export const a = 1\n",
        },
        "feat: web scaffold",
    )
    _commit_files(
        root,
        {"apps/worker/src/index.ts": "console.log(1)\n"},
        "feat: worker entry",
    )
    _commit_files(
        root,
        {"packages/shared/src/types.ts": "export type X = 1\n"},
        "feat: shared types",
    )
    # Commit touching TWO subpaths at once.
    _commit_files(
        root,
        {
            "apps/web/app/page.tsx": "export default 2\n",
            "apps/worker/src/index.ts": "console.log(2)\n",
        },
        "fix: web+worker sync",
    )
    # Renamed file inside apps/web.
    _git(root, "mv", "apps/web/lib/util.ts", "apps/web/lib/helpers.ts")
    _git(root, "commit", "-m", "refactor: rename util to helpers")
    # Feature-branch merge touching packages/shared.
    _git(root, "checkout", "-q", "-b", "feature/shared")
    _commit_files(
        root,
        {"packages/shared/src/extra.ts": "export const y = 2\n"},
        "feat: shared extra",
    )
    _git(root, "checkout", "-q", "main")
    _commit_files(
        root,
        {"apps/web/app/layout.tsx": "export default 3\n"},
        "feat: web layout",
    )
    _git(root, "merge", "--no-ff", "-m", "merge: feature/shared", "feature/shared")
    return root


# ── Core equivalence gate ────────────────────────────────────────────


@pytest.mark.parametrize("sp", SUBPATHS)
def test_partition_equals_legacy_scoped_calls(monorepo: Path, sp: str) -> None:
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    view = partition_snapshot(snapshot, sp)

    repo = load_repo(str(monorepo))

    # Tracked files: legacy scoped call + Stage 0's relativization pass.
    legacy_tracked = scope_files_to_subpath(
        get_tracked_files(repo, src=sp), sp,
    )
    assert list(view.tracked_files) == legacy_tracked

    # Commits: strict order-sensitive, field-by-field (pydantic value
    # equality covers sha/message/author/date/files_changed/is_bug_fix/
    # pr_number).
    legacy_commits = get_commits(repo, days=DAYS, src=sp)
    assert list(view.commits) == legacy_commits


def test_partition_drops_commits_outside_subpath(monorepo: Path) -> None:
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    web = partition_snapshot(snapshot, "apps/web")
    msgs = [c.message for c in web.commits]
    assert "feat: worker entry" not in msgs
    assert "feat: shared types" not in msgs
    # Cross-subpath commit appears, with only the in-subtree file,
    # relativized.
    cross = [c for c in web.commits if c.message == "fix: web+worker sync"]
    assert len(cross) == 1
    assert cross[0].files_changed == ["app/page.tsx"]


def test_partition_relativizes_all_paths(monorepo: Path) -> None:
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    for sp in SUBPATHS:
        view = partition_snapshot(snapshot, sp)
        prefix = sp + "/"
        for f in view.tracked_files:
            assert not f.startswith(prefix)
        for c in view.commits:
            assert c.files_changed, "empty commits must be dropped"
            for f in c.files_changed:
                assert not f.startswith(prefix)


def test_partition_does_not_mutate_snapshot(monorepo: Path) -> None:
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    before = [list(c.files_changed) for c in snapshot.commits]
    partition_snapshot(snapshot, "apps/web")
    after = [list(c.files_changed) for c in snapshot.commits]
    assert before == after


def test_partition_segment_safe(monorepo: Path) -> None:
    # apps/web must not leak into a hypothetical sibling prefix match.
    _commit_files(
        monorepo,
        {"apps/web-extra/x.ts": "export {}\n"},
        "feat: web-extra",
    )
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    view = partition_snapshot(snapshot, "apps/web")
    assert "feat: web-extra" not in [c.message for c in view.commits]
    assert all("web-extra" not in f for f in view.tracked_files)


# ── Guard rails ──────────────────────────────────────────────────────


def test_partition_rejects_empty_subpath(monorepo: Path) -> None:
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    with pytest.raises(ValueError):
        partition_snapshot(snapshot, "")
    with pytest.raises(ValueError):
        partition_snapshot(snapshot, ".")


def test_truncated_snapshot_refuses_to_partition(monorepo: Path) -> None:
    # max_commits below the history size → truncated → documented
    # divergence #1 → SnapshotNotPartitionable (callers fall back to
    # per-subpath git calls).
    snapshot = fetch_git_snapshot(monorepo, days=DAYS, max_commits=2)
    assert snapshot.truncated is True
    with pytest.raises(SnapshotNotPartitionable):
        partition_snapshot(snapshot, "apps/web")


def test_snapshot_is_frozen(monorepo: Path) -> None:
    snapshot = fetch_git_snapshot(monorepo, days=DAYS)
    assert isinstance(snapshot, GitSnapshot)
    with pytest.raises(AttributeError):
        snapshot.days = 1  # type: ignore[misc]
