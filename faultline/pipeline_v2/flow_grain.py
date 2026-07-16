"""Seg D — flow-grain laws T1-T4 (B71, ``FAULTLINE_FLOW_GRAIN``, default OFF).

Census class E (docs/anchor-arc/naming-grain-census-20260716.md): ~64% of the
8920-flow corpus is helper-grain, and a large share is minted at the WRONG grain
— empty-span stubs, barrel re-exports, duplicate mints, and per-property fanout
off one code object. These four structural laws re-grain the flow store to the
journey grain, driven ONLY by each flow's own coordinate set (no dictionaries):

* **T1 (empty span)** — a flow with NO resolvable (path, start, end) coordinate
  breaks the ``(file,line)->flow`` reverse-lookup contract; it is not a flow.
* **T2 (barrel re-anchor)** — an entry that is a barrel/re-export file with no
  local definition span re-anchors onto its definition site, or (no def site)
  falls to a T1/T3 candidate. Never a rename.
* **T3 (containment)** — a flow whose span-set is a subset of a SIBLING flow's
  (same entry file) span-set is a duplicate mint; it folds into the container
  (spans/paths union — nothing lost). Covers Soc0 ``cases.py`` twins and the
  hopp ``kernel/src/index.ts`` identical-span re-export pairs.
* **T4 (fanout budget)** — flows on one entry that share an IDENTICAL span whose
  LOC dominates each flow's own disjoint tail are one code path minted N times
  (documenso ``rate-limits.ts`` x14: all share ``rate-limit.ts:61-197``, differ
  only by a 5-6 line config slice). They fold to one representative.

Output-neutral when OFF: :func:`plan_flow_grain` is only consulted behind
:func:`flow_grain_enabled`; the OFF path never calls it, so serialized output is
byte-identical. The plan is PURE (no mutation) — the caller applies it, so the
laws are unit-testable in isolation against the named exhibits + the survivor
anti-case.

CONSERVATION NOTE (deviation flagged to the operator gates): T3/T4 folds union
the loser's spans/paths into the winner (no coordinate lost) and T2 re-anchors
(no drop); only T1 (truly empty span) hard-excludes, and an empty-span flow can
anchor no journey. The runtime effect on ``flows[]`` / ``feature_flow_edges[]`` /
``user_flows[]`` conservation is the flow-census + keyed-A/B's to certify — these
units hold the mechanism, not the corpus number.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from faultline.pipeline_v2.census_join import flow_span_empty, flow_span_records

__all__ = [
    "FLOW_GRAIN_ENV",
    "BARREL_BASENAMES",
    "flow_grain_enabled",
    "flow_entry_file",
    "is_barrel_file",
    "span_subset",
    "FlowGrainPlan",
    "plan_flow_grain",
    "apply_flow_grain",
]

FLOW_GRAIN_ENV = "FAULTLINE_FLOW_GRAIN"

#: Barrel / re-export file basenames — a file whose job is to re-export a
#: package's public surface, not to define behaviour. Structural (glyph-level),
#: not a per-repo list: every stack's index/barrel convention.
BARREL_BASENAMES = frozenset({
    "index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs",
    "mod.rs", "__init__.py", "index.py",
})


def flow_grain_enabled() -> bool:
    """B71 Seg D flow-grain laws. Default **ON** since the 2026-07-16 horizon-1
    flip (KEY_SCHEMA 30; keyed proof documenso + novu green — loc=0 flows -> 0).
    Arms the flow-store re-grain (T1-T4). ``FAULTLINE_FLOW_GRAIN=0`` leaves the
    flow store (and every channel that references it) byte-identical (the
    kill-switch law forever; unset ≡ explicit ``1``)."""
    return os.environ.get(FLOW_GRAIN_ENV, "1").strip().lower() in {"1", "true"}


# ── span helpers ────────────────────────────────────────────────────────────


def flow_entry_file(flow: Any) -> str | None:
    """The flow's entry file — ``entry_point_file`` or the richer
    ``entry_point.path`` fallback."""
    ep = getattr(flow, "entry_point_file", None)
    if ep:
        return str(ep)
    entry = getattr(flow, "entry_point", None)
    if entry is not None:
        p = getattr(entry, "path", None) if not isinstance(entry, dict) else entry.get("path")
        if p:
            return str(p)
    return None


def is_barrel_file(path: str | None) -> bool:
    """True when ``path``'s basename is a barrel/re-export convention file."""
    if not path:
        return False
    return path.rsplit("/", 1)[-1] in BARREL_BASENAMES


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _span_loc(records: dict[str, list[tuple[int, int]]]) -> int:
    return sum(e - s + 1 for spans in records.values() for s, e in _merge(spans))


