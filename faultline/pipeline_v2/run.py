"""Pipeline v2 orchestrator — wire Stages 0..7 end-to-end.

This module is the only place that knows the FULL sequencing rules:

  - Stage 0 must run before Stage 1 (extractors need ScanContext).
  - Stage 2 must run before Stage 3 (flow detection needs reconciled
    features).
  - Stage 4 must run after Stage 2 (residual is over the unattributed
    set Stage 2 emits).
  - Stage 3 must run BEFORE Stage 4 (we want flows on the deterministic
    features only — LLM-fallback features are inherently low-confidence
    and don't need flow detection).
  - Stage 5 merges deterministic + residual and applies naming
    discipline.
  - Stage 6 enriches with commit + coverage metrics.
  - Stage 7 assembles the FeatureMap and writes it out.

Run isolation (Sprint A0)
=========================

Every scan run is given a unique ``run_id`` and writes all artifacts
+ structured logs under ``~/.faultline/logs/<slug>/<run_id>/``. A
``latest`` symlink in the slug dir is atomically swapped after the
run so diagnostician scripts can resolve
``~/.faultline/logs/<slug>/latest/04-stage-residual.json`` without
knowing the timestamp. Two scans of the same repo never overwrite
each other.

Deep-copy boundary
==================

Between every pair of stages, the orchestrator hands the next stage
a ``copy.deepcopy`` of the upstream payload. Stages MUST NOT mutate
their input — and if they accidentally do, the artifact captured for
the upstream stage stays correct because they only touched a copy.
This catches "stage X silently re-orders Stage 1's output" bugs at
the architectural level.

Structured logging
==================

Each stage owns a :class:`StageLogger` that writes one JSONL record
per drop/emit/cluster/warn decision into
``<run_dir>/NN-stage-<name>.log``. The orchestrator wires the logger
from the outside so individual stages don't grow a new transitive
dependency.

Telemetry
=========

``scan_meta`` is built up incrementally and emitted on the FeatureMap:

  - ``run_id`` (new — directory name under ``~/.faultline/logs/<slug>/``)
  - ``stack`` / ``monorepo`` / ``workspace_manager`` / ``stack_signals``
    (from Stage 0)
  - ``model`` (the Haiku model id used for Stage 3 + Stage 4)
  - ``extractor_hits`` ({extractor_name: candidate_count}) and
    ``extractor_coverage_pct`` (deterministic-feature share of total)
  - ``llm_fallback_pct`` (residual feature share)
  - ``warnings`` (free-form list, includes the >30% fallback nudge)
  - ``elapsed_sec`` / ``cost_usd`` / ``calls`` (cost is Stage 3 + Stage 4)
  - ``stage_artifact_dir`` (the per-RUN dir — not the parent slug dir)
"""

from __future__ import annotations

import copy
import logging
import os
import time
from pathlib import Path
from typing import Any, TypeVar

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.run_dir import update_latest_symlink
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stack_auditor import (
    MIN_CONFIDENCE_TO_APPLY,
    run_stack_auditor,
)
from faultline.pipeline_v2.stage_0_6_shape import classify_repo_shape
from faultline.pipeline_v2.stage_0_intake import stage_0_intake
from faultline.pipeline_v2.stage_1_extractors import stage_1_extractors
from faultline.pipeline_v2.stage_1_per_workspace import (
    run_stage_1_per_workspace,
    should_activate_per_workspace,
)
from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile
from faultline.pipeline_v2.stage_3_flows import stage_3_flows
from faultline.pipeline_v2.stage_4_residual import stage_4_residual
from faultline.pipeline_v2.stage_5_3_sibling_collapse import (
    collapse_sibling_routes,
)
from faultline.pipeline_v2.stage_5_5_bipartite import stage_5_5_bipartite
from faultline.pipeline_v2.stage_5_postprocess import (
    stage_5_from_stage3_result_with_telemetry,
)
from faultline.pipeline_v2.stage_6_metrics import stage_6_metrics
from faultline.pipeline_v2.stage_6_5_product_clusterer import (
    run_product_clusterer,
)
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_DEPTH as _IMPORT_TREE_MAX_DEPTH,
    DEFAULT_MAX_FILES_PER_FEATURE as _IMPORT_TREE_MAX_FILES,
    DEFAULT_MAX_SYMBOLS_PER_FEATURE as _IMPORT_TREE_MAX_SYMBOLS,
    build_artifact_payload as _import_tree_artifact,
    enrich_with_import_tree,
)
from faultline.pipeline_v2.stage_6_4_framework_enrich import (
    run_stage_6_4,
)
from faultline.pipeline_v2.stage_6_6_branch_slicer import (
    run_stage_6_6,
)
from faultline.pipeline_v2.stage_7_output import (
    stage_7_output,
    write_stage_artifact,
)
from faultline.pipeline_v2.stage_8_rollup_strategies import (
    stage_8_rollup_flows,
    write_rollup_artifact,
)
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    _default_client_factory as _stage_8_default_client_factory,
    run_stage_8,
)
from faultline.pipeline_v2.stage_8_analyst import (
    DEFAULT_ANALYST_MODEL as _STAGE_8_ANALYST_MODEL,
    run_stage_8_analyst,
)

logger = logging.getLogger(__name__)


# ── Public model-id aliases ─────────────────────────────────────────────

# Short → fully-qualified mapping. CLI users type ``--model haiku`` and
# the orchestrator resolves to the canonical Anthropic model id.
MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6-20251108",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6": "claude-sonnet-4-6-20251108",
}

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Sprint A1: the warn threshold is HALF, not the old 30% cap. We do
# NOT truncate when this trips — we just nudge "you may want an
# extractor for stack X". Quality is enforced downstream in Stage 5.
LLM_FALLBACK_WARN_THRESHOLD = 0.50


def resolve_model(name: str) -> str:
    """Resolve a short or fully-qualified model name to its canonical id."""
    if not name:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(name, name)


# ── Deep-copy boundary helper ───────────────────────────────────────────

T = TypeVar("T")


