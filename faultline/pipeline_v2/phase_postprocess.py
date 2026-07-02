"""Per-tree pipeline phase — Stages 5 / 5.3 / 5.5 (post-process).

Extracted from ``run.py`` (refactor/run-decomposition) as straight-line
code — same stage order, same StageLogger stage indexes/names, same
artifact filenames, same deep-copy boundaries (via ``run._isolate``).

  - Stage 5   — naming discipline + A1 validation (+ incremental splice)
  - Stage 5.3 — sibling-router collapse (deterministic)
  - Stage 5.5 — bipartite store + blast-radius (deterministic)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultline.pipeline_v2.incremental_wiring import splice_untouched_features
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_5_3_sibling_collapse import (
    collapse_sibling_routes,
)
from faultline.pipeline_v2.stage_5_4_cross_flow_dedup import (
    dedup_cross_feature_flows,
)
from faultline.pipeline_v2.stage_5_5_bipartite import stage_5_5_bipartite
from faultline.pipeline_v2.stage_5_postprocess import (
    stage_5_from_stage3_result_with_telemetry,
)
from faultline.pipeline_v2.stage_7_output import write_stage_artifact


@dataclass
class PostprocessResult:
    """Stage 5 family outputs the orchestrator threads onward."""

    features: list[Any]
    stage5_result: Any
    validation_drops: Any
    s53: Any
    s53_features_pre: int
    s53_features_post: int
    s53_collapse_sample: list[dict[str, Any]]
    bipartite: Any
    s54_telemetry: dict[str, Any] | None = None


def run_postprocess_phase(
    *,
    deterministic_features: list[Any],
    stage3_features_with_flows: list[Any],
    residual_features: list[Any],
    ctx: Any,
    run_dir: Path,
    is_full_scan: bool,
    incremental_untouched: list[Any],
) -> PostprocessResult:
    """Run Stage 5 → 5.3 → 5.5 over the merged feature set.

    Body moved verbatim from ``run_pipeline_v2``.
    """
    # ``_isolate`` is looked up through the run module so it stays the
    # single deep-copy call site to instrument later.
    from faultline.pipeline_v2 import run as _run

    # ── Stage 5 — post-process (naming discipline + A1 validation) ─
    with StageLogger(run_dir, 5, "postprocess") as log5:
        stage5_result = stage_5_from_stage3_result_with_telemetry(
            deterministic=_run._isolate(deterministic_features),
            stage3_features_with_flows=_run._isolate(stage3_features_with_flows),
            residual=_run._isolate(residual_features),
            ctx=_run._isolate(ctx),
        )
        features = stage5_result.features
        validation_drops = stage5_result.validation_drops
        for name, reason in stage5_result.drop_log:
            log5.drop(name, reason)
        # ── Incremental splice (--since path ONLY) ─────────────────
        # Re-attach the untouched features re-hydrated from the base
        # scan (Stage 2.5) — see ``incremental_wiring`` for rationale.
        if not is_full_scan and incremental_untouched:
            spliced = splice_untouched_features(
                features, incremental_untouched,
            )
            log5.info(
                f"incremental splice: re-attached {spliced} untouched "
                f"feature(s) from base scan (skipped Stage 3/4)",
            )
        for feat in features:
            log5.emit(feat.name, "survived naming discipline")
        if any(v > 0 for v in validation_drops.as_dict().values()):
            log5.info(
                f"validation drops: filesystem_missing="
                f"{validation_drops.filesystem_missing} "
                f"anchor_duplicate={validation_drops.anchor_duplicate} "
                f"junk_name={validation_drops.junk_name}",
            )
        # Sprint S1 — sibling-workspace dedup telemetry.
        if stage5_result.dedup_merges:
            log5.info(
                f"dedup_merges: {len(stage5_result.dedup_merges)} "
                f"sibling-workspace duplicate(s) merged",
            )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=5,
            stage_name="postprocess",
            payload={
                "feature_count": len(features),
                "feature_names": [f.name for f in features],
                "validation_drops": validation_drops.as_dict(),
                "dedup_merges": [m.as_dict() for m in stage5_result.dedup_merges],
            },
            run_dir=run_dir,
        )

    # ── Stage 5.3 — sibling-router collapse (Sprint S4, deterministic) ─
    # Folds N≥3 route-shaped sibling features under a common parent
    # directory into ONE feature labelled after the parent. Anchor
    # preservation: Stage 2 high/medium-confidence features are kept
    # alongside their collapsed peers. Stage 4 low-confidence fallback
    # features collapse freely. Pure-Python, no LLM, no I/O.
    confidence_by_name: dict[str, str] = {}
    sources_by_name: dict[str, list[str]] = {}
    for f in deterministic_features:
        # Stage 2 produces "high" / "medium"; Stage 4 features map to
        # "low" via the residual feature loop below.
        confidence_by_name[f.name] = f.confidence
        sources_by_name[f.name] = list(f.sources)
    for f in residual_features:
        confidence_by_name.setdefault(f.name, "low")
        # Stage 4 fallback features carry no sources entry → empty
        # list means "no anchor signal"; the collapser falls back to
        # confidence and treats them as collapsible.
        sources_by_name.setdefault(f.name, [])
    with StageLogger(run_dir, 5, "sibling_collapse") as log5_3:
        s53 = collapse_sibling_routes(
            features,
            confidence_by_name=confidence_by_name,
            sources_by_name=sources_by_name,
            log=log5_3,
        )
        s53_features_pre = len(features)
        features = s53.features
        s53_features_post = len(features)
        s53_collapse_sample = [g.as_dict() for g in s53.collapse_groups[:5]]
        write_stage_artifact(
            ctx.repo_path,
            stage_index=5,
            stage_name="sibling_collapse",
            payload={
                "collapse_groups_count": len(s53.collapse_groups),
                "features_collapsed": s53.features_collapsed,
                "features_pre": s53_features_pre,
                "features_post": s53_features_post,
                "collapse_sample": s53_collapse_sample,
            },
            run_dir=run_dir,
        )

    # ── Stage 5.4 — cross-feature entry-twin flow dedup (deterministic) ─
    # Two overlapping features can each mint a flow at the SAME entry point
    # (S7-B dedups only within one feature's Stage-3 call) — one real flow
    # as two rows. Collapse globally BEFORE bipartite ids/edges exist; the
    # owner of the entry file keeps the flow. Default ON (bugfix);
    # FAULTLINE_STAGE_5_4_CROSS_FLOW_DEDUP=0 to disable.
    with StageLogger(run_dir, 5, "cross_flow_dedup") as log5_4:
        s54 = dedup_cross_feature_flows(features)
        s54_telemetry = s54.as_telemetry()
        log5_4.info(
            f"cross_flow_dedup enabled={s54.enabled} "
            f"entry_groups={s54.entry_groups} removed={s54.flows_removed}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=5,
            stage_name="cross_flow_dedup",
            payload=s54_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 5.5 — bipartite store + blast-radius (deterministic) ─
    # Pure in-memory pass over the Stage 5 features. Mutates each
    # contained Flow in place to populate the new bipartite fields
    # (id, primary_feature, secondary_features, shared_with_*_count,
    # cross_cutting), then returns a top-level flows[] projection and
    # the feature_flow_edges[] list. NO LLM — path-overlap only.
    with StageLogger(run_dir, 5, "bipartite") as log5_5:
        bipartite = stage_5_5_bipartite(features, log=log5_5)
        log5_5.info(
            f"bipartite: flows={bipartite.telemetry['flows_total']} "
            f"edges_primary={bipartite.telemetry['bipartite_edges_primary']} "
            f"edges_secondary={bipartite.telemetry['bipartite_edges_secondary']} "
            f"cross_cutting_flows={bipartite.telemetry['cross_cutting_flows_count']} "
            f"max_shared_flows={bipartite.telemetry['max_shared_with_flows']} "
            f"max_shared_features={bipartite.telemetry['max_shared_with_features']}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=5,
            stage_name="bipartite",
            payload={
                "telemetry": bipartite.telemetry,
                "flows": [
                    {
                        "id": f.id,
                        "name": f.name,
                        "primary_feature": f.primary_feature,
                        "secondary_features": list(f.secondary_features),
                        "shared_with_flows_count": f.shared_with_flows_count,
                        "shared_with_features_count": f.shared_with_features_count,
                        "cross_cutting": f.cross_cutting,
                    }
                    for f in bipartite.flows
                ],
                "edges": [e.model_dump() for e in bipartite.edges],
            },
            run_dir=run_dir,
        )

    return PostprocessResult(
        features=features,
        stage5_result=stage5_result,
        validation_drops=validation_drops,
        s53=s53,
        s53_features_pre=s53_features_pre,
        s53_features_post=s53_features_post,
        s54_telemetry=s54_telemetry,
        s53_collapse_sample=s53_collapse_sample,
        bipartite=bipartite,
    )


__all__ = [
    "PostprocessResult",
    "run_postprocess_phase",
]
