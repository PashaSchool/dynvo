"""Tests for faultline/mcp_server.py + mcp_context."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from faultline.mcp_context import (
    SCHEMA_VERSION,
    error_payload,
    fuzzy_feature_suggestions,
    load_map,
    resolve_feature,
)
from faultline.mcp_server import (
    find_feature,
    get_feature_files,
    get_feature_owners,
    get_flow_files,
    get_hotspots,
    get_repo_summary,
    list_features,
    refresh_feature_map,
    resource_feature,
    resource_repo_summary,
)


def _sample_map() -> dict:
    return {
        "repo_path": "/tmp/sample",
        "remote_url": "https://github.com/org/sample",
        "analyzed_at": "2026-04-11T10:00:00Z",
        "total_commits": 500,
        "date_range_days": 365,
        "features": [
            {
                "name": "payments",
                "display_name": "payments",
                "description": "Stripe payment processing",
                "aliases": ["billing"],
                "paths": ["src/payments/charge.ts", "src/payments/webhook.ts"],
                "authors": ["alice", "bob"],
                "total_commits": 80,
                "bug_fixes": 40,
                "bug_fix_ratio": 0.5,
                "health_score": 25.0,
                "coverage_pct": 60.0,
                "flows": [
                    {
                        "name": "checkout-flow",
                        "paths": ["src/payments/checkout.ts"],
                        "total_commits": 30,
                        "bug_fixes": 20,
                        "bug_fix_ratio": 0.67,
                        "health_score": 18.0,
                        "bus_factor": 1,
                        "hotspot_files": ["src/payments/charge.ts"],
                    },
                ],
            },
            {
                "name": "auth",
                "display_name": "auth",
                "description": "User authentication",
                "paths": ["src/auth/login.ts"],
                "authors": ["alice", "bob", "charlie"],
                "total_commits": 40,
                "bug_fixes": 5,
                "bug_fix_ratio": 0.125,
                "health_score": 85.0,
                "coverage_pct": 92.0,
                "flows": [],
            },
        ],
    }


@pytest.fixture(autouse=True)
def _reset_map_cache() -> None:
    """Drop the mtime cache between tests so fixtures don't bleed."""
    from faultline import mcp_context
    mcp_context._map_cache.clear()


@pytest.fixture
def fake_map(tmp_path: Path) -> Path:
    """Write a sample feature map and point the loader at it."""
    p = tmp_path / "feature-map-sample.json"
    p.write_text(json.dumps(_sample_map()))
    with patch.dict("os.environ", {"FAULTLINE_MAP_PATH": str(p)}):
        yield p


class TestLoadMap:
    def test_loads_from_env_path(self, fake_map: Path) -> None:
        data = load_map()
        assert data["repo_path"] == "/tmp/sample"
        assert len(data["features"]) == 2

    def test_raises_when_env_path_missing(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"FAULTLINE_MAP_PATH": str(tmp_path / "missing.json")}):
            with pytest.raises(RuntimeError, match="does not exist"):
                load_map()

    def test_mtime_cache_hit(self, fake_map: Path) -> None:
        """Second call with same mtime returns same object identity."""
        a = load_map()
        b = load_map()
        assert a is b

    def test_mtime_cache_invalidates_on_change(self, fake_map: Path) -> None:
        a = load_map()
        # Modify the file → mtime changes → cache should refresh
        new = _sample_map()
        new["repo_path"] = "/tmp/changed"
        fake_map.write_text(json.dumps(new))
        # Bump mtime explicitly in case the write happens within the same
        # filesystem second granularity.
        import os
        st = fake_map.stat()
        os.utime(fake_map, (st.st_atime, st.st_mtime + 2))
        b = load_map()
        assert b["repo_path"] == "/tmp/changed"
        assert a is not b


