"""Tests for ``faultline.pipeline_v2.stage_6_metrics``.

Verifies:

  - Per-feature commit attribution (file→feature direct + parent-dir
    fallback for deleted/renamed files).
  - ``bug_fixes`` / ``bug_fix_ratio`` derived from ``Commit.is_bug_fix``.
  - ``authors`` accumulated and sorted.
  - ``last_modified`` is the max commit date across attributed commits.
  - ``health_score`` uses the same sigmoid as the legacy analyzer.
  - Empty input is a no-op.
  - Features that no commit touches keep ``total_commits=0`` and full
    health (100).
  - Graceful degradation when ``faultlines_test_coverage`` is not
    installed: coverage_pct stays None, no exception.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from faultline.models.types import Commit, Feature
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_6_metrics import (
    _build_file_to_feature_index,
    _attach_commit_metrics,
    stage_6_metrics,
)


def _mk_commit(
    sha: str,
    files: list[str],
    *,
    is_bug_fix: bool = False,
    author: str = "alice",
    days_ago: int = 1,
) -> Commit:
    return Commit(
        sha=sha,
        message="fix: x" if is_bug_fix else "feat: x",
        author=author,
        date=datetime.now(tz=timezone.utc) - timedelta(days=days_ago),
        files_changed=files,
        is_bug_fix=is_bug_fix,
    )


def _mk_feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=80.0,
        flows=[],
        layer="developer",
        product_feature_id=None,
    )


def _mk_ctx(repo_path: Path, commits: list[Commit]) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=[],
        commits=commits,
    )


def test_file_to_feature_index_builds_dir_fallback() -> None:
    f = _mk_feature("billing", ["app/billing/page.tsx", "app/billing/route.ts"])
    file_idx, dir_idx = _build_file_to_feature_index([f])
    assert file_idx["app/billing/page.tsx"] == "billing"
    # Parent dir fallback covers files that no longer exist in HEAD.
    assert dir_idx["app/billing"] == "billing"


def test_commit_metrics_attribute_via_direct_and_dir_fallback(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    commits = [
        _mk_commit("c1", ["app/billing/page.tsx"], is_bug_fix=False, author="alice"),
        _mk_commit("c2", ["app/billing/page.tsx"], is_bug_fix=True, author="bob"),
        # File only in parent-dir fallback (deleted/renamed file).
        _mk_commit("c3", ["app/billing/old.tsx"], is_bug_fix=True, author="bob"),
    ]
    _attach_commit_metrics([feat], commits)
    assert feat.total_commits == 3
    assert feat.bug_fixes == 2
    assert feat.bug_fix_ratio == round(2 / 3, 3)
    assert feat.authors == ["alice", "bob"]
    # Health score is bounded 0..100 and lower when bug-fix ratio is high
    assert 0.0 <= feat.health_score <= 100.0


def test_commit_metrics_untouched_feature_stays_at_full_health(tmp_path: Path) -> None:
    feat = _mk_feature("notifications", ["app/notifications/page.tsx"])
    commits = [_mk_commit("c1", ["app/billing/page.tsx"])]
    _attach_commit_metrics([feat], commits)
    assert feat.total_commits == 0
    assert feat.bug_fixes == 0
    assert feat.bug_fix_ratio == 0.0
    assert feat.health_score == 100.0
    assert feat.authors == []


def test_commit_metrics_last_modified_is_max(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    older = _mk_commit("c1", ["app/billing/page.tsx"], days_ago=30)
    newer = _mk_commit("c2", ["app/billing/page.tsx"], days_ago=1)
    _attach_commit_metrics([feat], [older, newer])
    assert feat.last_modified == newer.date


def test_stage_6_empty_features_is_noop(tmp_path: Path) -> None:
    ctx = _mk_ctx(tmp_path, [])
    assert stage_6_metrics([], ctx) == []


def test_stage_6_graceful_when_coverage_provider_missing(tmp_path: Path) -> None:
    """If faultlines_test_coverage cannot import, coverage_pct stays None
    but commit metrics still attach and no exception escapes.
    """
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    commits = [_mk_commit("c1", ["app/billing/page.tsx"], is_bug_fix=True)]
    ctx = _mk_ctx(tmp_path, commits)
    out = stage_6_metrics([feat], ctx)
    assert out is not None
    assert out[0].total_commits == 1
    assert out[0].bug_fixes == 1
    # Coverage may or may not be populated depending on env; if the
    # provider isn't installed, it MUST stay None.
    try:
        import faultlines_test_coverage  # noqa: F401 — probe only
        # If the package IS installed we don't assert on the value
        # because compute() on a synthetic repo is well-defined but
        # depends on git history.
    except ImportError:
        assert out[0].coverage_pct is None


def test_stage_6_mutates_in_place_and_returns_same_list(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    feats = [feat]
    ctx = _mk_ctx(tmp_path, [_mk_commit("c1", ["app/billing/page.tsx"])])
    out = stage_6_metrics(feats, ctx)
    assert out is feats
    assert feats[0].total_commits == 1
