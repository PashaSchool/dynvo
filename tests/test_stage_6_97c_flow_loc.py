"""Stage 6.97c — deterministic Flow-level OWNED/SHARED LOC (operator bug B11).

Covers the interval primitives (merge / ≥2-coverage sweep / intersection),
the owned/shared PARTITION with its conservation invariant
(``loc + loc_shared == union(owned spans)``), the cross-flow sharing semantics
(shared iff ≥2 distinct flows cover a line; adjacency is NOT overlap), the
reactive-resume email-trio exhibit (13 owned / 100 shared), determinism,
the kill-switch, and the additive serializer contract (None omitted → byte-
identical to the pre-B11 engine).
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from faultline.models.types import Flow
from faultline.pipeline_v2.stage_6_97b_uf_loc import (
    flow_owned_spans,
    union_span_len,
)
from faultline.pipeline_v2.stage_6_97c_flow_loc import (
    STAGE_6_97C_ENV_FLAG,
    apply_flow_loc,
    flow_loc_enabled,
    intersect_intervals,
    merge_intervals,
    shared_mask_ge2,
)


# ── helpers ─────────────────────────────────────────────────────────────

def _node(file, lines, role="entry"):
    return SimpleNamespace(file=file, lines=lines, role=role)


def _fnode(file, lines, role="entry", kind="entry"):
    """A FlowNode-shaped dict (pydantic coerces it on a real ``Flow``)."""
    return {
        "id": f"{file}#{role}", "kind": kind, "file": file,
        "lines": lines, "role": role,
    }


def _flow(uuid, nodes, shared_paths=None, name=None):
    return SimpleNamespace(
        uuid=uuid, name=name, nodes=list(nodes),
        shared_paths=list(shared_paths or []),
        loc=None, loc_shared=None,
    )


def _real_flow(**kw):
    base = dict(
        name="x-flow", paths=[], authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=datetime.datetime(2020, 1, 1),
        health_score=100.0,
    )
    base.update(kw)
    return Flow(**base)


def _owned_union(fl):
    """Reference: the flow's whole owned footprint (per-file UNION)."""
    by: dict[str, list[tuple[int, int]]] = {}
    for p, s, e in flow_owned_spans(fl):
        by.setdefault(p, []).append((s, e))
    return sum(union_span_len(by[p]) for p in by)


# ── merge_intervals ──────────────────────────────────────────────────────

def test_merge_overlapping():
    assert merge_intervals([(1, 5), (3, 8)]) == [(1, 8)]


def test_merge_adjacent():
    assert merge_intervals([(10, 12), (13, 15)]) == [(10, 15)]


def test_merge_gap_kept():
    assert merge_intervals([(10, 12), (15, 16)]) == [(10, 12), (15, 16)]


def test_merge_unsorted_input():
    assert merge_intervals([(30, 40), (1, 5), (4, 8)]) == [(1, 8), (30, 40)]


def test_merge_empty():
    assert merge_intervals([]) == []


# ── shared_mask_ge2 (segments covered by >=2 distinct flows) ─────────────

def test_shared_all_three_cover_same_span():
    assert shared_mask_ge2([[(36, 135)], [(36, 135)], [(36, 135)]]) == [(36, 135)]


def test_shared_adjacent_distinct_flows_not_shared():
    # [1,5] and [6,10] are line-adjacent but never truly overlap → NOT shared
    assert shared_mask_ge2([[(1, 5)], [(6, 10)]]) == []


def test_shared_partial_overlap():
    assert shared_mask_ge2([[(1, 10)], [(5, 15)]]) == [(5, 10)]


def test_shared_single_flow_never_shared():
    assert shared_mask_ge2([[(1, 100)]]) == []


def test_shared_three_flows_only_common_core():
    # 1-20, 10-30, 15-40 → lines covered by >=2: 10-30 (10-20 by f1+f2,
    # 15-30 by f2+f3) merged → [10,30]
    assert shared_mask_ge2([[(1, 20)], [(10, 30)], [(15, 40)]]) == [(10, 30)]


def test_shared_empty():
    assert shared_mask_ge2([]) == []


# ── intersect_intervals ──────────────────────────────────────────────────

def test_intersect_basic():
    assert intersect_intervals([(36, 135), (141, 153)], [(36, 135)]) == [(36, 135)]


def test_intersect_partial():
    assert intersect_intervals([(1, 10)], [(5, 20)]) == [(5, 10)]


def test_intersect_disjoint_empty():
    assert intersect_intervals([(1, 5)], [(10, 20)]) == []


# ── apply_flow_loc: the exhibit + partition/conservation ────────────────

def _email_trio():
    layout = _node("auth.tsx", [36, 135], role="called")  # shared AuthEmailLayout
    return [
        _flow("verify-email", [_node("auth.tsx", [159, 171]), layout]),
        _flow("verify-email-change", [_node("auth.tsx", [179, 191]), layout]),
        _flow("reset-password", [_node("auth.tsx", [141, 153]), layout]),
    ]


def test_exhibit_trio_13_owned_100_shared():
    flows = _email_trio()
    tele = apply_flow_loc(flows)
    for fl in flows:
        assert fl.loc == 13, (fl.uuid, fl.loc)
        assert fl.loc_shared == 100, (fl.uuid, fl.loc_shared)
        # conservation: exclusive + shared == whole owned footprint (113)
        assert fl.loc + fl.loc_shared == _owned_union(fl) == 113
    assert tele == {
        "enabled": True, "flows_total": 3,
        "flows_with_shared": 3, "flows_exclusive_only": 0,
    }


