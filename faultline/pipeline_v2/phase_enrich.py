"""Per-tree pipeline phase — Stage 6 enrichment family.

Extracted from ``run.py`` (refactor/run-decomposition) as straight-line
code — same stage order, same StageLogger stage indexes/names, same
artifact filenames, same deep-copy boundaries (via ``run._isolate``).

  - Stage 6   — metrics enrichment (commit + coverage)
  - Stage 6.5 — Layer 2 product clusterer (deterministic)
  - Stage 6.3 — whole-import-tree enrichment (deterministic)
  - Stage 6.4 — framework-aware enrichment (deterministic)
  - Stage 6.6 — branch slicer (tree-sitter, optional dependency)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultline.replay.capture import write_stage_input
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_FILES_PER_FEATURE as _IMPORT_TREE_MAX_FILES,
    DEFAULT_MAX_SYMBOLS_PER_FEATURE as _IMPORT_TREE_MAX_SYMBOLS,
    build_artifact_payload as _import_tree_artifact,
    enrich_with_import_tree,
)
from faultline.pipeline_v2.stage_6_4_framework_enrich import (
    run_stage_6_4,
)
from faultline.pipeline_v2.stage_6_5_product_clusterer import (
    run_product_clusterer,
)
from faultline.pipeline_v2.stage_6_6_branch_slicer import (
    run_stage_6_6,
)
from faultline.pipeline_v2.stage_6_metrics import stage_6_metrics
from faultline.pipeline_v2.stage_7_output import write_stage_artifact


@dataclass
class EnrichResult:
    """Stage 6 family outputs the orchestrator threads onward."""

    features: list[Any]
    product_features: list[Any]
    dev_to_product_map: dict[str, Any]
    product_telemetry: dict[str, Any]
    enrichment: Any
    enrich_result: Any
    framework_enrich_telemetry: dict[str, Any]
    branch_result: Any
    branch_slicer_telemetry: dict[str, Any]


def run_enrich_phase(
    *,
    ctx: Any,
    features: list[Any],
    run_dir: Path,
    effective_max_tree_depth: int,
) -> EnrichResult:
    """Run Stage 6 → 6.5 → 6.3 → 6.4 → 6.6 enrichment passes.

    Body moved verbatim from ``run_pipeline_v2``.
    """
    # ``_isolate`` is looked up through the run module so it stays the
    # single deep-copy call site to instrument later.
    from faultline.pipeline_v2 import run as _run

    # ── Stage 6 — metrics enrichment ───────────────────────────────
    # NOTE: we feed Stage 6 the SAME ``features`` reference (not a
    # deep-copy) so the bipartite mutations made in Stage 5.5 survive
    # into the final output. Stage 6's contract is to fill blame /
    # coverage / commit fields; it MUST NOT mutate Feature.paths or
    # Flow.paths (which the bipartite IDs were minted from).
    write_stage_input(run_dir, 6, "metrics", {
        "features": features,
        "ctx": ctx,
    })
    with StageLogger(run_dir, 6, "metrics") as log6:
        features = stage_6_metrics(features, _run._isolate(ctx))
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
    write_stage_input(run_dir, 6, "product_clusterer", {
        "ctx": ctx,
        "features": features,
    })
    with StageLogger(run_dir, 6, "product_clusterer") as log6_5:
        product_features, dev_to_product_map, product_telemetry = (
            run_product_clusterer(_run._isolate(ctx), features, log=log6_5)
        )
        # Stamp the FIRST product label onto each dev feature as the
        # legacy single-valued ``product_feature_id`` for back-compat
        # with consumers that read the Layer-1 ↔ Layer-2 pointer
        # before the bipartite extension lands in their stack. The
        # full multi-label set lives in the orchestrator's mapping
        # dict and is preserved in the scan_meta telemetry below.
        for feat in features:
            labels = dev_to_product_map.get(feat.name)
            if labels:
                feat.product_feature_id = labels[0]
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
    write_stage_input(run_dir, 6, "import_tree", {
        "ctx": ctx,
        "features": features,
        "effective_max_tree_depth": effective_max_tree_depth,
    })
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
    write_stage_input(run_dir, 6, "framework_enrich", {
        "ctx": ctx,
        "features": features,
    })
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
    write_stage_input(run_dir, 6, "branch_slicer", {
        "ctx": ctx,
        "features": features,
    })
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

    return EnrichResult(
        features=features,
        product_features=product_features,
        dev_to_product_map=dev_to_product_map,
        product_telemetry=product_telemetry,
        enrichment=enrichment,
        enrich_result=enrich_result,
        framework_enrich_telemetry=framework_enrich_telemetry,
        branch_result=branch_result,
        branch_slicer_telemetry=branch_slicer_telemetry,
    )


__all__ = [
    "EnrichResult",
    "run_enrich_phase",
]
