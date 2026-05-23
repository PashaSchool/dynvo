"""Unit tests for faultline.pipeline_v2.indexes."""
from __future__ import annotations

from faultline.pipeline_v2.indexes import build_path_index, build_routes_index


def test_path_index_maps_files_to_feature_uuid():
    features = [
        {"uuid": "A" * 32, "paths": ["src/a.ts", "src/b.ts"]},
        {"uuid": "B" * 32, "paths": ["src/c.ts"]},
    ]
    idx = build_path_index(features, flows=[])
    assert idx["src/a.ts"]["feature_uuid"] == "A" * 32
    assert idx["src/c.ts"]["feature_uuid"] == "B" * 32
    assert idx["src/a.ts"]["flow_uuids"] == []


def test_path_index_attaches_multiple_flows_per_file():
    features = [{"uuid": "A" * 32, "paths": ["src/a.ts"]}]
    flows = [
        {"uuid": "F1" * 16, "paths": ["src/a.ts"]},
        {"uuid": "F2" * 16, "paths": ["src/a.ts"]},
    ]
    idx = build_path_index(features, flows)
    assert idx["src/a.ts"]["feature_uuid"] == "A" * 32
    assert "F1" * 16 in idx["src/a.ts"]["flow_uuids"]
    assert "F2" * 16 in idx["src/a.ts"]["flow_uuids"]


def test_path_index_first_owner_wins_on_conflict():
    features = [
        {"uuid": "A" * 32, "paths": ["src/a.ts"]},
        {"uuid": "B" * 32, "paths": ["src/a.ts"]},
    ]
    idx = build_path_index(features, [])
    assert idx["src/a.ts"]["feature_uuid"] == "A" * 32


def test_path_index_skips_features_without_uuid():
    features = [
        {"uuid": "", "paths": ["src/a.ts"]},
        {"uuid": "B" * 32, "paths": ["src/b.ts"]},
    ]
    idx = build_path_index(features, [])
    assert "src/a.ts" not in idx
    assert "src/b.ts" in idx


def test_routes_index_links_pattern_to_feature_uuid_via_file_owner():
    features = [
        {"uuid": "A" * 32, "paths": ["src/app/api/products/route.ts"]},
    ]
    # Duck-typed signal object
    class Sig:
        pattern = "/api/products"
        method = "GET"
        file = "src/app/api/products/route.ts"
    routes = build_routes_index(features, {"route": [Sig()]})
    assert len(routes) == 1
    assert routes[0]["pattern"] == "/api/products"
    assert routes[0]["method"] == "GET"
    assert routes[0]["feature_uuid"] == "A" * 32
    assert routes[0]["file"] == "src/app/api/products/route.ts"


def test_routes_index_returns_empty_when_no_extractor_signals():
    assert build_routes_index([], None) == []
    assert build_routes_index([], {}) == []
    assert build_routes_index([], {"route": []}) == []


def test_routes_index_orphan_route_gets_empty_feature_uuid():
    features = [{"uuid": "A" * 32, "paths": ["src/other.ts"]}]
    class Sig:
        pattern = "/api/orphan"
        method = "POST"
        file = "src/orphan.ts"
    routes = build_routes_index(features, {"route": [Sig()]})
    assert routes[0]["feature_uuid"] == ""


def test_routes_index_accepts_dict_signals():
    features = [{"uuid": "A" * 32, "paths": ["x.ts"]}]
    sig = {"pattern": "/x", "method": "GET", "file": "x.ts"}
    routes = build_routes_index(features, {"route": [sig]})
    assert routes[0]["feature_uuid"] == "A" * 32
