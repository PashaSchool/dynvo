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


# ── Fix 2 (2026-05-26): derive routes from real AnchorCandidate paths ─
# The Stage 1 route extractor emits AnchorCandidate (paths only — no
# pattern/method/file). routes_index must be populated by deriving the
# URL pattern from each route file path.
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.indexes import _derive_route_from_path


def test_derive_route_app_router_api():
    assert _derive_route_from_path("app/api/teams/[id]/route.ts") == (
        "/api/teams/:id", "GET")
    assert _derive_route_from_path("src/app/api/products/route.ts") == (
        "/api/products", "GET")


def test_derive_route_app_router_page_strips_group():
    assert _derive_route_from_path("src/app/(dashboard)/settings/page.tsx") == (
        "/settings", "PAGE")


def test_derive_route_pages_router_dynamic():
    assert _derive_route_from_path("pages/users/[id].tsx") == (
        "/users/:id", "PAGE")


def test_derive_route_non_route_path_returns_none():
    assert _derive_route_from_path("lib/util/helpers.ts") is None


def test_routes_index_from_anchor_candidate_paths():
    features = [{
        "uuid": "A" * 32,
        "paths": ["src/app/api/teams/[id]/route.ts", "src/app/teams/page.tsx"],
    }]
    cand = AnchorCandidate(
        name="teams",
        paths=("src/app/api/teams/[id]/route.ts", "src/app/teams/page.tsx"),
        source="route",
        confidence_self=0.9,
    )
    routes = build_routes_index(features, {"route": [cand]})
    by_file = {r["file"]: r for r in routes}
    assert by_file["src/app/api/teams/[id]/route.ts"]["pattern"] == "/api/teams/:id"
    assert by_file["src/app/api/teams/[id]/route.ts"]["method"] == "GET"
    assert by_file["src/app/teams/page.tsx"]["pattern"] == "/teams"
    assert by_file["src/app/teams/page.tsx"]["method"] == "PAGE"
    # owning feature attributed via path
    assert all(r["feature_uuid"] == "A" * 32 for r in routes)


def test_routes_index_dedups_repeated_route_files():
    features = [{"uuid": "A" * 32, "paths": ["app/api/x/route.ts"]}]
    cand = AnchorCandidate(
        name="x", paths=("app/api/x/route.ts", "app/api/x/route.ts"),
        source="route", confidence_self=0.9,
    )
    routes = build_routes_index(features, {"route": [cand]})
    assert len(routes) == 1


def test_derive_route_monorepo_workspace_prefix():
    # cal.com Turborepo: apps/api/v1/pages/api/... — workspace prefix
    # stripped, per-verb leaf names the method.
    assert _derive_route_from_path(
        "apps/api/v1/pages/api/teams/[teamId]/_get.ts") == (
        "/api/teams/:teamId", "GET")
    assert _derive_route_from_path(
        "apps/api/v1/pages/api/teams/[teamId]/_patch.ts") == (
        "/api/teams/:teamId", "PATCH")
    assert _derive_route_from_path(
        "apps/api/v1/pages/api/attendees/[id]/_delete.ts") == (
        "/api/attendees/:id", "DELETE")


def test_derive_route_monorepo_app_router():
    assert _derive_route_from_path(
        "apps/web/app/(use-page-wrapper)/teams/page.tsx") == (
        "/teams", "PAGE")


def test_derive_route_skips_convention_private_leaf():
    # _auth-middleware.ts / _app.tsx are not addressable routes.
    assert _derive_route_from_path(
        "apps/api/v1/pages/api/teams/[teamId]/_auth-middleware.ts") is None
