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
from faultline.replay.capture import write_stage_input
from faultline.pipeline_v2.stage_7_output import write_stage_artifact
from faultline.pipeline_v2.stage_8_5_member_backfill import (
    run_stage_8_5_backfill,
)
from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
    deown_anchor_scaffold,
    drop_all_nonsource_features,
    drop_phantom_product_features,
    reconcile_product_features,
    strip_nonsource_members,
)
from faultline.pipeline_v2.stage_8_6_5_scaffold_filter import (
    filter_shared_scaffold,
)
from faultline.pipeline_v2.stage_8_6_7_di_attribution import (
    attribute_di_services,
)
from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    desink_workspace_anchors,
)
from faultline.pipeline_v2.stage_8_8_shared_members import (
    enrich_shared_members,
)
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    subdecompose_workspace_anchors,
)
from faultline.pipeline_v2.stage_8_9_5_llm_component_split import (
    llm_component_split,
)
from faultline.pipeline_v2.stage_8_analyst import (
    DEFAULT_ANALYST_MODEL as _STAGE_8_ANALYST_MODEL,
    anchored_analyst_skip_active,
    anchored_skip_result,
    run_stage_8_analyst,
)
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    _default_client_factory as _stage_8_default_client_factory,
    run_stage_8,
)
from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
    attribute_domain_members,
    carve_service_domains,
)
from faultline.pipeline_v2.stage_8_9_7_vendor_connector_split import (
    split_vendor_connectors,
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
    stage_8_6_5_telemetry: dict[str, Any]
    stage_8_6_7_telemetry: dict[str, Any]
    stage_8_7_telemetry: dict[str, Any]
    stage_8_8_telemetry: dict[str, Any]
    stage_8_9_telemetry: dict[str, Any]
    stage_8_9_5_telemetry: dict[str, Any]
    stage_8_9_6_telemetry: dict[str, Any]
    stage_8_9_7_telemetry: dict[str, Any]


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
    write_stage_input(run_dir, 8, "marketing_clusterer", {
        "ctx": ctx,
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
        "product_telemetry": product_telemetry,
        "bipartite_flows": bipartite_flows,
        "model_id": model_id,
        "incremental_layer2_noop": incremental_layer2_noop,
    })
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
        elif s8_mode == "analyst" and anchored_analyst_skip_active(features):
            # Debt-pack (W2b follow-up): the anchored mint in
            # phase_finalize REPLACES the PF layer and writes
            # constrained-citation narratives — the Sonnet top-80 call
            # here was pure spend. Deterministic pass-through instead;
            # FAULTLINE_STAGE_8_ANALYST_SKIP_ANCHORED=0 restores it.
            log8.info(
                "mode=analyst SKIPPED (anchored-mint path owns the PF "
                "layer) — deterministic pass-through",
            )
            stage_8_result = anchored_skip_result(
                product_features, dev_to_product_map)
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
    write_stage_input(run_dir, 8, "rollup", {
        "product_features": product_features,
        "bipartite_flows": bipartite_flows,
        "ctx": ctx,
        "member_flows_map": s8_member_flows_map,
    })
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
    write_stage_input(run_dir, 8, "member_backfill", {
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
    })
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
    write_stage_input(run_dir, 8, "nonsource_drop", {
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
    })
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
        # Increment-4 LEVER A — workspace-anchor deflation (member-level).
        # (1) Strip non-source MEMBER files (static assets / locale-JSON /
        #     video) from any source/non-source-mix feature — a feature
        #     shouldn't OWN .png/.mp4/locale-json. (2) De-own shared-scaffold
        #     members (lib/utils/types/hooks/...) from WORKSPACE-ANCHOR
        #     features by flipping them to role="shared"/primary=False so they
        #     stop counting toward owned_max_feature_share. Both run AFTER
        #     flows/UFs are built (Stage 6.7) => flow-immune; the path stays in
        #     member_files (de-own) so phantom-dup / name dedup are unchanged.
        #     Deterministic, scale-invariant, reuses _path_is_source + a
        #     documented subset of the Stage-8.6.5 scaffold vocabulary.
        #     Default ON; FAULTLINE_STAGE_8_6_NONSOURCE_STRIP=0 /
        #     FAULTLINE_STAGE_8_6_ANCHOR_SCAFFOLD_DEOWN=0 disable them.
        nonsource_strip_result = strip_nonsource_members(features)
        anchor_scaffold_result = deown_anchor_scaffold(features)
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
            "nonsource_member_strip": nonsource_strip_result.as_telemetry(),
            "anchor_scaffold_deown": anchor_scaffold_result.as_telemetry(),
        }
        log8_ns.info(
            f"nonsource_drop features {features_before_ns}->{len(features)} "
            f"dropped={len(nonsource_dropped)} "
            f"pf_recomputed={pf_recon['recomputed']} "
            f"pf_dropped_empty={pf_recon['dropped_empty']} "
            f"pf_dropped_phantom={pf_phantom_dropped} "
            f"nonsource_members_stripped={nonsource_strip_result.members_removed}"
            f"(feats={nonsource_strip_result.features_trimmed}) "
            f"anchor_scaffold_deowned="
            f"{anchor_scaffold_result.members_reclassified}"
            f"(anchors={anchor_scaffold_result.anchors_deowned})",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="nonsource_drop",
            payload=stage_8_6_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.6.5 — shared-scaffold filter ───────────────────────
    # Shared workspace scaffold (packages/lib, packages/ui, i18n, utils,
    # hooks, shared components) leaks into SPECIFIC features' primary paths
    # via Stage 6.3 expansion (which has no fan-in guard). This stage demotes
    # a file from a specific feature's paths when it is BOTH under a structural
    # scaffold dir AND claimed by >= max(3, P90) specific features — top-decile
    # fan-in of the repo's own distribution. Runs BEFORE de-sink so the file
    # stays on its workspace anchor as residual (and surfaces as a role="shared"
    # member via Stage 8.8). The location restriction keeps high-fan-in DOMAIN
    # files (models/services) that a feature legitimately owns → precision rises
    # with recall protected (validated: documenso + inbox-zero recall flat-or-up,
    # inbox-zero micro precision +10pp). Scale-invariant, deterministic, no LLM.
    # Default ON; disable via FAULTLINE_STAGE_8_6_5_SCAFFOLD_FILTER=0.
    write_stage_input(run_dir, 8, "scaffold_filter", {"features": features})
    with StageLogger(run_dir, 8, "scaffold_filter") as log8_65:
        scaffold_result = filter_shared_scaffold(features)
        stage_8_6_5_telemetry = scaffold_result.as_telemetry()
        log8_65.info(
            f"scaffold_filter enabled={scaffold_result.enabled} "
            f"fan_in_threshold={scaffold_result.fan_in_threshold} "
            f"shared_scaffold_files={scaffold_result.shared_scaffold_files} "
            f"paths_removed={scaffold_result.paths_removed} "
            f"features_trimmed={scaffold_result.features_trimmed}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="scaffold_filter",
            payload=stage_8_6_5_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.6.7 — DI service attribution ───────────────────────
    # Services wired through dependency injection (a registry the framework
    # decorates onto a request object — ``server.services.secretService`` in
    # Fastify) are referenced from feature code BY NAME, never by import, so
    # static import-following can't reach them and they fall to the platform
    # bucket. Driven by eval/di-patterns.yaml (patterns in YAML, not Python),
    # this stage follows the NAMED reference: per detected pattern it maps each
    # referenced service token to its file(s) and attributes them to the
    # DOMINANT referencing feature under a scale-invariant fan-in cap (a service
    # used by too many features stays platform). Runs BEFORE de-sink so the moved
    # services are off the anchor. Validated on eval/membership/infisical
    # (machine-identities recall 0.17→0.64, platform_share −8pp, precision held).
    # Deterministic, no LLM. Default ON; disable via
    # FAULTLINE_STAGE_8_6_7_DI_ATTRIBUTION=0.
    write_stage_input(run_dir, 8, "di_attribution", {
        "ctx": ctx,
        "features": features,
    })
    with StageLogger(run_dir, 8, "di_attribution") as log8_67:
        di_result = attribute_di_services(ctx, features)
        stage_8_6_7_telemetry = di_result.as_telemetry()
        log8_67.info(
            f"di_attribution enabled={di_result.enabled} "
            f"files_moved={di_result.files_moved} "
            f"patterns={[p.pattern for p in di_result.patterns if p.detected]}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="di_attribution",
            payload=stage_8_6_7_telemetry,
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
    write_stage_input(run_dir, 8, "anchor_desink", {
        "features": features,
        "product_features": product_features,
    })
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

    # ── Stage 8.8 — shared-member enrichment of the de-sink residual ──
    # The de-sink residual (anchor-only files: shared services / models / UI)
    # is reached only by the anchor's own closure — the specific features that
    # actually USE it never claim it. This stage records that usage: for each
    # residual file, attach it as an N:M role="shared" member_file on every
    # specific feature whose own code DIRECTLY IMPORTS it (1-hop, the strongest
    # signal). NEVER touches feature.paths, so the paths-based gates
    # (structural max-share + membership) cannot regress by construction.
    # Honest: a shared <Button> shows on every feature that imports it;
    # genuinely-shared leaves with no importer stay residual. Deterministic, no
    # LLM. Default ON; disable via FAULTLINE_STAGE_8_8_SHARED_MEMBERS=0.
    write_stage_input(run_dir, 8, "shared_members", {
        "ctx": ctx,
        "features": features,
    })
    with StageLogger(run_dir, 8, "shared_members") as log8_8:
        shared_result = enrich_shared_members(ctx, features)
        stage_8_8_telemetry = shared_result.as_telemetry()
        log8_8.info(
            f"shared_members enabled={shared_result.enabled} "
            f"residual={shared_result.residual_files} "
            f"attached={shared_result.residual_attached} "
            f"coverage={shared_result.coverage_pct:.1%} "
            f"edges={shared_result.edges} "
            f"features_enriched={shared_result.features_enriched}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="shared_members",
            payload=stage_8_8_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.9 — workspace-anchor sub-decomposition ────────────────
    # The de-sink residual can still be a structural blob (one anchor owns
    # 20-25 % of the repo). This stage splits it along the repository's OWN
    # module structure (modules/ features/ services/ … → per-domain
    # developer sub-features), surfacing the real product capabilities the
    # blob hid and lifting feature recall. Each file lands in exactly ONE
    # domain bucket (no shared-file contention → zero attribution risk);
    # sub-features inherit the anchor's product_feature_id so product paths
    # are byte-stable. Deterministic, no LLM, scale-invariant (grain floor =
    # repo median feature size). Default ON; FAULTLINE_STAGE_8_9_SUBDECOMPOSE=0.
    write_stage_input(run_dir, 8, "anchor_subdecompose", {"features": features})
    with StageLogger(run_dir, 8, "anchor_subdecompose") as log8_9:
        subdecompose_result = subdecompose_workspace_anchors(features)
        stage_8_9_telemetry = subdecompose_result.as_telemetry()
        log8_9.info(
            f"anchor_subdecompose enabled={subdecompose_result.enabled} "
            f"anchors={subdecompose_result.anchors_total} "
            f"split={subdecompose_result.anchors_split} "
            f"subfeatures={subdecompose_result.subfeatures_created} "
            f"paths_moved={subdecompose_result.paths_moved} "
            f"iterations={subdecompose_result.iterations} "
            f"depth_cap_hit={subdecompose_result.depth_cap_hit}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="anchor_subdecompose",
            payload=stage_8_9_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.9.5 — LLM-semantic component-blob decomposition ───────
    # Where deterministic 8.9 leaves a residual blob dominated by a
    # ``components/`` subtree (plane ``web``, supabase ``studio``), a cached
    # LLM label per blob splits its PRODUCT-AREA children (``issues`` /
    # ``Auth`` …) into sub-features while UI groupings (``dropdowns`` /
    # ``icons``) stay in the residual — the semantic call the deterministic
    # casing rule provably cannot make. Coverage-preserving (files move into
    # real features, never a sink); ONE cheap call per blob, cached by
    # prompt-hash. Default OFF; FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT=1.
    write_stage_input(run_dir, 8, "llm_component_split", {
        "features": features,
        "ctx": ctx,
        "model_id": model_id,
    })
    with StageLogger(run_dir, 8, "llm_component_split") as log8_9_5:
        # model_id (the scan's RESOLVED model) — the stage's bare "haiku"
        # default only resolves through the subscription proxy; the real
        # Anthropic API 404s on it, silently disabling the split (the
        # supabase wave-4 miss, 2026-07-02).
        llm_split_result = llm_component_split(
            features,
            client=s8_client,
            model=model_id,
            cache_backend=getattr(ctx, "cache_backend", None),
            repo_slug=getattr(ctx, "slug", None) or ctx.repo_path.name,
        )
        stage_8_9_5_telemetry = llm_split_result.as_telemetry()
        log8_9_5.info(
            f"llm_component_split enabled={llm_split_result.enabled} "
            f"candidates={llm_split_result.candidates} "
            f"llm_calls={llm_split_result.llm_calls} "
            f"cache_hits={llm_split_result.cache_hits} "
            f"split={llm_split_result.features_split} "
            f"subfeatures={llm_split_result.subfeatures_created} "
            f"paths_moved={llm_split_result.paths_moved}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="llm_component_split",
            payload=stage_8_9_5_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.9.6 — deterministic domain-dir member attribution ─────
    # Member-only files under a components/hooks container whose domain dir
    # UNIQUELY names an existing dev feature transfer to it (the unowned-
    # ledger blob class 8.9.5 cannot reach — infisical hooks/api/<domain>).
    # $0 LLM, deterministic. Default OFF; FAULTLINE_STAGE_8_9_6_DOMAIN_ATTRIBUTION=1.
    write_stage_input(run_dir, 8, "domain_member_attribution", {
        "features": features,
        "product_features": product_features,
    })
    with StageLogger(run_dir, 8, "domain_member_attribution") as log8_9_6:
        dom_attr_result = attribute_domain_members(features)
        stage_8_9_6_telemetry = dom_attr_result.as_telemetry()
        log8_9_6.info(
            f"domain_member_attribution enabled={dom_attr_result.enabled} "
            f"sources={dom_attr_result.sources_examined} "
            f"transferred={dom_attr_result.files_transferred} "
            f"targets={dom_attr_result.targets_enriched} "
            f"ambiguous_skipped={dom_attr_result.ambiguous_skipped}",
        )
        # A transfer can cross product boundaries (frontend anchor →
        # backend domain feature) and the moved path was never in ANY
        # product union (member-only) — re-union product paths so the
        # Layer-2 surface stays consistent (audit #2, 2026-07-02). The
        # 8.9/8.9.5 splits don't need this (subfeatures inherit
        # product_feature_id → unions byte-stable); only run on transfers.
        if dom_attr_result.files_transferred:
            product_features, s896_reconcile = reconcile_product_features(
                [f for f in features
                 if getattr(f, "layer", "developer") == "developer"],
                product_features,
            )
            stage_8_9_6_telemetry["product_reconcile"] = s896_reconcile
        # ── Stage 8.9.6b — mega-anchor service-domain carve-out ───────
        # Service-domain flow subtrees (backend/services/<domain>/) owned by
        # a shared workspace/infra anchor are lifted into their own dev
        # feature so 6.7d maps them to a real capability and their journey
        # resettles off the shared bucket (validator I10). Rides the 8.9.6
        # flag; deterministic, $0 LLM.
        carve_result = carve_service_domains(features)
        # Only stamp the carve telemetry when the stage is ENABLED — when off
        # (the deterministic snapshot-gate env) the 8.9.6 artifact stays
        # byte-identical, so pinned digests never drift on a no-op carve.
        if carve_result.enabled:
            stage_8_9_6_telemetry["service_carve"] = carve_result.as_telemetry()
            log8_9_6.info(
                f"service_carve enabled={carve_result.enabled} "
                f"anchors_carved={carve_result.anchors_carved} "
                f"domains_carved={carve_result.domains_carved} "
                f"flows_moved={carve_result.flows_moved} "
                f"files_claimed={carve_result.files_claimed}",
            )
        if carve_result.anchors_carved:
            # Carve mints new devs (inheriting the anchor's product_feature_id)
            # and moves owned files onto them — re-union product paths so the
            # Layer-2 surface stays consistent (mirrors the domain-attribution
            # reconcile above). 6.7d, when enabled, re-maps them regardless.
            product_features, s896b_reconcile = reconcile_product_features(
                [f for f in features
                 if getattr(f, "layer", "developer") == "developer"],
                product_features,
            )
            stage_8_9_6_telemetry["service_carve_reconcile"] = s896b_reconcile
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="domain_member_attribution",
            payload=stage_8_9_6_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.9.7 — per-vendor connector split (default ON since
    # Product-Spine Wave 1; opt-out FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT=0) ──
    # Integration-hub dev features (majority of owned files stem-named after
    # DISTINCT public vendors — or, §4.4, dir-per-vendor children under a
    # detected hub dir) split into <parent>-<vendor> sub-features — the
    # per-connector grain users think in (dub golden: Connect Stripe /
    # Connect Shopify). Subfeatures inherit product_feature_id → product
    # unions conserved, no reconcile needed. $0 LLM, deterministic.
    from faultline.pipeline_v2.hub_relation import (
        apply_hub_pf_binding,
        detect_hub_relations,
    )

    write_stage_input(run_dir, 8, "vendor_connector_split", {
        "features": features,
        "product_features": product_features,
    })
    with StageLogger(run_dir, 8, "vendor_connector_split") as log8_9_7:
        # W1.1: detect member-less hubs too — a vendor hub whose files ride
        # inside one covering aggregate (midday rest/routers/apps inside the
        # apps/api workspace anchor) has no per-child grain yet; the carve
        # arm creates it so the 8.9.8 binding below finds members.
        hub_relations = detect_hub_relations(features, include_memberless=True)
        vendor_split_result = split_vendor_connectors(
            features,
            hub_dirs=tuple(
                h.hub_dir for h in hub_relations if h.member_dev_names
            ),
            carve_hub_dirs=tuple(
                h.hub_dir for h in hub_relations if not h.member_dev_names
            ),
            # D4 keyed husk floor (debt-pack): flowless sub-floor vendor
            # groups fold into the parent instead of minting shell twins.
            repo_root=ctx.repo_path,
        )
        stage_8_9_7_telemetry = vendor_split_result.as_telemetry()
        log8_9_7.info(
            f"vendor_connector_split enabled={vendor_split_result.enabled} "
            f"examined={vendor_split_result.features_examined} "
            f"hubs_split={vendor_split_result.hubs_split} "
            f"connectors={vendor_split_result.connectors_created} "
            f"files_moved={vendor_split_result.files_moved} "
            f"collisions_skipped={vendor_split_result.collisions_skipped} "
            f"aggregate_carves={vendor_split_result.aggregate_carves} "
            f"carve_connectors={vendor_split_result.carve_connectors_created}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="vendor_connector_split",
            payload=stage_8_9_7_telemetry,
            run_dir=run_dir,
        )

    # ── Stage 8.9.8 — hub/child PF binding (Product-Spine §4.4) ────────
    # Construction rule: every member of a connector hub (the hub dev +
    # its per-vendor children, incl. the sub-features minted above) lands
    # on ONE product feature — the majority non-shared PF among them, else
    # a deterministically minted parent-capability PF. Pulls hubs out of
    # Shared Platform; sibling parity holds by construction (children of
    # one hub never split between shared and a PF). $0 LLM, deterministic.
    # Kill-switch: FAULTLINE_SPINE_HUBS=0.
    with StageLogger(run_dir, 8, "hub_pf_binding") as log8_9_8:
        # Re-detect over the POST-split feature list so the minted
        # <parent>-<vendor> children participate as members.
        hub_binding_telemetry = apply_hub_pf_binding(
            features, product_features, dev_to_product_map,
        )
        stage_8_9_7_telemetry["hub_binding"] = hub_binding_telemetry
        log8_9_8.info(
            f"hub_pf_binding enabled={hub_binding_telemetry['enabled']} "
            f"hubs={hub_binding_telemetry['hubs']} "
            f"devs_rebound={hub_binding_telemetry['devs_rebound']} "
            f"pfs_minted={hub_binding_telemetry['pfs_minted']}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="hub_pf_binding",
            payload=hub_binding_telemetry,
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
        stage_8_6_5_telemetry=stage_8_6_5_telemetry,
        stage_8_6_7_telemetry=stage_8_6_7_telemetry,
        stage_8_7_telemetry=stage_8_7_telemetry,
        stage_8_8_telemetry=stage_8_8_telemetry,
        stage_8_9_telemetry=stage_8_9_telemetry,
        stage_8_9_5_telemetry=stage_8_9_5_telemetry,
        stage_8_9_6_telemetry=stage_8_9_6_telemetry,
        stage_8_9_7_telemetry=stage_8_9_7_telemetry,
    )


__all__ = [
    "Layer2Result",
    "run_layer2_phase",
]
