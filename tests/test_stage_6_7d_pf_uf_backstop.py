"""Tests for the Stage 6.7d PF-UF backstop.

Operator invariant (2026-07-05, CRITICAL): a product feature whose member
devs own >= 1 flow but which NO user flow references (validator I8, "фіча
без юзер-фловів") must never ship. The backstop, run inside ``_finish``:
  1. REASSIGNS journeys majority-owned by the uncovered PF's devs (donor
     keeps >= 1 journey);
  2. else SYNTHESIZES one thin tagged journey from the PF's highest-LOC
     flows (output-only — the FAULTLINE_SEED_SYSTEM_UFS precedent).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, FlowLineRange, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _REASON_PROMOTED,
    _REASON_UNCOVERED,
    _backstop_uncovered_pfs,
    _pf_uf_backstop_enabled,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _flow(uuid: str, loc: int = 10) -> Flow:
    return Flow(
        name=f"{uuid}-flow", uuid=uuid, paths=[f"src/{uuid}.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0,
        line_ranges=[FlowLineRange(path=f"src/{uuid}.py", start_line=1,
                                   end_line=loc)],
    )


def _dev(name: str, flows: list[Flow]) -> Feature:
    return Feature(
        name=name, display_name=name, paths=[f"src/{name}/a.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, layer="developer",
        flows=flows,
    )


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, paths=[f"src/{slug}/a.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, layer="product",
    )


def _uf(name: str, pfid: str | None, members: list[str]) -> UserFlow:
    return UserFlow(
        id="UF-000", name=name, resource=name.lower(), intent="manage",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members),
    )


# ── 1. reassignment (promotion subclass A) ─────────────────────────────


def test_reassigns_majority_owned_journey():
    """A journey whose members are majority-owned by the uncovered PF's dev
    moves to that PF; the donor keeps its second journey. No synthesis."""
    devs = [
        _dev("anomalies", [_flow("f1"), _flow("f2"), _flow("f3")]),
        _dev("cases", [_flow("f4")]),
    ]
    d2p = {"anomalies": ("anomalies",), "cases": ("cases",)}
    pfs = [_pf("anomalies", "Anomalies"), _pf("cases", "Cases")]
    ufs = [
        _uf("Investigate anomalies", "cases", ["f1", "f2"]),  # 2/2 owned by anomalies
        _uf("Manage cases", "cases", ["f4"]),
    ]
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    assert ufs[0].product_feature_id == "anomalies"
    assert ufs[1].product_feature_id == "cases"
    assert tele["pf_backstop_reassigned_ufs"] == 1
    assert tele["pf_backstop_synthesized"] == 0
    assert len(ufs) == 2  # nothing appended
    assert not ufs[0].synthesized  # reassigned journey is NOT synthetic


def test_donor_never_loses_its_only_journey():
    """Reassignment must not trade one I8 violation for another: when the
    majority-owned journey is the donor's ONLY one, synthesize instead."""
    devs = [
        _dev("anomalies", [_flow("f1"), _flow("f2"), _flow("f3", loc=50)]),
        _dev("cases", [_flow("f4")]),
    ]
    d2p = {"anomalies": ("anomalies",), "cases": ("cases",)}
    pfs = [_pf("anomalies", "Anomalies"), _pf("cases", "Cases")]
    ufs = [_uf("Investigate anomalies", "cases", ["f1", "f2"])]
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    # donor journey untouched; anomalies got a synthesized journey instead
    assert ufs[0].product_feature_id == "cases"
    assert tele["pf_backstop_reassigned_ufs"] == 0
    assert tele["pf_backstop_synthesized"] == 1
    synth = [u for u in ufs if u.synthesized]
    assert len(synth) == 1
    assert synth[0].product_feature_id == "anomalies"


def test_minority_share_does_not_reassign():
    """A journey with only a minority of members owned by the uncovered PF
    stays put — the PF is served by synthesis."""
    devs = [
        _dev("anomalies", [_flow("f1")]),
        _dev("cases", [_flow("f2"), _flow("f3"), _flow("f4")]),
    ]
    d2p = {"anomalies": ("anomalies",), "cases": ("cases",)}
    pfs = [_pf("anomalies", "Anomalies"), _pf("cases", "Cases")]
    ufs = [
        _uf("Triage work", "cases", ["f1", "f2", "f3"]),  # 1/3 anomalies
        _uf("Manage cases", "cases", ["f4"]),
    ]
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    assert ufs[0].product_feature_id == "cases"
    assert tele["pf_backstop_synthesized"] == 1


# ── 2. synthesis ────────────────────────────────────────────────────────


def test_synthesizes_thin_journey_for_promoted_capability():
    """Subclass (A): a residual-guard-promoted capability with no
    reassignable journey gets a thin journey tagged with the PROMOTED
    reason, members = its unclaimed flows by descending LOC."""
    devs = [_dev("network-security", [_flow("f1", loc=5), _flow("f2", loc=90)])]
    d2p = {"network-security": ("network-security",)}
    pfs = [_pf("network-security", "Network Security")]
    ufs: list[UserFlow] = [_uf("Other journey", "other", ["x1"])]
    tele = _backstop_uncovered_pfs(
        ufs, pfs, d2p, devs, promoted_caps={"Network Security"})
    synth = [u for u in ufs if u.synthesized]
    assert len(synth) == 1
    uf = synth[0]
    assert uf.synthesis_reason == _REASON_PROMOTED
    assert uf.product_feature_id == "network-security"
    assert uf.name == "Network Security"
    assert uf.member_flow_ids == ["f2", "f1"]  # highest-LOC first
    assert uf.member_count == 2
    assert uf.name_confidence == "low"
    assert tele["pf_backstop_synthesized"] == 1


