"""B75 — UF-giant cases-split units + NAMED anti-cases (probe canon
2026-07-21, ``tests/data/uf_cases_split_census.json`` — distilled member
entry/flow/route slices of the REAL exhibit boards: the twenty-on armed
board (209m/131m splits + 47m/44m/34m/32m/8m anti-cases), the tracecat
cold board (76m vendored-guard exhibit + its real manifest dep slice),
the novu cold board (80m backend) and the onyx cold board (63m cohesive
leaf).

Threshold DERIVATIONS are re-asserted here (rule-no-magic-tuning): K
reuses the lattice action-axis floor, N sits one above the census P75
of mintable dir-cluster leaves, the giant floor is the census band edge
(== 2*K*N), and the witness floor IS the validator's I15 ruler.
"""

from __future__ import annotations

import json
import statistics
import types
from pathlib import Path

import pytest

from faultline.models.types import UserFlow
from faultline.pipeline_v2.journey_lattice import (
    _CATCHALL_MIN_CLUSTERS,
    _I15_ATTACH_FLOOR,
    _MIN_MINTABLE,
    load_action_families,
)
from faultline.pipeline_v2.naming_contract import (
    _uf_flow_maps,
    display_law_violations,
    load_naming_vocab,
)
from faultline.pipeline_v2.uf_cases_split import (
    CASE_MEMBER_FLOOR,
    CASES_SPLIT_ENV,
    GIANT_MEMBER_FLOOR,
    SURFACE_SHARE_FLOOR,
    WITNESS_SHARE_FLOOR,
    _dep_family_tokens,
    _descend_cases,
    apply_uf_cases_split,
    cases_split_enabled,
    min_case_children,
    run_uf_cases_split,
)

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "data"
    / "uf_cases_split_census.json"
)


@pytest.fixture(scope="module")
def census() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vocab() -> dict:
    return load_naming_vocab()


def _build(ex: dict):
    """(parent UF, flows, flow_by_id, patterns_by_file) from one distilled
    exhibit — synthetic fixture objects holding the REAL boards' member
    entry files, flow names and route patterns (gate-1 law: fixtures hold
    the MECHANISM; the numbers are the probe's)."""
    flows = [
        types.SimpleNamespace(
            uuid=m["id"],
            name=m["flow_name"],
            entry_point_file=m["entry"],
            user_flow_id=ex["id"],
        )
        for m in ex["members"]
    ]
    uf = UserFlow(
        id=ex["id"],
        name=ex["name"],
        intent="manage",
        resource=ex["resource"],
        domain=ex["domain"] or None,
        member_flow_ids=[m["id"] for m in ex["members"]],
        member_count=ex["member_count"],
        routes=sorted({
            p for ps in ex["patterns_by_file"].values() for p in ps}),
    )
    _, _, flow_by_id = _uf_flow_maps(flows)
    return uf, flows, flow_by_id, ex["patterns_by_file"]


def _children_of(user_flows: list, parent_id: str) -> list:
    return [u for u in user_flows if str(u.id) != parent_id]


# ── threshold derivations (no magic numbers) ──────────────────────────


def test_derivation_k_reuses_lattice_floor() -> None:
    """K is NOT a new number: the exact lattice action-axis expression,
    agreeing with the object axis' cluster floor."""
    action_vocab = load_action_families()
    expected = max(
        int(action_vocab.get("min_action_families", 3) or 3), _MIN_MINTABLE)
    assert min_case_children() == expected == 3 == _CATCHALL_MIN_CLUSTERS


def test_derivation_case_floor_above_census_upper_quartile(census) -> None:
    """N == 1 + P75 of the mintable (>= _MIN_MINTABLE) dir-cluster leaf
    sizes across the census giant class — recomputed from the canon
    fixture, not asserted from air."""
    sizes: list[int] = []
    for key, ex in sorted(census.items()):
        if not isinstance(ex, dict) or not key.startswith("twenty_"):
            continue
        if ex["member_count"] < GIANT_MEMBER_FLOOR:
            continue
        entries = [(m["id"], m["entry"]) for m in ex["members"]]
        sizes.extend(
            len(mids)
            for _, mids in _descend_cases(entries, floor=_MIN_MINTABLE)
        )
    p75 = statistics.quantiles(sizes, n=4)[2]
    assert p75 == 4.0
    assert CASE_MEMBER_FLOOR == int(p75) + 1 == 5


