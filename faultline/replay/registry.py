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

__all__ = [
    "ReplayEnv", "SentinelMissHealth", "StageSpec", "STAGES",
    "stage_by_key", "pipeline_slice",
]


# ── Replay environment ──────────────────────────────────────────────────


class SentinelMissHealth(LlmHealth):
    """Replay-only LLM health: sentinel-key 401s are cache-miss noise.

    Under the $0 replay model every LLM unit is served from the warm
    llm-cache; a MISS goes out with the never-authenticating sentinel
    key and 401s. That 401 is a HARNESS artifact, not a scan-world auth
    death — flipping the sticky scan-wide flag on it mutates a
    live-degraded-but-auth-healthy world (json-parse degrades, credit-
    wall 400s — degrades are never cached, so their units always miss)
    into an auth-dead world, which takes DIFFERENT code paths (the B71
    naming-pack confidence downgrade; the llm_degraded stamp — G5
    forensics on the pinned formbricks corpus). Swallow the organic
    auth flip: the missed unit still degrades per-call exactly like a
    live in-flight failure (``_call_haiku``-family returns empty text →
    the stage's own degrade path). A RECORDED auth death is restored
    explicitly via :meth:`LlmHealth.seed_auth_failure`, which still
    arms the sticky short-circuit.
    """

    def record_failure(self, exc: BaseException, *, stage: str) -> bool:
        from faultline.pipeline_v2.llm_health import is_auth_error

        if is_auth_error(exc):
            logger.debug(
                "replay: sentinel-key auth miss at %s (cache-miss noise; "
                "health flag not flipped)", stage,
            )
            return False
        return super().record_failure(exc, stage=stage)


@dataclass
class ReplayEnv:
    """Fresh service objects + the NEW run dir for one replay run."""

    run_dir: Path
    run_id: str
    tracker: CostTracker = field(default_factory=lambda: CostTracker(max_cost=None))
    llm_health: LlmHealth = field(default_factory=SentinelMissHealth)

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
    # artifact-only rows: the stage EMITS an output artifact in a live
    # run but has NO input capture of its own — its orchestration lives
    # inside another stage's replay unit (between that stage's input
    # capture and the next one), so the OWNING runner emits the
    # artifact and the chain runner skips this row. Registered so
    # artifact-name → pipeline-order lookups (the mutation ship-gate)
    # can place the artifact; identity replay never targets it (no
    # input artifact exists to start from).
    artifact_only: bool = False
    # LLM-bearing stages map to an llm-cache subdir for --fresh-llm.
    llm_cache_dir: str | None = None


# ── Runners ─────────────────────────────────────────────────────────────