def span_subset(
    a: dict[str, list[tuple[int, int]]],
    b: dict[str, list[tuple[int, int]]],
) -> bool:
    """True when EVERY coordinate in span-set ``a`` is covered by span-set ``b``
    (same path). Empty ``a`` is never a subset (an empty flow is T1, not T3)."""
    if not a:
        return False
    for path, ranges in a.items():
        cover = _merge(b.get(path, []))
        for s, e in ranges:
            if not any(bs <= s and e <= be for bs, be in cover):
                return False
    return True


# ── grain plan ───────────────────────────────────────────────────────────────


@dataclass
class FlowGrainPlan:
    """A PURE re-grain decision over a flow list — the caller applies it.

    * ``drop`` — T1 uuids (empty span, un-anchorable).
    * ``reanchor`` — T2 {uuid: definition-site path} (entry moves, flow kept).
    * ``fold`` — T3/T4 {loser uuid: winner uuid} (loser's spans/paths union into
      the winner; loser row removed).
    """

    drop: set[str] = field(default_factory=set)
    reanchor: dict[str, str] = field(default_factory=dict)
    fold: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    def survivors(self, flows: Iterable[Any]) -> list[Any]:
        gone = self.drop | set(self.fold)
        return [f for f in flows if _uuid(f) not in gone]


def _uuid(flow: Any) -> str:
    return str(getattr(flow, "uuid", "") or getattr(flow, "name", ""))


def plan_flow_grain(flows: list[Any]) -> FlowGrainPlan:
    """Compute the T1-T4 re-grain plan for a flow list. Deterministic: iteration
    follows ``flows`` order; fold winners are chosen by (larger span mass, then
    lexicographically smaller uuid) so the same input always yields the same
    plan. Applies at most one verdict per flow, in priority T1 > T2 > T3 > T4."""
    plan = FlowGrainPlan()
    records = {_uuid(f): flow_span_records(f) for f in flows}

    # ── T1: empty span ────────────────────────────────────────────────────
    for f in flows:
        u = _uuid(f)
        if flow_span_empty(f):
            plan.drop.add(u)
            plan.reasons[u] = "t1_empty_span"

    live = [f for f in flows if _uuid(f) not in plan.drop]

    # ── T2: barrel entry with a definition site elsewhere ─────────────────
    # A flow entered at a barrel file whose OWN span lives entirely in the
    # barrel (owns 0 unique lines there) re-anchors onto a non-barrel span it
    # already carries — its real definition site. No def site => left for T3.
    for f in live:
        u = _uuid(f)
        entry = flow_entry_file(f)
        if not is_barrel_file(entry):
            continue
        recs = records[u]
        owned = getattr(f, "loc", None)
        non_barrel = [p for p in recs if not is_barrel_file(p)]
        if owned == 0 and non_barrel:
            plan.reanchor[u] = sorted(non_barrel)[0]
            plan.reasons.setdefault(u, "t2_barrel_reanchor")

    # ── T3 + T4: same-entry sibling folds ─────────────────────────────────
    by_entry: dict[str, list[Any]] = {}
    for f in live:
        entry = flow_entry_file(f)
        if entry:
            by_entry.setdefault(entry, []).append(f)

    def _mass(u: str) -> int:
        return _span_loc(records[u])

    def _better(a: Any, b: Any) -> Any:
        """Winner = larger span mass, tie -> lexicographically smaller uuid."""
        ua, ub = _uuid(a), _uuid(b)
        if _mass(ua) != _mass(ub):
            return a if _mass(ua) > _mass(ub) else b
        return a if ua <= ub else b

    for entry, group in by_entry.items():
        if len(group) < 2:
            continue
        folded: set[str] = set()

        # T3 — strict containment: loser span-set ⊆ a sibling's span-set. The
        # loser folds into the sibling ONLY when that sibling is the winner
        # (_better: larger span mass, tie -> smaller uuid) so the direction is
        # order-independent (mutual-subset twins fold deterministically).
        for a in group:
            ua = _uuid(a)
            if ua in folded or ua in plan.fold:
                continue
            for b in group:
                ub = _uuid(b)
                if ub == ua or ub in folded:
                    continue
                if span_subset(records[ua], records[ub]) and _uuid(_better(b, a)) == ub:
                    plan.fold[ua] = ub
                    plan.reasons.setdefault(ua, "t3_containment")
                    folded.add(ua)
                    break

        # T4 — shared-dominant-span fanout: >=2 flows share one identical span
        # record whose LOC dominates each flow's disjoint tail => one code path
        # minted N times. Fold into the representative (best _better).
        remaining = [f for f in group if _uuid(f) not in folded and _uuid(f) not in plan.fold]
        shared_index: dict[tuple[str, int, int], list[Any]] = {}
        for f in remaining:
            for p, ranges in records[_uuid(f)].items():
                for s, e in ranges:
                    shared_index.setdefault((p, s, e), []).append(f)
        for span_key, sharers in shared_index.items():
            if len(sharers) < 2:
                continue
            shared_loc = span_key[2] - span_key[1] + 1
            fam = []
            for f in sharers:
                u = _uuid(f)
                if u in folded or u in plan.fold:
                    continue
                tail = _span_loc(records[u]) - shared_loc
                if shared_loc > tail:  # shared block dominates this flow
                    fam.append(f)
            if len(fam) < 2:
                continue
            winner = fam[0]
            for f in fam[1:]:
                winner = _better(winner, f)
            wu = _uuid(winner)
            for f in fam:
                u = _uuid(f)
                if u == wu:
                    continue
                plan.fold[u] = wu
                plan.reasons.setdefault(u, "t4_fanout")
                folded.add(u)

    return plan


