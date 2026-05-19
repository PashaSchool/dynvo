"""Tests for the deterministic domain-noun extractor (Sprint B3.1).

Pure unit tests — no I/O. The extractor inspects path strings only.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.domain_noun import (
    DomainNoun,
    extract_domain_noun,
)


# ── Route-group signal (highest confidence) ──────────────────────────────


def test_route_group_fires_documents():
    """Next.js route-group ``(documents)`` produces the label 'Documents'."""
    paths = [
        "apps/web/(documents)/page.tsx",
        "apps/web/(documents)/[id]/page.tsx",
        "apps/web/(documents)/upload/page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.label == "Documents"
    assert noun.token == "documents"
    assert noun.confidence == 0.85
    assert len(noun.signal_paths) == 3


def test_route_group_kebab_becomes_titled_words():
    """``(data-room)`` route-group → ``"Data Room"`` (kebab-aware Title Case)."""
    paths = [
        "apps/web/(data-room)/upload/page.tsx",
        "apps/web/(data-room)/share/page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.label == "Data Room"
    assert noun.token == "data-room"
    assert noun.confidence == 0.85


# ── First-non-generic dir segment ────────────────────────────────────────


def test_first_non_generic_dir_wins():
    """``apps/web/dashboard/dataroom/...`` — dashboard is generic, dataroom wins."""
    paths = [
        "apps/web/dashboard/dataroom/page.tsx",
        "apps/web/dashboard/dataroom/[id]/page.tsx",
        "apps/web/dashboard/dataroom/upload/page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.label == "Dataroom"
    assert noun.token == "dataroom"
    assert noun.confidence == 0.70


# ── Generic-only paths return None ───────────────────────────────────────


def test_generic_only_returns_none():
    """``apps/web/components/Card.tsx`` — nothing but scaffolding."""
    paths = [
        "apps/web/components/Card.tsx",
        "apps/web/lib/utils.ts",
        "apps/web/hooks/useThing.ts",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    # Filename fallback could fire on 'card' / 'utils' / 'usething' but
    # each appears in only one path — no 60% majority. Returns None.
    assert noun is None


# ── 60% vote threshold ───────────────────────────────────────────────────


def test_majority_60pct_wins():
    """3 of 5 paths agree on 'billing' → wins (60%)."""
    paths = [
        "apps/web/(billing)/checkout/page.tsx",
        "apps/web/(billing)/invoice/page.tsx",
        "apps/web/(billing)/page.tsx",
        "apps/web/(reports)/page.tsx",
        "apps/web/(settings)/page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.label == "Billing"


def test_below_majority_returns_none():
    """2 of 5 paths agree on 'billing' (40%) — below 60% → None."""
    paths = [
        "apps/web/(billing)/page.tsx",
        "apps/web/(billing)/checkout/page.tsx",
        "apps/web/(reports)/page.tsx",
        "apps/web/(settings)/page.tsx",
        "apps/web/(team)/page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    # Highest vote-getter only has 2/5 = 40%. Below threshold.
    assert noun is None


# ── Filename-stem fallback ───────────────────────────────────────────────


def test_filename_stem_fallback():
    """No dir token → fallback to filename stems with low confidence."""
    paths = [
        "apps/web/dataroom-card.tsx",
        "apps/web/dataroom-list.tsx",
        "apps/web/dataroom-detail.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    # Each stem is unique ("dataroom-card", "dataroom-list", "dataroom-detail").
    # No majority because we vote on whole stems. Returns None — the
    # filename-stem fallback rewards exact repetition, not substring
    # commonality. This is OK: filename-only signal is unreliable in
    # practice; we'd rather emit None than a wrong label.
    assert noun is None


def test_filename_stem_only_signal_wins():
    """When ONLY filename stems carry signal (no dir hierarchy), repeats win.

    Per-path priority is route-group > first-non-generic dir > filename.
    With no dir hierarchy at all (file lives directly under the
    workspace prefix), the per-path candidate FALLS through to the
    filename stem.
    """
    paths = [
        "apps/web/dataroom.tsx",
        "apps/web/dataroom.ts",
        "apps/web/dataroom.js",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.token == "dataroom"
    assert noun.confidence == 0.50


def test_workspace_prefix_mismatch_returns_none():
    """Paths that don't sit under workspace_prefix return None (defensive)."""
    paths = [
        "apps/web/(documents)/page.tsx",
        "apps/web/(documents)/upload/page.tsx",
    ]
    # Caller passes a wrong prefix.
    noun = extract_domain_noun(paths, workspace_prefix="packages/cli")
    assert noun is None


def test_empty_paths_returns_none():
    """No paths → None."""
    assert extract_domain_noun([], workspace_prefix="apps/web") is None


# ── Robustness: dynamic segments, backslashes ────────────────────────────


def test_dynamic_segments_skipped():
    """``[id]`` and ``[...slug]`` are skipped as candidates."""
    paths = [
        "apps/web/(surveys)/[id]/page.tsx",
        "apps/web/(surveys)/[...slug]/page.tsx",
        "apps/web/(surveys)/new/page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.label == "Surveys"


def test_windows_separators_normalized():
    """Backslashes work the same as forward slashes."""
    paths = [
        r"apps\web\(billing)\page.tsx",
        r"apps\web\(billing)\checkout\page.tsx",
    ]
    noun = extract_domain_noun(paths, workspace_prefix="apps/web")
    assert noun is not None
    assert noun.label == "Billing"
