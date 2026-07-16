"""Census join + grain metric — the PINNED before/after measurement base
(B71, 2026-07-16).

Why this module exists
======================

The B71 naming/grain census (docs/anchor-arc/naming-grain-census-20260716.md)
found that the two measurements every B71 gate depends on are **not pinned**,
so a before/after comparison silently measures different things per board:

1. **The UF→flow join key drifts.** ``UserFlow.member_flow_ids`` carries flow
   ``uuid`` values on hoppscotch / novu / plane, but Soc0 resolves only through
   the reverse ``Flow.user_flow_id`` pointer (its member ids do not line up with
   the flow uuids). A resolver that reads member_flow_ids ONLY (the shape the
   6.7b refiner's ``_member_flows_for`` uses) drops every Soc0 member — so a
   census that walks members one way and a gate that walks the other way are
   incomparable. The canonical resolver here takes the UNION of every join path
   (forward pointer + uuid membership + name membership), deterministically.

2. **The helper-grain "loc" metric drifts.** The census "loc=0" population is
   the ``Flow.loc`` OWNED-span field (B11): 416/1479 on plane, ~780 corpus-wide.
   The raw ``line_ranges`` span, by contrast, is never empty on the armed boards
   (every flow carries >=1 coordinate). Measuring "helper-grain" off one field
   on one board and the other field on the next makes the census caveat's
   "definition drifts between boards" real. This module pins ONE definition used
   by BOTH the runtime laws (Seg D) and the census tooling.

Nothing here runs inside the scan pipeline — it is a pure, deterministic
measurement library imported by the census/eval tooling and by the Seg D grain
laws so both agree, byte-for-byte, on what a member is and what a span is. It is
therefore flag-free and output-neutral (it emits nothing into a scan).

Duck typing
===========

Every accessor works on BOTH a serialized JSON board (``dict``) and a live
pydantic model (``Flow`` / ``UserFlow``), because the census reads JSON while the
runtime laws hold model instances. Read through :func:`_get` only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "HELPER_GRAIN_MAX_LOC",
    "flow_join_keys",
    "resolve_uf_members",
    "index_flows_by_uf",
    "flow_span_loc",
    "flow_span_empty",
    "flow_owned_loc",
    "is_helper_grain",
]

#: The pinned helper-grain LOC ceiling (census metric only, NOT a runtime gate —
#: per the census T5 ruling, ``loc<=30`` stays a diagnostic, never a runtime
#: kill). Scale-invariant small constant bounding "a helper does one small
#: thing"; it is a census knob, not a per-repo tuning number.
HELPER_GRAIN_MAX_LOC = 30


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access — a JSON board row (dict) and a pydantic model
    read identically."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ── UF -> flow join (the pinned canonical resolver) ─────────────────────────


def flow_join_keys(flow: Any) -> tuple[str, ...]:
    """Every identifier a UF may legally reference this flow by, in priority
    order: its ``uuid`` (the id novu/plane/hopp members carry) then its ``name``
    (the name-membership fallback). Empty strings are dropped."""
    keys: list[str] = []
    uuid = _get(flow, "uuid", "") or ""
    name = _get(flow, "name", "") or ""
    if uuid:
        keys.append(str(uuid))
    if name and str(name) not in keys:
        keys.append(str(name))
    return tuple(keys)


def resolve_uf_members(uf: Any, flows: Iterable[Any]) -> list[Any]:
    """The CANONICAL member set of a UserFlow — the union of every join path,
    deduplicated, in a stable ``flows`` iteration order.

    A flow is a member of ``uf`` iff ANY of:

    * ``flow.user_flow_id == uf.id`` (the reverse pointer — the ONLY path that
      resolves Soc0), OR
    * ``flow.uuid`` is listed in ``uf.member_flow_ids`` (uuid membership —
      novu / plane / hoppscotch), OR
    * ``flow.name`` is listed in ``uf.member_flow_ids`` (name membership — the
      6.7b ``f.uuid or f.name`` keying fallback).

    Returns the flow objects (not ids) so callers can read spans/paths directly.
    Deterministic: a flow appears at most once, at its first ``flows`` position.
    """
    uf_id = _get(uf, "id")
    members_raw = _get(uf, "member_flow_ids", []) or []
    member_ids = {str(m) for m in members_raw}
    out: list[Any] = []
    seen: set[int] = set()
    for f in flows:
        fid = id(f)
        if fid in seen:
            continue
        f_uf = _get(f, "user_flow_id")
        matched = False
        if uf_id is not None and f_uf is not None and str(f_uf) == str(uf_id):
            matched = True
        else:
            for k in flow_join_keys(f):
                if k in member_ids:
                    matched = True
                    break
        if matched:
            out.append(f)
            seen.add(fid)
    return out


def index_flows_by_uf(
    user_flows: Iterable[Any], flows: Iterable[Any],
) -> dict[str, list[Any]]:
    """One pass: ``{uf.id: [member flow, ...]}`` using the canonical resolver.

    Materialises ``flows`` once so the union walk is O(UF x flows) without
    re-consuming a generator. Keyed by ``uf.id`` (stable within a scan)."""
    flow_list = list(flows)
    return {
        str(_get(uf, "id")): resolve_uf_members(uf, flow_list)
        for uf in user_flows
    }


# ── span / loc metric (the pinned helper-grain definition) ──────────────────


def _merged_span_total(spans_by_path: Mapping[str, list[tuple[int, int]]]) -> int:
    """Sum of per-file merged, non-overlapping span lengths (inclusive lines).
    Adjacent/overlapping ranges merge (``s <= cur_e + 1``) so a symbol split
    across two touching records is not double-counted."""
    total = 0
    for spans in spans_by_path.values():
        ordered = sorted(spans)
        cur_s, cur_e = ordered[0]
        for s, e in ordered[1:]:
            if s <= cur_e + 1:
                cur_e = max(cur_e, e)
            else:
                total += cur_e - cur_s + 1
                cur_s, cur_e = s, e
        total += cur_e - cur_s + 1
    return total


def _span_records(flow: Any) -> dict[str, list[tuple[int, int]]]:
    """Collect a flow's (path -> [(start,end)]) coordinate set from the FIRST
    populated source, in contract priority: ``line_ranges`` (the canonical
    reverse-lookup surface) then the ``loc_symbol_attributions`` / ``loc_nodes``
    parity views. All three share ``path`` / ``start_line`` / ``end_line``."""
    for src in ("line_ranges", "loc_symbol_attributions", "loc_nodes"):
        rows = _get(flow, src, []) or []
        by_path: dict[str, list[tuple[int, int]]] = {}
        for r in rows:
            p = _get(r, "path")
            s = _get(r, "start_line")
            e = _get(r, "end_line")
            if p is None or s is None or e is None:
                continue
            si, ei = int(s), int(e)
            if ei < si:
                continue
            by_path.setdefault(str(p), []).append((si, ei))
        if by_path:
            return by_path
    return {}


def flow_span_empty(flow: Any) -> bool:
    """True when a flow carries NO resolvable (path, start, end) coordinate at
    all — the strict reverse-lookup contract break ``(file,line)->flow`` cannot
    resolve. This is the unambiguous, board-independent T1 population (never
    conflated with owned-loc)."""
    return not _span_records(flow)


def flow_owned_loc(flow: Any) -> int | None:
    """The flow's OWNED-span LOC (``Flow.loc``, B11) when present — the field the
    census "loc=0"/"loc<=30" population is measured on. ``None`` when the board
    predates B11 or ran with ``FAULTLINE_FLOW_LOC=0`` (then the caller falls back
    to the raw span via :func:`flow_span_loc`)."""
    v = _get(flow, "loc")
    return None if v is None else int(v)


def flow_span_loc(flow: Any) -> int:
    """The pinned single LOC number for a flow: its OWNED-span LOC when the board
    carries it (matching the census "loc=N" figures — 416 loc=0 on plane), else
    the raw merged ``line_ranges`` span. Zero iff the owned span is zero OR (on a
    pre-B11 board) the coordinate set is empty."""
    owned = flow_owned_loc(flow)
    if owned is not None:
        return owned
    return _merged_span_total(_span_records(flow)) if not flow_span_empty(flow) else 0


def is_helper_grain(flow: Any, max_loc: int = HELPER_GRAIN_MAX_LOC) -> bool:
    """The PINNED helper-grain census predicate — ONE definition for every
    board: an empty span-set OR ``loc <= max_loc``. Documented as a census
    metric only; the Seg D runtime laws take their own strict predicates
    (:func:`flow_span_empty` for T1, containment for T3) and never this
    ceiling."""
    return flow_span_empty(flow) or flow_span_loc(flow) <= max_loc
