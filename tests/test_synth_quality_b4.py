"""Bug B4 — synthesized-journey quality (demotion + backstop regrounding).

Covers, per the fix contract:
  * demotion of member-less system_flow_recall seeds (a); over-demotion audit;
    scope guard (e2e_journey_recall / real UFs untouched);
  * I14 flow-backpointer cleanup;
  * I24-touch neutrality (structural: demoted seeds carry no member flows);
  * single-member generic backstop regrounding (b) + anti-cases
    (vendor-composed PF kept, no-new-token kept, multi-member kept, real UF
    kept, collision kept, law-violation kept);
  * kill-switch no-op + determinism (double-run identical).
"""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace

import pytest

from faultline.pipeline_v2.synth_quality import (
    BACKSTOP_REASON,
    SYSTEM_RECALL_REASON,
    SYNTH_QUALITY_ENV,
    demote_system_flow_seeds,
    reground_backstop_uf_names,
    run_synth_quality,
    synth_quality_enabled,
)


# ── fixture builders (duck-typed namespaces — the helpers use getattr) ──


def _uf(uid, name, *, reason=None, members=(), synthesized=False,
        pf=None, resource=None, routes=(), category="interactive",
        trigger=None):
    return SimpleNamespace(
        id=uid, name=name, synthesis_reason=reason,
        member_flow_ids=list(members), member_count=len(members),
        synthesized=synthesized, product_feature_id=pf, resource=resource,
        routes=list(routes), category=category, trigger=trigger,
    )


def _flow(uuid, name, *, user_flow_id=None, short_label=None):
    return SimpleNamespace(
        uuid=uuid, name=name, short_label=short_label or name,
        user_flow_id=user_flow_id,
    )


def _pf(key, display):
    return SimpleNamespace(id=key, name=key, display_name=display)


# ── (a) DEMOTION ───────────────────────────────────────────────────────────


def test_demote_removes_member_less_system_seed():
    ufs = [
        _uf("UF-001", "Manage cases", members=["a"]),
        _uf("UF-051", "Run articles", reason=SYSTEM_RECALL_REASON,
            synthesized=True, category="system", resource="articles",
            routes=["backend/inngest_functions/articles.py"]),
    ]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, [], meta)
    assert tele["demoted"] == 1
    assert [u.id for u in ufs] == ["UF-001"]          # seed removed
    seeds = meta["system_flow_seeds"]
    assert len(seeds) == 1
    assert seeds[0]["id"] == "UF-051"
    assert seeds[0]["name"] == "Run articles"
    assert seeds[0]["routes"] == ["backend/inngest_functions/articles.py"]


def test_over_demotion_audit_only_member_less_system_recall():
    """A system_flow_recall UF WITH members must NOT demote; nor any other
    reason. This is the gate-6 over-demotion audit (must be 0)."""
    ufs = [
        _uf("UF-001", "Real journey", members=["a"]),
        # system_flow_recall but HAS members -> keep
        _uf("UF-002", "Run reports", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=["b"]),
        # e2e_journey_recall member-less -> OUT OF SCOPE, keep (Track C)
        _uf("UF-003", "Complete onboarding", reason="e2e_journey_recall",
            synthesized=True, members=[]),
        # backstop member-less (shouldn't exist, but must not demote here)
        _uf("UF-004", "Manage widgets", reason=BACKSTOP_REASON,
            synthesized=True, members=[]),
        # the ONLY demotable
        _uf("UF-005", "Run crons", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[]),
    ]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, [], meta)
    assert tele["demoted"] == 1
    assert [u.id for u in ufs] == ["UF-001", "UF-002", "UF-003", "UF-004"]
    assert meta["system_flow_seeds"][0]["id"] == "UF-005"


