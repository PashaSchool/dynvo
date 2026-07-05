"""Tests for the cross-scan UF identity keeper (output layer, opt-in).

Contract under test (rule-cold-scan compliance by construction):

* absent prev-scan input ⇒ output serialization is byte-identical to an
  engine without the keeper (``identity`` omitted when ``None``);
* deterministic matching (same inputs → same matches, in any list order);
* pinned UFs keep previous id + name, FK ``Flow.user_flow_id`` remapped;
* disappeared previous UFs are RETIRED in telemetry, never resurrected;
* structural threshold boundary (≥ 0.5 qualifies, < 0.5 does not);
* name similarity is a tie-break only, never an eligibility channel.
"""

from __future__ import annotations

import json

import pytest

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.uf_identity_keeper import (
    OVERLAP_THRESHOLD,
    apply_identity_keeper,
    load_prev_scan,
    match_user_flows,
)


def _uf(
    uf_id: str,
    name: str,
    *,
    members: list[str] | None = None,
    routes: list[str] | None = None,
    resource: str = "thing",
    intent: str = "manage",
) -> UserFlow:
    return UserFlow(
        id=uf_id,
        name=name,
        intent=intent,
        resource=resource,
        member_flow_ids=list(members or []),
        member_count=len(members or []),
        routes=list(routes or []),
    )


def _flow(name: str, user_flow_id: str) -> Flow:
    return Flow(
        name=name,
        paths=[],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified="2026-01-01T00:00:00+00:00",
        health_score=1.0,
        user_flow_id=user_flow_id,
    )


def _prev(ufs: list[UserFlow], run_id: str = "prev-run-1") -> dict:
    return {
        "scan_meta": {"run_id": run_id},
        "user_flows": [u.model_dump(mode="json") for u in ufs],
    }


# ── Absent input ⇒ byte-identity ───────────────────────────────────────


def test_identity_field_omitted_when_none():
    """A UF without identity serializes WITHOUT the key — the on-disk

    shape of a keeper-less scan is byte-identical to the pre-keeper
    engine (snapshot-gate digests stay valid)."""
    u = _uf("UF-001", "Manage detectors")
    dumped = u.model_dump(mode="json")
    assert "identity" not in dumped
    assert "identity" not in json.dumps(dumped)
    # python-mode dumps too (replay stage-input serialization path)
    assert "identity" not in u.model_dump()


def test_identity_field_present_when_set():
    u = _uf("UF-001", "Manage detectors")
    u.identity = {"pinned_from": "r1", "prev_id": "UF-009",
                  "match_basis": "member", "overlap": 1.0,
                  "renamed_prevented": True}
    dumped = u.model_dump(mode="json")
    assert dumped["identity"]["prev_id"] == "UF-009"


def test_old_json_rehydrates_without_identity():
    u = _uf("UF-001", "Manage detectors")
    data = u.model_dump(mode="json")
    assert UserFlow(**data).identity is None


# ── Matching: determinism + channels ───────────────────────────────────


def test_member_overlap_match_pins_id_and_name():
    prev = [_uf("UF-007", "Create & publish surveys",
                members=["a", "b", "c"], resource="survey", intent="author")]
    new = [_uf("UF-001", "Author survey lifecycle",
               members=["a", "b", "c"], resource="survey", intent="author")]
    flows = [_flow("f1", "UF-001")]
    tel = apply_identity_keeper(new, flows, _prev(prev))
    assert new[0].id == "UF-007"
    assert new[0].name == "Create & publish surveys"
    assert new[0].identity["match_basis"] == "member"
    assert new[0].identity["pinned_from"] == "prev-run-1"
    assert new[0].identity["renamed_prevented"] is True
    # FK follows the pin
    assert flows[0].user_flow_id == "UF-007"
    assert tel["pinned"] == 1 and tel["pin_rate"] == 1.0
    assert tel["fk_remapped"] == 1
    assert tel["retired"] == []


def test_route_overlap_carries_when_member_uuids_churn():
    """Production rescans regenerate flow uuids — routes survive."""
    prev = [_uf("UF-002", "Browse dashboards", members=["old1", "old2"],
                routes=["/dash", "/dash/[id]"], resource="dashboard",
                intent="browse")]
    new = [_uf("UF-001", "View dashboard pages", members=["new1", "new2"],
               routes=["/dash", "/dash/[id]"], resource="dashboard",
               intent="browse")]
    tel = apply_identity_keeper(new, [], _prev(prev))
    assert new[0].id == "UF-002"
    assert new[0].identity["match_basis"] == "route"
    assert tel["basis_counts"] == {"route": 1}