def _union_into(winner: Any, loser: Any) -> None:
    """Conservation: fold ``loser``'s coordinates into ``winner`` so nothing is
    lost — union paths + ``line_ranges`` (dedup, sorted-stable) and record the
    fold in ``winner.merged_from`` (lineage trail)."""
    w_paths = list(getattr(winner, "paths", None) or [])
    for p in getattr(loser, "paths", None) or []:
        if p not in w_paths:
            w_paths.append(p)
    if hasattr(winner, "paths"):
        winner.paths = w_paths
    w_ranges = list(getattr(winner, "line_ranges", None) or [])
    seen = {(r.path, r.start_line, r.end_line) for r in w_ranges}
    for r in getattr(loser, "line_ranges", None) or []:
        key = (r.path, r.start_line, r.end_line)
        if key not in seen:
            w_ranges.append(r)
            seen.add(key)
    if hasattr(winner, "line_ranges"):
        winner.line_ranges = sorted(
            w_ranges, key=lambda r: (r.path, r.start_line, r.end_line),
        )
    merged = list(getattr(winner, "merged_from", None) or [])
    lu = _uuid(loser)
    if lu and lu not in merged:
        merged.append(lu)
        if hasattr(winner, "merged_from"):
            winner.merged_from = merged


def apply_flow_grain(flows: list[Any]) -> dict[str, int]:
    """Apply the T1-T4 plan to ``flows`` IN PLACE (``flows[:]`` is reshaped so
    the caller's flow store — and every channel that references it — sees the
    re-grained set). Fold winners absorb loser coordinates (conservation);
    re-anchors move the entry; T1 empties are dropped. Returns telemetry.

    Only ever called behind :func:`flow_grain_enabled` — the OFF path never
    reaches here, so the default output is byte-identical."""
    plan = plan_flow_grain(flows)
    by_uuid = {_uuid(f): f for f in flows}
    for loser_u, winner_u in plan.fold.items():
        loser = by_uuid.get(loser_u)
        winner = by_uuid.get(winner_u)
        if loser is not None and winner is not None:
            _union_into(winner, loser)
    for u, path in plan.reanchor.items():
        f = by_uuid.get(u)
        if f is not None and hasattr(f, "entry_point_file"):
            f.entry_point_file = path
    flows[:] = plan.survivors(flows)
    reasons = plan.reasons
    return {
        "t1_empty_dropped": len(plan.drop),
        "t2_barrel_reanchored": len(plan.reanchor),
        "t3_containment_folded": sum(1 for r in reasons.values() if r == "t3_containment"),
        "t4_fanout_folded": sum(1 for r in reasons.values() if r == "t4_fanout"),
        "total_folded": len(plan.fold),
    }
