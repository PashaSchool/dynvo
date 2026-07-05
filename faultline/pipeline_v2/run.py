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

``scan_meta`` is built up incrementally and emitted on the FeatureMap.
The assembly itself (fallback-share computation, warning aggregation,
the key-by-key dict build) lives in pure functions in
:mod:`faultline.pipeline_v2.scan_meta` — see that module's docstring
for the full key catalogue.

Decomposition (refactor/run-decomposition)
==========================================

This module keeps the orchestration skeleton (stage order, deep-copy
boundaries, StageLogger wiring, artifact writes). Two concerns moved
out into sibling modules with pure-function interfaces:

  - :mod:`faultline.pipeline_v2.scan_meta` — scan_meta / telemetry
    assembly.
  - :mod:`faultline.pipeline_v2.incremental_wiring` — the ``--since``
    incremental gating orchestration (Stage 2.5 gate, Stage 5 splice,
    Layer-2 no-op reuse, metric carry-forward, UF-refiner reuse plan).
"""

from __future__ import annotations

import copy
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from faultline.pipeline_v2.git_snapshot import GitSnapshot

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.incremental_wiring import (
    is_layer2_noop,
    run_incremental_gate,
)
from faultline.pipeline_v2.phase_enrich import run_enrich_phase
from faultline.pipeline_v2.phase_extract import run_extract_phase
from faultline.pipeline_v2.phase_finalize import run_finalize_phase
from faultline.pipeline_v2.phase_intake import run_intake_phase
from faultline.pipeline_v2.phase_layer2 import run_layer2_phase
from faultline.pipeline_v2.phase_postprocess import run_postprocess_phase
from faultline.pipeline_v2 import scan_result_cache as _scan_cache
from faultline.pipeline_v2.run_dir import update_latest_symlink
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.replay.capture import write_stage_input
from faultline.pipeline_v2.scan_meta import (
    LLM_FALLBACK_WARN_THRESHOLD,
    assemble_scan_meta,
    build_degradations,
    build_warnings,
    compute_fallback_share,
    extractor_hits_from_stage1 as _extractor_hits,
    workspace_anchor_telemetry as _workspace_anchor_telemetry,
)
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile
from faultline.pipeline_v2.stage_2_6_membership_closure import (
    run_membership_closure,
)
from faultline.pipeline_v2.stage_3_flows import stage_3_flows
from faultline.pipeline_v2.stage_4_residual import stage_4_residual
from faultline.pipeline_v2.stage_6_metrics import (
    attach_hotspots_to_product_features,
)
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_DEPTH as _IMPORT_TREE_MAX_DEPTH,
)
from faultline.pipeline_v2.stage_7_output import (
    stage_7_output,
    write_stage_artifact,
)

logger = logging.getLogger(__name__)


# ── Public model-id aliases ─────────────────────────────────────────────

# Short → fully-qualified mapping. CLI users type ``--model haiku`` and
# the orchestrator resolves to the canonical Anthropic model id.
MODEL_ALIASES: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    # Sonnet 4.6 has no dated snapshot on the API — the bare id is the only
    # valid form (the previously-pinned `-20251108` snapshot 404s). Verified
    # against the Anthropic API 2026-05-27.
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-7": "claude-opus-4-7",
}

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# ``LLM_FALLBACK_WARN_THRESHOLD`` (Sprint A1, 0.50) now lives in
# :mod:`faultline.pipeline_v2.scan_meta` and is re-exported above for
# backwards compatibility.


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


# ``_extractor_hits`` / ``_workspace_anchor_telemetry`` moved to
# :mod:`faultline.pipeline_v2.scan_meta` (imported above under their
# historical names so call sites + any external monkeypatches keep
# working through this module).


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
    org_id: str | None = None,
    subpath: str | None = None,
    git_snapshot: "GitSnapshot | None" = None,
    feature_history: bool = True,
    max_cost: float | None = None,
    prev_scan_path: Path | str | None = None,
    prev_scan_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the Layer 1 pipeline end-to-end against ``repo_path``.

    Args:
        repo_path: scan target.
        max_cost: hard USD ceiling for the scan's total LLM spend,
            shared across every stage. When the ceiling is reached,
            LLM-bearing stages degrade gracefully (skip remaining
            calls with a warning) instead of aborting the scan.
            ``None`` (default) disables enforcement.
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
        git_snapshot: pre-fetched whole-repo git pass to consume instead
            of Stage 0's own git calls (see
            :mod:`faultline.pipeline_v2.git_snapshot`). Injected by
            ``run_pipeline_multi`` so N sub-project scans share ONE
            history parse. ``None`` (default) → identical to today.
            When consumed, ``scan_meta.shared_git_pass`` is emitted
            ``True`` (additive key; absent otherwise).
        feature_history: run Stage 6.95 (per-product-feature /
            per-user-flow git-history timeline). Default ``True`` —
            it is cheap ($0 LLM, one in-memory commit sweep). Set
            ``False`` (CLI ``--no-feature-history``) to skip.
        prev_scan_path: EXPLICIT path to the previous scan's
            feature-map JSON, enabling the UF identity keeper (pin
            user-flow ids/names across rescans — see
            :mod:`faultline.pipeline_v2.uf_identity_keeper`). Per
            ``rule-cold-scan`` there is NO ambient discovery: ``None``
            (default) keeps behaviour byte-identical to today. A
            load failure degrades to an unpinned scan with a
            ``scan_meta`` warning (never fatal).
        prev_scan_json: same as ``prev_scan_path`` but as an
            already-loaded dict (worker/API callers holding the
            artifact from the DB). ``prev_scan_path`` wins when both
            are provided.

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

    # ── Top-level scan-result cache (opt-in) ───────────────────────────
    # temperature=0 is NOT bit-exact on Anthropic, so the LLM stages
    # (Stage 3 flows + 6.7b/6.7c UF + Stage 8 clusterer) diverge run-to-run
    # on an unchanged repo. When FAULTLINE_SCAN_CACHE=1, short-circuit the
    # WHOLE pipeline on a HIT: same (repo content identity + engine version +
    # config) → replay the byte-identical stored FeatureMap ($0, instant).
    # Computed HERE — before intake (Stage 0.5 auditor is a Haiku call) and
    # every downstream expensive stage. Default OFF → this block is a no-op
    # and behaviour is byte-identical to today. Incremental (--since) runs
    # are diff-based and never served from the full-scan cache. Every fault
    # is swallowed → fall through to a normal scan; a scan NEVER crashes here.
    # ── UF identity keeper input (EXPLICIT only — rule-cold-scan) ──────
    # Resolve the optional previous-scan artifact BEFORE the scan-result
    # cache gate: a pinned output must never be stored under (or served
    # from) the plain cache key, otherwise a later scan WITHOUT
    # --prev-scan would replay pinned names — exactly the ambient-state
    # poisoning class the legacy prev_assignments reader had. A load
    # failure degrades to an unpinned scan + scan_meta warning.
    prev_scan_warning: str | None = None
    if prev_scan_path is not None:
        from faultline.pipeline_v2.uf_identity_keeper import load_prev_scan
        try:
            prev_scan_json = load_prev_scan(prev_scan_path)
        except ValueError as exc:
            prev_scan_warning = (
                f"uf-identity-keeper: prev-scan unusable ({exc}); "
                "scan continued unpinned"
            )
            logger.warning("pipeline_v2: %s", prev_scan_warning)
            prev_scan_json = None

    scan_cache_key: str | None = None
    scan_cache_active = (
        _scan_cache.is_enabled() and since is None and prev_scan_json is None
    )
    if scan_cache_active:
        try:
            _cfg_sig = _scan_cache.scan_config_signature(
                model=model_id,
                days=days,
                subpath=subpath,
                max_tree_depth=effective_max_tree_depth,
                llm_reconcile=llm_reconcile,
                feature_history=feature_history,
            )
            scan_cache_key = _scan_cache.compute_scan_cache_key(
                repo_path,
                engine_version=_scan_cache.engine_version(),
                config_signature=_cfg_sig,
            )
        except Exception as exc:  # noqa: BLE001 — cache must never break a scan
            logger.warning(
                "scan_result_cache: key computation failed (%s) — normal scan",
                exc,
            )
            scan_cache_key = None
        if scan_cache_key and not _scan_cache.is_bypassed():
            try:
                _hit = _scan_cache.load_cached_scan(scan_cache_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scan_result_cache: read fault (%s) — normal scan", exc,
                )
                _hit = None
            if _hit is not None:
                served = _scan_cache.serve_from_cache(
                    _hit,
                    key=scan_cache_key,
                    repo_path=repo_path,
                    out_path=out_path,
                )
                if served is not None:
                    return served

    # One shared CostTracker across Stage 3 + Stage 4 so the reported
    # cost is the FULL LLM bill for this scan.
    tracker = CostTracker(max_cost=max_cost)

    # One shared LLM-health state across EVERY LLM-bearing stage: the
    # first auth-class failure (401/403 — dead key) flips a scan-level
    # flag, every stage stops issuing further LLM calls, and the scan
    # finishes VISIBLY degraded (``scan_meta.llm_degraded`` + warning)
    # instead of silently hollow. See :mod:`faultline.pipeline_v2.llm_health`.
    llm_health = LlmHealth()

    # ── Cache backend — constructed once, threaded via ctx ──────────
    # Env ``FAULTLINES_CACHE_BACKEND`` selects fs (default) or a lazily
    # injected DB backend (hosted workers). NOT a global singleton — the
    # instance lives on the ScanContext and flows to every cache site.
    from faultline.cache import get_cache_backend

    cache_backend = get_cache_backend(org_id=org_id)

    # ── Intake phase — Stage 0 intake / 0.5 auditor / 0.6 shape ────
    # Straight-line body lives in :mod:`faultline.pipeline_v2.phase_intake`
    # (same stage order, StageLogger indexes/names, artifact filenames).
    intake = run_intake_phase(
        repo_path,
        days=days,
        run_id=run_id,
        subpath=subpath,
        model_id=model_id,
        tracker=tracker,
        cache_backend=cache_backend,
        git_snapshot=git_snapshot,
        llm_health=llm_health,
    )
    ctx = intake.ctx
    verdict = intake.verdict
    shape_result = intake.shape_result
    repo_class_result = intake.repo_class_result
    run_dir = intake.run_dir

    # ── Framework Knowledge Layer — select the active profile (P4) ──
    # Highest-``detects`` profile wins; the DefaultProfile (positive
    # floor, null-object) wins when no concrete profile matches, so this
    # is always non-None and a no-op for unknown stacks. Selection is
    # deterministic, LLM-free, network-free. The selected profile is
    # threaded (not stashed on ScanContext — its schema stays stable)
    # into the attribution + flow stages, and its name is surfaced in
    # ``scan_meta.framework_profile``.
    #
    # Phase B+ — per-scan-unit refinement: when the Stage 0.6b
    # partition yields units whose own trees select a DIFFERENT
    # profile than the whole-repo winner (polar: FastAPI backend +
    # Next frontend units), ``select_scan_profile`` returns a
    # CompositeProfile that dispatches per unit. Single-package repos
    # and uniform monorepos get the whole-repo winner unchanged.
    from faultline.pipeline_v2.profiles import (
        select_scan_profile as _select_scan_profile,
    )

    framework_profile = _select_scan_profile(ctx)
    framework_profile_name = getattr(framework_profile, "name", "default")
    logger.info("framework_profile selected: %s", framework_profile_name)

    # ════════════════════════════════════════════════════════════════
    # PHASE SEAM (engine(repo, subpaths[]) groundwork):
    # Everything ABOVE this line is the repo-level INTAKE phase
    # (Stage 0 intake / 0.5 auditor / 0.6 shape) — it runs ONCE per
    # repository. Everything BELOW is the PER-TREE pipeline phase
    # (Stages 1→8 + output) that a future multi-subpath engine would
    # run once per selected subpath, sharing the single git pass made
    # by intake. No parameters or loops yet — boundary marker only.
    # ════════════════════════════════════════════════════════════════

    # ── Extract phase — Stage 1 (deterministic extractors) ─────────
    # Straight-line body lives in :mod:`faultline.pipeline_v2.phase_extract`
    # (global vs per-workspace dispatch + Stage 1 telemetry).
    extract = run_extract_phase(ctx, run_dir, profile=framework_profile)
    stage1_out = extract.stage1_out
    extractor_hits = extract.extractor_hits
    per_ws_telemetry = extract.per_ws_telemetry
    workspace_telemetry = extract.workspace_telemetry

    # ── Stage 2 — reconciliation ────────────────────────────────────
    write_stage_input(run_dir, 2, "reconcile", {
        "stage1_out": stage1_out,
        "ctx": ctx,
        "llm_reconcile": llm_reconcile,
    })
    with StageLogger(run_dir, 2, "reconcile") as log2:
        stage2 = stage_2_reconcile(
            _isolate(stage1_out), _isolate(ctx),
            llm_reconcile=llm_reconcile,
            llm_health=llm_health,
            profile=framework_profile,
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
        if stage2.schema_only_suppressed_count:
            log2.info(
                f"schema_only_suppressed: {stage2.schema_only_suppressed_count} "
                f"sample={stage2.schema_only_suppressed_sample}",
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
                # Schema-only phantom suppression telemetry.
                "schema_only_suppressed_count": stage2.schema_only_suppressed_count,
                "schema_only_suppressed_sample": stage2.schema_only_suppressed_sample,
            },
            run_dir=run_dir,
        )

    # ── Stage 2.6 — import-closure membership (deterministic) ──────
    # Pull the service / util / component files each ANCHOR feature
    # (route / fastapi / mvc / …) statically imports into that
    # feature's membership, guarded by the scale-invariant fan-in cap,
    # plus a conservative co-commit secondary signal. Runs BEFORE
    # Stage 3/4 so (a) attached files are EXCLUDED from Stage 4's
    # residual pool (junk-drawers shrink organically) and (b) the
    # enriched ``paths`` feed Stage 6 metrics, Stage 6.5 clustering and
    # Stage 8 Layer-2. See stage_2_6_membership_closure.py docstring.
    # ``extractor_signals`` feeds the URL-literal frontend→backend
    # linker channel (route table from explicit + filesystem routes).
    write_stage_input(run_dir, 2, "membership_closure", {
        "deterministic_features": deterministic_features,
        "unattributed": unattributed,
        "ctx": ctx,
        "stage1_out": stage1_out,
    })
    with StageLogger(run_dir, 2, "membership_closure") as log2_6:
        closure = run_membership_closure(
            _isolate(deterministic_features), _isolate(unattributed),
            _isolate(ctx), log=log2_6,
            extractor_signals=_isolate(stage1_out),
            profile=framework_profile,
        )
        deterministic_features = closure.features
        unattributed = closure.unattributed
        write_stage_artifact(
            ctx.repo_path,
            stage_index=2,
            stage_name="membership_closure",
            payload=closure.telemetry.as_dict(),
            run_dir=run_dir,
        )

    # ── Stage 2.5 — incremental LLM gating (--since path ONLY) ─────
    # Restrict the expensive LLM stages (Stage 3 per-feature flows +
    # Stage 4 per-cluster residual) to the files this diff touched.
    # On a FULL / cold scan this block is skipped entirely and the
    # whole-repo path below is byte-for-byte unchanged (cold-scan rule).
    # See ``incremental_wiring.run_incremental_gate`` for the rationale.
    is_full_scan = since is None
    incremental_base_scan: dict[str, Any] | None = None
    incremental_untouched: list[Any] = []
    incremental_gate_meta: dict[str, Any] = {}
    if not is_full_scan:
        gate_outcome = run_incremental_gate(
            repo_path=repo_path,
            since=since,
            base_scan_path=base_scan_path,
            deterministic_features=deterministic_features,
            unattributed=unattributed,
            ctx=ctx,
            run_dir=run_dir,
        )
        deterministic_features = gate_outcome.deterministic_features
        unattributed = gate_outcome.unattributed
        incremental_untouched = gate_outcome.untouched_features
        incremental_gate_meta = gate_outcome.gate_meta
        incremental_base_scan = gate_outcome.base_scan

    # ── Stage 3 — flow detection (Haiku) ───────────────────────────
    write_stage_input(run_dir, 3, "flows", {
        "deterministic_features": deterministic_features,
        "ctx": ctx,
        "model_id": model_id,
    })
    with StageLogger(run_dir, 3, "flows") as log3:
        stage3 = stage_3_flows(
            _isolate(deterministic_features), _isolate(ctx),
            model=model_id, cost_tracker=tracker,
            llm_health=llm_health,
            profile=framework_profile,
        )
        for fwf in stage3.features_with_flows:
            log3.emit(
                fwf.feature.name,
                f"{len(fwf.flows)} flows detected",
            )
        for w in stage3.warnings:
            log3.warn(w)
        log3.info(
            f"cost_usd={stage3.cost_usd:.4f} llm_calls={stage3.llm_calls} "
            f"cache_hits={stage3.cache_hits}",
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
                "cache_hits": stage3.cache_hits,
                "chunk_telemetry": stage3.chunk_telemetry,
                "warnings": stage3.warnings,
                "reach_telemetry": stage3.reach_telemetry,
            },
            run_dir=run_dir,
        )

    # ── Stage 4 — residual LLM fallback (cluster + saturation) ─────
    write_stage_input(run_dir, 4, "residual", {
        "unattributed": unattributed,
        "ctx": ctx,
        "deterministic_features": deterministic_features,
        "model_id": model_id,
    })
    with StageLogger(run_dir, 4, "residual") as log4:
        stage4 = stage_4_residual(
            _isolate(unattributed), _isolate(ctx),
            _isolate(deterministic_features),
            model=model_id, cost_tracker=tracker, log=log4,
            llm_health=llm_health,
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
            f"cache_hits={stage4.cache_hits} "
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
                "cache_hits": stage4.cache_hits,
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

    # ── Post-process phase — Stage 5 / 5.3 / 5.5 ───────────────────
    # Straight-line body lives in
    # :mod:`faultline.pipeline_v2.phase_postprocess` (naming discipline
    # + incremental splice, sibling-router collapse, bipartite store).
    post = run_postprocess_phase(
        deterministic_features=deterministic_features,
        stage3_features_with_flows=stage3.features_with_flows,
        residual_features=residual_features,
        ctx=ctx,
        run_dir=run_dir,
        is_full_scan=is_full_scan,
        incremental_untouched=incremental_untouched,
    )
    features = post.features
    stage5_result = post.stage5_result
    validation_drops = post.validation_drops
    s53 = post.s53
    s53_features_pre = post.s53_features_pre
    s53_features_post = post.s53_features_post
    s53_collapse_sample = post.s53_collapse_sample
    bipartite = post.bipartite

    # ── Enrich phase — Stage 6 / 6.5 / 6.3 / 6.4 / 6.6 ─────────────
    # Straight-line body lives in :mod:`faultline.pipeline_v2.phase_enrich`
    # (metrics, product clusterer, import tree, framework enrich,
    # branch slicer — same stage order + artifact filenames).
    enriched = run_enrich_phase(
        ctx=ctx,
        features=features,
        run_dir=run_dir,
        effective_max_tree_depth=effective_max_tree_depth,
    )
    features = enriched.features
    product_features = enriched.product_features
    dev_to_product_map = enriched.dev_to_product_map
    product_telemetry = enriched.product_telemetry
    enrichment = enriched.enrichment
    enrich_result = enriched.enrich_result
    framework_enrich_telemetry = enriched.framework_enrich_telemetry
    branch_result = enriched.branch_result
    branch_slicer_telemetry = enriched.branch_slicer_telemetry

    # ── Layer-2 phase — Stage 8 / rollup / 8.5 / 8.6 ───────────────
    # Straight-line body lives in :mod:`faultline.pipeline_v2.phase_layer2`
    # (marketing clusterer, flow rollup, member backfill, non-source
    # drop — same stage order + artifact filenames).
    #
    # Incremental Layer-2 reuse decision (--since path ONLY): a NO-OP
    # diff (zero touched dev features) lets the phase reuse the base
    # scan's FINAL product_features verbatim and SKIP the analyst — see
    # ``incremental_wiring.is_layer2_noop``. ALWAYS False on a full /
    # cold scan (cold-scan rule).
    incremental_layer2_noop = is_layer2_noop(
        is_full_scan=is_full_scan,
        base_scan=incremental_base_scan,
        gate_meta=incremental_gate_meta,
    )
    layer2 = run_layer2_phase(
        ctx=ctx,
        features=features,
        product_features=product_features,
        dev_to_product_map=dev_to_product_map,
        product_telemetry=product_telemetry,
        bipartite_flows=list(bipartite.flows),
        model_id=model_id,
        tracker=tracker,
        run_dir=run_dir,
        incremental_layer2_noop=incremental_layer2_noop,
        incremental_base_scan=incremental_base_scan,
        llm_health=llm_health,
    )
    features = layer2.features
    product_features = layer2.product_features
    dev_to_product_map = layer2.dev_to_product_map
    stage_8_telemetry = layer2.stage_8_telemetry
    stage_8_rollup_telemetry = layer2.stage_8_rollup_telemetry
    stage_8_5_backfill_telemetry = layer2.stage_8_5_backfill_telemetry
    stage_8_6_telemetry = layer2.stage_8_6_telemetry
    stage_8_6_5_telemetry = layer2.stage_8_6_5_telemetry
    stage_8_6_7_telemetry = layer2.stage_8_6_7_telemetry
    stage_8_7_telemetry = layer2.stage_8_7_telemetry
    stage_8_8_telemetry = layer2.stage_8_8_telemetry
    stage_8_9_telemetry = layer2.stage_8_9_telemetry
    stage_8_9_5_telemetry = layer2.stage_8_9_5_telemetry
    stage_8_9_6_telemetry = layer2.stage_8_9_6_telemetry
    stage_8_9_7_telemetry = layer2.stage_8_9_7_telemetry
    stage_5_4_telemetry = post.s54_telemetry

    # ── Product-feature hotspots (Sprint 2026-05-28) ───────────────
    # Stage 6 already attached hotspots to every Layer 1 (developer)
    # feature + their flows. Product (Layer 2) features were not yet
    # finalised at that point — their ``paths`` aggregate only settles
    # after Stage 8 rollup + Stage 8.5 backfill. Run the same
    # deterministic pass on them here so the final scan output carries
    # hotspots on both layers. Pure git data, no extra deps.
    # Replay v2 — input-only capture for the hotspot connector (this
    # pass writes no output artifact of its own; the replay chain needs
    # its input to bridge Stage 8.x → finalize).
    write_stage_input(run_dir, 8, "pf_hotspots", {
        "product_features": product_features,
        "ctx": ctx,
    })
    try:
        pfs_with_hotspots = attach_hotspots_to_product_features(
            product_features, ctx.commits,
        )
        logger.info(
            "stage_6_metrics: hotspots attached on %d/%d product features",
            pfs_with_hotspots, len(product_features),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "stage_6_metrics: product-feature hotspot pass failed: %s", exc,
        )

    # ── Scan meta assembly ─────────────────────────────────────────
    # Pure-function telemetry assembly — see ``scan_meta.py`` for the
    # fallback-share rules, warning aggregation, and the key-by-key
    # documentation of the scan_meta dict.
    share = compute_fallback_share(
        stage5_drop_log=stage5_result.drop_log,
        residual_features=residual_features,
        features=features,
    )
    total_features = share.total_features

    warnings = build_warnings(
        stage3=stage3,
        stage4=stage4,
        enrichment=enrichment,
        enrich_result=enrich_result,
        branch_result=branch_result,
        llm_share=share.llm_share,
        stack=ctx.stack,
    )
    degradations = build_degradations(
        stage3=stage3,
        stage4=stage4,
        enrichment=enrichment,
        enrich_result=enrich_result,
        branch_result=branch_result,
        llm_share=share.llm_share,
    )

    elapsed = round(time.monotonic() - t0, 2)
    cost_usd = round(tracker.total_cost_usd, 4)
    llm_calls = stage3.llm_calls + stage4.llm_calls

    scan_meta: dict[str, Any] = assemble_scan_meta(
        ctx=ctx,
        verdict=verdict,
        framework_profile=framework_profile_name,
        model_id=model_id,
        extractor_hits=extractor_hits,
        workspace_telemetry=workspace_telemetry,
        share=share,
        validation_drops=validation_drops,
        stage2=stage2,
        stage3=stage3,
        stage4=stage4,
        stage5_result=stage5_result,
        s53=s53,
        s53_features_pre=s53_features_pre,
        s53_features_post=s53_features_post,
        s53_collapse_sample=s53_collapse_sample,
        warnings=warnings,
        degradations=degradations,
        elapsed=elapsed,
        cost_usd=cost_usd,
        llm_calls=llm_calls,
        run_dir=run_dir,
        llm_reconcile=llm_reconcile,
        bipartite_telemetry=bipartite.telemetry,
        product_telemetry=product_telemetry,
        per_ws_telemetry=per_ws_telemetry,
        enrichment=enrichment,
        effective_max_tree_depth=effective_max_tree_depth,
        framework_enrich_telemetry=framework_enrich_telemetry,
        branch_slicer_telemetry=branch_slicer_telemetry,
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
        stage_5_4_telemetry=stage_5_4_telemetry,
        shape_result=shape_result,
        repo_class_result=repo_class_result,
    )

    # UF identity keeper — surface a failed prev-scan load VISIBLY
    # (the keeper itself runs in the finalize phase when input loaded).
    if prev_scan_warning:
        scan_meta.setdefault("warnings", []).append(prev_scan_warning)

    # Stage 2.6 membership-closure telemetry — additive key (consumers
    # treat absence as "stage not present"). Per-feature detail and
    # wall-clock timing stay in the 02-stage-membership_closure.json
    # artifact; scan_meta carries only run-stable counts so two
    # equivalent scans serialize identically (multi-subpath
    # equivalence gate).
    scan_meta["membership_closure"] = {
        k: v
        for k, v in closure.telemetry.as_dict().items()
        if k not in {"per_feature", "attached_sample", "elapsed_sec"}
    }

    # Multi-subpath engine telemetry — additive key, only emitted when
    # this run consumed an injected shared git snapshot (no schema-
    # version bump; consumers treat absence as "own git pass").
    if git_snapshot is not None:
        scan_meta["shared_git_pass"] = True

    # Phase B+ per-unit profile telemetry — additive key, only emitted
    # when the selection built a CompositeProfile (absence == the
    # whole-repo winner served every tree, exactly as before).
    unit_assignments = getattr(framework_profile, "unit_assignments", None)
    if unit_assignments:
        scan_meta["framework_profile_units"] = [
            {"subpath": sp, "profile": name} for sp, name in unit_assignments
        ]
        scan_meta["framework_profile_root"] = getattr(
            framework_profile, "root_profile_name", framework_profile_name,
        )

    # ── Finalize phase — Stage 6.8 / 3.5 / 6.9 / 6.7* / 7 ──────────
    # Straight-line body lives in :mod:`faultline.pipeline_v2.phase_finalize`
    # (lineage + indexes, incremental bookkeeping, flow expansion, test
    # strip, user-flow rollup/split/refine, output writer). Mutates
    # ``scan_meta`` in place exactly as the inline code did.
    out = run_finalize_phase(
        repo_path=repo_path,
        ctx=ctx,
        features=features,
        bipartite=bipartite,
        product_features=product_features,
        stage1_out=stage1_out,
        scan_meta=scan_meta,
        run_dir=run_dir,
        model_id=model_id,
        tracker=tracker,
        since=since,
        base_scan_path=base_scan_path,
        lineage_jaccard_threshold=lineage_jaccard_threshold,
        incremental_base_scan=incremental_base_scan,
        incremental_gate_meta=incremental_gate_meta,
        out_path=out_path,
        days=days,
        feature_history=feature_history,
        llm_health=llm_health,
        repo_class_result=repo_class_result,
        prev_scan_json=prev_scan_json,
    )

    # ── Flush any buffered cache writes (no-op for fs backend) ──────
    try:
        cache_backend.flush()
    except Exception as exc:  # noqa: BLE001 — never fail a scan on cache flush
        logger.warning("pipeline_v2: cache flush failed: %s", exc)

    # ── Store the FINAL FeatureMap under the scan-result cache key ─────
    # MISS path only (a HIT returned far above). We cache the byte-exact
    # bytes just written by Stage 7 so a later identical scan replays them
    # verbatim. Faults are swallowed — a cache-store failure never fails an
    # otherwise-successful scan. Additive ``scan_meta.scan_cache`` marker
    # records the outcome for telemetry (absent when the cache is OFF).
    if scan_cache_active and scan_cache_key:
        stored = False
        try:
            stored = _scan_cache.store_scan_result(scan_cache_key, out)
        except Exception as exc:  # noqa: BLE001 — never fail a scan on cache store
            logger.warning("scan_result_cache: store fault (%s)", exc)
        scan_meta["scan_cache"] = {
            "enabled": True,
            "served_from_cache": False,
            "stored": bool(stored),
            "key": scan_cache_key,
        }
        logger.info(
            "scan_result_cache: MISS — stored=%s (key=%s)",
            stored, scan_cache_key[:12],
        )

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