class TestResolveFeature:
    def test_exact_matches_name(self) -> None:
        fm = _sample_map()
        f = resolve_feature(fm, "payments", mode="exact")
        assert f is not None and f["name"] == "payments"

    def test_exact_matches_alias(self) -> None:
        fm = _sample_map()
        f = resolve_feature(fm, "billing", mode="exact")
        assert f is not None and f["name"] == "payments"

    def test_exact_does_not_match_description_substring(self) -> None:
        fm = _sample_map()
        # "stripe" appears in description but not in name/alias/label
        assert resolve_feature(fm, "stripe", mode="exact") is None

    def test_fuzzy_matches_description(self) -> None:
        fm = _sample_map()
        f = resolve_feature(fm, "stripe", mode="fuzzy")
        assert f is not None and f["name"] == "payments"

    def test_case_insensitive(self) -> None:
        fm = _sample_map()
        assert resolve_feature(fm, "AUTH", mode="exact") is not None

    def test_empty_query_returns_none(self) -> None:
        fm = _sample_map()
        assert resolve_feature(fm, "   ", mode="exact") is None


class TestFuzzySuggestions:
    def test_returns_top_matches(self) -> None:
        fm = _sample_map()
        out = fuzzy_feature_suggestions(fm, "paymentz", limit=5)
        assert "payments" in out

    def test_caps_to_limit(self) -> None:
        fm = _sample_map()
        out = fuzzy_feature_suggestions(fm, "x", limit=1)
        assert len(out) <= 1


class TestListFeatures:
    def test_returns_sorted_by_health(self, fake_map: Path) -> None:
        result = list_features()
        assert result["total_features"] == 2
        assert result["features"][0]["name"] == "payments"
        assert result["features"][1]["name"] == "auth"

    def test_pagination_offset(self, fake_map: Path) -> None:
        result = list_features(limit=1, offset=1)
        assert len(result["features"]) == 1
        assert result["features"][0]["name"] == "auth"
        assert result["has_more"] is False
        assert result["total_features"] == 2

    def test_pagination_first_page(self, fake_map: Path) -> None:
        result = list_features(limit=1, offset=0)
        assert len(result["features"]) == 1
        assert result["has_more"] is True

    def test_no_savings_metadata_in_payload(self, fake_map: Path) -> None:
        result = list_features()
        assert "_savings_metadata" not in result


class TestFindFeature:
    def test_matches_by_name(self, fake_map: Path) -> None:
        result = find_feature(query="payments")
        assert result is not None
        assert result["name"] == "payments"
        assert "alice" in result["owners"]

    def test_matches_by_description(self, fake_map: Path) -> None:
        result = find_feature(query="stripe")
        assert result is not None
        assert result["name"] == "payments"

    def test_case_insensitive(self, fake_map: Path) -> None:
        result = find_feature(query="AUTH")
        assert result is not None
        assert result["name"] == "auth"

    def test_returns_none_for_unknown(self, fake_map: Path) -> None:
        assert find_feature(query="nonexistent") is None


class TestGetFeatureFiles:
    def test_returns_files_for_known_feature(self, fake_map: Path) -> None:
        result = get_feature_files(feature_name="payments")
        assert result["feature"] == "payments"
        assert len(result["files"]) == 2

    def test_alias_resolves_to_same_feature(self, fake_map: Path) -> None:
        """Regression: find_feature finds via alias, get_feature_files must too."""
        result = get_feature_files(feature_name="billing")
        assert result["feature"] == "payments"

    def test_error_has_suggestions_not_full_list(self, fake_map: Path) -> None:
        result = get_feature_files(feature_name="unknowzz")
        assert "error" in result
        assert "suggestions" in result
        # Top-5 suggestions, not the full feature list dumped
        assert len(result["suggestions"]) <= 5
        # "available" key from old contract should be gone
        assert "available" not in result


class TestGetHotspots:
    def test_returns_riskiest_first(self, fake_map: Path) -> None:
        result = get_hotspots(limit=2)
        assert len(result["hotspots"]) == 2
        assert result["hotspots"][0]["name"] == "payments"

    def test_respects_limit(self, fake_map: Path) -> None:
        result = get_hotspots(limit=1)
        assert len(result["hotspots"]) == 1