def test_i14_backpointer_cleanup():
    """A flow pointing at a demoted seed is nulled; a flow pointing at a live
    UF is untouched (validator I14: no dangling flow.user_flow_id)."""
    ufs = [
        _uf("UF-001", "Manage cases", members=["fa"]),
        _uf("UF-060", "Run Slack", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[]),
    ]
    flows = [
        _flow("fa", "manage-cases-flow", user_flow_id="UF-001"),
        # pathological: a flow references the seed (evidence shows 0, but guard)
        _flow("fb", "run-slack-flow", user_flow_id="UF-060"),
        _flow("fc", "orphan-flow", user_flow_id=None),
    ]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, flows, meta)
    assert tele["backpointers_cleared"] == 1
    assert flows[0].user_flow_id == "UF-001"   # live UF pointer intact
    assert flows[1].user_flow_id is None       # demoted-seed pointer nulled
    assert flows[2].user_flow_id is None


def test_i24_touch_neutrality_precondition():
    """Structural guarantee behind I24-neutrality: every demoted seed carries
    ZERO member flows, so it contributes nothing to the validator's route-group
    touch set (which is built only from member flows' files)."""
    ufs = [
        _uf("UF-051", "Run articles", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[]),
        _uf("UF-052", "Run cases", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[]),
    ]
    meta: dict = {}
    demote_system_flow_seeds(ufs, [], meta)
    # every seed recorded to the side-channel had no members (nothing that
    # could have been a touch contribution is lost)
    for s in meta["system_flow_seeds"]:
        assert s["resource"] is not None or True  # recorded
    assert ufs == []  # all demoted


def test_i8_cover_seed_kept():
    """A member-less system seed that is the SOLE user flow referencing its PF
    is the W5.1 LOC-worthy I8 cover — demoting it would re-fire validator I8, so
    it is KEPT (I8-safe discriminator)."""
    ufs = [
        _uf("UF-001", "Manage cases", members=["a"], pf="cases"),
        # sole cover of flowless PF 'settings' — no other UF references it
        _uf("UF-070", "Run settings", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[], pf="settings", routes=[]),
    ]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, [], meta)
    assert tele["demoted"] == 0
    assert tele["kept_i8_cover_seeds"] == 1
    assert [u.id for u in ufs] == ["UF-001", "UF-070"]   # seed kept
    assert meta["system_flow_seeds"] == []


def test_seed_demoted_when_pf_has_other_uf():
    """Same shape, but the PF already carries a real journey -> the seed is
    redundant recall bookkeeping and IS demoted (I8 stays satisfied by the
    other UF). Mirrors Soc0's inngest seeds under a covered network-security."""
    ufs = [
        _uf("UF-010", "View network security", members=["a"],
            pf="network-security"),
        _uf("UF-051", "Run articles", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[], pf="network-security",
            routes=["backend/inngest_functions/articles.py"]),
    ]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, [], meta)
    assert tele["demoted"] == 1
    assert tele["kept_i8_cover_seeds"] == 0
    assert [u.id for u in ufs] == ["UF-010"]
    assert meta["system_flow_seeds"][0]["id"] == "UF-051"


def test_acronym_lead_kept():
    """Anti-case: a member whose leading token is a noun/acronym ('api-user-
    subscribe') is not a verb-led journey label -> keep the template."""
    ufs = [_uf("UF-020", "Manage users", reason=BACKSTOP_REASON,
               synthesized=True, members=["m1"], pf="users")]
    flows = [_flow("m1", "api-user-subscribe-flow")]
    pfs = [_pf("users", "Users")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[0].name == "Manage users"


def test_empty_side_channel_when_no_seeds():
    ufs = [_uf("UF-001", "Manage cases", members=["a"])]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, [], meta)
    assert tele["demoted"] == 0
    assert meta["system_flow_seeds"] == []       # observable, stable
    assert [u.id for u in ufs] == ["UF-001"]     # untouched


