"""Tests for ``faultline.analyzer.marketing_fetcher`` (Sprint E1).

Pure unit tests — no network. ``fetch_page_text`` is monkey-patched
where needed. Discovery + HTML parsing are exercised directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.analyzer.marketing_fetcher import (
    discover_marketing_site,
    extract_docs_sidebar_taxonomy,
    extract_product_taxonomy,
    fetch_llms_txt_urls,
    parse_llms_txt,
)


def _write_pkg_json(repo: Path, data: dict) -> None:
    (repo / "package.json").write_text(json.dumps(data), encoding="utf-8")


def test_discover_marketing_site_prefers_homepage(tmp_path: Path) -> None:
    _write_pkg_json(tmp_path, {"name": "x", "homepage": "https://example.dev"})
    assert discover_marketing_site(tmp_path) == "https://example.dev"


def test_discover_marketing_site_strips_trailing_slash(tmp_path: Path) -> None:
    _write_pkg_json(tmp_path, {"name": "x", "homepage": "https://foo.io/"})
    assert discover_marketing_site(tmp_path) == "https://foo.io"


def test_discover_marketing_site_skips_github_repository(tmp_path: Path) -> None:
    # When homepage is absent and repository points at github, we
    # should NOT return that as a marketing URL.
    _write_pkg_json(tmp_path, {
        "name": "x",
        "repository": {"url": "git+https://github.com/owner/repo.git"},
    })
    assert discover_marketing_site(tmp_path) is None


def test_discover_marketing_site_returns_none_without_package_json(
    tmp_path: Path,
) -> None:
    assert discover_marketing_site(tmp_path) is None


def test_discover_marketing_site_handles_invalid_url(tmp_path: Path) -> None:
    _write_pkg_json(tmp_path, {"name": "x", "homepage": "not-a-url"})
    assert discover_marketing_site(tmp_path) is None


def test_extract_product_taxonomy_pulls_headings() -> None:
    html = """
    <html><body>
      <h1>Welcome</h1>
      <h2>HTTP Uptime Monitoring</h2>
      <h2>Multi-Region Probing</h2>
      <h3>Status Page Builder</h3>
      <h3>Incident Management</h3>
      <h2>Pricing</h2>
      <h2>Sign Up</h2>
    </body></html>
    """
    candidates, conf = extract_product_taxonomy(html)
    assert "HTTP Uptime Monitoring" in candidates
    assert "Multi-Region Probing" in candidates
    assert "Status Page Builder" in candidates
    assert "Incident Management" in candidates
    # Filtered as nav chrome:
    assert "Pricing" not in candidates
    assert "Sign Up" not in candidates
    assert conf >= 0.5


def test_extract_product_taxonomy_filters_single_word_labels() -> None:
    html = "<h2>Auth</h2><h2>Privacy Policy</h2>"
    candidates, _conf = extract_product_taxonomy(html)
    # "Auth" is single-word → rejected; "Privacy Policy" is in
    # _GENERIC_NAV_WORDS-adjacent? Actually "Privacy Policy" is not
    # in the nav set but starts with a generic — check what survives.
    assert "Auth" not in candidates


def test_extract_product_taxonomy_returns_zero_on_empty_html() -> None:
    candidates, conf = extract_product_taxonomy("")
    assert candidates == []
    assert conf == 0.0


def test_extract_product_taxonomy_dedupes() -> None:
    html = """
    <h2>Document Sharing</h2>
    <h2>Document Sharing</h2>
    <h3>Document Sharing</h3>
    <h2>Access Control</h2>
    """
    candidates, _conf = extract_product_taxonomy(html)
    assert candidates.count("Document Sharing") == 1
    assert "Access Control" in candidates


def test_extract_product_taxonomy_caps_at_30() -> None:
    headings = "\n".join(
        f"<h2>Feature Number {i:02d}</h2>"
        for i in range(50)
    )
    candidates, _conf = extract_product_taxonomy(headings)
    assert len(candidates) <= 30


def test_extract_product_taxonomy_confidence_tiers() -> None:
    # 1 acceptable label → confidence 0.5
    html_low = "<h2>Single Feature Label</h2>"
    _c, conf_low = extract_product_taxonomy(html_low)
    assert conf_low == 0.5

    # 4 acceptable labels → 0.75
    html_mid = "\n".join(
        f"<h2>Distinct Feature {i}</h2>" for i in range(4)
    )
    _c, conf_mid = extract_product_taxonomy(html_mid)
    assert conf_mid == 0.75

    # 8 acceptable labels → 0.9
    html_hi = "\n".join(
        f"<h2>Distinct Feature {i}</h2>" for i in range(8)
    )
    _c, conf_hi = extract_product_taxonomy(html_hi)
    assert conf_hi == 0.9


def test_extract_product_taxonomy_includes_sidebar_li_links() -> None:
    html = """
    <nav>
      <ul>
        <li><a href="/docs/dashboards">Custom Dashboards</a></li>
        <li><a href="/docs/alerts">Alert Routing</a></li>
        <li><a href="/login">Log In</a></li>
      </ul>
    </nav>
    """
    candidates, _conf = extract_product_taxonomy(html)
    assert "Custom Dashboards" in candidates
    assert "Alert Routing" in candidates
    assert "Log In" not in candidates


def test_discover_marketing_site_falls_back_to_nested_workspace_package(
    tmp_path: Path,
) -> None:
    # Root package.json has no homepage, but apps/web/package.json does.
    _write_pkg_json(tmp_path, {"name": "monorepo-root"})
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "package.json").write_text(
        json.dumps({"name": "web", "homepage": "https://customer-facing.dev"}),
        encoding="utf-8",
    )
    assert discover_marketing_site(tmp_path) == "https://customer-facing.dev"


def test_discover_marketing_site_scans_packages_dir(tmp_path: Path) -> None:
    _write_pkg_json(tmp_path, {"name": "monorepo-root"})
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "package.json").write_text(
        json.dumps({"name": "core", "homepage": "https://product.io"}),
        encoding="utf-8",
    )
    assert discover_marketing_site(tmp_path) == "https://product.io"


def test_extract_product_taxonomy_rejects_all_caps_marketing() -> None:
    html = "<h2>BUY NOW LIMITED OFFER</h2><h2>Survey Templates</h2>"
    candidates, _conf = extract_product_taxonomy(html)
    assert "BUY NOW LIMITED OFFER" not in candidates
    assert "Survey Templates" in candidates


# ── M1 — additional coverage ────────────────────────────────────────────


def test_github_regex_allows_dots_in_repo_name() -> None:
    """Repo names with dots (cal.com, trigger.dev) must parse."""
    from faultline.analyzer.marketing_fetcher import _parse_github_owner_repo

    assert _parse_github_owner_repo(
        "https://github.com/calcom/cal.com.git"
    ) == ("calcom", "cal.com")
    assert _parse_github_owner_repo(
        "https://github.com/triggerdotdev/trigger.dev"
    ) == ("triggerdotdev", "trigger.dev")
    # Sanity — non-dotted names still work
    assert _parse_github_owner_repo(
        "https://github.com/go-chi/chi.git"
    ) == ("go-chi", "chi")


def test_discover_uses_domain_shape_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a repo named ``cal.com``, ``https://cal.com/`` should be
    preferred over the GH API homepage when it serves usable HTML."""
    # Simulate a repo with no package.json, only a git remote.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/calcom/cal.com.git\n',
        encoding="utf-8",
    )

    fetched_urls: list[str] = []

    def fake_fetch(url: str, *, timeout_s: int = 15) -> str | None:
        fetched_urls.append(url)
        if url == "https://cal.com":
            # 5KB of body — passes the ≥4KB usable-HTML threshold
            return "<html><body>" + "x" * 5000 + "</body></html>"
        if url.startswith("https://api.github.com"):
            return '{"homepage": "https://cal.diy"}'
        return None

    monkeypatch.setattr(
        "faultline.analyzer.marketing_fetcher.fetch_page_text", fake_fetch,
    )
    result = discover_marketing_site(tmp_path)
    assert result == "https://cal.com"
    # Domain-shape preflight should fire BEFORE the GH API call.
    assert fetched_urls[0] == "https://cal.com"