class TestGetFeatureOwners:
    def test_returns_authors_and_bus_factor(self, fake_map: Path) -> None:
        result = get_feature_owners(feature_name="payments")
        assert result["feature"] == "payments"
        assert result["owners"] == ["alice", "bob"]
        assert result["bus_factor"] == 1
        assert result["at_risk"] is True

    def test_not_at_risk_when_bus_factor_higher(self, fake_map: Path) -> None:
        result = get_feature_owners(feature_name="auth")
        assert result["at_risk"] is False

    def test_error_for_unknown_feature(self, fake_map: Path) -> None:
        result = get_feature_owners(feature_name="unknown")
        assert "error" in result
        assert "suggestions" in result


class TestGetFlowFiles:
    def test_returns_flow_files(self, fake_map: Path) -> None:
        result = get_flow_files(feature_name="payments", flow_name="checkout-flow")
        assert result["flow"] == "checkout-flow"
        assert result["files"] == ["src/payments/checkout.ts"]

    def test_error_for_unknown_flow_lists_available(self, fake_map: Path) -> None:
        result = get_flow_files(feature_name="payments", flow_name="missing")
        assert "error" in result
        assert "checkout-flow" in result["available_flows"]

    def test_error_for_unknown_feature(self, fake_map: Path) -> None:
        result = get_flow_files(feature_name="nope", flow_name="x")
        assert "error" in result
        assert "suggestions" in result


class TestGetRepoSummary:
    def test_returns_aggregated_stats(self, fake_map: Path) -> None:
        result = get_repo_summary()
        assert result["total_features"] == 2
        assert result["total_commits"] == 500
        assert result["total_bug_fixes"] == 45
        assert result["features_at_risk"] == 1
        assert result["avg_coverage_pct"] == 76.0

    def test_avg_health_computed_correctly(self, fake_map: Path) -> None:
        result = get_repo_summary()
        assert result["avg_health_score"] == 55.0


class TestSchemaVersion:
    def test_tool_response_carries_schema_version(self, fake_map: Path) -> None:
        for result in (
            list_features(),
            get_repo_summary(),
            get_hotspots(),
            find_feature(query="payments"),
            get_feature_files(feature_name="payments"),
            get_feature_owners(feature_name="payments"),
        ):
            assert result is not None
            assert result.get("_schema_version") == SCHEMA_VERSION

    def test_error_response_carries_schema_version(self, fake_map: Path) -> None:
        result = get_feature_files(feature_name="nope")
        assert result["_schema_version"] == SCHEMA_VERSION
        assert "error" in result

    def test_error_payload_helper_shape(self) -> None:
        out = error_payload("bad", code="x", extra=42)
        assert out["_schema_version"] == SCHEMA_VERSION
        assert out["error"] == "bad"
        assert out["code"] == "x"
        assert out["extra"] == 42


class TestRefreshFeatureMap:
    def test_returns_status_dict(self, fake_map: Path) -> None:
        result = refresh_feature_map()
        # Best-effort: helper may or may not be installed; we only
        # assert the contract.
        assert result["_schema_version"] == SCHEMA_VERSION
        assert "triggered" in result
        assert "map_path" in result


class TestResources:
    def test_repo_summary_resource_matches_tool(self, fake_map: Path) -> None:
        tool_out = get_repo_summary()
        resource_out = resource_repo_summary()
        # Same shape; schema version present.
        assert resource_out["total_features"] == tool_out["total_features"]
        assert resource_out["_schema_version"] == SCHEMA_VERSION

    def test_feature_resource_resolves_known(self, fake_map: Path) -> None:
        result = resource_feature(name="payments")
        assert result["name"] == "payments"
        assert "files" in result

    def test_feature_resource_unknown_returns_error(self, fake_map: Path) -> None:
        result = resource_feature(name="ghost")
        assert "error" in result
        assert "suggestions" in result
