"""Tests for the Phase-1 deterministic product-capability anchor extractors.

Covers each per-source extractor (i18n namespaces + values, nav labels,
analytics events, test titles), the English-filter, and the aggregator's
dedup / cap / determinism guarantees.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.anchor_extractors import (
    MAX_TOTAL_ANCHORS,
    ProductAnchor,
    anchor_telemetry,
    extract_analytics_anchors,
    extract_docs_anchors,
    extract_i18n_anchors,
    extract_nav_anchors,
    extract_product_anchors,
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
    # Ordering key is (source priority, lowercased text, locator) — verify
    # the emitted sequence matches that documented sort key exactly.
    from faultline.pipeline_v2.anchor_extractors import _SOURCE_PRIORITY

    keys = [(_SOURCE_PRIORITY[a.source], a.text.lower(), a.locator) for a in first]
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
