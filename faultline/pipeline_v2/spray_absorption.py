"""S2-A-v3 spray-generalization — the STRUCTURAL absorption stage
(``FAULTLINE_SPRAY_GENERALIZED``, default ON since the 2026-07-21
pack-3 flip, KEY_SCHEMA 34; explicit =0 stays the kill-switch).

The generalized R5-2 spray predicate + parent-name derivation live in
:mod:`faultline.pipeline_v2.naming_contract` (beside the R5-2 machinery
whose class the G0 boundary fences); THIS module owns the structural
APPLY — member union, row drops, I14 backpointer repoints — because the
naming contract's §4.8 hard law forbids identity writes inside the
naming module. Absorption is a sanctioned STRUCTURE change of the same
family as the 6.7e adjudicator merges and ``uf_synth_fold``: it rides
its own seam in ``phase_finalize`` immediately AFTER the naming
contract, so the predicate judges the FINAL display names — the same
names the 2026-07-19 probe judged (SHIP/HIGH: twenty-b 17/17 absorption
with group-absorption, 3 settings-PF groups AI/applications/data-model
-> 3 own-resource parents, 36 -> 22 rows; 0/55 boards false at K=2 and
K=3).

Laws held here:

* conservation — the parent takes the member UNION of the whole group
  (zero flow loss; ``member_count`` re-synced; routes union);
* I14 — every absorbed row's flow backpointers repoint to the survivor,
  never dangle;
* R5 no-new-dup — a parent name already worn by a live same-PF row
  outside the group is never duplicated (the survivor keeps its current
  name; absorption still happens);
* display law — a law-dirty mint never ships (survivor keeps its
  law-clean current name).

Flag unset/``0`` ⇒ :func:`run_spray_generalization` returns ``None``
before touching anything ⇒ user_flows[] + scan_meta byte-identical
(the KS 4-way gate).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from faultline.pipeline_v2.naming_contract import (
    _spray_fired_groups,
    _spray_parent_name,
    _spray_parent_resource,
    _uf_flow_maps,
    display_law_violations,
    load_naming_vocab,
    spray_generalized_enabled,
)

__all__ = [
    "apply_spray_generalization",
    "run_spray_generalization",
]


def run_spray_generalization(
    user_flows: list[Any],
    flows: Iterable[Any] = (),
    vocab: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The flag-gated stage entry ``phase_finalize`` wires right after the
    naming contract. OFF/unset ⇒ ``None`` BEFORE any read or mutation —
    the caller then writes no scan_meta key (byte-identity law)."""
    if not spray_generalized_enabled():
        return None
    v = vocab or load_naming_vocab()
    _, _, flow_by_id = _uf_flow_maps(flows)
    return apply_spray_generalization(user_flows, flow_by_id, v)


def apply_spray_generalization(
    user_flows: list[Any],
    flow_by_id: Mapping[str, Any],
    vocab: Mapping[str, Any],
) -> dict[str, Any]:
    """Collapse every fired spray group into ONE own-resource parent row.

    Mutates in place (only ever called behind ``spray_generalized_enabled``
    -> the OFF path never runs -> byte-identical). The survivor is the
    group's smallest id (deterministic, stays inside the scan's live id
    universe); it takes the minted parent name, the member UNION of the
    whole group (conservation — zero flow loss), the routes union, and the
    absorbed names on ``previous_names``; every absorbed flow backpointer
    is repointed (the I14 never-dangle law). A parent name already worn by
    a live same-PF row OUTSIDE the group keeps the survivor's current name
    instead (the R5 no-new-dup law — absorption still happens; a name is
    never duplicated). Returns telemetry."""
    tele: dict[str, Any] = {
        "groups_fired": 0,
        "rows_absorbed": 0,
        "parents": [],
        "parent_name_dup_kept": 0,
    }
    fired = _spray_fired_groups(user_flows, flow_by_id)
    if not fired:
        return tele

    # Deduped live flow objects for the I14 repoint (flow_by_id keys both
    # uuid and name forms onto the same object).
    _seen_fl: set[int] = set()
    live_flows: list[Any] = []
    for fl in flow_by_id.values():
        if id(fl) not in _seen_fl:
            _seen_fl.add(id(fl))
            live_flows.append(fl)

    dead_ids: set[str] = set()
    for (pfid, prefix), rows in fired:
        survivor = rows[0]
        absorbed = rows[1:]
        group_ids = {str(getattr(u, "id", "") or "") for u in rows}
        parent_name = _spray_parent_name(prefix, vocab)

        members: list[str] = []
        have: set[str] = set()
        routes: set[str] = set()
        for u in rows:
            for m in getattr(u, "member_flow_ids", None) or []:
                if str(m) not in have:
                    have.add(str(m))
                    members.append(str(m))
            routes.update(str(r) for r in (getattr(u, "routes", None) or []))

        folded = parent_name.strip().lower()
        dup = any(
            str(getattr(o, "product_feature_id", "") or "") == pfid
            and str(getattr(o, "id", "") or "") not in group_ids
            and str(getattr(o, "name", "") or "").strip().lower() == folded
            for o in user_flows
        )

        prev = list(getattr(survivor, "previous_names", None) or [])
        old_name = str(getattr(survivor, "name", "") or "")
        if dup:
            tele["parent_name_dup_kept"] += 1
        elif display_law_violations(parent_name, vocab):
            # A law-dirty mint never ships (house rule); the survivor keeps
            # its current law-clean name — absorption still happens.
            tele["parent_name_law_kept"] = (
                tele.get("parent_name_law_kept", 0) + 1)
        else:
            if old_name and old_name != parent_name and old_name not in prev:
                prev.append(old_name)
            survivor.name = parent_name
            survivor.resource = _spray_parent_resource(prefix)
        for u in absorbed:
            nm = str(getattr(u, "name", "") or "")
            if nm and nm not in prev:
                prev.append(nm)
        if hasattr(survivor, "previous_names"):
            survivor.previous_names = prev

        survivor.member_flow_ids = sorted(members)
        survivor.member_count = len(members)
        if routes:
            survivor.routes = sorted(routes)

        survivor_id = str(getattr(survivor, "id", "") or "")
        group_dead = {str(getattr(u, "id", "") or "") for u in absorbed}
        dead_ids.update(group_dead)
        for fl in live_flows:   # I14 — repoint, never dangle
            if str(getattr(fl, "user_flow_id", None) or "") in group_dead:
                fl.user_flow_id = survivor_id

        tele["groups_fired"] += 1
        tele["rows_absorbed"] += len(absorbed)
        tele["parents"].append(
            str(getattr(survivor, "name", "") or parent_name))

    if dead_ids:
        user_flows[:] = [
            u for u in user_flows
            if str(getattr(u, "id", "") or "") not in dead_ids
        ]
    return tele
