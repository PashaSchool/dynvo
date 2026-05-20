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
    extract_product_taxonomy,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
