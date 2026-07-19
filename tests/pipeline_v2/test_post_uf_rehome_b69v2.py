"""B69-v2 — Stage 6.99b post-UF PF-homing hygiene rehome.

NAMED ANTI-CASES (phase-1 §4 forensics + v2 keyed-A/B forensics — SACRED):
  (1) θ-guard / 'UF-013 Create and manage data rooms': an organic journey
      whose home anchor holds a member MAJORITY is untouched even when a
      foreign anchor matches some members (real dataroom-scoped faqs
      routes legally live under datarooms).
  (2) 'Manage dataroom FAQs' / real-faqs journey: after hygiene the faqs
      PF keeps its OWN journey — home_share=1.0 rows never move.
  (3) mupdf / signal-without-a-target: minority home but NO rival above
      the random-tail floor ⇒ do NOT invent a move (B49 lesson).
  (4) I8 orphan guard: never strip the source PF's last journey.
  (5) bare-'Manage' class at the C′ author: a render that collapses to a
      single word or echoes adjacent tokens ('Manage manage') is refused —
      the row keeps its name (rename_kept, honest).
  (6) OFF-gate: flag unset ⇒ helper False (the finalize wiring never calls
      the rail); no registry ⇒ honest skipped tele, board untouched.
Plus: the papermark-shaped disease row rehomes AND renames; fold-into-
existing dissolves a rehomed seed into the organic receiver twin;
determinism ×2.
"""

from __future__ import annotations

import copy
import os

from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
    homing_hygiene_enabled,
    run_post_uf_rehome,
)


class Fl:
    def __init__(self, uuid, ep, name):
        self.uuid = uuid
        self.entry_point_file = ep
        self.name = name


class Dev:
    def __init__(self, name, flows):
        self.name = name
        self.flows = flows


class PF:
    def __init__(self, name, anchor_id, display=None):
        self.name = name
        self.id = name
        self.anchor_id = anchor_id
        self.display_name = display or name.title()
        self.layer = "product"


class UF:
    _n = 0

    def __init__(self, name, pfid, members, *, synthesized=False,
                 resource=None):
        UF._n += 1
        self.id = f"UF-{UF._n:03d}"
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.synthesized = synthesized
        self.resource = resource


def _anchor(cid, prefixes):
    return SpineAnchor(
        canonical_id=cid, key=cid.split(":", 1)[-1], source="route",
        display=cid.split(":", 1)[-1].title(), prefixes=tuple(prefixes))


def _papermark_scene():
    """The keyed exhibit, minimally: PF faqs annexed a seed of pure
    dataroom pages; PF datarooms is the multiplicatively wider owner."""
    UF._n = 0  # stable row ids per scene (determinism comparisons)
    registry = {
        "route:faq": _anchor(
            "route:faq",
            ["app/api/faqs", "pages/api/teams/t/datarooms/d/faqs"]),
        "route:dataroom": _anchor(
            "route:dataroom",
            ["pages/datarooms", "pages/api/teams/t/datarooms"]),
    }
    pf_faqs = PF("faqs", "route:faq", "Faqs")
    pf_dr = PF("datarooms", "route:dataroom", "Datarooms")
    fl_list = Fl("f-list", "pages/datarooms/index.tsx", "list-datarooms-flow")
    fl_view = Fl("f-view", "pages/datarooms/d/index.tsx",
                 "view-dataroom-overview-flow")
    fl_faq = Fl("f-faq", "app/api/faqs/route.ts", "retrieve-faqs-flow")
    fl_dr1 = Fl("f-dr1", "pages/api/teams/t/datarooms/index.ts",
                "manage-team-datarooms-flow")
    fl_dr2 = Fl("f-dr2", "pages/api/teams/t/datarooms/d/index.ts",
                "manage-team-dataroom-by-id-flow")
    fl_drfaq = Fl("f-drfaq", "pages/api/teams/t/datarooms/d/faqs/index.ts",
                  "manage-team-dataroom-faqs-flow")
    devs = [Dev("d1", [fl_list, fl_view, fl_faq, fl_dr1, fl_dr2, fl_drfaq])]
    sick = UF("View faqs", "faqs", ["f-list", "f-view"],
              synthesized=True, resource="datarooms")
    real_faqs = UF("Manage dataroom FAQs", "faqs", ["f-faq"])
    keeper = UF("Create and manage data rooms", "datarooms",
                ["f-dr1", "f-dr2", "f-drfaq"])
    return registry, [pf_faqs, pf_dr], devs, sick, real_faqs, keeper