def _isolate(payload: T) -> T:
    """Return a ``copy.deepcopy`` of ``payload``.

    The orchestrator wraps every stage hand-off in this so each
    stage receives an independent copy of its input. Stages that
    mutate their input therefore can't corrupt either the upstream
    artifact (already captured) or the orchestrator's own references.

    Centralised here so we have ONE call site to instrument later
    (e.g. swap to a structural hash check during testing).
    """
    return copy.deepcopy(payload)


# ── Helper — flatten extractor results into hits dict ───────────────────


def _extractor_hits(stage1_out: dict[str, Any]) -> dict[str, int]:
    """``{extractor_name: candidate_count}`` for ``scan_meta.extractor_hits``.

    Drops the sentinel ``_errors`` key.
    """
    return {
        name: len(cands)
        for name, cands in stage1_out.items()
        if name != "_errors"
    }


def _workspace_anchor_telemetry(
    ctx: Any,
    stage1_out: dict[str, Any],
) -> dict[str, Any]:
    """Count how many declared workspaces produced a package-source anchor.

    Sprint D3 — surfaces silent drops of workspace packages so we can
    diagnose ``Stage 1 skipped my package`` regressions without
    re-reading the per-workspace report.

    Returns a dict with:
      - ``workspace_packages_detected`` — workspaces declared in ctx.
      - ``workspace_packages_anchored`` — workspaces whose slug appears
        in a package-source anchor.
      - ``workspace_packages_dropped`` — declared but no anchor.
      - ``drop_reasons`` — list of ``{name, path, reason}`` for the
        dropped workspaces (helps repo-owners audit silent skips).
    """
    workspaces = list(getattr(ctx, "workspaces", None) or [])
    detected = len(workspaces)
    if detected == 0:
        return {
            "workspace_packages_detected": 0,
            "workspace_packages_anchored": 0,
            "workspace_packages_dropped": 0,
            "drop_reasons": [],
        }

    package_anchors = stage1_out.get("package") or []
    anchored_slugs = {a.name for a in package_anchors if hasattr(a, "name")}

    # Mirror the workspace-slug derivation used by the package
    # extractor (single source of truth — both call the same helper).
    from faultline.pipeline_v2.extractors.package import _workspace_slug

    anchored = 0
    drop_reasons: list[dict[str, str]] = []
    for ws in workspaces:
        slug = _workspace_slug(ws)
        if slug and slug in anchored_slugs:
            anchored += 1
            continue
        # Diagnose: empty slug, or anchor was eaten by another
        # extractor's higher priority (very rare — the anchor stays
        # in stage1_out["package"]; only Stage 2 reconciliation
        # would later merge it).
        reason = (
            "empty slug (no package.json#name and no workspace path)"
            if not slug
            else f"slug {slug!r} not present in package-source anchors"
        )
        drop_reasons.append(
            {"name": ws.name, "path": ws.path, "reason": reason},
        )

    return {
        "workspace_packages_detected": detected,
        "workspace_packages_anchored": anchored,
        "workspace_packages_dropped": len(drop_reasons),
        "drop_reasons": drop_reasons[:20],
    }


# ── Public entry point ──────────────────────────────────────────────────


