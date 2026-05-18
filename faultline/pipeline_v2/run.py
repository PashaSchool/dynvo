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

Telemetry
=========

``scan_meta`` is built up incrementally and emitted on the FeatureMap:

  - ``stack`` / ``monorepo`` / ``workspace_manager`` / ``stack_signals``
    (from Stage 0)
  - ``model`` (the Haiku model id used for Stage 3 + Stage 4)
  - ``extractor_hits`` ({extractor_name: candidate_count}) and
    ``extractor_coverage_pct`` (deterministic-feature share of total)
  - ``llm_fallback_pct`` (residual feature share)
  - ``warnings`` (free-form list, includes the >30% fallback nudge)
  - ``elapsed_sec`` / ``cost_usd`` / ``calls`` (cost is Stage 3 + Stage 4)
  - ``stage_artifact_dir`` (where the per-stage debug snapshots live)

The orchestrator writes a stage artifact for every stage it runs, so
``~/.faultline/logs/<slug>/`` is a complete replay of the run.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.stage_0_intake import stage_0_intake
from faultline.pipeline_v2.stage_1_extractors import stage_1_extractors
from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile
from faultline.pipeline_v2.stage_3_flows import stage_3_flows
from faultline.pipeline_v2.stage_4_residual import stage_4_residual
from faultline.pipeline_v2.stage_5_postprocess import stage_5_from_stage3_result
from faultline.pipeline_v2.stage_6_metrics import stage_6_metrics
from faultline.pipeline_v2.stage_7_output import (
    stage_7_output,
    stage_artifact_dir,
    write_stage_artifact,
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
LLM_FALLBACK_WARN_THRESHOLD = 0.30


def resolve_model(name: str) -> str:
    """Resolve a short or fully-qualified model name to its canonical id."""
    if not name:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(name, name)


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


# ── Public entry point ──────────────────────────────────────────────────


def run_pipeline_v2(
    repo_path: Path | str,
    *,
    model: str = DEFAULT_MODEL,
    days: int = 365,
    out_path: Path | None = None,
    llm_reconcile: bool = False,
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

    Returns:
        A dict containing ``path`` (the written FeatureMap path) and
        every key from ``scan_meta`` so callers can introspect the run
        without re-reading the JSON.
    """
    repo_path = Path(repo_path).resolve()
    model_id = resolve_model(model)
    t0 = time.monotonic()

    # One shared CostTracker across Stage 3 + Stage 4 so the reported
    # cost is the FULL LLM bill for this scan.
    tracker = CostTracker(max_cost=None)

    # ── Stage 0 — intake ────────────────────────────────────────────
    ctx = stage_0_intake(repo_path, days=days)
    write_stage_artifact(
        ctx.repo_path,
        stage_index=0,
        stage_name="intake",
        payload={
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
    )

    # ── Stage 1 — extractors ────────────────────────────────────────
    stage1_out = stage_1_extractors(ctx)
    extractor_hits = _extractor_hits(stage1_out)
    write_stage_artifact(
        ctx.repo_path,
        stage_index=1,
        stage_name="extractors",
        payload={
            "extractor_hits": extractor_hits,
            "errors": stage1_out.get("_errors", {}),
        },
    )

    # ── Stage 2 — reconciliation ────────────────────────────────────
    stage2 = stage_2_reconcile(
        stage1_out, ctx, llm_reconcile=llm_reconcile,
    )
    deterministic_features = stage2.features
    unattributed = stage2.unattributed
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
        },
    )

    # ── Stage 3 — flow detection (Haiku) ───────────────────────────
    stage3 = stage_3_flows(
        deterministic_features, ctx,
        model=model_id, cost_tracker=tracker,
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
        },
    )

    # ── Stage 4 — residual LLM fallback ────────────────────────────
    stage4 = stage_4_residual(
        unattributed, ctx, deterministic_features,
        model=model_id, cost_tracker=tracker,
    )
    residual_features = stage4.residual_features
    write_stage_artifact(
        ctx.repo_path,
        stage_index=4,
        stage_name="residual",
        payload={
            "residual_feature_count": len(residual_features),
            "cost_usd": stage4.cost_usd,
            "llm_calls": stage4.llm_calls,
            "warnings": stage4.warnings,
            "chunks_processed": stage4.chunks_processed,
            "rejected_names": stage4.rejected_names,
        },
    )

    # ── Stage 5 — post-process (naming discipline) ─────────────────
    features = stage_5_from_stage3_result(
        deterministic=deterministic_features,
        stage3_features_with_flows=stage3.features_with_flows,
        residual=residual_features,
        ctx=ctx,
    )
    write_stage_artifact(
        ctx.repo_path,
        stage_index=5,
        stage_name="postprocess",
        payload={
            "feature_count": len(features),
            "feature_names": [f.name for f in features],
        },
    )

    # ── Stage 6 — metrics enrichment ───────────────────────────────
    features = stage_6_metrics(features, ctx)
    write_stage_artifact(
        ctx.repo_path,
        stage_index=6,
        stage_name="metrics",
        payload={
            "feature_count": len(features),
            "with_commits": sum(1 for f in features if f.total_commits > 0),
            "with_coverage": sum(
                1 for f in features if f.coverage_pct is not None
            ),
        },
    )

    # ── Scan meta assembly ─────────────────────────────────────────
    total_features = len(features)
    fallback_count = len(residual_features)
    llm_fallback_pct = (
        fallback_count / total_features if total_features > 0 else 0.0
    )
    deterministic_count = max(total_features - fallback_count, 0)
    extractor_coverage_pct = (
        deterministic_count / total_features if total_features > 0 else 0.0
    )

    warnings: list[str] = []
    warnings.extend(stage3.warnings)
    warnings.extend(stage4.warnings)
    if llm_fallback_pct > LLM_FALLBACK_WARN_THRESHOLD:
        warnings.append(
            f"LLM-fallback handled {llm_fallback_pct * 100:.0f}% of features; "
            f"consider adding a custom extractor for stack {ctx.stack}."
        )

    elapsed = round(time.monotonic() - t0, 2)
    cost_usd = round(tracker.total_cost_usd, 4)
    llm_calls = stage3.llm_calls + stage4.llm_calls

    scan_meta: dict[str, Any] = {
        "pipeline_version": "v2",
        "stack": ctx.stack,
        "monorepo": ctx.monorepo,
        "workspace_manager": ctx.workspace_manager,
        "stack_signals": ctx.stack_signals,
        "model": model_id,
        "extractor_hits": extractor_hits,
        "extractor_coverage_pct": round(extractor_coverage_pct, 3),
        "llm_fallback_pct": round(llm_fallback_pct, 3),
        "deterministic_feature_count": deterministic_count,
        "residual_feature_count": fallback_count,
        "warnings": warnings,
        "elapsed_sec": elapsed,
        "cost_usd": cost_usd,
        "calls": llm_calls,
        "stage3_cost_usd": round(stage3.cost_usd, 4),
        "stage4_cost_usd": round(stage4.cost_usd, 4),
        "stage_artifact_dir": str(stage_artifact_dir(ctx.repo_path)),
        "llm_reconcile": bool(llm_reconcile),
    }

    # ── Stage 7 — output ───────────────────────────────────────────
    out = stage_7_output(features, ctx, scan_meta, out_path, days=days)
    logger.info(
        "pipeline_v2 done: %d features, cost $%.4f, elapsed %.1fs → %s",
        total_features, cost_usd, elapsed, out,
    )

    return {"path": str(out), **scan_meta}


__all__ = [
    "run_pipeline_v2",
    "resolve_model",
    "MODEL_ALIASES",
    "DEFAULT_MODEL",
]
