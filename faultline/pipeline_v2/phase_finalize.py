"""Per-tree pipeline phase — lineage, late flow passes, output.

Extracted from ``run.py`` (refactor/run-decomposition) as straight-line
code — same stage order, same StageLogger stage indexes/names, same
artifact filenames, same lazy (function-local) imports.

  - Stage 6.8  — lineage + indexes (UUID stamping, path/routes index)
  - Incremental scan bookkeeping (head SHA + metric carry-forward)
  - Stage 3.5  — flow expansion (deterministic call-graph)
  - Stage 6.9  — test-file output-tree strip
  - Stage 6.7  — User-Flow rollup ($0 LLM)
  - Stage 6.7c — Mega-UF semantic split (additive Sonnet)
  - Stage 6.7b — User-Flow LLM refiner (additive Haiku)
  - Stage 7    — output (FeatureMap assembly + writer)

``scan_meta`` is updated IN PLACE (lineage stats, incremental meta,
per-stage telemetry) exactly as the inline code did.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.incremental_wiring import (
    apply_incremental_bookkeeping,
    plan_uf_domain_allowlist,
)
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_7_output import (
    stage_7_output,
    write_stage_artifact,
)


def run_finalize_phase(
    *,
    repo_path: Path,
    ctx: Any,
    features: list[Any],
    bipartite: Any,
    product_features: list[Any],
    stage1_out: dict[str, Any],
    scan_meta: dict[str, Any],
    run_dir: Path,
    model_id: str,
    tracker: CostTracker,
    since: str | None,
    base_scan_path: Path | str | None,
    lineage_jaccard_threshold: float | None,
    incremental_base_scan: dict[str, Any] | None,
    incremental_gate_meta: dict[str, Any],
    out_path: Path | None,
    days: int,
) -> Path:
    """Run Stage 6.8 → 3.5 → 6.9 → 6.7/6.7c/6.7b → 7 and write output.

    Body moved verbatim from ``run_pipeline_v2``. Returns the written
    FeatureMap path.
    """
    # ── Stage 6.8 — lineage + indexes (Sprint 1, 2026-05-23) ──────
    # Pure post-pass: stamps stable UUIDs on every Feature + Flow,
    # builds path_index + routes_index. NEVER affects scan-quality
    # decisions. When ``base_scan_path`` is provided we match against
    # the previous scan for cross-scan UUID stability; otherwise every
    # feature/flow gets a fresh uuid4 (cold-scan default).
    from faultline.pipeline_v2.incremental import (
        load_base_scan as _load_base_scan,
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

    # Reuse the base scan already loaded by the Stage 2.5 incremental
    # gate (avoids re-parsing a large JSON). Falls back to a fresh load
    # for callers that pass ``base_scan_path`` for lineage WITHOUT
    # ``--since`` (full scan with lineage stamping).
    base_scan_dict: dict[str, Any] | None = incremental_base_scan
    if base_scan_dict is None and base_scan_path is not None:
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
    # Head SHA + Stage 6 metric carry-forward for untouched features —
    # see ``incremental_wiring.apply_incremental_bookkeeping``.
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
    scan_meta["lineage_rename_threshold"] = rename_threshold
    scan_meta["is_full_scan"] = is_full_scan
    scan_meta.update(incremental_meta)
    # Stage 2.5 LLM-gating telemetry (empty dict on a full scan).
    scan_meta.update(incremental_gate_meta)

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
        # max_depth=1 — a flow's attributed implementation is the entry
        # symbol + its DIRECT callees (same-file AND imported), with no
        # transitive recursion. Deeper walks turn each flow into the
        # whole transitive closure of the import graph and stop being a
        # narrative slice of ONE behaviour (measured: avg 62.5 nodes/flow
        # and 235/447 flows hitting the node cap at depth 4). Cross-file
        # resolution is independently hard-capped at depth 1 inside
        # build_call_graph; this aligns same-file recursion to the same
        # "entry + direct callees" target. Fan-in gating then demotes
        # high-fan-in shared infrastructure to role=shared (excluded
        # from core LOC, still recorded as a shared-dependency badge).
        fx = expand_flows(
            features,
            ctx,
            routes_index=lineage_result.routes_index,
            max_depth=1,
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

    # ── Stage 6.9 — test-file output-tree strip ────────────────────
    # "Post-everything tree hygiene": despite the 6.9 label this is
    # wired to run LAST (after every Stage 6.x metric pass, after Stage
    # 8 analyst + Stage 8.5 backfill, and after Stage 3.5 flow expansion
    # which populates loc_nodes/loc_edges) so it sees the fully-enriched
    # tree and cannot disturb any upstream computation. It removes
    # test-file entries from the OUTPUT TREE only and NEVER recomputes a
    # metric scalar — coverage_pct/health/bug_fix_ratio are computed in
    # Stage 6 WITH the test files on purpose. Disable with
    # FAULTLINE_STAGE_6_9_TEST_STRIP=0. See the module docstring.
    from faultline.pipeline_v2.stage_6_9_test_strip import (
        stage_6_9_enabled,
        strip_test_paths,
    )

    test_strip_telemetry: dict[str, int] = {
        "paths_removed": 0,
        "features_dropped": 0,
        "flows_dropped": 0,
        "flow_entries_recomputed": 0,
    }
    with StageLogger(run_dir, 6, "test_strip") as log6_9:
        if stage_6_9_enabled():
            test_strip_telemetry = strip_test_paths(features, bipartite.flows)
            log6_9.info(
                "test_strip: paths_removed=%d features_dropped=%d "
                "flows_dropped=%d flow_entries_recomputed=%d"
                % (
                    test_strip_telemetry["paths_removed"],
                    test_strip_telemetry["features_dropped"],
                    test_strip_telemetry["flows_dropped"],
                    test_strip_telemetry["flow_entries_recomputed"],
                ),
            )
        else:
            test_strip_telemetry["disabled"] = True
            log6_9.info("test_strip: disabled via %s=0"
                        % "FAULTLINE_STAGE_6_9_TEST_STRIP")
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="test_strip",
            payload=test_strip_telemetry,
            run_dir=run_dir,
        )
    scan_meta["stage_6_9_test_strip"] = dict(test_strip_telemetry)

    # ── Stage 6.7 — User-Flow rollup (Layer-2-for-flows, $0 LLM) ────
    # Deterministic post-pass: rolls the code-grain flow store up into
    # product-grain user_flows[] and stamps Flow.user_flow_id. Runs
    # after product_features (6.5) + bipartite store + test_strip so
    # domains, cross-links, and the final flow set all exist. Additive —
    # mirrors the developer_feature → product_feature model for flows.
    from faultline.pipeline_v2.stage_6_7_user_flows import run_user_flow_rollup
    user_flows: list = []
    with StageLogger(run_dir, 6, "user_flows") as log6_7:
        user_flows, uf_telemetry = run_user_flow_rollup(
            bipartite.flows, features,
            routes_index=lineage_result.routes_index,
        )
        log6_7.info(
            "user_flows: %d flows -> %d unique -> %d UF, %d domains, "
            "%d with cross_links (dedup_dropped=%d)"
            % (
                uf_telemetry["total_flows"],
                uf_telemetry["unique_flows"],
                uf_telemetry["user_flows"],
                uf_telemetry["domains"],
                uf_telemetry["uf_with_cross_links"],
                uf_telemetry["dedup_dropped"],
            ),
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="user_flows",
            payload={
                **uf_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
            },
            run_dir=run_dir,
        )
    scan_meta["stage_6_7_user_flows"] = dict(uf_telemetry)

    # ── Stage 6.7c — Mega-UF semantic split (additive Sonnet) ──────
    # 6.7's deterministic clusterer over-merges genuinely-distinct journeys
    # into a few mega-UFs (cal.com: one 'availability' UF spanned 33
    # journeys). A handful of LLM calls partition ONLY those mega-mixed UFs
    # into per-journey sub-UFs (recall-safe — unplaced members fall to a
    # residual sub-UF, no flow dropped). Runs BEFORE 6.7b so the refiner
    # names the split UFs. Shared CostTracker; graceful degrade keeps the
    # mega-UF on any LLM failure. Measured F1 64→74 on cal.com vs uf-golden.
    from faultline.pipeline_v2.stage_6_7c_uf_splitter import split_mega_user_flows
    with StageLogger(run_dir, 6, "uf_splitter") as log6_7c:
        user_flows, uf_split_telemetry = split_mega_user_flows(
            user_flows,
            bipartite.flows,
            cost_tracker=tracker,
            log=log6_7c,
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="uf_splitter",
            payload={
                **uf_split_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
            },
            run_dir=run_dir,
        )
    scan_meta["stage_6_7c_uf_splitter"] = dict(uf_split_telemetry)

    # ── Stage 6.7b — User-Flow LLM refiner (additive Haiku) ─────────
    # One Haiku call per domain over the deterministic 6.7 UF clusters:
    # journey-grain name/description, resolves intent="other", infers
    # ui_tier from the frontend surface, drafts AC from test-reach.
    # Membership/grain from 6.7 are NOT changed. Graceful per-domain
    # degrade: on any LLM failure the UFs keep their deterministic
    # name/intent. Uses the SAME shared CostTracker + model_id as the
    # rest of the LLM stages; no README, no .ai/specs.
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows
    with StageLogger(run_dir, 6, "uf_refiner") as log6_7b:
        # ── Incremental UF-refiner reuse (--since path ONLY) ───────
        # Only domains with a changed UF still get a Haiku call — see
        # ``incremental_wiring.plan_uf_domain_allowlist``. On a full /
        # cold scan ``domain_allowlist`` stays None → every domain is
        # refined, byte-identical to before (cold-scan rule).
        uf_domain_allowlist: set[str | None] | None = None
        if not is_full_scan and incremental_base_scan is not None:
            uf_domain_allowlist = plan_uf_domain_allowlist(
                user_flows, incremental_base_scan, log6_7b,
            )
        user_flows, uf_refine_telemetry = refine_user_flows(
            user_flows,
            bipartite.flows,
            model=model_id,
            cost_tracker=tracker,
            log=log6_7b,
            domain_allowlist=uf_domain_allowlist,
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="uf_refiner",
            payload={
                **uf_refine_telemetry,
                "user_flows": [uf.model_dump() for uf in user_flows],
            },
            run_dir=run_dir,
        )
    scan_meta["stage_6_7b_uf_refiner"] = dict(uf_refine_telemetry)

    # ── Stage 7 — output ───────────────────────────────────────────
    from faultline import __version__ as _engine_version  # late import
    with StageLogger(run_dir, 7, "output") as log7:
        out = stage_7_output(
            features, ctx, scan_meta, out_path,
            days=days,
            flows=bipartite.flows,
            feature_flow_edges=bipartite.edges,
            product_features=product_features,
            user_flows=user_flows,
            path_index=lineage_result.path_index,
            routes_index=lineage_result.routes_index,
            is_full_scan=is_full_scan,
            base_scan_commit=(since or ""),
            scan_commit=head,
            engine_version=_engine_version,
        )
        log7.info(f"wrote feature map to {out}", feature=None)

    return out


__all__ = [
    "run_finalize_phase",
]