def test_discover_falls_back_to_github_homepage_when_domain_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/owner/notadomain.git\n',
        encoding="utf-8",
    )

    def fake_fetch(url: str, *, timeout_s: int = 15) -> str | None:
        if url.startswith("https://api.github.com"):
            return '{"homepage": "https://product.io"}'
        return None

    monkeypatch.setattr(
        "faultline.analyzer.marketing_fetcher.fetch_page_text", fake_fetch,
    )
    # ``notadomain`` is not a domain-shape so preflight is skipped.
    assert discover_marketing_site(tmp_path) == "https://product.io"


def test_extract_taxonomy_rejects_pitch_verb_prefix() -> None:
    """Imperative-verb-prefixed H2s are pitch sentences, not labels."""
    html = (
        "<h2>Turn Slack conversations into trackable work</h2>"
        "<h2>Wrangle product chaos</h2>"
        "<h2>Survey Templates</h2>"
    )
    candidates, _conf = extract_product_taxonomy(html)
    assert "Survey Templates" in candidates
    assert all("Turn" not in c for c in candidates)
    assert all("Wrangle" not in c for c in candidates)


def test_fetch_sitemap_urls_parses_loc_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultline.analyzer.marketing_fetcher import fetch_sitemap_urls

    sitemap_xml = (
        '<?xml version="1.0"?>'
        '<urlset>'
        '<url><loc>https://example.com/</loc></url>'
        '<url><loc>https://example.com/features</loc></url>'
        '<url><loc>https://example.com/pricing</loc></url>'
        '<url><loc>https://example.com/blog/post-1</loc></url>'
        '<url><loc>https://other-host.com/spam</loc></url>'
        '</urlset>'
    )

    def fake_fetch(url: str, *, timeout_s: int = 10) -> str | None:
        if url == "https://example.com/sitemap.xml":
            return sitemap_xml
        return None

    monkeypatch.setattr(
        "faultline.analyzer.marketing_fetcher.fetch_page_text", fake_fetch,
    )
    urls = fetch_sitemap_urls("https://example.com")
    # Includes same-host entries, excludes blog noise + cross-host
    assert "https://example.com/" in urls
    assert "https://example.com/features" in urls
    assert "https://example.com/pricing" in urls
    assert all("/blog/" not in u for u in urls)
    assert all("other-host.com" not in u for u in urls)