def test_unique_resource_intent_key_channel():
    """No structural overlap at all, but the (resource,intent) key is

    unique on both sides — an unambiguous journey identity."""
    prev = [_uf("UF-003", "Export invoices", members=["x"],
                resource="invoice", intent="export")]
    new = [_uf("UF-001", "Download invoice exports", members=["y"],
               resource="invoice", intent="export")]
    tel = apply_identity_keeper(new, [], _prev(prev))
    assert new[0].id == "UF-003"
    assert new[0].identity["match_basis"] == "resource-intent"
    assert tel["pinned"] == 1


def test_ambiguous_resource_intent_key_does_not_match():
    """Duplicate (resource,intent) keys are NOT an identity signal."""
    prev = [
        _uf("UF-001", "Manage teams A", members=["p1"], resource="team"),
        _uf("UF-002", "Manage teams B", members=["p2"], resource="team"),
    ]
    new = [_uf("UF-001", "Manage teams", members=["q1"], resource="team")]
    tel = apply_identity_keeper(new, [], _prev(prev))
    assert tel["pinned"] == 0
    assert new[0].identity is None
    assert len(tel["retired"]) == 2


def test_match_determinism_under_input_order():
    prev = [
        _uf("UF-001", "Alpha", members=["a1", "a2"], resource="alpha",
            intent="author"),
        _uf("UF-002", "Beta", members=["b1", "b2"], resource="beta",
            intent="browse"),
        _uf("UF-003", "Gamma", members=["c1", "c2"], resource="gamma",
            intent="manage"),
    ]
    new = [
        _uf("UF-001", "Gamma new", members=["c1", "c2"], resource="gamma",
            intent="manage"),
        _uf("UF-002", "Alpha new", members=["a1", "a2"], resource="alpha",
            intent="author"),
        _uf("UF-003", "Beta new", members=["b1", "b2"], resource="beta",
            intent="browse"),
    ]
    m1 = match_user_flows([u.model_dump() for u in prev], new)
    m2 = match_user_flows(
        [u.model_dump() for u in reversed(prev)], new)
    pairs1 = {(prev[m.prev_idx].id, new[m.new_idx].id) for m in m1}
    pairs2 = {(list(reversed(prev))[m.prev_idx].id, new[m.new_idx].id)
              for m in m2}
    assert pairs1 == pairs2 == {
        ("UF-003", "UF-001"), ("UF-001", "UF-002"), ("UF-002", "UF-003"),
    }


def test_name_similarity_is_tiebreak_only():
    """Identical structural overlap → the name-closer prev wins; but a

    high name similarity alone (zero structural overlap, ambiguous key)
    must NOT create a match."""
    # tie-break case: two prevs share the same members set
    prev = [
        _uf("UF-001", "Completely different label", members=["m1", "m2"],
            resource="doc", intent="author"),
        _uf("UF-002", "Sign documents", members=["m1", "m2"],
            resource="doc", intent="author"),
    ]
    new = [_uf("UF-001", "Sign documents", members=["m1", "m2"],
               resource="doc", intent="author")]
    matches = match_user_flows([u.model_dump() for u in prev], new)
    assert len(matches) == 1
    assert prev[matches[0].prev_idx].id == "UF-002"

    # name-only case: no members/routes shared, key ambiguous → no match
    prev2 = [
        _uf("UF-001", "Send reminder emails", members=["z1"], resource="email"),
        _uf("UF-002", "Send weekly emails", members=["z2"], resource="email"),
    ]
    new2 = [_uf("UF-001", "Send reminder emails", members=["z9"],
                resource="email")]
    assert match_user_flows([u.model_dump() for u in prev2], new2) == []


# ── Threshold boundary (structural 0.5) ────────────────────────────────


