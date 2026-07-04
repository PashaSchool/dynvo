"""Stage registry for replay v2 — one runner per pipeline_v2 stage.

Each :class:`StageSpec` names a stage (matching the ``NN-stage-<name>``
artifact convention), declares the pipeline order, and provides a
``run(env, state)`` callable that executes the CURRENT code for that
stage against a named-state input dict (the decoded
``NN-stage-<name>-input.json``) and returns the named-state keys the
stage produced/updated. The runner also writes the stage's OUTPUT
artifact into the replay run dir with the SAME payload shape the live
orchestrator writes — that is what the identity ship-gate compares.

The runners deliberately REPLICATE the orchestration snippets from
``run.py`` / ``phase_*.py`` (same stage functions, same artifact
payloads) instead of refactoring those modules: the identity-replay
gate on the pinned corpus pins the two copies together — if the
orchestrator's wiring changes and the registry is not updated, the
identity test diverges loudly.

Service objects (CostTracker, LlmHealth, StageLogger, LLM clients,
cache backends, framework profile) are FRESH per replay — see
:class:`ReplayEnv`. LLM-bearing stages therefore replay against the
content-keyed llm-cache by default: identical inputs hit the warm
cache and reproduce the recorded outputs at $0.

No stage function is modified. No prompts are touched.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_7_output import write_stage_artifact

logger = logging.getLogger(__name__)

__all__ = ["ReplayEnv", "StageSpec", "STAGES", "stage_by_key", "pipeline_slice"]


# ── Replay environment ──────────────────────────────────────────────────


@dataclass
class ReplayEnv:
    """Fresh service objects + the NEW run dir for one replay run."""

    run_dir: Path
    run_id: str
    tracker: CostTracker = field(default_factory=lambda: CostTracker(max_cost=None))
    llm_health: LlmHealth = field(default_factory=LlmHealth)

    def cache_backend(self) -> Any:
        from faultline.cache import get_cache_backend
        try:
            return get_cache_backend()
        except Exception:  # noqa: BLE001 — cache is best-effort
            return None

    def profile(self, ctx: Any) -> Any:
        # Phase B+ — replay uses the same per-unit-aware selection as
        # the live pipeline so replayed stages see the same profile.
        from faultline.pipeline_v2.profiles import select_scan_profile
        return select_scan_profile(ctx)


def prepare_ctx(ctx: Any, env: ReplayEnv) -> Any:
    """Point a decoded ScanContext at the replay run (dir/id/cache)."""
    ctx.run_id = env.run_id
    ctx.run_dir = env.run_dir
    ctx.cache_backend = env.cache_backend()
    return ctx


def relink_bipartite(flows: list[Any], features: list[Any]) -> list[Any]:
    """Restore flow object identity between ``bipartite_flows`` and the
    flows contained in ``features`` (lost across serialization).

    Matched by ``Flow.id`` (minted content-derived in Stage 5.5);
    unmatched entries keep their decoded standalone instance.
    """
    by_id: dict[str, Any] = {}
    for feat in features:
        for fl in getattr(feat, "flows", None) or []:
            fid = getattr(fl, "id", None)
            if fid:
                by_id.setdefault(fid, fl)
    return [by_id.get(getattr(fl, "id", None), fl) for fl in flows]


# ── Spec ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StageSpec:
    key: str                    # canonical stage name (artifact stage name)
    index: int                  # NN artifact prefix
    order: int                  # global pipeline position
    run: Callable[[ReplayEnv, dict[str, Any]], dict[str, Any]]
    # presence-gated stages (env-gated in the pipeline): when the source
    # run has no input artifact for them, the chain skips them silently.
    optional: bool = False
    # connector steps have no output artifact in a live run (input-only
    # capture); they are excluded from identity comparison.
    connector: bool = False
    # LLM-bearing stages map to an llm-cache subdir for --fresh-llm.
    llm_cache_dir: str | None = None


# ── Runners ─────────────────────────────────────────────────────────────


def _run_intake(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_0_intake import stage_0_intake

    ctx = stage_0_intake(
        Path(state["repo_path"]),
        days=state["days"],
        run_id=env.run_id,
        subpath=state.get("subpath"),
    )
    ctx = prepare_ctx(ctx, env)
    with StageLogger(env.run_dir, 0, "intake") as log0:
        log0.info(
            f"intake: stack={ctx.stack} monorepo={ctx.monorepo} "
            f"workspace_manager={ctx.workspace_manager} "
            f"tracked_files={len(ctx.tracked_files)} "
            f"commits={len(ctx.commits)} run_id={ctx.run_id}",
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=0, stage_name="intake",
            payload={
                "run_id": ctx.run_id,
                "stack": ctx.stack,
                "monorepo": ctx.monorepo,
                "workspace_manager": ctx.workspace_manager,
                "stack_signals": ctx.stack_signals,
                "tracked_files_count": len(ctx.tracked_files),
                "commits_count": len(ctx.commits),
                "workspaces": [
                    {"name": w.name, "path": w.path, "stack": w.stack}
                    for w in (ctx.workspaces or [])
                ],
            },
            run_dir=env.run_dir,
        )
    return {"ctx": ctx}


def _run_auditor(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stack_auditor import (
        MIN_CONFIDENCE_TO_APPLY,
        run_stack_auditor,
    )

    ctx = prepare_ctx(state["ctx"], env)
    model_id = state["model_id"]
    with StageLogger(env.run_dir, 0, "auditor") as log_aud:
        verdict = run_stack_auditor(
            copy.deepcopy(ctx),
            model=model_id,
            cost_tracker=env.tracker,
            log=log_aud,
            llm_health=env.llm_health,
            cache=env.cache_backend(),
        )
        if verdict.confidence >= MIN_CONFIDENCE_TO_APPLY:
            ctx = ctx.with_audited_stack(
                audited_stack=verdict.primary_stack,
                secondary_stacks=verdict.secondary_stacks,
                extractor_hints=verdict.extractor_hints,
                auditor_confidence=verdict.confidence,
            )
        else:
            log_aud.warn(
                f"auditor_low_confidence: {verdict.confidence:.2f} — "
                f"falling back to Stage 0 heuristic stack={ctx.stack}",
            )
        write_stage_artifact(
            ctx.repo_path, stage_index=0, stage_name="auditor",
            payload={
                "primary_stack": verdict.primary_stack,
                "secondary_stacks": list(verdict.secondary_stacks),
                "confidence": verdict.confidence,
                "extractor_hints": list(verdict.extractor_hints),
                "reasoning": verdict.reasoning,
                "cost_usd": verdict.cost_usd,
                "fallback_used": verdict.fallback_used,
                "applied": verdict.confidence >= MIN_CONFIDENCE_TO_APPLY,
                "stage_0_stack": ctx.stack,
                "corrections": list(verdict.corrections),
            },
            run_dir=env.run_dir,
        )
    return {"ctx": ctx}


def _run_shape(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_0_6_shape import classify_repo_shape

    ctx = prepare_ctx(state["ctx"], env)
    shape_result = classify_repo_shape(ctx)  # writes 06-stage-shape.json
    ctx = ctx.with_shape(shape_result)
    with StageLogger(env.run_dir, 6, "shape") as log_shape:
        log_shape.info(
            f"shape={shape_result.shape} "
            f"confidence={shape_result.confidence:.2f} "
            f"matched_signals={list(shape_result.matched_signals)}",
        )
    return {"ctx": ctx}


def _run_repo_class(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_0_7_repo_class import (
        classify_repo_class_per_unit,
        should_suppress_user_flows,
        write_repo_class_artifact,
    )

    ctx = prepare_ctx(state["ctx"], env)
    verdict = classify_repo_class_per_unit(ctx)
    write_repo_class_artifact(ctx, verdict)  # writes 06-stage-repo_class.json
    with StageLogger(env.run_dir, 6, "repo_class") as log_rc:
        log_rc.info(
            f"repo_class={verdict.repo_class} "
            f"confidence={verdict.confidence:.2f} "
            f"uf_suppression={should_suppress_user_flows(verdict)} "
            f"matched_signals={list(verdict.matched_signals)}",
        )
    return {"ctx": ctx}


def _run_extractors(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2 import run as _run
    from faultline.pipeline_v2.phase_extract import run_extract_phase

    ctx = prepare_ctx(state["ctx"], env)
    # phase_extract is already a pure function of (ctx, run_dir) — reuse
    # it wholesale (it re-captures the stage input in the replay dir,
    # which is exactly the mirror-capture the chain wants).
    del _run  # imported for parity with the live path; not needed here
    extract = run_extract_phase(ctx, env.run_dir)
    return {"stage1_out": extract.stage1_out}


def _run_reconcile(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile

    ctx = prepare_ctx(state["ctx"], env)
    stage1_out = state["stage1_out"]
    llm_reconcile = bool(state.get("llm_reconcile", False))
    profile = env.profile(ctx)
    with StageLogger(env.run_dir, 2, "reconcile") as log2:
        stage2 = stage_2_reconcile(
            copy.deepcopy(stage1_out), copy.deepcopy(ctx),
            llm_reconcile=llm_reconcile,
            llm_health=env.llm_health,
            profile=profile,
        )
        deterministic_features = stage2.features
        unattributed = stage2.unattributed
        for f in deterministic_features:
            log2.emit(
                f.name,
                f"reconciled from {len(f.paths)} paths "
                f"(confidence={f.confidence}, sources={','.join(f.sources)})",
            )
        log2.info(f"unattributed: {len(unattributed)} paths")
        write_stage_artifact(
            ctx.repo_path, stage_index=2, stage_name="reconcile",
            payload={
                "feature_count": len(deterministic_features),
                "unattributed_count": len(unattributed),
                "features": [
                    {
                        "name": f.name,
                        "paths": len(f.paths),
                        "confidence": f.confidence,
                        "sources": f.sources,
                    }
                    for f in deterministic_features
                ],
                "notes": stage2.notes,
                "zero_path_drops_count": stage2.zero_path_drops_count,
                "zero_path_drops_sample": stage2.zero_path_drops_sample,
                "schema_only_suppressed_count": stage2.schema_only_suppressed_count,
                "schema_only_suppressed_sample": stage2.schema_only_suppressed_sample,
            },
            run_dir=env.run_dir,
        )
    return {
        "deterministic_features": deterministic_features,
        "unattributed": unattributed,
    }


def _run_membership_closure(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_2_6_membership_closure import (
        run_membership_closure,
    )

    ctx = prepare_ctx(state["ctx"], env)
    profile = env.profile(ctx)
    with StageLogger(env.run_dir, 2, "membership_closure") as log2_6:
        closure = run_membership_closure(
            copy.deepcopy(state["deterministic_features"]),
            copy.deepcopy(state["unattributed"]),
            copy.deepcopy(ctx), log=log2_6,
            extractor_signals=copy.deepcopy(state["stage1_out"]),
            profile=profile,
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=2, stage_name="membership_closure",
            payload=closure.telemetry.as_dict(),
            run_dir=env.run_dir,
        )
    return {
        "deterministic_features": closure.features,
        "unattributed": closure.unattributed,
    }


def _run_flows(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_3_flows import stage_3_flows

    ctx = prepare_ctx(state["ctx"], env)
    profile = env.profile(ctx)
    with StageLogger(env.run_dir, 3, "flows") as log3:
        stage3 = stage_3_flows(
            copy.deepcopy(state["deterministic_features"]),
            copy.deepcopy(ctx),
            model=state["model_id"], cost_tracker=env.tracker,
            llm_health=env.llm_health,
            profile=profile,
        )
        for fwf in stage3.features_with_flows:
            log3.emit(fwf.feature.name, f"{len(fwf.flows)} flows detected")
        for w in stage3.warnings:
            log3.warn(w)
        write_stage_artifact(
            ctx.repo_path, stage_index=3, stage_name="flows",
            payload={
                "feature_count": len(stage3.features_with_flows),
                "total_flows": sum(
                    len(fwf.flows) for fwf in stage3.features_with_flows
                ),
                "cost_usd": stage3.cost_usd,
                "llm_calls": stage3.llm_calls,
                "cache_hits": stage3.cache_hits,
                "chunk_telemetry": stage3.chunk_telemetry,
                "warnings": stage3.warnings,
                "reach_telemetry": stage3.reach_telemetry,
            },
            run_dir=env.run_dir,
        )
    return {"stage3_features_with_flows": stage3.features_with_flows}


def _run_residual(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_4_residual import stage_4_residual

    ctx = prepare_ctx(state["ctx"], env)
    with StageLogger(env.run_dir, 4, "residual") as log4:
        stage4 = stage_4_residual(
            copy.deepcopy(state["unattributed"]), copy.deepcopy(ctx),
            copy.deepcopy(state["deterministic_features"]),
            model=state["model_id"], cost_tracker=env.tracker, log=log4,
            llm_health=env.llm_health,
        )
        residual_features = stage4.residual_features
        for f in residual_features:
            log4.emit(f.name, f"residual cluster from {len(f.paths)} paths")
        for name in stage4.rejected_names:
            log4.drop(name, "rejected by naming-discipline filter")
        for w in stage4.warnings:
            log4.warn(w)
        write_stage_artifact(
            ctx.repo_path, stage_index=4, stage_name="residual",
            payload={
                "residual_feature_count": len(residual_features),
                "cost_usd": stage4.cost_usd,
                "llm_calls": stage4.llm_calls,
                "cache_hits": stage4.cache_hits,
                "warnings": stage4.warnings,
                "clusters_total": stage4.clusters_total,
                "clusters_processed": stage4.clusters_processed,
                "saturation_stopped": stage4.saturation_stopped,
                "rejected_names": stage4.rejected_names,
                "singletons_synthesized": stage4.singletons_synthesized,
                "singletons_skipped": stage4.singletons_skipped,
                "cost_cap_hit": stage4.cost_cap_hit,
                "guard_singletons_dropped": stage4.guard_singletons_dropped,
                "guard_incoherent_clusters_split":
                    stage4.guard_incoherent_clusters_split,
                "guard_drops_sample": stage4.guard_drops_sample,
                "guard_noise_path_drops": stage4.guard_noise_path_drops,
            },
            run_dir=env.run_dir,
        )
    return {"residual_features": residual_features}


def _run_postprocess(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.incremental_wiring import splice_untouched_features
    from faultline.pipeline_v2.stage_5_postprocess import (
        stage_5_from_stage3_result_with_telemetry,
    )

    ctx = prepare_ctx(state["ctx"], env)
    is_full_scan = bool(state.get("is_full_scan", True))
    incremental_untouched = state.get("incremental_untouched") or []
    with StageLogger(env.run_dir, 5, "postprocess") as log5:
        stage5_result = stage_5_from_stage3_result_with_telemetry(
            deterministic=copy.deepcopy(state["deterministic_features"]),
            stage3_features_with_flows=copy.deepcopy(
                state["stage3_features_with_flows"],
            ),
            residual=copy.deepcopy(state["residual_features"]),
            ctx=copy.deepcopy(ctx),
        )
        features = stage5_result.features
        validation_drops = stage5_result.validation_drops
        for name, reason in stage5_result.drop_log:
            log5.drop(name, reason)
        if not is_full_scan and incremental_untouched:
            splice_untouched_features(features, incremental_untouched)
        for feat in features:
            log5.emit(feat.name, "survived naming discipline")
        write_stage_artifact(
            ctx.repo_path, stage_index=5, stage_name="postprocess",
            payload={
                "feature_count": len(features),
                "feature_names": [f.name for f in features],
                "validation_drops": validation_drops.as_dict(),
                "dedup_merges": [m.as_dict() for m in stage5_result.dedup_merges],
            },
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_sibling_collapse(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_5_3_sibling_collapse import (
        collapse_sibling_routes,
    )

    features = state["features"]
    confidence_by_name: dict[str, str] = {}
    sources_by_name: dict[str, list[str]] = {}
    for f in state["deterministic_features"]:
        confidence_by_name[f.name] = f.confidence
        sources_by_name[f.name] = list(f.sources)
    for f in state["residual_features"]:
        confidence_by_name.setdefault(f.name, "low")
        sources_by_name.setdefault(f.name, [])
    with StageLogger(env.run_dir, 5, "sibling_collapse") as log5_3:
        s53 = collapse_sibling_routes(
            features,
            confidence_by_name=confidence_by_name,
            sources_by_name=sources_by_name,
            log=log5_3,
        )
        s53_features_pre = len(features)
        features = s53.features
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=5,
            stage_name="sibling_collapse",
            payload={
                "collapse_groups_count": len(s53.collapse_groups),
                "features_collapsed": s53.features_collapsed,
                "features_pre": s53_features_pre,
                "features_post": len(features),
                "collapse_sample": [g.as_dict() for g in s53.collapse_groups[:5]],
            },
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_cross_flow_dedup(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_5_4_cross_flow_dedup import (
        dedup_cross_feature_flows,
    )

    features = state["features"]
    with StageLogger(env.run_dir, 5, "cross_flow_dedup") as log5_4:
        s54 = dedup_cross_feature_flows(features)
        log5_4.info(
            f"cross_flow_dedup enabled={s54.enabled} "
            f"entry_groups={s54.entry_groups} removed={s54.flows_removed}",
        )
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=5,
            stage_name="cross_flow_dedup",
            payload=s54.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_bipartite(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_5_5_bipartite import stage_5_5_bipartite

    features = state["features"]
    with StageLogger(env.run_dir, 5, "bipartite") as log5_5:
        bipartite = stage_5_5_bipartite(features, log=log5_5)
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=5,
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
            run_dir=env.run_dir,
        )
    return {
        "features": bipartite.features,
        "bipartite_flows": list(bipartite.flows),
        "bipartite_edges": list(bipartite.edges),
    }


def _run_metrics(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_metrics import stage_6_metrics

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    with StageLogger(env.run_dir, 6, "metrics") as log6:
        features = stage_6_metrics(features, copy.deepcopy(ctx))
        with_commits = sum(1 for f in features if f.total_commits > 0)
        with_coverage = sum(1 for f in features if f.coverage_pct is not None)
        log6.info(
            f"enriched: with_commits={with_commits} "
            f"with_coverage={with_coverage} of {len(features)}",
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="metrics",
            payload={
                "feature_count": len(features),
                "with_commits": with_commits,
                "with_coverage": with_coverage,
            },
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_product_clusterer(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_5_product_clusterer import (
        run_product_clusterer,
    )

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    with StageLogger(env.run_dir, 6, "product_clusterer") as log6_5:
        product_features, dev_to_product_map, product_telemetry = (
            run_product_clusterer(copy.deepcopy(ctx), features, log=log6_5)
        )
        for feat in features:
            labels = dev_to_product_map.get(feat.name)
            if labels:
                feat.product_feature_id = labels[0]
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="product_clusterer",
            payload={
                "product_features": [
                    {
                        "name": pf.name,
                        "developer_feature_count": len(pf.paths),
                        "paths_total": len(pf.paths),
                        "health_score": pf.health_score,
                    }
                    for pf in product_features
                ],
                "telemetry": product_telemetry,
                "dev_to_product_map": {
                    k: list(v) for k, v in dev_to_product_map.items()
                },
            },
            run_dir=env.run_dir,
        )
    return {
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
        "product_telemetry": product_telemetry,
    }


def _run_import_tree(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        DEFAULT_MAX_FILES_PER_FEATURE as _MAX_FILES,
        DEFAULT_MAX_SYMBOLS_PER_FEATURE as _MAX_SYMBOLS,
        build_artifact_payload,
        enrich_with_import_tree,
    )

    ctx = prepare_ctx(state["ctx"], env)
    depth = int(state["effective_max_tree_depth"])
    with StageLogger(env.run_dir, 6, "import_tree") as log6_3:
        enrichment = enrich_with_import_tree(
            ctx, state["features"], log=log6_3,
            max_depth=depth,
            max_files_per_feature=_MAX_FILES,
            max_symbols_per_feature=_MAX_SYMBOLS,
        )
        features = list(enrichment.enriched_features)
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="import_tree",
            payload=build_artifact_payload(
                enrichment,
                max_depth=depth,
                max_files_per_feature=_MAX_FILES,
                max_symbols_per_feature=_MAX_SYMBOLS,
            ),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_framework_enrich(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_4_framework_enrich import run_stage_6_4

    ctx = prepare_ctx(state["ctx"], env)
    with StageLogger(env.run_dir, 6, "framework_enrich") as log6_4:
        enrich_result = run_stage_6_4(ctx, state["features"], log6_4)
        features = list(enrich_result.enriched_features)
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="framework_enrich",
            payload=enrich_result.telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_branch_slicer(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_6_branch_slicer import run_stage_6_6

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    with StageLogger(env.run_dir, 6, "branch_slicer") as log6_6:
        branch_result = run_stage_6_6(ctx, features, log6_6)
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="branch_slicer",
            payload=branch_result.telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_marketing_clusterer(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_analyst import (
        DEFAULT_ANALYST_MODEL as _ANALYST_MODEL,
        run_stage_8_analyst,
    )
    from faultline.pipeline_v2.stage_8_marketing_clusterer import (
        _default_client_factory,
        run_stage_8,
    )

    if state.get("incremental_layer2_noop"):
        raise NotImplementedError(
            "replay of an incremental (--since) Layer-2 no-op run is not "
            "supported — replay full/cold scans",
        )
    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    product_features = state["product_features"]
    dev_to_product_map = state["dev_to_product_map"]
    product_telemetry = state["product_telemetry"]
    with StageLogger(env.run_dir, 8, "marketing_clusterer") as log8:
        s8_client = _default_client_factory()
        s8_pre_breakdown = product_telemetry.get(
            "product_clusterer_source_breakdown", {},
        )
        s8_nav_map = product_telemetry.get("nav_taxonomy_map", {}) or {}
        s8_mode = os.environ.get(
            "FAULTLINE_STAGE_8_MODE", "analyst",
        ).strip().lower() or "analyst"
        if s8_mode == "analyst":
            log8.info(f"mode=analyst model={_ANALYST_MODEL}")
            stage_8_result = run_stage_8_analyst(
                ctx, features, product_features,
                dev_to_product_map_pre=dev_to_product_map,
                source_breakdown_pre=s8_pre_breakdown,
                top_flows=list(state["bipartite_flows"]),
                log=log8,
                client=s8_client,
                model=_ANALYST_MODEL,
                cost_tracker=env.tracker,
                llm_health=env.llm_health,
                nav_taxonomy_map=s8_nav_map,
            )
        else:
            log8.info(f"mode=haiku-clusterer model={state['model_id']}")
            stage_8_result = run_stage_8(
                ctx, features, product_features,
                dev_to_product_map_pre=dev_to_product_map,
                source_breakdown_pre=s8_pre_breakdown,
                log=log8,
                client=s8_client,
                model=state["model_id"],
                cost_tracker=env.tracker,
                llm_health=env.llm_health,
                nav_taxonomy_map=s8_nav_map,
            )
        product_features = stage_8_result.product_features
        dev_to_product_map = stage_8_result.dev_to_product_map
        for feat in features:
            labels = dev_to_product_map.get(feat.name)
            feat.product_feature_id = labels[0] if labels else None
        write_stage_artifact(
            ctx.repo_path, stage_index=8, stage_name="marketing_clusterer",
            payload={
                "telemetry": stage_8_result.telemetry,
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
            run_dir=env.run_dir,
        )
    member_flows_map = getattr(stage_8_result, "member_flows_map", {}) or {}
    return {
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
        "member_flows_map": member_flows_map,
    }


def _run_rollup(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_rollup_strategies import (
        stage_8_rollup_flows,
        write_rollup_artifact,
    )

    ctx = prepare_ctx(state["ctx"], env)
    product_features = state["product_features"]
    with StageLogger(env.run_dir, 8, "rollup") as log8_rollup:
        rollup_result = stage_8_rollup_flows(
            product_features,
            list(state["bipartite_flows"]),
            ctx,
            sonnet_member_flows_map=(state.get("member_flows_map") or None),
        )
        write_rollup_artifact(ctx, product_features, rollup_result)
        log8_rollup.info(
            f"rollup strategy={rollup_result.strategy_used} "
            f"pfs_attributed={rollup_result.pfs_attributed_count}/"
            f"{len(product_features)} "
            f"total_attachments={rollup_result.total_attachments} "
            f"unattributed_flows={len(rollup_result.unattributed_flows)}",
        )
    return {"product_features": product_features}


def _run_member_backfill(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_5_member_backfill import (
        run_stage_8_5_backfill,
    )

    features = state["features"]
    with StageLogger(env.run_dir, 8, "member_backfill") as log8_bf:
        backfill_result = run_stage_8_5_backfill(
            features,
            state["product_features"],
            state["dev_to_product_map"],
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
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="member_backfill",
            payload={
                "telemetry": backfill_result.as_telemetry(),
                "assignments": backfill_result.assignments,
            },
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_nonsource_drop(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
        deown_anchor_scaffold,
        drop_all_nonsource_features,
        drop_phantom_product_features,
        reconcile_product_features,
        strip_nonsource_members,
    )

    features = state["features"]
    product_features = state["product_features"]
    dev_to_product_map = state["dev_to_product_map"]
    with StageLogger(env.run_dir, 8, "nonsource_drop") as log8_ns:
        features_before_ns = len(features)
        features, nonsource_dropped = drop_all_nonsource_features(features)
        if nonsource_dropped:
            product_features, pf_recon = reconcile_product_features(
                features, product_features,
            )
        else:
            pf_recon = {"recomputed": 0, "dropped_empty": 0}
        product_features, pf_phantom_dropped = drop_phantom_product_features(
            features, product_features,
        )
        nonsource_strip_result = strip_nonsource_members(features)
        anchor_scaffold_result = deown_anchor_scaffold(features)
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
            f"nonsource_drop features {features_before_ns}->{len(features)}",
        )
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="nonsource_drop",
            payload=stage_8_6_telemetry,
            run_dir=env.run_dir,
        )
    return {
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
    }


def _run_scaffold_filter(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_6_5_scaffold_filter import (
        filter_shared_scaffold,
    )

    features = state["features"]
    with StageLogger(env.run_dir, 8, "scaffold_filter") as log8_65:
        scaffold_result = filter_shared_scaffold(features)
        log8_65.info(f"scaffold_filter enabled={scaffold_result.enabled}")
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="scaffold_filter",
            payload=scaffold_result.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_di_attribution(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_6_7_di_attribution import (
        attribute_di_services,
    )

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    with StageLogger(env.run_dir, 8, "di_attribution") as log8_67:
        di_result = attribute_di_services(ctx, features)
        log8_67.info(f"di_attribution enabled={di_result.enabled}")
        write_stage_artifact(
            ctx.repo_path, stage_index=8, stage_name="di_attribution",
            payload=di_result.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_anchor_desink(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        desink_workspace_anchors,
    )

    features = state["features"]
    product_features = state["product_features"]
    with StageLogger(env.run_dir, 8, "anchor_desink") as log8_7:
        desink_result = desink_workspace_anchors(features, product_features)
        log8_7.info(f"anchor_desink enabled={desink_result.enabled}")
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="anchor_desink",
            payload=desink_result.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features, "product_features": product_features}


def _run_shared_members(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_8_shared_members import (
        enrich_shared_members,
    )

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    with StageLogger(env.run_dir, 8, "shared_members") as log8_8:
        shared_result = enrich_shared_members(ctx, features)
        log8_8.info(f"shared_members enabled={shared_result.enabled}")
        write_stage_artifact(
            ctx.repo_path, stage_index=8, stage_name="shared_members",
            payload=shared_result.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_anchor_subdecompose(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
        subdecompose_workspace_anchors,
    )

    features = state["features"]
    with StageLogger(env.run_dir, 8, "anchor_subdecompose") as log8_9:
        subdecompose_result = subdecompose_workspace_anchors(features)
        log8_9.info(
            f"anchor_subdecompose enabled={subdecompose_result.enabled}",
        )
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="anchor_subdecompose",
            payload=subdecompose_result.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_llm_component_split(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_9_5_llm_component_split import (
        llm_component_split,
    )
    from faultline.pipeline_v2.stage_8_marketing_clusterer import (
        _default_client_factory,
    )

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    with StageLogger(env.run_dir, 8, "llm_component_split") as log8_9_5:
        llm_split_result = llm_component_split(
            features,
            client=_default_client_factory(),
            model=state["model_id"],
            cache_backend=getattr(ctx, "cache_backend", None),
            repo_slug=getattr(ctx, "slug", None) or ctx.repo_path.name,
        )
        log8_9_5.info(
            f"llm_component_split enabled={llm_split_result.enabled}",
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=8, stage_name="llm_component_split",
            payload=llm_split_result.as_telemetry(),
            run_dir=env.run_dir,
        )
    return {"features": features}


def _run_domain_member_attribution(
    env: ReplayEnv, state: dict[str, Any],
) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
        attribute_domain_members,
    )
    from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
        reconcile_product_features,
    )

    features = state["features"]
    product_features = state["product_features"]
    with StageLogger(env.run_dir, 8, "domain_member_attribution") as log8_9_6:
        dom_attr_result = attribute_domain_members(features)
        log8_9_6.info(
            f"domain_member_attribution enabled={dom_attr_result.enabled}",
        )
        stage_8_9_6_telemetry = dom_attr_result.as_telemetry()
        if dom_attr_result.files_transferred:
            product_features, s896_reconcile = reconcile_product_features(
                [f for f in features
                 if getattr(f, "layer", "developer") == "developer"],
                product_features,
            )
            stage_8_9_6_telemetry["product_reconcile"] = s896_reconcile
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="domain_member_attribution",
            payload=stage_8_9_6_telemetry,
            run_dir=env.run_dir,
        )
    return {"features": features, "product_features": product_features}


def _run_pf_hotspots(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_metrics import (
        attach_hotspots_to_product_features,
    )

    ctx = prepare_ctx(state["ctx"], env)
    product_features = state["product_features"]
    try:
        attach_hotspots_to_product_features(product_features, ctx.commits)
    except Exception as exc:  # noqa: BLE001 — mirror the live defensive guard
        logger.warning("replay pf_hotspots: hotspot pass failed: %s", exc)
    return {"product_features": product_features}


def _run_lineage(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.incremental import load_base_scan
    from faultline.pipeline_v2.incremental_wiring import (
        apply_incremental_bookkeeping,
    )
    from faultline.pipeline_v2.lineage import (
        RELATED_THRESHOLD,
    )
    from faultline.pipeline_v2.stage_6_8_lineage import run_stage_6_8
    from faultline.pipeline_v2.system_flows import classify_routes

    features = state["features"]
    flows = state["bipartite_flows"]
    scan_meta = state["scan_meta"]
    since = state.get("since")
    repo_path = Path(state["repo_path"])
    base_scan_path = state.get("base_scan_path")
    base_scan_dict = load_base_scan(base_scan_path) if base_scan_path else None

    lineage_result = run_stage_6_8(
        features,
        list(flows),
        base_scan=base_scan_dict,
        extractor_signals=state["stage1_out"],
        rename_threshold=float(state["lineage_jaccard_threshold"]),
        related_threshold=RELATED_THRESHOLD,
    )
    scan_meta["system_flow_routes"] = classify_routes(
        lineage_result.routes_index, repo_path,
    )
    is_full_scan = since is None
    head, incremental_meta = apply_incremental_bookkeeping(
        repo_path=repo_path,
        since=since,
        is_full_scan=is_full_scan,
        base_scan=base_scan_dict,
        features=features,
    )
    scan_meta["lineage_feature_stats"] = lineage_result.feature_lineage_stats
    scan_meta["lineage_flow_stats"] = lineage_result.flow_lineage_stats
    scan_meta["lineage_rename_threshold"] = float(
        state["lineage_jaccard_threshold"],
    )
    scan_meta["is_full_scan"] = is_full_scan
    scan_meta.update(incremental_meta)
    return {
        "features": features,
        "bipartite_flows": flows,
        "scan_meta": scan_meta,
        "routes_index": lineage_result.routes_index,
        "path_index": lineage_result.path_index,
        "head": head,
        "is_full_scan": is_full_scan,
    }


def _run_flow_expansion(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.flow_expansion import expand_flows

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    flows = state["bipartite_flows"]
    scan_meta = state["scan_meta"]
    with StageLogger(env.run_dir, 3, "flow_expansion") as log3_5:
        fx = expand_flows(
            features,
            ctx,
            routes_index=state["routes_index"],
            max_depth=1,
            log=log3_5,
            top_level_flows=list(flows),
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=3, stage_name="flow_expansion",
            payload=fx.telemetry,
            run_dir=env.run_dir,
        )
    scan_meta["stage_3_5_flow_expansion"] = dict(fx.telemetry)
    return {
        "features": features,
        "bipartite_flows": flows,
        "scan_meta": scan_meta,
    }


def _run_test_strip(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_9_test_strip import (
        stage_6_9_enabled,
        strip_test_paths,
    )

    features = state["features"]
    flows = state["bipartite_flows"]
    scan_meta = state["scan_meta"]
    test_strip_telemetry: dict[str, int] = {
        "paths_removed": 0,
        "features_dropped": 0,
        "flows_dropped": 0,
        "flow_entries_recomputed": 0,
    }
    with StageLogger(env.run_dir, 6, "test_strip") as log6_9:
        if stage_6_9_enabled():
            test_strip_telemetry = strip_test_paths(features, flows)
        else:
            test_strip_telemetry["disabled"] = True
            log6_9.info("test_strip: disabled via FAULTLINE_STAGE_6_9_TEST_STRIP=0")
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=6,
            stage_name="test_strip",
            payload=test_strip_telemetry,
            run_dir=env.run_dir,
        )
    scan_meta["stage_6_9_test_strip"] = dict(test_strip_telemetry)
    return {
        "features": features,
        "bipartite_flows": flows,
        "scan_meta": scan_meta,
    }


def _run_generated_strip(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_9b_generated_strip import (
        stage_6_9b_enabled,
        strip_generated_paths,
    )
    from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
        drop_phantom_product_features,
    )

    features = state["features"]
    flows = state["bipartite_flows"]
    product_features = state["product_features"]
    scan_meta = state["scan_meta"]
    generated_strip_telemetry: dict[str, int] = {
        "paths_removed": 0,
        "features_dropped": 0,
        "flows_dropped": 0,
    }
    with StageLogger(env.run_dir, 6, "generated_strip") as log6_9b:
        if stage_6_9b_enabled():
            generated_strip_telemetry = strip_generated_paths(features, flows)
        else:
            generated_strip_telemetry["disabled"] = True
            log6_9b.info(
                "generated_strip: disabled via "
                "FAULTLINE_STAGE_6_9B_GENERATED_STRIP=0",
            )
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=6,
            stage_name="generated_strip",
            payload=generated_strip_telemetry,
            run_dir=env.run_dir,
        )
    scan_meta["stage_6_9b_generated_strip"] = dict(generated_strip_telemetry)
    test_strip_meta = scan_meta.get("stage_6_9_test_strip") or {}
    if test_strip_meta.get("features_dropped") or generated_strip_telemetry.get(
        "features_dropped"
    ):
        product_features, pf_phantom_post = drop_phantom_product_features(
            features, product_features,
        )
        scan_meta["stage_6_9_test_strip"]["pf_dropped_phantom"] = pf_phantom_post
    return {
        "features": features,
        "bipartite_flows": flows,
        "product_features": product_features,
        "scan_meta": scan_meta,
    }


def _product_strings_for(
    repo_path: Path, features: list[Any], flows: list[Any],
) -> Any:
    """Recompute the deterministic product-string index (not captured —
    derived from the pinned clone + the feature/flow member sets)."""
    from faultline.pipeline_v2.product_strings import collect_product_strings

    ps_candidates: set[str] = set()
    for f in features:
        ps_candidates.update(f.paths or [])
        ps_candidates.update(mf.path for mf in (f.member_files or []))
    for fl in flows:
        ps_candidates.update(fl.paths or [])
        if fl.entry_point_file:
            ps_candidates.add(fl.entry_point_file)
    return collect_product_strings(repo_path, ps_candidates)


def _run_user_flows(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_7_user_flows import run_user_flow_rollup

    features = state["features"]
    flows = state["bipartite_flows"]
    scan_meta = state["scan_meta"]
    product_strings = _product_strings_for(
        Path(state["repo_path"]), features, flows,
    )
    with StageLogger(env.run_dir, 6, "user_flows") as log6_7:
        user_flows, uf_telemetry = run_user_flow_rollup(
            flows, features,
            routes_index=state["routes_index"],
            product_strings=product_strings,
        )
        write_stage_artifact(
            Path(state["repo_path"]), stage_index=6, stage_name="user_flows",
            payload={
                **uf_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
            },
            run_dir=env.run_dir,
        )
        log6_7.info(
            f"user_flows: {uf_telemetry['total_flows']} flows -> "
            f"{uf_telemetry['user_flows']} UF (replay)",
        )
    scan_meta["stage_6_7_user_flows"] = dict(uf_telemetry)
    return {"user_flows": user_flows, "scan_meta": scan_meta}


def _run_uf_splitter(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_7c_uf_splitter import split_mega_user_flows

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = state["scan_meta"]
    with StageLogger(env.run_dir, 6, "uf_splitter") as log6_7c:
        user_flows, uf_split_telemetry = split_mega_user_flows(
            state["user_flows"],
            state["bipartite_flows"],
            cost_tracker=env.tracker,
            log=log6_7c,
            llm_health=env.llm_health,
            cache=getattr(ctx, "cache_backend", None),
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="uf_splitter",
            payload={
                **uf_split_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
            },
            run_dir=env.run_dir,
        )
    scan_meta["stage_6_7c_uf_splitter"] = dict(uf_split_telemetry)
    return {"user_flows": user_flows, "scan_meta": scan_meta}


def _run_uf_refiner(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = state["scan_meta"]
    flows = state["bipartite_flows"]
    product_strings = _product_strings_for(
        Path(state["repo_path"]), state["features"], flows,
    )
    with StageLogger(env.run_dir, 6, "uf_refiner") as log6_7b:
        user_flows, uf_refine_telemetry = refine_user_flows(
            state["user_flows"],
            flows,
            model=state["model_id"],
            cost_tracker=env.tracker,
            log=log6_7b,
            domain_allowlist=None,  # full-scan path only (see runner docs)
            llm_health=env.llm_health,
            product_strings=product_strings,
            cache=getattr(ctx, "cache_backend", None),
        )
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="uf_refiner",
            payload={
                **uf_refine_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
            },
            run_dir=env.run_dir,
        )
    scan_meta["stage_6_7b_uf_refiner"] = dict(uf_refine_telemetry)
    return {"user_flows": user_flows, "scan_meta": scan_meta}


def _run_journey_abstraction(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        align_enabled,
        run_journey_abstraction,
    )

    features = state["features"]
    user_flows = state["user_flows"]
    product_features = state["product_features"]
    scan_meta = state["scan_meta"]
    repo_path = Path(state["repo_path"])
    with StageLogger(env.run_dir, 6, "journey_abstraction") as log6_7d:
        _anchors = None
        _raw_anchors = None
        try:
            if align_enabled():
                from faultline.pipeline_v2.anchor_extractors import (
                    build_alignment_pool, extract_raw_anchors,
                )
                # RAW extraction feeds the grain gate (pool caps understate a
                # fine-grained vocabulary); the curated pool feeds the prompt
                # — mirrors phase_finalize exactly.
                _raw_anchors = extract_raw_anchors(repo_path)
                _anchors = build_alignment_pool(_raw_anchors)
        except Exception:  # noqa: BLE001 — anchors are optional
            _anchors = None
            _raw_anchors = None
        (
            user_flows,
            product_features,
            s67d_dev_map,
            s67d_telemetry,
        ) = run_journey_abstraction(
            user_flows,
            product_features,
            features,
            state["routes_index"],
            product_anchors=_anchors,
            raw_anchors=_raw_anchors,
            model=state["model_id"],
            cost_tracker=env.tracker,
            cache=env.cache_backend(),
            log=log6_7d,
            llm_health=env.llm_health,
        )
        if s67d_dev_map:
            for _dev in features:
                _slugs = s67d_dev_map.get(getattr(_dev, "name", None))
                if _slugs:
                    _dev.product_feature_id = _slugs[0]
        write_stage_artifact(
            repo_path, stage_index=6, stage_name="journey_abstraction",
            payload={
                **s67d_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
                "product_features": [
                    {"name": pf.name, "display_name": pf.display_name,
                     "n_paths": len(pf.paths)}
                    for pf in product_features
                ],
            },
            run_dir=env.run_dir,
        )
    _s67d_meta = dict(s67d_telemetry)
    # Lift structured degradation records (align requested-but-refused) into
    # the canonical scan_meta.degradations[] — mirrors phase_finalize.
    _s67d_degr = _s67d_meta.pop("degradations", None) or []
    if _s67d_degr:
        scan_meta.setdefault("degradations", []).extend(_s67d_degr)
    scan_meta["stage_6_7d_journey_abstraction"] = _s67d_meta
    return {
        "user_flows": user_flows,
        "product_features": product_features,
        "features": features,
        "scan_meta": scan_meta,
    }


def _run_dual_evidence(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.anchor_extractors import (
        build_alignment_pool, extract_raw_anchors,
    )
    from faultline.pipeline_v2.dual_evidence import attach_dual_evidence

    product_features = state["product_features"]
    user_flows = state["user_flows"]
    scan_meta = state["scan_meta"]
    repo_path = Path(state["repo_path"])
    with StageLogger(env.run_dir, 6, "dual_evidence") as log_de:
        try:
            anchors = build_alignment_pool(extract_raw_anchors(repo_path))
            de_stats = attach_dual_evidence(product_features, user_flows, anchors)
            scan_meta["stage_dual_evidence"] = dict(de_stats)
        except Exception as _de_exc:  # noqa: BLE001 — best-effort, never fatal
            log_de.info(f"dual_evidence skipped: {_de_exc}", feature=None)
    return {
        "product_features": product_features,
        "user_flows": user_flows,
        "scan_meta": scan_meta,
    }


def _run_history(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_95_history import stage_6_95_history

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = state["scan_meta"]
    feature_history = bool(state.get("feature_history", True))
    history_telemetry: dict[str, Any] = {"skipped": True}
    with StageLogger(env.run_dir, 6, "history") as log6_95:
        if feature_history:
            history_telemetry = stage_6_95_history(
                state["product_features"],
                state["user_flows"],
                list(state["bipartite_flows"]),
                state["features"],
                ctx.commits,
            )
        else:
            log6_95.info("history: skipped via --no-feature-history")
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="history",
            payload=history_telemetry,
            run_dir=env.run_dir,
        )
    scan_meta["stage_6_95"] = dict(history_telemetry)
    return {
        "product_features": state["product_features"],
        "user_flows": state["user_flows"],
        "features": state["features"],
        "scan_meta": scan_meta,
    }


def _run_impact(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_96_impact import stage_6_96_impact

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = state["scan_meta"]
    feature_history = bool(state.get("feature_history", True))
    impact_telemetry: dict[str, Any] = {"skipped": True}
    with StageLogger(env.run_dir, 6, "impact") as log6_96:
        if feature_history:
            impact_telemetry = stage_6_96_impact(
                state["product_features"],
                state["user_flows"],
                list(state["bipartite_flows"]),
                state["features"],
                ctx.commits,
                Path(state["repo_path"]),
                subpath=getattr(ctx, "subpath", None),
                log=log6_96,
            )
        else:
            log6_96.info("impact: skipped via --no-feature-history")
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="impact",
            payload=impact_telemetry,
            run_dir=env.run_dir,
        )
    scan_meta["stage_6_96_impact"] = dict(impact_telemetry)
    return {
        "product_features": state["product_features"],
        "user_flows": state["user_flows"],
        "scan_meta": scan_meta,
    }


def _run_monorepo_assembly(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.llm_health import stamp_llm_degraded

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = state["scan_meta"]
    features = state["features"]
    # The live pipeline stamps LLM health right before this stage.
    stamp_llm_degraded(scan_meta, env.llm_health)
    monorepo_view: dict[str, Any] = {}
    with StageLogger(env.run_dir, 6, "monorepo_assembly") as log66:
        try:
            from faultline.pipeline_v2.stage_6_6_monorepo_assembly import (
                build_monorepo_assembly,
            )
            monorepo_view = build_monorepo_assembly(ctx, features)
            write_stage_artifact(
                ctx.repo_path, stage_index=6, stage_name="monorepo_assembly",
                payload=monorepo_view,
                run_dir=env.run_dir,
            )
        except Exception as exc:  # noqa: BLE001 — never fail on the view
            log66.info(f"monorepo assembly skipped ({exc})", feature=None)
            monorepo_view = {}
    if monorepo_view:
        scan_meta["monorepo_assembly"] = {
            k: v for k, v in monorepo_view.get("stats", {}).items()
        }
    return {"monorepo_view": monorepo_view, "scan_meta": scan_meta}


def _run_output(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline import __version__ as _engine_version
    from faultline.pipeline_v2.stage_7_output import stage_7_output

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = dict(state["scan_meta"])
    # Spec: replayed runs are stamped with their source run id.
    scan_meta["replayed_from"] = state.get("_replayed_from", "")
    out_path = env.run_dir / "feature-map-replay.json"
    with StageLogger(env.run_dir, 7, "output") as log7:
        out = stage_7_output(
            state["features"], ctx, scan_meta, out_path,
            days=state["days"],
            flows=state["bipartite_flows"],
            feature_flow_edges=state["bipartite_edges"],
            product_features=state["product_features"],
            user_flows=state["user_flows"],
            path_index=state["path_index"],
            routes_index=state["routes_index"],
            is_full_scan=bool(state.get("is_full_scan", True)),
            base_scan_commit=(state.get("since") or ""),
            scan_commit=state.get("head", ""),
            engine_version=_engine_version,
            monorepo=state.get("monorepo_view") or {},
        )
        log7.info(f"wrote feature map to {out}", feature=None)
    return {"out_path": out}


# ── The ordered registry ────────────────────────────────────────────────

STAGES: list[StageSpec] = [
    StageSpec("intake", 0, 0, _run_intake),
    StageSpec("auditor", 0, 1, _run_auditor, llm_cache_dir="auditor"),
    StageSpec("shape", 6, 2, _run_shape),
    StageSpec("repo_class", 6, 3, _run_repo_class),
    StageSpec("extractors", 1, 4, _run_extractors),
    StageSpec("reconcile", 2, 5, _run_reconcile),
    StageSpec("membership_closure", 2, 6, _run_membership_closure),
    StageSpec("flows", 3, 7, _run_flows, llm_cache_dir="flows"),
    StageSpec("residual", 4, 8, _run_residual, llm_cache_dir="residual"),
    StageSpec("postprocess", 5, 9, _run_postprocess),
    StageSpec("sibling_collapse", 5, 10, _run_sibling_collapse),
    StageSpec("cross_flow_dedup", 5, 11, _run_cross_flow_dedup),
    StageSpec("bipartite", 5, 12, _run_bipartite),
    StageSpec("metrics", 6, 13, _run_metrics),
    StageSpec("product_clusterer", 6, 14, _run_product_clusterer),
    StageSpec("import_tree", 6, 15, _run_import_tree),
    StageSpec("framework_enrich", 6, 16, _run_framework_enrich),
    StageSpec("branch_slicer", 6, 17, _run_branch_slicer),
    StageSpec(
        "marketing_clusterer", 8, 18, _run_marketing_clusterer,
        llm_cache_dir="product-cluster",
    ),
    StageSpec("rollup", 8, 19, _run_rollup),
    StageSpec("member_backfill", 8, 20, _run_member_backfill),
    StageSpec("nonsource_drop", 8, 21, _run_nonsource_drop),
    StageSpec("scaffold_filter", 8, 22, _run_scaffold_filter),
    StageSpec("di_attribution", 8, 23, _run_di_attribution),
    StageSpec("anchor_desink", 8, 24, _run_anchor_desink),
    StageSpec("shared_members", 8, 25, _run_shared_members),
    StageSpec("anchor_subdecompose", 8, 26, _run_anchor_subdecompose),
    StageSpec(
        "llm_component_split", 8, 27, _run_llm_component_split,
        llm_cache_dir="llm-component-split",
    ),
    StageSpec("domain_member_attribution", 8, 28, _run_domain_member_attribution),
    StageSpec("pf_hotspots", 8, 29, _run_pf_hotspots, connector=True),
    StageSpec("lineage", 6, 30, _run_lineage, connector=True),
    StageSpec("flow_expansion", 3, 31, _run_flow_expansion),
    StageSpec("test_strip", 6, 32, _run_test_strip),
    StageSpec("generated_strip", 6, 33, _run_generated_strip),
    StageSpec("user_flows", 6, 34, _run_user_flows),
    StageSpec("uf_splitter", 6, 35, _run_uf_splitter, llm_cache_dir="uf-split"),
    StageSpec("uf_refiner", 6, 36, _run_uf_refiner, llm_cache_dir="uf-refine"),
    StageSpec(
        "journey_abstraction", 6, 37, _run_journey_abstraction,
        optional=True, llm_cache_dir="abstraction",
    ),
    StageSpec("dual_evidence", 6, 38, _run_dual_evidence,
              optional=True, connector=True),
    StageSpec("history", 6, 39, _run_history),
    StageSpec("impact", 6, 40, _run_impact),
    StageSpec("monorepo_assembly", 6, 41, _run_monorepo_assembly),
    StageSpec("output", 7, 42, _run_output),
]

_BY_KEY = {s.key: s for s in STAGES}


def stage_by_key(key: str) -> StageSpec:
    """Resolve ``--stage`` input: ``flows`` or ``03-flows`` or
    ``03-stage-flows``."""
    k = key.strip()
    if k in _BY_KEY:
        return _BY_KEY[k]
    # strip a numeric prefix and optional "stage-" infix
    parts = k.split("-", 1)
    if parts[0].isdigit() and len(parts) == 2:
        rest = parts[1]
        rest = rest.removeprefix("stage-")
        if rest in _BY_KEY:
            return _BY_KEY[rest]
    raise KeyError(
        f"unknown stage {key!r}; valid stages: "
        + ", ".join(s.key for s in STAGES),
    )


def pipeline_slice(start: str, through: str | None) -> list[StageSpec]:
    """The ordered chain from ``start`` up to and including ``through``
    (or just ``[start]`` when ``through`` is None)."""
    a = stage_by_key(start)
    if through is None:
        return [a]
    b = stage_by_key(through)
    if b.order < a.order:
        raise ValueError(
            f"--through stage {b.key!r} (order {b.order}) is upstream of "
            f"--stage {a.key!r} (order {a.order})",
        )
    return [s for s in STAGES if a.order <= s.order <= b.order]
