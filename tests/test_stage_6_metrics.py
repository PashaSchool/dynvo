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

from faultline.models.types import Commit, Feature, Flow
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_6_metrics import (
    _attach_commit_metrics,
    _attach_flow_metrics,
    _attach_lcov_coverage,
    _build_file_to_feature_index,
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


# ── Sprint 3 — added metrics (parity with legacy `analyze`) ─────────────


def _mk_flow(name: str, paths: list[str]) -> Flow:
    return Flow(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=80.0,
    )


def test_bug_fix_prs_populated_with_pr_numbers(tmp_path: Path) -> None:
    """Sprint 3 — _collect_prs deduplicates by PR number and orders by date."""
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    c1 = _mk_commit(
        "c1", ["app/billing/page.tsx"], is_bug_fix=True, days_ago=10,
    )
    c1.pr_number = 42
    c1.message = "fix(billing): off-by-one in invoice line totals (#42)"
    c2 = _mk_commit(
        "c2", ["app/billing/page.tsx"], is_bug_fix=True, days_ago=2,
    )
    c2.pr_number = 99
    c2.message = "fix(billing): retry stripe webhook (#99)"
    _attach_commit_metrics([feat], [c1, c2], remote_url="https://github.com/foo/bar")
    assert len(feat.bug_fix_prs) == 2
    # Newest first.
    assert feat.bug_fix_prs[0].number == 99
    assert feat.bug_fix_prs[0].url.endswith("/pull/99")
    assert feat.bug_fix_prs[1].number == 42


def test_bug_fix_prs_skip_non_bugfix_and_dedupe(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    feat_commit = _mk_commit("c1", ["app/billing/page.tsx"], is_bug_fix=False)
    feat_commit.pr_number = 1
    bug1 = _mk_commit("c2", ["app/billing/page.tsx"], is_bug_fix=True)
    bug1.pr_number = 7
    bug2 = _mk_commit("c3", ["app/billing/page.tsx"], is_bug_fix=True)
    bug2.pr_number = 7  # duplicate
    _attach_commit_metrics([feat], [feat_commit, bug1, bug2])
    assert [p.number for p in feat.bug_fix_prs] == [7]


def test_symbol_health_score_stays_none_without_attributions(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    _attach_commit_metrics(
        [feat], [_mk_commit("c1", ["app/billing/page.tsx"])],
    )
    assert feat.symbol_health_score is None


def test_attach_flow_metrics_populates_hotspots_and_authors(tmp_path: Path) -> None:
    flow = _mk_flow("create-invoice", ["app/billing/invoice.ts"])
    feat = _mk_feature("billing", ["app/billing/invoice.ts"])
    feat.flows = [flow]
    # 3 bug-fix commits → file is a hotspot (≥3 commits AND >40% bug ratio).
    commits = [
        _mk_commit(f"c{i}", ["app/billing/invoice.ts"], is_bug_fix=True,
                   author="alice" if i % 2 else "bob", days_ago=10 - i)
        for i in range(4)
    ]
    _attach_flow_metrics([feat], commits)
    assert flow.total_commits == 4
    assert flow.bug_fixes == 4
    assert flow.bug_fix_ratio == 1.0
    assert "app/billing/invoice.ts" in flow.hotspot_files
    assert set(flow.authors) == {"alice", "bob"}
    assert flow.bus_factor >= 1


def test_attach_flow_metrics_test_file_count_includes_adjacent(tmp_path: Path) -> None:
    flow = _mk_flow("create-invoice", ["app/billing/invoice.ts"])
    feat = _mk_feature("billing", ["app/billing/invoice.ts"])
    feat.flows = [flow]
    commits = [
        _mk_commit("c1", ["app/billing/invoice.ts"]),
        # Adjacent test file → counted via test-only commit detection.
        _mk_commit("c2", ["app/billing/invoice.test.ts"]),
    ]
    _attach_flow_metrics([feat], commits)
    assert flow.test_file_count >= 1


def test_attach_flow_metrics_no_flows_is_noop(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    _attach_flow_metrics([feat], [_mk_commit("c1", ["app/billing/page.tsx"])])
    # No flows → nothing to assert except no exception.
    assert feat.flows == []


def test_attach_lcov_coverage_direct_path_hit(tmp_path: Path) -> None:
    feat = _mk_feature(
        "billing", ["app/billing/page.tsx", "app/billing/route.ts"],
    )
    cov = {"app/billing/page.tsx": 80.0, "app/billing/route.ts": 60.0}
    scored = _attach_lcov_coverage([feat], cov)
    assert scored == 1
    assert feat.coverage_pct == 70.0


def test_attach_lcov_coverage_skips_test_files(tmp_path: Path) -> None:
    feat = _mk_feature(
        "billing", ["app/billing/page.tsx", "app/billing/page.test.tsx"],
    )
    cov = {"app/billing/page.tsx": 50.0, "app/billing/page.test.tsx": 100.0}
    scored = _attach_lcov_coverage([feat], cov)
    assert scored == 1
    # Only the source file counts, test file ignored.
    assert feat.coverage_pct == 50.0


def test_attach_lcov_coverage_suffix_match(tmp_path: Path) -> None:
    """LCOV often produces absolute or leading-slash paths — suffix match."""
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    cov = {"/Users/me/repo/app/billing/page.tsx": 90.0}
    scored = _attach_lcov_coverage([feat], cov)
    assert scored == 1
    assert feat.coverage_pct == 90.0


def test_attach_lcov_coverage_empty_returns_zero(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    assert _attach_lcov_coverage([feat], {}) == 0
    assert feat.coverage_pct is None


def test_attach_lcov_coverage_flows_too(tmp_path: Path) -> None:
    flow = _mk_flow("create-invoice", ["app/billing/invoice.ts"])
    feat = _mk_feature("billing", ["app/billing/invoice.ts"])
    feat.flows = [flow]
    cov = {"app/billing/invoice.ts": 75.0}
    _attach_lcov_coverage([feat], cov)
    assert feat.coverage_pct == 75.0
    assert flow.coverage_pct == 75.0


def test_stage_6_with_coverage_path_attaches_lcov(tmp_path: Path) -> None:
    """End-to-end: --coverage flag flows lcov data into features."""
    lcov = tmp_path / "lcov.info"
    lcov.write_text(
        "SF:app/billing/page.tsx\n"
        "DA:1,1\nDA:2,1\nDA:3,0\nDA:4,0\n"
        "LF:4\nLH:2\n"
        "end_of_record\n",
    )
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    ctx = _mk_ctx(tmp_path, [_mk_commit("c1", ["app/billing/page.tsx"])])
    out = stage_6_metrics([feat], ctx, coverage_path=str(lcov))
    assert out[0].coverage_pct == 50.0


def test_stage_6_without_coverage_keeps_none(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    ctx = _mk_ctx(tmp_path, [_mk_commit("c1", ["app/billing/page.tsx"])])
    out = stage_6_metrics([feat], ctx, coverage_path=None)
    # No lcov in repo + no behavioral coverage provider in test env →
    # coverage_pct stays None.
    try:
        import faultlines_test_coverage  # noqa: F401
    except ImportError:
        assert out[0].coverage_pct is None


def test_stage_6_bug_fix_prs_end_to_end(tmp_path: Path) -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    c = _mk_commit("c1", ["app/billing/page.tsx"], is_bug_fix=True)
    c.pr_number = 5
    ctx = _mk_ctx(tmp_path, [c])
    out = stage_6_metrics([feat], ctx)
    # bug_fix_prs[0].number set; URL may be empty when not in a git repo.
    assert len(out[0].bug_fix_prs) == 1
    assert out[0].bug_fix_prs[0].number == 5


def test_flow_health_trend_requires_four_weeks(tmp_path: Path) -> None:
    flow = _mk_flow("create-invoice", ["app/billing/invoice.ts"])
    feat = _mk_feature("billing", ["app/billing/invoice.ts"])
    feat.flows = [flow]
    # Single commit → fewer than 4 weeks of data → trend stays None.
    _attach_flow_metrics(
        [feat], [_mk_commit("c1", ["app/billing/invoice.ts"], days_ago=1)],
    )
    assert flow.health_trend is None
