"""Tests for Stage 5.3 — sibling-router collapse (Sprint S4).

Universal across stacks; no repo-specific names appear here. Cases:

  - Tiny: 2 siblings → NOT collapsed (below MIN_SIBLINGS).
  - Repo-root scattering: depth 1 parent → NOT collapsed.
  - Mixed file kinds: only route-shaped collapse, util.ts kept.
  - Five siblings under one routes/v1 dir → collapsed to v1-routes.
  - Stage 2 anchor preserved alongside collapsed peers.
  - Anchor-overlap exception: synthesized label already exists as anchor.
  - Empty input.
  - Telemetry: counts match collapse groups + members.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_5_3_sibling_collapse import (
    MIN_PARENT_DEPTH,
    MIN_SIBLINGS,
    _display_label,
    _is_route_shaped,
    _primary_parent_dir,
    _synthesize_parent_label,
    collapse_sibling_routes,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _feat(name: str, paths: list[str]) -> Feature:
    """Construct a minimal Feature for tests."""
    return Feature(
        name=name,
        display_name=None,
        description=None,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )


# ── Threshold sanity ──────────────────────────────────────────────────────


def test_min_siblings_floor() -> None:
    """Documents the structural floor; tighten only with corpus-wide evidence."""
    assert MIN_SIBLINGS >= 3
    assert MIN_PARENT_DEPTH >= 2


# ── Helper coverage ───────────────────────────────────────────────────────


def test_primary_parent_dir_picks_lex_min_path() -> None:
    f = _feat("x", ["b/y/route.ts", "a/x/route.ts"])
    assert _primary_parent_dir(f) == "a/x"


def test_primary_parent_dir_returns_none_for_top_level_file() -> None:
    f = _feat("x", ["README"])
    assert _primary_parent_dir(f) is None


def test_route_shape_detection_for_common_conventions() -> None:
    assert _is_route_shaped(_feat("x", ["a/b/route.ts"]))
    assert _is_route_shaped(_feat("x", ["a/b/email-router.ts"]))
    assert _is_route_shaped(_feat("x", ["a/b/handler.go"]))
    assert _is_route_shaped(_feat("x", ["a/b/urls.py"]))
    assert _is_route_shaped(_feat("x", ["a/b/UserController.cs"]))
    assert _is_route_shaped(_feat("x", ["a/b/router.ex"]))
    # Negative: util / lib / type files don't qualify.
    assert not _is_route_shaped(_feat("x", ["a/b/utils.ts"]))
    assert not _is_route_shaped(_feat("x", ["a/b/types.ts"]))
    assert not _is_route_shaped(_feat("x", ["a/b/index.ts"]))


def test_synthesize_parent_label_versioned_dir() -> None:
    assert _synthesize_parent_label("backend/src/server/routes/v1") == "v1-routes"
    assert _synthesize_parent_label("apps/web/api/admin") == "api-admin-routes"
    # Already ends in container suffix → no double suffix.
    assert _synthesize_parent_label("services/auth/handlers") == "auth-handlers"
    assert _synthesize_parent_label("app/controllers") == "app-controllers"


def test_display_label_titlecases_kebab() -> None:
    assert _display_label("v1-routes") == "V1 Routes"
    assert _display_label("api-admin-routes") == "Api Admin Routes"


# ── Empty / no-op ─────────────────────────────────────────────────────────


def test_empty_input_returns_empty_result() -> None:
    result = collapse_sibling_routes([])
    assert result.features == []
    assert result.collapse_groups == []
    assert result.features_collapsed == 0


def test_two_siblings_do_not_collapse() -> None:
    """Below MIN_SIBLINGS → no collapse."""
    features = [
        _feat("email", ["backend/api/v1/email-router.ts"]),
        _feat("auth", ["backend/api/v1/auth-router.ts"]),
    ]
    result = collapse_sibling_routes(features)
    assert len(result.features) == 2
    assert result.collapse_groups == []


def test_repo_root_siblings_do_not_collapse() -> None:
    """Top-level files (depth 1 parent) → never collapse."""
    features = [
        _feat("email", ["email.ts"]),
        _feat("auth", ["auth.ts"]),
        _feat("status", ["status.ts"]),
        _feat("ping", ["ping.ts"]),
    ]
    result = collapse_sibling_routes(features)
    assert len(result.features) == 4
    assert result.collapse_groups == []


# ── Core collapse cases ───────────────────────────────────────────────────


def test_five_route_siblings_collapse_to_v1_routes() -> None:
    """Parent ``backend/server/routes/v1`` (container in path) →
    ``v1-routes``."""
    features = [
        _feat("email", ["backend/server/routes/v1/email-router.ts"]),
        _feat("auth", ["backend/server/routes/v1/auth-router.ts"]),
        _feat("status", ["backend/server/routes/v1/status-router.ts"]),
        _feat("ping", ["backend/server/routes/v1/ping-router.ts"]),
        _feat("admin", ["backend/server/routes/v1/admin-router.ts"]),
    ]
    result = collapse_sibling_routes(features)
    assert len(result.collapse_groups) == 1
    group = result.collapse_groups[0]
    assert group.parent_path == "backend/server/routes/v1"
    assert group.parent_label == "v1-routes"
    assert group.member_count == 5
    # Survivor list has exactly 1 merged feature.
    assert len(result.features) == 1
    merged = result.features[0]
    assert merged.name == "v1-routes"
    assert merged.display_name == "V1 Routes"
    assert len(merged.paths) == 5


def test_no_container_in_path_uses_last_two_segments_plus_routes() -> None:
    """Parent ``backend/api/v1`` (no container suffix) →
    ``api-v1-routes`` (last two segments + ``-routes``)."""
    features = [
        _feat(f"r{i}", [f"backend/api/v1/r{i}-router.ts"])
        for i in range(4)
    ]
    result = collapse_sibling_routes(features)
    assert len(result.collapse_groups) == 1
    assert result.collapse_groups[0].parent_label == "api-v1-routes"


def test_mixed_kinds_only_route_shaped_collapse() -> None:
    """3 route.ts + 1 utils.ts under the same dir: only routes collapse."""
    features = [
        _feat("email", ["backend/server/routes/v1/email-router.ts"]),
        _feat("auth", ["backend/server/routes/v1/auth-router.ts"]),
        _feat("status", ["backend/server/routes/v1/status-router.ts"]),
        _feat("utils", ["backend/server/routes/v1/utils.ts"]),
    ]
    result = collapse_sibling_routes(features)
    # 3 route-shaped siblings ≥ MIN_SIBLINGS → collapse fires.
    assert len(result.collapse_groups) == 1
    assert result.collapse_groups[0].member_count == 3
    names = {f.name for f in result.features}
    assert "v1-routes" in names
    assert "utils" in names
    assert len(result.features) == 2


def test_mixed_kinds_below_threshold_no_collapse() -> None:
    """Only 2 routes survive route-shape filter → below MIN_SIBLINGS."""
    features = [
        _feat("email", ["backend/api/v1/email-router.ts"]),
        _feat("auth", ["backend/api/v1/auth-router.ts"]),
        _feat("utils", ["backend/api/v1/utils.ts"]),
        _feat("types", ["backend/api/v1/types.ts"]),
    ]
    result = collapse_sibling_routes(features)
    assert result.collapse_groups == []
    assert len(result.features) == 4


# ── Anchor preservation guard ─────────────────────────────────────────────


def test_high_confidence_anchor_preserved_when_in_collapsing_group() -> None:
    """5 features under apps/web/api/admin/; ``admin-dashboard`` is a
    Stage 1 anchor (high confidence) — kept; other 4 collapse."""
    features = [
        _feat("admin-dashboard", ["apps/web/api/admin/admin-router.ts"]),
        _feat("users", ["apps/web/api/admin/users-router.ts"]),
        _feat("billing", ["apps/web/api/admin/billing-router.ts"]),
        _feat("audit", ["apps/web/api/admin/audit-router.ts"]),
        _feat("settings", ["apps/web/api/admin/settings-router.ts"]),
    ]
    confidence = {
        "admin-dashboard": "high",
        "users": "low",
        "billing": "low",
        "audit": "low",
        "settings": "low",
    }
    result = collapse_sibling_routes(features, confidence_by_name=confidence)
    assert len(result.collapse_groups) == 1
    assert result.collapse_groups[0].member_count == 4
    names = {f.name for f in result.features}
    assert "admin-dashboard" in names
    assert "users" not in names
    # Synthesized label is "api-admin-routes" not "admin-dashboard".
    assert "api-admin-routes" in names


def test_medium_confidence_anchor_also_preserved() -> None:
    features = [
        _feat("auth", ["backend/routes/v1/auth-router.ts"]),
        _feat("email", ["backend/routes/v1/email-router.ts"]),
        _feat("secret", ["backend/routes/v1/secret-router.ts"]),
        _feat("identity", ["backend/routes/v1/identity-router.ts"]),
    ]
    confidence = {"auth": "medium", "email": "low",
                  "secret": "low", "identity": "low"}
    result = collapse_sibling_routes(features, confidence_by_name=confidence)
    assert len(result.collapse_groups) == 1
    # auth preserved, 3 low collapse.
    assert result.collapse_groups[0].member_count == 3
    names = {f.name for f in result.features}
    assert "auth" in names
    assert "v1-routes" in names


def test_no_confidence_map_collapses_all_route_shaped() -> None:
    """When confidence map is None, anchor guard disabled — all collapse."""
    features = [
        _feat("auth", ["backend/routes/v1/auth-router.ts"]),
        _feat("email", ["backend/routes/v1/email-router.ts"]),
        _feat("secret", ["backend/routes/v1/secret-router.ts"]),
        _feat("identity", ["backend/routes/v1/identity-router.ts"]),
    ]
    result = collapse_sibling_routes(features)
    assert len(result.collapse_groups) == 1
    assert result.collapse_groups[0].member_count == 4
    assert {f.name for f in result.features} == {"v1-routes"}


# ── Anchor-overlap exception ──────────────────────────────────────────────


def test_anchor_name_matches_synth_label_absorbs_rest() -> None:
    """If a member's name happens to equal the synthesized label, fold
    the rest INTO that anchor (preserve anchor's name)."""
    features = [
        _feat("v1-routes", ["backend/routes/v1/index.ts"]),
        _feat("email", ["backend/routes/v1/email-router.ts"]),
        _feat("secret", ["backend/routes/v1/secret-router.ts"]),
        _feat("identity", ["backend/routes/v1/identity-router.ts"]),
    ]
    # Note: "v1-routes" only has index.ts (not router-shaped); we mark
    # it route-shaped by adding a router file too.
    features[0] = _feat("v1-routes", [
        "backend/routes/v1/index.ts",
        "backend/routes/v1/main-router.ts",
    ])
    result = collapse_sibling_routes(features)
    assert len(result.collapse_groups) == 1
    group = result.collapse_groups[0]
    # The anchor-match is included in members (its router file qualifies).
    assert group.parent_label == "v1-routes"
    names = {f.name for f in result.features}
    assert "v1-routes" in names
    # All siblings folded.
    merged = next(f for f in result.features if f.name == "v1-routes")
    assert len(merged.paths) >= 4


# ── Disjoint dirs don't cross-collapse ────────────────────────────────────


def test_two_separate_dirs_each_collapse_independently() -> None:
    features = [
        # Dir 1: backend/routes/v1
        _feat("email", ["backend/routes/v1/email-router.ts"]),
        _feat("auth", ["backend/routes/v1/auth-router.ts"]),
        _feat("status", ["backend/routes/v1/status-router.ts"]),
        # Dir 2: backend/routes/v2
        _feat("ping", ["backend/routes/v2/ping-router.ts"]),
        _feat("session", ["backend/routes/v2/session-router.ts"]),
        _feat("mfa", ["backend/routes/v2/mfa-router.ts"]),
    ]
    result = collapse_sibling_routes(features)
    assert len(result.collapse_groups) == 2
    labels = {g.parent_label for g in result.collapse_groups}
    assert labels == {"v1-routes", "v2-routes"}
    assert len(result.features) == 2


# ── Telemetry ─────────────────────────────────────────────────────────────


def test_telemetry_counts_match_groups() -> None:
    features = [
        _feat(f"r{i}", [f"backend/routes/v1/r{i}-router.ts"])
        for i in range(7)
    ]
    result = collapse_sibling_routes(features)
    assert len(result.collapse_groups) == 1
    assert result.features_collapsed == 7
    g = result.collapse_groups[0]
    d = g.as_dict()
    assert d["parent"] == "backend/routes/v1"
    assert d["label"] == "v1-routes"
    assert d["member_count"] == 7
    assert len(d["members_sample"]) == 5  # default sample size


def test_merged_feature_unions_paths_and_authors() -> None:
    f1 = _feat("a", ["backend/routes/v1/a-router.ts"])
    f1.authors = ["alice"]
    f1.total_commits = 5
    f1.bug_fixes = 1
    f2 = _feat("b", ["backend/routes/v1/b-router.ts"])
    f2.authors = ["bob"]
    f2.total_commits = 3
    f2.bug_fixes = 2
    f3 = _feat("c", ["backend/routes/v1/c-router.ts"])
    f3.authors = ["alice", "carol"]
    f3.total_commits = 2
    f3.bug_fixes = 0
    result = collapse_sibling_routes([f1, f2, f3])
    assert len(result.features) == 1
    merged = result.features[0]
    assert set(merged.paths) == {
        "backend/routes/v1/a-router.ts",
        "backend/routes/v1/b-router.ts",
        "backend/routes/v1/c-router.ts",
    }
    assert merged.authors == ["alice", "bob", "carol"]
    assert merged.total_commits == 10
    assert merged.bug_fixes == 3
    assert merged.bug_fix_ratio == pytest.approx(0.3, rel=1e-3)


# ── Determinism ───────────────────────────────────────────────────────────


def test_idempotent_on_collapsed_input() -> None:
    """Running the collapser twice on the same input is a no-op the
    second time (no further collapse occurs once everything has been
    folded)."""
    features = [
        _feat(f"r{i}", [f"backend/routes/v1/r{i}-router.ts"])
        for i in range(5)
    ]
    first = collapse_sibling_routes(features)
    second = collapse_sibling_routes(first.features)
    assert len(second.features) == 1
    assert second.collapse_groups == []  # nothing left to collapse
    assert {f.name for f in first.features} == {f.name for f in second.features}
