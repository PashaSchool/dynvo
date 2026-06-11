"""Stage 6.96 — impact-over-time: import-graph blast radius at N git
snapshots (deterministic, $0 LLM).

Stage 6.95 answers "how did this entity's OWN activity evolve?" from
the in-memory commit list. This sibling stage answers the question the
commit list cannot: "how did the REST of the codebase's dependence on
this entity evolve?" — the entity's blast radius (external importer
count) at N historical snapshots, materialised via short-lived
``git worktree`` checkouts (see :mod:`faultline.pipeline_v2.snapshots`).

Validated by the Phase-0 prototype on documenso (8 snapshots, ~10 s
total): PF "AI" external importers 427→756 (+77% / 11 months), i18n
69→10 (decoupled into a package), Prisma 331→515 — decision-grade
stories invisible to the per-week commit series.

Health-over-time deliberately does NOT get snapshots: the health
formula is 100% git-derived, so Stage 6.95's ``health_lite`` series
already covers it without a single checkout. Impact is the only
snapshot metric.

Entity → member-set resolution
==============================
Reuses Stage 6.95's resolution verbatim (``_entity_specs``): product
feature → final ``paths`` aggregate, user flow → union of member
flows' ``paths``. The member set is TODAY's paths projected
retroactively — consistent with 6.95; ``ImpactPoint.members_present``
records how many of them exist at each snapshot. Only entities that
already carry a ``history`` (i.e. were scored by 6.95) gain ``impact``
— the field lives ON :class:`EntityHistory`.

Scale-invariance of the event rule (project hard rule)
======================================================
``coupling_spike`` / ``decoupled`` fire on a per-step relative reach
change ``(r_b - r_a) / max(r_a, 1)``. There is NO tuned absolute
threshold; a step is an event only when BOTH gates pass, each derived
from the scan's OWN pooled distributions:

  1. ``|rel| > P90`` of the pooled nonzero ``|rel|`` values across ALL
     entities of the scan — top-decile semantics ("a spike is a step
     more extreme than 9 in 10 of this repo's own reach moves"), the
     same percentile-of-own-distribution family as Stage 6.95's
     ``TEST_WAVE_PERCENTILE`` quartile rule.
  2. ``|Δreach| > P50`` (median) of the pooled nonzero ``|Δreach|`` —
     suppresses micro-entities whose 1→3 step is a huge ratio but a
     trivial absolute move, again relative to the repo's own deltas.

Min-points gate: an entity needs >= ``MIN_IMPACT_POINTS_FOR_EVENTS``
(3) impact points before any event fires — with fewer points there is
at most one delta, i.e. zero context to distinguish a spike from the
series' ordinary slope (structural requirement, not a tuned constant).
Steps where EITHER endpoint has ``members_present == 0`` are excluded
from both the pool and the events: such a step measures the entity
being born, not its coupling changing.

``impact_trend`` compares the first vs last snapshot where the entity
exists: the total relative drift must exceed the scan's own MEDIAN
per-step ``|rel|`` (a drift smaller than the repo's typical
between-snapshot fluctuation is indistinguishable from noise →
``stable``).

Events are APPENDED to the existing ``history.events`` list (then
re-sorted by week) — additive schema only, no SCHEMA_VERSION bump.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from faultline.models.types import (
    Commit,
    Feature,
    Flow,
    HistoryEvent,
    ImpactPoint,
    UserFlow,
)
from faultline.pipeline_v2.snapshots import (
    DEFAULT_SNAPSHOT_COUNT,
    build_snapshot_import_index,
    impact_reach,
    percentile,
    run_snapshots,
    select_snapshot_commits,
)
from faultline.pipeline_v2.stage_6_95_history import (
    _entity_specs,
    _week_index,
    _week_label,
)

logger = logging.getLogger(__name__)

__all__ = [
    "stage_6_96_impact",
    "MIN_IMPACT_POINTS_FOR_EVENTS",
    "SPIKE_PERCENTILE",
]

# Minimum impact points before spike/decouple events can fire — see the
# module docstring (structural: < 3 points gives <= 1 delta, no context).
MIN_IMPACT_POINTS_FOR_EVENTS: int = 3

# Percentile of the SCAN's pooled nonzero |relative Δreach| a step must
# STRICTLY exceed to be an event. Top-decile semantics — computed from
# the scan's own distribution, never an absolute constant.
SPIKE_PERCENTILE: float = 0.90

# Median (P50) — used for the absolute-|Δreach| noise gate and the
# trend band. Median semantics: "the repo's typical reach move".
_MEDIAN: float = 0.50


def _entity_deltas(
    points: list[ImpactPoint],
) -> list[tuple[ImpactPoint, ImpactPoint, float, int]]:
    """Consecutive (a, b, rel, abs_delta) steps where BOTH endpoints
    have the entity existing (``members_present > 0``)."""
    out: list[tuple[ImpactPoint, ImpactPoint, float, int]] = []
    for a, b in zip(points, points[1:]):
        if a.members_present <= 0 or b.members_present <= 0:
            continue
        delta = b.reach - a.reach
        rel = delta / max(a.reach, 1)
        out.append((a, b, rel, abs(delta)))
    return out


def stage_6_96_impact(
    product_features: list[Feature],
    user_flows: list[UserFlow],
    flows: list[Flow],
    developer_features: list[Feature],
    commits: list[Commit],
    repo_path: Path,
    *,
    subpath: str | None = None,
    snapshot_count: int = DEFAULT_SNAPSHOT_COUNT,
    log: Any = None,
) -> dict[str, Any]:
    """Attach ``history.impact`` (+ events + trend) to every entity that
    Stage 6.95 scored. Mutates the entities in place; returns the stage
    telemetry dict.

    Per-snapshot work: one detached worktree checkout, one reverse-
    import index over the snapshot tree, then an O(members) reach
    lookup per entity. Any snapshot failure is skipped (warning), the
    wall budget caps the total (see ``snapshots.py``); a fully-failed
    runner leaves every ``impact`` list empty — the scan never fails.

    ``subpath`` mirrors ``ScanContext.subpath``: member paths and the
    index are subtree-relative, so the snapshot scan root is
    ``<worktree>/<subpath>``. A snapshot where the subtree does not
    exist yet yields ``reach=0 / members_present=0`` points.
    """
    t0 = time.monotonic()
    telemetry: dict[str, Any] = {
        "impact_snapshots": 0,
        "impact_skipped_snapshots": 0,
        "impact_budget_exceeded": False,
        "entities_total": 0,
        "entities_with_impact": 0,
        "coupling_spike_events": 0,
        "decoupled_events": 0,
        "trends": {"growing": 0, "shrinking": 0, "stable": 0},
    }

    specs = _entity_specs(
        product_features, user_flows, flows, developer_features,
    )
    entities: list[tuple[Any, list[str]]] = [
        (obj, paths)
        for _kind, obj, paths, _tests in specs
        if getattr(obj, "history", None) is not None and paths
    ]
    telemetry["entities_total"] = len(entities)

    snaps = select_snapshot_commits(commits, snapshot_count)
    if not entities or not snaps:
        telemetry["skipped"] = True
        telemetry["elapsed_sec"] = round(time.monotonic() - t0, 3)
        return telemetry

    def _compute(worktree_root: Path, _sha: str) -> list[tuple[int, int]]:
        scan_root = worktree_root / subpath if subpath else worktree_root
        if not scan_root.is_dir():
            # Subtree doesn't exist at this snapshot — every entity
            # predates its own birth here.
            return [(0, 0)] * len(entities)
        index = build_snapshot_import_index(scan_root)
        return [impact_reach(paths, index) for _obj, paths in entities]

    results, run_telemetry = run_snapshots(
        repo_path,
        [c.sha for c in snaps],
        _compute,
        log=log,
    )
    telemetry.update(
        {
            k: run_telemetry[k]
            for k in (
                "impact_snapshots",
                "impact_skipped_snapshots",
                "impact_budget_exceeded",
                "budget_sec",
                "planned_snapshots",
            )
        },
    )

    # ── Assemble per-entity series (snapshot order is ascending) ─────
    series: list[list[ImpactPoint]] = [[] for _ in entities]
    for commit in snaps:
        per_entity = results.get(commit.sha)
        if per_entity is None:
            continue  # failed / budget-skipped snapshot
        week = _week_label(commit.date.date())
        for i, (reach, present) in enumerate(per_entity):
            series[i].append(
                ImpactPoint(week=week, reach=reach, members_present=present),
            )

    # ── Pooled scale-invariant thresholds (see module docstring) ─────
    pooled_rel: list[float] = []
    pooled_abs: list[float] = []
    for points in series:
        for _a, _b, rel, abs_delta in _entity_deltas(points):
            if abs_delta > 0:
                pooled_rel.append(abs(rel))
                pooled_abs.append(float(abs_delta))
    rel_threshold = percentile(pooled_rel, SPIKE_PERCENTILE) if pooled_rel else 0.0
    abs_threshold = percentile(pooled_abs, _MEDIAN) if pooled_abs else 0.0
    trend_band = percentile(pooled_rel, _MEDIAN) if pooled_rel else 0.0

    # ── Attach impact + events + trend ───────────────────────────────
    for (obj, _paths), points in zip(entities, series):
        history = obj.history
        history.impact = points
        if not points:
            continue
        telemetry["entities_with_impact"] += 1

        deltas = _entity_deltas(points)
        if len(points) >= MIN_IMPACT_POINTS_FOR_EVENTS and pooled_rel:
            new_events: list[HistoryEvent] = []
            for a, b, rel, abs_delta in deltas:
                if abs(rel) <= rel_threshold or abs_delta <= abs_threshold:
                    continue
                kind = "coupling_spike" if rel > 0 else "decoupled"
                new_events.append(
                    HistoryEvent(
                        kind=kind,  # type: ignore[arg-type]
                        week=b.week,
                        detail=f"reach {a.reach}->{b.reach}",
                    ),
                )
                key = (
                    "coupling_spike_events"
                    if rel > 0 else "decoupled_events"
                )
                telemetry[key] += 1
            if new_events:
                history.events.extend(new_events)
                history.events.sort(
                    key=lambda e: (_week_index(e.week), e.kind),
                )

        # Trend: first vs last snapshot where the entity exists.
        alive = [p for p in points if p.members_present > 0]
        if len(alive) >= 2:
            total_rel = (alive[-1].reach - alive[0].reach) / max(
                alive[0].reach, 1,
            )
            if total_rel > trend_band:
                trend = "growing"
            elif -total_rel > trend_band:
                trend = "shrinking"
            else:
                trend = "stable"
            history.impact_trend = trend
            telemetry["trends"][trend] += 1

    telemetry["elapsed_sec"] = round(time.monotonic() - t0, 3)
    return telemetry
