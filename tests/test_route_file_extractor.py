"""Tests for the Next.js App Router route-file extractor (Phase 3b PoC).

Fixture (tests/fixtures/tiny_nextjs_app/):
  app/page.tsx                                → flow: "/"
  app/layout.tsx                              → kind=layout
  app/(marketing)/pricing/page.tsx            → flow: "/pricing", parent=(marketing)
  app/(dashboard)/billing/page.tsx            → flow: "/billing", parent=(dashboard)
  app/(dashboard)/settings/[tab]/page.tsx     → flow: "/settings/:tab"
  app/api/webhooks/stripe/route.ts            → api: GET + POST exports
  app/[locale]/about/page.tsx                 → flow: "/:locale/about"
  app/_internal/helper.ts                     → SKIPPED (private)
"""

from __future__ import annotations

from pathlib import Path

from faultline.extractors.route_file import (
    NextRouteFileExtractor,
    collect_routes,
    is_nextjs_app_router,
)
from faultline.protocols import Extractor


REPO = Path(__file__).parent / "fixtures" / "tiny_nextjs_app"


def test_is_nextjs_app_router_detects_fixture():
    assert is_nextjs_app_router(REPO)


def test_is_nextjs_app_router_negative_when_no_app_dir(tmp_path):
    assert not is_nextjs_app_router(tmp_path)


def test_extractor_satisfies_protocol():
    e = NextRouteFileExtractor()
    assert isinstance(e, Extractor)


def test_collect_routes_finds_all_pages_and_api():
    routes = collect_routes(REPO)
    by_path = {r.url_path: r for r in routes}
    # All page URLs we expect
    assert "/" in by_path
    assert "/pricing" in by_path
    assert "/billing" in by_path
    assert "/settings/:tab" in by_path
    assert "/:locale/about" in by_path
    # API route URL
    assert "/api/webhooks/stripe" in by_path


def test_route_groups_become_parent_hint_not_url():
    routes = collect_routes(REPO)
    by_path = {r.url_path: r for r in routes}
    assert by_path["/billing"].parent_hint == "(dashboard)"
    assert by_path["/pricing"].parent_hint == "(marketing)"
    # Root page has no parent group
    assert by_path["/"].parent_hint is None


def test_dynamic_segments_become_colon_placeholders():
    routes = collect_routes(REPO)
    by_path = {r.url_path: r for r in routes}
    assert "/settings/:tab" in by_path
    assert "/:locale/about" in by_path


def test_api_route_methods_extracted_from_exports():
    routes = collect_routes(REPO)
    api = next(r for r in routes if r.url_path == "/api/webhooks/stripe")
    assert api.kind == "api"
    assert "GET" in api.methods
    assert "POST" in api.methods
    assert api.methods[0] in ("GET", "POST")


def test_private_folders_are_skipped():
    routes = collect_routes(REPO)
    paths = [r.handler_file for r in routes]
    assert not any("_internal" in p for p in paths)


def test_layout_emitted_with_kind_layout():
    routes = collect_routes(REPO)
    layouts = [r for r in routes if r.kind == "layout"]
    assert len(layouts) >= 1
    assert any(l.handler_file.endswith("layout.tsx") for l in layouts)


def test_extractor_emits_signal_per_route():
    e = NextRouteFileExtractor()
    signals = e.extract(REPO, files=[])
    assert all(s.kind == "route" for s in signals)
    assert all(s.source.startswith("route-file-extractor") for s in signals)
    assert all(s.payload.get("framework") == "nextjs-app-router" for s in signals)
    # API + at least 5 page routes + 1 layout = >=7 signals
    assert len(signals) >= 7


def test_signal_payload_contains_required_fields():
    e = NextRouteFileExtractor()
    signals = e.extract(REPO, files=[])
    page_signal = next(s for s in signals if s.payload.get("path") == "/billing")
    assert page_signal.payload["handler_file"].endswith("billing/page.tsx")
    assert page_signal.payload["parent_hint"] == "(dashboard)"
    assert page_signal.payload["kind"] == "page"


def test_api_signal_lists_all_methods():
    e = NextRouteFileExtractor()
    signals = e.extract(REPO, files=[])
    api_sig = next(s for s in signals if s.payload.get("path") == "/api/webhooks/stripe")
    methods = api_sig.payload["methods"]
    assert "GET" in methods
    assert "POST" in methods


def test_build_route_hints_block_empty_for_non_nextjs(tmp_path):
    from faultline.extractors.route_file import build_route_hints_block
    assert build_route_hints_block(tmp_path) == ""


def test_build_route_hints_block_contains_groups_and_routes():
    from faultline.extractors.route_file import build_route_hints_block
    block = build_route_hints_block(REPO)
    assert "ROUTING-HINT" in block
    assert "Next.js App Router" in block
    # Both maintainer-declared groups should appear
    assert "(marketing)" in block
    assert "(dashboard)" in block
    # API + page lines
    assert "/api/webhooks/stripe" in block
    assert "/billing" in block
    # Method labels for API
    assert "GET" in block and "POST" in block
    # Top-level routes (root page) labelled separately
    assert "Top-level routes" in block


def test_build_route_hints_block_excludes_layouts():
    from faultline.extractors.route_file import build_route_hints_block
    block = build_route_hints_block(REPO)
    # layout.tsx exists in fixture but should NOT appear in hints
    # (framework files don't help feature clustering)
    assert "layout.tsx" not in block
    assert "kind: layout" not in block


def test_route_hints_budget_truncates_with_marker():
    """Hints over budget should truncate gracefully."""
    from faultline.extractors.route_file import build_route_hints_block
    # Tiny budget forces truncation; PoC fixture has ~8 routes
    block = build_route_hints_block(REPO, budget_chars=200)
    assert len(block) <= 350   # close to budget plus footer
    assert "ROUTING-HINT" in block


def test_is_route_hints_enabled_off_by_default(monkeypatch):
    monkeypatch.delenv("FAULTLINE_ROUTE_HINTS", raising=False)
    from faultline.extractors.route_file import is_route_hints_enabled
    assert is_route_hints_enabled() is False


def test_is_route_hints_enabled_on(monkeypatch):
    from faultline.extractors.route_file import is_route_hints_enabled
    monkeypatch.setenv("FAULTLINE_ROUTE_HINTS", "1")
    assert is_route_hints_enabled() is True
    monkeypatch.setenv("FAULTLINE_ROUTE_HINTS", "true")
    assert is_route_hints_enabled() is True
    monkeypatch.setenv("FAULTLINE_ROUTE_HINTS", "0")
    assert is_route_hints_enabled() is False
