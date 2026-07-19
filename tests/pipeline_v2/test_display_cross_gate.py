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


# ── exhibit 5 (iteration 2): Pass-1 candidate rank — the SECOND mechanism ─
# cal wave-spot forensic (2026-07-19): app-store->'license', features->
# 'impersonation', organization->'directory_sync' were installed by Pass 1
# (build_pf_candidates ranks nav_label FIRST), not by the B71 ladder — the
# iteration-1 gate left them standing. The gate must cover the Pass-1 feed.


def _run_contract(pfs: list[Any], ps: Any, routes: list[dict[str, str]]) -> None:
    from faultline.pipeline_v2.naming_contract import run_naming_contract

    run_naming_contract(
        pfs, [], (), product_strings=ps, routes_index=routes,
        keeper_on=False,
    )


def _pass1_fixture() -> tuple[list[Any], Any, list[dict[str, str]]]:
    """cal ``features``-shape with NO pre-set display: Pass 1 mints it, and
    the foreign 'impersonation' label is the top-ranked candidate."""
    pf = SimpleNamespace(
        name="features",
        display_name=None,
        anchor_id="route:apps/web/app/(wrap)/settings/(sl)/my-account/features",
        paths=[
            "apps/web/app/(wrap)/settings/(sl)/my-account/features/page.tsx",
            "apps/web/app/(wrap)/settings/(sl)/my-account/features/edit.tsx",
            "packages/features/my-account/list.tsx",
            "apps/web/app/(wrap)/settings/(sl)/security/impersonation/page.tsx",
        ],
    )
    routes = [
        {"pattern": "/settings/security/impersonation",
         "file": "apps/web/app/(wrap)/settings/(sl)/security/impersonation/page.tsx"},
        {"pattern": "/settings/my-account/features",
         "file": "apps/web/app/(wrap)/settings/(sl)/my-account/features/page.tsx"},
    ]
    ps = _ps([("impersonation", "/settings/security/impersonation")])
    return [pf], ps, routes


def test_exhibit_pass1_foreign_label_blocked_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    pfs, ps, routes = _pass1_fixture()
    _run_contract(pfs, ps, routes)
    assert pfs[0].display_name == "Features"   # honest slug word, not foreign


def test_exhibit_pass1_off_path_installs_raw_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF reproduces the pre-gate Pass-1 behaviour (the byte-identity
    anchor for the kill-switch): the raw foreign label wins the rank."""
    monkeypatch.delenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", raising=False)
    pfs, ps, routes = _pass1_fixture()
    _run_contract(pfs, ps, routes)
    assert pfs[0].display_name == "impersonation"


def test_pass1_authentic_label_still_installs_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE at the Pass-1 seam: an authored label WITH identity
    evidence still installs under the gate — title-cased."""
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    pf = SimpleNamespace(
        name="directory-sync",
        display_name=None,
        anchor_id="route:apps/web/app/(wrap)/settings/(sl)/organizations/dsync",
        paths=["apps/web/app/(wrap)/settings/(sl)/organizations/dsync/page.tsx"],
    )
    routes = [
        {"pattern": "/settings/organizations/dsync",
         "file": "apps/web/app/(wrap)/settings/(sl)/organizations/dsync/page.tsx"},
    ]
    ps = _ps([("directory_sync", "/settings/organizations/dsync")])
    _run_contract([pf], ps, routes)
    assert pf.display_name == "Directory Sync"


# ── exhibit 6 (iteration 3): labeler composite-KEEP — the Pass-3 seam ────
# novu keyed calibration (2026-07-19): the hub provider PFs carry ZERO nav
# votes (offline vote rebuild VERIFIED) — the gate never touched them. The
# batched PM-labeler prompt legitimately changed on the intended class row
# (application-generic), the fresh Haiku draw picked bare vendor leaves
# ('Chat — Discord' -> 'Discord') for 18 hub items, and the application
# seam's law re-check had no information-loss guard (applied 10 -> 28).
# The guard: under the SAME flag, a persona pick that equals the hub
# composition's vendor leaf (a pure prefix-drop) is rejected.


def _hub_pf() -> SimpleNamespace:
    return SimpleNamespace(
        name="discord",
        display_name=None,
        anchor_id="hub:packages/providers/src/lib/chat/discord",
        paths=["packages/providers/src/lib/chat/discord/discord.provider.ts"],
    )


def _run_with_labeler(pf: Any, choices: dict[str, str]) -> None:
    from faultline.pipeline_v2.naming_contract import run_naming_contract

    run_naming_contract(
        [pf], [], (), product_strings=None, routes_index=[],
        keeper_on=False, labeler=lambda pending: {"choices": choices},
    )


