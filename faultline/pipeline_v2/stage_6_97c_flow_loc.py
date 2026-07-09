"""Stage 6.97c — deterministic Flow-level OWNED/SHARED LOC ($0, output layer).

Operator bug B11 (2026-07-09): distinct flows that share one file rendered an
IDENTICAL file-grain LOC. The reactive-resume email trio — ``verify-email``,
``verify-email-change``, ``reset-password-templates`` — each own a small unique
entry span in ``packages/email/src/templates/auth.tsx`` (13 lines) PLUS the
same shared ``AuthEmailLayout`` span ``[36,135]`` (100 lines). With no
flow-level LOC field the dashboard fell back to the file's line count and
showed a blended "113" for all three, hiding both the sharing and each flow's
own footprint.

This stage stamps two ADDITIVE fields on every :class:`Flow`:

  * ``loc``        — OWNED-EXCLUSIVE line count: the lines of this flow's owned
                     spans that NO OTHER flow covers (its unique story).
  * ``loc_shared`` — SHARED line count: the owned-span lines this flow shares
                     with ≥1 sibling flow (the blast-radius surface — a shared
                     helper/layout legitimately belongs to every flow that uses
                     it, per ``flow-feature-concept``).

OWNED-span selection (mirror of the validator)
----------------------------------------------
The owned spans are exactly :func:`stage_6_97b_uf_loc.flow_owned_spans` — the
in-memory mirror of the app-repo validator ``eval/validate_scan.py:
_spine_flow_loc_owned`` (nodes with a valid 2-int ``lines`` span, EXCLUDING
``role=="interior"`` and files in the flow's ``shared_paths[]`` ledger;
null-line nodes contribute 0). This stage does NOT recompute or reselect spans
— it only PARTITIONS that owned footprint into exclusive vs shared.

Conservation (why I13 / I19 are unmoved)
----------------------------------------
By construction ``loc + loc_shared == union(owned spans)`` — the SAME figure
``_spine_flow_loc_owned`` yields (the historical "113"). Nothing is excised;
the total owned LOC is only split for display, so the validator's loc-accounting
(I13) is unchanged and I19's owned-span numerator — computed independently from
``flow.nodes`` — is untouched (these fields are additive; the node ledger is
never mutated).

Sharing definition (deterministic, scale-invariant)
---------------------------------------------------
A line is SHARED when ≥2 DISTINCT flows' owned spans cover it, computed per file
by an interval-coverage sweep over every flow's owned footprint (each flow's own
overlapping spans are merged FIRST so a flow never counts against itself). No
tuned thresholds (``rule-no-magic-tuning``): the split is a pure structural
property of the corpus's flow spans.

Additivity / kill-switch
------------------------
Strictly ADDITIVE: the only output-JSON change is the new ``flows[].loc`` /
``flows[].loc_shared`` keys. ``FAULTLINE_FLOW_LOC=0`` skips the stage entirely,
leaving both at their ``None`` default which the serializer omits — byte-
identical to the pre-B11 engine. Deterministic (sorted files, sorted intervals,
sorted sweep events; list-ordered flow inputs; no set iteration on the counted
path). No LLM, no network, no disk reads.
"""

from __future__ import annotations

import os
from typing import Any

from faultline.pipeline_v2.stage_6_97b_uf_loc import (
    flow_owned_spans,
    union_span_len,
)

__all__ = [
    "STAGE_6_97C_ENV_FLAG",
    "flow_loc_enabled",
    "merge_intervals",
    "shared_mask_ge2",
    "intersect_intervals",
    "apply_flow_loc",
]

STAGE_6_97C_ENV_FLAG = "FAULTLINE_FLOW_LOC"


def flow_loc_enabled() -> bool:
    """Default ON; ``FAULTLINE_FLOW_LOC=0`` (or false/False) disables."""
    return os.environ.get(STAGE_6_97C_ENV_FLAG, "1").strip() not in {
        "0", "false", "False",
    }