def test_rank_sitemap_urls_prefers_product_keywords() -> None:
    from faultline.analyzer.marketing_fetcher import (
        rank_sitemap_urls_by_product_likelihood,
    )

    urls = [
        "https://example.com/about",
        "https://example.com/features",
        "https://example.com/pricing",
        "https://example.com/team",
    ]
    ranked = rank_sitemap_urls_by_product_likelihood(urls)
    # /features and /pricing must come before /about and /team
    assert ranked[0] in {
        "https://example.com/features",
        "https://example.com/pricing",
    }
    assert ranked[1] in {
        "https://example.com/features",
        "https://example.com/pricing",
    }


def test_sitemap_index_expanded_one_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sitemap-index style — first <loc>s point at nested sitemaps."""
    from faultline.analyzer.marketing_fetcher import fetch_sitemap_urls

    index_xml = (
        '<sitemapindex>'
        '<sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>'
        '<sitemap><loc>https://example.com/sitemap-blog.xml</loc></sitemap>'
        '</sitemapindex>'
    )
    nested_xml = (
        '<urlset>'
        '<url><loc>https://example.com/features</loc></url>'
        '<url><loc>https://example.com/docs</loc></url>'
        '</urlset>'
    )

    def fake_fetch(url: str, *, timeout_s: int = 10) -> str | None:
        if url == "https://example.com/sitemap.xml":
            return index_xml
        if url == "https://example.com/sitemap-pages.xml":
            return nested_xml
        return None

    monkeypatch.setattr(
        "faultline.analyzer.marketing_fetcher.fetch_page_text", fake_fetch,
    )
    urls = fetch_sitemap_urls("https://example.com")
    assert "https://example.com/features" in urls
    assert "https://example.com/docs" in urls


# ── Sprint v7-A additions ───────────────────────────────────────────────


def test_fetch_llms_txt_urls_emits_canonical_order() -> None:
    urls = fetch_llms_txt_urls("https://better-auth.com")
    # llms-full.txt comes first — it's the comprehensive expansion.
    assert urls[0] == "https://better-auth.com/llms-full.txt"
    assert urls[1] == "https://better-auth.com/llms.txt"
    # docs.<host> variants follow the primary host.
    assert "https://docs.better-auth.com/llms.txt" in urls
    assert "https://docs.better-auth.com/llms-full.txt" in urls
    # No duplicates.
    assert len(urls) == len(set(urls))


def test_fetch_llms_txt_urls_skips_docs_when_already_docs_host() -> None:
    urls = fetch_llms_txt_urls("https://docs.example.com")
    # Should NOT generate docs.docs.example.com
    assert all("docs.docs." not in u for u in urls)


def test_fetch_llms_txt_urls_returns_empty_on_invalid() -> None:
    assert fetch_llms_txt_urls("") == []
    assert fetch_llms_txt_urls("not-a-url") == []


def test_parse_llms_txt_rollup_collapses_many_siblings() -> None:
    """35-OAuth-providers shape — many sibling bullets under one section
    means the section name IS the user-facing capability."""
    text = """
