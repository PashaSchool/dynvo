"""Stage 5.5 — bipartite feature ↔ flow store + blast-radius metric.

Pure Python. No LLM. Runs AFTER Stage 5 post-process (so we operate
on the cleaned, slugified, naming-disciplined feature list) and BEFORE
Stage 6 metrics enrichment.

Why this stage exists
=====================

Today's containment view (``Feature.flows[]``) forces every flow to
have exactly one parent. But cross-cutting flows — auth checks,
logging, validation, telemetry — naturally belong to MANY features.
Picking one arbitrarily loses the "shared infrastructure" signal that
is half our moat vs Sourcegraph.

This stage promotes the existing per-feature lists into a bipartite
graph stored as ``feature_flow_edges[]`` while keeping the containment
view intact for the landing app's existing renderer. Each flow gets:

  * ``id``                         — global stable id (``primary::slug``)
  * ``primary_feature``            — canonical owner (from Stage 3)
  * ``secondary_features``         — cross-cutting attachments
  * ``shared_with_flows_count``    — flows sharing ≥1 path with this one
  * ``shared_with_features_count`` — ``len(secondary_features)``
  * ``cross_cutting``              — convenience flag

Algorithm
=========

Deterministic, two cheap passes over the post-Stage-5 feature list:

  1. Build ``path → set[feature_name]`` from ``Feature.paths``.
  2. For each ``Flow``:
       a. Resolve ``primary_feature`` (the feature that owns this flow
          in the containment view).
       b. Compute ``secondary_features`` = union over flow paths of
          ``path_to_features[p]``, minus the primary, minus the empty
          set.
       c. Mint ``flow.id`` = ``f"{primary}::{slug}"``.
  3. For each pair of flows ``(a, b)`` with a ≠ b, increment a counter
     when they share ≥1 path. The result is ``shared_with_flows_count``
     per flow.
  4. Emit one ``FeatureFlowEdge`` with type=``primary`` per flow plus
     one with type=``secondary`` per (flow, secondary feature) pair.
  5. Return the top-level ``flows[]`` projection AND the edge list,
     PLUS a Stage5_5 telemetry dict for ``scan_meta``.

Invariants
==========

  * Every flow has exactly one primary edge.
  * Secondary features never include the primary feature.
  * ``len(top_level.flows)`` == sum of ``len(f.flows)`` across all
    features (every flow has exactly one primary owner).
  * ``bipartite_edges_primary`` == ``len(top_level.flows)``.
  * Two flows that share zero paths produce ``shared_with_flows_count
    = 0``. A flow with zero paths produces 0 for both counts.

No LLM, no network, no git — pure in-memory computation over the
existing Stage 5 output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from faultline.models.types import Feature, FeatureFlowEdge, Flow

if TYPE_CHECKING:
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)


# ── Result shape ──────────────────────────────────────────────────────────


@dataclass
class Stage5_5Result:
    """Bipartite store + telemetry produced by Stage 5.5.

    Attributes:
        features: the post-Stage-5 features, with each contained
            ``Flow`` mutated in place so the new bipartite fields
            (``id``, ``primary_feature``, ``secondary_features``,
            ``shared_with_flows_count``, ``shared_with_features_count``,
            ``cross_cutting``) are populated.
        flows: top-level projection — every flow, once, in stable order
            (sorted by ``id``).
        edges: bipartite edge list. Every flow contributes exactly one
            ``type="primary"`` edge plus zero-or-more ``type="secondary"``
            edges.
        telemetry: counts to fold into ``scan_meta``.
    """

    features: list[Feature]
    flows: list[Flow]
    edges: list[FeatureFlowEdge]
    telemetry: dict[str, int] = field(default_factory=dict)


# ── Internal helpers ──────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Kebab-case slug used inside the global ``Flow.id``."""
    if not text:
        return ""
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _flow_id(primary: str, flow_name: str) -> str:
    """Mint the global stable id ``"{primary}::{slug}"``.

    We don't reuse ``flow_name`` verbatim because Stage 3 emits human
    labels that may carry stray casing or punctuation; we want a stable
    debuggable form across rescans.
    """
    return f"{primary}::{_slugify(flow_name)}"


def _build_path_to_features(features: Iterable[Feature]) -> dict[str, set[str]]:
    """Reverse-index ``Feature.paths`` + ``Feature.shared_attributions``
    so we can ask "who reaches into path P?".

    Two sources are consulted:

      * ``Feature.paths`` — primary path attribution (Stage 2). Every
        tracked file is owned by exactly one feature here.
      * ``Feature.shared_attributions[*].file_path`` — a feature that
        symbol-attributes into a file it doesn't own is still
        considered a reacher for blast-radius purposes. Per the
        Sprint B1 spec: "A flow whose primary feature owns ALL its
        paths plus another feature reaching in via shared_attributions
        → still counts as secondary."

    Without this second source, cross-feature flow attribution is
    structurally zero on the v2 pipeline because every flow's
    ``Flow.paths`` is its single ``entry_point_file`` (which by
    construction lives under exactly one feature's ``paths``).
    """
    out: dict[str, set[str]] = {}
    for feat in features:
        for path in feat.paths:
            if not path:
                continue
            out.setdefault(path, set()).add(feat.name)
        for attr in getattr(feat, "shared_attributions", None) or []:
            path = getattr(attr, "file_path", None)
            if not path:
                continue
            out.setdefault(path, set()).add(feat.name)
    return out


# ── Public entry point ────────────────────────────────────────────────────


