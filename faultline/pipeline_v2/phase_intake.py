"""Repo-level INTAKE phase — Stage 0 intake / 0.5 auditor / 0.6 shape.

Extracted from ``run.py`` (refactor/run-decomposition) as straight-line
code — same stage order, same StageLogger stage indexes/names, same
artifact filenames, same deep-copy boundaries (via ``run._isolate``).

This phase runs ONCE per repository. A future
``engine(repo, subpaths[])`` will call it once and then run the
per-tree pipeline phase (Stages 1→8 + output) per selected subpath —
see the PHASE SEAM marker in :mod:`faultline.pipeline_v2.run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.pipeline_v2.git_snapshot import GitSnapshot
    from faultline.pipeline_v2.llm_health import LlmHealth

from faultline.llm.cost import CostTracker
from faultline.replay.capture import write_stage_input
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stack_auditor import (
    MIN_CONFIDENCE_TO_APPLY,
    run_stack_auditor,
)
from faultline.pipeline_v2.stage_0_6_shape import classify_repo_shape
from faultline.pipeline_v2.stage_0_intake import stage_0_intake
from faultline.pipeline_v2.stage_7_output import write_stage_artifact


@dataclass
class IntakeResult:
    """Everything the intake phase hands to the per-tree pipeline."""

    ctx: Any
    verdict: Any
    shape_result: Any
    run_dir: Path


def run_intake_phase(
    repo_path: Path,
    *,
    days: int,
    run_id: str | None,
    subpath: str | None,
    model_id: str,
    tracker: CostTracker,
    cache_backend: Any,
    git_snapshot: "GitSnapshot | None" = None,
    llm_health: "LlmHealth | None" = None,
) -> IntakeResult:
    """Run Stage 0 (intake) + 0.5 (stack auditor) + 0.6 (shape classifier).

    Body moved verbatim from ``run_pipeline_v2`` — see the per-stage
    comments below for each stage's contract.
    """
    # ``_isolate`` is looked up through the run module so it stays the
    # single deep-copy call site to instrument later.
    from faultline.pipeline_v2 import run as _run

    # ── Stage 0 — intake ────────────────────────────────────────────
    # ``subpath`` scopes the whole scan to a monorepo sub-project. Stage 0
    # raises ``SubpathScopeError`` (a ValueError) if scoping can't be
    # applied — we let it propagate (fail loud), never silently scan the
    # wrong tree. ``git_snapshot`` (multi-subpath engine) replaces Stage
    # 0's own git calls with an in-memory partition of one shared pass.
    ctx = stage_0_intake(
        repo_path,
        days=days,
        run_id=run_id,
        subpath=subpath,
        git_snapshot=git_snapshot,
    )
    ctx.cache_backend = cache_backend
    run_dir = ctx.run_dir
    assert run_dir is not None, "Stage 0 must populate ctx.run_dir"

    # Replay v2 — Stage 0's exact input is the CLI argument set. Captured
    # after intake only because the run_dir doesn't exist any earlier.
    write_stage_input(run_dir, 0, "intake", {
        "repo_path": str(repo_path),
        "days": days,
        "subpath": subpath,
    })

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
    write_stage_input(run_dir, 0, "auditor", {
        "ctx": ctx,
        "model_id": model_id,
    })
    with StageLogger(run_dir, 0, "auditor") as log_aud:
        # Content-keyed llm-cache (determinism): identical repo state must
        # replay identical hints, else the volatile auditor prose re-rolls
        # the stage-8 analyst cache key downstream.
        try:
            from faultline.cache import get_cache_backend
            _aud_cache = get_cache_backend()
        except Exception:  # noqa: BLE001 — caching is best-effort
            _aud_cache = None
        verdict = run_stack_auditor(
            _run._isolate(ctx),
            model=model_id,
            cost_tracker=tracker,
            log=log_aud,
            llm_health=llm_health,
            cache=_aud_cache,
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
    write_stage_input(run_dir, 6, "shape", {"ctx": ctx})
    shape_result = classify_repo_shape(ctx)
    ctx = ctx.with_shape(shape_result)
    with StageLogger(run_dir, 6, "shape") as log_shape:
        log_shape.info(
            f"shape={shape_result.shape} "
            f"confidence={shape_result.confidence:.2f} "
            f"matched_signals={list(shape_result.matched_signals)}",
        )

    return IntakeResult(
        ctx=ctx,
        verdict=verdict,
        shape_result=shape_result,
        run_dir=run_dir,
    )


__all__ = [
    "IntakeResult",
    "run_intake_phase",
]
