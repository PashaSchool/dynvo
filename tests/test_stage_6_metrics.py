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
from faultline.models.types import Flow
from faultline.pipeline_v2.stage_6_metrics import (
    HOTSPOT_BUG_RATIO_MIN,
    HOTSPOT_COMMITS_MIN,
    _attach_commit_metrics,
    _attach_hotspots,
    _build_file_to_feature_index,
    _build_path_commit_index,
    _hotspots_from_paths,
    attach_hotspots_to_product_features,
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
    # 2026-06 metric-honesty review: index now returns a third element
    # (ambiguous dirs) — unambiguous dirs still map as before.
    f = _mk_feature("billing", ["app/billing/page.tsx", "app/billing/route.ts"])
    file_idx, dir_idx, ambiguous = _build_file_to_feature_index([f])
    assert file_idx["app/billing/page.tsx"] == "billing"
    # Parent dir fallback covers files that no longer exist in HEAD.
    assert dir_idx["app/billing"] == "billing"
    assert ambiguous == set()


def test_file_to_feature_index_shared_dir_is_ambiguous() -> None:
    # 2026-06 metric-honesty review: a dir owned by 2+ features must
    # NOT enter the fallback map (was setdefault → first feature won).
    a = _mk_feature("invoices", ["app/billing/invoices.tsx"])
    b = _mk_feature("payments", ["app/billing/payments.tsx"])
    file_idx, dir_idx, ambiguous = _build_file_to_feature_index([a, b])
    assert file_idx["app/billing/invoices.tsx"] == "invoices"
    assert file_idx["app/billing/payments.tsx"] == "payments"
    assert "app/billing" not in dir_idx
    assert ambiguous == {"app/billing"}


def test_commit_metrics_shared_dir_no_cross_attribution() -> None:
    # Two features share app/billing. A commit touching a deleted file
    # in that dir must not be attributed to EITHER feature (2026-06
    # metric-honesty review: previously the first-registered feature
    # absorbed all such commits, inflating its counters).
    a = _mk_feature("invoices", ["app/billing/invoices.tsx"])
    b = _mk_feature("payments", ["app/billing/payments.tsx"])
    commits = [
        _mk_commit("c1", ["app/billing/invoices.tsx"], author="alice"),
        _mk_commit("c2", ["app/billing/payments.tsx"], author="bob"),
        # Deleted file — dir is ambiguous, fallback must be skipped.
        _mk_commit("c3", ["app/billing/old.tsx"], is_bug_fix=True),
    ]
    _attach_commit_metrics([a, b], commits)
    assert a.total_commits == 1  # exact match only
    assert b.total_commits == 1  # exact match only
    assert a.bug_fixes == 0
    assert b.bug_fixes == 0


def test_commit_metrics_unambiguous_dir_still_attributes() -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    commits = [_mk_commit("c1", ["app/billing/deleted.tsx"])]
    _attach_commit_metrics([feat], commits)
    assert feat.total_commits == 1


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
    # 2026-06 metric-honesty review: the 100.0 placeholder must be
    # flagged as evidence-free.
    assert feat.health_confidence == "insufficient"


def test_health_confidence_tiers() -> None:
    # Floor = nearest-rank P25 of NONZERO per-feature commit counts.
    # Counts here: [1, 5, 5, 5, 5] → P25 = 5 → the 1-commit feature is
    # below the floor ("low"), the 5-commit features are "high", and a
    # zero-commit feature is "insufficient".
    sparse = _mk_feature("sparse", ["app/sparse/page.tsx"])
    untouched = _mk_feature("untouched", ["app/untouched/page.tsx"])
    active = [
        _mk_feature(f"active{i}", [f"app/active{i}/page.tsx"])
        for i in range(4)
    ]
    commits = [_mk_commit("s1", ["app/sparse/page.tsx"])]
    for i in range(4):
        commits.extend(
            _mk_commit(f"a{i}{j}", [f"app/active{i}/page.tsx"])
            for j in range(5)
        )
    _attach_commit_metrics([sparse, untouched, *active], commits)
    assert untouched.health_confidence == "insufficient"
    assert sparse.health_confidence == "low"
    assert all(f.health_confidence == "high" for f in active)


def test_health_confidence_single_active_feature_is_high() -> None:
    feat = _mk_feature("billing", ["app/billing/page.tsx"])
    _attach_commit_metrics([feat], [_mk_commit("c1", ["app/billing/page.tsx"])])
    # Only nonzero count IS the P25 → at the floor → high.
    assert feat.health_confidence == "high"


def test_health_confidence_rehydrates_old_json_with_default() -> None:
    # Scans serialized before health_confidence existed must rehydrate
    # with the conservative default.
    payload = _mk_feature("legacy", ["src/a.py"]).model_dump(mode="json")
    payload.pop("health_confidence")
    feat = Feature.model_validate(payload)
    assert feat.health_confidence == "low"


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


# ── Hotspot tests (Sprint 2026-05-28) ────────────────────────────────────


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


def _bug_commits(path: str, n_bugs: int, n_other: int, sha_prefix: str) -> list[Commit]:
    commits: list[Commit] = []
    for i in range(n_bugs):
        commits.append(_mk_commit(f"{sha_prefix}b{i}", [path], is_bug_fix=True, days_ago=i + 1))
    for i in range(n_other):
        commits.append(_mk_commit(f"{sha_prefix}o{i}", [path], is_bug_fix=False, days_ago=100 + i))
    return commits


def test_hotspot_thresholds_are_scale_invariant_constants() -> None:
    """Universal thresholds — ratio + minimum sample size."""
    assert 0.0 < HOTSPOT_BUG_RATIO_MIN < 1.0
    assert HOTSPOT_COMMITS_MIN >= 3  # statistical floor


def test_feature_with_no_hotspots_emits_empty_list() -> None:
    """Low bug_fix_ratio across all files → empty hotspot_files."""
    feat = _mk_feature("billing", ["a.ts", "b.ts"])
    # 1 bug + 9 features per file → ratio = 10%, below 40% threshold
    commits = _bug_commits("a.ts", 1, 9, "A") + _bug_commits("b.ts", 1, 9, "B")
    path_index = _build_path_commit_index(commits)
    assert _hotspots_from_paths(feat.paths, path_index) == []


def test_feature_with_one_qualifying_hotspot() -> None:
    """One file ≥40% ratio AND ≥5 commits → exactly one hotspot."""
    feat = _mk_feature("auth", ["login.ts", "session.ts"])
    # login.ts: 4 bugs / 6 commits = 67% — qualifies (≥5 commits, ≥40%)
    # session.ts: 0 bugs / 6 commits — doesn't
    commits = _bug_commits("login.ts", 4, 2, "L") + _bug_commits("session.ts", 0, 6, "S")
    path_index = _build_path_commit_index(commits)
    hot = _hotspots_from_paths(feat.paths, path_index)
    assert len(hot) == 1
    assert hot[0].path == "login.ts"
    assert hot[0].bug_fixes == 4
    assert hot[0].total_commits == 6
    assert hot[0].bug_fix_ratio == round(4 / 6, 3)


def test_hotspot_requires_minimum_commits_even_at_ratio_1() -> None:
    """2-of-2 bug-fixes is ratio=1.0 but commits<5 → MUST NOT qualify."""
    feat = _mk_feature("admin", ["risky.ts"])
    commits = _bug_commits("risky.ts", 2, 0, "R")  # ratio 1.0 but only 2 commits
    path_index = _build_path_commit_index(commits)
    assert _hotspots_from_paths(feat.paths, path_index) == []


def test_hotspots_sorted_ratio_desc_then_total_commits_desc() -> None:
    """Higher ratio first; ties broken by total_commits desc."""
    feat = _mk_feature("svc", ["a.ts", "b.ts", "c.ts"])
    # a.ts: 6/10 = 60% (10 commits)
    # b.ts: 8/10 = 80% (10 commits) — highest ratio → first
    # c.ts: 6/8  = 75% (8 commits)
    commits = (
        _bug_commits("a.ts", 6, 4, "A")
        + _bug_commits("b.ts", 8, 2, "B")
        + _bug_commits("c.ts", 6, 2, "C")
    )
    path_index = _build_path_commit_index(commits)
    hot = _hotspots_from_paths(feat.paths, path_index)
    assert [h.path for h in hot] == ["b.ts", "c.ts", "a.ts"]
    # Tie-break check: forge two paths with identical ratio + commits,
    # the secondary key (total_commits desc) must keep order stable;
    # tertiary key is path (alpha) so output is deterministic.
    feat2 = _mk_feature("tied", ["z.ts", "y.ts"])
    tied = _bug_commits("z.ts", 5, 5, "Z") + _bug_commits("y.ts", 5, 5, "Y")
    idx2 = _build_path_commit_index(tied)
    hot2 = _hotspots_from_paths(feat2.paths, idx2)
    # Same ratio + same total_commits → fall through to path alpha.
    assert [h.path for h in hot2] == ["y.ts", "z.ts"]


def test_flow_inherits_parent_feature_paths_when_own_paths_empty(tmp_path: Path) -> None:
    """Flow with empty .paths falls back to parent feature's paths."""
    feat = _mk_feature("auth", ["login.ts"])
    flow = _mk_flow("login-flow", paths=[])  # empty — must fall back
    feat.flows = [flow]
    commits = _bug_commits("login.ts", 4, 2, "L")
    _attach_hotspots([feat], commits)
    assert len(flow.hotspot_files_detail) == 1
    assert flow.hotspot_files_detail[0].path == "login.ts"


def test_attach_hotspots_populates_feature_and_flow() -> None:
    feat = _mk_feature("auth", ["login.ts", "session.ts"])
    flow = _mk_flow("login-flow", ["login.ts"])
    feat.flows = [flow]
    commits = _bug_commits("login.ts", 4, 2, "L")
    feats_hot, flows_hot = _attach_hotspots([feat], commits)
    assert feats_hot == 1
    assert flows_hot == 1
    assert len(feat.hotspot_files) == 1
    assert feat.hotspot_files[0].path == "login.ts"
    assert len(flow.hotspot_files_detail) == 1


def test_attach_hotspots_handles_empty_inputs() -> None:
    assert _attach_hotspots([], []) == (0, 0)
    feat = _mk_feature("x", ["a.ts"])
    assert _attach_hotspots([feat], []) == (0, 0)
    assert feat.hotspot_files == []


def test_product_feature_hotspots_via_helper() -> None:
    pf = _mk_feature("Billing", ["stripe/checkout.ts", "stripe/webhook.ts"])
    pf.layer = "product"
    commits = (
        _bug_commits("stripe/checkout.ts", 5, 3, "C")
        + _bug_commits("stripe/webhook.ts", 0, 8, "W")
    )
    count = attach_hotspots_to_product_features([pf], commits)
    assert count == 1
    assert len(pf.hotspot_files) == 1
    assert pf.hotspot_files[0].path == "stripe/checkout.ts"
    assert pf.hotspot_files[0].bug_fixes == 5


def test_stage_6_metrics_attaches_hotspots_end_to_end(tmp_path: Path) -> None:
    feat = _mk_feature("auth", ["login.ts"])
    feat.flows = [_mk_flow("login-flow", ["login.ts"])]
    commits = _bug_commits("login.ts", 5, 3, "X")
    ctx = _mk_ctx(tmp_path, commits)
    out = stage_6_metrics([feat], ctx)
    assert len(out[0].hotspot_files) == 1
    assert out[0].hotspot_files[0].path == "login.ts"
    assert len(out[0].flows[0].hotspot_files_detail) == 1
