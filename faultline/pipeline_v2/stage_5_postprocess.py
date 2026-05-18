"""Stage 5 — post-process naming-discipline pass (no LLM).

Pure Python. Applies the Fix A/B/C/D + bare-``references`` subset of
``faultline.analyzer.post_process`` to the merged Stage 2 + Stage 4
``DeveloperFeature`` list, then upgrades each survivor to a public
:class:`faultline.models.types.Feature` ready for Stage 6 metrics
enrichment.

What this stage does
====================

  1. Convert ``DeveloperFeature`` (the pipeline-v2 internal record) to
     ``Feature`` (the public schema record), preserving the Layer 1
     fields ``layer="developer"`` + ``product_feature_id=None``.
  2. Run a TRIMMED subset of ``post_process``:

       - Fix A — empty-name drop
       - Fix B — uncategorized catch-all drop
       - Fix C — demo / references / examples package drop
       - bare-``references`` shared-infra drop (post_process commit
         7067839, via ``_NOISE_NAMES``)
       - Fix D — ``_slugify_names`` final-pass normalisation

  3. Skip the legacy aggregator paths:

       - ``merge_sub_features``        — sonnet_scanner-specific
       - ``reattribute_noise_files``   — pre-existing data
       - ``refine_by_path_signal``     — pre-existing data
       - ``extract_overlooked_top_dirs`` — Go-style monolith bias
       - ``commit_prefix_enrichment_pass`` — git-prefix mining
       - The mega-bucket / triple-slug / marketing-flow branches of
         ``drop_noise_features`` (sonnet-scanner output shapes only).

What this stage does NOT do
===========================

  - No LLM calls.
  - No mutation of ``Feature.layer`` or ``Feature.product_feature_id``.
  - No flow rewriting (Stage 3 owns flows; this only filters by name).

Idempotent on identical input.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from faultline.analyzer.post_process import (
    _DEMO_PREFIXES,
    _NOISE_NAMES,
    _is_uncategorized,
    _slugify_names,
)
from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows, FlowSpec

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── DeveloperFeature → public Feature conversion ──────────────────────────


def _flow_spec_to_flow(spec: FlowSpec) -> Flow:
    """Bridge :class:`FlowSpec` into the public :class:`Flow` schema.

    Stage 6 will enrich the Flow with git-blame data (authors,
    timeline, bug-fix metrics). For now we emit the minimal shape so
    serialisation roundtrips cleanly.
    """
    return Flow(
        name=spec.name,
        description=spec.description or None,
        entry_point_file=spec.entry_point_file,
        entry_point_line=spec.entry_point_line,
        paths=[spec.entry_point_file] if spec.entry_point_file else [],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )


def _dev_feature_to_feature(
    dev: DeveloperFeature,
    flows: list[FlowSpec] | None = None,
) -> Feature:
    """Bridge a Stage 2/3/4 :class:`DeveloperFeature` to a public
    :class:`Feature`. Layer 1 fields are stamped explicitly so the
    downstream FeatureMap validator routes this entry to
    ``developer_features``.
    """
    return Feature(
        name=dev.name,
        display_name=dev.display_name,
        description=dev.rationale or None,
        paths=list(dev.paths),
        authors=[],          # Stage 6 fills these.
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[_flow_spec_to_flow(f) for f in (flows or [])],
        layer="developer",
        product_feature_id=None,
    )


def _is_demo_name(name: str) -> bool:
    """Replicates Fix C's demo / references / examples drop predicate."""
    return any(
        name == p.rstrip("-/") or name.startswith(p) for p in _DEMO_PREFIXES
    )


# ── Naming-discipline pass ────────────────────────────────────────────────


