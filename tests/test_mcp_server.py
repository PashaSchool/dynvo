"""Tests for the standalone ``faultlines-mcp`` package.

Tool logic lives in :mod:`faultlines_mcp.core` as pure functions with the
uniform signature ``fn(scan, args, runtime=None) -> {"summary", "details"}``.
The scan dict is passed in directly, so these tests build a sample map and
assert on the returned ``details``. Disk/env loading (``_load_map``) stays in
:mod:`faultlines_mcp.server` and is tested separately.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from faultlines_mcp.core import (
    _savings_metadata,
    find_feature,
    get_feature_files,
    get_feature_owners,
    get_flow_files,
    get_hotspots,
    get_repo_summary,
    list_features,
)
from faultlines_mcp.server import _load_map


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
                "description": "Stripe payment processing",
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


@pytest.fixture
def scan() -> dict:
    """The sample feature map, passed directly to the pure tool functions."""
    return _sample_map()


@pytest.fixture
def map_on_disk(tmp_path: Path) -> Path:
    """Write a sample feature map and point the loader at it via env."""
    p = tmp_path / "feature-map-sample.json"
    p.write_text(json.dumps(_sample_map()))
    with patch.dict("os.environ", {"FAULTLINE_MAP_PATH": str(p)}):
        yield p


class TestLoadMap:
    def test_loads_from_env_path(self, map_on_disk: Path) -> None:
        data = _load_map()
        assert data["repo_path"] == "/tmp/sample"
        assert len(data["features"]) == 2

    def test_raises_when_env_path_missing(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"FAULTLINE_MAP_PATH": str(tmp_path / "missing.json")}):
            with pytest.raises(RuntimeError, match="does not exist"):
                _load_map()


class TestSavingsMetadata:
    def test_returns_positive_savings_for_small_response(self) -> None:
        m = _savings_metadata(files_returned=3)
        assert m["estimated_tokens_saved"] > 0
        assert m["files_returned"] == 3
        assert m["baseline_tokens"] > 0

    def test_clamps_savings_to_zero_when_overshooting(self) -> None:
        m = _savings_metadata(files_returned=999)
        assert m["estimated_tokens_saved"] == 0


class TestListFeatures:
    def test_returns_sorted_by_health(self, scan: dict) -> None:
        details = list_features(scan, {})["details"]
        assert details["total_features"] == 2
        # Sorted by health ascending — payments (25) before auth (85)
        assert details["features"][0]["name"] == "payments"
        assert details["features"][1]["name"] == "auth"

    def test_includes_savings_metadata(self, scan: dict) -> None:
        details = list_features(scan, {})["details"]
        assert "_savings_metadata" in details


class TestFindFeature:
    def test_matches_by_name(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "payments"})["details"]
        assert details["name"] == "payments"
        assert len(details["files"]) == 2
        assert "alice" in details["owners"]

    def test_matches_by_description(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "stripe"})["details"]
        assert details["name"] == "payments"

    def test_case_insensitive(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "AUTH"})["details"]
        assert details["name"] == "auth"

    def test_unmatched_for_unknown(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "nonexistent"})["details"]
        assert details["matched"] is False


class TestGetFeatureFiles:
    def test_returns_files_for_known_feature(self, scan: dict) -> None:
        details = get_feature_files(scan, {"feature_name": "payments"})["details"]
        assert details["feature"] == "payments"
        assert len(details["files"]) == 2
        assert "src/payments/charge.ts" in details["files"]

    def test_returns_hotspot_files(self, scan: dict) -> None:
        details = get_feature_files(scan, {"feature_name": "payments"})["details"]
        assert "src/payments/charge.ts" in details["hotspot_files"]

    def test_error_for_unknown_feature(self, scan: dict) -> None:
        details = get_feature_files(scan, {"feature_name": "unknown"})["details"]
        assert "error" in details
        assert "payments" in details["available"]


class TestGetHotspots:
    def test_returns_riskiest_first(self, scan: dict) -> None:
        details = get_hotspots(scan, {"limit": 2})["details"]
        assert len(details["hotspots"]) == 2
        assert details["hotspots"][0]["name"] == "payments"
        assert details["hotspots"][0]["health"] < details["hotspots"][1]["health"]

    def test_respects_limit(self, scan: dict) -> None:
        details = get_hotspots(scan, {"limit": 1})["details"]
        assert len(details["hotspots"]) == 1


class TestGetFeatureOwners:
    def test_returns_authors_and_bus_factor(self, scan: dict) -> None:
        details = get_feature_owners(scan, {"feature_name": "payments"})["details"]
        assert details["feature"] == "payments"
        assert details["owners"] == ["alice", "bob"]
        assert details["bus_factor"] == 1
        assert details["at_risk"] is True

    def test_not_at_risk_when_bus_factor_higher(self, scan: dict) -> None:
        details = get_feature_owners(scan, {"feature_name": "auth"})["details"]
        assert details["at_risk"] is False

    def test_error_for_unknown_feature(self, scan: dict) -> None:
        details = get_feature_owners(scan, {"feature_name": "unknown"})["details"]
        assert "error" in details


class TestGetFlowFiles:
    def test_returns_flow_files(self, scan: dict) -> None:
        details = get_flow_files(
            scan, {"feature_name": "payments", "flow_name": "checkout-flow"}
        )["details"]
        assert details["flow"] == "checkout-flow"
        assert details["files"] == ["src/payments/checkout.ts"]
        assert details["hotspot_files"] == ["src/payments/charge.ts"]

    def test_error_for_unknown_flow(self, scan: dict) -> None:
        details = get_flow_files(
            scan, {"feature_name": "payments", "flow_name": "missing"}
        )["details"]
        assert "error" in details


class TestGetRepoSummary:
    def test_returns_aggregated_stats(self, scan: dict) -> None:
        details = get_repo_summary(scan, {})["details"]
        assert details["total_features"] == 2
        assert details["total_commits"] == 500
        assert details["total_bug_fixes"] == 45
        assert details["features_at_risk"] == 1  # payments health < 50
        assert details["avg_coverage_pct"] == 76.0  # (60 + 92) / 2

    def test_avg_health_computed_correctly(self, scan: dict) -> None:
        details = get_repo_summary(scan, {})["details"]
        assert details["avg_health_score"] == 55.0  # (25 + 85) / 2