def test_derivation_giant_floor_census_band_edge() -> None:
    """The giant floor is the census band edge (un-flagged large journeys
    top out at 29; the flagged class begins at 30) and arithmetically the
    smallest parent where K minimal cases coexist with a residual
    majority: 2*K*N."""
    assert GIANT_MEMBER_FLOOR == 30
    assert GIANT_MEMBER_FLOOR == 2 * min_case_children() * CASE_MEMBER_FLOOR


def test_derivation_witness_floor_is_i15_ruler() -> None:
    """The route-witness share floor IS the validator's attach ruler —
    reused, not tuned; the surface floor is the half band."""
    assert WITNESS_SHARE_FLOOR == _I15_ATTACH_FLOOR
    assert SURFACE_SHARE_FLOOR == 0.5


# ── kill-switch discipline ────────────────────────────────────────────


def test_flag_off_returns_none_untouched(census, monkeypatch) -> None:
    monkeypatch.delenv(CASES_SPLIT_ENV, raising=False)
    assert not cases_split_enabled()
    uf, flows, _, _ = _build(census["twenty_209_browse_object_record"])
    before = [dict(u.model_dump()) if hasattr(u, "model_dump") else u.dict()
              for u in [uf]]
    ufs = [uf]
    assert run_uf_cases_split(ufs, flows) is None
    after = [dict(u.model_dump()) if hasattr(u, "model_dump") else u.dict()
             for u in ufs]
    assert before == after

    monkeypatch.setenv(CASES_SPLIT_ENV, "0")
    assert not cases_split_enabled()
    monkeypatch.setenv(CASES_SPLIT_ENV, "1")
    assert cases_split_enabled()


# ── probe-canon splits ────────────────────────────────────────────────


def test_giant_209_splits_to_probe_children(census, vocab) -> None:
    """twenty 209m 'Browse object record' -> 10 children / 73 extracted /
    residual 136, wearing the probe's names."""
    ex = census["twenty_209_browse_object_record"]
    uf, _, flow_by_id, pats = _build(ex)
    ufs = [uf]
    tele = apply_uf_cases_split(
        ufs, flow_by_id, vocab, patterns_by_file=pats)
    kids = _children_of(ufs, ex["id"])
    assert tele["giants_split"] == 1
    assert len(kids) == 10
    assert tele["members_extracted"] == 73
    assert uf.member_count == 136          # honest iteration-2 debt
    by_key = {str(k.domain): k for k in kids}
    # The probe's five NAMED children, exact sizes.
    assert by_key["cases:filter-dropdown"].member_count == 12
    assert by_key["cases:field-list"].member_count == 9
    assert by_key["cases:calendar"].member_count == 8
    assert by_key["cases:board"].member_count == 7
    assert by_key["cases:record-pages"].member_count == 6
    # 'record pages' is the route-witnessed child.
    pages = by_key["cases:record-pages"]
    assert pages.name_confidence == "medium"
    assert pages.routes
    # Parent survives as the residual — id, name, lineage kept.
    assert uf.id == ex["id"]
    assert uf.name == ex["name"]


def test_giant_131_settings_direct_names(census, vocab) -> None:
    """twenty 131m settings -> 11 children / residual 72 with DIRECT
    settings-area names (security / billing / members / profile)."""
    ex = census["twenty_131_settings"]
    uf, _, flow_by_id, pats = _build(ex)
    ufs = [uf]
    tele = apply_uf_cases_split(
        ufs, flow_by_id, vocab, patterns_by_file=pats)
    kids = _children_of(ufs, ex["id"])
    assert tele["giants_split"] == 1
    assert len(kids) == 11
    assert uf.member_count == 72           # honest iteration-2 debt
    keys = {str(k.domain) for k in kids}
    assert {"cases:security", "cases:billing", "cases:members",
            "cases:profile"} <= keys
    for k in kids:
        toks = str(k.name).lower().split()
        area = str(k.domain).split(":", 1)[1].split("-")[0]
        assert area in " ".join(toks)      # direct area name, no jargon


