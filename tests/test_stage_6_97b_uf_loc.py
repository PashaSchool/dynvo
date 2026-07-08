"""Stage 6.97b — deterministic User-Flow-level LOC (operator bug B3).

Covers the span-union math (overlap / adjacency / disjoint / reversed),
the owned-span selection (interior + shared_paths + null-line exclusion,
mirroring the validator), the per-file UNION aggregation across member
flows (shared-file dedup), mc=0 → honest 0, uuid-vs-name keying,
determinism, the kill-switch, and the additive serializer contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultline.models.types import UserFlow
from faultline.pipeline_v2.stage_6_97b_uf_loc import (
    STAGE_6_97B_ENV_FLAG,
    apply_uf_loc,
    flow_owned_spans,
    uf_loc_enabled,
    union_span_len,
)


# ── helpers ─────────────────────────────────────────────────────────────

def _node(file, lines, role="entry"):
    return SimpleNamespace(file=file, lines=lines, role=role)


def _flow(uuid, nodes, shared_paths=None, name=None):
    return SimpleNamespace(
        uuid=uuid,
        name=name,
        nodes=list(nodes),
        shared_paths=list(shared_paths or []),
    )


def _shared(path):
    return SimpleNamespace(path=path)


def _uf(uid, member_ids):
    return UserFlow(
        id=uid, name=uid, intent="manage", resource="thing",
        member_flow_ids=list(member_ids), member_count=len(member_ids),
    )


# ── union_span_len ──────────────────────────────────────────────────────

def test_union_single_span_inclusive():
    # 10..20 inclusive = 11 lines
    assert union_span_len([(10, 20)]) == 11


def test_union_disjoint():
    assert union_span_len([(1, 5), (10, 12)]) == 5 + 3


def test_union_overlapping_merges():
    # 10..20 and 15..30 → 10..30 = 21 (not 11 + 16 = 27)
    assert union_span_len([(10, 20), (15, 30)]) == 21


def test_union_adjacent_merges():
    # 10..12 and 13..15 are line-adjacent → contiguous 10..15 = 6
    assert union_span_len([(10, 12), (13, 15)]) == 6


def test_union_gap_of_two_does_not_merge():
    # 10..12 and 15..16 → gap (13,14) → 3 + 2 = 5
    assert union_span_len([(10, 12), (15, 16)]) == 5


def test_union_empty_is_zero():
    assert union_span_len([]) == 0


def test_union_order_independent_deterministic():
    a = union_span_len([(30, 40), (10, 20), (15, 22)])
    b = union_span_len([(10, 20), (15, 22), (30, 40)])
    assert a == b == (union_span_len([(10, 22)]) + union_span_len([(30, 40)]))


def test_union_single_line_span():
    assert union_span_len([(7, 7)]) == 1


# ── flow_owned_spans (owned selection mirrors validator) ────────────────

def test_owned_keeps_valid_entry_span():
    fl = _flow("f1", [_node("a.ts", [10, 20], role="entry")])
    assert flow_owned_spans(fl) == [("a.ts", 10, 20)]


def test_owned_excludes_interior_role():
    fl = _flow("f1", [
        _node("a.ts", [10, 20], role="entry"),
        _node("a.ts", [100, 200], role="interior"),  # W4 page-interior → excluded
    ])
    assert flow_owned_spans(fl) == [("a.ts", 10, 20)]


def test_owned_excludes_shared_paths_ledger_file():
    fl = _flow(
        "f1",
        [_node("owned.ts", [1, 5]), _node("shared.ts", [1, 99])],
        shared_paths=[_shared("shared.ts")],
    )
    assert flow_owned_spans(fl) == [("owned.ts", 1, 5)]


def test_owned_null_line_node_contributes_nothing():
    # role="sink" nodes commonly carry lines=None → 0 honestly
    fl = _flow("f1", [
        _node("a.ts", [1, 3], role="entry"),
        _node("b.ts", None, role="sink"),
    ])
    assert flow_owned_spans(fl) == [("a.ts", 1, 3)]


def test_owned_rejects_malformed_and_bool_lines():
    fl = _flow("f1", [
        _node("a.ts", [1], role="entry"),        # len != 2
        _node("b.ts", [True, False], role="entry"),  # bool is not a real span
        _node("c.ts", "12-20", role="entry"),    # not a list
        _node("d.ts", [5, 9], role="entry"),     # valid
    ])
    assert flow_owned_spans(fl) == [("d.ts", 5, 9)]


def test_owned_reversed_lines_normalised():
    fl = _flow("f1", [_node("a.ts", [20, 10], role="entry")])
    assert flow_owned_spans(fl) == [("a.ts", 10, 20)]


def test_owned_skips_node_without_file():
    fl = _flow("f1", [_node(None, [1, 5]), _node("", [1, 5])])
    assert flow_owned_spans(fl) == []


def test_owned_works_on_dict_shape():
    # rehydrated / replay artifact shape (dicts, not pydantic objects)
    fl = {
        "uuid": "f1",
        "nodes": [
            {"file": "a.ts", "lines": [10, 20], "role": "entry"},
            {"file": "x.ts", "lines": [1, 9], "role": "interior"},
        ],
        "shared_paths": [{"path": "s.ts"}],
    }
    assert flow_owned_spans(fl) == [("a.ts", 10, 20)]


# ── apply_uf_loc (per-file UNION across member flows) ───────────────────

def test_uf_loc_unions_shared_file_across_member_flows():
    # Two member flows both touch shared.ts at overlapping lines; the
    # journey LOC must UNION them, not double-count.
    f1 = _flow("f1", [_node("shared.ts", [10, 20]), _node("a.ts", [1, 5])])
    f2 = _flow("f2", [_node("shared.ts", [15, 30]), _node("b.ts", [1, 4])])
    uf = _uf("UF-1", ["f1", "f2"])
    tele = apply_uf_loc([uf], [f1, f2])
    # shared.ts union 10..30 = 21; a.ts 5; b.ts 4  → 30
    assert uf.loc == 21 + 5 + 4
    # naive per-flow sum would be (11 + 5) + (16 + 4) = 36 (double-counts)
    assert uf.loc < 36
    assert tele == {
        "enabled": True,
        "user_flows_total": 1,
        "user_flows_with_loc": 1,
        "user_flows_zero_loc": 0,
    }


def test_uf_loc_mc0_placeholder_is_honest_zero():
    uf = _uf("UF-empty", [])  # mc=0 system-recall placeholder
    tele = apply_uf_loc([uf], [_flow("f1", [_node("a.ts", [1, 9])])])
    assert uf.loc == 0
    assert uf.loc is not None
    assert tele["user_flows_zero_loc"] == 1
    assert tele["user_flows_with_loc"] == 0


def test_uf_loc_all_null_member_spans_is_zero():
    f1 = _flow("f1", [_node("a.ts", None, role="sink")])
    uf = _uf("UF-1", ["f1"])
    apply_uf_loc([uf], [f1])
    assert uf.loc == 0


def test_uf_loc_resolves_member_by_name_not_only_uuid():
    # member_flow_ids may carry a flow NAME instead of a uuid
    f1 = _flow("uuid-1", [_node("a.ts", [1, 10])], name="the-flow")
    uf = _uf("UF-1", ["the-flow"])
    apply_uf_loc([uf], [f1])
    assert uf.loc == 10


def test_uf_loc_ignores_dangling_member_ref():
    f1 = _flow("f1", [_node("a.ts", [1, 10])])
    uf = _uf("UF-1", ["f1", "does-not-exist"])
    apply_uf_loc([uf], [f1])
    assert uf.loc == 10


def test_uf_loc_deterministic_double_run():
    flows = [
        _flow("f1", [_node("s.ts", [10, 20]), _node("a.ts", [1, 5])]),
        _flow("f2", [_node("s.ts", [15, 30]), _node("b.ts", [1, 4])]),
        _flow("f3", [_node("c.ts", [1, 100]), _node("s.ts", [40, 50])]),
    ]
    uf_a = _uf("UF-1", ["f1", "f2", "f3"])
    uf_b = _uf("UF-1", ["f1", "f2", "f3"])
    apply_uf_loc([uf_a], flows)
    apply_uf_loc([uf_b], list(reversed(flows)))  # input order must not matter
    assert uf_a.loc == uf_b.loc


# ── kill-switch ─────────────────────────────────────────────────────────

def test_uf_loc_enabled_default_on(monkeypatch):
    monkeypatch.delenv(STAGE_6_97B_ENV_FLAG, raising=False)
    assert uf_loc_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "False"])
def test_uf_loc_kill_switch_off(monkeypatch, val):
    monkeypatch.setenv(STAGE_6_97B_ENV_FLAG, val)
    assert uf_loc_enabled() is False


# ── additive serializer contract ────────────────────────────────────────

def test_uf_loc_none_omitted_from_dump_byte_identity():
    # Unstamped UF (kill-switch off / old JSON) must serialize WITHOUT a
    # ``loc`` key — byte-identical to the pre-B3 engine.
    uf = _uf("UF-1", [])
    assert uf.loc is None
    assert "loc" not in uf.model_dump()


def test_uf_loc_zero_and_positive_present_in_dump():
    uf = _uf("UF-1", [])
    uf.loc = 0
    assert uf.model_dump().get("loc") == 0
    assert "loc" in uf.model_dump()
    uf.loc = 137
    assert uf.model_dump().get("loc") == 137


def test_apply_uf_loc_makes_field_serialize():
    f1 = _flow("f1", [_node("a.ts", [1, 10])])
    uf = _uf("UF-1", ["f1"])
    apply_uf_loc([uf], [f1])
    dumped = uf.model_dump()
    assert dumped["loc"] == 10
