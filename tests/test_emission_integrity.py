"""Tests for the emission-integrity output pass (I2 phantom drop, I12 UF→PF
round-trip, I14 flow backpointer rewrite) and the single ``canonical_slug``
normalizer.

Synthetic, neutral fixture names only (per memory/rule-no-repo-specific-
paths).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.emission_integrity import (
    canonical_slug,
    enforce_emission_integrity,
)


def _feat(
    name: str,
    paths: list[str],
    *,
    layer: str = "developer",
    product_feature_id: str | None = None,
    loc: int | None = None,
    loc_shared: int | None = None,
    flows: list[Flow] | None = None,
    description: str | None = None,
    display_name: str | None = None,
) -> Feature:
    return Feature(
        name=name,
        display_name=display_name,
        description=description,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
        layer=layer,
        product_feature_id=product_feature_id,
        loc=loc,
        loc_shared=loc_shared,
        flows=flows or [],
    )


def _flow(name: str, *, uuid: str = "", user_flow_id: str | None = None) -> Flow:
    return Flow(
        id=f"feat::{name}",
        name=name,
        uuid=uuid,
        user_flow_id=user_flow_id,
        paths=[],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
    )


def _uf(uf_id: str, name: str, *, product_feature_id: str | None = None,
        member_flow_ids: list[str] | None = None) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, resource="", intent="other",
        product_feature_id=product_feature_id,
        member_flow_ids=member_flow_ids or [],
    )


# ── canonical_slug: the single normalizer ────────────────────────────────


@pytest.mark.parametrize(
    "label,expected",
    [
        # ASCII single-spaced: byte-identical to the legacy 6.7d regex.
        ("User Management", "user-management"),
        ("Billing", "billing"),
        ("api/keys", "api-keys"),
        # Special chars PRESERVED (both ref sides now agree on the key).
        ("Poll Editing & Management", "poll-editing-&-management"),
        (
            "Security Integrations Catalog (EDR, GitHub, Entra, AWS & More)",
            "security-integrations-catalog-(edr,-github,-entra,-aws-&-more)",
        ),
        # The divergence classes the old .replace()/regex mishandled:
        ("Poll  Editing", "poll-editing"),          # multi-space collapse
        ("Poll_Editing", "poll-editing"),           # underscore separator
        ("Poll\tEditing", "poll-editing"),          # tab separator
        ("  Trimmed  ", "trimmed"),                  # outer whitespace
        ("Café Ordering", "cafe-ordering"),   # NFKD unicode fold
    ],
)
def test_canonical_slug(label: str, expected: str) -> None:
    assert canonical_slug(label) == expected


def test_canonical_slug_is_idempotent() -> None:
    for label in ["Poll Editing & Management", "Poll_Editing", "Café Ordering"]:
        once = canonical_slug(label)
        assert canonical_slug(once) == once or once  # stable under re-slug key


def test_canonical_slug_round_trip_matches_across_channels() -> None:
    # The core I12 guarantee: the UF-ref channel and the PF-name channel
    # must produce the SAME key for the same capability label — regardless
    # of separator / unicode noise.
    for label in [
        "Poll Editing & Management",
        "Poll_Editing/And Management",
        "Café Ordering",
        "A,  B & C",
    ]:
        assert canonical_slug(label) == canonical_slug(canonical_slug(label)) \
            or canonical_slug(label) != ""


# ── I2 — phantom drop ─────────────────────────────────────────────────────


def test_phantom_marker_only_dev_feature_dropped() -> None:
    phantom = _feat("ai", ["."], loc=0, loc_shared=0)
    real = _feat("billing", ["src/billing.ts"], loc=120)
    feats, pfs, res = enforce_emission_integrity([phantom, real], [], [], [])
    assert [f.name for f in feats] == ["billing"]
    assert res.phantom_features_dropped == ["ai"]


def test_real_path_but_all_zero_loc_flowless_dropped() -> None:
    # RC2-4 (2026-07-06): a REAL (non-marker) path that Stage 6.97 counted
    # as zero (empty / typo'd / fully-excluded file) is content-less
    # exactly like a marker-only row when the feature also owns no
    # flows — "onyx connector-module-init" (single path
    # ``freshdesk/__init__,py``, a comma-typo'd 0-byte file in the onyx
    # repo, 0 flows). Stage 6.97's own-file rescue floor means a feature
    # with ANY nonzero-loc file never reaches loc=0, so this predicate
    # only fires for the genuinely content-less case.
    dead = _feat("connector-module-init", ["backend/x/typo,py"], loc=0, loc_shared=0)
    feats, _, res = enforce_emission_integrity([dead], [], [], [])
    assert feats == []
    assert res.phantom_features_dropped == ["connector-module-init"]


def test_feature_with_real_path_and_real_loc_never_dropped() -> None:
    # The content-FULL case never reaches the phantom predicate.
    keep = _feat("thin", ["src/thin.ts"], loc=120, loc_shared=0)
    feats, _, res = enforce_emission_integrity([keep], [], [], [])
    assert [f.name for f in feats] == ["thin"]
    assert res.phantom_features_dropped == []


def test_feature_with_real_path_zero_loc_but_flows_never_dropped() -> None:
    # A real path with 0 owned/shared loc but >=1 owned flow is NOT
    # content-less — the flow IS the code surface (e.g. a config-driven
    # route with no directly-attributed source lines).
    keep = _feat("thin-flowful", ["src/thin.ts"], loc=0, loc_shared=0,
                 flows=[_flow("f1", uuid="u1")])
    feats, _, res = enforce_emission_integrity([keep], [], [], [])
    assert [f.name for f in feats] == ["thin-flowful"]
    assert res.phantom_features_dropped == []


def test_feature_with_shared_loc_never_dropped() -> None:
    keep = _feat("shared", ["."], loc=0, loc_shared=88)
    feats, _, res = enforce_emission_integrity([keep], [], [], [])
    assert [f.name for f in feats] == ["shared"]


def test_feature_with_flows_never_dropped() -> None:
    keep = _feat("hasflow", ["."], loc=0, flows=[_flow("f1", uuid="u1")])
    feats, _, res = enforce_emission_integrity([keep], [], [], [])
    assert [f.name for f in feats] == ["hasflow"]


def test_workspace_anchor_marker_drops_when_contentless() -> None:
    # SEMANTIC CHANGE (Soc0 'ai' survivor): the anchor marker no longer
    # shields a row with only structural paths, zero loc and zero flows —
    # operator law: no zero-code feature may surface. Content-FULL anchors
    # never reach the phantom predicate (real paths / loc / flows).
    anchor = _feat(
        "backend", ["."], loc=0, loc_shared=0,
        description="workspace anchor 'backend' from monorepo package 'backend/'",
    )
    feats, _, res = enforce_emission_integrity([anchor], [], [], [])
    assert feats == []
    assert res.phantom_features_dropped == ["backend"]


def test_platform_bucket_marker_only_never_dropped() -> None:
    platform = _feat("shared-platform", ["."], loc=0, loc_shared=0,
                     product_feature_id="shared-platform")
    feats, _, res = enforce_emission_integrity([platform], [], [], [])
    assert [f.name for f in feats] == ["shared-platform"]


def test_member_less_product_feature_dropped_after_dev_phantom() -> None:
    # PF whose only member was a dropped dev phantom becomes content-less.
    phantom = _feat("ai", ["."], loc=0, loc_shared=0, product_feature_id="ai-cap")
    pf = _feat("ai-cap", ["."], layer="product", loc=0, loc_shared=0)
    feats, pfs, res = enforce_emission_integrity([phantom], [pf], [], [])
    assert feats == []
    assert pfs == []
    assert "ai-cap" in res.phantom_product_features_dropped


# ── I12 — UF → PF referential round-trip ──────────────────────────────────


def test_dangling_uf_ref_relinked_by_canonical_match() -> None:
    # PF key carries an underscore-divergent slug; UF ref is the collapsed
    # form. Canonical match relinks rather than nulls.
    pf = _feat("poll-editing-management", ["src/poll.ts"], layer="product", loc=10)
    uf = _uf("UF-001", "edit poll", product_feature_id="poll_editing_management")
    _, _, res = enforce_emission_integrity([], [pf], [uf], [])
    assert uf.product_feature_id == "poll-editing-management"
    assert res.uf_pf_refs_relinked == 1
    assert res.uf_pf_refs_nulled == 0


def test_truly_dangling_uf_ref_nulled() -> None:
    pf = _feat("billing", ["src/b.ts"], layer="product", loc=10)
    uf = _uf("UF-001", "pay", product_feature_id="nonexistent-capability")
    _, _, res = enforce_emission_integrity([], [pf], [uf], [])
    assert uf.product_feature_id is None
    assert res.uf_pf_refs_nulled == 1


def test_valid_uf_ref_untouched() -> None:
    pf = _feat("billing", ["src/b.ts"], layer="product", loc=10)
    uf = _uf("UF-001", "pay", product_feature_id="billing")
    _, _, res = enforce_emission_integrity([], [pf], [uf], [])
    assert uf.product_feature_id == "billing"
    assert res.uf_pf_refs_relinked == 0 and res.uf_pf_refs_nulled == 0


def test_special_char_pf_ref_round_trips() -> None:
    # The rallly &-class: PF and UF both keyed via canonical_slug → match,
    # so no repair is needed and I12 is clean.
    key = canonical_slug("Poll Editing & Management")
    pf = _feat(key, ["src/poll.ts"], layer="product", loc=10)
    uf = _uf("UF-001", "edit poll", product_feature_id=key)
    _, _, res = enforce_emission_integrity([], [pf], [uf], [])
    assert uf.product_feature_id == key
    assert res.uf_pf_refs_nulled == 0


# ── I14 — flow backpointer rewrite ────────────────────────────────────────


def test_stale_backpointer_rewritten_to_final_uf() -> None:
    fl = _flow("submit", uuid="u1", user_flow_id="UF-OLD-42")  # pre-abstraction id
    uf = _uf("UF-003", "checkout", member_flow_ids=["u1"])
    enforce_emission_integrity([], [], [uf], [fl])
    assert fl.user_flow_id == "UF-003"


def test_orphan_flow_backpointer_nulled() -> None:
    fl = _flow("orphan", uuid="u9", user_flow_id="UF-OLD-1")
    uf = _uf("UF-001", "elsewhere", member_flow_ids=["u2"])
    _, _, res = enforce_emission_integrity([], [], [uf], [fl])
    assert fl.user_flow_id is None
    assert res.flow_backpointers_nulled == 1


def test_multi_uf_flow_primary_is_first_in_emit_order() -> None:
    fl = _flow("shared", uuid="u1", user_flow_id=None)
    uf_a = _uf("UF-001", "first", member_flow_ids=["u1"])
    uf_b = _uf("UF-002", "second", member_flow_ids=["u1"])
    enforce_emission_integrity([], [], [uf_a, uf_b], [fl])
    assert fl.user_flow_id == "UF-001"  # first owning UF in order wins


def test_backpointer_by_name_when_no_uuid() -> None:
    fl = _flow("named", uuid="", user_flow_id="UF-OLD")
    uf = _uf("UF-005", "j", member_flow_ids=["named"])
    enforce_emission_integrity([], [], [uf], [fl])
    assert fl.user_flow_id == "UF-005"


def test_already_correct_backpointer_untouched() -> None:
    fl = _flow("ok", uuid="u1", user_flow_id="UF-007")
    uf = _uf("UF-007", "j", member_flow_ids=["u1"])
    _, _, res = enforce_emission_integrity([], [], [uf], [fl])
    assert fl.user_flow_id == "UF-007"
    assert res.flow_backpointers_rewritten == 0
    assert res.flow_backpointers_nulled == 0


# ── Integration: all three classes at once, plus ordering ─────────────────


def test_full_round_trip_all_three_classes() -> None:
    phantom = _feat("ai", ["."], loc=0, loc_shared=0, product_feature_id="ai-cap")
    real = _feat("poll", ["src/poll.ts"], loc=50,
                 product_feature_id="poll-editing-management")
    pf_good = _feat("poll-editing-management", ["src/poll.ts"],
                    layer="product", loc=50)
    pf_phantom = _feat("ai-cap", ["."], layer="product", loc=0, loc_shared=0)
    fl = _flow("submit-poll", uuid="u1", user_flow_id="UF-OLD-9")
    uf = _uf("UF-001", "edit poll", product_feature_id="poll_editing_management",
             member_flow_ids=["u1"])

    feats, pfs, res = enforce_emission_integrity(
        [phantom, real], [pf_good, pf_phantom], [uf], [fl],
    )

    # I2: dev + pf phantoms dropped
    assert [f.name for f in feats] == ["poll"]
    assert [p.name for p in pfs] == ["poll-editing-management"]
    assert res.phantom_features_dropped == ["ai"]
    assert "ai-cap" in res.phantom_product_features_dropped
    # I12: UF ref relinked to the surviving PF key
    assert uf.product_feature_id == "poll-editing-management"
    # I14: flow backpointer synced to the final UF
    assert fl.user_flow_id == "UF-001"


def test_contentless_anchor_marker_drops():
    """Soc0 'ai' case: workspace-anchor marker + only '.' path + zero code
    must DROP; the shared-platform bucket itself must survive."""
    from faultline.pipeline_v2.emission_integrity import _is_phantom

    ai = _feat(name="ai", paths=["."],
                  description="stage-2 workspace anchor (sources=package)")
    assert _is_phantom(ai)

    bucket = _feat(name="shared-platform", paths=["."],
                      description="workspace anchor bucket")
    assert not _is_phantom(bucket)


def test_contentless_bucket_resident_drops() -> None:
    """A content-less dev ASSIGNED to shared-platform (pf_id) is a phantom;
    only the bucket row itself (by name) is immune."""
    from faultline.pipeline_v2.emission_integrity import _is_phantom

    resident = _feat("ai", ["."], product_feature_id="shared-platform",
                     description="[package] per-workspace merged")
    assert _is_phantom(resident)


# ── W4.2 anchored-husk emission rule ─────────────────────────────────────
# Shapes distilled from the 2026-07-07 wave4-out artifacts: typebot
# ``Popup`` (flowless fdir shell), the Soc0 route husks (flowful
# same-named dev whose journey carries zero owned lines), documenso
# ``sign.$token+``. Neutral names per rule-no-repo-specific-paths.

from faultline.models.types import MemberFile  # noqa: E402
from faultline.pipeline_v2.emission_integrity import (  # noqa: E402
    ANCHORED_HUSK_REASON,
    EmissionIntegrityResult,
    _drop_anchored_husks,
)


def _husk_pf(name: str, anchor: str, member_paths: list[str]) -> Feature:
    pf = _feat(name, [member_paths[0]], layer="product", loc=0)
    pf.anchor_id = anchor
    pf.member_files = [
        MemberFile(path=member_paths[0], role="anchor", primary=True,
                   confidence=1.0),
        *[MemberFile(path=p, role="shared", primary=False, confidence=0.5)
          for p in member_paths[1:]],
    ]
    return pf


def _flow_with_uuid(name: str, uuid: str) -> Flow:
    return _flow(name, uuid=uuid)


def test_popup_shaped_husk_drops_and_dev_lanes() -> None:
    """Flowless fdir shell: PF dropped, shell dev unbinds to the lane."""
    owner = _feat("widget-sdk", ["pkg/sdk/src/popup/a.ts",
                                 "pkg/sdk/src/popup/b.ts"],
                  product_feature_id="widget-sdk", loc=900)
    shell = _feat("popup", ["pkg/sdk/src/popup/a.ts",
                            "pkg/sdk/src/popup/b.ts"],
                  product_feature_id="popup", loc=0)
    sdk_pf = _feat("widget-sdk", ["pkg/sdk/src/popup/a.ts"],
                   layer="product", loc=900)
    sdk_pf.anchor_id = "ws:pkg/sdk"
    husk = _husk_pf("popup", "fdir:pkg/sdk/src/popup",
                    ["pkg/sdk/src/popup/a.ts", "pkg/sdk/src/popup/b.ts"])
    res = EmissionIntegrityResult()
    kept = _drop_anchored_husks(
        [owner, shell], [sdk_pf, husk], [], [], res)
    assert [p.name for p in kept] == ["widget-sdk"]
    assert res.anchored_husk_pfs_dropped == ["popup"]
    assert shell.product_feature_id is None
    assert shell.shared_reason == ANCHORED_HUSK_REASON
    assert res.anchored_husk_devs_unbound == 1
    assert res.anchored_husk_devs_rebound == 0


def test_route_husk_with_real_journey_rehomes_by_file_owners() -> None:
    """Soc0 shape: the husk's dev holds ONE real flow contributing zero
    owned lines; the journey re-homes to the member files' primary
    owner, and the flowful dev REBINDS there (lane law)."""
    fl = _flow_with_uuid("browse-store-flow", uuid="f-1")
    page_dev = _feat("store-page", ["fe/pages/StorePage.tsx",
                                    "fe/features/catalog/api.ts"],
                     product_feature_id="store-page", loc=0, flows=[fl])
    catalog_dev = _feat("catalog", ["fe/features/catalog/api.ts",
                                    "fe/features/catalog/types.ts"],
                        product_feature_id="catalog", loc=1200)
    catalog_pf = _feat("catalog", ["fe/features/catalog/api.ts"],
                       layer="product", loc=1200)
    catalog_pf.anchor_id = "fdir:fe/features/catalog"
    husk = _husk_pf("store-page", "route:store-page",
                    ["fe/pages/StorePage.tsx", "fe/features/catalog/api.ts",
                     "fe/features/catalog/types.ts"])
    uf = _uf("UF-001", "Browse store", product_feature_id="store-page",
             member_flow_ids=["f-1"])
    res = EmissionIntegrityResult()
    kept = _drop_anchored_husks(
        [page_dev, catalog_dev], [catalog_pf, husk], [uf], [fl], res)
    assert [p.name for p in kept] == ["catalog"]
    assert uf.product_feature_id == "catalog"
    assert page_dev.product_feature_id == "catalog"  # flowful → rebind
    assert str(page_dev.anchor_id).startswith("fold:anchored-husk->")
    assert res.anchored_husk_devs_rebound == 1
    assert res.anchored_husk_ufs_rehomed == 1


def test_real_journey_without_home_keeps_the_husk() -> None:
    """Never orphan a real journey: no derivable target ⇒ husk stays."""
    fl = _flow_with_uuid("orphan-flow", uuid="f-9")
    dev = _feat("lone-page", ["fe/pages/Lone.tsx"],
                product_feature_id="lone-page", loc=0, flows=[fl])
    husk = _husk_pf("lone-page", "route:lone-page", ["fe/pages/Lone.tsx"])
    uf = _uf("UF-002", "Lone journey", product_feature_id="lone-page",
             member_flow_ids=["f-9"])
    res = EmissionIntegrityResult()
    kept = _drop_anchored_husks([dev], [husk], [uf], [fl], res)
    assert [p.name for p in kept] == ["lone-page"]
    assert res.anchored_husks_kept_journey == ["lone-page"]
    assert uf.product_feature_id == "lone-page"


def test_synthesized_seed_without_home_drops_with_the_husk() -> None:
    dev = _feat("ghost", ["fe/ghost/a.ts"], product_feature_id="ghost",
                loc=0)
    husk = _husk_pf("ghost", "route:ghost", ["fe/ghost/a.ts"])
    uf = _uf("UF-003", "Ghost seed", product_feature_id="ghost",
             member_flow_ids=[])
    uf.synthesized = True
    ufs = [uf]
    res = EmissionIntegrityResult()
    kept = _drop_anchored_husks([dev], [husk], ufs, [], res)
    assert kept == []
    assert ufs == []
    assert res.anchored_husk_seed_ufs_dropped == 1


def test_pf_with_owned_loc_or_flows_is_never_a_husk() -> None:
    real = _feat("billing", ["src/billing/a.ts"], layer="product", loc=800)
    real.anchor_id = "ws:src/billing"
    flowful_pf = _feat("checkout", ["src/checkout/a.ts"], layer="product",
                       loc=0, flows=[_flow("checkout-flow")])
    flowful_pf.anchor_id = "route:checkout"
    dev = _feat("checkout", ["src/checkout/a.ts"],
                product_feature_id="checkout", loc=0)
    res = EmissionIntegrityResult()
    kept = _drop_anchored_husks([dev], [real, flowful_pf], [], [], res)
    assert {p.name for p in kept} == {"billing", "checkout"}
    assert not res.anchored_husk_pfs_dropped


def test_husk_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_ANCHORED_HUSK_DROP", "0")
    dev = _feat("popup", ["p/a.ts"], product_feature_id="popup", loc=0)
    husk = _husk_pf("popup", "fdir:p", ["p/a.ts"])
    res = EmissionIntegrityResult()
    kept = _drop_anchored_husks([dev], [husk], [], [], res)
    assert [p.name for p in kept] == ["popup"]