def merge_intervals(
    intervals: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Merge overlapping OR line-adjacent inclusive ``(start, end)`` intervals
    into a sorted non-overlapping list (``s <= cur_e + 1`` treats 10-12 and
    13-15 as contiguous 10-15 — the same adjacency rule as
    :func:`union_span_len`). Deterministic: sorted before the sweep."""
    out: list[tuple[int, int]] = []
    cur_s: int | None = None
    cur_e: int | None = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e + 1:  # type: ignore[operator]
            cur_e = max(cur_e, e)  # type: ignore[type-var]
        else:
            out.append((cur_s, cur_e))  # type: ignore[arg-type]
            cur_s, cur_e = s, e
    if cur_s is not None:
        out.append((cur_s, cur_e))  # type: ignore[arg-type]
    return out


def shared_mask_ge2(
    per_flow_intervals: list[list[tuple[int, int]]],
) -> list[tuple[int, int]]:
    """Line segments (one file) covered by ≥2 DISTINCT flows.

    ``per_flow_intervals`` is one entry PER flow: that flow's OWN merged,
    non-overlapping intervals on this file. Because each flow's intervals are
    pre-merged, a flow contributes at most +1 at any line, so the running
    coverage between consecutive event lines equals the number of distinct
    flows covering that segment. Returns the merged segments where coverage is
    ≥2. Deterministic (a half-open ``+1`` at ``s`` / ``-1`` at ``e+1`` delta
    map, swept over sorted event lines)."""
    delta: dict[int, int] = {}
    for intervals in per_flow_intervals:
        for s, e in intervals:
            delta[s] = delta.get(s, 0) + 1
            delta[e + 1] = delta.get(e + 1, 0) - 1  # half-open (inclusive span)
    if not delta:
        return []
    shared: list[tuple[int, int]] = []
    cov = 0
    prev: int | None = None
    for line in sorted(delta):
        # The segment [prev, line-1] carries the coverage accumulated BEFORE
        # this line's delta is applied.
        if prev is not None and cov >= 2 and line - 1 >= prev:
            shared.append((prev, line - 1))
        cov += delta[line]
        prev = line
    return merge_intervals(shared)


def intersect_intervals(
    a: list[tuple[int, int]],
    b: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Intersection of two sorted non-overlapping inclusive-interval lists."""
    out: list[tuple[int, int]] = []
    i = j = 0
    a = sorted(a)
    b = sorted(b)
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo <= hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _flow_footprint(flow: Any) -> dict[str, list[tuple[int, int]]]:
    """This flow's owned spans, merged per file into non-overlapping intervals.
    Empty dict when the flow owns no valid spans."""
    by_file: dict[str, list[tuple[int, int]]] = {}
    for path, s, e in flow_owned_spans(flow):
        by_file.setdefault(path, []).append((s, e))
    return {p: merge_intervals(ivs) for p, ivs in by_file.items()}


def apply_flow_loc(flows: list[Any] | None) -> dict[str, Any]:
    """Stamp ``loc`` / ``loc_shared`` (ints) on every Flow IN PLACE; return
    telemetry. Deterministic; safe to call unconditionally (no-op inputs give
    an honest empty telemetry)."""
    flows = list(flows or [])
    # Per-flow footprint (merged intervals per file), computed once.
    footprints: list[dict[str, list[tuple[int, int]]]] = [
        _flow_footprint(fl) for fl in flows
    ]
    # Global per-file: the list of per-flow interval lists (for the ≥2 sweep).
    by_file: dict[str, list[list[tuple[int, int]]]] = {}
    for fp in footprints:
        for path, ivs in fp.items():
            by_file.setdefault(path, []).append(ivs)
    # Shared mask per file (sorted keys — determinism belt-and-braces).
    shared_by_file: dict[str, list[tuple[int, int]]] = {
        path: shared_mask_ge2(by_file[path]) for path in sorted(by_file)
    }

    stamped = 0
    with_shared = 0
    for fl, fp in zip(flows, footprints):
        own_total = 0
        shared_total = 0
        for path in sorted(fp):
            ivs = fp[path]
            own_total += union_span_len(ivs)
            mask = shared_by_file.get(path) or []
            if mask:
                shared_total += union_span_len(intersect_intervals(ivs, mask))
        # Conservation: loc is the exclusive remainder of the owned footprint.
        fl.loc = own_total - shared_total
        fl.loc_shared = shared_total
        stamped += 1
        if shared_total > 0:
            with_shared += 1

    return {
        "enabled": True,
        "flows_total": stamped,
        "flows_with_shared": with_shared,
        "flows_exclusive_only": stamped - with_shared,
    }