def test_no_sharing_all_owned_exclusive():
    flows = [
        _flow("f1", [_node("a.ts", [1, 10])]),
        _flow("f2", [_node("b.ts", [1, 20])]),
    ]
    apply_flow_loc(flows)
    assert (flows[0].loc, flows[0].loc_shared) == (10, 0)
    assert (flows[1].loc, flows[1].loc_shared) == (20, 0)


def test_partial_overlap_splits_lines():
    # f1 owns a.ts[1,10]; f2 owns a.ts[5,10] → lines 5-10 shared by both
    f1 = _flow("f1", [_node("a.ts", [1, 10])])
    f2 = _flow("f2", [_node("a.ts", [5, 10])])
    apply_flow_loc([f1, f2])
    # f1: 1-4 exclusive (4), 5-10 shared (6)
    assert (f1.loc, f1.loc_shared) == (4, 6)
    # f2: all 5-10 shared (6), 0 exclusive
    assert (f2.loc, f2.loc_shared) == (0, 6)
    assert f1.loc + f1.loc_shared == _owned_union(f1)
    assert f2.loc + f2.loc_shared == _owned_union(f2)


def test_conservation_holds_for_mixed_config():
    flows = [
        _flow("f1", [_node("a.ts", [1, 50]), _node("shared.ts", [1, 100])]),
        _flow("f2", [_node("shared.ts", [50, 150]), _node("b.ts", [1, 30])]),
        _flow("f3", [_node("c.ts", [1, 5])]),
    ]
    apply_flow_loc(flows)
    for fl in flows:
        assert fl.loc >= 0 and fl.loc_shared >= 0
        assert fl.loc + fl.loc_shared == _owned_union(fl)


def test_interior_and_shared_paths_excluded_from_both():
    # interior + shared_paths nodes never enter the owned footprint at all
    f = _flow(
        "f1",
        [
            _node("a.ts", [1, 10], role="entry"),
            _node("a.ts", [200, 300], role="interior"),   # excluded
            _node("x.ts", [1, 99], role="called"),         # shared_paths → excluded
        ],
        shared_paths=[SimpleNamespace(path="x.ts")],
    )
    apply_flow_loc([f])
    assert f.loc == 10 and f.loc_shared == 0
    assert f.loc + f.loc_shared == _owned_union(f) == 10


def test_null_line_sink_contributes_zero():
    f = _flow("f1", [
        _node("a.ts", [1, 3], role="entry"),
        _node("b.ts", None, role="sink"),
    ])
    apply_flow_loc([f])
    assert f.loc == 3 and f.loc_shared == 0


def test_empty_and_flowless_are_honest_zero():
    f = _flow("f1", [])
    tele = apply_flow_loc([f])
    assert f.loc == 0 and f.loc_shared == 0
    assert tele["flows_total"] == 1 and tele["flows_with_shared"] == 0


def test_apply_flow_loc_empty_input():
    assert apply_flow_loc([]) == {
        "enabled": True, "flows_total": 0,
        "flows_with_shared": 0, "flows_exclusive_only": 0,
    }


def test_deterministic_input_order_independent():
    flows = _email_trio()
    apply_flow_loc(flows)
    got = {f.uuid: (f.loc, f.loc_shared) for f in flows}
    flows_rev = list(reversed(_email_trio()))
    apply_flow_loc(flows_rev)
    got_rev = {f.uuid: (f.loc, f.loc_shared) for f in flows_rev}
    assert got == got_rev


# ── kill-switch ─────────────────────────────────────────────────────────

def test_flow_loc_enabled_default_on(monkeypatch):
    monkeypatch.delenv(STAGE_6_97C_ENV_FLAG, raising=False)
    assert flow_loc_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "False"])
def test_flow_loc_kill_switch_off(monkeypatch, val):
    monkeypatch.setenv(STAGE_6_97C_ENV_FLAG, val)
    assert flow_loc_enabled() is False


# ── additive serializer contract ────────────────────────────────────────

def test_none_loc_omitted_byte_identity():
    fl = _real_flow()
    assert fl.loc is None and fl.loc_shared is None
    dumped = fl.model_dump()
    assert "loc" not in dumped and "loc_shared" not in dumped
    # a pre-existing None field must STAY (nothing else shifts)
    assert "health_trend" in dumped


def test_stamped_loc_present_including_zero():
    fl = _real_flow()
    fl.loc, fl.loc_shared = 0, 0
    d = fl.model_dump()
    assert d["loc"] == 0 and d["loc_shared"] == 0
    fl.loc, fl.loc_shared = 13, 100
    d = fl.model_dump()
    assert d["loc"] == 13 and d["loc_shared"] == 100


def test_apply_then_serialize_real_flow():
    layout = _fnode("auth.tsx", [36, 135], role="called", kind="function")
    f1 = _real_flow(name="verify-email-flow",
                    nodes=[_fnode("auth.tsx", [159, 171]), layout])
    f2 = _real_flow(name="reset-password-flow",
                    nodes=[_fnode("auth.tsx", [141, 153]), layout])
    apply_flow_loc([f1, f2])
    assert f1.model_dump()["loc"] == 13
    assert f1.model_dump()["loc_shared"] == 100