# Better Auth

The most comprehensive auth framework.

## OAuth Providers

- Apple
- Atlassian
- Discord
- Dropbox
- Facebook
- GitHub
- GitLab
- Google
- Kakao
- LinkedIn

## Database Adapters

- Drizzle Adapter
- Kysely Adapter
""".strip()
    labels, conf = parse_llms_txt(text)
    # Section with many bullets → only header emits.
    assert "OAuth Providers" in labels
    # Individual provider names get rolled up (NOT emitted).
    assert "Apple" not in labels
    assert "Google" not in labels
    # Section with few bullets → leaves come through.
    assert "Database Adapters" in labels or "Drizzle Adapter" in labels
    assert conf >= 0.6


def test_parse_llms_txt_emits_leaves_when_section_sparse() -> None:
    text = """
## Plugins

- Magic Link
- Two Factor

## Other Section

- Email OTP
""".strip()
    labels, _conf = parse_llms_txt(text)
    # ≤3 bullets per section → leaves come through.
    assert "Magic Link" in labels
    assert "Two Factor" in labels
    assert "Email OTP" in labels


def test_parse_llms_txt_returns_empty_on_blank() -> None:
    labels, conf = parse_llms_txt("")
    assert labels == []
    assert conf == 0.0


def test_parse_llms_txt_accepts_inline_links() -> None:
    text = """
## API Endpoints

- [User Management](/docs/api/users)
- [Session Tracking](/docs/api/sessions)
- [Organization Auth](/docs/api/orgs)
- [Two-Factor Auth](/docs/api/2fa)
""".strip()
    labels, _conf = parse_llms_txt(text)
    # 4+ bullets → rolls up to section.
    assert "API Endpoints" in labels


def test_extract_docs_sidebar_taxonomy_collapses_many_siblings() -> None:
    html = """
<aside class="sidebar">
  <h3>OAuth Providers</h3>
  <a href="/p/apple">Apple</a>
  <a href="/p/atlassian">Atlassian</a>
  <a href="/p/discord">Discord</a>
  <a href="/p/dropbox">Dropbox</a>
  <a href="/p/facebook">Facebook</a>
  <a href="/p/github">GitHub</a>
  <a href="/p/google">Google</a>
  <h3>Quick Links</h3>
  <a href="/q/getting-started">Getting Started</a>
  <a href="/q/installation">Installation</a>
</aside>
"""
    labels, conf = extract_docs_sidebar_taxonomy(html)
    assert "OAuth Providers" in labels
    # Sparse section bullets come through; provider names roll up.
    assert "Apple" not in labels
    assert conf >= 0.5


def test_extract_docs_sidebar_taxonomy_handles_nextra_nav() -> None:
    """Nextra-style sidebar uses <nav> with class containing 'nav'."""
    html = """
<nav class="nx-docs-nav">
  <h4>Authentication</h4>
  <a href="/a/email">Email Auth</a>
  <a href="/a/oauth">OAuth Auth</a>
</nav>
"""
    labels, _conf = extract_docs_sidebar_taxonomy(html)
    # Sparse (2 anchors) → leaves come through.
    assert "Authentication" in labels or "Email Auth" in labels


def test_extract_docs_sidebar_taxonomy_returns_empty_on_no_sidebar() -> None:
    html = "<html><body><h1>Some Page</h1><p>No sidebar at all.</p></body></html>"
    labels, conf = extract_docs_sidebar_taxonomy(html)
    assert labels == []
    assert conf == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
