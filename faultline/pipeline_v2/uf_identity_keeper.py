"""Cross-scan User-Flow identity keeper (output layer, strictly opt-in).

Problem: the UF layer pays a "name lottery" on every rescan — two scans
of an unchanged repo can surface noticeably different journey names
(measured within-session draw spread ≈ 4.6 coverage-F1). For a paying
customer that reads as instability. The keeper pins accepted UF
identities (``id`` + ``name``) across rescans so the lottery is paid
once per repo.

Architecture contract (the #1 project law — ``rule-cold-scan``)
================================================================

The legacy ``prev_assignments`` cache silently locked feature names from
ambient ``~/.faultline`` state and poisoned every evaluation. The keeper
is immune to that failure class BY CONSTRUCTION:

* Identity input is an EXPLICIT ``--prev-scan <path>`` argument (CLI) /
  ``prev_scan_json`` (API). The production WORKER passes the previous
  scan artifact from the DB. There is NO ambient filesystem discovery,
  NO ``~/.faultline`` read, NO default-on behaviour of any kind.
* Absent input ⇒ this module is never invoked and the scan output is
  byte-identical to an engine without the keeper (guarded by tests and
  the snapshot gate — ``UserFlow.identity`` is omitted from dumps when
  ``None``).
* Eval paths never pass a prev scan ⇒ evaluation is provably
  unaffected.

Design (deterministic, $0 LLM, output layer — the flow graph and Stage
6.7d are untouched):

1. **Matching** previous UF ↔ new UF, structural channels only:
   (a) Jaccard over ``member_flow_ids`` (flow uuids — stable when the
       upstream flow layer is stable, e.g. lineage-matched rescans);
   (b) Jaccard over ``routes`` (router paths survive uuid churn);
   (c) exact ``(resource, intent)`` key equality, admitted only when
       the key is UNIQUE on both sides (no ambiguity possible);
   name similarity is a tie-break ONLY, never an eligibility channel.
2. **On match** the new UF keeps the previous ``id`` and ``name`` and
   records ``identity`` telemetry. ``Flow.user_flow_id`` back-pointers
   are remapped so output linkage stays coherent.
3. **Unmatched new** UFs keep fresh ids (renumbered only on collision
   with a pinned id). **Disappeared** UFs are listed in
   ``scan_meta.uf_identity.retired[]`` — never resurrected (no zombie
   journeys).
4. **Threshold** is structural — ``OVERLAP_THRESHOLD = 0.5`` = majority
   overlap ("shares at least as much as it differs"), per
   ``rule-no-magic-tuning``. The 2026-07-05 sensitivity sweep on the 5
   recorded same-repo draw pairs (dub / cal-com / formbricks / saleor /
   supabase; 501 prev UFs total) showed pin-rate varies smoothly with
   no cliff: 76.7% @0.3 → 75.3% @0.4 → 74.7% @0.5 → 71.1% @0.6 →
   68.1% @0.7 → 65.7% @0.8. 0.5 is the scale-invariant majority
   midpoint, not a per-repo tuned value. Measured name-churn reduction
   on the same pairs: share of new UFs carrying a previous scan's name
   25.4% → 79.8% with the keeper.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

#: Structural majority-overlap eligibility cutoff (see module docstring).
OVERLAP_THRESHOLD = 0.5

#: Wave-3 (§4.8) keeper master switch. Default ON — the PRODUCTION path
#: (worker passes ``prev_scan_json`` from the DB) pins identities and
#: display names by default. ``FAULTLINE_KEEPER=0`` disables BOTH the UF
#: identity keeper and the naming-contract display-pin channel even when
#: a prev scan is provided — eval runners set it (alongside simply not
#: passing a prev scan) so cold-scan purity stays provable
#: (rule-cold-scan). Absent prev-scan input the keeper never runs either
#: way — there is still NO ambient filesystem discovery.
KEEPER_ENV = "FAULTLINE_KEEPER"


def keeper_enabled() -> bool:
    """Default ON; ``FAULTLINE_KEEPER=0`` disables all cross-scan pinning."""
    return os.environ.get(KEEPER_ENV, "1").strip().lower() not in {
        "0", "false",
    }

_UF_ID_RE = re.compile(r"^UF-(\d+)$")


# ── Prev-scan loading (explicit input only) ────────────────────────────


def load_prev_scan(path: Path | str) -> dict[str, Any]:
    """Load an EXPLICITLY provided previous feature-map JSON.

    Raises ``ValueError`` on unreadable / non-object documents so the
    caller can degrade loudly (warning in ``scan_meta``) instead of
    silently scanning without identity.
    """
    p = Path(path)
    try:
        doc = json.loads(p.read_text())
    except OSError as exc:
        raise ValueError(f"prev-scan not readable: {p} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"prev-scan is not valid JSON: {p} ({exc})") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"prev-scan is not a JSON object: {p}")
    return doc


# ── Matching ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UFMatch:
    """One accepted previous-UF ↔ new-UF pairing."""

    prev_idx: int
    new_idx: int
    basis: str            # "member" | "route" | "resource-intent"
    overlap: float        # structural Jaccard of the winning channel
    key_equal: bool       # (resource, intent) agreement
    name_sim: float       # tie-break only


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _uf_field(uf: Any, name: str, default: Any = None) -> Any:
    """Field access that works for both pydantic UserFlow and plain dicts."""
    if isinstance(uf, dict):
        return uf.get(name, default)
    return getattr(uf, name, default)


def _key(uf: Any) -> tuple[str, str]:
    return (_norm(_uf_field(uf, "resource")), _norm(_uf_field(uf, "intent")))


def _members(uf: Any) -> set[str]:
    return {str(m) for m in (_uf_field(uf, "member_flow_ids") or []) if m}


def _routes(uf: Any) -> set[str]:
    return {str(r) for r in (_uf_field(uf, "routes") or []) if r}


def _name_sim(a: Any, b: Any) -> float:
    return SequenceMatcher(
        None, _norm(_uf_field(a, "name")), _norm(_uf_field(b, "name"))
    ).ratio()


def match_user_flows(
    prev_ufs: Sequence[Any],
    new_ufs: Sequence[Any],
    *,
    threshold: float = OVERLAP_THRESHOLD,
) -> list[UFMatch]:
    """Deterministic 1:1 greedy matching of previous UFs to new UFs.

    Eligibility (structural only — name similarity NEVER qualifies a
    pair, it only orders equal candidates):

    * best structural Jaccard (members, else/or routes) ≥ ``threshold``;
      OR
    * ``(resource, intent)`` keys are equal AND that key is unique in
      BOTH scans (an unambiguous journey identity).

    Greedy order: overlap desc → key-equality desc → name-similarity
    desc → prev id asc → new index asc. Fully deterministic for a given
    input pair.
    """
    # Unique-key maps for the (resource, intent) channel.
    def _unique_keys(ufs: Sequence[Any]) -> set[tuple[str, str]]:
        counts: dict[tuple[str, str], int] = {}
        for uf in ufs:
            k = _key(uf)
            if k != ("", ""):
                counts[k] = counts.get(k, 0) + 1
        return {k for k, n in counts.items() if n == 1}

    prev_unique = _unique_keys(prev_ufs)
    new_unique = _unique_keys(new_ufs)

    candidates: list[UFMatch] = []
    for pi, p in enumerate(prev_ufs):
        p_members, p_routes, p_key = _members(p), _routes(p), _key(p)
        for ni, n in enumerate(new_ufs):
            member_j = _jaccard(p_members, _members(n))
            route_j = _jaccard(p_routes, _routes(n))
            if member_j >= route_j:
                basis, overlap = "member", member_j
            else:
                basis, overlap = "route", route_j
            key_equal = p_key != ("", "") and p_key == _key(n)
            structurally_eligible = overlap >= threshold
            key_eligible = (
                key_equal and p_key in prev_unique and p_key in new_unique
            )
            if not structurally_eligible and not key_eligible:
                continue
            if not structurally_eligible:
                basis = "resource-intent"
            candidates.append(
                UFMatch(
                    prev_idx=pi,
                    new_idx=ni,
                    basis=basis,
                    overlap=round(overlap, 4),
                    key_equal=key_equal,
                    name_sim=round(_name_sim(p, n), 4),
                )
            )

    candidates.sort(
        key=lambda m: (
            -m.overlap,
            -int(m.key_equal),
            -m.name_sim,
            str(_uf_field(prev_ufs[m.prev_idx], "id") or ""),
            m.new_idx,
        )
    )

    taken_prev: set[int] = set()
    taken_new: set[int] = set()
    accepted: list[UFMatch] = []
    for m in candidates:
        if m.prev_idx in taken_prev or m.new_idx in taken_new:
            continue
        taken_prev.add(m.prev_idx)
        taken_new.add(m.new_idx)
        accepted.append(m)
    return accepted


# ── Application (pin ids + names, remap FKs, telemetry) ────────────────


def _next_uf_number(used_ids: set[str]) -> int:
    top = 0
    for uid in used_ids:
        m = _UF_ID_RE.match(uid)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def apply_identity_keeper(
    user_flows: list[Any],
    flows: list[Any],
    prev_scan: dict[str, Any],
    *,
    threshold: float = OVERLAP_THRESHOLD,
) -> dict[str, Any]:
    """Pin matched UF identities in place; return ``uf_identity`` telemetry.

    Mutates ``user_flows`` (ids / names / ``identity``) and the
    ``Flow.user_flow_id`` back-pointers on ``flows``. Never adds or
    removes a UF: disappeared previous UFs are only LISTED in the
    returned telemetry (``retired[]``) — no zombie journeys.
    """
    prev_ufs = [
        u for u in (prev_scan.get("user_flows") or []) if isinstance(u, dict)
    ]
    prev_meta = prev_scan.get("scan_meta") or {}
    prev_scan_id = str(
        prev_meta.get("run_id")
        or prev_scan.get("analyzed_at")
        or "prev-scan"
    )

    matches = match_user_flows(prev_ufs, user_flows, threshold=threshold)
    matched_new = {m.new_idx: m for m in matches}
    matched_prev = {m.prev_idx for m in matches}

    id_remap: dict[str, str] = {}
    used_ids: set[str] = set()
    renames_prevented = 0
    basis_counts: dict[str, int] = {}

    # Pass 1 — pinned UFs claim their previous id + name.
    for m in matches:
        uf = user_flows[m.new_idx]
        prev = prev_ufs[m.prev_idx]
        old_id = str(_uf_field(uf, "id") or "")
        prev_id = str(prev.get("id") or old_id)
        prev_name = str(prev.get("name") or _uf_field(uf, "name") or "")
        renamed_prevented = _norm(_uf_field(uf, "name")) != _norm(prev_name)
        renames_prevented += int(renamed_prevented)
        basis_counts[m.basis] = basis_counts.get(m.basis, 0) + 1
        identity = {
            "pinned_from": prev_scan_id,
            "prev_id": prev_id,
            "match_basis": m.basis,
            "overlap": m.overlap,
            "renamed_prevented": renamed_prevented,
        }
        if isinstance(uf, dict):  # defensive — normal path is pydantic
            uf["id"], uf["name"], uf["identity"] = prev_id, prev_name, identity
        else:
            uf.id = prev_id
            uf.name = prev_name
            uf.identity = identity
        if old_id:
            id_remap[old_id] = prev_id
        used_ids.add(prev_id)

    # Pass 2 — unmatched new UFs keep their id unless it collides with a
    # pinned id (or an earlier unmatched id); collisions renumber to the
    # next free UF-nnn, in original list order (deterministic).
    for ni, uf in enumerate(user_flows):
        if ni in matched_new:
            continue
        old_id = str(_uf_field(uf, "id") or "")
        final_id = old_id
        if not final_id or final_id in used_ids:
            final_id = f"UF-{_next_uf_number(used_ids):03d}"
        if final_id != old_id:
            if isinstance(uf, dict):
                uf["id"] = final_id
            else:
                uf.id = final_id
            if old_id:
                id_remap[old_id] = final_id
        used_ids.add(final_id)

    # FK remap — one pass over the ORIGINAL values so id swaps are safe.
    fk_remapped = 0
    for fl in flows or []:
        fk = _uf_field(fl, "user_flow_id")
        if fk and str(fk) in id_remap and id_remap[str(fk)] != str(fk):
            if isinstance(fl, dict):
                fl["user_flow_id"] = id_remap[str(fk)]
            else:
                fl.user_flow_id = id_remap[str(fk)]
            fk_remapped += 1

    retired = [
        {"id": str(p.get("id") or ""), "name": str(p.get("name") or "")}
        for pi, p in enumerate(prev_ufs)
        if pi not in matched_prev
    ]

    prev_total = len(prev_ufs)
    telemetry: dict[str, Any] = {
        "enabled": True,
        "prev_scan_id": prev_scan_id,
        "threshold": threshold,
        "prev_total": prev_total,
        "new_total": len(user_flows),
        "pinned": len(matches),
        "pin_rate": round(len(matches) / prev_total, 4) if prev_total else 0.0,
        "renames_prevented": renames_prevented,
        "basis_counts": basis_counts,
        "fk_remapped": fk_remapped,
        "retired": retired,
    }
    return telemetry


__all__ = [
    "KEEPER_ENV",
    "OVERLAP_THRESHOLD",
    "UFMatch",
    "apply_identity_keeper",
    "keeper_enabled",
    "load_prev_scan",
    "match_user_flows",
]
