"""Stage 6.99 — path_index-aware I16 journey re-home (B20).

An LLM-drawn journey (Stage 6.7d) can bind foreign-entry members that were
invisible at backstop time: the validator's I16 flags a UF as misattached when
>50% of its member-flow ``entry_point_file``s are owned (path_index → dev →
product_feature_id) by ANOTHER PF. B13's own-entry filter is backstop-only and
runs BEFORE path_index is final, so it cannot see the LLM-drawn foreign members.

This pass runs AFTER the final path_index, applies the validator's OWN
entry-owner ruler at bind time, and re-homes each majority-foreign UF to its
**strict-majority** entry-owner PF (the owner holding >50% of the UF's owned
member entries). Mutates ONLY ``UserFlow.product_feature_id`` — path_index,
member_files and PF membership are untouched.

The strict-majority guard is the fix's soul: a *plurality* that stays a minority
would swap the PF WITHOUT clearing I16 (pure churn on a genuinely distributed
journey). Requiring a strict majority guarantees the re-home clears the invariant
and touches nothing it cannot fix. Lane/None owners are never re-home targets.
Kill-switch ``FAULTLINE_I16_REHOME_B20=0`` → byte-identical.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any, Mapping

#: Mirror of the validator's lane sentinel (a lane-resident entry owner is
#: foreign but never a re-home target — a journey anchored on lane infra is not
#: re-homable, it is the "distributed / lane-entry" residual, left as-is).
_LANE_OWNER = object()

REHOME_ENV = "FAULTLINE_I16_REHOME_B20"

__all__ = ["REHOME_ENV", "i16_rehome_enabled", "rehome_foreign_entry_ufs"]


def i16_rehome_enabled() -> bool:
    """Default ON; ``FAULTLINE_I16_REHOME_B20=0`` restores the pre-B20 UF
    homes byte-identically."""
    return os.environ.get(REHOME_ENV, "1").strip().lower() not in {"0", "false"}


def _attr(o: Any, name: str) -> Any:
    return o.get(name) if isinstance(o, dict) else getattr(o, name, None)


def _file_owner_pf(
    path_index: Any,
    feat_by_uuid: Mapping[Any, Any],
    lane_uuids: frozenset[str],
) -> dict[str, Any]:
    """``file -> owning PF key`` — the validator's exact map (path_index →
    feature_uuid → dev.product_feature_id, product-layer → its own name, a
    lane-resident dev → the lane sentinel)."""
    owner: dict[str, Any] = {}
    if isinstance(path_index, dict):
        items: list[tuple[Any, Any]] = list(path_index.items())
    else:
        items = [(_attr(e, "path"), e) for e in (path_index or [])
                 if _attr(e, "path")]
    for p, ent in items:
        fuid = _attr(ent, "feature_uuid")
        f = feat_by_uuid.get(fuid)
        if f is not None:
            if _attr(f, "layer") == "developer":
                owner[p] = _attr(f, "product_feature_id")
            else:
                owner[p] = _attr(f, "name")
        if owner.get(p) is None and fuid in lane_uuids:
            owner[p] = _LANE_OWNER
    return owner


def rehome_foreign_entry_ufs(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
    path_index: Any,
    lane_rows: Any = None,
) -> dict[str, Any]:
    """Re-home majority-foreign UFs to their strict-majority entry-owner PF.
    Mutates ``uf.product_feature_id`` in place. Returns telemetry."""
    tele: dict[str, Any] = {"enabled": True, "rehomed": 0, "moves": []}
    feat_by_uuid = {_attr(f, "uuid"): f for f in
                    (list(features) + list(product_features)) if _attr(f, "uuid")}
    lane_uuids = frozenset(
        _attr(r, "uuid") for r in (lane_rows or []) if _attr(r, "uuid"))
    owner = _file_owner_pf(path_index or {}, feat_by_uuid, lane_uuids)
    flow_by_uuid = {}
    for f in features:
        for fl in (_attr(f, "flows") or []):
            u = _attr(fl, "uuid")
            if u:
                flow_by_uuid[u] = fl
    pf_keys = {(_attr(pf, "id") or _attr(pf, "name")) for pf in product_features}

    for uf in user_flows:
        pfid = _attr(uf, "product_feature_id")
        if not pfid:
            continue
        dist: Counter = Counter()
        chk = mis = 0
        for fid in (_attr(uf, "member_flow_ids") or []):
            fl = flow_by_uuid.get(fid)
            ep = _attr(fl, "entry_point_file") if fl is not None else None
            if not ep:
                continue
            own = owner.get(ep)
            if own is None:
                continue
            chk += 1
            dist[own] += 1
            if own != pfid:
                mis += 1
        if not chk or mis / chk <= 0.5:
            continue  # not majority-foreign (minority-foreign is left untouched)
        # strict-majority entry-owner PF (excl. lane/None), tie -> alpha.
        cand = sorted(
            ((c, o) for o, c in dist.items()
             if o is not _LANE_OWNER and o is not None),
            key=lambda x: (-x[0], str(x[1])))
        if not cand:
            continue
        top_ct, top_owner = cand[0]
        if top_ct / chk <= 0.5 or top_owner == pfid or top_owner not in pf_keys:
            continue  # no strict-majority PF target -> distributed/lane residual
        uf.product_feature_id = top_owner
        tele["rehomed"] += 1
        tele["moves"].append({
            "uf": _attr(uf, "name"), "from": pfid, "to": top_owner,
            "was_foreign": f"{mis}/{chk}", "now_foreign": f"{chk - top_ct}/{chk}",
        })
    return tele
