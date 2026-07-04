"""Tests for the Phase-1 deterministic product-capability anchor extractors.

Covers each per-source extractor (i18n namespaces + values, nav labels,
analytics events, test titles), the English-filter, and the aggregator's
dedup / cap / determinism guarantees.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.anchor_extractors import (
    MAX_I18N_VALUES,
    MAX_TOTAL_ANCHORS,
    ProductAnchor,
    anchor_telemetry,
    build_alignment_pool,
    extract_analytics_anchors,
    extract_docs_anchors,
    extract_i18n_anchors,
    extract_nav_anchors,
    extract_product_anchors,
    extract_raw_anchors,
    extract_test_anchors,
)


# ── i18n ─────────────────────────────────────────────────────────────────


def test_i18n_extracts_namespaces_and_values(tmp_path: Path) -> None:
    locale = tmp_path / "apps" / "web" / "locales" / "en"
    locale.mkdir(parents=True)
    (locale / "common.json").write_text(
        json.dumps(
            {
                "user_settings": {"title": "Manage your account"},
                "api-keys": {"create": "Create API key"},
            },
        ),
    )

    anchors = extract_i18n_anchors(tmp_path)
    texts = {a.text for a in anchors}

    # Top-level namespaces humanised snake/kebab → words.
    assert "User Settings" in texts
    assert "Api Keys" in texts
    # Leaf English UI string values (uppercase-initial).
    assert "Manage your account" in texts
    assert "Create API key" in texts
    # Every anchor is tagged + carries provenance.
    assert all(a.source == "i18n" for a in anchors)
    assert all(a.locator for a in anchors)


def test_i18n_english_filter_drops_non_ascii(tmp_path: Path) -> None:
    locale = tmp_path / "i18n"
    locale.mkdir(parents=True)
    # English file is preferred; non-English values must be filtered.
    (locale / "en.json").write_text(
        json.dumps({"greeting": "Welcome back", "noise": "Привіт світ"}),
    )

    texts = {a.text for a in extract_i18n_anchors(tmp_path)}
    assert "Welcome back" in texts
    assert "Привіт світ" not in texts


def test_i18n_absent_source_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const x = 1;")
    assert extract_i18n_anchors(tmp_path) == []


# ── nav ──────────────────────────────────────────────────────────────────


def test_nav_extracts_uppercase_labels(tmp_path: Path) -> None:
    comp = tmp_path / "components"
    comp.mkdir(parents=True)
    (comp / "Sidebar.tsx").write_text(
        """
        const items = [
          { label: "Dashboard", href: "/" },
          { title: "Billing & Invoices" },
          { name: "lowercase ignored" },
        ];
        """,
    )

    texts = {a.text for a in extract_nav_anchors(tmp_path)}
    assert "Dashboard" in texts
    assert "Billing & Invoices" in texts
    # lowercase-initial prop value is not author-intent product nav.
    assert "lowercase ignored" not in texts


def test_nav_only_reads_nav_named_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "Random.tsx").write_text('const label = "Should Not Appear";')
    assert extract_nav_anchors(tmp_path) == []


# ── analytics ──────────────────────────────────────────────────────────────


def test_analytics_extracts_event_names(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "telemetry.ts").write_text(
        """
        track("Booking Created");
        capture("invite_sent");
        analytics.page("Pricing Viewed");
        logEvent("subscription_upgraded");
        """,
    )

    texts = {a.text for a in extract_analytics_anchors(tmp_path)}
    assert "Booking Created" in texts
    assert "invite_sent" in texts
    assert "Pricing Viewed" in texts
    assert "subscription_upgraded" in texts
    assert all(a.source == "analytics" for a in extract_analytics_anchors(tmp_path))


# ── test titles ────────────────────────────────────────────────────────────


def test_test_titles_extracted(tmp_path: Path) -> None:
    tests_dir = tmp_path / "e2e"
    tests_dir.mkdir()
    (tests_dir / "booking.spec.ts").write_text(
        """
        describe("Booking flow", () => {
          it("creates a booking for a free slot", () => {});
          test("rejects an expired invite link", () => {});
        });
        """,
    )

    texts = {a.text for a in extract_test_anchors(tmp_path)}
    assert "Booking flow" in texts
    assert "creates a booking for a free slot" in texts
    assert "rejects an expired invite link" in texts


# ── docs stub ────────────────────────────────────────────────────────────


def test_docs_is_phase1_stub(tmp_path: Path) -> None:
    assert extract_docs_anchors(tmp_path) == []


# ── aggregator ─────────────────────────────────────────────────────────────


def test_aggregator_dedups_by_lowercased_text(tmp_path: Path) -> None:
    locale = tmp_path / "locales" / "en"
    locale.mkdir(parents=True)
    (locale / "common.json").write_text(json.dumps({"billing": {"x": "Dashboard"}}))
    nav = tmp_path / "components"
    nav.mkdir(parents=True)
    (nav / "Nav.tsx").write_text('const a = [{ label: "Dashboard" }];')

    anchors = extract_product_anchors(tmp_path)
    dashboard = [a for a in anchors if a.text.lower() == "dashboard"]
    # "Dashboard" appears via i18n value AND nav label → exactly one survives,
    # and the higher-trust source (nav) wins the cross-source collision.
    assert len(dashboard) == 1
    assert dashboard[0].source == "nav"


def test_aggregator_is_deterministic(tmp_path: Path) -> None:
    locale = tmp_path / "i18n"
    locale.mkdir(parents=True)
    (locale / "en.json").write_text(
        json.dumps({"account": {"t": "Manage account"}, "team_settings": {}}),
    )
    nav = tmp_path / "components"
    nav.mkdir(parents=True)
    (nav / "Menu.tsx").write_text('const a = [{ title: "Workspace Settings" }];')

    first = extract_product_anchors(tmp_path)
    second = extract_product_anchors(tmp_path)
    # Same input → byte-identical output (no randomness, stable sort).
    assert first == second
    # Ordering key is (source priority, tier rank, lowercased text, locator) —
    # verify the emitted sequence matches that documented sort key exactly
    # (tier rank keeps action-grain i18n NAMESPACE keys ahead of leaf values).
    from faultline.pipeline_v2.anchor_extractors import (
        _SOURCE_PRIORITY,
        TIER1_ACTION,
        anchor_tier,
    )

    keys = [
        (_SOURCE_PRIORITY[a.source],
         0 if anchor_tier(a) == TIER1_ACTION else 1,
         a.text.lower(), a.locator)
        for a in first
    ]
    assert keys == sorted(keys)


def test_aggregator_caps_total(tmp_path: Path) -> None:
    locale = tmp_path / "locales" / "en"
    locale.mkdir(parents=True)
    # Emit far more distinct leaf UI-string values than the total cap.
    payload = {
        f"ns{i}": f"Capability number {i:04d}"
        for i in range(MAX_TOTAL_ANCHORS + 200)
    }
    (locale / "common.json").write_text(json.dumps(payload))

    anchors = extract_product_anchors(tmp_path)
    assert len(anchors) <= MAX_TOTAL_ANCHORS


def test_empty_repo_returns_empty(tmp_path: Path) -> None:
    assert extract_product_anchors(tmp_path) == []


# ── telemetry ──────────────────────────────────────────────────────────────


def test_anchor_telemetry_shape() -> None:
    anchors = [
        ProductAnchor("Dashboard", "nav", "Nav.tsx"),
        ProductAnchor("Billing", "i18n", "en.json#billing"),
        ProductAnchor("Booking Created", "analytics", "t.ts"),
    ]
    tel = anchor_telemetry(anchors)
    assert tel["total"] == 3
    assert tel["by_source"] == {"analytics": 1, "i18n": 1, "nav": 1}
    assert set(tel["sample"]) <= {"Dashboard", "Billing", "Booking Created"}
    assert len(tel["sample"]) <= 15
    # No raw arg → no raw keys (back-compat).
    assert "raw_total" not in tel


def test_anchor_telemetry_includes_raw_counts() -> None:
    pool = [ProductAnchor("Dashboard", "nav", "Nav.tsx")]
    raw = [
        ProductAnchor("Dashboard", "nav", "Nav.tsx"),
        ProductAnchor("creates a booking", "test", "a.spec.ts"),
        ProductAnchor("Getting Started", "docs_nav", "apps/docs/Nav.tsx"),
    ]
    tel = anchor_telemetry(pool, raw=raw)
    # Pool counts under the canonical keys; raw extraction shown separately.
    assert tel["total"] == 1
    assert tel["by_source"] == {"nav": 1}
    assert tel["raw_total"] == 3
    assert tel["raw_by_source"] == {"docs_nav": 1, "nav": 1, "test": 1}


# ── Pool curation: test titles excluded by default, fallback when no signal ──


def test_test_titles_excluded_from_pool_by_default(tmp_path: Path) -> None:
    # Strong non-test signal (>= _FALLBACK_MIN_SIGNAL nav labels) so fallback
    # does NOT trigger.
    comp = tmp_path / "src" / "components"
    comp.mkdir(parents=True)
    labels = ",".join(f'{{ label: "Feature {i:02d}" }}' for i in range(20))
    (comp / "Nav.tsx").write_text(f"const items = [{labels}];")
    e2e = tmp_path / "e2e"
    e2e.mkdir()
    (e2e / "x.spec.ts").write_text(
        'describe("creates a thing", () => { it("does the y action", () => {}); });',
    )

    raw = extract_raw_anchors(tmp_path)
    pool = build_alignment_pool(raw)
    # Test titles ARE extracted (telemetry) ...
    assert any(a.source == "test" for a in raw)
    # ... but EXCLUDED from the alignment pool, while app nav survives.
    assert all(a.source != "test" for a in pool)
    assert any(a.source == "nav" for a in pool)


def test_test_titles_fallback_when_no_other_signal(tmp_path: Path) -> None:
    # A library/CLI with ONLY tests → test titles admitted as low-trust fallback
    # so the pool is not empty.
    e2e = tmp_path / "tests"
    e2e.mkdir()
    (e2e / "schema.spec.ts").write_text(
        'describe("validates the schema", () => {\n'
        '  it("rejects malformed input payloads", () => {});\n'
        '  it("accepts a well-formed request body", () => {});\n'
        "});",
    )
    pool = build_alignment_pool(extract_raw_anchors(tmp_path))
    assert pool  # not empty
    assert all(a.source == "test" for a in pool)


def test_test_fallback_is_capped(tmp_path: Path) -> None:
    from faultline.pipeline_v2.anchor_extractors import _TEST_FALLBACK_CAP

    e2e = tmp_path / "tests"
    e2e.mkdir()
    titles = "\n".join(
        f'  it("does distinct thing number {i:03d}", () => {{}});'
        for i in range(200)
    )
    (e2e / "big.spec.ts").write_text(f'describe("suite", () => {{\n{titles}\n}});')
    pool = build_alignment_pool(extract_raw_anchors(tmp_path))
    # Even in fallback, test titles can never blob the pool.
    assert 0 < len(pool) <= _TEST_FALLBACK_CAP


# ── Pool curation: docs-site nav excluded, in-app nav kept ───────────────────


def test_docs_site_nav_excluded_app_nav_kept(tmp_path: Path) -> None:
    # In-app nav (real feature surface).
    app = tmp_path / "apps" / "web" / "components"
    app.mkdir(parents=True)
    (app / "Sidebar.tsx").write_text('const a = [{ label: "Team Settings" }];')
    # Docs-site nav (marketing/docs page titles) under apps/docs.
    docs = tmp_path / "apps" / "docs" / "src"
    docs.mkdir(parents=True)
    (docs / "Sidebar.tsx").write_text('const a = [{ label: "Getting Started Guide" }];')

    raw = extract_raw_anchors(tmp_path)
    raw_pairs = {(a.text, a.source) for a in raw}
    assert ("Team Settings", "nav") in raw_pairs
    assert ("Getting Started Guide", "docs_nav") in raw_pairs

    pool_texts = {a.text for a in build_alignment_pool(raw)}
    assert "Team Settings" in pool_texts            # app nav kept
    assert "Getting Started Guide" not in pool_texts  # docs-site nav excluded


def test_docusaurus_sidebars_config_tagged_docs_nav(tmp_path: Path) -> None:
    # A docusaurus `sidebars.js` is docs-site nav regardless of location.
    root = tmp_path
    (root / "sidebars.ts").write_text('export default [{ label: "API Reference" }];')
    raw = extract_raw_anchors(tmp_path)
    api_ref = [a for a in raw if a.text == "API Reference"]
    assert api_ref and api_ref[0].source == "docs_nav"
    assert all(a.source != "docs_nav" for a in build_alignment_pool(raw))


# ── Per-source caps + i18n per-value cap ─────────────────────────────────────


def test_pool_per_source_caps_no_single_source_dominates(tmp_path: Path) -> None:
    from faultline.pipeline_v2.anchor_extractors import _POOL_PER_SOURCE_CAP

    locale = tmp_path / "locales" / "en"
    locale.mkdir(parents=True)
    payload = {f"ns{i}": f"Capability label number {i:04d}" for i in range(2000)}
    (locale / "common.json").write_text(json.dumps(payload))

    pool = extract_product_anchors(tmp_path)
    i18n_in_pool = sum(1 for a in pool if a.source == "i18n")
    assert i18n_in_pool <= _POOL_PER_SOURCE_CAP["i18n"]


def test_i18n_per_value_cap(tmp_path: Path) -> None:
    locale = tmp_path / "locales" / "en"
    locale.mkdir(parents=True)
    # Far more distinct leaf values than the per-value cap.
    payload = {
        f"ns{i}": f"Capability value number {i:05d}"
        for i in range(MAX_I18N_VALUES + 800)
    }
    (locale / "common.json").write_text(json.dumps(payload))

    anchors = extract_i18n_anchors(tmp_path)
    # The recursive walker bails at the per-value ceiling (F1) — no 85MB blowout.
    assert len(anchors) <= MAX_I18N_VALUES


# ── widget-vocabulary / design-system demotion (MISSION-92 lever #3) ────────


def test_widget_labels_demoted_to_tier2() -> None:
    from faultline.pipeline_v2.anchor_extractors import (
        TIER1_ACTION,
        TIER2_ADVISORY,
        anchor_tier,
        demote_widget_anchors,
        is_widget_label,
    )

    # Every spelling variant of a widget noun matches the fixed public list.
    for label in ("Tooltip", "Alert Dialog", "Hover Card", "Tabs",
                  "Scroll-area", "scroll_area", "AlertDialog", "Dropdown Menu"):
        assert is_widget_label(label), label
    # Whole-label match only: compound product labels never demote.
    for label in ("Tabs Settings", "Form Builder", "Alert Rules",
                  "Booking Calendar Sync"):
        assert not is_widget_label(label), label

    anchors = [
        ProductAnchor("Tooltip", "nav", "apps/web/Sidebar.tsx"),
        ProductAnchor("Create Booking", "nav", "apps/web/Sidebar.tsx"),
    ]
    out, n = demote_widget_anchors(anchors)
    assert n == 1
    assert anchor_tier(out[0]) == TIER2_ADVISORY
    assert anchor_tier(out[1]) == TIER1_ACTION


def test_design_system_paths_always_tier2() -> None:
    from faultline.pipeline_v2.anchor_extractors import (
        TIER2_ADVISORY,
        anchor_tier,
        demote_widget_anchors,
    )

    # A NON-widget label still demotes when it comes from a design-system
    # surface — those packages document widgets, not product capabilities.
    ds_locators = [
        "packages/ui/src/components/nav-menu.tsx",
        "apps/design-system/Sidebar.tsx",
        "apps/studio/components/Tabs.stories.tsx",
        "lib/ui-kit/menu.tsx",
    ]
    anchors = [ProductAnchor("Realtime Inspector", "nav", loc) for loc in ds_locators]
    out, n = demote_widget_anchors(anchors)
    assert n == len(ds_locators)
    assert all(anchor_tier(a) == TIER2_ADVISORY for a in out)


def test_demotion_applied_in_raw_extraction(tmp_path: Path) -> None:
    from faultline.pipeline_v2.anchor_extractors import distinct_tier_counts

    comp = tmp_path / "packages" / "ui" / "components"
    comp.mkdir(parents=True)
    (comp / "Tabs.tsx").write_text('const x = { label: "Tooltip" };')
    app = tmp_path / "apps" / "web"
    app.mkdir(parents=True)
    (app / "Sidebar.tsx").write_text('const x = { label: "Create Booking" };')

    raw = extract_raw_anchors(tmp_path)
    by_text = {a.text: a for a in raw}
    assert by_text["Tooltip"].tier == "tier2_advisory"
    assert by_text["Create Booking"].tier == "tier1_action"
    t1, _t2 = distinct_tier_counts(raw)
    assert t1 == 1  # only the genuine app-nav label arms the gate


def test_telemetry_reports_tier_counts() -> None:
    anchors = [
        ProductAnchor("Create Booking", "nav", "apps/web/Sidebar.tsx"),
        ProductAnchor("Some copy value", "i18n", "locales/en.json"),
    ]
    tel = anchor_telemetry(anchors)
    assert tel["tier1_distinct"] == 1
    assert tel["tier2_distinct"] == 1