# ── NAMED anti-cases (spec survivors) ─────────────────────────────────


def _assert_untouched(census, vocab, key: str) -> dict:
    ex = census[key]
    uf, _, flow_by_id, pats = _build(ex)
    before = list(uf.member_flow_ids)
    ufs = [uf]
    tele = apply_uf_cases_split(
        ufs, flow_by_id, vocab, patterns_by_file=pats)
    assert len(ufs) == 1
    assert uf.member_flow_ids == before
    assert uf.member_count == ex["member_count"]
    assert uf.name == ex["name"]
    return tele


def test_anticase_34m_activities_single_leaf(census, vocab) -> None:
    """twenty 34m activities — the WHOLE subtree is one cohesive leaf
    (healthy Hnorm 0.97 == sick 0.94 refuted entropy; the count-of-
    children boundary keeps it)."""
    ex = census["twenty_34_activities"]
    entries = [(m["id"], m["entry"]) for m in ex["members"]]
    cands = _descend_cases(entries)
    assert len(cands) == 1                 # ONE leaf — no cases to make
    tele = _assert_untouched(census, vocab, "twenty_34_activities")
    assert tele["below_k_kept"] == 1


def test_anticase_44m_workflows_single_qualifier(census, vocab) -> None:
    """twenty 44m workflows — exactly one qualified case (the executor
    split the probe correctly refused: qual=1 < K)."""
    tele = _assert_untouched(census, vocab, "twenty_44_workflows")
    assert tele["giants_seen"] == 1
    assert tele["below_k_kept"] == 1


def test_anticase_47m_page_layout_kept(census, vocab) -> None:
    tele = _assert_untouched(census, vocab, "twenty_47_page_layout")
    assert tele["below_k_kept"] == 1


def test_anticase_32m_navigation_kept(census, vocab) -> None:
    tele = _assert_untouched(census, vocab, "twenty_32_navigation")
    assert tele["below_k_kept"] == 1


def test_anticase_8m_connect_server_below_giant_floor(
        census, vocab) -> None:
    """twenty 8m 'Connect twenty server' — below the census band edge:
    the mechanism never even examines it."""
    tele = _assert_untouched(census, vocab, "twenty_8_connect_server")
    assert tele["giants_seen"] == 0


def test_anticase_onyx_63m_cohesive_leaf(census, vocab) -> None:
    """onyx 63m 'Manage onyx' — one cohesive backend leaf; failure mode
    is UNDER-split, never over-split."""
    tele = _assert_untouched(census, vocab, "onyx_63_cohesive")
    assert tele["giants_seen"] == 1
    assert tele["below_k_kept"] == 1


def test_anticase_novu_80m_backend(census, vocab) -> None:
    """novu 80m 'Manage agent integrations' — backend blob: zero JSX
    surface, witness share under the I15 ruler ⇒ zero qualified cases."""
    tele = _assert_untouched(census, vocab, "novu_80_backend")
    assert tele["giants_seen"] == 1
    assert tele["below_k_kept"] == 1
    assert tele["vendored_rejected"] == 0  # kept by shares, not the guard


# ── vendored guard (tracecat exhibit) ─────────────────────────────────


def _tracecat_dep_tokens(census) -> frozenset:
    toks: set[str] = set()
    for dep in census["tracecat_dep_names"]:
        toks |= _dep_family_tokens(dep)
    return frozenset(toks)


