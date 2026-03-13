"""Tests for action/pr_comment.py — PR comment generation logic."""

import json
import pytest
from datetime import datetime, timezone

from action.pr_comment import (
    parse_changed_files,
    filter_touched_features,
    render_comment,
    check_quality_gate,
    _feature_bus_factor,
    _feature_health_trend,
    _health_icon,
    _trend_icon,
    _sanitize_md,
    _render_features_table,
    _render_bus_factor_warnings,
    _collect_hotspot_files,
    COMMENT_MARKER,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _feature(
    name="auth",
    paths=None,
    health_score=80.0,
    bug_fix_ratio=0.1,
    total_commits=50,
    bug_fixes=5,
    authors=None,
    flows=None,
    bug_fix_prs=None,
):
    return {
        "name": name,
        "description": None,
        "paths": paths or ["auth/login.ts", "auth/signup.ts"],
        "authors": authors or ["alice", "bob"],
        "total_commits": total_commits,
        "bug_fixes": bug_fixes,
        "bug_fix_ratio": bug_fix_ratio,
        "last_modified": datetime.now(tz=timezone.utc).isoformat(),
        "health_score": health_score,
        "flows": flows or [],
        "bug_fix_prs": bug_fix_prs or [],
    }


def _flow(
    name="login-flow",
    health_score=70.0,
    bug_fix_ratio=0.15,
    bus_factor=2,
    health_trend=0.03,
    hotspot_files=None,
):
    return {
        "name": name,
        "paths": ["auth/login.ts"],
        "authors": ["alice"],
        "total_commits": 20,
        "bug_fixes": 3,
        "bug_fix_ratio": bug_fix_ratio,
        "health_score": health_score,
        "bus_factor": bus_factor,
        "health_trend": health_trend,
        "hotspot_files": hotspot_files or [],
        "test_file_count": 1,
        "weekly_points": [],
        "coverage_pct": None,
        "bug_fix_prs": [],
    }


def _feature_map(features=None):
    return {
        "repo_path": "/test/repo",
        "remote_url": "https://github.com/test/repo",
        "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_commits": 200,
        "date_range_days": 365,
        "features": features or [],
    }


# ---------------------------------------------------------------------------
# parse_changed_files
# ---------------------------------------------------------------------------

class TestParseChangedFiles:
    def test_colon_separated(self):
        result = parse_changed_files("a.ts:b.ts:c.ts")
        assert result == {"a.ts", "b.ts", "c.ts"}

    def test_empty_string(self):
        assert parse_changed_files("") == set()

    def test_trailing_colon(self):
        result = parse_changed_files("a.ts:b.ts:")
        assert result == {"a.ts", "b.ts"}

    def test_single_file(self):
        assert parse_changed_files("only.ts") == {"only.ts"}


# ---------------------------------------------------------------------------
# filter_touched_features
# ---------------------------------------------------------------------------

class TestFilterTouchedFeatures:
    def test_matches_overlap(self):
        features = [
            _feature(name="auth", paths=["auth/login.ts"]),
            _feature(name="payments", paths=["pay/stripe.ts"]),
        ]
        changed = {"auth/login.ts", "README.md"}
        result = filter_touched_features(features, changed)
        assert len(result) == 1
        assert result[0]["name"] == "auth"

    def test_no_overlap(self):
        features = [_feature(name="auth", paths=["auth/login.ts"])]
        changed = {"README.md"}
        assert filter_touched_features(features, changed) == []

    def test_multiple_features_touched(self):
        features = [
            _feature(name="auth", paths=["auth/login.ts"]),
            _feature(name="payments", paths=["pay/stripe.ts"]),
        ]
        changed = {"auth/login.ts", "pay/stripe.ts"}
        result = filter_touched_features(features, changed)
        assert len(result) == 2

    def test_empty_changed(self):
        features = [_feature()]
        assert filter_touched_features(features, set()) == []


# ---------------------------------------------------------------------------
# _feature_bus_factor
# ---------------------------------------------------------------------------

class TestFeatureBusFactor:
    def test_from_flows(self):
        f = _feature(flows=[_flow(bus_factor=2), _flow(bus_factor=3)])
        assert _feature_bus_factor(f) == 2

    def test_min_across_flows(self):
        f = _feature(flows=[_flow(bus_factor=1), _flow(bus_factor=5)])
        assert _feature_bus_factor(f) == 1

    def test_no_flows_uses_authors(self):
        f = _feature(authors=["alice", "bob"], flows=[])
        assert _feature_bus_factor(f) == 2

    def test_no_flows_caps_at_3(self):
        f = _feature(authors=["a", "b", "c", "d", "e"], flows=[])
        assert _feature_bus_factor(f) == 3


# ---------------------------------------------------------------------------
# _feature_health_trend
# ---------------------------------------------------------------------------

class TestFeatureHealthTrend:
    def test_average_of_flows(self):
        f = _feature(flows=[
            _flow(health_trend=0.1),
            _flow(health_trend=0.2),
        ])
        assert _feature_health_trend(f) == pytest.approx(0.15)

    def test_none_when_no_flows(self):
        assert _feature_health_trend(_feature(flows=[])) is None

    def test_none_when_all_trends_none(self):
        f = _feature(flows=[_flow(health_trend=None)])
        assert _feature_health_trend(f) is None


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

class TestIcons:
    def test_health_green(self):
        assert _health_icon(75) == "\U0001f7e2"

    def test_health_yellow(self):
        assert _health_icon(50) == "\U0001f7e1"

    def test_health_red(self):
        assert _health_icon(30) == "\U0001f534"

    def test_trend_up(self):
        assert _trend_icon(0.1) == "\u2191"

    def test_trend_down(self):
        assert _trend_icon(-0.1) == "\u2193"

    def test_trend_stable(self):
        assert _trend_icon(0.01) == "\u2192"

    def test_trend_none(self):
        assert _trend_icon(None) == "\u2014"


# ---------------------------------------------------------------------------
# _render_features_table
# ---------------------------------------------------------------------------

class TestSanitizeMd:
    def test_escapes_pipe(self):
        assert _sanitize_md("auth|login") == "auth\\|login"

    def test_replaces_newline(self):
        assert _sanitize_md("auth\nlogin") == "auth login"

    def test_clean_string_unchanged(self):
        assert _sanitize_md("payments") == "payments"


class TestRenderFeaturesTable:
    def test_contains_header(self):
        table = _render_features_table([_feature()])
        assert "Feature" in table
        assert "Health" in table
        assert "Bug %" in table

    def test_contains_feature_name(self):
        table = _render_features_table([_feature(name="payments")])
        assert "payments" in table

    def test_sorted_by_risk(self):
        features = [
            _feature(name="low", bug_fix_ratio=0.05),
            _feature(name="high", bug_fix_ratio=0.5),
        ]
        table = _render_features_table(features)
        high_pos = table.index("high")
        low_pos = table.index("low")
        assert high_pos < low_pos


# ---------------------------------------------------------------------------
# _render_bus_factor_warnings
# ---------------------------------------------------------------------------

class TestRenderBusFactorWarnings:
    def test_warns_on_low_bus_factor(self):
        f = _feature(name="risky", flows=[_flow(bus_factor=1)])
        result = _render_bus_factor_warnings([f])
        assert "risky" in result
        assert "1 author" in result

    def test_no_warning_when_bus_factor_ok(self):
        f = _feature(name="safe", flows=[_flow(bus_factor=3)])
        assert _render_bus_factor_warnings([f]) == ""


# ---------------------------------------------------------------------------
# _collect_hotspot_files
# ---------------------------------------------------------------------------

class TestCollectHotspotFiles:
    def test_collects_from_flows(self):
        f = _feature(
            paths=["auth/login.ts"],
            flows=[_flow(hotspot_files=["auth/login.ts"], bug_fix_ratio=0.6)],
        )
        changed = {"auth/login.ts"}
        result = _collect_hotspot_files([f], changed)
        assert len(result) == 1
        assert result[0]["path"] == "auth/login.ts"

    def test_ignores_unchanged_hotspots(self):
        f = _feature(
            paths=["auth/login.ts"],
            flows=[_flow(hotspot_files=["auth/login.ts"])],
        )
        changed = {"other/file.ts"}
        assert _collect_hotspot_files([f], changed) == []

    def test_fallback_to_feature_ratio(self):
        f = _feature(
            name="bad",
            paths=["bad/file.ts"],
            bug_fix_ratio=0.5,
            flows=[],
        )
        changed = {"bad/file.ts"}
        result = _collect_hotspot_files([f], changed)
        assert len(result) == 1

    def test_no_fallback_when_ratio_low(self):
        f = _feature(
            paths=["ok/file.ts"],
            bug_fix_ratio=0.1,
            flows=[],
        )
        changed = {"ok/file.ts"}
        assert _collect_hotspot_files([f], changed) == []


# ---------------------------------------------------------------------------
# render_comment
# ---------------------------------------------------------------------------

class TestRenderComment:
    def test_contains_marker(self):
        fm = _feature_map([_feature()])
        comment = render_comment(fm, [], set(), "")
        assert COMMENT_MARKER in comment

    def test_contains_summary(self):
        fm = _feature_map([_feature()])
        comment = render_comment(fm, [], set(), "")
        assert "200" in comment
        assert "365" in comment

    def test_contains_touched_table(self):
        f = _feature(name="auth")
        fm = _feature_map([f])
        comment = render_comment(fm, [f], {"auth/login.ts"}, "")
        assert "auth" in comment
        assert "Features touched" in comment

    def test_no_touched_message(self):
        fm = _feature_map([_feature()])
        comment = render_comment(fm, [], set(), "")
        assert "No tracked features" in comment

    def test_all_features_in_details(self):
        f = _feature(name="hidden")
        fm = _feature_map([f])
        comment = render_comment(fm, [], set(), "")
        assert "hidden" in comment
        assert "<details>" in comment


# ---------------------------------------------------------------------------
# check_quality_gate
# ---------------------------------------------------------------------------

class TestCheckQualityGate:
    def test_passes_above_threshold(self):
        features = [_feature(health_score=80)]
        assert check_quality_gate(features, 50.0) == []

    def test_fails_below_threshold(self):
        features = [_feature(name="bad", health_score=30)]
        result = check_quality_gate(features, 50.0)
        assert len(result) == 1
        assert result[0]["name"] == "bad"

    def test_exact_threshold_passes(self):
        features = [_feature(health_score=50)]
        assert check_quality_gate(features, 50.0) == []

    def test_empty_features(self):
        assert check_quality_gate([], 50.0) == []
