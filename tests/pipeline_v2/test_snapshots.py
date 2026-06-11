"""Unit tests for the impact-over-time snapshot machinery.

Covers: snapshot selection (even spacing / short history / N >
commits / same-week collapse), budget resolution env overrides, the
worktree runner's robustness contract (per-snapshot failure → skip +
cleanup, budget exceeded → remaining skipped, NO leftover worktrees),
the lean reverse-import resolver (TS relative / tsconfig alias /
workspace package / Python absolute + relative), reach correctness on
a crafted fixture, and determinism.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from faultline.models.types import Commit
from faultline.pipeline_v2.snapshots import (
    DEFAULT_PER_SNAPSHOT_BUDGET_SEC,
    build_snapshot_import_index,
    impact_reach,
    list_snapshot_files,
    percentile,
    resolve_snapshot_budget_sec,
    run_snapshots,
    select_snapshot_commits,
)


def _commit(sha: str, day: int, *, week_offset: int = 0) -> Commit:
    base = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
    return Commit(
        sha=sha,
        message="feat: x",
        author="dev",
        date=base + timedelta(weeks=week_offset, days=day % 5),
        files_changed=["a.ts"],
    )


# ── Snapshot selection ──────────────────────────────────────────────────


def test_selection_even_spacing_includes_endpoints() -> None:
    commits = [_commit(f"s{i:02d}", 0, week_offset=i) for i in range(20)]
    picked = select_snapshot_commits(commits, n=8)
    assert len(picked) == 8
    assert picked[0].sha == "s00"
    assert picked[-1].sha == "s19"
    # Ascending by date, roughly evenly spaced.
    idxs = [int(c.sha[1:]) for c in picked]
    assert idxs == sorted(idxs)
    gaps = [b - a for a, b in zip(idxs, idxs[1:])]
    assert max(gaps) - min(gaps) <= 1


def test_selection_short_history_keeps_all() -> None:
    commits = [_commit(f"s{i}", 0, week_offset=i) for i in range(3)]
    picked = select_snapshot_commits(commits, n=8)
    assert [c.sha for c in picked] == ["s0", "s1", "s2"]


def test_selection_input_order_independent() -> None:
    commits = [_commit(f"s{i:02d}", 0, week_offset=i) for i in range(15)]
    a = select_snapshot_commits(commits, n=6)
    b = select_snapshot_commits(list(reversed(commits)), n=6)
    assert [c.sha for c in a] == [c.sha for c in b]


def test_selection_same_week_collapses_to_latest() -> None:
    # Three commits in the same ISO week, n large enough to pick all.
    commits = [
        _commit("a", 0),
        _commit("b", 1),
        _commit("c", 2),
        _commit("d", 0, week_offset=4),
    ]
    picked = select_snapshot_commits(commits, n=8)
    assert [c.sha for c in picked] == ["c", "d"]


def test_selection_empty_and_zero_n() -> None:
    assert select_snapshot_commits([], n=8) == []
    assert select_snapshot_commits([_commit("a", 0)], n=0) == []


# ── Budget resolution ───────────────────────────────────────────────────


def test_budget_default_scales_with_snapshot_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAULTLINE_IMPACT_BUDGET_SEC", raising=False)
    monkeypatch.delenv("FAULTLINE_IMPACT_PER_SNAPSHOT_SEC", raising=False)
    assert resolve_snapshot_budget_sec(8) == DEFAULT_PER_SNAPSHOT_BUDGET_SEC * 8
    # Floor at one work-unit so n=0 never yields a zero (=disabled) wall.
    assert resolve_snapshot_budget_sec(0) == DEFAULT_PER_SNAPSHOT_BUDGET_SEC


def test_budget_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_IMPACT_PER_SNAPSHOT_SEC", "2.5")
    assert resolve_snapshot_budget_sec(4) == 10.0
    monkeypatch.setenv("FAULTLINE_IMPACT_BUDGET_SEC", "123")
    assert resolve_snapshot_budget_sec(4) == 123.0
    monkeypatch.setenv("FAULTLINE_IMPACT_BUDGET_SEC", "0")
    assert resolve_snapshot_budget_sec(4) == 0.0


def test_budget_env_non_numeric_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_IMPACT_BUDGET_SEC", "lots")
    monkeypatch.setenv("FAULTLINE_IMPACT_PER_SNAPSHOT_SEC", "many")
    assert resolve_snapshot_budget_sec(2) == DEFAULT_PER_SNAPSHOT_BUDGET_SEC * 2


# ── Worktree runner ─────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A real repo with 3 commits; returns (path, shas oldest-first)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    shas: list[str] = []
    for i in range(3):
        (repo / "counter.txt").write_text(f"v{i}\n")
        _git(repo, "add", "-A")
        _git(
            repo, "-c", f"user.name=t", "commit", "-q", "-m", f"c{i}",
            "--date", f"2024-01-0{i + 1}T12:00:00Z",
        )
        shas.append(_git(repo, "rev-parse", "HEAD").strip())
    return repo, shas


def _no_leftover_worktrees(repo: Path) -> bool:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return listing.count("worktree ") == 1  # only the main checkout


def test_runner_computes_per_snapshot_and_cleans_up(
    git_repo: tuple[Path, list[str]],
) -> None:
    repo, shas = git_repo
    results, telem = run_snapshots(
        repo, shas, lambda root, sha: (root / "counter.txt").read_text(),
    )
    assert {sha: r.strip() for sha, r in results.items()} == {
        shas[0]: "v0", shas[1]: "v1", shas[2]: "v2",
    }
    assert telem["impact_snapshots"] == 3
    assert telem["impact_skipped_snapshots"] == 0
    assert telem["impact_budget_exceeded"] is False
    assert _no_leftover_worktrees(repo)


def test_runner_skips_failing_snapshot_and_keeps_going(
    git_repo: tuple[Path, list[str]],
) -> None:
    repo, shas = git_repo

    def compute(root: Path, sha: str) -> str:
        if sha == shas[1]:
            raise RuntimeError("boom")
        return "ok"

    results, telem = run_snapshots(repo, shas, compute)
    assert set(results) == {shas[0], shas[2]}
    assert telem["impact_snapshots"] == 2
    assert telem["impact_skipped_snapshots"] == 1
    assert _no_leftover_worktrees(repo)


def test_runner_skips_unknown_sha(git_repo: tuple[Path, list[str]]) -> None:
    repo, shas = git_repo
    bad = "deadbeef" * 5
    results, telem = run_snapshots(repo, [bad, shas[0]], lambda r, s: "ok")
    assert set(results) == {shas[0]}
    assert telem["impact_skipped_snapshots"] == 1
    assert _no_leftover_worktrees(repo)


def test_runner_budget_exceeded_skips_remaining(
    git_repo: tuple[Path, list[str]],
) -> None:
    repo, shas = git_repo

    def slow(root: Path, sha: str) -> str:
        time.sleep(0.05)
        return "ok"

    results, telem = run_snapshots(repo, shas, slow, budget_sec=0.01)
    assert telem["impact_budget_exceeded"] is True
    assert telem["impact_snapshots"] >= 1          # first one always runs
    assert telem["impact_skipped_snapshots"] >= 1  # the rest skipped
    assert (
        telem["impact_snapshots"] + telem["impact_skipped_snapshots"]
        == len(shas)
    )
    assert _no_leftover_worktrees(repo)


def test_runner_on_non_repo_degrades_to_all_skipped(tmp_path: Path) -> None:
    results, telem = run_snapshots(tmp_path, ["abc123"], lambda r, s: "ok")
    assert results == {}
    assert telem["impact_snapshots"] == 0
    assert telem["impact_skipped_snapshots"] == 1


def test_runner_forwards_messages_to_stage_logger(
    git_repo: tuple[Path, list[str]],
) -> None:
    repo, shas = git_repo
    messages: list[str] = []

    class FakeLog:
        def info(self, msg: str) -> None:
            messages.append(msg)

    def compute(root: Path, sha: str) -> str:
        if sha == shas[0]:
            raise RuntimeError("boom")
        return "ok"

    run_snapshots(repo, shas, compute, log=FakeLog())
    assert any(m.startswith("WARN") for m in messages)      # skip warning
    assert any("snapshots done" in m for m in messages)     # summary


def test_runner_no_shas_is_noop(git_repo: tuple[Path, list[str]]) -> None:
    repo, _ = git_repo
    results, telem = run_snapshots(repo, [], lambda r, s: "ok")
    assert results == {}
    assert telem["planned_snapshots"] == 0


# ── File listing ────────────────────────────────────────────────────────


def test_list_snapshot_files_skips_git_and_symlinks(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "b.py").write_text("x")
    os.symlink(tmp_path / "b.py", tmp_path / "link.py")
    assert list_snapshot_files(tmp_path) == ["b.py", "src/a.ts"]


# ── Lean resolver + reach ───────────────────────────────────────────────


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_reach_relative_import_two_external_one_member(tmp_path: Path) -> None:
    """Crafted fixture: member imported by 2 external files → reach 2;
    member-to-member import never counts."""
    _write(tmp_path, "src/billing/core.ts", "export const x = 1\n")
    _write(tmp_path, "src/billing/ui.ts", "import { x } from './core'\n")
    _write(tmp_path, "src/app/page.ts", "import { x } from '../billing/core'\n")
    _write(tmp_path, "src/admin/panel.ts",
           "const m = require('../billing/core')\n")
    _write(tmp_path, "src/other/none.ts", "import { y } from './nothing'\n")
    index = build_snapshot_import_index(tmp_path)
    members = ["src/billing/core.ts", "src/billing/ui.ts"]
    reach, present = impact_reach(members, index)
    assert reach == 2          # page.ts + panel.ts; ui.ts is a member
    assert present == 2


def test_reach_counts_each_importer_once_across_members(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "lib/a.ts", "export const a = 1\n")
    _write(tmp_path, "lib/b.ts", "export const b = 1\n")
    _write(
        tmp_path, "src/uses-both.ts",
        "import { a } from '../lib/a'\nimport { b } from '../lib/b'\n",
    )
    index = build_snapshot_import_index(tmp_path)
    reach, present = impact_reach(["lib/a.ts", "lib/b.ts"], index)
    assert reach == 1
    assert present == 2


def test_reach_missing_member_counts_absent(tmp_path: Path) -> None:
    _write(tmp_path, "lib/a.ts", "export const a = 1\n")
    index = build_snapshot_import_index(tmp_path)
    reach, present = impact_reach(["lib/a.ts", "lib/not-yet-born.ts"], index)
    assert reach == 0
    assert present == 1


def test_resolver_tsconfig_alias(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}},
    }))
    _write(tmp_path, "src/lib/util.ts", "export const u = 1\n")
    _write(tmp_path, "src/app/page.ts", "import { u } from '@/lib/util'\n")
    index = build_snapshot_import_index(tmp_path)
    reach, present = impact_reach(["src/lib/util.ts"], index)
    assert reach == 1
    assert present == 1


def test_resolver_workspace_scoped_package(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"workspaces": ["packages/*"]}),
    )
    _write(
        tmp_path, "packages/i18n/package.json",
        json.dumps({"name": "@acme/i18n", "main": "index.ts"}),
    )
    _write(tmp_path, "packages/i18n/index.ts", "export const t = 1\n")
    _write(tmp_path, "apps/web/page.ts", "import { t } from '@acme/i18n'\n")
    index = build_snapshot_import_index(tmp_path)
    reach, present = impact_reach(["packages/i18n/index.ts"], index)
    assert reach == 1
    assert present == 1


def test_resolver_python_absolute_and_relative(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py", "X = 1\n")
    _write(tmp_path, "pkg/sibling.py", "from .core import X\n")
    _write(tmp_path, "tool.py", "import pkg.core\n")
    _write(tmp_path, "other.py", "from pkg.core import X\n")
    index = build_snapshot_import_index(tmp_path)
    reach, present = impact_reach(["pkg/core.py"], index)
    # sibling.py (relative), tool.py + other.py (absolute) — all external.
    assert reach == 3
    assert present == 1


def test_resolver_python_src_layout_and_bare_package(tmp_path: Path) -> None:
    _write(tmp_path, "src/mylib/__init__.py", "")
    _write(tmp_path, "src/mylib/api.py", "def f(): ...\n")
    _write(tmp_path, "scripts/run.py", "from mylib.api import f\n")
    _write(tmp_path, "src/mylib/glue.py", "from . import api\n")
    index = build_snapshot_import_index(tmp_path)
    reach, _present = impact_reach(["src/mylib/api.py"], index)
    assert reach == 1  # scripts/run.py via the src/ root
    reach_init, _ = impact_reach(["src/mylib/__init__.py"], index)
    assert reach_init == 1  # glue.py's `from . import api`


def test_resolver_python_two_dot_relative(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/core.py", "X = 1\n")
    _write(tmp_path, "pkg/sub/deep.py", "from ..core import X\n")
    index = build_snapshot_import_index(tmp_path)
    reach, _ = impact_reach(["pkg/core.py"], index)
    assert reach == 1


def test_resolver_python_root_level_dot_import_is_noop(
    tmp_path: Path,
) -> None:
    from faultline.pipeline_v2.snapshots import _python_candidates

    # ``from . import x`` in a ROOT-level file has no package dir.
    assert _python_candidates("main.py", ".") == []
    _write(tmp_path, "main.py", "from . import x\n")
    _write(tmp_path, "x.py", "y = 1\n")
    index = build_snapshot_import_index(tmp_path)
    reach, _ = impact_reach(["x.py"], index)
    assert reach == 0


def test_unreadable_file_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "lib/a.ts", "export const a = 1\n")
    _write(tmp_path, "locked.ts", "import { a } from './lib/a'\n")
    (tmp_path / "locked.ts").chmod(0o000)
    try:
        index = build_snapshot_import_index(tmp_path)
    finally:
        (tmp_path / "locked.ts").chmod(0o644)
    reach, _ = impact_reach(["lib/a.ts"], index)
    assert reach == 0


def test_vendor_and_test_importers_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "lib/core.ts", "export const x = 1\n")
    _write(tmp_path, "node_modules/dep/index.ts",
           "import { x } from '../../lib/core'\n")
    _write(tmp_path, "tests/core.test.ts",
           "import { x } from '../lib/core'\n")
    _write(tmp_path, "src/real.ts", "import { x } from '../lib/core'\n")
    index = build_snapshot_import_index(tmp_path)
    reach, _ = impact_reach(["lib/core.ts"], index)
    assert reach == 1  # only src/real.ts


def test_index_is_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, "lib/a.ts", "export const a = 1\n")
    _write(tmp_path, "x.ts", "import { a } from './lib/a'\n")
    _write(tmp_path, "y.py", "import lib\n")
    one = build_snapshot_import_index(tmp_path)
    two = build_snapshot_import_index(tmp_path)
    assert one.files == two.files
    assert one.importers_of == two.importers_of


def test_oversized_file_skipped_as_importer(tmp_path: Path) -> None:
    _write(tmp_path, "lib/a.ts", "export const a = 1\n")
    big = "import { a } from './lib/a'\n" + ("//x\n" * 1_000_000)
    _write(tmp_path, "bundle.ts", big)
    index = build_snapshot_import_index(tmp_path)
    reach, _ = impact_reach(["lib/a.ts"], index)
    assert reach == 0


# ── percentile helper ───────────────────────────────────────────────────


def test_percentile_interpolation() -> None:
    assert percentile([1.0], 0.9) == 1.0
    assert percentile([1.0, 3.0], 0.5) == 2.0
    assert percentile([0.0, 10.0, 20.0], 0.9) == pytest.approx(18.0)
    assert percentile([5.0, 1.0, 3.0], 0.5) == 3.0  # order-independent