def test_disease_row_rehomes_and_renames():
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    ufs = [sick, real_faqs, keeper]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert tele["rehomed"] == 1
    assert sick.product_feature_id == "datarooms"
    # C′ — renamed from its OWN group resource, view-verdict members.
    assert tele["renamed"] == 1
    assert sick.name == "View datarooms"
    assert tele["moves"][0]["from"] == "faqs"
    assert tele["moves"][0]["to"] == "datarooms"
    assert tele["moves"][0]["home_share"] == 0.0


def test_anticase_theta_guard_keeper_untouched():
    """(1) 'UF-013' — majority home wins even with a foreign faq member."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    ufs = [sick, real_faqs, keeper]
    run_post_uf_rehome(ufs, devs, pfs, registry)
    assert keeper.product_feature_id == "datarooms"
    assert keeper.name == "Create and manage data rooms"


def test_anticase_real_faqs_journey_stays():
    """(2) the faqs PF keeps its OWN journey (home_share=1.0)."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    ufs = [sick, real_faqs, keeper]
    run_post_uf_rehome(ufs, devs, pfs, registry)
    assert real_faqs.product_feature_id == "faqs"
    assert real_faqs.name == "Manage dataroom FAQs"


def test_anticase_signal_without_target_mupdf():
    """(3) minority home, no rival above the floor ⇒ no invented move."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    fl_m1 = Fl("f-m1", "pages/api/mupdf/get-pages.ts", "get-pages-flow")
    fl_m2 = Fl("f-m2", "pages/api/mupdf/convert-page.ts", "convert-page-flow")
    devs.append(Dev("d2", [fl_m1, fl_m2]))
    mupdf = UF("View dataroom documents", "datarooms", ["f-m1", "f-m2"],
               synthesized=True, resource="mupdf")
    ufs = [sick, real_faqs, keeper, mupdf]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert mupdf.product_feature_id == "datarooms"
    assert mupdf.name == "View dataroom documents"
    assert tele["signal_no_target"] >= 1


def test_anticase_orphan_guard():
    """(4) never strip the source PF's last journey (I8)."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    ufs = [sick, keeper]  # faqs PF holds ONLY the sick row
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert sick.product_feature_id == "faqs"
    assert tele["orphan_guarded"] == 1
    assert tele["rehomed"] == 0


def test_fold_into_existing_organic_twin():
    """A rehomed seed sharing a member flow with an organic receiver
    journey dissolves into it (no shadow row)."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    organic = UF("Browse and filter datarooms", "datarooms",
                 ["f-list", "f-dr1"])
    ufs = [sick, real_faqs, keeper, organic]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert tele["folded"] == 1
    assert sick not in ufs
    assert organic.member_flow_ids == ["f-list", "f-dr1", "f-view"]
    assert organic.member_count == 3
    assert tele["folds"][0]["into"] == organic.id


def test_anticase_cprime_refuses_bare_render():
    """(5) a route-group literally named 'manage' renders 'Manage manage'
    → refused; the row moves but keeps its name (rename_kept)."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    sick.resource = "manage"
    ufs = [sick, real_faqs, keeper]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert tele["rehomed"] == 1
    assert sick.product_feature_id == "datarooms"
    assert tele["renamed"] == 0
    assert tele["rename_kept"] == 1
    assert sick.name == "View faqs"  # unchanged — honest over pretty


def test_anticase_off_gate_and_no_registry(monkeypatch):
    """(6) default ON (horizon-1 flip); and a missing registry is an honest
    no-op regardless of the flag."""
    # SEMANTIC (horizon-1 flip): unset now defaults ON.
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    assert homing_hygiene_enabled() is True
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert homing_hygiene_enabled() is True
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    ufs = [sick, real_faqs, keeper]
    tele = run_post_uf_rehome(ufs, devs, pfs, None)
    assert tele["skipped"] == "no_anchor_registry"
    assert sick.product_feature_id == "faqs"
    assert sick.name == "View faqs"


def test_determinism_two_runs_identical():
    a = _papermark_scene()
    b = _papermark_scene()
    ufs_a = [a[3], a[4], a[5]]
    ufs_b = [b[3], b[4], b[5]]
    tele_a = run_post_uf_rehome(ufs_a, a[2], a[1], a[0])
    tele_b = run_post_uf_rehome(ufs_b, b[2], b[1], b[0])
    assert copy.deepcopy(tele_a) == copy.deepcopy(tele_b)
    assert [(u.name, u.product_feature_id) for u in ufs_a] == \
        [(u.name, u.product_feature_id) for u in ufs_b]


