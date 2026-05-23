"""Stage 6.8 — lineage + path/routes indexing.

Sprint 1 (2026-05-23). Runs after Stage 6 metrics and before Stage 7
output assembly. Pure post-processing — does NOT change any
quality-affecting scan decision. Outputs:

  * stamps ``uuid`` + ``previous_names`` + ``split_from`` +
    ``merged_from`` on every Feature and Flow.
  * builds ``path_index`` + ``routes_index`` (additive scan surfaces).
  * extends ``scan_meta`` with ``lineage_feature_stats`` +
    ``lineage_flow_stats`` for telemetry.

When ``base_scan`` is ``None``, every feature/flow gets a fresh
uuid4 (cold-scan default).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.indexes import (
    build_path_index,
    build_routes_index,
)
from faultline.pipeline_v2.lineage import (
    RELATED_THRESHOLD,
    RENAME_THRESHOLD,
    assign_feature_lineage,
    assign_flow_lineage,
)

logger = logging.getLogger(__name__)


@dataclass
class LineageResult:
    """What Stage 6.8 produces. Caller wires these into Stage 7."""

    path_index: dict[str, dict[str, Any]]
    routes_index: list[dict[str, Any]]
    feature_lineage_stats: dict[str, Any]
    flow_lineage_stats: dict[str, Any]
    carried_forward_count: int = 0


def _feature_to_dict(f: Feature) -> dict[str, Any]:
    return {
        "name": f.name,
        "paths": list(f.paths),
        "uuid": f.uuid or "",
        "previous_names": list(f.previous_names),
    }


def _flow_to_dict(f: Flow) -> dict[str, Any]:
    return {
        "name": f.name,
        "paths": list(f.paths),
        "uuid": f.uuid or "",
        "previous_names": list(f.previous_names),
    }


def run_stage_6_8(
    features: list[Feature],
    flows: list[Flow],
    *,
    base_scan: dict[str, Any] | None = None,
    extractor_signals: dict[str, list[Any]] | None = None,
    rename_threshold: float = RENAME_THRESHOLD,
    related_threshold: float = RELATED_THRESHOLD,
) -> LineageResult:
    """Stamp lineage UUIDs on features + flows and build the indexes.

    Mutates the Feature / Flow objects in place (sets ``uuid``,
    ``previous_names``, ``split_from``, ``merged_from``).

    Args:
        features: Stage 6 enriched developer features.
        flows: Sprint B1 bipartite flow list.
        base_scan: previous scan dict (from
            :func:`faultline.pipeline_v2.incremental.load_base_scan`).
            None on cold scans.
        extractor_signals: Stage 1 raw outputs — passed through to
            ``build_routes_index`` so the route registry can map
            file → feature_uuid using owners we just stamped.
        rename_threshold / related_threshold: Jaccard cutoffs.

    Returns:
        :class:`LineageResult` with the indexes + telemetry.
    """
    # ── Lineage: features ───────────────────────────────────────────
    base_features = (
        (base_scan or {}).get("developer_features")
        or (base_scan or {}).get("features")
        or []
    )
    new_feat_dicts = [_feature_to_dict(f) for f in features]
    feat_records, feat_stats = assign_feature_lineage(
        new_feat_dicts,
        base_features,
        rename_threshold=rename_threshold,
        related_threshold=related_threshold,
    )
    for f, rec in zip(features, feat_records):
        f.uuid = rec.uuid
        f.previous_names = list(rec.previous_names)
        f.split_from = rec.split_from
        f.merged_from = list(rec.merged_from)

    # ── Lineage: flows ──────────────────────────────────────────────
    base_flows = (base_scan or {}).get("flows") or []
    new_flow_dicts = [_flow_to_dict(f) for f in flows]
    flow_records, flow_stats = assign_flow_lineage(
        new_flow_dicts,
        base_flows,
        rename_threshold=rename_threshold,
        related_threshold=related_threshold,
    )
    for f, rec in zip(flows, flow_records):
        f.uuid = rec.uuid
        f.previous_names = list(rec.previous_names)
        f.split_from = rec.split_from
        f.merged_from = list(rec.merged_from)

    # ── Indexes ─────────────────────────────────────────────────────
    feat_view = [
        {"uuid": f.uuid, "paths": list(f.paths)} for f in features
    ]
    flow_view = [
        {"uuid": f.uuid, "paths": list(f.paths)} for f in flows
    ]
    path_index = build_path_index(feat_view, flow_view)
    routes_index = build_routes_index(feat_view, extractor_signals)

    logger.info(
        "stage_6_8: features new=%d base=%d carried=%d renamed=%d "
        "split=%d merged=%d fresh=%d | flows new=%d base=%d carried=%d",
        feat_stats.new_count, feat_stats.base_count,
        feat_stats.carried_forward, feat_stats.renamed,
        feat_stats.split, feat_stats.merged, feat_stats.fresh,
        flow_stats.new_count, flow_stats.base_count,
        flow_stats.carried_forward,
    )

    return LineageResult(
        path_index=path_index,
        routes_index=routes_index,
        feature_lineage_stats=feat_stats.as_dict(),
        flow_lineage_stats=flow_stats.as_dict(),
    )


__all__ = ["LineageResult", "run_stage_6_8"]
