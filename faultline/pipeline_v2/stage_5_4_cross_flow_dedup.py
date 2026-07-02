"""Stage 5.4 — cross-feature entry-point flow dedup (deterministic, $0).

Stage 3's S7-B dedup collapses same-entry flows WITHIN one feature's LLM
call, but its ``seen_entries`` set is call-local — when two features'
path-sets overlap (owner + shared-membership claimant), BOTH flow-detect
the same code object and each mints its own flow at the same
``(entry_point_file, entry_point_line)``. The result is one real flow
serialized as two rows under different names/features (measured 2026-07-02:
plane 41, supabase 15, documenso 6 extra rows; line_ranges identical in
every observed pair — e.g. ``group-data-by-criteria-flow@array`` vs
``sort-and-group-data-flow@shared-state`` both at ``array.ts:19``).

Same doctrine as S7-B ("flows sharing an entry point are the same flow
under different names"), applied GLOBALLY after the feature set is final
(post 5.3 sibling-collapse, pre 5.5 bipartite — so ids/edges/rollup never
see the twins). Winner selection is semantic then deterministic:

  1. the flow whose PRIMARY feature OWNS the entry file (it is in that
     feature's ``paths``) — the owner's flow is the honest attribution;
  2. if none or several own it: the feature owning MORE of the flow's own
     ``paths`` (a scale-invariant specificity signal);
  3. final tie-break: lexicographically smallest ``(feature.name,
     flow.name)`` — stable across runs.

Losers are removed from their feature's ``flows`` list only — the
feature's paths/membership are untouched (this is a FLOW-row dedup, not a
file re-attribution). Default ON (a bugfix, like S7-B / the generated
strip); opt out via ``FAULTLINE_STAGE_5_4_CROSS_FLOW_DEDUP=0``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import Feature


def _is_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_5_4_CROSS_FLOW_DEDUP", "1") != "0"


@dataclass
class CrossFlowDedupResult:
    enabled: bool = False
    entry_groups: int = 0          # same-entry groups with >1 flow
    flows_removed: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "entry_groups": self.entry_groups,
            "flows_removed": self.flows_removed,
            "sample": list(self.sample[:20]),
        }


def dedup_cross_feature_flows(
    features: list["Feature"],
) -> CrossFlowDedupResult:
    """Collapse cross-feature same-entry flow twins. Mutates *features* in
    place (removes loser flows). No-op when disabled or nothing collides."""
    result = CrossFlowDedupResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    # (entry_file, entry_line) -> [(feature, flow)] — input order preserved.
    groups: dict[tuple[str, int], list[tuple["Feature", Any]]] = {}
    for feat in features:
        for fl in getattr(feat, "flows", None) or []:
            ef = getattr(fl, "entry_point_file", None)
            el = getattr(fl, "entry_point_line", None)
            if ef and el is not None:
                groups.setdefault((ef, el), []).append((feat, fl))

    to_remove: dict[int, set[int]] = {}  # id(feature) -> {id(flow), ...}
    for (ef, _el), members in groups.items():
        if len(members) < 2:
            continue
        result.entry_groups += 1

        def _rank(pair: tuple["Feature", Any]) -> tuple:
            feat, fl = pair
            owns_entry = ef in set(getattr(feat, "paths", None) or [])
            flow_paths = set(getattr(fl, "paths", None) or [])
            owned_of_flow = len(
                flow_paths & set(getattr(feat, "paths", None) or []))
            # sort ascending → winner first: owner first (False<True flipped
            # via negation), then most-owned, then stable lexicographic.
            return (
                0 if owns_entry else 1,
                -owned_of_flow,
                getattr(feat, "name", "") or "",
                getattr(fl, "name", "") or "",
            )

        ordered = sorted(members, key=_rank)
        winner_feat, winner_flow = ordered[0]
        for feat, fl in ordered[1:]:
            to_remove.setdefault(id(feat), set()).add(id(fl))
            result.flows_removed += 1
            if len(result.sample) < 20:
                result.sample.append({
                    "entry": f"{ef}:{_el}",
                    "kept": f"{getattr(winner_flow, 'name', '')}"
                            f"@{getattr(winner_feat, 'name', '')}",
                    "removed": f"{getattr(fl, 'name', '')}"
                               f"@{getattr(feat, 'name', '')}",
                })

    if to_remove:
        for feat in features:
            drop = to_remove.get(id(feat))
            if drop:
                feat.flows = [
                    fl for fl in (getattr(feat, "flows", None) or [])
                    if id(fl) not in drop
                ]
    return result


__all__ = ["CrossFlowDedupResult", "dedup_cross_feature_flows"]
