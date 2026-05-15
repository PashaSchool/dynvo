"""Tests for the JSX nav-component extractor (Sprint 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.jsx_nav import (
    DENSE_LINK_THRESHOLD,
    JsxNavExtractor,
    NavLink,
    collect_nav_links,
)
from faultline.protocols import Extractor


def _w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# ── basic detection ──────────────────────────────────────────────────


def test_extractor_conforms_to_protocol():
    assert isinstance(JsxNavExtractor(), Extractor)


def test_filename_based_detection_picks_up_sidebar(tmp_path):
    _w(tmp_path, "src/components/Sidebar.tsx", '''
        import Link from "next/link";
        export function Sidebar() {
          return (
            <nav>
              <Link href="/dashboard">Dashboard</Link>
              <Link href="/billing">Billing</Link>
            </nav>
          );
        }
    ''')
    out = collect_nav_links(tmp_path)
    assert len(out) == 2
    hrefs = {l.href: l.label for l in out}
    assert hrefs == {"/dashboard": "Dashboard", "/billing": "Billing"}


def test_dense_link_detection_picks_up_non_named_file(tmp_path):
    body = '<div>' + ''.join(
        f'<Link href="/section-{i}">Section {i}</Link>'
        for i in range(DENSE_LINK_THRESHOLD)
    ) + '</div>'
    _w(tmp_path, "src/components/RandomThing.tsx", body)
    out = collect_nav_links(tmp_path)
    assert len(out) == DENSE_LINK_THRESHOLD


def test_skips_files_with_too_few_links_when_not_nav_named(tmp_path):
    """Two links in a non-nav-named file → not enough to qualify."""
    _w(tmp_path, "src/components/Card.tsx", '''
        <div>
          <Link href="/help">Help</Link>
          <Link href="/about">About</Link>
        </div>
    ''')
    assert collect_nav_links(tmp_path) == []


def test_navlink_react_router_style(tmp_path):
    _w(tmp_path, "src/components/MainNav.tsx", '''
        <nav>
          <NavLink to="/users">Users</NavLink>
          <NavLink to="/teams">Teams</NavLink>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    assert {l.label for l in out} == {"Users", "Teams"}


def test_anchor_inside_nav_component(tmp_path):
    """Anchor tags inside a nav component are extracted just like
    Link/NavLink. Marketing routes (/docs, /blog) are filtered, so
    we use product routes here.
    """
    _w(tmp_path, "src/components/Header.tsx", '''
        <header>
          <a href="/dashboard">Dashboard</a>
          <a href="/users">Users</a>
          <a href="/teams">Teams</a>
        </header>
    ''')
    out = collect_nav_links(tmp_path)
    assert len(out) == 3
    assert {l.href for l in out} == {"/dashboard", "/users", "/teams"}


# ── filtering ────────────────────────────────────────────────────────


def test_skips_external_urls(tmp_path):
    _w(tmp_path, "src/components/Sidebar.tsx", '''
        <nav>
          <Link href="https://twitter.com/x">Twitter</Link>
          <Link href="mailto:hi@x.com">Email Us</Link>
          <Link href="/dashboard">Dashboard</Link>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    hrefs = {l.href for l in out}
    assert hrefs == {"/dashboard"}


def test_skips_anchors_and_empty_hrefs(tmp_path):
    _w(tmp_path, "src/components/Topnav.tsx", '''
        <nav>
          <Link href="#section">Anchor</Link>
          <Link href="">Empty</Link>
          <Link href="/">Root</Link>
          <Link href="/real">Real</Link>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    assert {l.href for l in out} == {"/real"}


def test_strips_query_and_hash_from_href(tmp_path):
    _w(tmp_path, "src/components/sidebar.tsx", '''
        <nav>
          <Link href="/billing?utm=foo">Billing</Link>
          <Link href="/users#top">Users</Link>
          <Link href="/teams">Teams</Link>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    hrefs = {l.href for l in out}
    assert hrefs == {"/billing", "/users", "/teams"}


def test_skips_node_modules(tmp_path):
    _w(tmp_path, "node_modules/some-pkg/Sidebar.tsx", '''
        <nav>
          <Link href="/x">X</Link>
          <Link href="/y">Y</Link>
          <Link href="/z">Z</Link>
        </nav>
    ''')
    assert collect_nav_links(tmp_path) == []


def test_skips_test_dirs(tmp_path):
    _w(tmp_path, "tests/Sidebar.test.tsx", '''
        <nav>
          <Link href="/x">X</Link>
          <Link href="/y">Y</Link>
          <Link href="/z">Z</Link>
        </nav>
    ''')
    assert collect_nav_links(tmp_path) == []


# ── empty / icon-only labels ─────────────────────────────────────────


def test_label_left_empty_when_link_uses_child_component(tmp_path):
    """When the label is a child component (icon, complex JSX), the
    extractor records an empty label rather than a wrong one. The
    aggregator can fall back to the route segment.
    """
    _w(tmp_path, "src/components/Sidebar.tsx", '''
        <nav>
          <Link href="/dashboard"><Icon name="home" /></Link>
          <Link href="/billing"><BillingIcon /></Link>
          <Link href="/settings">Settings</Link>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    by_href = {l.href: l.label for l in out}
    assert by_href.get("/settings") == "Settings"
    # Icon-child links produce an empty label — caller can salvage from href.
    assert by_href.get("/dashboard") == ""
    assert by_href.get("/billing") == ""


# ── extractor wrapper ────────────────────────────────────────────────


def test_extractor_emits_nav_link_signals(tmp_path):
    _w(tmp_path, "src/components/Sidebar.tsx", '''
        <nav>
          <Link href="/dashboard">Dashboard</Link>
          <Link href="/billing">Billing</Link>
        </nav>
    ''')
    sigs = JsxNavExtractor().extract(tmp_path, files=())
    assert all(s.kind == "nav-link" for s in sigs)
    assert all(s.source == "jsx-nav-extractor" for s in sigs)
    payloads = [s.payload for s in sigs]
    assert {p["label"] for p in payloads} == {"Dashboard", "Billing"}


def test_extractor_applicable_false_on_non_react_repo(tmp_path):
    (tmp_path / "src" / "main.py").parent.mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    assert JsxNavExtractor().applicable(tmp_path) is False


def test_skips_marketing_nav_routes(tmp_path):
    """Privacy / Terms / Careers / Pricing are SITE SHELL, not
    product features. Should be filtered out so they don't pollute
    the recall critique with phantom expectations.
    """
    _w(tmp_path, "src/components/Footer.tsx", '''
        <nav>
          <Link href="/privacy">Privacy Policy</Link>
          <Link href="/terms">Terms of Service</Link>
          <Link href="/careers">Careers</Link>
          <Link href="/pricing">Pricing</Link>
          <Link href="/dashboard">Dashboard</Link>
          <Link href="/billing">Billing</Link>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    hrefs = {l.href for l in out}
    assert hrefs == {"/dashboard", "/billing"}


def test_skips_marketing_label_even_when_route_unfamiliar(tmp_path):
    """Some sites route legal pages oddly, but the label still
    gives them away.
    """
    _w(tmp_path, "src/components/Sidebar.tsx", '''
        <nav>
          <Link href="/info/x">Privacy Policy</Link>
          <Link href="/info/y">Get Started</Link>
          <Link href="/dashboard">Dashboard</Link>
        </nav>
    ''')
    out = collect_nav_links(tmp_path)
    hrefs = {l.href for l in out}
    assert hrefs == {"/dashboard"}


def test_extractor_applicable_true_when_react_present(tmp_path):
    _w(tmp_path, "src/App.tsx", "export default () => <div/>;")
    assert JsxNavExtractor().applicable(tmp_path) is True