def _artifact_only_stage(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Placeholder runner for ``artifact_only`` StageSpec rows — never
    executed: the chain runner skips artifact-only rows and the owning
    composite runner emits their artifacts (see StageSpec.artifact_only)."""
    raise RuntimeError(
        "artifact-only stage has no standalone runner — its artifact is "
        "emitted by the composite runner that replicates its run.py "
        "orchestration block",
    )


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

    # ── B34 — lazy-import edges + dispatch-registry seeds ($0) ─────
    # Replicates the run.py orchestration block that lives INSIDE the
    # flows replay unit (between the stage-3 and stage-4 input
    # captures, run.py "B34" section): Tier 1 collects lazy-import
    # edges, Tier 2 mints dispatch-registry flow seeds by MUTATING
    # stage3.features_with_flows in place — downstream stages must see
    # the seeds for chain replay to stay byte-identical to a live run.
    # Both stages are artifact-only StageSpec rows (no input capture of
    # their own); their artifacts are emitted HERE.
    from faultline.pipeline_v2.dispatch_registry import (
        dispatch_registry_enabled,
        run_dispatch_registry_stage,
    )
    from faultline.pipeline_v2.lazy_imports import (
        collect_lazy_import_edges,
        lazy_import_edges_enabled,
    )
    if lazy_import_edges_enabled() or dispatch_registry_enabled():
        with StageLogger(env.run_dir, 3, "lazy_imports") as log_li:
            try:
                _lazy_edges = collect_lazy_import_edges(
                    ctx.repo_path, list(ctx.tracked_files),
                )
                log_li.info(
                    "lazy_imports: %d resolved repo-internal edges "
                    "(py=%d ts=%d optional=%d)" % (
                        len(_lazy_edges),
                        sum(1 for e in _lazy_edges if e.lang == "py"),
                        sum(1 for e in _lazy_edges if e.lang == "ts"),
                        sum(1 for e in _lazy_edges if e.optional),
                    ),
                    feature=None,
                )
                if lazy_import_edges_enabled():
                    write_stage_artifact(
                        ctx.repo_path, stage_index=3,
                        stage_name="lazy_imports",
                        payload={
                            "edges": [
                                {
                                    "src": e.src, "target": e.target,
                                    "target_file": e.target_file,
                                    "lang": e.lang, "kind": e.kind,
                                    "optional": e.optional,
                                } for e in _lazy_edges
                            ],
                        },
                        run_dir=env.run_dir,
                    )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                _lazy_edges = []
                log_li.info(
                    f"lazy_imports: FAILED ({exc}) — continuing without edges",
                    feature=None,
                )
        if dispatch_registry_enabled():
            with StageLogger(env.run_dir, 3, "dispatch_registry") as log_dr:
                try:
                    _dr_tele = run_dispatch_registry_stage(
                        stage3.features_with_flows, ctx, _lazy_edges,
                    )
                    log_dr.info(
                        "dispatch_registry: minted %d seeds "
                        "(targets=%d py=%d ts=%d covered-skip=%d "
                        "no-owner-skip=%d)" % (
                            _dr_tele["minted"], _dr_tele["targets_total"],
                            _dr_tele["py_targets"], _dr_tele["ts_targets"],
                            _dr_tele["skipped_covered"],
                            _dr_tele["skipped_no_owner"],
                        ),
                        feature=None,
                    )
                    write_stage_artifact(
                        ctx.repo_path, stage_index=3,
                        stage_name="dispatch_registry",
                        payload=_dr_tele, run_dir=env.run_dir,
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    log_dr.info(
                        f"dispatch_registry: FAILED ({exc}) — no seeds minted",
                        feature=None,
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
        anchored_analyst_skip_active,
        anchored_skip_result,
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
        if s8_mode == "analyst" and anchored_analyst_skip_active(features):
            # Parity with phase_layer2 (W2b debt-pack): the anchored mint
            # in phase_finalize owns the PF layer, so the live scan takes
            # a deterministic pass-through instead of the Sonnet call —
            # replay must take the SAME branch or the artifact diverges.
            log8.info(
                "mode=analyst SKIPPED (anchored-mint path owns the PF "
                "layer) — deterministic pass-through",
            )
            stage_8_result = anchored_skip_result(
                product_features, dev_to_product_map)
        elif s8_mode == "analyst":
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


def _replayed_repo_root(env: ReplayEnv, state: dict[str, Any]) -> Path | None:
    """Recover the source scan's repo root for stages whose captured
    input carries no ctx: read ``repo_path`` from the SOURCE run's
    00-intake input (the replay run dir is minted as a sibling of the
    source run dir, and the runner stamps ``_replayed_from`` with the
    source dir name). ``None`` when unavailable — callers keep the
    repo-root-less historical behavior."""
    from faultline.replay.capture import (
        MissingStageInputError,
        load_stage_input,
    )

    source_name = state.get("_replayed_from")
    if not source_name:
        return None
    try:
        intake = load_stage_input(
            env.run_dir.parent / str(source_name), 0, "intake",
        )
    except (MissingStageInputError, ValueError):
        return None
    repo_path = intake.get("repo_path")
    return Path(repo_path) if repo_path else None


# ── G5 — cross-unit channel recovery ────────────────────────────────────
# The composite runners below replicate phase_finalize / phase_layer2 /
# run.py orchestration blocks that read ORCHESTRATOR LOCALS no capture
# serializes (routes_index at late stages, mint side-channels, e2e
# authored names, bipartite edges …). Two transports, in preference
# order:
#   1. ``state["_chain"]`` — the upstream-produced key set on a chained
#      replay (runner.py G5); live-parity by construction.
#   2. sibling captures of the SOURCE run (the ``_replayed_from`` dir) —
#      the pinned recorded world, used by standalone identity replays
#      (G3 precedent: ``_replayed_repo_root``).
# Every consumer degrades explicitly (guarded fallback + logged) when a
# channel is unrecoverable — a replicated block must never crash a
# replay whose gate does not compare its artifact.


def _chain_get(state: dict[str, Any], key: str, default: Any = None) -> Any:
    chain = state.get("_chain") or {}
    return chain.get(key, default)


def _sibling_input(
    env: ReplayEnv, state: dict[str, Any], index: int, key: str,
) -> dict[str, Any] | None:
    """Load another stage's recorded input from the SOURCE run dir."""
    from faultline.replay.capture import (
        MissingStageInputError,
        load_stage_input,
    )

    source_name = state.get("_replayed_from")
    if not source_name:
        return None
    try:
        return load_stage_input(
            env.run_dir.parent / str(source_name), index, key,
        )
    except MissingStageInputError:
        return None
    except Exception:  # noqa: BLE001 — sibling recovery is best-effort
        return None


def _replay_model_id(
    env: ReplayEnv, state: dict[str, Any], sibling_cache: dict[str, Any],
) -> str:
    """The scan's model id for stages whose capture predates the G5
    persona seams: own state → sibling uf_refiner / flows captures →
    the CLI default (last resort; matches any default-flag baseline)."""
    if state.get("model_id"):
        return str(state["model_id"])
    if "model_id" not in sibling_cache:
        sib = (
            _sibling_input(env, state, 6, "uf_refiner")
            or _sibling_input(env, state, 3, "flows")
            or {}
        )
        sibling_cache["model_id"] = sib.get("model_id")
    if sibling_cache["model_id"]:
        return str(sibling_cache["model_id"])
    from faultline.pipeline_v2.run import DEFAULT_MODEL
    return str(DEFAULT_MODEL)


def _mint_nav_keys(features: list[Any], repo_path: Path | None) -> set[str]:
    """The Stage-6.86 nav-key recipe, verbatim (confirmers are optional;
    any failure ⇒ empty set — same as the live block)."""
    nav_keys: set[str] = set()
    if repo_path is None:
        return nav_keys
    try:
        from faultline.pipeline_v2.product_strings import (
            collect_product_strings,
            normalize_href,
        )
        from faultline.pipeline_v2.spine_anchors import normalize_anchor_key
        candidates: set[str] = set()
        for f in features:
            candidates.update(f.paths or [])
        nav_index = collect_product_strings(repo_path, candidates)
        for pairs in nav_index.nav_pairs_by_file.values():
            for _label, href in pairs:
                if not href:
                    continue
                norm = normalize_href(str(href)) or ""
                for seg in norm.strip("/").split("/"):
                    if seg and not seg.startswith(":"):
                        nav_keys.add(normalize_anchor_key(seg))
                        break
    except Exception:  # noqa: BLE001 — confirmers are optional
        return set()
    return nav_keys


def _mint_meta(scan_meta: dict[str, Any]) -> dict[str, Any]:
    return dict(scan_meta.get("stage_6_86_anchored_mint") or {})


def _anchored_mint_applied(state: dict[str, Any]) -> bool:
    applied = _chain_get(state, "anchored_mint_applied")
    if applied is not None:
        return bool(applied)
    return bool(_mint_meta(state.get("scan_meta") or {}).get("applied"))


def _instrument_channels(
    state: dict[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    """(instrument_dirs, dev_artifact_units) — W4.2/B28 mint channels,
    recovered from the chain else the mint telemetry in scan_meta."""
    dirs = _chain_get(state, "instrument_dirs")
    units = _chain_get(state, "dev_artifact_units")
    if dirs is None or units is None:
        ti = _mint_meta(state.get("scan_meta") or {}).get(
            "technology_instruments") or {}
        if dirs is None:
            dirs = ti.get("dirs") or []
        if units is None:
            units = ti.get("dev_artifact_units") or ()
    return frozenset(dirs), frozenset(units)


def _run_product_thesis(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Stage 0.8 — run.py:469, between the extract phase and the
    reconcile capture. The stage function itself writes both the input
    capture (into the replay dir — the mirror-capture the chain wants)
    and the 06-stage-product_thesis artifact; it never raises."""
    from faultline.pipeline_v2.stage_0_8_product_thesis import run_stage_0_8

    ctx = prepare_ctx(state["ctx"], env)
    run_stage_0_8(ctx, state["stage1_out"], state["repo_class_verdict"])
    return {}


def _run_page_interior(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Stage 6.55 part 1 (phase_finalize:215-248). ``features`` is not
    in the capture: chain → threaded upstream state; standalone → the
    lineage-input sibling (features are untouched between that capture
    and the live 6.55 site — Stage 6.8 only stamps uuids, which the
    span stats never read)."""
    from faultline.pipeline_v2.stage_6_55_page_interior import (
        degenerate_span_stats,
        refine_flow_spans,
        run_stage_6_55,
    )

    ctx = prepare_ctx(state["ctx"], env)
    routes_index = state["routes_index"]
    features = _chain_get(state, "features")
    if features is None:
        sib = _sibling_input(env, state, 6, "lineage") or {}
        features = sib.get("features") or []
    with StageLogger(env.run_dir, 6, "page_interior") as log6_55:
        interior_result = run_stage_6_55(ctx, routes_index, log6_55)
        interior_telemetry: dict[str, Any] = {"active": interior_result.active}
        if interior_result.active:
            interior_telemetry.update(interior_result.telemetry)
            interior_telemetry["degenerate_spans_before"] = (
                degenerate_span_stats(features)
            )
            interior_telemetry["span_refine"] = refine_flow_spans(
                features, interior_result,
            )
        else:
            interior_telemetry["reason"] = interior_result.reason
        write_stage_artifact(
            ctx.repo_path, stage_index=6, stage_name="page_interior",
            payload=interior_telemetry, run_dir=env.run_dir,
        )
    # part 2 (node injection) lives after the Stage-3.5 expansion — see
    # _run_flow_expansion; the telemetry dict + result ride the chain.
    return {
        "features": features,
        "interior_result": interior_result,
        "interior_telemetry": interior_telemetry,
    }


def _run_anchored_mint(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Stage 6.86 + the same-unit W4.3/W4 tails (phase_finalize:535-720):
    anchored mint (artifact) → lane excavation → cross-PF flow-span
    split. The capture is complete for the mint; the excavation's
    ``feature_flow_edges`` are chain-only (guarded [] standalone — the
    excavation runs after the artifact write, so identity is untouched)."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        anchored_mint_enabled,
        run_anchored_mint,
    )

    ctx = prepare_ctx(state["ctx"], env)
    features = state["features"]
    product_features = state["product_features"]
    routes_index = state["routes_index"]
    stage1_out = state["stage1_out"]
    scan_meta = state["scan_meta"]
    repo_path = ctx.repo_path

    anchored_mint_applied = False
    anchored_hub_stamps: dict[str, str] = {}
    instrument_dirs: frozenset[str] = frozenset()
    dev_artifact_units: frozenset[str] = frozenset()
    hh_anchor_registry: dict[str, Any] | None = None
    nav_keys: set[str] = set()
    if anchored_mint_enabled():
        with StageLogger(env.run_dir, 6, "anchored_mint") as log_mint:
            try:
                nav_keys = _mint_nav_keys(features, repo_path)
                mint_pfs, mint_tele = run_anchored_mint(
                    features, routes_index, ctx,
                    extractor_signals=stage1_out,
                    nav_keys=frozenset(nav_keys),
                )
                hh_anchor_registry = mint_tele.pop(
                    "homing_hygiene_anchor_registry", None)
                if mint_tele.get("applied"):
                    product_features = mint_pfs
                    anchored_mint_applied = True
                    anchored_hub_stamps = dict(
                        mint_tele.get("hub_family_stamps") or {})
                    instrument_dirs = frozenset(
                        (mint_tele.get("technology_instruments") or {})
                        .get("dirs") or []
                    )
                    dev_artifact_units = frozenset(
                        (mint_tele.get("technology_instruments") or {})
                        .get("dev_artifact_units") or ()
                    )
                scan_meta["stage_6_86_anchored_mint"] = {
                    k: v for k, v in mint_tele.items()
                    if k != "hub_family_stamps"
                }
                log_mint.info(
                    "anchored_mint (replay): applied=%s anchors=%d pf=%d"
                    % (
                        mint_tele.get("applied"),
                        mint_tele.get("anchors_total", 0),
                        mint_tele.get("pf_minted", 0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path, stage_index=6, stage_name="anchored_mint",
                    payload={k: v for k, v in mint_tele.items()
                             if k != "hub_family_stamps"},
                    run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"anchored-mint failed ({exc}); "
                    f"pre-spine product layer kept"
                )
                log_mint.info(
                    f"anchored_mint: FAILED ({exc}) — old product layer kept",
                    feature=None,
                )

    # W4.3 — lane excavation (same replay unit; log-only, no artifact).
    if anchored_mint_applied:
        from faultline.pipeline_v2.lane_excavation import (
            lane_excavation_enabled,
            run_lane_excavation,
        )
        if lane_excavation_enabled():
            with StageLogger(env.run_dir, 6, "lane_excavation") as log_exc:
                try:
                    exc_tele = run_lane_excavation(
                        features, product_features, routes_index, ctx,
                        extractor_signals=stage1_out,
                        instrument_dirs=instrument_dirs,
                        feature_flow_edges=list(
                            _chain_get(state, "bipartite_edges") or []),
                    )
                    if exc_tele.get("groups"):
                        scan_meta["lane_excavation"] = exc_tele
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    scan_meta.setdefault("warnings", []).append(
                        f"lane-excavation failed ({exc}); lane left as-is"
                    )
                    log_exc.info(
                        f"lane_excavation: FAILED ({exc}) — lane left as-is",
                        feature=None,
                    )

    # W4 — cross-PF flow-attribution split (same replay unit).
    from faultline.pipeline_v2.flow_span_split import (
        flow_span_split_enabled,
        split_cross_pf_flow_attribution,
    )
    if anchored_mint_applied and flow_span_split_enabled():
        try:
            scan_meta["flow_span_split"] = split_cross_pf_flow_attribution(
                features, product_features,
            )
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"flow-span split failed ({exc}); unsplit flow surface kept"
            )

    return {
        "features": features,
        "product_features": product_features,
        "scan_meta": scan_meta,
        "anchored_mint_applied": anchored_mint_applied,
        "anchored_hub_stamps": anchored_hub_stamps,
        "instrument_dirs": instrument_dirs,
        "dev_artifact_units": dev_artifact_units,
        "hh_anchor_registry": hh_anchor_registry,
        "nav_keys": nav_keys,
    }


def _run_journey_lattice(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Stage 6.88 lattice + the post-lattice window it owns in the
    pinned corpus (phase_finalize:1478-1920): e2e_truth / e2e_orphan_uf
    / transport_handoff / mega_pf_nav_rehome / devgrain_demote — their
    artifacts have no input capture of their own and dual_evidence (the
    only capture between) is gated off in the recorded world. CAVEAT: on
    a dual-evidence-armed capture set the live order interleaves
    dual_evidence between the lattice and e2e — this runner then emits
    the window BEFORE the dual_evidence row runs (documented drift; the
    identity gate never compares the window artifacts).

    ``features``/``ctx``/``routes_index``/``model_id`` are not in the
    capture: chain → threaded upstream; standalone → siblings
    (uf_refiner-input features + the ONE features-mutating pass between
    that capture and this one — lane_rehome — replicated below; the
    other window passes are user_flows-only or recorded no-ops)."""
    import re as _re

    user_flows = state["user_flows"]
    product_features = state["product_features"]
    scan_meta = state["scan_meta"]
    sibling_cache: dict[str, Any] = {}

    refiner_sib: dict[str, Any] | None = None
    features = _chain_get(state, "features")
    flows = _chain_get(state, "bipartite_flows")
    ctx = _chain_get(state, "ctx")
    if features is None or flows is None or ctx is None:
        refiner_sib = _sibling_input(env, state, 6, "uf_refiner") or {}
        if features is None:
            features = refiner_sib.get("features") or []
        if flows is None:
            flows = refiner_sib.get("bipartite_flows") or []
        if ctx is None:
            ctx = refiner_sib.get("ctx")
    routes_index = _chain_get(state, "routes_index")
    if routes_index is None:
        sib_pi = _sibling_input(env, state, 6, "page_interior") or {}
        routes_index = sib_pi.get("routes_index") or []
        if ctx is None:
            ctx = sib_pi.get("ctx")
    if ctx is not None:
        ctx = prepare_ctx(ctx, env)
    repo_path = getattr(ctx, "repo_path", None) or _replayed_repo_root(
        env, state) or Path(".")
    model_id = _replay_model_id(env, state, sibling_cache)
    anchored_mint_applied = _anchored_mint_applied(state)
    uf_suppressed = bool(scan_meta.get("uf_suppressed_reason"))
    nav_keys = set(_chain_get(state, "nav_keys") or (
        _mint_nav_keys(features, repo_path) if anchored_mint_applied else set()
    ))

    # W3.2 lane_rehome — the one features-mutating pass between the
    # uf_refiner capture and this one (phase_finalize:1233-1263); its
    # user_flows/PF inputs are pinned by THIS stage's own capture.
    if anchored_mint_applied and not uf_suppressed:
        from faultline.pipeline_v2.lane_rehome import (
            lane_rehome_enabled,
            rehome_uf_cited_lane_devs,
        )
        if lane_rehome_enabled():
            with StageLogger(env.run_dir, 6, "lane_rehome") as log_lr:
                try:
                    lr_tele = rehome_uf_cited_lane_devs(
                        features, product_features, user_flows,
                        list(flows), repo_path=repo_path,
                    )
                    scan_meta["lane_rehome"] = lr_tele
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    log_lr.info(
                        f"lane_rehome: FAILED ({exc}) — lane left as-is",
                        feature=None,
                    )

    # ── Stage 6.88 — journey lattice (phase_finalize:1492-1613) ────────
    from faultline.pipeline_v2.journey_lattice import (
        dedup_lattice_journeys,
        fold_thin_lattice_children,
        journey_lattice_enabled,
        run_journey_lattice,
    )
    from faultline.pipeline_v2.phase_finalize import _recover_uncovered_donors
    if not uf_suppressed and journey_lattice_enabled():
        with StageLogger(env.run_dir, 6, "journey_lattice") as log_jl:
            try:
                interior_result = _chain_get(state, "interior_result")
                if interior_result is None and ctx is not None:
                    # rebuild the 6.55 parse (deterministic, content-
                    # hash cached) — the live object never rides a capture.
                    try:
                        from faultline.pipeline_v2.stage_6_55_page_interior import (
                            run_stage_6_55,
                        )
                        interior_result = run_stage_6_55(
                            ctx, routes_index, log_jl)
                    except Exception:  # noqa: BLE001 — evidence is optional
                        interior_result = None
                _jl_interior = None
                if interior_result is not None and interior_result.active:
                    from faultline.pipeline_v2.stage_6_55_page_interior import (
                        build_interior_evidence as _jl_evidence,
                    )
                    try:
                        _jl_interior = _jl_evidence(
                            interior_result, features, product_features,
                        )
                    except Exception:  # noqa: BLE001 — evidence is optional
                        _jl_interior = None
                _jl_labeler = None
                _jl_verifier = None
                try:
                    from faultline.pipeline_v2.personas import (
                        build_draft_verifier as _jl_bdv,
                        build_pm_labeler as _jl_bpl,
                    )
                    _jl_cache = getattr(ctx, "cache_backend", None)
                    _jl_verifier = _jl_bdv(
                        model_id=model_id,
                        cost_tracker=env.tracker,
                        cache=_jl_cache,
                        llm_health=env.llm_health,
                        log=log_jl,
                    )
                    _jl_labeler = _jl_bpl(
                        model_id=model_id,
                        cost_tracker=env.tracker,
                        cache=_jl_cache,
                        llm_health=env.llm_health,
                        log=log_jl,
                        thesis=scan_meta.get("product_thesis"),
                        verifier=_jl_verifier,
                    )
                except Exception:  # noqa: BLE001 — personas are optional
                    _jl_labeler = None
                    _jl_verifier = None
                jl_tele = run_journey_lattice(
                    user_flows, features, product_features, routes_index,
                    interior_evidence=_jl_interior,
                    labeler=_jl_labeler,
                    verifier=_jl_verifier,
                )
                if jl_tele.get("applied"):
                    from faultline.pipeline_v2.conservation import (
                        apply_uf_conservation as _jl_cons,
                    )
                    jl_tele["conservation_after"] = _jl_cons(
                        user_flows, features, product_features,
                        null_shared_without_signal=True,
                    )
                    jl_tele["dedup_after"] = dedup_lattice_journeys(user_flows)
                    jl_tele["thin_fold_after"] = fold_thin_lattice_children(
                        user_flows, list(flows))
                    _jl_donor = _recover_uncovered_donors(
                        user_flows, features, product_features,
                    )
                    if _jl_donor is not None:
                        jl_tele["donor_backstop_after"] = _jl_donor
                scan_meta["journey_lattice"] = jl_tele
                write_stage_artifact(
                    repo_path, stage_index=6, stage_name="journey_lattice",
                    payload=dict(jl_tele), run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — lattice never breaks a scan
                scan_meta.setdefault("warnings", []).append(
                    f"journey-lattice failed ({exc}); journeys unpartitioned"
                )
                log_jl.info(
                    f"journey_lattice: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── Stage 6.98/6.98b — e2e truth + orphan-journey synthesis ────────
    from faultline.pipeline_v2.e2e_truth import (
        e2e_truth_enabled, matched_authored_names, orphan_uf_enabled,
        run_e2e_truth, scan_meta_view, synthesize_orphan_journeys,
    )
    e2e_authored_names: dict[str, list[str]] = {}
    e2e_payload: dict[str, Any] | None = None
    if e2e_truth_enabled():
        with StageLogger(env.run_dir, 6, "e2e_truth") as log_e2e:
            try:
                e2e_payload = run_e2e_truth(
                    repo_path, user_flows,
                    routes_index=routes_index,
                    flows=list(flows),
                )
                scan_meta["e2e_truth"] = scan_meta_view(e2e_payload)
                write_stage_artifact(
                    repo_path, 6, "e2e_truth", e2e_payload,
                    run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log_e2e.info(
                    f"e2e_truth: FAILED ({exc}) — continuing", feature=None,
                )
    if (e2e_truth_enabled() and orphan_uf_enabled()
            and e2e_payload is not None and not e2e_payload.get("e2e_absent")):
        with StageLogger(env.run_dir, 6, "e2e_orphan_uf") as log_orph:
            try:
                _synth = synthesize_orphan_journeys(
                    e2e_payload, product_features, features,
                    routes_index, user_flows,
                    flows=list(flows),
                )
                minted = _synth["minted"]
                if minted:
                    max_id = 0
                    for uf in user_flows:
                        _m = _re.match(r"^UF-(\d+)$", str(uf.id or ""))
                        if _m:
                            max_id = max(max_id, int(_m.group(1)))
                    fresh = [uf for uf, _titles in minted]
                    fresh.sort(key=lambda u: ((u.name or "").lower(),
                                              str(u.resource or "")))
                    for i, uf in enumerate(fresh, start=1):
                        uf.id = f"UF-{max_id + i:03d}"
                    for uf, _titles in minted:
                        user_flows.append(uf)
                        e2e_authored_names[uf.id] = [uf.name]
                for uid, labels in matched_authored_names(e2e_payload).items():
                    e2e_authored_names.setdefault(uid, list(labels))
                scan_meta["e2e_orphan_uf"] = _synth["tele"]
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log_orph.info(
                    f"e2e_orphan_uf: FAILED ({exc}) — continuing", feature=None,
                )

    # ── Stage 6.985 — transport-lane journey-conservation handoff ──────
    from faultline.pipeline_v2.transport_handoff import (
        run_transport_handoff,
        transport_handoff_enabled,
    )
    _transport_candidates = dict(
        (_mint_meta(scan_meta).get("technology_instruments") or {})
        .get("transport_candidates") or {}
    )
    stage1_out = _chain_get(state, "stage1_out")
    if stage1_out is None and transport_handoff_enabled() and _transport_candidates:
        sib_mint = _sibling_input(env, state, 6, "anchored_mint") or {}
        stage1_out = sib_mint.get("stage1_out") or {}
    if transport_handoff_enabled() and _transport_candidates:
        with StageLogger(env.run_dir, 6, "transport_handoff") as log_th:
            try:
                th_tele = run_transport_handoff(
                    features, product_features, user_flows,
                    list(flows), routes_index,
                    ctx, _transport_candidates,
                    extractor_signals=stage1_out or {},
                    feature_flow_edges=list(
                        _chain_get(state, "bipartite_edges") or []),
                    nav_keys=frozenset(nav_keys),
                )
                scan_meta["transport_handoff"] = th_tele
                write_stage_artifact(
                    repo_path, stage_index=6, stage_name="transport_handoff",
                    payload=dict(th_tele), run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"transport-handoff failed ({exc}); "
                    f"candidate PFs left product (no journey touched)"
                )
                log_th.info(
                    f"transport_handoff: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── Stage 6.986 — mega-PF nav-area re-home (artifact only if triggered) ──
    from faultline.pipeline_v2.mega_pf_nav_rehome import (
        mega_pf_nav_rehome_enabled,
        run_mega_pf_nav_rehome,
    )
    if (mega_pf_nav_rehome_enabled() and anchored_mint_applied
            and not uf_suppressed):
        with StageLogger(env.run_dir, 6, "mega_pf_nav_rehome") as log_b24:
            try:
                b24_tele = run_mega_pf_nav_rehome(
                    features, product_features, user_flows,
                    list(flows), routes_index, ctx,
                    extractor_signals=stage1_out or {},
                    feature_flow_edges=list(
                        _chain_get(state, "bipartite_edges") or []),
                    transport_candidate_units=set(_transport_candidates),
                )
                # S5a: armed_sources exists only in the ARMED world (see
                # phase_finalize) — no-fire armed boards still emit the
                # selection census; unarmed inertness untouched.
                if b24_tele.get("triggered") or b24_tele.get("armed_sources"):
                    scan_meta["mega_pf_nav_rehome"] = b24_tele
                    write_stage_artifact(
                        repo_path, stage_index=6,
                        stage_name="mega_pf_nav_rehome",
                        payload=dict(b24_tele), run_dir=env.run_dir,
                    )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"mega-pf nav re-home failed ({exc}); "
                    f"journeys left untouched"
                )
                log_b24.info(
                    f"mega_pf_nav_rehome: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── Stage 6.987 — devgrain-leaf PF demote (log/scan_meta only) ─────
    from faultline.pipeline_v2.devgrain_demote import (
        fdir_devgrain_gate_enabled,
        run_devgrain_demote,
    )
    if (fdir_devgrain_gate_enabled() and anchored_mint_applied
            and not uf_suppressed):
        with StageLogger(env.run_dir, 6, "devgrain_demote") as log_dg:
            try:
                dg_tele = run_devgrain_demote(
                    features, product_features, user_flows,
                    nav_keys=frozenset(nav_keys),
                )
                scan_meta["devgrain_demote"] = dg_tele
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"devgrain demote failed ({exc}); PFs left untouched"
                )
                log_dg.info(
                    f"devgrain_demote: FAILED ({exc}) — continuing",
                    feature=None,
                )

    return {
        "user_flows": user_flows,
        "product_features": product_features,
        "features": features,
        "scan_meta": scan_meta,
        "e2e_authored_names": e2e_authored_names,
    }


def _run_naming_contract(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    """Stage 6.87 naming contract + the post-naming window it owns
    (phase_finalize:2639-3131): 6.7e adjudicator → dispatch homing →
    synth quality (coverage_gaps) → platform lane → 6.99 i16 rehome →
    6.99b post-UF rehome → cost refresh → uf_loc → flow_loc →
    flow_name_v2 → terminal_classification (all artifact-only rows of
    this unit). Channels not in the capture: features/ctx/routes_index/
    path_index ← chain else the output-input sibling (dev features are
    untouched between this capture and Stage 7 — every pass in the
    window mutates user_flows / PF displays / gaps only);
    product_strings ← recomputed over the user_flows-input sibling
    (captured 3 lines after the live index was built); authored e2e
    names ← chain else the e2e artifact + orphan-tagged UFs;
    ``_hh_anchor_registry`` ← chain only (live mint objects; standalone
    passes None — post-artifact, never gate-compared)."""
    product_features = state["product_features"]
    user_flows = state["user_flows"]
    flows = state["bipartite_flows"]
    prev_scan_json = state.get("prev_scan_json")
    scan_meta = state["scan_meta"]
    sibling_cache: dict[str, Any] = {}

    features = _chain_get(state, "features")
    ctx = _chain_get(state, "ctx")
    routes_index = _chain_get(state, "routes_index")
    path_index = _chain_get(state, "path_index")
    if features is None or ctx is None or routes_index is None or path_index is None:
        out_sib = _sibling_input(env, state, 7, "output") or {}
        if features is None:
            features = out_sib.get("features") or []
        if ctx is None:
            ctx = out_sib.get("ctx")
        if routes_index is None:
            routes_index = out_sib.get("routes_index") or []
        if path_index is None:
            path_index = out_sib.get("path_index") or {}
    if ctx is not None:
        ctx = prepare_ctx(ctx, env)
    repo_path = getattr(ctx, "repo_path", None) or _replayed_repo_root(
        env, state) or Path(".")
    model_id = _replay_model_id(env, state, sibling_cache)
    anchored_mint_applied = _anchored_mint_applied(state)
    uf_suppressed = bool(scan_meta.get("uf_suppressed_reason"))
    instrument_dirs, dev_artifact_units = _instrument_channels(state)

    # product_strings — the live index is built ONCE before Stage 6.7
    # over the pre-UF feature/flow member sets; the user_flows capture
    # (written 3 lines later) pins exactly that world.
    product_strings = _chain_get(state, "product_strings")
    if product_strings is None:
        uf_sib = _sibling_input(env, state, 6, "user_flows") or {}
        ps_features = uf_sib.get("features") or []
        ps_flows = uf_sib.get("bipartite_flows") or []
        try:
            product_strings = _product_strings_for(
                Path(repo_path), ps_features, ps_flows)
        except Exception:  # noqa: BLE001 — evidence is optional
            product_strings = None

    # Track-C authored names — chain else reconstructed: matched names
    # from the recorded e2e artifact, minted-orphan names from the
    # synthesis_reason-tagged UFs already in THIS capture.
    e2e_authored_names = _chain_get(state, "e2e_authored_names")
    if e2e_authored_names is None:
        e2e_authored_names = {}
        try:
            from faultline.pipeline_v2.e2e_truth import (
                E2E_ORPHAN_REASON,
                matched_authored_names,
            )
            for uf in user_flows:
                if getattr(uf, "synthesis_reason", None) == E2E_ORPHAN_REASON:
                    e2e_authored_names[uf.id] = [uf.name]
            source_name = state.get("_replayed_from")
            if source_name:
                import json as _json
                e2e_art = (env.run_dir.parent / str(source_name)
                           / "06-stage-e2e_truth.json")
                if e2e_art.exists():
                    payload = _json.loads(e2e_art.read_text(encoding="utf-8"))
                    for uid, labels in matched_authored_names(payload).items():
                        e2e_authored_names.setdefault(uid, list(labels))
        except Exception:  # noqa: BLE001 — authored channel is optional
            pass

    from faultline.pipeline_v2.naming_contract import (
        naming_contract_enabled,
        run_naming_contract,
    )
    from faultline.pipeline_v2.uf_identity_keeper import keeper_enabled
    if naming_contract_enabled():
        with StageLogger(env.run_dir, 7, "naming_contract") as log_nc:
            try:
                _nc_labeler = None
                _nc_verifier = None
                try:
                    from faultline.pipeline_v2.personas import (
                        build_draft_verifier,
                        build_pm_labeler,
                    )
                    _nc_cache = getattr(ctx, "cache_backend", None)
                    _nc_verifier = build_draft_verifier(
                        model_id=model_id,
                        cost_tracker=env.tracker,
                        cache=_nc_cache,
                        llm_health=env.llm_health,
                        log=log_nc,
                    )
                    _nc_labeler = build_pm_labeler(
                        model_id=model_id,
                        cost_tracker=env.tracker,
                        cache=_nc_cache,
                        llm_health=env.llm_health,
                        log=log_nc,
                        thesis=scan_meta.get("product_thesis"),
                        verifier=_nc_verifier,
                    )
                except Exception:  # noqa: BLE001 — persona is optional
                    _nc_labeler = None
                    _nc_verifier = None
                nc_tele = run_naming_contract(
                    product_features,
                    user_flows,
                    list(flows),
                    prev_scan=prev_scan_json,
                    keeper_on=keeper_enabled(),
                    product_strings=product_strings,
                    routes_index=routes_index,
                    uf_authored_names=e2e_authored_names,
                    labeler=_nc_labeler,
                    verifier=_nc_verifier,
                    repo_root=repo_path,
                )
                scan_meta["naming_contract"] = nc_tele
                write_stage_artifact(
                    repo_path, stage_index=7, stage_name="naming_contract",
                    payload=dict(nc_tele), run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — naming must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"naming-contract failed ({exc}); displays unpolished"
                )
                log_nc.info(
                    f"naming_contract: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── Stage 6.7e — journey-evidence adjudicator (keyed-only) ─────────
    from faultline.pipeline_v2.stage_6_7e_adjudicator import (
        adjudicator_6_7e_enabled,
        run_stage_6_7e,
    )
    adjudicated_gaps: list[Any] = []
    if adjudicator_6_7e_enabled():
        try:
            adj_tele, adjudicated_gaps = run_stage_6_7e(
                user_flows,
                list(flows),
                product_features,
                repo_root=repo_path,
                product_strings=product_strings,
                routes_index=routes_index,
                uf_authored_names=e2e_authored_names,
                keeper_on=keeper_enabled(),
                model_id=model_id,
                cost_tracker=env.tracker,
                cache=getattr(ctx, "cache_backend", None),
                llm_health=env.llm_health,
            )
            if adj_tele.get("ran"):
                scan_meta["adjudicator_6_7e"] = adj_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            adjudicated_gaps = []
            scan_meta.setdefault("warnings", []).append(
                f"stage-6.7e adjudicator failed ({exc}); journeys unchanged")

    # ── Stage 6.985d — dispatch-mint homing ────────────────────────────
    from faultline.pipeline_v2.dispatch_homing import (
        dispatch_homing_enabled,
        home_dispatch_mints,
    )
    if dispatch_homing_enabled():
        try:
            dh_tele = home_dispatch_mints(
                user_flows, features, product_features,
                path_index=path_index)
            if dh_tele.get("rehomed"):
                scan_meta["dispatch_homing"] = dh_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"dispatch-homing failed ({exc}); UF homes left as-is")

    # ── Stage 6.98 — synth quality (+ typed coverage-gap channel) ──────
    from faultline.pipeline_v2.synth_quality import (
        run_synth_quality,
        synth_quality_enabled,
    )
    coverage_gaps: list[Any] | None = None
    if synth_quality_enabled():
        try:
            _sq_tele = run_synth_quality(
                user_flows,
                list(flows),
                product_features,
                scan_meta,
                developer_features=features,
            )
            _gaps = _sq_tele.get("coverage_gaps")
            if _gaps is not None:
                coverage_gaps = list(_gaps)
        except Exception as exc:  # noqa: BLE001 — quality pass never breaks a scan
            scan_meta.setdefault("warnings", []).append(
                f"synth-quality pass failed ({exc}); journeys unchanged"
            )
    if adjudicated_gaps:
        coverage_gaps = list(coverage_gaps or []) + list(adjudicated_gaps)
        coverage_gaps.sort(key=lambda g: (
            str(getattr(g, "product_feature_id", "") or ""),
            str(getattr(g, "id", "") or ""),
        ))

    # ── platform_infrastructure[] lane ──────────────────────────────────
    platform_infrastructure: list[dict[str, Any]] | None = None
    if anchored_mint_applied:
        from faultline.pipeline_v2.stage_6_86_anchored_mint import (
            build_platform_infrastructure_lane,
        )
        try:
            platform_infrastructure = build_platform_infrastructure_lane(
                features, user_flows=user_flows)
            scan_meta.setdefault("stage_6_86_anchored_mint", {})[
                "platform_infrastructure_rows"
            ] = len(platform_infrastructure)
        except Exception as exc:  # noqa: BLE001 — lane must never break a scan
            platform_infrastructure = []
            scan_meta.setdefault("warnings", []).append(
                f"platform-infrastructure lane failed ({exc}); lane empty"
            )

    # ── Stage 6.99 — i16 journey re-home ────────────────────────────────
    from faultline.pipeline_v2.stage_6_99_i16_rehome import (
        i16_rehome_enabled,
        rehome_foreign_entry_ufs,
    )
    if i16_rehome_enabled():
        try:
            rh_tele = rehome_foreign_entry_ufs(
                user_flows, features, product_features,
                path_index, platform_infrastructure)
            if rh_tele.get("rehomed"):
                scan_meta["i16_rehome"] = rh_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"i16-rehome failed ({exc}); UF homes left as-is")

    # ── Stage 6.99b — post-UF PF-homing hygiene ─────────────────────────
    from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
        homing_hygiene_enabled as _hh_enabled,
        run_post_uf_rehome,
    )
    if _hh_enabled():
        try:
            _hh_tele = run_post_uf_rehome(
                user_flows, features, product_features,
                _chain_get(state, "hh_anchor_registry"))
            scan_meta["post_uf_rehome"] = _hh_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"post-uf-rehome failed ({exc}); UF homes left as-is")

    # W3 rider — full-bill LLM cost refresh (fresh replay tracker).
    scan_meta["cost_usd"] = round(env.tracker.total_cost_usd, 4)
    scan_meta["calls"] = env.tracker.call_count

    # ── Stage 6.97b — journey-level LOC ─────────────────────────────────
    from faultline.pipeline_v2.stage_6_97b_uf_loc import (
        apply_uf_loc,
        uf_loc_enabled,
    )
    if uf_loc_enabled():
        with StageLogger(env.run_dir, 7, "uf_loc") as log_ufloc:
            try:
                _uf_loc_tele = apply_uf_loc(user_flows, list(flows))
                write_stage_artifact(
                    repo_path, stage_index=7, stage_name="uf_loc",
                    payload=_uf_loc_tele, run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — metric must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"uf-loc stage failed ({exc}); user_flows[].loc left unset"
                )
                log_ufloc.info(
                    f"uf_loc: FAILED ({exc}) — continuing without uf loc",
                    feature=None,
                )

    # ── Stage 6.97c — flow-level OWNED/SHARED LOC ───────────────────────
    from faultline.pipeline_v2.stage_6_97c_flow_loc import (
        apply_flow_loc,
        flow_loc_enabled,
    )
    if flow_loc_enabled():
        with StageLogger(env.run_dir, 7, "flow_loc") as log_flowloc:
            try:
                _flow_loc_tele = apply_flow_loc(list(flows))
                write_stage_artifact(
                    repo_path, stage_index=7, stage_name="flow_loc",
                    payload=_flow_loc_tele, run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — metric must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flow-loc stage failed ({exc}); flows[].loc left unset"
                )
                log_flowloc.info(
                    f"flow_loc: FAILED ({exc}) — continuing without flow loc",
                    feature=None,
                )

    # ── B30 — deterministic verb+resource flow naming ───────────────────
    from faultline.pipeline_v2.flow_name_v2 import (
        apply_flow_name_v2,
        flow_name_v2_enabled,
    )
    if flow_name_v2_enabled():
        with StageLogger(env.run_dir, 7, "flow_name_v2") as log_fnv2:
            try:
                _fnv2_tele = apply_flow_name_v2(
                    list(flows),
                    routes_index=routes_index,
                    repo_path=repo_path,
                )
                write_stage_artifact(
                    repo_path, stage_index=7, stage_name="flow_name_v2",
                    payload=_fnv2_tele, run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — naming must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flow-name-v2 stage failed ({exc}); flow names left as-is"
                )
                log_fnv2.info(
                    f"flow_name_v2: FAILED ({exc}) — continuing with old names",
                    feature=None,
                )

    # ── Stage 6.995 — B68 terminal 4-way classification ─────────────────
    from faultline.pipeline_v2.terminal_classification import (
        run_terminal_classification,
        terminal_classification_enabled,
    )
    if terminal_classification_enabled() and coverage_gaps is not None:
        try:
            _tc_tele = run_terminal_classification(
                coverage_gaps,
                product_features,
                features,
                scan_meta,
                dev_artifact_units=dev_artifact_units,
                instrument_dirs=instrument_dirs,
                repo_path=repo_path,
            )
            write_stage_artifact(
                repo_path, stage_index=7,
                stage_name="terminal_classification",
                payload=_tc_tele, run_dir=env.run_dir,
            )
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"terminal-classification stage failed ({exc}); "
                f"coverage_gaps left as-is"
            )

    return {
        "product_features": product_features,
        "user_flows": user_flows,
        "scan_meta": scan_meta,
        "coverage_gaps": coverage_gaps,
        "platform_infrastructure": platform_infrastructure,
    }


def _run_vendor_connector_split(
    env: ReplayEnv, state: dict[str, Any],
) -> dict[str, Any]:
    from faultline.pipeline_v2.hub_relation import detect_hub_relations
    from faultline.pipeline_v2.stage_8_9_7_vendor_connector_split import (
        split_vendor_connectors,
    )

    features = state["features"]
    product_features = state["product_features"]
    with StageLogger(env.run_dir, 8, "vendor_connector_split") as log8_9_7:
        # Parity with phase_layer2 (Product-Spine §4.4 + W1.1 + D4 husk
        # floor): the live site arms hub_dirs / carve_hub_dirs from
        # detect_hub_relations(include_memberless=True) and passes the
        # repo root — replaying the bare stem-rule call diverges.
        hub_relations = detect_hub_relations(features, include_memberless=True)
        vendor_split_result = split_vendor_connectors(
            features,
            hub_dirs=tuple(
                h.hub_dir for h in hub_relations if h.member_dev_names
            ),
            carve_hub_dirs=tuple(
                h.hub_dir for h in hub_relations if not h.member_dev_names
            ),
            repo_root=_replayed_repo_root(env, state),
        )
        log8_9_7.info(
            f"vendor_connector_split enabled={vendor_split_result.enabled}",
        )
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="vendor_connector_split",
            payload=vendor_split_result.as_telemetry(),
            run_dir=env.run_dir,
        )

    # ── Stage 8.9.8 — hub/child PF binding (G5; phase_layer2:772-793) ──
    # Same replay unit (between the vendor-split and pf_hotspots input
    # captures); artifact-only row. ``dev_to_product_map`` is chain-only
    # (nonsource_drop produces the live-parity map) — standalone falls
    # back to the devs' stamped product_feature_id fields (the same
    # ownership truth the finalize conservation pass reads); identity
    # never compares this artifact.
    from faultline.pipeline_v2.hub_relation import apply_hub_pf_binding

    dev_to_product_map = _chain_get(state, "dev_to_product_map")
    if dev_to_product_map is None:
        dev_to_product_map = {
            f.name: (f.product_feature_id,)
            for f in features
            if getattr(f, "product_feature_id", None)
        }
    with StageLogger(env.run_dir, 8, "hub_pf_binding") as log8_9_8:
        hub_binding_telemetry = apply_hub_pf_binding(
            features, product_features, dev_to_product_map,
        )
        log8_9_8.info(
            f"hub_pf_binding enabled={hub_binding_telemetry['enabled']} "
            f"hubs={hub_binding_telemetry['hubs']} "
            f"devs_rebound={hub_binding_telemetry['devs_rebound']} "
            f"pfs_minted={hub_binding_telemetry['pfs_minted']}",
        )
        write_stage_artifact(
            Path(state.get("repo_path", ".")), stage_index=8,
            stage_name="hub_pf_binding",
            payload=hub_binding_telemetry,
            run_dir=env.run_dir,
        )
    return {
        "features": features,
        "product_features": product_features,
        "dev_to_product_map": dev_to_product_map,
    }


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

    # ── Stage 6.55 part 2 (G5; phase_finalize:332-346) — interior nodes
    # onto the expanded graph. ``interior_result`` is a live object the
    # page_interior runner threads via the chain; standalone
    # flow_expansion replay has none (the live artifact was written in
    # part 1 — identity is untouched; only chain state fidelity gains).
    interior_result = _chain_get(state, "interior_result")
    interior_telemetry = _chain_get(state, "interior_telemetry")
    if interior_result is not None and interior_result.active:
        from faultline.pipeline_v2.stage_6_55_page_interior import (
            degenerate_span_stats,
            inject_interior_nodes,
        )
        if interior_telemetry is None:
            interior_telemetry = {"active": True}
        interior_telemetry["node_inject"] = inject_interior_nodes(
            features, interior_result,
        )
        interior_telemetry["degenerate_spans_after"] = (
            degenerate_span_stats(features)
        )
        scan_meta["stage_6_55_page_interior"] = dict(interior_telemetry)

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
            generated_strip_telemetry = strip_generated_paths(
                features, flows, repo_root=state.get("repo_path"))
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
        try:
            if align_enabled():
                from faultline.pipeline_v2.anchor_extractors import (
                    build_alignment_pool, extract_raw_anchors,
                )
                _anchors = build_alignment_pool(extract_raw_anchors(repo_path))
        except Exception:  # noqa: BLE001 — anchors are optional
            _anchors = None
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
    # first_draw_spec is artifact/llm-cache-only observability (mission-92)
    # -- filtered here so replayed scan_meta matches the live-run shape.
    scan_meta["stage_6_7d_journey_abstraction"] = {
        k: v for k, v in s67d_telemetry.items() if k != "first_draw_spec"
    }
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

    # ── G5 — same-unit tails (phase_finalize:2138-2278) ────────────────
    # uf_identity: gated on an EXPLICIT prev_scan_json which no capture
    # in this unit carries — a prev-scan-armed source run would need its
    # own uf_identity capture (it has one; a registered row can join
    # when such a baseline exists). Skipped here exactly like a live
    # scan without --prev-scan.
    # file_lane + B15 shared-leaf consistency: artifact-only window
    # between this capture and feature_loc-input. product_features /
    # routes_index are chain-only channels; standalone falls back to the
    # history-input / page_interior-input siblings (identity never
    # compares the file_lane artifact).
    from faultline.pipeline_v2.file_lane import (
        enforce_shared_leaf_consistency,
        file_lane_enabled,
        run_file_lane_infra,
        shared_leaf_consistency_enabled,
    )

    anchored_mint_applied = _anchored_mint_applied(state)
    product_features = _chain_get(state, "product_features")
    if product_features is None:
        product_features = (
            _sibling_input(env, state, 6, "history") or {}
        ).get("product_features") or []
    routes_index = _chain_get(state, "routes_index")
    if routes_index is None:
        routes_index = (
            _sibling_input(env, state, 6, "page_interior") or {}
        ).get("routes_index") or []
    if file_lane_enabled() and anchored_mint_applied:
        with StageLogger(env.run_dir, 6, "file_lane") as log_fl:
            try:
                fl_tele = run_file_lane_infra(
                    features, product_features, routes_index, ctx,
                )
                scan_meta["file_lane"] = fl_tele
                write_stage_artifact(
                    ctx.repo_path, stage_index=6, stage_name="file_lane",
                    payload=dict(fl_tele), run_dir=env.run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — lane must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"file-lane failed ({exc}); no infra reclassified"
                )
                log_fl.info(
                    f"file_lane: FAILED ({exc}) — continuing", feature=None)
    if shared_leaf_consistency_enabled() and anchored_mint_applied:
        try:
            slc_tele = enforce_shared_leaf_consistency(
                features, product_features, routes_index)
            scan_meta["shared_leaf_consistency"] = slc_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"shared-leaf-consistency failed ({exc}); no roles changed")

    return {
        "monorepo_view": monorepo_view,
        "scan_meta": scan_meta,
        "features": features,
        "product_features": product_features,
    }


def _run_feature_loc(env: ReplayEnv, state: dict[str, Any]) -> dict[str, Any]:
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    ctx = prepare_ctx(state["ctx"], env)
    scan_meta = state["scan_meta"]
    features = state["features"]
    product_features = state["product_features"]
    # §4.5 flow-accounting inputs — present in captures from perf wave 2
    # onward; ``None`` on OLD captures reproduces the pre-conservation
    # behaviour those captures recorded (drift fix, see phase_finalize).
    user_flows = state.get("user_flows")
    bipartite_flows = state.get("bipartite_flows")
    with StageLogger(env.run_dir, 6, "feature_loc") as log697:
        try:
            loc_telemetry = apply_feature_loc(
                features, product_features, ctx.repo_path,
                user_flows=user_flows,
                flows=bipartite_flows,
            )
            scan_meta["feature_loc"] = loc_telemetry
            if loc_telemetry.get("loc_accounting"):
                scan_meta["loc_accounting"] = loc_telemetry["loc_accounting"]
            # B59 — artifact-ink lane aggregate (absent when the flag is OFF).
            if loc_telemetry.get("artifact_ink"):
                scan_meta["artifact_ink"] = loc_telemetry["artifact_ink"]
            write_stage_artifact(
                ctx.repo_path, stage_index=6, stage_name="feature_loc",
                payload=loc_telemetry,
                run_dir=env.run_dir,
            )
        except Exception as exc:  # noqa: BLE001 — metric must never break a scan
            scan_meta["feature_loc"] = {"enabled": False, "error": str(exc)}
            log697.info(f"feature_loc: FAILED ({exc})", feature=None)

    # ── G5 — same-unit tails (phase_finalize:2358-2625) ────────────────
    # B15b data-leaf rail → flowless-shell resolution → surface-taxonomy
    # emission (artifact) → W5.1 loc-worthy backstop → emission
    # integrity (artifact) + path_index refresh + terminal home. All sit
    # between the feature_loc and naming_contract input captures.
    # routes_index / path_index / model_id are chain-else-sibling
    # channels; identity compares only the feature_loc artifact above.
    sibling_cache: dict[str, Any] = {}
    anchored_mint_applied = _anchored_mint_applied(state)
    uf_suppressed = bool(scan_meta.get("uf_suppressed_reason"))
    instrument_dirs, dev_artifact_units = _instrument_channels(state)
    routes_index = _chain_get(state, "routes_index")
    if routes_index is None:
        routes_index = (
            _sibling_input(env, state, 6, "page_interior") or {}
        ).get("routes_index") or []
    repo_path = ctx.repo_path

    from faultline.pipeline_v2.file_lane import (
        data_leaf_enabled,
        enforce_data_leaf_shared,
    )
    if data_leaf_enabled() and anchored_mint_applied:
        try:
            _repo_loc = (scan_meta.get("loc_accounting") or {}).get("repo_loc")
            dl_tele = enforce_data_leaf_shared(
                features, product_features, routes_index, _repo_loc)
            scan_meta["data_leaf"] = dl_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"data-leaf rail failed ({exc}); no roles changed")

    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _shell_absorb_enabled,
        resolve_flowless_shells,
    )
    if (_shell_absorb_enabled()
            and not anchored_mint_applied
            and (scan_meta.get("stage_6_7d_journey_abstraction") or {}).get("applied")):
        with StageLogger(env.run_dir, 6, "flowless_shells") as log_shell:
            try:
                product_features, shell_tele = resolve_flowless_shells(
                    features, product_features)
                scan_meta["flowless_shells"] = shell_tele
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flowless-shell resolution failed ({exc}); shells left as-is")
                log_shell.info(
                    f"flowless_shells: FAILED ({exc}) — continuing", feature=None)

    # ── Stage 6.85 — surface-taxonomy emission lane (artifact) ─────────
    from faultline.pipeline_v2.surface_taxonomy import apply_emission_taxonomy
    non_product_surfaces: list[dict[str, Any]] = []
    with StageLogger(env.run_dir, 6, "surface_taxonomy") as log_st:
        try:
            _st_adjudicator = None
            try:
                from faultline.pipeline_v2.personas import (
                    build_surface_adjudicator,
                )
                _st_adjudicator = build_surface_adjudicator(
                    model_id=_replay_model_id(env, state, sibling_cache),
                    cost_tracker=env.tracker,
                    cache=getattr(ctx, "cache_backend", None),
                    llm_health=env.llm_health,
                    log=log_st,
                    thesis=scan_meta.get("product_thesis"),
                )
            except Exception:  # noqa: BLE001 — persona is optional
                _st_adjudicator = None
            st_tele, non_product_surfaces, product_features = (
                apply_emission_taxonomy(
                    features, product_features, user_flows or [],
                    list(bipartite_flows or []), routes_index,
                    repo_path=repo_path,
                    adjudicator=_st_adjudicator,
                    instrument_dirs=instrument_dirs,
                    dev_artifact_units=dev_artifact_units,
                )
            )
            scan_meta["surface_taxonomy_emission"] = st_tele
            write_stage_artifact(
                ctx.repo_path, stage_index=6, stage_name="surface_taxonomy",
                payload={**st_tele, "non_product_surfaces": [
                    {k: v for k, v in e.items() if k != "user_flows"}
                    for e in non_product_surfaces
                ]},
                run_dir=env.run_dir,
            )
        except Exception as exc:  # noqa: BLE001 — taxonomy must never break a scan
            non_product_surfaces = []
            scan_meta.setdefault("warnings", []).append(
                f"surface-taxonomy emission failed ({exc}); "
                f"lane left empty, tags may be partial"
            )
            log_st.info(
                f"surface_taxonomy: FAILED ({exc}) — continuing", feature=None,
            )

    # ── W5.1 — LOC-worthy PF backstop ───────────────────────────────────
    from faultline.pipeline_v2.phase_finalize import _recover_uncovered_donors
    if not uf_suppressed:
        _lw_tele = _recover_uncovered_donors(
            user_flows or [], features, product_features, loc_only=True,
        )
        if _lw_tele is not None:
            scan_meta["loc_worthy_backstop"] = _lw_tele

    # ── Emission integrity (artifact) + path_index refresh + terminal home ──
    from faultline.pipeline_v2.emission_integrity import (
        enforce_emission_integrity,
    )
    path_index = _chain_get(state, "path_index")
    with StageLogger(env.run_dir, 7, "emission_integrity") as log_ei:
        features, product_features, ei_result = enforce_emission_integrity(
            features, product_features, user_flows or [],
            list(bipartite_flows or []),
        )
        scan_meta["emission_integrity"] = ei_result.as_dict()
        write_stage_artifact(
            ctx.repo_path, stage_index=7, stage_name="emission_integrity",
            payload=ei_result.as_dict(), run_dir=env.run_dir,
        )
        # W1.1 path_index emission refresh — the stale index is a chain
        # channel (lineage runner output); standalone feature_loc replay
        # has no index to refresh (stats skipped, artifact unaffected).
        if path_index is not None:
            from faultline.pipeline_v2.indexes import build_path_index

            _stale_index = path_index
            path_index = build_path_index(
                [{"uuid": f.uuid, "paths": list(f.paths)} for f in features],
                [{"uuid": fl.uuid, "paths": list(fl.paths)}
                 for fl in (bipartite_flows or [])],
            )
            scan_meta["emission_integrity"]["path_index_refresh"] = {
                "entries_before": len(_stale_index),
                "entries_after": len(path_index),
                "owners_changed": sum(
                    1 for p, e in path_index.items()
                    if (_stale_index.get(p) or {}).get("feature_uuid")
                    != e.get("feature_uuid")
                ),
            }
        from faultline.pipeline_v2.uf_terminal_home import (
            assign_terminal_homes,
            terminal_home_enabled,
        )
        if terminal_home_enabled():
            th_tele = assign_terminal_homes(
                user_flows or [], features, product_features,
            )
            scan_meta["uf_terminal_home"] = th_tele

    out: dict[str, Any] = {
        "features": features,
        "product_features": product_features,
        "scan_meta": scan_meta,
        "non_product_surfaces": non_product_surfaces,
        "user_flows": user_flows,
    }
    if path_index is not None:
        out["path_index"] = path_index
    return out


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
            # G5 — the late lanes ride the capture; coverage_gaps are
            # never captured (chain-only, from the naming-contract unit).
            non_product_surfaces=state.get("non_product_surfaces"),
            platform_infrastructure=state.get("platform_infrastructure"),
            coverage_gaps=_chain_get(state, "coverage_gaps"),
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
    # G5 — Stage 0.8 (run.py, between the extract phase and the
    # reconcile capture); the stage writes its own capture + artifact.
    StageSpec("product_thesis", 6, 5, _run_product_thesis),
    StageSpec("reconcile", 2, 6, _run_reconcile),
    StageSpec("membership_closure", 2, 7, _run_membership_closure),
    StageSpec("flows", 3, 8, _run_flows, llm_cache_dir="flows"),
    # artifact-only: emitted by _run_flows (the run.py B34 block sits
    # inside the flows replay unit — no input capture of its own).
    StageSpec("lazy_imports", 3, 9, _artifact_only_stage, artifact_only=True),
    StageSpec("dispatch_registry", 3, 10, _artifact_only_stage,
              artifact_only=True),
    StageSpec("residual", 4, 11, _run_residual, llm_cache_dir="residual"),
    StageSpec("postprocess", 5, 12, _run_postprocess),
    StageSpec("sibling_collapse", 5, 13, _run_sibling_collapse),
    StageSpec("cross_flow_dedup", 5, 14, _run_cross_flow_dedup),
    StageSpec("bipartite", 5, 15, _run_bipartite),
    StageSpec("metrics", 6, 16, _run_metrics),
    StageSpec("product_clusterer", 6, 17, _run_product_clusterer),
    StageSpec("import_tree", 6, 18, _run_import_tree),
    StageSpec("framework_enrich", 6, 19, _run_framework_enrich),
    StageSpec("branch_slicer", 6, 20, _run_branch_slicer),
    StageSpec(
        "marketing_clusterer", 8, 21, _run_marketing_clusterer,
        llm_cache_dir="product-cluster",
    ),
    StageSpec("rollup", 8, 22, _run_rollup),
    StageSpec("member_backfill", 8, 23, _run_member_backfill),
    StageSpec("nonsource_drop", 8, 24, _run_nonsource_drop),
    StageSpec("scaffold_filter", 8, 25, _run_scaffold_filter),
    StageSpec("di_attribution", 8, 26, _run_di_attribution),
    StageSpec("anchor_desink", 8, 27, _run_anchor_desink),
    StageSpec("shared_members", 8, 28, _run_shared_members),
    StageSpec("anchor_subdecompose", 8, 29, _run_anchor_subdecompose),
    StageSpec(
        "llm_component_split", 8, 30, _run_llm_component_split,
        llm_cache_dir="llm-component-split",
    ),
    StageSpec("domain_member_attribution", 8, 31, _run_domain_member_attribution),
    # optional: recorded runs that predate the stage have no input artifact
    # for it — replay chains over them must skip it silently.
    StageSpec("vendor_connector_split", 8, 32, _run_vendor_connector_split,
              optional=True),
    # G5 — artifact-only: emitted by _run_vendor_connector_split (the
    # phase_layer2 8.9.8 block sits inside the vendor-split replay unit).
    StageSpec("hub_pf_binding", 8, 33, _artifact_only_stage,
              artifact_only=True),
    StageSpec("pf_hotspots", 8, 34, _run_pf_hotspots, connector=True),
    StageSpec("lineage", 6, 35, _run_lineage, connector=True),
    # G5 — Stage 6.55 part 1 (between the lineage and flow_expansion
    # captures; UPSTREAM of test_strip in the mutation gate).
    StageSpec("page_interior", 6, 36, _run_page_interior),
    StageSpec("flow_expansion", 3, 37, _run_flow_expansion),
    StageSpec("test_strip", 6, 38, _run_test_strip),
    StageSpec("generated_strip", 6, 39, _run_generated_strip),
    # G5 — Stage 6.86 + same-unit W4.3/W4 tails; capture is flag-gated
    # (FAULTLINE_SPINE_ANCHORED_MINT) → optional.
    StageSpec("anchored_mint", 6, 40, _run_anchored_mint, optional=True),
    StageSpec("user_flows", 6, 41, _run_user_flows),
    StageSpec("uf_splitter", 6, 42, _run_uf_splitter, llm_cache_dir="uf-split"),
    StageSpec("uf_refiner", 6, 43, _run_uf_refiner, llm_cache_dir="uf-refine"),
    StageSpec(
        "journey_abstraction", 6, 44, _run_journey_abstraction,
        optional=True, llm_cache_dir="abstraction",
    ),
    # G5 — Stage 6.88 + the post-lattice window (e2e / transport / mega /
    # devgrain emissions); capture is flag-gated → optional.
    StageSpec("journey_lattice", 6, 45, _run_journey_lattice, optional=True),
    # G5 — artifact-only rows of the journey_lattice replay unit.
    StageSpec("e2e_truth", 6, 46, _artifact_only_stage, artifact_only=True),
    StageSpec("transport_handoff", 6, 47, _artifact_only_stage,
              artifact_only=True),
    StageSpec("mega_pf_nav_rehome", 6, 48, _artifact_only_stage,
              artifact_only=True),
    StageSpec("dual_evidence", 6, 49, _run_dual_evidence,
              optional=True, connector=True),
    StageSpec("history", 6, 50, _run_history),
    StageSpec("impact", 6, 51, _run_impact),
    StageSpec("monorepo_assembly", 6, 52, _run_monorepo_assembly),
    # G5 — artifact-only: emitted by _run_monorepo_assembly (between the
    # monorepo_assembly and feature_loc captures).
    StageSpec("file_lane", 6, 53, _artifact_only_stage, artifact_only=True),
    # optional: recorded runs that predate Stage 6.97 have no input
    # artifact — replay chains over them must skip it silently.
    StageSpec("feature_loc", 6, 54, _run_feature_loc, optional=True),
    # G5 — artifact-only rows of the feature_loc replay unit.
    StageSpec("surface_taxonomy", 6, 55, _artifact_only_stage,
              artifact_only=True),
    StageSpec("emission_integrity", 7, 56, _artifact_only_stage,
              artifact_only=True),
    # G5 — Stage 6.87 + the post-naming window (uf_loc / flow_loc /
    # flow_name_v2 / terminal_classification emissions); capture is
    # flag-gated (FAULTLINE_NAMING_CONTRACT) → optional.
    StageSpec("naming_contract", 7, 57, _run_naming_contract, optional=True),
    # G5 — artifact-only rows of the naming_contract replay unit.
    StageSpec("uf_loc", 7, 58, _artifact_only_stage, artifact_only=True),
    StageSpec("flow_loc", 7, 59, _artifact_only_stage, artifact_only=True),
    StageSpec("flow_name_v2", 7, 60, _artifact_only_stage,
              artifact_only=True),
    StageSpec("terminal_classification", 7, 61, _artifact_only_stage,
              artifact_only=True),
    StageSpec("output", 7, 62, _run_output),
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
