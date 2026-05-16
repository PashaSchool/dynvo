"""Tests for ``faultline.extractors.route_segment.RouteSegmentExtractor``
(Sprint 9b).

Documents the leaf-segment derivation across Remix flat-routes,
Next App Router and Next Pages Router. Synthetic repos via
``tmp_path`` only.
"""

from __future__ import annotations

from pathlib import Path

from faultline.extractors.route_segment import RouteSegmentExtractor


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("// stub\n")


# ── applicable() ─────────────────────────────────────────────────────


def test_applicable_false_on_empty_repo(tmp_path):
    assert RouteSegmentExtractor().applicable(tmp_path) is False


def test_applicable_true_when_apps_dir_present(tmp_path):
    (tmp_path / "apps").mkdir()
    assert RouteSegmentExtractor().applicable(tmp_path) is True


def test_applicable_true_when_app_dir_present(tmp_path):
    (tmp_path / "app").mkdir()
    assert RouteSegmentExtractor().applicable(tmp_path) is True


def test_applicable_true_when_pages_dir_present(tmp_path):
    (tmp_path / "pages").mkdir()
    assert RouteSegmentExtractor().applicable(tmp_path) is True


# ── extract() — Next App Router ──────────────────────────────────────


def test_next_app_router_route_group_leaf(tmp_path):
    _touch(tmp_path / "app" / "(dashboard)" / "billing" / "page.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "billing" in slugs


def test_next_app_router_under_apps_workspace(tmp_path):
    _touch(tmp_path / "apps" / "web" / "app" / "(marketing)" / "pricing" / "page.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "pricing" in slugs


def test_next_app_router_skips_layout_only(tmp_path):
    _touch(tmp_path / "app" / "(dashboard)" / "layout.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    # layout.tsx doesn't match _NEXT_APP_PAGE_RE (requires /page.<ext>)
    assert out == []


# ── extract() — Next Pages Router ────────────────────────────────────


def test_next_pages_router_leaf(tmp_path):
    _touch(tmp_path / "pages" / "settings.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "settings" in slugs


def test_next_pages_router_skips_app_scaffolding(tmp_path):
    _touch(tmp_path / "pages" / "_app.tsx")
    _touch(tmp_path / "pages" / "_document.tsx")
    _touch(tmp_path / "pages" / "404.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    assert out == []


# ── extract() — Remix flat-routes ────────────────────────────────────


def test_remix_flat_route_leaf(tmp_path):
    _touch(tmp_path / "apps" / "remix" / "app" / "routes" / "_authenticated+" / "documents._index.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "documents" in slugs


def test_remix_dynamic_param_stripped(tmp_path):
    """``$teamUrl`` is a route param, not a feature."""
    _touch(tmp_path / "apps" / "remix" / "app" / "routes" / "_authenticated+" / "t.$teamUrl+" / "inbox.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "inbox" in slugs


# ── dedup, deeply nested, empty ──────────────────────────────────────


def test_dedup_on_same_slug_across_paths(tmp_path):
    _touch(tmp_path / "app" / "(dashboard)" / "settings" / "page.tsx")
    _touch(tmp_path / "app" / "(marketing)" / "settings" / "page.tsx")
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = [s.payload["slug"].lower() for s in out]
    assert slugs.count("settings") == 1


def test_deeply_nested_remix_route(tmp_path):
    _touch(
        tmp_path
        / "apps" / "remix" / "app" / "routes"
        / "_authenticated+" / "settings+" / "team+"
        / "members.tsx"
    )
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "members" in slugs


def test_empty_repo_returns_empty(tmp_path):
    (tmp_path / "app").mkdir()
    assert RouteSegmentExtractor().extract(tmp_path) == []


def test_skip_dirs_are_not_walked(tmp_path):
    _touch(tmp_path / "node_modules" / "x" / "app" / "evil" / "page.tsx")
    _touch(tmp_path / ".next" / "app" / "evil" / "page.tsx")
    (tmp_path / "app").mkdir(exist_ok=True)
    out = RouteSegmentExtractor().extract(tmp_path)
    slugs = {s.payload["slug"] for s in out}
    assert "evil" not in slugs


def test_extractor_name_attribute_present():
    assert RouteSegmentExtractor.name == "route-segment-extractor"