def test_threshold_boundary_exact_half_qualifies():
    # |A∩B| / |A∪B| = 2/4 = 0.5 → qualifies (>= threshold)
    prev = [_uf("UF-005", "Half overlap", members=["a", "b", "c"],
                resource="r1", intent="author")]
    new = [_uf("UF-001", "New half", members=["b", "c", "d"],
               resource="r2", intent="browse")]
    # ambiguity-proof: keys differ so only the structural channel exists
    assert _jaccard_of(prev[0], new[0]) == pytest.approx(0.5)
    matches = match_user_flows([u.model_dump() for u in prev], new)
    assert len(matches) == 1
    assert matches[0].overlap == pytest.approx(0.5)


def test_threshold_boundary_below_half_rejected():
    # 1/3 ≈ 0.33 < 0.5 → no structural match; keys differ → no key match
    prev = [_uf("UF-005", "Low overlap", members=["a", "b"],
                resource="r1", intent="author")]
    new = [_uf("UF-001", "New low", members=["b", "c"],
               resource="r2", intent="browse")]
    assert match_user_flows([u.model_dump() for u in prev], new) == []


def _jaccard_of(p: UserFlow, n: UserFlow) -> float:
    a, b = set(p.member_flow_ids), set(n.member_flow_ids)
    return len(a & b) / len(a | b)


# ── Retirement (no zombies) + collisions ───────────────────────────────


def test_retired_listed_never_resurrected():
    prev = [
        _uf("UF-001", "Kept journey", members=["k1", "k2"],
            resource="kept", intent="manage"),
        _uf("UF-002", "Gone journey", members=["g1", "g2"],
            resource="gone", intent="export"),
    ]
    new = [_uf("UF-001", "Kept journey renamed", members=["k1", "k2"],
               resource="kept", intent="manage")]
    tel = apply_identity_keeper(new, [], _prev(prev))
    assert tel["retired"] == [{"id": "UF-002", "name": "Gone journey"}]
    # no zombie: the UF list still has exactly one entry
    assert len(new) == 1
    assert {u.id for u in new} == {"UF-001"}


def test_pin_collision_renumbers_unmatched_deterministically():
    """New UF-001 matches prev UF-002; the OTHER new UF already holds

    id UF-002 → it must be renumbered to a free id, and flows' FKs must
    follow both remaps."""
    prev = [_uf("UF-002", "Pinned journey", members=["p1", "p2"],
                resource="pin", intent="manage")]
    new = [
        _uf("UF-001", "Pinned journey new", members=["p1", "p2"],
            resource="pin", intent="manage"),
        _uf("UF-002", "Fresh journey", members=["f1"],
            resource="fresh", intent="browse"),
    ]
    flows = [_flow("fa", "UF-001"), _flow("fb", "UF-002")]
    tel = apply_identity_keeper(new, flows, _prev(prev))
    assert new[0].id == "UF-002"          # pinned
    assert new[1].id == "UF-003"          # renumbered off the collision
    assert new[1].identity is None        # unmatched carries no identity
    assert flows[0].user_flow_id == "UF-002"
    assert flows[1].user_flow_id == "UF-003"
    assert tel["fk_remapped"] == 2
    ids = [u.id for u in new]
    assert len(ids) == len(set(ids))


def test_empty_prev_user_flows_is_a_clean_noop():
    new = [_uf("UF-001", "Solo journey", members=["s1"])]
    tel = apply_identity_keeper(new, [], {"scan_meta": {"run_id": "r0"},
                                          "user_flows": []})
    assert tel["pinned"] == 0 and tel["pin_rate"] == 0.0
    assert tel["retired"] == []
    assert new[0].id == "UF-001" and new[0].identity is None


# ── load_prev_scan (explicit input, loud failure) ──────────────────────


def test_load_prev_scan_roundtrip(tmp_path):
    doc = {"user_flows": [], "scan_meta": {"run_id": "x"}}
    p = tmp_path / "prev.json"
    p.write_text(json.dumps(doc))
    assert load_prev_scan(p) == doc


@pytest.mark.parametrize("payload", ["not json {", '["a", "list"]'])
def test_load_prev_scan_rejects_bad_documents(tmp_path, payload):
    p = tmp_path / "prev.json"
    p.write_text(payload)
    with pytest.raises(ValueError):
        load_prev_scan(p)


def test_load_prev_scan_missing_file(tmp_path):
    with pytest.raises(ValueError):
        load_prev_scan(tmp_path / "absent.json")


# ── Structural constant sanity ─────────────────────────────────────────


def test_threshold_is_majority_overlap():
    assert OVERLAP_THRESHOLD == 0.5