def _apply_naming_discipline(
    features: list[Feature],
) -> tuple[list[Feature], list[tuple[str, str, int]]]:
    """Apply Fix A + Fix B + Fix C + bare-references + Fix D.

    Returns ``(survivors, dropped)``. ``dropped`` is a list of
    ``(name, reason, path_count)`` tuples for telemetry.
    """
    cleaned: list[Feature] = []
    dropped: list[tuple[str, str, int]] = []

    for f in features:
        name = f.name
        path_count = len(f.paths)

        # Fix A — empty-name drop.
        if not name or not name.strip():
            dropped.append((name, "empty name (Fix A)", path_count))
            continue

        # Fix B — uncategorized catch-all drop (incl. multi-slash).
        if _is_uncategorized(name):
            dropped.append((name, "uncategorized catch-all (Fix B)", path_count))
            continue

        # Fix C — demo / references / examples package drop.
        if _is_demo_name(name):
            dropped.append((name, "demo/example package (Fix C)", path_count))
            continue

        # Bare 'references' drop (post_process commit 7067839).
        if name in _NOISE_NAMES:
            dropped.append((name, "shared-infra/noise", path_count))
            continue

        cleaned.append(f)

    # Fix D — final-pass slugification.
    cleaned, slug_dropped = _slugify_names(cleaned)
    dropped.extend(
        (name, f"slug: {reason}", n) for (name, reason, n) in slug_dropped
    )

    return cleaned, dropped


# ── Public entry point ────────────────────────────────────────────────────


def stage_5_postprocess(
    deterministic: list[DeveloperFeature],
    residual: list[DeveloperFeature],
    flows_by_feature: dict[str, list[FlowSpec]] | None = None,
    ctx: "ScanContext | None" = None,
) -> list[Feature]:
    """Concatenate deterministic + residual features, apply
    naming-discipline filter (Fix A/B/C/D + bare-references), and emit
    public :class:`Feature` records ready for Stage 6.

    Args:
        deterministic: Stage 2 reconciled features (high/medium
            confidence).
        residual: Stage 4 LLM-fallback features (low confidence).
        flows_by_feature: optional ``{feature_name: [FlowSpec, ...]}``
            mapping from Stage 3. When None, every feature emits with
            an empty ``flows`` list.
        ctx: Stage 0 context (currently unused; reserved for symmetry
            and future telemetry).

    Returns:
        list of :class:`Feature` records with naming discipline applied
        and ``layer="developer"`` stamped.
    """
    _ = ctx  # reserved
    flows_by_feature = flows_by_feature or {}

    combined: list[DeveloperFeature] = list(deterministic) + list(residual)
    public_features: list[Feature] = [
        _dev_feature_to_feature(dev, flows_by_feature.get(dev.name, []))
        for dev in combined
    ]

    cleaned, dropped = _apply_naming_discipline(public_features)
    for name, reason, n in dropped:
        logger.info(
            "stage_5_postprocess: dropped %s (%s, %d files)", name, reason, n,
        )
    return cleaned


# ── Convenience adapter for callers using FeatureWithFlows ────────────────


def stage_5_from_stage3_result(
    deterministic: list[DeveloperFeature],
    stage3_features_with_flows: list[FeatureWithFlows],
    residual: list[DeveloperFeature],
    ctx: "ScanContext | None" = None,
) -> list[Feature]:
    """Variant for callers that already hold the Stage 3 output shape.

    Builds the ``flows_by_feature`` index from ``stage3_features_with_flows``
    keyed by ``feature.name``, then delegates to :func:`stage_5_postprocess`.

    Note: Stage 4 residual features carry no flows (Stage 3 ran before
    Stage 4) — they emit with ``flows=[]``.
    """
    flows_by_feature = {
        fwf.feature.name: fwf.flows for fwf in stage3_features_with_flows
    }
    return stage_5_postprocess(
        deterministic=deterministic,
        residual=residual,
        flows_by_feature=flows_by_feature,
        ctx=ctx,
    )


__all__ = [
    "stage_5_postprocess",
    "stage_5_from_stage3_result",
]
