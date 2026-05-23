"""Sprint 2 — unit tests for T2 cross-stack HTTP matching."""

from __future__ import annotations

from faultline.pipeline_v2.flow_expansion.cross_stack import (
    _normalise_pattern,
    _url_matches_pattern,
    find_cross_stack_hits,
)


# ── pattern normalisation ───────────────────────────────────────────────

class TestNormalisePattern:
    def test_collapses_nextjs_dynamic_segments(self):
        assert _normalise_pattern("/api/users/[id]") == "/api/users/*"
        assert _normalise_pattern("/api/posts/[...slug]") == "/api/posts/*"

    def test_collapses_fastapi_segments(self):
        assert _normalise_pattern("/items/{item_id}") == "/items/*"

    def test_collapses_rails_express_segments(self):
        assert _normalise_pattern("/users/:id/posts") == "/users/*/posts"

    def test_strips_trailing_slash_except_root(self):
        assert _normalise_pattern("/api/x/") == "/api/x"
        assert _normalise_pattern("/") == "/"


# ── url matching ────────────────────────────────────────────────────────

class TestUrlMatchesPattern:
    def test_exact_match(self):
        assert _url_matches_pattern("/api/products", "/api/products")

    def test_prefix_under_dynamic_segment(self):
        # client: fetch("/api/users/" + id) → literal prefix
        assert _url_matches_pattern("/api/users/", "/api/users/[id]")

    def test_no_false_positive_on_different_path(self):
        assert not _url_matches_pattern("/api/posts", "/api/users")

    def test_template_wildcard_in_url(self):
        # client: `${API}/api/x` → "*/api/x"
        assert _url_matches_pattern("*/api/x", "*/api/x")


# ── HTTP client detection ───────────────────────────────────────────────

ROUTES = [
    {"pattern": "/api/products", "method": "GET",
     "feature_uuid": "uuid-prod", "file": "src/app/api/products/route.ts"},
    {"pattern": "/api/users/[id]", "method": "GET",
     "feature_uuid": "uuid-users", "file": "src/app/api/users/[id]/route.ts"},
    {"pattern": "/api/orders", "method": "POST",
     "feature_uuid": "uuid-orders", "file": "src/app/api/orders/route.ts"},
]


class TestFindCrossStackHits:
    def test_fetch_literal_matches_route(self):
        hits = find_cross_stack_hits(
            client_file="src/components/Products.tsx",
            client_symbol="Products",
            source_slice='fetch("/api/products")',
            routes_index=ROUTES,
        )
        assert len(hits) == 1
        assert hits[0].route_pattern == "/api/products"
        assert hits[0].route_feature_uuid == "uuid-prod"
        assert hits[0].is_template is False

    def test_axios_get_matches(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol="x",
            source_slice='axios.get("/api/products")',
            routes_index=ROUTES,
        )
        assert len(hits) == 1
        assert hits[0].route_pattern == "/api/products"

    def test_template_interpolation_demotes_confidence(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol=None,
            source_slice='fetch(`${BASE}/api/products`)',
            routes_index=ROUTES,
        )
        assert len(hits) == 1
        assert hits[0].is_template is True

    def test_dynamic_segment_prefix_match(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol=None,
            source_slice='fetch("/api/users/" + userId)',
            routes_index=ROUTES,
        )
        # `fetch("/api/users/" + id)` → literal "/api/users/" → matches /api/users/[id]
        assert len(hits) >= 1
        assert any(h.route_pattern == "/api/users/[id]" for h in hits)

    def test_python_requests_get_matches(self):
        hits = find_cross_stack_hits(
            client_file="client.py", client_symbol="fetch_products",
            source_slice='requests.get("/api/products")',
            routes_index=ROUTES,
        )
        assert len(hits) == 1

    def test_go_http_get_matches(self):
        hits = find_cross_stack_hits(
            client_file="client.go", client_symbol="FetchProducts",
            source_slice='http.Get("/api/products")',
            routes_index=ROUTES,
        )
        assert len(hits) == 1

    def test_no_routes_yields_no_hits(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol=None,
            source_slice='fetch("/api/products")',
            routes_index=[],
        )
        assert hits == []

    def test_external_host_stripped_then_matched(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol=None,
            source_slice='fetch("https://example.com/api/products")',
            routes_index=ROUTES,
        )
        assert len(hits) == 1
        assert hits[0].route_pattern == "/api/products"

    def test_dedups_same_url_same_route(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol=None,
            source_slice='fetch("/api/products"); fetch("/api/products")',
            routes_index=ROUTES,
        )
        # Same (client_file, url, pattern) key → single hit.
        assert len(hits) == 1

    def test_unrelated_path_no_match(self):
        hits = find_cross_stack_hits(
            client_file="x.ts", client_symbol=None,
            source_slice='fetch("/api/health")',
            routes_index=ROUTES,
        )
        assert hits == []
