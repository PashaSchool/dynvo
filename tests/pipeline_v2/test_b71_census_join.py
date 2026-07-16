"""B71 PIN — canonical UF->flow join resolver + pinned helper-grain metric.

These units hold the MECHANISM against the census facts verified on the armed
keyed boards (docs/anchor-arc/naming-grain-census-20260716.md):

* the join key drifts — Soc0 resolves ONLY through ``Flow.user_flow_id``, while
  novu/plane/hoppscotch resolve through ``uuid`` in ``member_flow_ids``;
* the "loc=0" population is ``Flow.loc`` (owned span, B11): 416 on plane, ~780
  corpus-wide — NOT the raw ``line_ranges`` span, which is never empty on the
  armed boards.

Fixtures are synthetic (authority = engine signal, not offline sims); they pin
the definition so the before/after census gates are comparable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Flow, FlowLineRange, UserFlow
from faultline.pipeline_v2.census_join import (
    HELPER_GRAIN_MAX_LOC,
    flow_join_keys,
    flow_owned_loc,
    flow_span_empty,
    flow_span_loc,
    index_flows_by_uf,
    is_helper_grain,
    resolve_uf_members,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _flow(
    name: str,
    *,
    uuid: str = "",
    user_flow_id: str | None = None,
    ranges: list[tuple[str, int, int]] | None = None,
    loc: int | None = None,
) -> Flow:
    fl = Flow(
        name=name,
        uuid=uuid,
        user_flow_id=user_flow_id,
        paths=[],
        authors=[],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=_NOW,
        health_score=90.0,
        loc=loc,
    )
    if ranges:
        fl.line_ranges = [
            FlowLineRange(path=p, start_line=s, end_line=e) for p, s, e in ranges
        ]
    return fl


def _uf(uf_id: str, member_flow_ids: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=uf_id, intent="manage", resource="x",
        member_flow_ids=member_flow_ids,
    )


# ── Join key: the three legal membership paths ──────────────────────────────


def test_join_soc0_reverse_pointer_only() -> None:
    """Soc0 EXHIBIT: a member reachable ONLY through the reverse
    ``Flow.user_flow_id`` pointer (member_flow_ids does NOT list its uuid/name).
    The member_flow_ids-only reader (6.7b ``_member_flows_for``) drops it; the
    canonical resolver keeps it."""
    f = _flow("investigate-edr-core-flow", uuid="soc0uuidhex01", user_flow_id="UF-007")
    uf = _uf("UF-007", member_flow_ids=["some-other-token-that-does-not-match"])
    members = resolve_uf_members(uf, [f])
    assert members == [f]


def test_join_uuid_membership_novu_plane_hopp() -> None:
    """novu/plane/hoppscotch EXHIBIT: members resolve through ``uuid`` listed in
    member_flow_ids (the dominant path on those boards)."""
    f = _flow("browse-slack-flow", uuid="31b9e33ac6d7254c6355f297eb3ae94e")
    uf = _uf("UF-001", member_flow_ids=["31b9e33ac6d7254c6355f297eb3ae94e"])
    assert resolve_uf_members(uf, [f]) == [f]


def test_join_name_membership_fallback() -> None:
    """Name-membership fallback: the 6.7b ``f.uuid or f.name`` keying means a
    flow with NO uuid is referenced by name."""
    f = _flow("create-inbox-flow", uuid="")
    uf = _uf("UF-002", member_flow_ids=["create-inbox-flow"])
    assert resolve_uf_members(uf, [f]) == [f]


def test_join_union_dedup_and_order() -> None:
    """Union across all three paths, deduplicated, stable ``flows`` order — a
    flow matched by both pointer AND uuid appears once, at its list position."""
    f1 = _flow("a-flow", uuid="uu-a", user_flow_id="UF-010")  # pointer + uuid
    f2 = _flow("b-flow", uuid="uu-b")                          # uuid only
    f3 = _flow("c-flow", uuid="uu-c", user_flow_id="UF-999")   # foreign
    uf = _uf("UF-010", member_flow_ids=["uu-a", "uu-b"])
    members = resolve_uf_members(uf, [f1, f2, f3])
    assert members == [f1, f2]  # f3 excluded, no double f1


def test_join_index_by_uf_id() -> None:
    """``index_flows_by_uf`` returns {uf.id: members} in one pass; membership
    matches per-UF resolver."""
    f1 = _flow("x-flow", uuid="uu-x", user_flow_id="UF-1")
    f2 = _flow("y-flow", uuid="uu-y", user_flow_id="UF-2")
    ufs = [_uf("UF-1", ["uu-x"]), _uf("UF-2", ["uu-y"])]
    idx = index_flows_by_uf(ufs, [f1, f2])
    assert idx == {"UF-1": [f1], "UF-2": [f2]}


def test_join_keys_priority_uuid_then_name() -> None:
    """``flow_join_keys`` yields uuid then name; empty strings dropped."""
    assert flow_join_keys(_flow("n", uuid="uu")) == ("uu", "n")
    assert flow_join_keys(_flow("n", uuid="")) == ("n",)


def test_join_reads_dict_board_row() -> None:
    """Duck typing — the resolver reads a serialized JSON board row (dict)
    identically to a model (the census walks dicts)."""
    f = {"name": "d-flow", "uuid": "uu-d", "user_flow_id": None}
    uf = {"id": "UF-5", "member_flow_ids": ["uu-d"]}
    assert resolve_uf_members(uf, [f]) == [f]


# ── Helper-grain metric: the pinned single definition ───────────────────────


def test_owned_loc_is_the_census_loc_field() -> None:
    """The census "loc=0" population is ``Flow.loc`` (owned span, B11) — 416 on
    plane. ``flow_owned_loc`` reads exactly that field."""
    assert flow_owned_loc(_flow("f", loc=0)) == 0
    assert flow_owned_loc(_flow("f", loc=42)) == 42
    assert flow_owned_loc(_flow("f", loc=None)) is None


def test_span_loc_prefers_owned_then_line_ranges() -> None:
    """``flow_span_loc`` returns owned-loc when present (matching the census
    figure), else the raw merged ``line_ranges`` span (pre-B11 fallback)."""
    # owned present -> owned wins even though line_ranges span is larger
    both = _flow("f", loc=0, ranges=[("a.py", 1, 100)])
    assert flow_span_loc(both) == 0
    # owned absent -> raw merged span
    raw = _flow("f", loc=None, ranges=[("a.py", 10, 19), ("a.py", 20, 24)])
    assert flow_span_loc(raw) == 15  # 10..24 merged (adjacent) inclusive


def test_span_empty_is_strict_reverse_lookup_contract() -> None:
    """T1 population = STRICT empty span-set (no coordinate at all). A plane DRF
    url stub with a real ``line_ranges`` coordinate is NOT empty-span even though
    its owned-loc is 0 (that is a containment/T3 concern, not T1)."""
    drf_stub = _flow("manage-schema-flow", loc=0, ranges=[("urls.py", 1, 1)])
    assert flow_span_empty(drf_stub) is False   # has a coordinate
    assert flow_span_loc(drf_stub) == 0          # but owns nothing
    truly_empty = _flow("barrel-reexport-flow", loc=0, ranges=None)
    assert flow_span_empty(truly_empty) is True


def test_helper_grain_census_predicate() -> None:
    """The pinned helper-grain census predicate: empty span OR loc<=30. Each
    non-empty fixture carries a real coordinate so the loc-ceiling path is
    isolated from the empty-span path."""
    big = [("a.py", 1, 200)]  # non-empty span, so only the ceiling decides
    assert is_helper_grain(_flow("f", loc=0, ranges=[("a.py", 1, 1)])) is True
    assert is_helper_grain(_flow("f", loc=HELPER_GRAIN_MAX_LOC, ranges=big)) is True
    assert is_helper_grain(_flow("f", loc=HELPER_GRAIN_MAX_LOC + 1, ranges=big)) is False
    assert is_helper_grain(_flow("f", loc=None, ranges=None)) is True  # empty span


def test_documenso_domain_core_anticase_not_empty_span() -> None:
    """ANTI-CASE (census §4): documenso ``sign-data-flow`` / ``verify-signature
    -flow`` (loc 9, domain core). They own 9 lines: NOT empty-span (T1-safe),
    and loc>0 so the strict T1 population never includes them. The census
    ceiling flags them as small (<=30) but that is a diagnostic, never a kill."""
    sign = _flow("sign-data-flow", loc=9, ranges=[("signing.ts", 40, 48)])
    verify = _flow("verify-signature-flow", loc=9, ranges=[("verify.ts", 12, 20)])
    for f in (sign, verify):
        assert flow_span_empty(f) is False
        assert flow_span_loc(f) == 9
        assert flow_span_loc(f) > 0  # survives any loc==0 / empty-span T1 gate
