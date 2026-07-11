"""B40 — provenance-graded name_confidence + ``name_evidence`` audit trail.

FAULTLINE_NAME_EVIDENCE_RUNGS (default OFF) arms two extra grounding rungs in
Law C — a nav-label token match (resource-grounding) and an all-dispatch-member
registry provenance (verb-grounding for Run/act leads) — and stamps
``UserFlow.name_evidence`` in every arm (fired rungs, or ``missing:*`` for a
low). It also folds ``_stem`` singularization into synth_quality's multi-member
agreement so plural/singular object mismatches stop degrading the low→medium
lift. Doctrine: an uplift fires ONLY on a genuine rung; UF NAMES are byte-stable
whether the flag is on or off (only name_confidence/name_evidence may differ).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.naming_contract import (
    NAME_EVIDENCE_RUNGS_ENV,
    _apply_uf_name_laws,
    load_naming_vocab,
    name_evidence_rungs_enabled,
)
from faultline.pipeline_v2.synth_quality import (
    _derive_multi_member_name,
    _verb_class_index,
)

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def _pf(slug: str, display: str) -> Feature:
    f = Feature(name=slug, paths=[], authors=[], total_commits=0, bug_fixes=0,
                bug_fix_ratio=0.0, last_modified=_EPOCH, health_score=80.0,
                layer="product")
    f.display_name = display
    return f


def _uf(uid: str, name: str, pfid: str, members: list[str], *,
        resource: str = "thing", domain: str | None = None,
        conf: str = "high") -> UserFlow:
    return UserFlow(
        id=uid, name=name, intent="manage", resource=resource,
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), domain=domain, name_confidence=conf)


def _apply(ufs, pfs, flow_names, *, nav=None, origin=None, authored=frozenset()):
    vocab = load_naming_vocab()
    pf_by_slug = {str(p.name): p for p in pfs}
    tele: dict = {}
    _apply_uf_name_laws(ufs, pf_by_slug, vocab, flow_names, tele,
                        authored_ids=set(authored), keeper_on=True,
                        nav_labels=nav or {}, flow_origin_by_id=origin or {})
    return tele


# ── flag plumbing ────────────────────────────────────────────────────────

def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NAME_EVIDENCE_RUNGS_ENV, raising=False)
    assert name_evidence_rungs_enabled() is False
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    assert name_evidence_rungs_enabled() is True
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "0")
    assert name_evidence_rungs_enabled() is False


# ── nav rung — resource grounding from the author's nav label ─────────────

def _nav_scene():
    # verb grounded by member (lead None + a member action), resource NOT
    # grounded by member/pf tokens (so only the nav label can lift it).
    flow_names = {"m1": "run-job-flow"}
    ufs = [_uf("UF-1", "Manage accounts", "acct", ["m1"],
               resource="account", conf="high")]
    pfs = [_pf("acct", "Jobs")]
    return ufs, pfs, flow_names


def test_nav_rung_lifts_only_on_token_match(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    # no nav label → resource ungrounded → low.
    ufs, pfs, fn = _nav_scene()
    _apply(ufs, pfs, fn)
    assert ufs[0].name_confidence == "low"
    assert ufs[0].name_evidence == ["missing:resource"]

    # nav label whose token ("accounts") matches the UF name → resource
    # grounded → high, stamped "nav".
    ufs, pfs, fn = _nav_scene()
    _apply(ufs, pfs, fn, nav={"acct": "Accounts"})
    assert ufs[0].name_confidence == "high"
    assert ufs[0].name_evidence == ["nav"]


def test_nav_rung_no_uplift_without_token_match(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    ufs, pfs, fn = _nav_scene()
    # nav label shares NO token with "Manage accounts" → no uplift.
    _apply(ufs, pfs, fn, nav={"acct": "Billing"})
    assert ufs[0].name_confidence == "low"
    assert ufs[0].name_evidence == ["missing:resource"]


# ── registry rung — all-dispatch-member provenance ────────────────────────

def _registry_scene():
    # resource grounded (member echoes "report"); verb NOT grounded (lead
    # "act" from "Run", members are read-class) — so only registry can lift.
    flow_names = {"m1": "view-report-flow"}
    ufs = [_uf("UF-1", "Run reports", "rep", ["m1"], resource="report",
               conf="high")]
    pfs = [_pf("rep", "Reports")]
    return ufs, pfs, flow_names


def test_registry_rung_requires_all_members_dispatch(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    # sole member IS a dispatch mint → verb grounded via registry → high.
    ufs, pfs, fn = _registry_scene()
    _apply(ufs, pfs, fn, origin={"m1": "dispatch"})
    assert ufs[0].name_confidence == "high"
    assert ufs[0].name_evidence == ["registry"]

    # not a dispatch mint → verb ungrounded → medium (resource only).
    ufs, pfs, fn = _registry_scene()
    _apply(ufs, pfs, fn)
    assert ufs[0].name_confidence == "medium"
    assert ufs[0].name_evidence == ["resource", "missing:verb"]


def test_registry_rung_partial_membership_no_uplift(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    flow_names = {"m1": "view-report-flow", "m2": "view-report-detail-flow"}
    ufs = [_uf("UF-1", "Run reports", "rep", ["m1", "m2"], resource="report")]
    pfs = [_pf("rep", "Reports")]
    # only ONE of two members is a dispatch mint → registry rung does NOT fire.
    _apply(ufs, pfs, flow_names, origin={"m1": "dispatch"})
    assert ufs[0].name_confidence == "medium"
    assert "registry" not in (ufs[0].name_evidence or [])


# ── member-less rows never uplift ─────────────────────────────────────────

def test_memberless_never_uplifts(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    ufs = [_uf("UF-1", "Manage widgets", "widgets", [], resource="widget",
               conf="high")]
    # even with a matching nav label a member-LESS row stays low by rubric.
    _apply(ufs, [_pf("widgets", "Widgets")], {}, nav={"widgets": "Widgets"})
    assert ufs[0].name_confidence == "low"
    assert ufs[0].name_evidence == ["missing:members"]


# ── structural-route evidence on a member-grounded high ───────────────────

def test_structural_route_stamped_on_member_grounded_high(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "1")
    flow_names = {"c": "create-invoice-flow", "l": "list-invoices-flow"}
    ufs = [_uf("UF-1", "Manage invoices", "invoices", ["c", "l"],
               resource="invoice", conf="low")]
    _apply(ufs, [_pf("invoices", "Invoices")], flow_names)
    assert ufs[0].name_confidence == "high"
    assert ufs[0].name_evidence == ["structural-route"]


# ── flag OFF: byte-identical confidence + no name_evidence ────────────────

def test_flag_off_no_evidence_and_confidence_unchanged(monkeypatch) -> None:
    monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, "0")
    # the nav scene WOULD be high under ON; OFF it must stay at the base
    # rubric (low) and carry NO name_evidence field.
    ufs, pfs, fn = _nav_scene()
    _apply(ufs, pfs, fn, nav={"acct": "Accounts"})
    assert ufs[0].name_confidence == "low"
    assert ufs[0].name_evidence is None

    # registry scene likewise unchanged (medium) with no evidence.
    ufs, pfs, fn = _registry_scene()
    _apply(ufs, pfs, fn, origin={"m1": "dispatch"})
    assert ufs[0].name_confidence == "medium"
    assert ufs[0].name_evidence is None


def test_names_byte_identical_on_vs_off(monkeypatch) -> None:
    def _run(flag: str) -> list[str]:
        monkeypatch.setenv(NAME_EVIDENCE_RUNGS_ENV, flag)
        ufs = [
            _uf("UF-1", "Manage accounts", "acct", ["m1"], resource="account"),
            _uf("UF-2", "Run reports", "rep", ["m2"], resource="report"),
            _uf("UF-3", "Manage widgets", "widgets", [], resource="widget"),
        ]
        pfs = [_pf("acct", "Jobs"), _pf("rep", "Reports"),
               _pf("widgets", "Widgets")]
        fn = {"m1": "run-job-flow", "m2": "view-report-flow"}
        _apply(ufs, pfs, fn, nav={"acct": "Accounts"},
               origin={"m2": "dispatch"})
        return [u.name for u in ufs]

    assert _run("1") == _run("0")  # names never change with the flag


# ── synth_quality: singular-folded member agreement ───────────────────────

def _vc():
    return _verb_class_index(load_naming_vocab())


def test_stem_widened_agreement_counts_singular_plural() -> None:
    vc = _vc()
    # two members whose objects differ only by plural: onboarding / onboardings.
    members = ["fetch-onboarding-flow", "fetch-onboardings-flow"]
    cand_off, strong_off = _derive_multi_member_name(
        members, "onboarding", "Widgets", "Manage widgets", vc,
        stem_agreement=False)
    cand_on, strong_on = _derive_multi_member_name(
        members, "onboarding", "Widgets", "Manage widgets", vc,
        stem_agreement=True)
    # name byte-identical; only the strong (confidence) signal changes.
    assert cand_on == cand_off == "Ingest onboarding"
    assert strong_off is False
    assert strong_on is True


def test_stem_agreement_anticase_distinct_objects_stay_weak() -> None:
    vc = _vc()
    # distinct objects (onboarding / billing) do NOT concur → no widening.
    members = ["fetch-onboarding-flow", "fetch-billing-flow"]
    _, strong_on = _derive_multi_member_name(
        members, "onboarding", "Widgets", "Manage widgets", vc,
        stem_agreement=True)
    assert strong_on is False
