"""Stage 6.7a — deterministic UF pre-clustering (S2 Seg A, flag OFF).

The keyed UF layer's STRUCTURE (which flows compose which journey) is decided
today by LLM draws: 6.7c splits mega-UFs (Sonnet), 6.7d rewrites the whole
user_flows[] at journey grain (Sonnet). The 2026-07-18 probe measured the two
failure classes that follow: FAIL-OPEN (LLM dies mid-scan → the raw rollup
passes through: healthy 78-82 UFs vs 264 on the degraded Soc0 board) and
RESAMPLE (1 drifting flow of 980 flips the whole-batch cache key → −26% UF
count). Both are STRUCTURE failures — the deterministic substrate already
knows the clusters (94.9% stable flow-uuids).

Under ``FAULTLINE_UF_DET_AGGREGATION`` the journey STRUCTURE is computed
deterministically here and the LLM layer may ONLY NAME it (precedent law
"LLM-abstraction P0: fit = output-layer"): the 6.7b refiner keeps refining
name/description/intent/ui_tier/acceptance per domain (its contract already
forbids membership changes), while the structural LLM stages (6.7c split,
6.7d rewrite) are skipped by phase_finalize. Consequence — UF-COUNT is
invariant to LLM death AND to resampling, structurally.

The cluster rule (calibrated on the healthy Soc0 13:25Z artifacts, $0):
ONE cluster PER ROLLUP DOMAIN — the purest structural rule available (zero
tuned constants; scale-invariant). On the calibration artifacts it reproduces
the healthy GRAIN exactly (82 clusters vs the healthy run's 82 journey UFs)
and keeps 93% of the healthy journeys' domain fences (75/80 of the Sonnet
journeys draw from exactly one rollup domain). What it does NOT reproduce is
Sonnet's semantic member SELECTION: the healthy 6.7d run dropped 186/292
rollup UFs (54% of member slots) with NO structural predicate (kept vs
dropped profiles are indistinguishable on every structural axis — measured),
so its member-sets match this partition at mean Jaccard 0.52 (restricted to
the surviving-member universe; 0.36 raw), and even an ORACLE grouping of
atomic rollup UFs tops out at 63% of journeys at J>=0.8. That selection is
the LLM's semantic judgment and is deliberately NOT imitated here:
CONSERVATION is law (spec SACRED: no journey may be lost in pre-clustering —
fate-tally zero scattered), so clusters carry the FULL member union.

Deterministic, no LLM, no I/O. Default OFF; =0/unset leaves user_flows[]
untouched and the LLM stages gated exactly as before (byte-identical).
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import UserFlow

DET_AGGREGATION_ENV = "FAULTLINE_UF_DET_AGGREGATION"

# Fixed intent vocabulary order — majority tie-break only (a stable total
# order, not a preference weighting).
_INTENT_ORDER = (
    "author", "browse", "lifecycle", "execute", "manage", "bulk", "export",
    "other",
)


def det_aggregation_enabled() -> bool:
    """Default OFF — set ``FAULTLINE_UF_DET_AGGREGATION=1`` to arm."""
    return os.environ.get(DET_AGGREGATION_ENV, "0").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _dominant(ufs: list["UserFlow"]) -> "UserFlow":
    """The cluster's representative constituent: largest member_count, then
    smallest id — a deterministic, evidence-grounded choice (the heaviest
    journey carries the domain's primary vocabulary)."""
    return sorted(
        ufs, key=lambda u: (-(u.member_count or 0), u.id or ""),
    )[0]


def _majority_intent(ufs: list["UserFlow"]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for u in ufs:
        counts[u.intent or "other"] += 1
    # max count; ties resolved by the fixed vocabulary order.
    def rank(intent: str) -> tuple[int, int]:
        try:
            pos = _INTENT_ORDER.index(intent)
        except ValueError:
            pos = len(_INTENT_ORDER)
        return (-counts[intent], pos)
    return sorted(counts, key=rank)[0]


def _majority_pf(ufs: list["UserFlow"]) -> str | None:
    counts: dict[str, int] = defaultdict(int)
    for u in ufs:
        if u.product_feature_id:
            counts[u.product_feature_id] += 1
    if not counts:
        return None
    return sorted(counts, key=lambda s: (-counts[s], s))[0]


def aggregate_user_flows(
    user_flows: list["UserFlow"],
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Fold the deterministic rollup UFs into domain-grain journey clusters.

    Pure function: reads only the given UFs, returns NEW UserFlow objects
    (the inputs are not mutated) + telemetry with a per-input fate tally.
    Canonical order everywhere (sorted domains, sorted member unions) makes
    the output invariant to input order and to PYTHONHASHSEED.
    """
    telemetry: dict[str, Any] = {
        "enabled": True,
        "input_ufs": len(user_flows),
        "clusters": 0,
        "domains": 0,
        "members_in": 0,
        "members_out": 0,
        "singleton_clusters": 0,
        "merged_clusters": 0,
        "scattered": 0,          # conservation law: MUST stay 0
        "fate": {},              # input UF id -> cluster id (every input named)
    }
    if not user_flows:
        return [], telemetry

    from faultline.models.types import UserFlow

    by_domain: dict[str, list["UserFlow"]] = defaultdict(list)
    for u in user_flows:
        by_domain[str(u.domain) if u.domain is not None else ""].append(u)

    telemetry["domains"] = len(by_domain)
    members_in = 0
    for u in user_flows:
        members_in += len(set(u.member_flow_ids or []))
    telemetry["members_in"] = members_in

    clusters: list["UserFlow"] = []
    fate: dict[str, str] = {}
    members_out = 0
    for n, (dom_key, ufs) in enumerate(
        sorted(by_domain.items(), key=lambda kv: kv[0]), start=1,
    ):
        # Canonical constituent order (id) — stable regardless of input order.
        ufs = sorted(ufs, key=lambda u: u.id or "")
        dom = _dominant(ufs)
        member_union: set[str] = set()
        routes: set[str] = set()
        cross: set[str] = set()
        ac_draft = 0
        for u in ufs:
            member_union.update(u.member_flow_ids or [])
            routes.update(u.routes or [])
            cross.update(u.cross_links or [])
            ac_draft += u.ac_draft_count or 0
        cid = f"UF-{n:03d}"
        cluster = UserFlow(
            id=cid,
            name=dom.name,
            description=None,
            domain=dom.domain,
            product_feature_id=_majority_pf(ufs),
            intent=_majority_intent(ufs),
            resource=dom.resource,
            member_flow_ids=sorted(member_union),
            member_count=len(member_union),
            routes=sorted(routes),
            cross_links=sorted(cross),
            ac_draft_count=ac_draft,
            acceptance=[],
            coverage_pct=None,
            ui_tier=None,
            category=dom.category,
            trigger=dom.trigger,
            refined=False,
            name_confidence=dom.name_confidence,
        )
        clusters.append(cluster)
        members_out += len(member_union)
        for u in ufs:
            fate[u.id] = cid
        if len(ufs) == 1:
            telemetry["singleton_clusters"] += 1
        else:
            telemetry["merged_clusters"] += 1

    telemetry["clusters"] = len(clusters)
    telemetry["members_out"] = members_out
    telemetry["fate"] = fate
    telemetry["scattered"] = sum(
        1 for u in user_flows if u.id not in fate
    )
    return clusters, telemetry


__all__ = [
    "DET_AGGREGATION_ENV",
    "aggregate_user_flows",
    "det_aggregation_enabled",
]