def test_tracecat_vendored_guard_zero_technical_children(
        census, vocab) -> None:
    """tracecat 76m — the guard (S3 no-product-surface ∧ S1 dep-family
    echo over the REAL manifest slice) rejects every tiptap-*/ai-*
    candidate BEFORE extraction; the survivors fall under K ⇒ the giant
    ships untouched with ZERO technical children."""
    ex = census["tracecat_76_vendored"]
    uf, _, flow_by_id, pats = _build(ex)
    ufs = [uf]
    tele = apply_uf_cases_split(
        ufs, flow_by_id, vocab,
        patterns_by_file=pats,
        dep_tokens=_tracecat_dep_tokens(census),
    )
    assert tele["giants_split"] == 0
    assert _children_of(ufs, ex["id"]) == []
    assert tele["vendored_rejected"] >= 3   # tiptap-icons / tiptap-ui* / ai-elements
    assert uf.member_count == ex["member_count"]


def test_tracecat_guard_is_load_bearing(census, vocab) -> None:
    """Counterfactual: WITHOUT the guard the same board mints vendored-UI
    children with technical names — the exact disease the tune fences."""
    ex = census["tracecat_76_vendored"]
    uf, _, flow_by_id, pats = _build(ex)
    ufs = [uf]
    tele = apply_uf_cases_split(
        ufs, flow_by_id, vocab,
        patterns_by_file=pats,
        dep_tokens=frozenset(),
    )
    assert tele["giants_split"] == 1
    names = " ".join(k.name.lower() for k in _children_of(ufs, ex["id"]))
    assert "tiptap" in names or "ai" in names


def test_dep_family_tokens_shape() -> None:
    assert _dep_family_tokens("@tiptap/react") == {"tiptap"}
    assert _dep_family_tokens("ai") == {"ai"}
    assert _dep_family_tokens("@types/bun") == {"bun"}
    assert "trigger" in _dep_family_tokens("trigger.dev")


# ── conservation + I14 + display laws ─────────────────────────────────


def test_conservation_union_children_plus_residual_equals_parent(
        census, vocab) -> None:
    """union(children) + residual == members(parent), children pairwise
    disjoint, member_count synced — by NAME, per member id."""
    ex = census["twenty_209_browse_object_record"]
    uf, _, flow_by_id, pats = _build(ex)
    original = list(uf.member_flow_ids)
    ufs = [uf]
    apply_uf_cases_split(ufs, flow_by_id, vocab, patterns_by_file=pats)
    kids = _children_of(ufs, ex["id"])
    claimed: list[str] = []
    for k in kids:
        assert k.member_count == len(k.member_flow_ids)
        claimed.extend(k.member_flow_ids)
    assert len(claimed) == len(set(claimed))          # pairwise disjoint
    assert sorted(claimed + list(uf.member_flow_ids)) == sorted(original)
    assert uf.member_count == len(uf.member_flow_ids)


def test_i14_backpointers_repoint_to_children(census, vocab) -> None:
    """Every extracted member's flow backpointer lands on its child;
    residual members keep the parent — never dangle."""
    ex = census["twenty_209_browse_object_record"]
    uf, flows, flow_by_id, pats = _build(ex)
    ufs = [uf]
    apply_uf_cases_split(ufs, flow_by_id, vocab, patterns_by_file=pats)
    kids = _children_of(ufs, ex["id"])
    owner = {m: str(k.id) for k in kids for m in k.member_flow_ids}
    live_ids = {str(u.id) for u in ufs}
    for fl in flows:
        expected = owner.get(fl.uuid, ex["id"])
        assert fl.user_flow_id == expected
        assert fl.user_flow_id in live_ids


def test_child_names_law_clean_no_dup_never_high(census, vocab) -> None:
    """R5-5 caps ride the display law at mint: every child name is
    law-clean, unique within the capability, and never born 'high'."""
    for key in ("twenty_209_browse_object_record", "twenty_131_settings"):
        ex = census[key]
        uf, _, flow_by_id, pats = _build(ex)
        ufs = [uf]
        apply_uf_cases_split(
            ufs, flow_by_id, vocab, patterns_by_file=pats)
        names = [str(u.name).strip().lower() for u in ufs]
        assert len(names) == len(set(names))
        for k in _children_of(ufs, ex["id"]):
            assert display_law_violations(str(k.name), vocab) == []
            assert k.name_confidence in {"medium", "low"}
            assert str(k.id).startswith("UF-CS-")
