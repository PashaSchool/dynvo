"""Per-tree pipeline phase — Stage 1 (deterministic extractors).

Extracted from ``run.py`` (refactor/run-decomposition) as straight-line
code — same StageLogger stage index/name, same artifact filename, same
deep-copy boundary (via ``run._isolate``), same telemetry keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from faultline.replay.capture import write_stage_input
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_1_extractors import stage_1_extractors
from faultline.pipeline_v2.stage_1_per_workspace import (
    run_stage_1_per_workspace,
    should_activate_per_workspace,
)
from faultline.pipeline_v2.stage_7_output import write_stage_artifact


@dataclass
class ExtractResult:
    """Stage 1 outputs + telemetry the orchestrator threads onward."""

    stage1_out: dict[str, Any]
    extractor_hits: dict[str, int]
    per_ws_telemetry: dict[str, Any]
    workspace_telemetry: dict[str, Any]


def run_extract_phase(ctx: Any, run_dir: Path) -> ExtractResult:
    """Run Stage 1 — extractors (global or per-workspace dispatch).

    Body moved verbatim from ``run_pipeline_v2``.
    """
    # ``_isolate`` / telemetry helpers are looked up through the run
    # module so external monkeypatches on ``run`` keep working and the
    # deep-copy boundary stays a single instrumentable call site.
    from faultline.pipeline_v2 import run as _run

    # ── Stage 1 — extractors ────────────────────────────────────────
    # Sprint S3 — per-workspace dispatch for polyglot monorepos.
    # When the auditor (or per-workspace stack diversity) flags the
    # repo as polyglot, we replace the global Stage 1 with a per-
    # workspace pass that scopes ``tracked_files`` + ``stack`` to one
    # workspace at a time. This unblocks NestJS+Next, Fastify+Vite,
    # Rust-WASM+Next, etc. — repos where a single-stack global pass
    # emits zero anchors and Stage 4 LLM-fallback synthesises 100%.
    write_stage_input(run_dir, 1, "extractors", {"ctx": ctx})

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
            pw_result = run_stage_1_per_workspace(_run._isolate(ctx))
            if not pw_result.workspaces_used:
                # Activation passed but no workspaces materialised
                # (synthesis returned empty + no declared list).
                # Fall back to the global pass so we don't lose
                # signals altogether.
                log1.warn(
                    "per-workspace dispatch activated but no workspaces "
                    "found — falling back to global Stage 1",
                )
                stage1_out = stage_1_extractors(_run._isolate(ctx))
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
            stage1_out = stage_1_extractors(_run._isolate(ctx))

        extractor_hits = _run._extractor_hits(stage1_out)
        for name, count in extractor_hits.items():
            log1.info(f"{name}: {count} candidates", feature=None)
        # "_errors" carries a dict[str, str] payload, not anchor candidates.
        stage1_errs = cast("dict[str, str]", stage1_out.get("_errors") or {})
        for name, err in stage1_errs.items():
            log1.warn(f"extractor {name} errored: {err}")

        # Sprint D3 — workspace package detection telemetry.
        # Counts how many declared workspaces produced a package-source
        # anchor with the workspace's slug. Helps catch regressions in
        # generic-named packages (``packages/ui``, ``packages/utils``)
        # that historically went undetected.
        workspace_telemetry = _run._workspace_anchor_telemetry(ctx, stage1_out)
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

    return ExtractResult(
        stage1_out=stage1_out,
        extractor_hits=extractor_hits,
        per_ws_telemetry=per_ws_telemetry,
        workspace_telemetry=workspace_telemetry,
    )


__all__ = [
    "ExtractResult",
    "run_extract_phase",
]