def test_synthesizes_with_uncovered_reason_for_draw_gap():
    """Subclass (B): a draw-emitted capability nobody referenced gets the
    UNCOVERED reason."""
    devs = [_dev("ticketing", [_flow("f1")])]
    d2p = {"ticketing": ("ticketing",)}
    pfs = [_pf("ticketing", "Ticketing")]
    ufs: list[UserFlow] = []
    _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    assert len(ufs) == 1
    assert ufs[0].synthesis_reason == _REASON_UNCOVERED


def test_member_cap_and_loc_order():
    """Members are capped and picked by descending LOC (ties by id)."""
    flows = [_flow(f"f{i:02d}", loc=i) for i in range(1, 13)]
    devs = [_dev("inventory", flows)]
    d2p = {"inventory": ("inventory",)}
    pfs = [_pf("inventory", "Inventory")]
    ufs: list[UserFlow] = []
    _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    got = ufs[0].member_flow_ids
    assert len(got) == 8  # _BACKSTOP_MEMBER_CAP
    assert got[0] == "f12" and got[-1] == "f05"  # LOC desc


def test_all_flows_claimed_still_synthesizes():
    """Board completeness: when every owned flow is already claimed by
    other journeys (minority shares), the thin journey still references
    the top flows — it is tagged, so eval excludes it."""
    devs = [
        _dev("shared-platform", [_flow("f1"), _flow("f2")]),
        _dev("cases", [_flow("f3"), _flow("f4"), _flow("f5")]),
    ]
    d2p = {"shared-platform": ("shared-platform",), "cases": ("cases",)}
    pfs = [_pf("shared-platform", "Shared Platform"), _pf("cases", "Cases")]
    ufs = [
        _uf("Journey A", "cases", ["f1", "f3", "f4"]),  # claims f1 (minority)
        _uf("Journey B", "cases", ["f2", "f5"]),        # claims f2 (tie, not majority)
    ]
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    synth = [u for u in ufs if u.synthesized]
    assert len(synth) == 1
    assert set(synth[0].member_flow_ids) <= {"f1", "f2"}
    assert tele["pf_backstop_synthesized"] == 1


# ── 3. no-op / guard behaviour ──────────────────────────────────────────


def test_covered_pf_gets_no_duplicate():
    """A PF the draw already covered is untouched — no reassign, no synth."""
    devs = [_dev("cases", [_flow("f1")])]
    d2p = {"cases": ("cases",)}
    pfs = [_pf("cases", "Cases")]
    ufs = [_uf("Manage cases", "cases", ["f1"])]
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    assert len(ufs) == 1
    assert tele["pf_backstop_uncovered"] == 0
    assert tele["pf_backstop_synthesized"] == 0


def test_flowless_pf_is_skipped():
    """A PF whose devs own zero flows is NOT an I8 violation — no backstop."""
    devs = [_dev("docs", [])]
    d2p = {"docs": ("docs",)}
    pfs = [_pf("docs", "Docs")]
    ufs: list[UserFlow] = []
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    assert not ufs
    assert tele["pf_backstop_uncovered"] == 0


def test_kill_switch_env(monkeypatch):
    monkeypatch.delenv("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", raising=False)
    assert _pf_uf_backstop_enabled() is True
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", "0")
    assert _pf_uf_backstop_enabled() is False


# ── 4. tags + serialization contract ────────────────────────────────────


def test_tag_presence_and_omitted_when_default():
    """Synthesized UFs dump the tags; ordinary UFs dump WITHOUT the keys
    (snapshot byte-identity for every pre-existing scan shape)."""
    plain = _uf("Manage cases", "cases", ["f1"])
    dump = plain.model_dump()
    assert "synthesized" not in dump
    assert "synthesis_reason" not in dump

    devs = [_dev("ticketing", [_flow("f1")])]
    ufs: list[UserFlow] = []
    _backstop_uncovered_pfs(
        ufs, [_pf("ticketing", "Ticketing")],
        {"ticketing": ("ticketing",)}, devs, set())
    dump = ufs[0].model_dump()
    assert dump["synthesized"] is True
    assert dump["synthesis_reason"] == _REASON_UNCOVERED


def test_old_json_rehydrates_without_tags():
    """Pre-backstop UF JSON (no tag keys) rehydrates with defaults."""
    uf = UserFlow(id="UF-001", name="n", resource="r", intent="manage")
    raw = uf.model_dump()
    again = UserFlow(**raw)
    assert again.synthesized is False
    assert again.synthesis_reason is None


# ── 5. determinism ──────────────────────────────────────────────────────


def test_deterministic_across_runs():
    """Two runs over deep-copied identical inputs produce identical
    journeys, ordering, and telemetry."""
    def build():
        devs = [
            _dev("anomalies", [_flow("f1", loc=7), _flow("f2", loc=30)]),
            _dev("cases", [_flow("f3"), _flow("f4")]),
            _dev("ticketing", [_flow("f5", loc=3)]),
        ]
        d2p = {"anomalies": ("anomalies",), "cases": ("cases",),
               "ticketing": ("ticketing",)}
        pfs = [_pf("anomalies", "Anomalies"), _pf("cases", "Cases"),
               _pf("ticketing", "Ticketing")]
        ufs = [_uf("Manage cases", "cases", ["f3", "f4"])]
        return devs, d2p, pfs, ufs

    results = []
    for _ in range(2):
        devs, d2p, pfs, ufs = copy.deepcopy(build())
        tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, {"Anomalies"})
        results.append(([u.model_dump() for u in ufs], tele))
    assert results[0] == results[1]
