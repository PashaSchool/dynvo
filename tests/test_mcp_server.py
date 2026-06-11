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
    TOOLS,
    _score_feature,
    _token_coverage,
    _tokenize,
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


class TestResponseMetadata:
    """The old static formula (15 × 2500 − 500 = 37000 'saved') is gone:
    metadata now reports only the measured size of the actual payload."""

    def test_no_static_savings_constant_in_response(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "payments"})["details"]
        meta = details["_savings_metadata"]
        assert "estimated_tokens_saved" not in meta
        assert "baseline_tokens" not in meta
        assert "37000" not in json.dumps(details)

    def test_response_tokens_est_tracks_actual_payload(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "payments"})["details"]
        meta = details["_savings_metadata"]
        # ~len(json)/4 of the payload before the metadata block was added
        without_meta = {k: v for k, v in details.items() if k != "_savings_metadata"}
        expected = len(json.dumps(without_meta, default=str, separators=(",", ":"))) // 4
        assert meta["response_tokens_est"] == max(1, expected)
        assert meta["files_returned"] == 2

    def test_empty_result_makes_no_savings_claim(self, scan: dict) -> None:
        details = find_feature(scan, {"query": "zzz qqq"})["details"]
        assert details["matched"] is False
        assert "estimated_tokens_saved" not in json.dumps(details)
        assert "_savings_metadata" not in details

    def test_all_tools_report_no_fabricated_savings(self, scan: dict) -> None:
        for name, spec in TOOLS.items():
            args = {
                "query": "payments", "feature_name": "payments",
                "flow_name": "checkout-flow",
                "changed_files": ["src/payments/charge.ts"],
            }
            payload = json.dumps(spec["fn"](scan, args))
            assert "estimated_tokens_saved" not in payload, name
            assert "baseline_tokens" not in payload, name


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


class TestTokenizer:
    def test_kebab_snake_parens_split(self) -> None:
        # parens stripped, kebab split, "s"-fold ("base" keeps its 'e')
        assert _tokenize("organization-knowledge-base-(rag)") == [
            "organization", "knowledge", "base", "rag",
        ]

    def test_camel_case_split(self) -> None:
        assert _tokenize("checkoutFlowV2") == ["checkout", "flow", "v2"]

    def test_suffix_fold_plural(self) -> None:
        assert _tokenize("payments") == _tokenize("payment")

    def test_empty_and_punct_only(self) -> None:
        assert _tokenize("") == []
        assert _tokenize("()---") == []


class TestTokenCoverage:
    def test_exact_beats_prefix(self) -> None:
        q = _tokenize("knowledge")
        assert _token_coverage(q, _tokenize("knowledge-base")) == 1.0
        assert _token_coverage(q, _tokenize("know-how")) == 0.5  # prefix only

    def test_zero_when_no_overlap(self) -> None:
        assert _token_coverage(_tokenize("zzz"), _tokenize("payments")) == 0.0

    def test_name_field_outweighs_description(self) -> None:
        by_name = _score_feature(_tokenize("billing"), {"name": "billing"})
        by_desc = _score_feature(_tokenize("billing"), {"name": "x", "description": "billing"})
        assert by_name > by_desc


class TestFindFeatureTokenMatch:
    """The review's headline case: space-separated query vs kebab+parens name."""

    @pytest.fixture
    def rag_scan(self) -> dict:
        return {
            "features": [
                {"name": "organization-knowledge-base-(rag)",
                 "description": "RAG over org docs", "paths": ["src/kb/rag.ts"],
                 "flows": [], "health_score": 70},
                {"name": "knowledge-export",
                 "description": "Export knowledge articles", "paths": ["src/kb/export.ts"],
                 "flows": [], "health_score": 80},
                {"name": "billing", "description": "Stripe billing",
                 "paths": ["src/billing.ts"], "flows": [], "health_score": 90},
                {"name": "auth", "description": "knowledge of users",  # desc-only overlap
                 "paths": ["src/auth.ts"], "flows": [], "health_score": 95},
            ],
        }

    def test_space_query_matches_kebab_paren_name(self, rag_scan: dict) -> None:
        details = find_feature(rag_scan, {"query": "knowledge RAG"})["details"]
        assert details["matched"] is True
        assert details["name"] == "organization-knowledge-base-(rag)"
        assert details["match_score"] > 0

    def test_top3_candidates_ranked_desc(self, rag_scan: dict) -> None:
        details = find_feature(rag_scan, {"query": "knowledge"})["details"]
        cands = details["candidates"]
        assert 1 <= len(cands) <= 3
        scores = [c["score"] for c in cands]
        assert scores == sorted(scores, reverse=True)
        # name-token matches outrank the description-only match ("auth")
        assert cands[0]["name"] in ("knowledge-export", "organization-knowledge-base-(rag)")

    def test_zero_overlap_returns_matched_false(self, rag_scan: dict) -> None:
        details = find_feature(rag_scan, {"query": "qqq zzz"})["details"]
        assert details["matched"] is False
        assert details["candidates"] == []

    def test_best_match_fields_preserved(self, rag_scan: dict) -> None:
        # additive schema: historical flat fields still present
        details = find_feature(rag_scan, {"query": "billing"})["details"]
        for key in ("name", "description", "health", "files", "file_count", "owners", "flows"):
            assert key in details


class TestToolSchemas:
    def test_every_tool_exposes_optional_repo_slug(self) -> None:
        for name, spec in TOOLS.items():
            schema = spec["inputSchema"]
            props = schema["properties"]
            assert "repo_slug" in props, name
            assert props["repo_slug"]["type"] == "string", name
            assert "session default" in props["repo_slug"]["description"], name
            assert "repo_slug" not in schema.get("required", []), name


class TestLoadMapRepoSlug:
    def test_loads_newest_map_for_slug(self, tmp_path: Path) -> None:
        fl = tmp_path / ".faultline"
        fl.mkdir()
        old = dict(_sample_map(), total_commits=1)
        new = dict(_sample_map(), total_commits=2)
        (fl / "feature-map-sample-2026-01-01.json").write_text(json.dumps(old))
        (fl / "feature-map-sample-2026-02-01.json").write_text(json.dumps(new))
        (fl / "feature-map-other-2026-03-01.json").write_text(json.dumps(_sample_map()))
        with patch.object(Path, "home", return_value=tmp_path):
            data = _load_map(repo_slug="sample")
        assert data["total_commits"] == 2

    def test_slug_overrides_env_path(self, tmp_path: Path) -> None:
        fl = tmp_path / ".faultline"
        fl.mkdir()
        (fl / "feature-map-sample.json").write_text(json.dumps(_sample_map()))
        env_map = tmp_path / "env-map.json"
        env_map.write_text(json.dumps(dict(_sample_map(), repo_path="/env/map")))
        with patch.dict("os.environ", {"FAULTLINE_MAP_PATH": str(env_map)}):
            with patch.object(Path, "home", return_value=tmp_path):
                assert _load_map(repo_slug="sample")["repo_path"] == "/tmp/sample"
            assert _load_map()["repo_path"] == "/env/map"

    def test_unknown_slug_raises(self, tmp_path: Path) -> None:
        (tmp_path / ".faultline").mkdir()
        with patch.object(Path, "home", return_value=tmp_path):
            with pytest.raises(RuntimeError, match="repo slug 'nope'"):
                _load_map(repo_slug="nope")


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
