"""scan_meta / telemetry assembly for the pipeline-v2 orchestrator.

Extracted from ``run.py`` (refactor/run-decomposition) — pure functions
only. Every function takes explicit inputs and returns plain data; no
StageLogger, no artifact writes, no LLM, no I/O. The orchestrator in
:mod:`faultline.pipeline_v2.run` calls these at the end of a scan to
fold per-stage results into the final ``scan_meta`` dict emitted on the
FeatureMap.

``scan_meta`` is built up incrementally and emitted on the FeatureMap:

  - ``run_id`` (directory name under ``~/.faultline/logs/<slug>/``)
  - ``stack`` / ``monorepo`` / ``workspace_manager`` / ``stack_signals``
    (from Stage 0)
  - ``model`` (the Haiku model id used for Stage 3 + Stage 4)
  - ``extractor_hits`` ({extractor_name: candidate_count}) and
    ``extractor_coverage_pct`` (deterministic-feature share of total)
  - ``llm_fallback_pct`` (residual feature share)
  - ``warnings`` (free-form list, includes the >30% fallback nudge)
  - ``elapsed_sec`` / ``cost_usd`` / ``calls`` — assembled here from the
    Stage 3 + Stage 4 snapshot, then REFRESHED by ``phase_finalize`` to
    the shared CostTracker's full bill (finalize-phase LLM stages —
    6.7b/6.7c/6.7d + personas — included; W3 rider, chain4 finding)
  - ``stage_artifact_dir`` (the per-RUN dir — not the parent slug dir)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultline.models.types import SCHEMA_VERSION
from faultline.pipeline_v2 import degradations
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_FILES_PER_FEATURE as _IMPORT_TREE_MAX_FILES,
    DEFAULT_MAX_SYMBOLS_PER_FEATURE as _IMPORT_TREE_MAX_SYMBOLS,
)

# Sprint A1: the warn threshold is HALF, not the old 30% cap. We do
# NOT truncate when this trips — we just nudge "you may want an
# extractor for stack X". Quality is enforced downstream in Stage 5.
LLM_FALLBACK_WARN_THRESHOLD = 0.50


# ── Helper — flatten extractor results into hits dict ───────────────────


def extractor_hits_from_stage1(stage1_out: dict[str, Any]) -> dict[str, int]:
    """``{extractor_name: candidate_count}`` for ``scan_meta.extractor_hits``.

    Drops the sentinel ``_errors`` key.
    """
    return {
        name: len(cands)
        for name, cands in stage1_out.items()
        if name != "_errors"
    }


def workspace_anchor_telemetry(
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


# ── Fallback-share computation ──────────────────────────────────────────


@dataclass(frozen=True)
class FallbackShare:
    """LLM-fallback share of the final feature set.

    Counts fallback survivors by NAME match against the
    post-A1-validation residual list (stage5 stripped FS-missing +
    anchor-dup before naming discipline; some may still have been
    dropped by Fix A/B/C/D).
    """

    fallback_count: int
    total_features: int
    deterministic_count: int
    llm_share: float
    extractor_coverage_pct: float


def compute_fallback_share(
    *,
    stage5_drop_log: list[tuple[str, str]],
    residual_features: list[Any],
    features: list[Any],
) -> FallbackShare:
    """Compute the residual (LLM-fallback) share of the final features."""
    pre_naming_fallback_names = {
        name for (name, reason) in stage5_drop_log
        if reason.startswith("junk_name:")
    }
    # Fallback features that BOTH survived A1 validation AND naming discipline.
    survived_fallback_names = (
        {f.name for f in residual_features}
        - pre_naming_fallback_names
        # Also subtract any A1-validation drops:
        - {
            name for (name, reason) in stage5_drop_log
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
    return FallbackShare(
        fallback_count=fallback_count,
        total_features=total_features,
        deterministic_count=deterministic_count,
        llm_share=llm_share,
        extractor_coverage_pct=extractor_coverage_pct,
    )


# ── Warning aggregation ─────────────────────────────────────────────────


def build_warnings(
    *,
    stage3: Any,
    stage4: Any,
    enrichment: Any,
    enrich_result: Any,
    branch_result: Any,
    llm_share: float,
    stack: str | None,
) -> list[str]:
    """Aggregate per-stage warnings + budget events + the fallback nudge.

    Sprint F (2026-05-20) — surface budget-exceeded events into
    scan_meta so dashboards / replay tooling can see them without
    opening the per-stage artifact.
    """
    warnings: list[str] = []
    warnings.extend(stage3.warnings)
    warnings.extend(stage4.warnings)
    # NB: budget degradation is now a DETERMINISTIC per-feature-allowance count
    # (Stages 6.3/6.4/6.6). The volatile wall-clock ``elapsed_sec`` is
    # deliberately NOT embedded in these warning strings — it made the scan
    # output non-byte-identical on the degradation path. It remains available
    # as the per-stage ``elapsed_sec`` telemetry key (scrubbed from the digest).
    if enrichment.budget_exceeded:
        warnings.append(
            f"stage_6_3_budget_exceeded budget_sec={enrichment.budget_sec} "
            f"features_skipped={enrichment.features_budget_skipped}"
        )
    if enrich_result.budget_exceeded:
        warnings.append(
            f"stage_6_4_budget_exceeded budget_sec={enrich_result.budget_sec} "
            f"features_skipped={enrich_result.features_budget_skipped}"
        )
    if getattr(branch_result, "budget_exceeded", False):
        warnings.append(
            f"stage_6_6_budget_exceeded budget_sec={branch_result.budget_sec} "
            f"features_skipped={branch_result.features_budget_skipped}"
        )
    if llm_share > LLM_FALLBACK_WARN_THRESHOLD:
        # Sprint A1: informational nudge only. The old 30%-share cap
        # was REMOVED; we no longer truncate Stage 4 output. This
        # warning tells the operator "you're heavily relying on the
        # LLM for this stack — write an extractor".
        warnings.append(
            f"scan_meta.llm_share = {llm_share:.2f} — fallback exceeds "
            f"half of features; consider adding extractor for stack="
            f"{stack}."
        )
    return warnings


def build_degradations(
    *,
    stage3: Any,
    stage4: Any,
    enrichment: Any,
    enrich_result: Any,
    branch_result: Any,
    llm_share: float,
) -> list[dict[str, Any]]:
    """Aggregate STRUCTURED degradation events into ``scan_meta.degradations[]``.

    The typed sibling of :func:`build_warnings`: same signals, machine-readable
    so workers / boards can group by ``type`` (WHERE + HOW OFTEN) and remediate.
    Each record follows the canonical schema in
    :mod:`faultline.pipeline_v2.degradations`. Stages emit their own records
    (``stage.degradations``); the budget-exceeded enrichment events and the
    high-fallback nudge are derived here from the same fields
    :func:`build_warnings` reads, so the two never drift.
    """
    out: list[dict[str, Any]] = []
    out.extend(getattr(stage3, "degradations", None) or [])
    out.extend(getattr(stage4, "degradations", None) or [])
    for stage_name, res in (
        ("stage_6_3_import_tree", enrichment),
        ("stage_6_4_enrich", enrich_result),
        ("stage_6_6_branch_slicer", branch_result),
    ):
        if getattr(res, "budget_exceeded", False):
            out.append(
                degradations.budget_exceeded(
                    stage=stage_name,
                    budget_sec=getattr(res, "budget_sec", 0),
                    features_skipped=getattr(
                        res, "features_budget_skipped", 0,
                    ),
                    elapsed_sec=getattr(res, "elapsed_sec", 0),
                ),
            )
    if llm_share > LLM_FALLBACK_WARN_THRESHOLD:
        out.append(
            degradations.high_llm_fallback(
                share=llm_share, threshold=LLM_FALLBACK_WARN_THRESHOLD,
            ),
        )
    return out


# ── scan_meta dict assembly ─────────────────────────────────────────────


def assemble_scan_meta(
    *,
    ctx: Any,
    verdict: Any,
    framework_profile: str = "default",
    model_id: str,
    extractor_hits: dict[str, int],
    workspace_telemetry: dict[str, Any],
    share: FallbackShare,
    validation_drops: Any,
    stage2: Any,
    stage3: Any,
    stage4: Any,
    stage5_result: Any,
    s53: Any,
    s53_features_pre: int,
    s53_features_post: int,
    s53_collapse_sample: list[dict[str, Any]],
    warnings: list[str],
    degradations: list[dict[str, Any]] | None = None,
    elapsed: float,
    cost_usd: float,
    llm_calls: int,
    run_dir: Path,
    llm_reconcile: bool,
    bipartite_telemetry: dict[str, Any],
    product_telemetry: dict[str, Any],
    per_ws_telemetry: dict[str, Any],
    enrichment: Any,
    effective_max_tree_depth: int,
    framework_enrich_telemetry: dict[str, Any],
    branch_slicer_telemetry: dict[str, Any],
    stage_8_telemetry: dict[str, Any],
    stage_8_rollup_telemetry: dict[str, Any],
    stage_8_5_backfill_telemetry: dict[str, Any],
    stage_8_6_telemetry: dict[str, Any],
    shape_result: Any,
    repo_class_result: Any = None,
    stage_8_7_telemetry: dict[str, Any] | None = None,
    stage_8_8_telemetry: dict[str, Any] | None = None,
    stage_8_9_telemetry: dict[str, Any] | None = None,
    stage_8_9_5_telemetry: dict[str, Any] | None = None,
    stage_8_9_6_telemetry: dict[str, Any] | None = None,
    stage_8_9_7_telemetry: dict[str, Any] | None = None,
    stage_5_4_telemetry: dict[str, Any] | None = None,
    stage_8_6_5_telemetry: dict[str, Any] | None = None,
    stage_8_6_7_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the ``scan_meta`` dict from the per-stage results.

    Pure function — every input is explicit, no globals, no I/O. The
    key set and ordering are load-bearing for dashboards and replay
    tooling; do not reorder or rename without checking consumers.
    """
    scan_meta: dict[str, Any] = {
        # On-disk schema version (see SCHEMA_VERSION in models/types.py
        # for the bump policy). Duplicated from FeatureMap.schema_version
        # so consumers that only read scan_meta (without parsing the
        # full map) can still detect the schema generation.
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": "v2",
        "run_id": ctx.run_id,
        "stack": ctx.stack,
        "monorepo": ctx.monorepo,
        "workspace_manager": ctx.workspace_manager,
        "stack_signals": ctx.stack_signals,
        # Monorepo sub-project scoping. ``None`` for a whole-repo scan.
        # When set, every feature/flow path in this FeatureMap is relative
        # to ``subpath`` (prepend ``subpath/`` to reconstruct a repo-root-
        # relative path).
        "subpath": ctx.subpath,
        # Authoritative workspace list (name + path), emitted from
        # ``detect_workspace`` so the app can populate
        # ``workspacePackageCount`` and re-seed the sub-project picker
        # from engine truth (spec §3.3). Empty list for non-monorepos.
        "workspaces": [
            {"name": w.name, "path": w.path, "stack": w.stack}
            for w in (ctx.workspaces or [])
        ],
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
        # P4 framework-awareness — the active FrameworkProfile that drove
        # attribution + flow seeding. ``"default"`` means the null-object
        # profile won (no concrete framework profile registered/matched),
        # i.e. behaviour is identical to pre-P4.
        "framework_profile": framework_profile,
        "model": model_id,
        "extractor_hits": extractor_hits,
        # Sprint D3 — workspace package telemetry. Exposed at top of
        # scan_meta so dashboards can flag silent skips without
        # parsing the per-stage artifact.
        "workspace_packages": workspace_telemetry,
        "extractor_coverage_pct": round(share.extractor_coverage_pct, 3),
        # ``llm_fallback_pct`` kept for backwards-compat with existing
        # dashboards. ``llm_share`` is the Sprint A1 canonical name.
        "llm_fallback_pct": round(share.llm_share, 3),
        "llm_share": round(share.llm_share, 3),
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
        # Schema-only phantom suppression: bare DB-entity models dropped
        # because they had no owning code (every source schema-class).
        # Non-zero is expected on schema-heavy stacks (one Prisma file
        # with many models) and is the fix for the phantom-dup-feature
        # cloning bug; it is NOT a warning.
        "stage_2_schema_only_suppressed_count": stage2.schema_only_suppressed_count,
        "stage_2_schema_only_suppressed_sample": stage2.schema_only_suppressed_sample,
        # Sprint S4 — Stage 5.3 sibling-router collapse telemetry.
        "stage_5_3_collapse_groups_count": len(s53.collapse_groups),
        "stage_5_3_features_collapsed": s53.features_collapsed,
        "stage_5_3_features_pre": s53_features_pre,
        "stage_5_3_features_post": s53_features_post,
        "stage_5_3_collapse_sample": s53_collapse_sample,
        "deterministic_feature_count": share.deterministic_count,
        "residual_feature_count": share.fallback_count,
        "warnings": warnings,
        "degradations": degradations or [],
        "elapsed_sec": elapsed,
        "cost_usd": cost_usd,
        "calls": llm_calls,
        "stage3_cost_usd": round(stage3.cost_usd, 4),
        # Stage 3 flow-detection warm-cache (CacheKind.LLM_FLOWS): per-feature
        # units replayed from the content-hash cache instead of a Haiku call.
        # On a re-scan of an unchanged repo this drives Stage 3 reproducibility.
        "stage_3_cache_hits": stage3.cache_hits,
        "stage_3_llm_calls": stage3.llm_calls,
        # Chunked oversized-feature flow detection (Stage-8.9 oversized
        # contract reused at Stage 3): features routed through the chunked
        # path, chunks planned, chunk Haiku calls / cache hits (subsets of
        # stage_3_llm_calls / stage_3_cache_hits), and validated flows
        # produced by chunk calls. ``getattr`` keeps older Stage3Result
        # stubs (tests) working.
        "stage_3_features_chunked": (getattr(
            stage3, "chunk_telemetry", None) or {}).get("features_chunked", 0),
        "stage_3_chunks_total": (getattr(
            stage3, "chunk_telemetry", None) or {}).get("chunks_total", 0),
        "stage_3_chunk_llm_calls": (getattr(
            stage3, "chunk_telemetry", None) or {}).get("chunk_llm_calls", 0),
        "stage_3_chunk_cache_hits": (getattr(
            stage3, "chunk_telemetry", None) or {}).get("chunk_cache_hits", 0),
        "stage_3_flows_from_chunks": (getattr(
            stage3, "chunk_telemetry", None) or {}).get("flows_from_chunks", 0),
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
        **bipartite_telemetry,
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
        # Stage 8.5 — deterministic path-overlap member backfill.
        # Additive: only stamps product_feature_id on analyst-unmapped
        # dev features; never alters the product_features[] array.
        "stage_8_5_backfill": dict(stage_8_5_backfill_telemetry),
        # Stage 8.6 — universal non-source scaffold/docs drop. Count +
        # sample of developer features removed because their entire
        # path-set was non-source, plus Layer-2 reconcile counters.
        "stage_8_6_nonsource_drops": stage_8_6_telemetry["dropped"],
        "stage_8_6_nonsource_drop": dict(stage_8_6_telemetry),
        # Stage 8.6.5 — shared-scaffold filter. High-fan-in scaffold files
        # (lib/ui/utils/i18n/hooks/components) demoted from specific features'
        # primary paths (they stay on the workspace anchor as residual).
        "stage_8_6_5_scaffold_filter": dict(stage_8_6_5_telemetry or {}),
        # Stage 8.6.7 — DI service attribution (named-reference services moved
        # off the platform bucket to their owning feature).
        "stage_8_6_7_di_attribution": dict(stage_8_6_7_telemetry or {}),
        # Stage 8.7 — workspace-anchor de-sink. Workspace anchors that
        # released paths claimed by a more-specific feature (the blob
        # double-claim), plus the affected product features resynced.
        "stage_8_7_anchor_desink": dict(stage_8_7_telemetry or {}),
        # Stage 8.8 — shared-member enrichment. De-sink residual files
        # attached as N:M role="shared" member_files on the specific
        # features that directly import them (paths untouched).
        "stage_8_8_shared_members": dict(stage_8_8_telemetry or {}),
        # Stage 8.9 — workspace-anchor sub-decomposition. The de-sink
        # residual split along the repo's module structure into per-domain
        # developer sub-features (paths conserved, product paths byte-stable).
        "stage_8_9_subdecompose": dict(stage_8_9_telemetry or {}),
        "stage_8_9_5_llm_component_split": dict(stage_8_9_5_telemetry or {}),
        "stage_8_9_6_domain_member_attribution": dict(stage_8_9_6_telemetry or {}),
        "stage_8_9_7_vendor_connector_split": dict(stage_8_9_7_telemetry or {}),
        "stage_5_4_cross_flow_dedup": dict(stage_5_4_telemetry or {}),
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
    # Stage 0.7 — repo-class exit gate (StackProfile Phase C). Additive
    # key: {class, confidence, rationale, matched_signals, gate_enabled,
    # uf_suppression_eligible}. Absent only for legacy callers that
    # don't classify (default None).
    if repo_class_result is not None:
        from faultline.pipeline_v2.stage_0_7_repo_class import scan_meta_block
        scan_meta["repo_class"] = scan_meta_block(repo_class_result)
    return scan_meta


__all__ = [
    "LLM_FALLBACK_WARN_THRESHOLD",
    "FallbackShare",
    "assemble_scan_meta",
    "build_warnings",
    "compute_fallback_share",
    "extractor_hits_from_stage1",
    "workspace_anchor_telemetry",
]