def stage_5_5_bipartite(
    features: list[Feature],
    *,
    log: "StageLogger | None" = None,
) -> Stage5_5Result:
    """Compute the bipartite store + blast-radius metrics.

    Args:
        features: the Stage 5 output — features with their primary
            flows already attached via ``Feature.flows[]``.
        log: optional :class:`StageLogger` for per-edge / per-blast
            structured events.

    Returns:
        A :class:`Stage5_5Result` with the mutated feature list (so
        per-flow fields are populated for downstream stages), the
        top-level flow projection, the edge list, and a telemetry dict.

    Notes:
        Stage 6 expects ``Feature.flows[]`` populated so health / cost
        enrichment still hangs off the containment view. We do NOT
        strip it.
    """
    # ── Step 1 — reverse-index paths to feature names ───────────────
    path_to_features = _build_path_to_features(features)

    # ── Step 2 — walk every flow once, populate per-flow fields ─────
    # Collect (Flow, primary_feature_name) pairs in stable iteration
    # order so the top-level ``flows[]`` projection is reproducible
    # across rescans.
    all_flows: list[tuple[Flow, str]] = []
    for feat in features:
        primary = feat.name
        for flow in feat.flows:
            # Primary attribution is the containing feature; this also
            # canonicalises Stage 3's pre-B1 absence of these fields.
            flow.primary_feature = primary
            flow.id = _flow_id(primary, flow.name)

            # Cross-cutting attachments derived from path ownership.
            secondaries: set[str] = set()
            for path in flow.paths or []:
                owners = path_to_features.get(path) or set()
                for owner in owners:
                    if owner != primary:
                        secondaries.add(owner)
            flow.secondary_features = sorted(secondaries)
            flow.shared_with_features_count = len(secondaries)
            flow.cross_cutting = bool(secondaries)

            all_flows.append((flow, primary))

    # ── Step 3 — pairwise "share at least one path" → counters ──────
    # The intuitive O(N^2) is fine for our scale (Layer 1 produces a
    # few hundred flows max); we keep it deterministic instead of
    # introducing a hash-based shortcut that would obscure the math.
    flow_path_sets: list[set[str]] = [
        set(flow.paths or []) for (flow, _) in all_flows
    ]
    for i in range(len(all_flows)):
        paths_i = flow_path_sets[i]
        if not paths_i:
            continue
        shared_count = 0
        for j in range(len(all_flows)):
            if i == j:
                continue
            if paths_i & flow_path_sets[j]:
                shared_count += 1
        all_flows[i][0].shared_with_flows_count = shared_count

    # ── Step 4 — emit edges ────────────────────────────────────────
    edges: list[FeatureFlowEdge] = []
    for flow, primary in all_flows:
        assert flow.id is not None  # set above
        # Primary edge — one per flow.
        edges.append(
            FeatureFlowEdge(
                feature=primary,
                flow_id=flow.id,
                type="primary",
                reason=None,
            ),
        )
        if log is not None:
            log.info(
                f"edge primary feature={primary} flow_id={flow.id}",
                feature=primary,
                flow_id=flow.id,
                edge_type="primary",
            )

        # Secondary edges — one per cross-cutting feature.
        for sec in flow.secondary_features:
            edges.append(
                FeatureFlowEdge(
                    feature=sec,
                    flow_id=flow.id,
                    type="secondary",
                    reason="path-overlap",
                ),
            )
            if log is not None:
                # ``reason`` is the first positional param of
                # StageLogger.info — pass the structured edge-reason
                # under a different key so it lands in ``**extra``.
                log.info(
                    f"edge secondary feature={sec} flow_id={flow.id} "
                    f"reason=path-overlap",
                    feature=sec,
                    flow_id=flow.id,
                    edge_type="secondary",
                    edge_reason="path-overlap",
                )

        if log is not None:
            log.info(
                f"blast-radius flow_id={flow.id} "
                f"shared_with_flows={flow.shared_with_flows_count} "
                f"shared_with_features={flow.shared_with_features_count}",
                feature=primary,
                flow_id=flow.id,
                shared_with_flows=flow.shared_with_flows_count,
                shared_with_features=flow.shared_with_features_count,
            )

    # ── Step 5 — top-level projection ──────────────────────────────
    # Stable order: by id. The Flow instances themselves are SHARED
    # with Feature.flows[] (we don't deep-copy) — they're the same
    # object, mutated in place. Pydantic serialises them identically
    # in both locations, which is what we want.
    top_level_flows: list[Flow] = sorted(
        (flow for (flow, _) in all_flows), key=lambda f: f.id or "",
    )

    # ── Telemetry ──────────────────────────────────────────────────
    edges_primary = sum(1 for e in edges if e.type == "primary")
    edges_secondary = sum(1 for e in edges if e.type == "secondary")
    cross_cutting_flows = sum(
        1 for (flow, _) in all_flows if flow.cross_cutting
    )
    max_shared_with_flows = max(
        (flow.shared_with_flows_count for (flow, _) in all_flows),
        default=0,
    )
    max_shared_with_features = max(
        (flow.shared_with_features_count for (flow, _) in all_flows),
        default=0,
    )

    telemetry: dict[str, int] = {
        "bipartite_edges_total": len(edges),
        "bipartite_edges_primary": edges_primary,
        "bipartite_edges_secondary": edges_secondary,
        "cross_cutting_flows_count": cross_cutting_flows,
        "flows_total": len(all_flows),
        "max_shared_with_flows": max_shared_with_flows,
        "max_shared_with_features": max_shared_with_features,
    }

    return Stage5_5Result(
        features=features,
        flows=top_level_flows,
        edges=edges,
        telemetry=telemetry,
    )


__all__ = ["stage_5_5_bipartite", "Stage5_5Result"]
