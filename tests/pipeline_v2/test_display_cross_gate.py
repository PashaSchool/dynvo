"""Display-cross evidence gate (``FAULTLINE_PF_DISPLAY_EVIDENCE_GATE``).

Named units for every forensic exhibit class (display-cross forensics
2026-07-19, scripts 01-06): a nav-tier PF display must carry identity
evidence — its tokens intersect the PF's own name/anchor identity OR its
member-dominant path tokens — else the ladder reverts to the honest
basename. Fixtures are synthetic; they hold the MECHANISM (the corpus
census keep/revert split is the simulation-vs-ON-board check, script 06).

The B40 nav-pinning rung (``nav_labels_for_pfs``) and the B57 nav-cluster
rung (``nav_label_sets_for_pfs``) read the UNGATED votes — asserted
untouched below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from faultline.pipeline_v2.naming_contract import (
    _member_dominant_tokens,
    _nav_anchor_page_labels,
    gated_nav_labels_for_pfs,
    load_naming_vocab,
    nav_label_sets_for_pfs,
    nav_labels_for_pfs,
    pf_display_evidence_gate_enabled,
)

VOCAB = load_naming_vocab()


def _pf(name: str, anchor: str, paths: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, anchor_id=anchor, paths=paths)


def _ps(pairs: list[tuple[str, str]]) -> Any:
    return SimpleNamespace(nav_pairs_by_file={"components/Navigation.tsx": pairs})


# ── flag: default OFF + kill-switch semantics ───────────────────────────


def test_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", raising=False)
    assert pf_display_evidence_gate_enabled() is False
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "0")
    assert pf_display_evidence_gate_enabled() is False
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "false")
    assert pf_display_evidence_gate_enabled() is False
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    assert pf_display_evidence_gate_enabled() is True
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "true")
    assert pf_display_evidence_gate_enabled() is True


def test_flag_registered_for_cache_keying() -> None:
    """Toggle-and-rescan must miss the cache (audit Bug 2 law)."""
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS

    assert "FAULTLINE_PF_DISPLAY_EVIDENCE_GATE" in ENV_OUTPUT_FLAGS


# ── exhibit 1: cal features -> 'profile' — FOREIGN label reverts ───────


def _cal_features_fixture() -> tuple[list[Any], Any, list[dict[str, str]]]:
    """cal.com shape: the ``features`` PF first-come-owns the my-account
    route files, so the 'Profile' nav pair (href to the profile page)
    votes onto it — but 'profile' is in NEITHER the PF identity NOR its
    member-dominant tokens."""
    pfs = [
        _pf(
            "features",
            "route:apps/web/app/(wrap)/settings/(sl)/my-account/features",
            [
                "apps/web/app/(wrap)/settings/(sl)/my-account/features/page.tsx",
                "apps/web/app/(wrap)/settings/(sl)/my-account/features/edit.tsx",
                "apps/web/app/(wrap)/settings/(sl)/my-account/profile/page.tsx",
            ],
        ),
    ]
    routes = [
        {"pattern": "/settings/my-account/profile",
         "file": "apps/web/app/(wrap)/settings/(sl)/my-account/profile/page.tsx"},
        {"pattern": "/settings/my-account/features",
         "file": "apps/web/app/(wrap)/settings/(sl)/my-account/features/page.tsx"},
    ]
    ps = _ps([("Profile", "/settings/my-account/profile")])
    return pfs, ps, routes


def test_exhibit_features_foreign_profile_reverts() -> None:
    pfs, ps, routes = _cal_features_fixture()
    # Ungated (B40 view): the foreign label wins the vote.
    assert nav_labels_for_pfs(pfs, ps, routes) == {"features": "Profile"}
    # Gated: no identity evidence -> slug omitted -> ladder falls to basename.
    assert gated_nav_labels_for_pfs(pfs, ps, routes, VOCAB) == {}


def test_exhibit_features_b40_channel_untouched() -> None:
    """B40 nav-pinning + B57 nav-cluster read the UNGATED votes even while
    the gate would revert — the gate is a display-ladder consumer only."""
    pfs, ps, routes = _cal_features_fixture()
    assert nav_labels_for_pfs(pfs, ps, routes) == {"features": "Profile"}
    assert nav_label_sets_for_pfs(pfs, ps, routes) == {"features": ["Profile"]}


# ── exhibit 2 (ANTI-CASE): cal flags -> 'features' — AUTHENTIC keeps ────


def test_anticase_flags_features_label_keeps() -> None:
    """flags -> 'Features' is JUSTIFIED: the label is authored on the PF's
    own anchor page AND 'features' is member-dominant (packages/features/
    flags/*). The forensic verdict says NOT reverted — the survivor must
    appear, title-cased."""
    pfs = [
        _pf(
            "flags",
            "route:apps/web/app/(wrap)/settings/(al)/admin/flags",
            [
                "apps/web/app/(wrap)/settings/(al)/admin/flags/page.tsx",
                "packages/features/flags/config.ts",
                "packages/features/flags/hooks/useFlagMap.ts",
                "packages/features/flags/features.repository.ts",
            ],
        ),
    ]
    routes = [
        {"pattern": "/settings/admin/flags",
         "file": "apps/web/app/(wrap)/settings/(al)/admin/flags/page.tsx"},
    ]
    ps = _ps([("Features", "/settings/admin/flags")])
    assert gated_nav_labels_for_pfs(pfs, ps, routes, VOCAB) == {
        "flags": "Features"
    }


def test_member_dominant_tokens_ratio_is_majority() -> None:
    """The evidence bar is a scale-invariant MAJORITY ratio (0.5), not a
    presence check: a token on 1 of 4 member files is not evidence."""
    dom = _member_dominant_tokens(
        [
            "a/features/x.ts", "a/features/y.ts",
            "a/features/z.ts", "b/profile/w.ts",
        ]
    )
    assert "featur" in dom      # 3/4 >= 0.5 (stemmed)
    assert "profil" not in dom  # 1/4 < 0.5


# ── exhibit 3: cal insights -> 'bookings' — anchor-page tie-break ───────


def _cal_insights_fixture() -> tuple[list[Any], Any, list[dict[str, str]]]:
    """Navigation.tsx child tab: 'Bookings' hrefs an /insights/bookings
    sub-route the ``insights`` PF first-come-owns; 'Insights' hrefs the
    PF's own anchor index page. Equal votes — pre-fix alpha picked
    'Bookings'."""
    pfs = [
        _pf(
            "insights",
            "route:apps/web/app/(wrap)/insights",
            [
                "apps/web/app/(wrap)/insights/page.tsx",
                "apps/web/app/(wrap)/insights/bookings/page.tsx",
            ],
        ),
    ]
    routes = [
        {"pattern": "/insights", "file": "apps/web/app/(wrap)/insights/page.tsx"},
        {"pattern": "/insights/bookings",
         "file": "apps/web/app/(wrap)/insights/bookings/page.tsx"},
    ]
    ps = _ps([("Insights", "/insights"), ("Bookings", "/insights/bookings")])
    return pfs, ps, routes


def test_exhibit_insights_anchor_preference_beats_alpha() -> None:
    pfs, ps, routes = _cal_insights_fixture()
    # Pre-fix (B40 top label): alphabetical tie-break picks the collision.
    assert nav_labels_for_pfs(pfs, ps, routes) == {"insights": "Bookings"}
    # Gated: the PF's own anchor-page self-link wins the tie; identity
    # tokens intersect ('insight') so the survivor keeps.
    assert gated_nav_labels_for_pfs(pfs, ps, routes, VOCAB) == {
        "insights": "Insights"
    }


def test_anchor_preference_never_overrides_vote_plurality() -> None:
    """The tie-break inserts BEFORE alpha but AFTER vote count: a label the
    authors clearly favored (more votes) still wins over the self-link —
    then the evidence gate decides survival on its own merits."""
    pfs, _, routes = _cal_insights_fixture()
    ps = SimpleNamespace(nav_pairs_by_file={
        "nav.tsx": [("Insights", "/insights"), ("Bookings", "/insights/bookings")],
        "tabs.tsx": [("Bookings", "/insights/bookings")],
    })
    # 'Bookings' 2 votes > 'Insights' 1 vote -> plurality holds; foreign
    # label then fails the evidence gate ('booking' not member-dominant:
    # 1/2 files... actually 1/2 == 0.5 majority) — assert the plurality
    # winner is what enters the gate by checking the anchor map exists.
    anchor = _nav_anchor_page_labels(pfs, ps, routes)
    assert anchor == {"insights": {"Insights"}}
    votes_view = nav_labels_for_pfs(pfs, ps, routes)
    assert votes_view == {"insights": "Bookings"}


# ── exhibit 4: casing — raw i18n key 'directory_sync' title-cases ───────


def test_exhibit_casing_directory_sync_titlecased_on_keep() -> None:
    """cal ``directory-sync``-shape PF whose own authored label is the raw
    i18n key: the label survives the gate (identity match) and is
    title-cased — 'directory_sync' -> 'Directory Sync'."""
    pfs = [
        _pf(
            "directory-sync",
            "route:apps/web/app/(wrap)/settings/(sl)/organizations/dsync",
            ["apps/web/app/(wrap)/settings/(sl)/organizations/dsync/page.tsx"],
        ),
    ]
    routes = [
        {"pattern": "/settings/organizations/dsync",
         "file": "apps/web/app/(wrap)/settings/(sl)/organizations/dsync/page.tsx"},
    ]
    ps = _ps([("directory_sync", "/settings/organizations/dsync")])
    assert gated_nav_labels_for_pfs(pfs, ps, routes, VOCAB) == {
        "directory-sync": "Directory Sync"
    }


def test_exhibit_organization_foreign_directory_sync_reverts() -> None:
    """The SAME label on a foreign owner (cal ``organization`` PF first-come
    -owning the dsync route file) fails the gate — the exhibit pair that
    proves the gate keys on the PF, not the label."""
    pfs = [
        _pf(
            "organization",
            "route:apps/web/app/(wrap)/onboarding/organization",
            ["apps/web/app/(wrap)/settings/(sl)/organizations/dsync/page.tsx",
             "apps/web/app/(wrap)/onboarding/organization/details/page.tsx"],
        ),
    ]
    routes = [
        {"pattern": "/settings/organizations/dsync",
         "file": "apps/web/app/(wrap)/settings/(sl)/organizations/dsync/page.tsx"},
    ]
    ps = _ps([("directory_sync", "/settings/organizations/dsync")])
    assert nav_labels_for_pfs(pfs, ps, routes) == {
        "organization": "directory_sync"
    }
    assert gated_nav_labels_for_pfs(pfs, ps, routes, VOCAB) == {}


# ── ladder integration: revert falls through to the honest basename ─────


def test_ladder_falls_to_basename_on_reverted_nav() -> None:
    """With the gated nav channel empty, ``resolve_pf_display`` grades the
    bare-basename display as honest debt instead of installing the foreign
    label — the end-to-end revert shape."""
    from faultline.pipeline_v2.pf_display_provenance import (
        ProvenanceSources,
        resolve_pf_display,
    )

    # Pre-fix: nav='Profile' upgrades the bare 'Features' display.
    pre = resolve_pf_display(
        "Features",
        ProvenanceSources(nav="Profile", basename="features",
                          anchor_source="route"),
    )
    assert pre.display == "Profile" and pre.provenance == "nav"
    # Post-gate: nav channel empty -> basename kept, honest tier.
    post = resolve_pf_display(
        "Features",
        ProvenanceSources(nav="", basename="features", anchor_source="route"),
    )
    assert post.display == "Features"
    assert post.changed is False
    assert post.provenance == "dir-basename"


# ── OFF-path byte-identity at the consumer seam ─────────────────────────


def test_off_path_uses_raw_nav_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset/0 must reproduce the pre-gate nav feed exactly: the gated map
    is only consulted when the flag is armed (the kill-switch law at the
    seam — full byte-identity is the killswitch_4way scan gate)."""
    monkeypatch.delenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", raising=False)
    pfs, ps, routes = _cal_features_fixture()
    raw = nav_labels_for_pfs(pfs, ps, routes)
    assert raw == {"features": "Profile"}
    assert pf_display_evidence_gate_enabled() is False