def test_rename_collision_gated_against_final_board():
    """C′ never mints a duplicate display: if 'View datarooms' already
    exists on the board the rehomed row keeps its old name."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    occupier = UF("View datarooms", "datarooms", ["f-dr2"])
    ufs = [sick, real_faqs, keeper, occupier]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert tele["rehomed"] == 1
    assert sick.name == "View faqs"
    assert tele["rename_kept"] == 1


def test_anticase_home_evidence_guard():
    """(7, offline-drive forensics) a row holding ANY home-anchor-matched
    member ('View dataroom analytics and audit log' carries faqs' own
    app/(ee)/api/faqs route) is home-tied — never moved."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    fl_an1 = Fl("f-an1", "pages/api/teams/t/analytics/a.ts",
                "view-analytics-flow")
    fl_an2 = Fl("f-an2", "pages/api/teams/t/analytics/b.ts",
                "view-audit-log-flow")
    devs.append(Dev("d3", [fl_an1, fl_an2]))
    mixed = UF("View dataroom analytics and audit log", "faqs",
               ["f-faq", "f-an1", "f-an2"])
    ufs = [sick, real_faqs, keeper, mixed]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert mixed.product_feature_id == "faqs"
    assert tele.get("home_evidence_guarded", 0) >= 1


def test_anticase_home_noun_echo_guard():
    """(8, offline-drive forensics) 'Manage dataroom FAQs': faq-CRUD flows
    under the datarooms API subtree — zero structural home match, but the
    flow names / resource echo the home noun — dataroom-SCOPED faqs stay
    under PF faqs."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    # Entries OUTSIDE every faq-anchor prefix (the real rebuilt registry's
    # shape: the merged faq anchor does NOT cover the datarooms/[id]/faqs
    # subtree) — home_share is exactly 0.0; only the NAME/resource echo
    # ties the row home.
    fl_f1 = Fl("f-f1", "pages/api/teams/t/datarooms/d/qa/index.ts",
               "manage-team-dataroom-faqs-flow")
    fl_f2 = Fl("f-f2", "pages/api/teams/t/datarooms/d/qa/one.ts",
               "manage-team-dataroom-faq-by-faq-id-flow")
    devs.append(Dev("d4", [fl_f1, fl_f2]))
    scoped = UF("Manage dataroom FAQs", "faqs", ["f-f1", "f-f2"],
                resource="faq")
    ufs = [sick, real_faqs, keeper, scoped]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert scoped.product_feature_id == "faqs"
    assert tele.get("home_echo_guarded", 0) >= 1
    # …and the actual disease row still fires alongside the guards:
    assert sick.product_feature_id == "datarooms"


def test_fold_rung3_resource_twin():
    """The rehomed seed dissolves into the organic receiver journey of the
    SAME singular-folded resource even with no member overlap and a
    different name ('View faqs' res=datarooms → 'Browse and filter
    datarooms' res=dataroom)."""
    registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
    fl_x = Fl("f-x", "pages/datarooms/settings.tsx", "dataroom-settings-flow")
    devs.append(Dev("d5", [fl_x]))
    organic = UF("Browse and filter datarooms", "datarooms", ["f-x"])
    organic.resource = "dataroom"
    ufs = [sick, real_faqs, keeper, organic]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert tele["folded"] == 1
    assert sick not in ufs
    assert organic.member_flow_ids == ["f-x", "f-list", "f-view"]


def test_anticase_organic_rows_telemetry_only():
    """(9, cal.com control forensics) an ORGANIC row tripping the breadth
    ruler is a telemetry-only candidate — never moved (the rail's action
    scope is the synthesized seed class; organic mis-homes belong to the
    I16/B24 family)."""
    # MECHANICAL flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): the
    # telemetry-only law holds in the UNARMED world — under the flipped
    # FAULTLINE_MEGA_DECOMP_ARM default the organic candidates become
    # S5a Seg C ledger moves by design; pin the kill-switch.
    os.environ["FAULTLINE_MEGA_DECOMP_ARM"] = "0"
    try:
        registry, pfs, devs, sick, real_faqs, keeper = _papermark_scene()
        organic_sick = UF("Reset forgotten password", "faqs",
                          ["f-list", "f-view"])  # organic, zero home tie
        ufs = [organic_sick, real_faqs, keeper]
        tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    finally:
        os.environ.pop("FAULTLINE_MEGA_DECOMP_ARM", None)
    assert organic_sick.product_feature_id == "faqs"  # untouched
    assert tele["rehomed"] == 0 and tele["folded"] == 0
    assert tele["organic_candidates"] == 1
    assert tele["organic_candidate_rows"][0]["to"] == "datarooms"