def run_pipeline_v2(
    repo_path: Path | str,
    *,
    model: str = DEFAULT_MODEL,
    days: int = 365,
    out_path: Path | None = None,
    llm_reconcile: bool = False,
    run_id: str | None = None,
    max_tree_depth: int | None = None,
    since: str | None = None,
    base_scan_path: Path | str | None = None,
    lineage_jaccard_threshold: float | None = None,
    coverage_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the Layer 1 pipeline end-to-end against ``repo_path``.

    Args:
        repo_path: scan target.
        model: Haiku model id or alias (``"haiku"`` / ``"sonnet"`` /
            fully-qualified). Used for both Stage 3 and Stage 4.
        days: history window for git intake (Stage 0).
        out_path: explicit output path for the FeatureMap JSON. When
            ``None``, the writer picks a timestamped path under
            ``~/.faultline/``.
        llm_reconcile: pass-through to Stage 2's LLM-assisted name
            picker. Default ``False`` for a fully deterministic run.
        run_id: override the auto-generated run id. Useful for A/B
            experiments (``--run-id baseline`` then
            ``--run-id with-clustering``). Default ``None`` →
            ``<utc-ts>-<sha8>``.

    Returns:
        A dict containing ``path`` (the written FeatureMap path) and
        every key from ``scan_meta`` so callers can introspect the run
        without re-reading the JSON.
    """
    repo_path = Path(repo_path).resolve()
    model_id = resolve_model(model)
    t0 = time.monotonic()

    # Sprint C3b — caller-overridable Stage 6.3 BFS depth.
    # Defaults to the module-level :data:`_IMPORT_TREE_MAX_DEPTH`
    # (=8) when not supplied so legacy callers / library users
    # unaware of the new knob keep the new ceiling.
    effective_max_tree_depth = (
        int(max_tree_depth)
        if max_tree_depth is not None
        else _IMPORT_TREE_MAX_DEPTH
    )

    # One shared CostTracker across Stage 3 + Stage 4 so the reported
    # cost is the FULL LLM bill for this scan.
    tracker = CostTracker(max_cost=None)

    # ── Stage 0 — intake ────────────────────────────────────────────
    ctx = stage_0_intake(repo_path, days=days, run_id=run_id)
    run_dir = ctx.run_dir
    assert run_dir is not None, "Stage 0 must populate ctx.run_dir"

    with StageLogger(run_dir, 0, "intake") as log0:
        log0.info(
            f"intake: stack={ctx.stack} monorepo={ctx.monorepo} "
            f"workspace_manager={ctx.workspace_manager} "
            f"tracked_files={len(ctx.tracked_files)} "
            f"commits={len(ctx.commits)} run_id={ctx.run_id}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=0,
            stage_name="intake",
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
            run_dir=run_dir,
        )

    # ── Stage 0.5 — stack auditor (Haiku, additive) ────────────────
    # One LLM call between heuristic Stage 0 and deterministic Stage 1.
    # Read-only against ``ctx``; produces an :class:`AuditorVerdict`
    # that we fold into ``ctx`` ONLY when confidence ≥ 0.5. Stage 0's
    # ``stack`` field is never mutated — the verdict is surfaced via
    # ``ctx.audited_stack`` so downstream code can prefer either.
    with StageLogger(run_dir, 0, "auditor") as log_aud:
        verdict = run_stack_auditor(
            _isolate(ctx),
            model=model_id,
            cost_tracker=tracker,
            log=log_aud,
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
            ctx.repo_path,
            stage_index=0,
            stage_name="auditor",
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
                # Sprint S3.1 — structural correction telemetry.
                "corrections": list(verdict.corrections),
            },
            run_dir=run_dir,
        )

    # ── Stage 0.6 — shape classifier (deterministic, NO LLM) ──────
    # Decides which Stage 8 flow-rollup STRATEGY applies (turborepo /
    # oss-library / backend-monolith / single-saas-routed / cli-tool /
    # framework-repo / universal-residual). Pure function over Stage 0 +
    # 0.5 + structural manifest reads. Writes 06-stage-shape.json
    # artifact directly to run_dir.
    shape_result = classify_repo_shape(ctx)
    ctx = ctx.with_shape(shape_result)
    with StageLogger(run_dir, 6, "shape") as log_shape:
        log_shape.info(
            f"shape={shape_result.shape} "
            f"confidence={shape_result.confidence:.2f} "
            f"matched_signals={list(shape_result.matched_signals)}",
        )

    # ── Stage 1 — extractors ────────────────────────────────────────
    # Sprint S3 — per-workspace dispatch for polyglot monorepos.
    # When the auditor (or per-workspace stack diversity) flags the
    # repo as polyglot, we replace the global Stage 1 with a per-
    # workspace pass that scopes ``tracked_files`` + ``stack`` to one
    # workspace at a time. This unblocks NestJS+Next, Fastify+Vite,
    # Rust-WASM+Next, etc. — repos where a single-stack global pass
    # emits zero anchors and Stage 4 LLM-fallback synthesises 100%.
    per_ws_active = should_activate_per_workspace(ctx)
    per_ws_telemetry: dict[str, Any] = {
        "stage_1_per_workspace_active": False,
        "stage_1_per_workspace_workspaces_synthesised": False,
        "stage_1_per_workspace_workspaces_processed": [],
        "stage_1_per_workspace_anchors_total": 0,
        "stage_1_per_workspace_skipped_global_stage_1": False,
    }
    with StageLogger(run_dir, 1, "extractors") as log1:
        if per_ws_active:
            pw_result = run_stage_1_per_workspace(_isolate(ctx))
            if not pw_result.workspaces_used:
                # Activation passed but no workspaces materialised
                # (synthesis returned empty + no declared list).
                # Fall back to the global pass so we don't lose
                # signals altogether.
                log1.warn(
                    "per-workspace dispatch activated but no workspaces "
                    "found — falling back to global Stage 1",
                )
                stage1_out = stage_1_extractors(_isolate(ctx))
            else:
                stage1_out = pw_result.stage1_out
                per_ws_telemetry["stage_1_per_workspace_active"] = True
                per_ws_telemetry["stage_1_per_workspace_workspaces_synthesised"] = (
                    pw_result.synthesised_workspaces
                )
                per_ws_telemetry["stage_1_per_workspace_workspaces_processed"] = [
                    {
                        "name": r.name,
                        "path": r.path,
                        "inferred_stack": r.inferred_stack,
                        "extractors_fired": list(r.extractors_fired),
                        "anchors_emitted": r.anchors_emitted,
                    }
                    for r in pw_result.workspaces_processed
                ]
                per_ws_telemetry["stage_1_per_workspace_anchors_total"] = sum(
                    r.anchors_emitted for r in pw_result.workspaces_processed
                )
                per_ws_telemetry["stage_1_per_workspace_skipped_global_stage_1"] = True
                per_ws_telemetry["stage_1_per_workspace_leftover_files"] = (
                    pw_result.leftover_files_scanned
                )
                log1.info(
                    f"per-workspace active: workspaces="
                    f"{len(pw_result.workspaces_used)} "
                    f"synthesised={pw_result.synthesised_workspaces} "
                    f"anchors_total="
                    f"{per_ws_telemetry['stage_1_per_workspace_anchors_total']}",
                )
        else:
            stage1_out = stage_1_extractors(_isolate(ctx))

        extractor_hits = _extractor_hits(stage1_out)
        for name, count in extractor_hits.items():
            log1.info(f"{name}: {count} candidates", feature=None)
        for name, err in (stage1_out.get("_errors") or {}).items():
            log1.warn(f"extractor {name} errored: {err}")

        # Sprint D3 — workspace package detection telemetry.
        # Counts how many declared workspaces produced a package-source
        # anchor with the workspace's slug. Helps catch regressions in
        # generic-named packages (``packages/ui``, ``packages/utils``)
        # that historically went undetected.
        workspace_telemetry = _workspace_anchor_telemetry(ctx, stage1_out)
        if workspace_telemetry["workspace_packages_detected"]:
            log1.info(
                f"workspace_packages: detected="
                f"{workspace_telemetry['workspace_packages_detected']} "
                f"anchored={workspace_telemetry['workspace_packages_anchored']} "
                f"dropped={workspace_telemetry['workspace_packages_dropped']}",
            )

        write_stage_artifact(
            ctx.repo_path,
            stage_index=1,
            stage_name="extractors",
            payload={
                "extractor_hits": extractor_hits,
                "errors": stage1_out.get("_errors", {}),
                "per_workspace": per_ws_telemetry,
                "workspace_packages": workspace_telemetry,
            },
            run_dir=run_dir,
        )

    # ── Stage 2 — reconciliation ────────────────────────────────────
    with StageLogger(run_dir, 2, "reconcile") as log2:
        stage2 = stage_2_reconcile(
            _isolate(stage1_out), _isolate(ctx),
            llm_reconcile=llm_reconcile,
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
        if stage2.zero_path_drops_count:
            log2.info(
                f"zero_path_drops: {stage2.zero_path_drops_count} "
                f"sample={stage2.zero_path_drops_sample}",
            )
        for note in stage2.notes:
            log2.info(note)
        write_stage_artifact(
            ctx.repo_path,
            stage_index=2,
            stage_name="reconcile",
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
                # Sprint S4b — zero-path defensive drop telemetry.
                "zero_path_drops_count": stage2.zero_path_drops_count,
                "zero_path_drops_sample": stage2.zero_path_drops_sample,
            },
            run_dir=run_dir,
        )

    # ── Stage 3 — flow detection (Haiku) ───────────────────────────
    with StageLogger(run_dir, 3, "flows") as log3:
        stage3 = stage_3_flows(
            _isolate(deterministic_features), _isolate(ctx),
            model=model_id, cost_tracker=tracker,
        )
        for fwf in stage3.features_with_flows:
            log3.emit(
                fwf.feature.name,
                f"{len(fwf.flows)} flows detected",
            )
        for w in stage3.warnings:
            log3.warn(w)
        log3.info(
            f"cost_usd={stage3.cost_usd:.4f} llm_calls={stage3.llm_calls}",
        )
        # Sprint C1 — call-graph reach enrichment summary.
        if stage3.reach_telemetry:
            log3.info(
                "reach: avg_paths="
                f"{stage3.reach_telemetry.get('stage_3_flow_reach_avg_paths', 0)} "
                f"max_paths={stage3.reach_telemetry.get('stage_3_flow_reach_max_paths', 0)} "
                f"p50_depth={stage3.reach_telemetry.get('stage_3_flow_reach_p50_depth', 0)} "
                f"total={stage3.reach_telemetry.get('stage_3_flow_reach_total_paths', 0)} "
                f"enriched={stage3.reach_telemetry.get('stage_3_flow_reach_enriched_count', 0)}",
            )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=3,
            stage_name="flows",
            payload={
                "feature_count": len(stage3.features_with_flows),
                "total_flows": sum(
                    len(fwf.flows) for fwf in stage3.features_with_flows
                ),
                "cost_usd": stage3.cost_usd,
                "llm_calls": stage3.llm_calls,
                "warnings": stage3.warnings,
                "reach_telemetry": stage3.reach_telemetry,
            },
            run_dir=run_dir,
        )

    # ── Stage 4 — residual LLM fallback (cluster + saturation) ─────
    with StageLogger(run_dir, 4, "residual") as log4:
        stage4 = stage_4_residual(
            _isolate(unattributed), _isolate(ctx),
            _isolate(deterministic_features),
            model=model_id, cost_tracker=tracker, log=log4,
        )
        residual_features = stage4.residual_features
        for f in residual_features:
            log4.emit(
                f.name,
                f"residual cluster from {len(f.paths)} paths",
            )
        for name in stage4.rejected_names:
            log4.drop(name, "rejected by naming-discipline filter")
        for w in stage4.warnings:
            log4.warn(w)
        log4.info(
            f"cost_usd={stage4.cost_usd:.4f} llm_calls={stage4.llm_calls} "
            f"clusters_processed={stage4.clusters_processed}/"
            f"{stage4.clusters_total} "
            f"saturation_stopped={stage4.saturation_stopped}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=4,
            stage_name="residual",
            payload={
                "residual_feature_count": len(residual_features),
                "cost_usd": stage4.cost_usd,
                "llm_calls": stage4.llm_calls,
                "warnings": stage4.warnings,
                "clusters_total": stage4.clusters_total,
                "clusters_processed": stage4.clusters_processed,
                "saturation_stopped": stage4.saturation_stopped,
                "rejected_names": stage4.rejected_names,
                "singletons_synthesized": stage4.singletons_synthesized,
                "singletons_skipped": stage4.singletons_skipped,
                "cost_cap_hit": stage4.cost_cap_hit,
                # Sprint S2b — structural guard telemetry.
                "guard_singletons_dropped": stage4.guard_singletons_dropped,
                "guard_incoherent_clusters_split":
                    stage4.guard_incoherent_clusters_split,
                "guard_drops_sample": stage4.guard_drops_sample,
                # Sprint S2c — noise-path-segment drop counter.
                "guard_noise_path_drops": stage4.guard_noise_path_drops,
            },
            run_dir=run_dir,
        )

    # ── Stage 5 — post-process (naming discipline + A1 validation) ─
    with StageLogger(run_dir, 5, "postprocess") as log5:
        stage5_result = stage_5_from_stage3_result_with_telemetry(
            deterministic=_isolate(deterministic_features),
            stage3_features_with_flows=_isolate(stage3.features_with_flows),
            residual=_isolate(residual_features),
            ctx=_isolate(ctx),
        )
        features = stage5_result.features
        validation_drops = stage5_result.validation_drops
        for name, reason in stage5_result.drop_log:
            log5.drop(name, reason)
        for f in features:
            log5.emit(f.name, "survived naming discipline")
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

    # ── Stage 5.5 — bipartite store + blast-radius (deterministic) ─
    # Pure in-memory pass over the Stage 5 features. Mutates each
    # contained Flow in place to populate the new bipartite fields
    # (id, primary_feature, secondary_features, shared_with_*_count,
    # cross_cutting), then returns a top-level flows[] projection and
    # the feature_flow_edges[] list. NO LLM — path-overlap only.
    with StageLogger(run_dir, 5, "bipartite") as log5_5:
        bipartite = stage_5_5_bipartite(features, log=log5_5)
        for w in []:  # placeholder for future warnings
            log5_5.warn(w)
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

    # ── Stage 6 — metrics enrichment ───────────────────────────────
    # NOTE: we feed Stage 6 the SAME ``features`` reference (not a
    # deep-copy) so the bipartite mutations made in Stage 5.5 survive
    # into the final output. Stage 6's contract is to fill blame /
    # coverage / commit fields; it MUST NOT mutate Feature.paths or
    # Flow.paths (which the bipartite IDs were minted from).
    with StageLogger(run_dir, 6, "metrics") as log6:
        features = stage_6_metrics(
            features, _isolate(ctx),
            coverage_path=str(coverage_path) if coverage_path else None,
        )
        with_commits = sum(1 for f in features if f.total_commits > 0)
        with_coverage = sum(1 for f in features if f.coverage_pct is not None)
        log6.info(
            f"enriched: with_commits={with_commits} "
            f"with_coverage={with_coverage} of {len(features)}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="metrics",
            payload={
                "feature_count": len(features),
                "with_commits": with_commits,
                "with_coverage": with_coverage,
            },
            run_dir=run_dir,
        )

    # ── Stage 6.5 — Layer 2 product clusterer (deterministic) ──────
    # Pure rule-based clustering — workspace concentration + dep-anchor
    # imports + optional ``faultlines.yaml`` override. NO LLM. Folds
    # Stage 6 dev features into customer-facing product features.
    with StageLogger(run_dir, 6, "product_clusterer") as log6_5:
        product_features, dev_to_product_map, product_telemetry = (
            run_product_clusterer(_isolate(ctx), features, log=log6_5)
        )
        # Stamp the FIRST product label onto each dev feature as the
        # legacy single-valued ``product_feature_id`` for back-compat
        # with consumers that read the Layer-1 ↔ Layer-2 pointer
        # before the bipartite extension lands in their stack. The
        # full multi-label set lives in the orchestrator's mapping
        # dict and is preserved in the scan_meta telemetry below.
        for f in features:
            labels = dev_to_product_map.get(f.name)
            if labels:
                f.product_feature_id = labels[0]
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="product_clusterer",
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
            run_dir=run_dir,
        )

    # ── Stage 6.3 — whole-import-tree enrichment (deterministic) ───
    # Sprint C3 (2026-05-20). Closes two gaps the user identified:
    #   * Forward flow trees stayed depth-0 because the legacy
    #     tsconfig loader picked the root config (no paths) and
    #     missed per-workspace alias maps.
    #   * Reverse package-anchor / schema-source features had no
    #     consumer expansion at all (Billing showed paths=1).
    # The stage runs AFTER 6.5 so the deterministic product
    # clusterer (which uses paths[0] as a workspace heuristic) is
    # unaffected by path explosion. NO LLM.
    with StageLogger(run_dir, 6, "import_tree") as log6_3:
        enrichment = enrich_with_import_tree(
            ctx, features, log=log6_3,
            max_depth=effective_max_tree_depth,
            max_files_per_feature=_IMPORT_TREE_MAX_FILES,
            max_symbols_per_feature=_IMPORT_TREE_MAX_SYMBOLS,
        )
        features = list(enrichment.enriched_features)
        artifact_payload = _import_tree_artifact(
            enrichment,
            max_depth=effective_max_tree_depth,
            max_files_per_feature=_IMPORT_TREE_MAX_FILES,
            max_symbols_per_feature=_IMPORT_TREE_MAX_SYMBOLS,
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="import_tree",
            payload=artifact_payload,
            run_dir=run_dir,
        )
        log6_3.info(
            "import-tree summary: "
            f"total_seeds={enrichment.total_seeds} "
            f"files_reached={enrichment.total_files_reached} "
            f"symbols_emitted={enrichment.total_symbols_emitted} "
            f"cycles={enrichment.cycles_detected} "
            f"depth_capped={enrichment.depth_capped_events} "
            f"external_skipped={enrichment.external_skipped} "
            f"cache_hits={enrichment.cache_hits} "
            f"elapsed={enrichment.elapsed_sec}s",
        )

    # ── Stage 6.4 — framework-aware enrichment (deterministic) ─────
    # Sprint C4 (2026-05-20). Closes the gap C3's import-tree cannot
    # bridge: HTTP route handlers reached via fetch URL strings, Server
    # Actions across the network boundary, store mutations dispatched
    # by string action type, tRPC procedures referenced by namespace
    # string. v1 ships ONE linker — Next.js HTTP route. Future linkers
    # plug in via Python entry-points without modifying Stage 6.4 core.
    # NO LLM, NO network — pure file IO + regex.
    with StageLogger(run_dir, 6, "framework_enrich") as log6_4:
        enrich_result = run_stage_6_4(ctx, features, log6_4)
        features = list(enrich_result.enriched_features)
        framework_enrich_telemetry = enrich_result.telemetry()
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="framework_enrich",
            payload=framework_enrich_telemetry,
            run_dir=run_dir,
        )
        log6_4.info(
            "framework-enrich summary: "
            f"active_linkers={enrich_result.active_linkers} "
            f"skipped_linkers={[s['name'] for s in enrich_result.skipped_linkers]} "
            f"links_emitted_total={enrich_result.links_emitted_total} "
            f"elapsed={enrich_result.elapsed_sec}s",
        )

    # ── Stage 6.6 — branch slicer (Sprint D2, deterministic) ───────
    # Tree-sitter walks each (feature × symbol_attribution) and emits
    # intra-symbol conditional regions (if / else / ternary /
    # switch_case / try / catch / match_arm) as role=``branch``
    # attributions. Optional dependency: when tree-sitter is not
    # installed, stage is a no-op and the rest of the pipeline runs
    # unchanged. NO LLM. NO network. See
    # `faultline/pipeline_v2/stage_6_6_branch_slicer.py` docstring.
    with StageLogger(run_dir, 6, "branch_slicer") as log6_6:
        branch_result = run_stage_6_6(ctx, features, log6_6)
        branch_slicer_telemetry = branch_result.telemetry()
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="branch_slicer",
            payload=branch_slicer_telemetry,
            run_dir=run_dir,
        )

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
        if s8_mode == "analyst":
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
                top_flows=list(bipartite.flows),
                log=log8,
                client=s8_client,
                model=_STAGE_8_ANALYST_MODEL,
                cost_tracker=tracker,
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
            )
        # Apply Stage 8 overrides — replace product_features and the
        # legacy single-valued ``product_feature_id`` stamp.
        product_features = stage_8_result.product_features
        dev_to_product_map = stage_8_result.dev_to_product_map
        for f in features:
            labels = dev_to_product_map.get(f.name)
            f.product_feature_id = labels[0] if labels else None
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
            list(bipartite.flows),
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
            len(rollup_result.unattributed_flows) / max(len(bipartite.flows), 1),
            4,
        ),
        "capped_pfs_count": len(rollup_result.diagnostics.get("capped_pfs", [])),
    }

    # ── Scan meta assembly ─────────────────────────────────────────
    # Count fallback survivors by NAME match against the post-A1-validation
    # residual list (stage5 stripped FS-missing + anchor-dup before naming
    # discipline; some may still have been dropped by Fix A/B/C/D).
    pre_naming_fallback_names = {
        name for (name, reason) in stage5_result.drop_log
        if reason.startswith("junk_name:")
    }
    # Fallback features that BOTH survived A1 validation AND naming discipline.
    survived_fallback_names = (
        {f.name for f in residual_features}
        - pre_naming_fallback_names
        # Also subtract any A1-validation drops:
        - {
            name for (name, reason) in stage5_result.drop_log
            if not reason.startswith("junk_name:")
        }
    )
    final_feature_names = {f.name for f in features}
    fallback_count = len(survived_fallback_names & final_feature_names)
    total_features = len(features)
    deterministic_count = max(total_features - fallback_count, 0)

    llm_share = (
        fallback_count / total_features if total_features > 0 else 0.0
    )
    extractor_coverage_pct = (
        deterministic_count / total_features if total_features > 0 else 0.0
    )

    warnings: list[str] = []
    warnings.extend(stage3.warnings)
    warnings.extend(stage4.warnings)
    # Sprint F (2026-05-20) — surface budget-exceeded events into
    # scan_meta so dashboards / replay tooling can see them without
    # opening the per-stage artifact.
    if enrichment.budget_exceeded:
        warnings.append(
            f"stage_6_3_budget_exceeded budget_sec={enrichment.budget_sec} "
            f"features_skipped={enrichment.features_budget_skipped} "
            f"elapsed_sec={enrichment.elapsed_sec}"
        )
    if enrich_result.budget_exceeded:
        warnings.append(
            f"stage_6_4_budget_exceeded budget_sec={enrich_result.budget_sec} "
            f"features_skipped={enrich_result.features_budget_skipped} "
            f"elapsed_sec={enrich_result.elapsed_sec}"
        )
    if getattr(branch_result, "budget_exceeded", False):
        warnings.append(
            f"stage_6_6_budget_exceeded budget_sec={branch_result.budget_sec} "
            f"features_skipped={branch_result.features_budget_skipped} "
            f"elapsed_sec={branch_result.elapsed_sec}"
        )
    if llm_share > LLM_FALLBACK_WARN_THRESHOLD:
        # Sprint A1: informational nudge only. The old 30%-share cap
        # was REMOVED; we no longer truncate Stage 4 output. This
        # warning tells the operator "you're heavily relying on the
        # LLM for this stack — write an extractor".
        warnings.append(
            f"scan_meta.llm_share = {llm_share:.2f} — fallback exceeds "
            f"half of features; consider adding extractor for stack="
            f"{ctx.stack}."
        )

    elapsed = round(time.monotonic() - t0, 2)
    cost_usd = round(tracker.total_cost_usd, 4)
    llm_calls = stage3.llm_calls + stage4.llm_calls

    scan_meta: dict[str, Any] = {
        "pipeline_version": "v2",
        "run_id": ctx.run_id,
        "stack": ctx.stack,
        "monorepo": ctx.monorepo,
        "workspace_manager": ctx.workspace_manager,
        "stack_signals": ctx.stack_signals,
        # Sprint A3 — Stage 0.5 auditor surface.
        # Always emitted so consumers can detect a fallback by
        # `auditor_fallback_used: True` rather than absent keys.
        "audited_stack": verdict.primary_stack if not verdict.fallback_used
                         else None,
        "secondary_stacks": list(verdict.secondary_stacks),
        "auditor_confidence": round(verdict.confidence, 3),
        "auditor_hints": list(verdict.extractor_hints),
        "auditor_cost_usd": round(verdict.cost_usd, 6),
        "auditor_fallback_used": verdict.fallback_used,
        "auditor_reasoning": verdict.reasoning,
        # Sprint S3.1 — deterministic correction overrides applied
        # AFTER the LLM verdict. Empty list when no rule fired.
        "auditor_corrections": list(verdict.corrections),
        "model": model_id,
        "extractor_hits": extractor_hits,
        # Sprint D3 — workspace package telemetry. Exposed at top of
        # scan_meta so dashboards can flag silent skips without
        # parsing the per-stage artifact.
        "workspace_packages": workspace_telemetry,
        "extractor_coverage_pct": round(extractor_coverage_pct, 3),
        # ``llm_fallback_pct`` kept for backwards-compat with existing
        # dashboards. ``llm_share`` is the Sprint A1 canonical name.
        "llm_fallback_pct": round(llm_share, 3),
        "llm_share": round(llm_share, 3),
        "validation_drops": validation_drops.as_dict(),
        # Sprint S1 — sibling-workspace dedup telemetry. ``count`` is the
        # number of merge events; ``sample`` is up to 3 events for
        # diagnostics ({merged_name, from[], paths_sample[]}).
        "dedup_merges_count": len(stage5_result.dedup_merges),
        "dedup_merges_sample": [
            m.as_dict() for m in stage5_result.dedup_merges[:3]
        ],
        # Sprint S4b — Stage 2 defensive zero-path drop telemetry.
        # Surfaces URL-ghost / shared-anchor-orphan features that were
        # evicted after attribution so downstream stages don't carry
        # them as developer features. Non-zero means an extractor
        # over-emitted slugs against the same source file and even
        # the zero-path-protection inside _attribute_paths couldn't
        # rescue them — usually a hint to tighten that extractor.
        "stage_2_zero_path_drops_count": stage2.zero_path_drops_count,
        "stage_2_zero_path_drops_sample": stage2.zero_path_drops_sample,
        # Sprint S4 — Stage 5.3 sibling-router collapse telemetry.
        "stage_5_3_collapse_groups_count": len(s53.collapse_groups),
        "stage_5_3_features_collapsed": s53.features_collapsed,
        "stage_5_3_features_pre": s53_features_pre,
        "stage_5_3_features_post": s53_features_post,
        "stage_5_3_collapse_sample": s53_collapse_sample,
        "deterministic_feature_count": deterministic_count,
        "residual_feature_count": fallback_count,
        "warnings": warnings,
        "elapsed_sec": elapsed,
        "cost_usd": cost_usd,
        "calls": llm_calls,
        "stage3_cost_usd": round(stage3.cost_usd, 4),
        "stage4_cost_usd": round(stage4.cost_usd, 4),
        "stage_4_clusters_total": stage4.clusters_total,
        "stage_4_clusters_processed": stage4.clusters_processed,
        "stage_4_singletons_synthesized": stage4.singletons_synthesized,
        "stage_4_singletons_skipped": stage4.singletons_skipped,
        "stage_4_saturation_stopped": stage4.saturation_stopped,
        "stage_4_cost_cap_hit": stage4.cost_cap_hit,
        # Sprint S2b — structural guard telemetry.
        "stage_4_singletons_dropped": stage4.guard_singletons_dropped,
        "stage_4_incoherent_clusters_split":
            stage4.guard_incoherent_clusters_split,
        "stage_4_drops_sample": stage4.guard_drops_sample,
        # Sprint S2c — noise-path-segment drop counter.
        "stage_4_noise_path_drops": stage4.guard_noise_path_drops,
        "stage_artifact_dir": str(run_dir),
        "llm_reconcile": bool(llm_reconcile),
        # Sprint B1 — bipartite store telemetry (deterministic).
        **bipartite.telemetry,
        # Sprint C1 — call-graph reach telemetry (deterministic).
        **(stage3.reach_telemetry or {}),
        # Sprint B3 — Layer 2 product clusterer telemetry (deterministic).
        **product_telemetry,
        # Sprint S3 — per-workspace Stage 1 dispatch telemetry.
        **per_ws_telemetry,
        # Sprint C3 — Stage 6.3 whole-import-tree enrichment telemetry.
        "stage_6_3_alias_map_size": len(enrichment.alias_map),
        "stage_6_3_total_seeds": enrichment.total_seeds,
        "stage_6_3_total_files_reached": enrichment.total_files_reached,
        "stage_6_3_total_symbols_emitted":
            enrichment.total_symbols_emitted,
        "stage_6_3_cycles_detected": enrichment.cycles_detected,
        "stage_6_3_depth_capped_events": enrichment.depth_capped_events,
        "stage_6_3_external_skipped": enrichment.external_skipped,
        "stage_6_3_cache_hits": enrichment.cache_hits,
        "stage_6_3_elapsed_sec": enrichment.elapsed_sec,
        # Sprint C3b — nested config namespace so external tools can
        # introspect the depth / cap configuration without scraping
        # flat keys. ``max_depth_configured`` reflects the EFFECTIVE
        # value used by this scan (CLI override OR default 8).
        "stage_6_3": {
            "max_depth_configured": effective_max_tree_depth,
            "max_files_per_feature": _IMPORT_TREE_MAX_FILES,
            "max_symbols_per_feature": _IMPORT_TREE_MAX_SYMBOLS,
            "alias_map_size": len(enrichment.alias_map),
            "total_seeds": enrichment.total_seeds,
            "total_files_reached": enrichment.total_files_reached,
            "total_symbols_emitted": enrichment.total_symbols_emitted,
            "depth_capped_events": enrichment.depth_capped_events,
            "elapsed_sec": enrichment.elapsed_sec,
        },
        # Sprint C4 — Stage 6.4 framework-aware enrichment telemetry.
        # Pluggable linker registry; v1 ships nextjs-http-route. Skipped
        # linkers (e.g. non-Next stacks) appear in ``skipped_linkers``
        # with a reason so coverage gaps are observable from the artifact.
        "stage_6_4": {
            "active_linkers": framework_enrich_telemetry["active_linkers"],
            "skipped_linkers": framework_enrich_telemetry["skipped_linkers"],
            "per_linker": framework_enrich_telemetry["per_linker"],
            "links_emitted_total": framework_enrich_telemetry["links_emitted_total"],
            "elapsed_sec": framework_enrich_telemetry["elapsed_sec"],
        },
        # Sprint D2 — Stage 6.6 branch-slicer telemetry. Optional
        # tree-sitter pass that emits intra-symbol conditional ranges
        # (if/else/ternary/switch_case/try/catch/match_arm) as new
        # role=``branch`` attributions on each feature. ``active=false``
        # with a ``reason`` when tree-sitter is not installed (graceful
        # degrade); the rest of the pipeline is unaffected.
        "stage_6_6": dict(branch_slicer_telemetry),
        # Sprint E1 — Stage 8 marketing-grounded Layer 2 clusterer.
        # ``source`` is one of "customer-yaml" / "marketing+haiku" /
        # "deterministic-only". When "marketing+haiku", taxonomy_size +
        # marketing_url surface the maintainer page that grounded the
        # Haiku mapping. ``haiku_call_cost_usd`` is included in the
        # top-level ``cost_usd`` total via the shared CostTracker.
        "stage_8": dict(stage_8_telemetry),
        # Sprint S6.1 — Stage 8 per-shape flow-rollup dispatcher
        # telemetry. ``rollup_strategy`` is one of the SHAPE_ROLLUPS
        # registry keys; ``pfs_attributed_count`` reports how many PFs
        # got at least one flow attached.
        "stage_08_rollup": dict(stage_8_rollup_telemetry),
        # Sprint S6.1 — Stage 0.6 deterministic shape classifier.
        # Used by the Stage 8 flow-rollup dispatcher to pick the per-
        # shape attribution strategy. Universal-residual is the safe
        # fallback when no shape clears MIN_CONFIDENCE.
        "stage_06": {
            "shape": shape_result.shape,
            "shape_confidence": shape_result.confidence,
            "shape_rationale": shape_result.rationale,
            "matched_signals": list(shape_result.matched_signals),
            "fallback_used": shape_result.shape == "universal-residual",
        },
    }

    # ── Stage 6.8 — lineage + indexes (Sprint 1, 2026-05-23) ──────
    # Pure post-pass: stamps stable UUIDs on every Feature + Flow,
    # builds path_index + routes_index. NEVER affects scan-quality
    # decisions. When ``base_scan_path`` is provided we match against
    # the previous scan for cross-scan UUID stability; otherwise every
    # feature/flow gets a fresh uuid4 (cold-scan default).
    from faultline.pipeline_v2.incremental import (
        carry_forward_metrics as _carry_forward_metrics,
        changed_files_since as _changed_files_since,
        head_sha as _head_sha,
        load_base_scan as _load_base_scan,
        touched_feature_uuids as _touched_feature_uuids,
    )
    from faultline.pipeline_v2.lineage import (
        RELATED_THRESHOLD as _RELATED_THRESHOLD,
        RENAME_THRESHOLD as _RENAME_THRESHOLD,
    )
    from faultline.pipeline_v2.stage_6_8_lineage import run_stage_6_8

    rename_threshold = (
        float(lineage_jaccard_threshold)
        if lineage_jaccard_threshold is not None
        else _RENAME_THRESHOLD
    )

    base_scan_dict: dict[str, Any] | None = None
    if base_scan_path is not None:
        base_scan_dict = _load_base_scan(base_scan_path)

    lineage_result = run_stage_6_8(
        features,
        list(bipartite.flows),
        base_scan=base_scan_dict,
        extractor_signals=stage1_out,
        rename_threshold=rename_threshold,
        related_threshold=_RELATED_THRESHOLD,
    )

    # ── Incremental scan bookkeeping ───────────────────────────────
    is_full_scan = since is None
    head = _head_sha(repo_path) if not is_full_scan else _head_sha(repo_path)
    carried_count = 0
    incremental_meta: dict[str, Any] = {
        "incremental_changed_files": [],
        "incremental_touched_uuids": [],
        "incremental_carried_forward_count": 0,
    }
    if not is_full_scan:
        if base_scan_dict is None:
            raise ValueError(
                "--since requires --base-scan-path (engine cannot match "
                "lineage without a previous scan)."
            )
        changed = _changed_files_since(repo_path, since or "")
        touched = _touched_feature_uuids(changed, base_scan_dict)
        # Carry forward Stage 6 metrics for untouched features.
        # We mutate the Feature pydantic models via model_dump round-trip
        # so the carry-forward helper can operate on plain dicts.
        base_feats = (
            base_scan_dict.get("developer_features")
            or base_scan_dict.get("features")
            or []
        )
        # Mutate features in-place — easier than rebuilding pydantic models.
        feat_payload = [f.model_dump() for f in features]
        carried_count = _carry_forward_metrics(
            feat_payload, list(base_feats), touched,
        )
        # Push the touched metric values back onto the Feature objects.
        by_uuid = {p.get("uuid"): p for p in feat_payload if p.get("uuid")}
        for f in features:
            p = by_uuid.get(f.uuid)
            if not p:
                continue
            for k in (
                "health_score", "bug_fix_ratio", "bug_fixes",
                "coverage_pct", "total_commits",
                "symbol_health_score",
            ):
                if k in p and p[k] is not None:
                    setattr(f, k, p[k])
        incremental_meta = {
            "incremental_changed_files": list(changed),
            "incremental_touched_uuids": sorted(touched),
            "incremental_carried_forward_count": carried_count,
        }
    scan_meta["lineage_feature_stats"] = lineage_result.feature_lineage_stats
    scan_meta["lineage_flow_stats"] = lineage_result.flow_lineage_stats
    scan_meta["lineage_rename_threshold"] = rename_threshold
    scan_meta["is_full_scan"] = is_full_scan
    scan_meta.update(incremental_meta)

    # ── Stage 3.5 — flow expansion (Sprint 2, deterministic) ──────
    # Enriches every Flow with {entry, nodes[], edges[], summary}
    # via T1 (intra-repo call graph) + T2 (cross-stack HTTP boundary
    # matched against the Sprint 1 routes_index). Mutates Flow
    # objects in place under both Feature.flows (containment view)
    # AND the top-level bipartite list. Pure in-memory; no LLM; no
    # persistence — preserves [[rule-cold-scan]]. Legacy fields on
    # Flow (paths, participants, entry_point_file, coverage_pct,
    # flow_symbol_attributions, uuid, all Stage 5.5 bipartite fields)
    # are preserved unchanged.
    #
    # Inserted between Stage 6.8 (lineage / routes_index build) and
    # Stage 7 (output) so:
    #   - routes_index is available for T2 cross-stack matching;
    #   - the expansion lands in the final FeatureMap JSON;
    #   - lineage-stable UUIDs are present on every Flow for the
    #     ``top_level_flows`` mirror pass.
    from faultline.pipeline_v2.flow_expansion import expand_flows
    with StageLogger(run_dir, 3, "flow_expansion") as log3_5:
        fx = expand_flows(
            features,
            ctx,
            routes_index=lineage_result.routes_index,
            log=log3_5,
            top_level_flows=list(bipartite.flows),
        )
        log3_5.info(
            f"expansion: flows_expanded={fx.telemetry['flows_expanded']} "
            f"nodes_total={fx.telemetry['nodes_total']} "
            f"edges_total={fx.telemetry['edges_total']} "
            f"cross_stack_hops_total={fx.telemetry['cross_stack_hops_total']} "
            f"deepest_depth={fx.telemetry['deepest_depth_reached']} "
            f"truncated={fx.telemetry['flows_truncated']} "
            f"unsupported_stack={fx.telemetry['flows_unsupported_stack']}",
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=3,
            stage_name="flow_expansion",
            payload=fx.telemetry,
            run_dir=run_dir,
        )
    scan_meta["stage_3_5_flow_expansion"] = dict(fx.telemetry)

    # ── Stage 7 — output ───────────────────────────────────────────
    from faultline import __version__ as _engine_version  # late import
    with StageLogger(run_dir, 7, "output") as log7:
        out = stage_7_output(
            features, ctx, scan_meta, out_path,
            days=days,
            flows=bipartite.flows,
            feature_flow_edges=bipartite.edges,
            product_features=product_features,
            path_index=lineage_result.path_index,
            routes_index=lineage_result.routes_index,
            is_full_scan=is_full_scan,
            base_scan_commit=(since or ""),
            scan_commit=head,
            engine_version=_engine_version,
        )
        log7.info(f"wrote feature map to {out}", feature=None)

    # ── Atomically point `latest` at this run ──────────────────────
    update_latest_symlink(ctx.repo_path, ctx.run_id or "")

    logger.info(
        "pipeline_v2 done: run_id=%s %d features, cost $%.4f, elapsed %.1fs → %s",
        ctx.run_id, total_features, cost_usd, elapsed, out,
    )

    return {"path": str(out), **scan_meta}


__all__ = [
    "run_pipeline_v2",
    "resolve_model",
    "MODEL_ALIASES",
    "DEFAULT_MODEL",
]