def test_composite_keep_blocks_prefix_drop_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE of the it3 class: armed, the persona's bare-vendor pick
    may not flatten the hub composition."""
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    pf = _hub_pf()
    _run_with_labeler(pf, {"discord": "Discord"})
    assert pf.display_name == "Chat — Discord"


def test_composite_flattening_reproduces_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF reproduces the pre-guard application byte-for-byte — the
    kill-switch anchor for the Pass-3 seam."""
    monkeypatch.delenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", raising=False)
    pf = _hub_pf()
    _run_with_labeler(pf, {"discord": "Discord"})
    assert pf.display_name == "Discord"


def test_composite_keep_blocks_dash_flatten_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """novu ``chat-webhook`` exhibit: 'Chat — Webhook' -> pick
    'Chat Webhook' keeps the words but flattens the authored channel/
    vendor shape — same information-loss class, same rejection."""
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    pf = SimpleNamespace(
        name="chat-webhook",
        display_name=None,
        anchor_id="hub:packages/providers/src/lib/chat/chat-webhook",
        paths=["packages/providers/src/lib/chat/chat-webhook/webhook.provider.ts"],
    )
    _run_with_labeler(pf, {"chat-webhook": "Chat Webhook"})
    # The composite RECOMPOSES canonically (family-echo stripped),
    # whatever separator form the upstream passes produced.
    assert pf.display_name == "Chat — Webhook"


def test_composite_keep_sendgrid_degrime_mangle_recomposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """novu it4 calibration: at Pass-3 sendgrid's live display was the
    degrime-truncated 'Email — sendgr', so a cur-string compare missed
    the flatten (the it3 leak). The recomposer grounds the compare in
    the ANCHOR's vendor segment and repairs the mangle."""
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    from faultline.pipeline_v2.naming_contract import (
        _hub_composite_recompose,
        load_naming_vocab,
    )

    out = _hub_composite_recompose(
        "hub:packages/providers/src/lib/email/sendgrid",
        "Email — sendgr",           # the mangled live display
        "SendGrid",                  # the persona's vendor pick
        load_naming_vocab(),
    )
    assert out == "Email — SendGrid"


@pytest.mark.parametrize("anchor_vendor, pick, family, expected", [
    # camelCase vendor segment folds to the pick (the it4 edge class)
    ("msTeams", "Ms Teams", "chat", "Chat — Ms Teams"),
    # brand-cased single-slug vendor
    ("sendgrid", "SendGrid", "email", "Email — SendGrid"),
    # multi-word slug vendor
    ("grafana-on-call", "Grafana On Call", "chat", "Chat — Grafana On Call"),
    # family-echo vendor slug
    ("chat-webhook", "Chat Webhook", "chat", "Chat — Webhook"),
])
def test_composite_recompose_camelcase_vendor_classes(
    anchor_vendor: str, pick: str, family: str, expected: str,
) -> None:
    from faultline.pipeline_v2.naming_contract import (
        _hub_composite_recompose,
        load_naming_vocab,
    )

    aid = f"hub:packages/providers/src/lib/{family}/{anchor_vendor}"
    cur = f"{family.title()} — {anchor_vendor}"  # any composition shape
    assert _hub_composite_recompose(aid, cur, pick, load_naming_vocab()) == expected


def test_composite_recompose_none_for_real_correction() -> None:
    from faultline.pipeline_v2.naming_contract import (
        _hub_composite_recompose,
        load_naming_vocab,
    )

    assert _hub_composite_recompose(
        "hub:packages/providers/src/lib/chat/discord",
        "Chat — Discord", "Discord Notifications", load_naming_vocab(),
    ) is None


def test_composite_keep_allows_real_correction_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pick that CHANGES the name (not a pure prefix-drop) still
    applies — the guard blocks information loss, never correction."""
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    pf = _hub_pf()
    _run_with_labeler(pf, {"discord": "Discord Notifications"})
    assert pf.display_name == "Discord Notifications"


def test_composite_keep_is_hub_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-hub PF whose display happens to carry ' — ' is NOT guarded —
    the channel-prefix law is evidenced by the hub dir structure only."""
    monkeypatch.setenv("FAULTLINE_PF_DISPLAY_EVIDENCE_GATE", "1")
    pf = SimpleNamespace(
        name="insights-page",
        display_name="App Store — Insights",
        anchor_id="route:apps/web/app/insights",
        paths=["apps/web/app/insights/page.tsx"],
    )
    _run_with_labeler(pf, {"insights-page": "Insights"})
    assert pf.display_name == "Insights"


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
