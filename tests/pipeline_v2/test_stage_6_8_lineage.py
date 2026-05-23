"""Unit tests for the Stage 6.8 lineage orchestrator."""
from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_6_8_lineage import run_stage_6_8


def _feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=100.0,
    )


def _flow(name: str, paths: list[str], primary: str) -> Flow:
    return Flow(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=100.0,
        primary_feature=primary,
    )


def test_stage_6_8_cold_scan_stamps_uuids():
    features = [_feature("billing", ["a.ts", "b.ts"])]
    flows = [_flow("checkout-flow", ["a.ts"], primary="billing")]
    result = run_stage_6_8(features, flows, base_scan=None)
    assert features[0].uuid != ""
    assert flows[0].uuid != ""
    assert "a.ts" in result.path_index
    assert result.path_index["a.ts"]["feature_uuid"] == features[0].uuid
    assert flows[0].uuid in result.path_index["a.ts"]["flow_uuids"]


def test_stage_6_8_reuses_base_uuid_on_match():
    base_uuid = "X" * 32
    base = {
        "developer_features": [
            {"name": "billing", "paths": ["a.ts", "b.ts"], "uuid": base_uuid},
        ],
        "flows": [],
    }
    features = [_feature("billing", ["a.ts", "b.ts"])]
    result = run_stage_6_8(features, [], base_scan=base)
    assert features[0].uuid == base_uuid
    assert result.feature_lineage_stats["carried_forward"] == 1


def test_stage_6_8_records_rename_in_previous_names():
    base_uuid = "Y" * 32
    base = {
        "features": [
            {"name": "subscriptions", "paths": ["a.ts", "b.ts"], "uuid": base_uuid},
        ],
    }
    features = [_feature("billing", ["a.ts", "b.ts"])]
    result = run_stage_6_8(features, [], base_scan=base)
    assert features[0].uuid == base_uuid
    assert features[0].previous_names == ["subscriptions"]
    assert result.feature_lineage_stats["renamed"] == 1


def test_stage_6_8_routes_index_built_from_extractor_signals():
    features = [_feature("api", ["src/api/users.ts"])]
    class Sig:
        pattern = "/users"
        method = "GET"
        file = "src/api/users.ts"
    result = run_stage_6_8(
        features, [], base_scan=None,
        extractor_signals={"route": [Sig()]},
    )
    assert len(result.routes_index) == 1
    assert result.routes_index[0]["pattern"] == "/users"
    assert result.routes_index[0]["feature_uuid"] == features[0].uuid
