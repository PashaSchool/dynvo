"""Stage 6.97b — deterministic User-Flow-level LOC ($0, output layer).

Operator bug B3 (2026-07-08): ``user_flows[].loc`` was ``None`` for
EVERY journey — even "Manage cases end-to-end" with 35 member flows —
because the engine never emitted a UF-level LOC. Flow-level span data
DID exist (``flows[].nodes[].lines`` + ``line_ranges`` + ``shared_paths``);
the dashboard just had nothing to render, so the LOC column was blank
for all journeys.

This stage stamps ``UserFlow.loc`` = the OWNED-span line count of the
journey, computed as the **UNION** of its member flows' owned spans.

OWNED-span selection (mirror of the validator)
----------------------------------------------
The app-repo validator ``eval/validate_scan.py:_spine_flow_loc_owned``
is the canonical owned-LOC ruler for a single flow: from ``flow.nodes``,
sum span lengths of nodes carrying a valid 2-int ``lines`` pair, EXCLUDING
nodes with ``role == "interior"`` (W4 page-interior attributions) and
nodes on files the flow's ``shared_paths[]`` ledger assigns to a different
owner. Null-line nodes (``lines is None`` — the ``role="sink"`` common
case) contribute 0 honestly. :func:`flow_owned_spans` mirrors that
selection exactly, at the in-memory object level.

UNION vs naive-sum (the journey-level choice)
---------------------------------------------
Two member flows of the SAME journey routinely share a file (a shared
list page, a common store, an auth guard). Summing each member flow's
owned LOC would DOUBLE-COUNT those shared lines. The honest journey LOC
is the UNION of owned spans across member flows, merged per file into
non-overlapping (start, end) intervals — consistent with the
flow-feature-concept line-level model ("a feature's lines are roughly the
union of its flows' lines") and with the existing product-feature flow
accounting in :func:`stage_6_97_feature_loc._apply_flow_accounting`,
which already unions member-journey spans per file. On the wave8 corpus
the union runs ~14-16% below the naive sum — exactly the intra-journey
file sharing it should collapse.

Honest zero, never null
-----------------------
A journey with zero resolvable owned spans (an ``mc=0`` system-recall
placeholder with no member flows, or a journey whose every member span is
null-line / interior / shared-out) gets ``loc = 0`` — an honest integer,
not ``None``.

Additivity / kill-switch
------------------------
Strictly ADDITIVE: the only output change is the new ``UserFlow.loc``
key. ``FAULTLINE_UF_LOC=0`` skips the stage entirely, leaving ``loc``
at its ``None`` default which the serializer omits — byte-identical to
the pre-B3 engine. Deterministic (sorted interval merge, list-ordered
inputs, no set iteration on the counted path). No LLM, no network, no
disk reads.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "STAGE_6_97B_ENV_FLAG",
    "uf_loc_enabled",
    "flow_owned_spans",
    "union_span_len",
    "apply_uf_loc",
]

STAGE_6_97B_ENV_FLAG = "FAULTLINE_UF_LOC"


def uf_loc_enabled() -> bool:
    """Default ON; ``FAULTLINE_UF_LOC=0`` (or false/False) disables."""
    return os.environ.get(STAGE_6_97B_ENV_FLAG, "1").strip() not in {
        "0", "false", "False",
    }


def _valid_span(ln: Any) -> tuple[int, int] | None:
    """A node's ``lines`` as an ordered ``(start, end)`` int pair, or
    ``None`` when it is not a valid 2-int span (null / malformed / bool)."""
    if (
        isinstance(ln, (list, tuple))
        and len(ln) == 2
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ln)
    ):
        s, e = int(ln[0]), int(ln[1])
        return (s, e) if s <= e else (e, s)
    return None


def flow_owned_spans(flow: Any) -> list[tuple[str, int, int]]:
    """OWNED ``(path, start, end)`` spans of ONE flow.

    Mirrors ``validate_scan.py:_spine_flow_loc_owned`` selection: keep
    ``flow.nodes`` entries with a valid 2-int ``lines`` span, EXCLUDING
    those with ``role == "interior"`` and those whose ``file`` is in the
    flow's ``shared_paths[]`` ledger. Null-line nodes contribute nothing.

    Works on both pydantic objects (attribute access) and plain dicts
    (rehydrated / replay artifacts) so it is reusable off the live path.
    """
    def _get(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    shared: set[str] = set()
    for sp in (_get(flow, "shared_paths") or []):
        p = _get(sp, "path")
        if p:
            shared.add(p)

    out: list[tuple[str, int, int]] = []
    for nd in (_get(flow, "nodes") or []):
        span = _valid_span(_get(nd, "lines"))
        if span is None:
            continue
        if _get(nd, "role") == "interior":
            continue
        file = _get(nd, "file")
        if not file or file in shared:
            continue
        out.append((file, span[0], span[1]))
    return out


def union_span_len(intervals: list[tuple[int, int]]) -> int:
    """Total line count of the UNION of inclusive ``(start, end)``
    intervals. Overlapping OR line-adjacent ranges merge (``s <= cur_e + 1``
    treats lines 10-12 and 13-15 as the contiguous 10-15). Deterministic:
    intervals are sorted before the sweep."""
    total = 0
    cur_s: int | None = None
    cur_e: int | None = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e + 1:  # type: ignore[operator]
            cur_e = max(cur_e, e)  # type: ignore[type-var]
        else:
            total += cur_e - cur_s + 1  # type: ignore[operator]
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += cur_e - cur_s + 1  # type: ignore[operator]
    return total


def _uf_owned_loc(uf: Any, flow_by_key: dict[str, Any]) -> int:
    """Journey OWNED LOC = per-file UNION of member flows' owned spans."""
    spans_by_file: dict[str, list[tuple[int, int]]] = {}
    for mid in (getattr(uf, "member_flow_ids", None) or []):
        fl = flow_by_key.get(mid)
        if fl is None:
            continue
        for path, s, e in flow_owned_spans(fl):
            spans_by_file.setdefault(path, []).append((s, e))
    # sorted() over keys keeps the sum order deterministic (belt-and-braces;
    # the sum is commutative but we never iterate a set on the counted path).
    return sum(union_span_len(spans_by_file[p]) for p in sorted(spans_by_file))


def apply_uf_loc(
    user_flows: list[Any] | None,
    flows: list[Any] | None,
) -> dict[str, Any]:
    """Stamp ``loc`` (int) on every UserFlow IN PLACE; return telemetry.

    ``flows`` are keyed by BOTH ``uuid`` and ``name`` (member_flow_ids may
    carry either — see ``UserFlow.member_flow_ids``), mirroring
    ``stage_6_97_feature_loc._apply_flow_accounting``.
    """
    flow_by_key: dict[str, Any] = {}
    for fl in (flows or []):
        for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
            if key and key not in flow_by_key:
                flow_by_key[key] = fl

    stamped = 0
    nonzero = 0
    for uf in (user_flows or []):
        loc = _uf_owned_loc(uf, flow_by_key)
        uf.loc = loc
        stamped += 1
        if loc > 0:
            nonzero += 1

    return {
        "enabled": True,
        "user_flows_total": stamped,
        "user_flows_with_loc": nonzero,
        "user_flows_zero_loc": stamped - nonzero,
    }
