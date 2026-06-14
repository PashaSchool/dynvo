"""Per-tree pipeline phase — Stage 8 Layer-2 family.

Extracted from ``run.py`` (refactor/run-decomposition) as straight-line
code — same stage order, same StageLogger stage indexes/names, same
artifact filenames, same telemetry keys.

  - Stage 8   — marketing-grounded Layer 2 clusterer (analyst / haiku)
  - Stage 8   — flow rollup (per-shape strategy dispatcher)
  - Stage 8.5 — deterministic path-overlap member backfill
  - Stage 8.6 — universal non-source scaffold/docs drop
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.incremental_wiring import reuse_base_layer2
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_7_output import write_stage_artifact
from faultline.pipeline_v2.stage_8_5_member_backfill import (
    run_stage_8_5_backfill,
)
from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
    drop_all_nonsource_features,
    drop_phantom_product_features,
    reconcile_product_features,
)
from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    desink_workspace_anchors,
)
from faultline.pipeline_v2.stage_8_analyst import (
    DEFAULT_ANALYST_MODEL as _STAGE_8_ANALYST_MODEL,
    run_stage_8_analyst,
)
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    _default_client_factory as _stage_8_default_client_factory,
    run_stage_8,
)
from faultline.pipeline_v2.stage_8_rollup_strategies import (
    stage_8_rollup_flows,
    write_rollup_artifact,
)


@dataclass
class Layer2Result:
    """Stage 8 family outputs the orchestrator threads onward."""

    features: list[Any]
    product_features: list[Any]
    dev_to_product_map: dict[str, Any]
    stage_8_telemetry: dict[str, Any]
    stage_8_rollup_telemetry: dict[str, Any]
    stage_8_5_backfill_telemetry: dict[str, Any]
    stage_8_6_telemetry: dict[str, Any]
    stage_8_7_telemetry: dict[str, Any]


def run_layer2_phase(
    *,
    ctx: Any,
    features: list[Any],
    product_features: list[Any],
    dev_to_product_map: dict[str, Any],
    product_telemetry: dict[str, Any],
    bipartite_flows: list[Any],
    model_id: str,
    tracker: CostTracker,
    run_dir: Path,
    incremental_layer2_noop: bool,
    incremental_base_scan: dict[str, Any] | None,
    llm_health: LlmHealth | None = None,
) -> Layer2Result:
    """Run Stage 8 → rollup → 8.5 → 8.6 over the enriched feature set.

    Body moved verbatim from ``run_pipeline_v2``.
    """
    # ── Stage 8 — marketing-grounded Layer 2 clusterer (Sprint E1) ──
    # Refines Stage 6.5's deterministic ``product_features`` using the
    # maintainer's PUBLIC marketing taxonomy + a single Haiku call.
    # Cascade: customer-yaml (passthrough) → marketing+haiku → fallback
    # to Stage 6.5 result. Marketing fetch + Haiku call only fire when
    # the Anthropic SDK is configured (ANTHROPIC_API_KEY set). NO
    # README reads — homepage discovery is package.json#homepage only.
    stage_8_telemetry: dict[str, Any] = {
        "source": "deterministic-only",
        "haiku_called": False,
    }
    with StageLogger(run_dir, 8, "marketing_clusterer") as log8:
        s8_client = _stage_8_default_client_factory()
        # Source-breakdown was already computed by Stage 6.5 and stamped
        # onto ``product_telemetry``; re-key for Stage 8's input.
        s8_pre_breakdown: dict[str, int] = product_telemetry.get(
            "product_clusterer_source_breakdown", {},
        )
        # In-repo nav taxonomy matches (Stage 6.5 rule 2.5) — the
        # vendor's own labels rank ABOVE external marketing, so both
        # Stage 8 modes receive the map and preserve those labels.
        s8_nav_map: dict[str, str] = product_telemetry.get(
            "nav_taxonomy_map", {},
        ) or {}
        # Sprint M4 dispatcher — ``FAULTLINE_STAGE_8_MODE`` selects
        # between the Sonnet analyst ("analyst", default since
        # 2026-05-21 corpus validation: avg L2 P 40.8 → 87.9, R 43.9 →
        # 85.7) and the legacy Haiku label-mapper ("haiku-clusterer",
        # retained as cheap fallback + automatic recovery when Sonnet
        # errors). Both modules expose ``run_stage_8*`` with identical
        # signatures so the rest of this stage is identical.
        s8_mode = os.environ.get(
            "FAULTLINE_STAGE_8_MODE", "analyst",
        ).strip().lower() or "analyst"
        if incremental_layer2_noop:
            # No developer feature changed → reuse the base scan's FINAL
            # Layer-2 verbatim (see ``incremental_wiring.reuse_base_layer2``).
            # incremental_layer2_noop implies a loaded base scan.
            assert incremental_base_scan is not None
            stage_8_result = reuse_base_layer2(incremental_base_scan, log8)
        elif s8_mode == "analyst":
            log8.info(f"mode=analyst model={_STAGE_8_ANALYST_MODEL}")
            stage_8_result = run_stage_8_analyst(
                ctx,
                features,
                product_features,
                dev_to_product_map_pre=dev_to_product_map,
                source_breakdown_pre=s8_pre_breakdown,
                # Sprint S6.3 — surface flows to the analyst so it can
                # populate ``member_flows`` per PF (consumed by Stage 8
                # rollup for oss-library / framework-repo shapes).
                top_flows=list(bipartite_flows),
                log=log8,
                client=s8_client,
                model=_STAGE_8_ANALYST_MODEL,
                cost_tracker=tracker,
                llm_health=llm_health,
                nav_taxonomy_map=s8_nav_map,
            )
        else:
            log8.info(f"mode=haiku-clusterer model={model_id}")
            stage_8_result = run_stage_8(
                ctx,
                features,
                product_features,
                dev_to_product_map_pre=dev_to_product_map,
                source_breakdown_pre=s8_pre_breakdown,
                log=log8,
                client=s8_client,
                model=model_id,
                cost_tracker=tracker,
                llm_health=llm_health,
                nav_taxonomy_map=s8_nav_map,
            )
        # Apply Stage 8 overrides — replace product_features and the
        # legacy single-valued ``product_feature_id`` stamp.
        product_features = stage_8_result.product_features
        dev_to_product_map = stage_8_result.dev_to_product_map
        for feat in features:
            labels = dev_to_product_map.get(feat.name)
            feat.product_feature_id = labels[0] if labels else None
        stage_8_telemetry = stage_8_result.telemetry
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="marketing_clusterer",
            payload={
                "telemetry": stage_8_telemetry,
                "product_features": [
                    {
                        "name": pf.name,
                        "developer_feature_count": len(pf.paths),
                        "paths_total": len(pf.paths),
                        "health_score": pf.health_score,
                    }
                    for pf in product_features
                ],
                "dev_to_product_map": {
                    k: list(v) for k, v in dev_to_product_map.items()
                },
            },
            run_dir=run_dir,
        )

    # ── Stage 8 (rollup) — attach flows to product_features ────────
    # Sprint S6.1 — per-shape flow-rollup dispatcher. Uses
    # ctx.repo_shape (from Stage 0.6) to pick a strategy:
    #   turborepo-monorepo → workspace-match
    #   single-saas-routed → entry-point-in-paths
    #   backend-monolith   → controller-class match (+ EP fallback)
    #   cli-tool           → command-name match (+ EP fallback)
    #   oss-library        → sonnet member_flows map ONLY (no path fb)
    #   framework-repo     → sonnet member_flows + EP fallback
    #   universal-residual → 2-pass entry-point + 50% overlap
    # ``sonnet_member_flows_map`` is sourced from Stage 8's analyst
    # response (Sprint S6.3). Empty dict when Haiku fallback fired
    # or when ``FAULTLINE_STAGE_8_MODE=haiku-clusterer`` — oss-library
    # / framework-repo strategies still degrade gracefully (no
    # attachments, logged warning) in that case.
    s8_member_flows_map = getattr(stage_8_result, "member_flows_map", {}) or {}
    with StageLogger(run_dir, 8, "rollup") as log8_rollup:
        rollup_result = stage_8_rollup_flows(
            product_features,
            list(bipartite_flows),
            ctx,
            sonnet_member_flows_map=s8_member_flows_map or None,
        )
        write_rollup_artifact(ctx, product_features, rollup_result)
        log8_rollup.info(
            f"rollup strategy={rollup_result.strategy_used} "
            f"pfs_attributed={rollup_result.pfs_attributed_count}/"
            f"{len(product_features)} "
            f"total_attachments={rollup_result.total_attachments} "
            f"unattributed_flows={len(rollup_result.unattributed_flows)}",
        )
    stage_8_rollup_telemetry: dict[str, Any] = {
        "rollup_strategy": rollup_result.strategy_used,
        "pfs_total": len(product_features),
        "pfs_attributed_count": rollup_result.pfs_attributed_count,
        "pfs_empty_count": len(product_features) - rollup_result.pfs_attributed_count,
        "total_attachments": rollup_result.total_attachments,
        "unattributed_flow_count": len(rollup_result.unattributed_flows),
        "unattributed_flow_pct": round(
            len(rollup_result.unattributed_flows) / max(len(bipartite_flows), 1),
            4,
        ),
        "capped_pfs_count": len(rollup_result.diagnostics.get("capped_pfs", [])),
    }

    # ── Stage 8.5 — deterministic path-overlap member backfill ─────
    # ADDITIVE: runs after the analyst + rollup, only ever STAMPS
    # ``product_feature_id`` on dev features the analyst left UNMAPPED
    # (the bulk that never reached the capped analyst payload). Never
    # touches the product_features[] array, the analyst prompt, or any
    # already-mapped feature → Layer-2 product P/R are invariant. Scale-
    # invariant majority-overlap threshold (see module docstring).
    # Default ON; disable via FAULTLINE_STAGE_8_5_BACKFILL=0.
    with StageLogger(run_dir, 8, "member_backfill") as log8_bf:
        backfill_result = run_stage_8_5_backfill(
            features,
            product_features,
            dev_to_product_map,
        )
        log8_bf.info(
            f"backfill enabled={backfill_result.enabled} "
            f"threshold={backfill_result.threshold} "
            f"attached={backfill_result.attached} "
            f"attached_pct {backfill_result.attached_pct_before:.3f}"
            f"->{backfill_result.attached_pct_after:.3f} "
            f"still_unmapped={backfill_result.still_unmapped}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="member_backfill",
            payload={
                "telemetry": backfill_result.as_telemetry(),
                "assignments": backfill_result.assignments,
            },
            run_dir=run_dir,
        )
    stage_8_5_backfill_telemetry = backfill_result.as_telemetry()

    # ── Stage 8.6 — universal non-source scaffold/docs drop ────────
    # Path-sets are FINAL after Stage 8.5 backfill. Drop every developer
    # feature whose entire path-set is non-source (docs / config / static
    # / certs / lockfiles) — junk that carries no behaviour yet inflates
    # the feature count and the llm_fallback_pct denominator. All-or-
    # nothing: a single source path keeps the feature. Deterministic, no
    # LLM, scale-invariant (extension-category + tiny build-leaf set; no
    # path names, counts, or ratios). Runs BEFORE scan_meta assembly so
    # llm_fallback_pct recomputes over the pruned set. After dropping we
    # reconcile Layer-2: recompute surviving product features' path union
    # and drop any product feature that lost all its members.
    # Default ON; disable via FAULTLINE_STAGE_8_6_NONSOURCE_DROP=0.
    stage_8_6_telemetry: dict[str, Any] = {
        "dropped": 0,
        "dropped_sample": [],
        "pf_recomputed": 0,
        "pf_dropped_empty": 0,
    }
    with StageLogger(run_dir, 8, "nonsource_drop") as log8_ns:
        features_before_ns = len(features)
        features, nonsource_dropped = drop_all_nonsource_features(features)
        if nonsource_dropped:
            product_features, pf_recon = reconcile_product_features(
                features, product_features,
            )
        else:
            pf_recon = {"recomputed": 0, "dropped_empty": 0}
        # Always drop phantom product features (zero developer members),
        # independent of the non-source drop above. reconcile_product_features
        # only runs when non-source features were dropped, so on a clean repo a
        # phantom PF (a named cluster whose members merged/renamed away) would
        # otherwise survive to output as a content-less, duplicate row.
        product_features, pf_phantom_dropped = drop_phantom_product_features(
            features, product_features,
        )
        # Keep dev_to_product_map consistent with surviving features +
        # product features (drop entries for vanished members / PFs).
        surviving_pf_names = {pf.name for pf in product_features}
        surviving_feat_names = {f.name for f in features}
        dev_to_product_map = {
            k: tuple(v for v in vals if v in surviving_pf_names)
            for k, vals in dev_to_product_map.items()
            if k in surviving_feat_names
        }
        stage_8_6_telemetry = {
            "dropped": len(nonsource_dropped),
            "dropped_sample": list(nonsource_dropped[:20]),
            "pf_recomputed": pf_recon["recomputed"],
            "pf_dropped_empty": pf_recon["dropped_empty"],
            "pf_dropped_phantom": pf_phantom_dropped,
        }
        log8_ns.info(
            f"nonsource_drop features {features_before_ns}->{len(features)} "
            f"dropped={len(nonsource_dropped)} "
            f"pf_recomputed={pf_recon['recomputed']} "
            f"pf_dropped_empty={pf_recon['dropped_empty']} "
            f"pf_dropped_phantom={pf_phantom_dropped}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="nonsource_drop",
            payload=stage_8_6_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.7 — workspace-anchor de-sink ───────────────────────
    # A workspace-anchor feature (``backend``, ``frontend-v2``,
    # ``packages/lib``) claims the WHOLE workspace tree as a fallback
    # container; Stage 6.3 then re-expands the specific route/mvc/schema
    # features inside it, so the anchor and those features double-claim
    # every import-reachable file → the anchor is a structural blob. This
    # stage releases the double-claim: the anchor keeps only the residual
    # (paths no specific feature claims), the rest stay attributed to the
    # specific owner. Pure precision (eval/structural_audit max-share
    # drops), no membership recall cost (eval/membership). Zero-path
    # protection keeps a fully-claimed anchor whole. Resyncs the affected
    # product features' path union itself (the Stage 8.6 reconcile above
    # is conditional). Deterministic, no LLM, scale-invariant. Default ON;
    # disable via FAULTLINE_STAGE_8_7_DESINK=0.
    with StageLogger(run_dir, 8, "anchor_desink") as log8_7:
        desink_result = desink_workspace_anchors(features, product_features)
        stage_8_7_telemetry = desink_result.as_telemetry()
        log8_7.info(
            f"anchor_desink enabled={desink_result.enabled} "
            f"anchors={desink_result.anchors_total} "
            f"desunk={desink_result.anchors_desunk} "
            f"protected={desink_result.anchors_protected} "
            f"paths_removed={desink_result.paths_removed} "
            f"pf_resynced={desink_result.product_features_resynced}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="anchor_desink",
            payload=stage_8_7_telemetry,
            run_dir=run_dir,
        )

    return Layer2Result(
        features=features,
        product_features=product_features,
        dev_to_product_map=dev_to_product_map,
        stage_8_telemetry=stage_8_telemetry,
        stage_8_rollup_telemetry=stage_8_rollup_telemetry,
        stage_8_5_backfill_telemetry=stage_8_5_backfill_telemetry,
        stage_8_6_telemetry=stage_8_6_telemetry,
        stage_8_7_telemetry=stage_8_7_telemetry,
    )


__all__ = [
    "Layer2Result",
    "run_layer2_phase",
]
