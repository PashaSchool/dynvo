"""Tests for the product-string evidence collector (naming-evidence core).

Covers the three structural sources (i18n catalogs, nav/sidebar
registries, route/page titles), the HARD README exclusion, anchor-first
bundle assembly, the structural caps, and determinism.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.product_strings import (
    ProductStringIndex,
    collect_product_strings,
)


def _write(repo: Path, rel: str, content: str) -> str:
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return rel


# ── i18n catalogs ───────────────────────────────────────────────────────


def test_i18n_default_locale_json_collected(tmp_path: Path) -> None:
    rel = _write(tmp_path, "apps/web/locales/en.json", json.dumps({
        "billing": {"title": "Billing & Invoices", "cta": "Upgrade plan"},
        "nav": {"docs": "Documentation"},
    }))
    idx = collect_product_strings(tmp_path, [rel])
    texts = [r.text for r in idx.strings_for_file(rel)]
    assert "Billing & Invoices" in texts
    assert "Upgrade plan" in texts
    assert "Documentation" in texts
    assert all(r.source == "i18n" for r in idx.strings_for_file(rel))


def test_i18n_non_default_locale_skipped(tmp_path: Path) -> None:
    fr = _write(tmp_path, "locales/fr.json", json.dumps({"x": "Facturation"}))
    de = _write(tmp_path, "locales/de/common.json", json.dumps({"x": "Abrechnung"}))
    idx = collect_product_strings(tmp_path, [fr, de])
    assert idx.total_strings == 0


def test_i18n_en_subdir_collected(tmp_path: Path) -> None:
    rel = _write(tmp_path, "i18n/en/common.json", json.dumps({"k": "Data Rooms"}))
    idx = collect_product_strings(tmp_path, [rel])
    assert [r.text for r in idx.strings_for_file(rel)] == ["Data Rooms"]


def test_i18n_ts_locale_module_collected(tmp_path: Path) -> None:
    rel = _write(tmp_path, "src/i18n/en.ts", (
        "export default {\n"
        "  dashboard: 'Team Dashboard',\n"
        '  settings: "Workspace Settings",\n'
        "}\n"
    ))
    idx = collect_product_strings(tmp_path, [rel])
    texts = [r.text for r in idx.strings_for_file(rel)]
    assert "Team Dashboard" in texts
    assert "Workspace Settings" in texts


def test_i18n_path_like_values_filtered(tmp_path: Path) -> None:
    rel = _write(tmp_path, "locales/en.json", json.dumps({
        "url": "https://example.com",
        "route": "/settings/billing",
        "real": "Reset password",
    }))
    idx = collect_product_strings(tmp_path, [rel])
    texts = [r.text for r in idx.strings_for_file(rel)]
    assert texts == ["Reset password"]


# ── README / prose hard exclusion ───────────────────────────────────────


def test_readme_strings_never_enter_any_bundle(tmp_path: Path) -> None:
    # Even when README.md is explicitly passed as a candidate member
    # file, none of its content may enter the index or a bundle
    # (rule-no-readme; founder decision excluding the reviewer's README
    # suggestion).
    readme = _write(tmp_path, "README.md", (
        "# SuperProduct\n\nZero-Trust Quantum Sync for Enterprises\n"
    ))
    nav = _write(tmp_path, "src/components/Sidebar.tsx", (
        "export const items = [\n"
        "  { label: 'Bookings', href: '/bookings' },\n"
        "]\n"
    ))
    idx = collect_product_strings(tmp_path, [readme, nav])
    assert readme not in idx.by_file
    bundle = idx.bundle_for([readme, nav])
    assert all("Quantum" not in s and "SuperProduct" not in s for s in bundle)
    assert any("Bookings" in s for s in bundle)


def test_prose_docs_excluded_before_io(tmp_path: Path) -> None:
    for rel in ("docs/guide.mdx", "CHANGELOG.md", "notes.txt", "spec.rst"):
        _write(tmp_path, rel, "Marketing Magic Words")
    idx = collect_product_strings(
        tmp_path, ["docs/guide.mdx", "CHANGELOG.md", "notes.txt", "spec.rst"],
    )
    assert idx.total_strings == 0


# ── Nav / sidebar registries ────────────────────────────────────────────


def test_nav_object_literal_labels_with_href(tmp_path: Path) -> None:
    rel = _write(tmp_path, "apps/web/components/Sidebar.tsx", (
        "const navItems = [\n"
        "  { label: 'Documents', href: '/documents' },\n"
        "  { label: 'Templates', href: '/templates' },\n"
        "]\n"
        "export function Sidebar() { return <nav>{...}</nav> }\n"
    ))
    idx = collect_product_strings(tmp_path, [rel])
    texts = [r.text for r in idx.strings_for_file(rel)]
    assert "Documents (/documents)" in texts
    assert "Templates (/templates)" in texts
    assert all(r.source == "nav" for r in idx.strings_for_file(rel))


def test_nav_jsx_link_pairs(tmp_path: Path) -> None:
    # Content signature (<nav>) triggers collection even when the
    # basename is not nav-ish.
    rel = _write(tmp_path, "src/Shell.tsx", (
        "<nav>\n"
        '  <Link href="/teams">Teams</Link>\n'
        '  <a href="/api-keys">API Keys</a>\n'
        "</nav>\n"
    ))
    idx = collect_product_strings(tmp_path, [rel])
    texts = [r.text for r in idx.strings_for_file(rel)]
    assert "Teams (/teams)" in texts
    assert "API Keys (/api-keys)" in texts


def test_non_nav_code_file_contributes_nothing(tmp_path: Path) -> None:
    rel = _write(tmp_path, "src/lib/math.ts", "export const add = (a,b)=>a+b\n")
    idx = collect_product_strings(tmp_path, [rel])
    assert idx.total_strings == 0


# ── Route / page titles ─────────────────────────────────────────────────


def test_metadata_title_and_h1_collected(tmp_path: Path) -> None:
    rel = _write(tmp_path, "apps/web/app/settings/page.tsx", (
        "export const metadata = { title: 'Workspace Settings' }\n"
        "export default function Page() {\n"
        "  return <main><h1>Manage your workspace</h1></main>\n"
        "}\n"
    ))
    idx = collect_product_strings(tmp_path, [rel])
    texts = [r.text for r in idx.strings_for_file(rel)]
    assert "Workspace Settings" in texts
    assert "Manage your workspace" in texts
    assert all(r.source == "title" for r in idx.strings_for_file(rel))


def test_title_tag_collected(tmp_path: Path) -> None:
    rel = _write(tmp_path, "pages/pricing.tsx", (
        "<Head><title>Pricing Plans</title></Head>\n"
    ))
    idx = collect_product_strings(tmp_path, [rel])
    assert "Pricing Plans" in [r.text for r in idx.strings_for_file(rel)]


def test_title_key_outside_metadata_context_ignored(tmp_path: Path) -> None:
    # A ``title:`` key in a random config object (no metadata /
    # generateMetadata / <Head> / <title> signature) must not leak in.
    rel = _write(tmp_path, "app/charts/page.tsx", (
        "const chartConfig = { titleX: 1 }\n"
        "export default function P() { return <div/> }\n"
    ))
    idx = collect_product_strings(tmp_path, [rel])
    assert idx.total_strings == 0


# ── Bundle assembly ─────────────────────────────────────────────────────


def _index_two_files(tmp_path: Path) -> tuple[ProductStringIndex, str, str]:
    anchor = _write(tmp_path, "app/billing/page.tsx", (
        "export const metadata = { title: 'Billing' }\n"
    ))
    closure = _write(tmp_path, "components/MainNav.tsx", (
        "const items = [{ label: 'Settings', href: '/settings' }]\n"
        "<nav/>\n"
    ))
    return collect_product_strings(tmp_path, [anchor, closure]), anchor, closure


def test_bundle_anchor_first_ordering(tmp_path: Path) -> None:
    idx, anchor, closure = _index_two_files(tmp_path)
    bundle = idx.bundle_for([closure, anchor], anchor_paths=[anchor])
    # anchor file's title comes BEFORE the closure file's nav label,
    # even though nav has higher source priority within a group.
    assert bundle[0] == "Billing"
    assert any(s.startswith("Settings") for s in bundle[1:])


def test_bundle_cap_and_dedupe(tmp_path: Path) -> None:
    rel = _write(tmp_path, "locales/en.json", json.dumps({
        f"k{i:02d}": f"Label {i:02d}" for i in range(35)
    }))
    idx = collect_product_strings(tmp_path, [rel])
    bundle = idx.bundle_for([rel], cap=10)
    assert len(bundle) == 10
    assert len(set(bundle)) == 10


def test_bundle_deterministic_for_any_input_order(tmp_path: Path) -> None:
    idx, anchor, closure = _index_two_files(tmp_path)
    b1 = idx.bundle_for([anchor, closure], anchor_paths=[anchor])
    b2 = idx.bundle_for([closure, anchor], anchor_paths=[anchor])
    assert b1 == b2


def test_collect_deterministic_across_runs(tmp_path: Path) -> None:
    _, anchor, closure = _index_two_files(tmp_path)
    i1 = collect_product_strings(tmp_path, [anchor, closure])
    i2 = collect_product_strings(tmp_path, [closure, anchor])
    assert {f: [r.text for r in rows] for f, rows in i1.by_file.items()} == {
        f: [r.text for r in rows] for f, rows in i2.by_file.items()
    }


def test_evidence_tokens_for(tmp_path: Path) -> None:
    idx, anchor, _ = _index_two_files(tmp_path)
    toks = idx.evidence_tokens_for([anchor])
    assert "billing" in toks