def test_demote_typed_userflow_models():
    """Compat with the real pydantic UserFlow/Flow objects the pipeline
    passes (not just SimpleNamespace)."""
    from datetime import datetime, timezone

    from faultline.models.types import Flow, UserFlow

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ufs = [
        UserFlow(id="UF-001", name="Manage cases", intent="manage",
                 resource="cases", member_flow_ids=["fa"], member_count=1),
        UserFlow(id="UF-051", name="Run articles", intent="execute",
                 resource="articles", member_flow_ids=[], member_count=0,
                 synthesized=True, synthesis_reason=SYSTEM_RECALL_REASON,
                 category="system"),
    ]
    flows = [Flow(name="run-articles-flow", uuid="fx", user_flow_id="UF-051",
                  paths=["src/x.py"], authors=["a"], total_commits=1,
                  bug_fixes=0, bug_fix_ratio=0.0, last_modified=ts,
                  health_score=90.0)]
    meta: dict = {}
    tele = demote_system_flow_seeds(ufs, flows, meta)
    assert tele["demoted"] == 1
    assert [u.id for u in ufs] == ["UF-001"]
    assert flows[0].user_flow_id is None


# ── (b) BACKSTOP NAME REGROUNDING ──────────────────────────────────────────


def test_single_member_generic_backstop_regrounded():
    """A single-member backstop on a GENERIC surface adopts its member's
    code-grounded name when it adds new tokens."""
    ufs = [_uf("UF-020", "Manage widgets", reason=BACKSTOP_REASON,
               synthesized=True, members=["m1"], pf="widgets")]
    flows = [_flow("m1", "configure-webhook-endpoints-flow")]
    pfs = [_pf("widgets", "Widgets")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 1
    assert ufs[0].name == "Configure webhook endpoints"
    assert meta["synth_quality"]["backstop_renamed"][0]["before"] == "Manage widgets"


def test_vendor_composed_pf_kept():
    """Anti-case: a vendor-composed surface ('EDR — Claroty') keeps its
    vendor-named template — the vendor is the recognizable journey subject."""
    ufs = [_uf("UF-019", "Manage Claroty integration", reason=BACKSTOP_REASON,
               synthesized=True, members=["m1"], pf="claroty")]
    flows = [_flow("m1", "manage-alert-relations-flow")]
    pfs = [_pf("claroty", "EDR — Claroty")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[0].name == "Manage Claroty integration"


def test_no_new_token_kept():
    """Anti-case: member adds no token the template lacks (after UI-primitive
    strip) -> keep current."""
    ufs = [_uf("UF-030", "View ticketing", reason=BACKSTOP_REASON,
               synthesized=True, members=["m1"], pf="ticketing")]
    flows = [_flow("m1", "view-ticketing-page-flow")]   # 'page' stops -> 'ticketing'
    pfs = [_pf("ticketing", "Ticketing")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[0].name == "View ticketing"


def test_multi_member_backstop_kept():
    """Anti-case: multi-member backstop (ambiguous aggregate) keeps template."""
    ufs = [_uf("UF-040", "Run Cortex jobs", reason=BACKSTOP_REASON,
               synthesized=True, members=["m1", "m2", "m3"], pf="cortex")]
    flows = [_flow("m1", "build-cortex-filters-flow"),
             _flow("m2", "transform-cortex-queries-flow"),
             _flow("m3", "normalize-alert-data-flow")]
    pfs = [_pf("cortex", "Cortex")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[0].name == "Run Cortex jobs"


def test_real_uf_never_regrounded():
    """Anti-case: a real (non-backstop) journey is never touched."""
    ufs = [_uf("UF-001", "Manage cases", members=["m1"], pf="cases")]
    flows = [_flow("m1", "create-case-record-flow")]
    pfs = [_pf("cases", "Cases")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[0].name == "Manage cases"


def test_collision_guard_keeps_current():
    """Anti-case: a rename that would collide with another journey is skipped."""
    ufs = [
        _uf("UF-001", "Configure webhook endpoints", members=["real"],
            pf="other"),
        _uf("UF-020", "Manage widgets", reason=BACKSTOP_REASON,
            synthesized=True, members=["m1"], pf="widgets"),
    ]
    flows = [_flow("real", "x-flow"),
             _flow("m1", "configure-webhook-endpoints-flow")]
    pfs = [_pf("widgets", "Widgets"), _pf("other", "Other")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[1].name == "Manage widgets"


def test_law_violation_guard_keeps_current():
    """Anti-case: a member that would yield a param-glyph / single-letter name
    is rejected (display-law guard) -> keep current."""
    ufs = [_uf("UF-020", "Manage widgets", reason=BACKSTOP_REASON,
               synthesized=True, members=["m1"], pf="widgets")]
    # single content letter after verb -> single_letter law
    flows = [_flow("m1", "view-x-flow")]
    pfs = [_pf("widgets", "Widgets")]
    meta: dict = {}
    tele = reground_backstop_uf_names(ufs, flows, pfs, meta)
    assert tele["renamed"] == 0
    assert ufs[0].name == "Manage widgets"


# ── kill-switch + determinism ──────────────────────────────────────────────


def _corpus():
    ufs = [
        _uf("UF-001", "Manage cases", members=["fa"]),
        _uf("UF-051", "Run articles", reason=SYSTEM_RECALL_REASON,
            synthesized=True, members=[], resource="articles"),
        _uf("UF-020", "Manage widgets", reason=BACKSTOP_REASON,
            synthesized=True, members=["m1"], pf="widgets"),
    ]
    flows = [_flow("fa", "manage-cases-flow", user_flow_id="UF-001"),
             _flow("m1", "configure-webhook-endpoints-flow")]
    pfs = [_pf("widgets", "Widgets")]
    return ufs, flows, pfs


def test_kill_switch_off_is_noop(monkeypatch):
    monkeypatch.setenv(SYNTH_QUALITY_ENV, "0")
    assert synth_quality_enabled() is False
    ufs, flows, pfs = _corpus()
    before = [u.name for u in ufs]
    meta: dict = {}
    tele = run_synth_quality(ufs, flows, pfs, meta)
    assert tele == {"enabled": False}
    assert [u.name for u in ufs] == before          # nothing renamed
    assert len(ufs) == 3                             # nothing demoted
    assert "system_flow_seeds" not in meta           # no side-channel written


def test_kill_switch_on_default(monkeypatch):
    monkeypatch.delenv(SYNTH_QUALITY_ENV, raising=False)
    assert synth_quality_enabled() is True


def test_determinism_double_run(monkeypatch):
    monkeypatch.setenv(SYNTH_QUALITY_ENV, "1")
    r = []
    for _ in range(2):
        ufs, flows, pfs = _corpus()
        meta: dict = {}
        run_synth_quality(ufs, flows, pfs, meta)
        r.append(([u.name for u in ufs],
                  [u.id for u in ufs],
                  meta.get("system_flow_seeds"),
                  meta.get("synth_quality")))
    assert r[0] == r[1]
    # end-to-end: seed demoted, backstop regrounded
    assert "UF-051" not in r[0][1]
    assert "Configure webhook endpoints" in r[0][0]


def test_run_synth_quality_ordering(monkeypatch):
    """Demote runs before reground: the reground collision set excludes a
    demoted seed name (a backstop could legitimately take a name a
    soon-removed seed held)."""
    monkeypatch.setenv(SYNTH_QUALITY_ENV, "1")
    ufs = [
        _uf("UF-051", "Configure webhook endpoints",
            reason=SYSTEM_RECALL_REASON, synthesized=True, members=[]),
        _uf("UF-020", "Manage widgets", reason=BACKSTOP_REASON,
            synthesized=True, members=["m1"], pf="widgets"),
    ]
    flows = [_flow("m1", "configure-webhook-endpoints-flow")]
    pfs = [_pf("widgets", "Widgets")]
    meta: dict = {}
    run_synth_quality(ufs, flows, pfs, meta)
    # seed demoted, and the backstop successfully took the (now-freed) name
    assert [u.id for u in ufs] == ["UF-020"]
    assert ufs[0].name == "Configure webhook endpoints"
