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
from faultline.pipeline_v2.llm_health import LlmHealth, stamp_llm_degraded
from faultline.pipeline_v2.incremental_wiring import (
    apply_incremental_bookkeeping,
    plan_uf_domain_allowlist,
)
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.replay.capture import write_stage_input
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
    feature_history: bool = True,
    llm_health: LlmHealth | None = None,
    repo_class_result: Any = None,
    prev_scan_json: dict[str, Any] | None = None,
) -> Path:
    """Run Stage 6.8 → 3.5 → 6.9 → 6.7/6.7c/6.7b → 6.95 → 7 and write output.

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

    # Replay v2 — input-only capture for the lineage connector (Stage 6.8
    # writes no output artifact; the replay chain needs its input to
    # re-stamp UUIDs + rebuild indexes when chaining into the finalize
    # stages). ``base_scan`` is captured as a PATH reference, not inline.
    write_stage_input(run_dir, 6, "lineage", {
        "features": features,
        "bipartite_flows": list(bipartite.flows),
        "stage1_out": stage1_out,
        "scan_meta": scan_meta,
        "base_scan_path": str(base_scan_path) if base_scan_path else None,
        "lineage_jaccard_threshold": rename_threshold,
        "since": since,
        "repo_path": str(repo_path),
    })
    lineage_result = run_stage_6_8(
        features,
        list(bipartite.flows),
        base_scan=base_scan_dict,
        extractor_signals=stage1_out,
        rename_threshold=rename_threshold,
        related_threshold=_RELATED_THRESHOLD,
    )

    # ── Stage 6.8b — system/background-flow classification (deterministic) ──
    # Tag every route's ``trigger`` (scheduled|queue|webhook|interactive) from
    # eval/system-flow-patterns.yaml: framework cron manifests + path-segment
    # conventions + job-library markers. Lets Stage 6.7 separate background jobs
    # (cron / queue / webhook) from interactive user journeys. No LLM; reads only
    # the cloned repo. SELF-DETECTING — a repo with no jobs tags everything
    # ``interactive`` (a clean no-op), so non-job repos are byte-identical.
    from faultline.pipeline_v2.system_flows import classify_routes
    scan_meta["system_flow_routes"] = classify_routes(
        lineage_result.routes_index, repo_path,
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
    write_stage_input(run_dir, 3, "flow_expansion", {
        "features": features,
        "ctx": ctx,
        "routes_index": lineage_result.routes_index,
        "bipartite_flows": list(bipartite.flows),
        "scan_meta": scan_meta,
    })
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
    write_stage_input(run_dir, 6, "test_strip", {
        "features": features,
        "bipartite_flows": list(bipartite.flows),
        "scan_meta": scan_meta,
    })
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

    # Stage 6.9b — generated-code strip (sibling of the test-strip): remove
    # machine-generated source (protobuf / sqlc / stringer / k8s-gen / dart …)
    # from the output tree so it never surfaces as a hand-authored product
    # feature. Same invariants (never mutates a metric scalar; drops emptied
    # features/flows). Disable with FAULTLINE_STAGE_6_9B_GENERATED_STRIP=0.
    from faultline.pipeline_v2.stage_6_9b_generated_strip import (
        stage_6_9b_enabled,
        strip_generated_paths,
    )

    generated_strip_telemetry: dict[str, int] = {
        "paths_removed": 0,
        "features_dropped": 0,
        "flows_dropped": 0,
    }
    write_stage_input(run_dir, 6, "generated_strip", {
        "features": features,
        "bipartite_flows": list(bipartite.flows),
        "product_features": product_features,
        "scan_meta": scan_meta,
    })
    with StageLogger(run_dir, 6, "generated_strip") as log6_9b:
        if stage_6_9b_enabled():
            generated_strip_telemetry = strip_generated_paths(
                features, bipartite.flows,
            )
            log6_9b.info(
                "generated_strip: paths_removed=%d features_dropped=%d "
                "flows_dropped=%d"
                % (
                    generated_strip_telemetry["paths_removed"],
                    generated_strip_telemetry["features_dropped"],
                    generated_strip_telemetry["flows_dropped"],
                ),
            )
        else:
            generated_strip_telemetry["disabled"] = True
            log6_9b.info("generated_strip: disabled via %s=0"
                         % "FAULTLINE_STAGE_6_9B_GENERATED_STRIP")
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="generated_strip",
            payload=generated_strip_telemetry,
            run_dir=run_dir,
        )
    scan_meta["stage_6_9b_generated_strip"] = dict(generated_strip_telemetry)

    # Reconcile Layer-2 after the strips. A product feature whose only member
    # was a test-only / generated-only developer feature is now empty — the
    # strips dropped that feature here in the finalize phase, AFTER Stage 8.6's
    # phantom drop ran. Re-apply the same deterministic, path-preserving rule now
    # that the developer-feature set is final, so a content-less duplicate row
    # (e.g. an "Integrations" cluster pointing only at e2e/ + tests/ paths) never
    # reaches output. No-op when neither strip dropped a feature.
    if test_strip_telemetry.get("features_dropped") or generated_strip_telemetry.get(
        "features_dropped"
    ):
        from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
            drop_phantom_product_features,
        )
        product_features, pf_phantom_post = drop_phantom_product_features(
            features, product_features,
        )
        scan_meta["stage_6_9_test_strip"]["pf_dropped_phantom"] = pf_phantom_post

    # ── Stage 0.7 exit gate — UF-synthesis suppression (Phase C) ────
    # A CONFIDENT non-product repo_class verdict (library / cli-tool /
    # infra-daemon / framework) means this scan unit has no user
    # journeys to synthesize: the whole UF family (6.7 rollup, 6.7c
    # splitter, 6.7b refiner, 6.7d abstraction) is SKIPPED and
    # ``user_flows: []`` is emitted with an explicit
    # ``scan_meta.uf_suppressed_reason``. The developer-feature/flow
    # skeleton above is untouched. Fail-open by construction: ambiguous
    # verdicts classify product-app and never reach here; the
    # ``FAULTLINE_REPO_CLASS_GATE=0`` kill-switch disables suppression
    # (the verdict itself is still emitted). Spec: StackProfile Phase C.
    from faultline.pipeline_v2.stage_0_7_repo_class import (
        should_suppress_user_flows,
        suppression_reason,
    )

    uf_suppressed = should_suppress_user_flows(repo_class_result)
    if uf_suppressed:
        uf_reason = suppression_reason(repo_class_result)
        scan_meta["uf_suppressed_reason"] = uf_reason
        uf_marker = {"suppressed": True, "reason": uf_reason}
        user_flows: list = []
        with StageLogger(run_dir, 6, "user_flows") as log6_7:
            log6_7.info(
                "user_flows: SUPPRESSED (%s, confidence=%.2f) — "
                "non-product scan unit exits the product funnel; "
                "developer features/flows unaffected"
                % (uf_reason, repo_class_result.confidence),
            )
            write_stage_artifact(
                ctx.repo_path,
                stage_index=6,
                stage_name="user_flows",
                payload={
                    **uf_marker,
                    "repo_class": repo_class_result.repo_class,
                    "confidence": repo_class_result.confidence,
                    "user_flows": [],
                },
                run_dir=run_dir,
            )
        scan_meta["stage_6_7_user_flows"] = dict(uf_marker)
        scan_meta["stage_6_7c_uf_splitter"] = dict(uf_marker)
        scan_meta["stage_6_7b_uf_refiner"] = dict(uf_marker)

    # ── Stage 6.7 — User-Flow rollup (Layer-2-for-flows, $0 LLM) ────
    # Deterministic post-pass: rolls the code-grain flow store up into
    # product-grain user_flows[] and stamps Flow.user_flow_id. Runs
    # after product_features (6.5) + bipartite store + test_strip so
    # domains, cross-links, and the final flow set all exist. Additive —
    # mirrors the developer_feature → product_feature model for flows.
    from faultline.pipeline_v2.product_strings import collect_product_strings
    from faultline.pipeline_v2.stage_6_7_user_flows import run_user_flow_rollup

    # Naming-evidence core (2026-06) — collect the product-string index
    # ONCE over every member file (features + flows) and share it with
    # Stage 6.7 (slot-consistent resource labels) and Stage 6.7b (UF
    # refiner evidence + name validation). Deterministic, $0 LLM,
    # README structurally excluded inside the collector.
    if not uf_suppressed:
        ps_candidates: set[str] = set()
        for f in features:
            ps_candidates.update(f.paths or [])
            ps_candidates.update(mf.path for mf in (f.member_files or []))
        for fl in bipartite.flows:
            ps_candidates.update(fl.paths or [])
            if fl.entry_point_file:
                ps_candidates.add(fl.entry_point_file)
        product_strings = collect_product_strings(repo_path, ps_candidates)

        user_flows: list = []
        write_stage_input(run_dir, 6, "user_flows", {
            "bipartite_flows": list(bipartite.flows),
            "features": features,
            "routes_index": lineage_result.routes_index,
            "scan_meta": scan_meta,
            "repo_path": str(repo_path),
        })
        with StageLogger(run_dir, 6, "user_flows") as log6_7:
            user_flows, uf_telemetry = run_user_flow_rollup(
                bipartite.flows, features,
                routes_index=lineage_result.routes_index,
                product_strings=product_strings,
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
        # Content-hash LLM cache for the UF-path stages (6.7c split + 6.7b
        # refine): same backend Stage 3 / Stage 8 use (threaded on the scan
        # context by run.py). A warm entry replays the stage's PARSED LLM
        # output byte-identically at $0 on an unchanged repo; ``None`` (or the
        # per-stage env opt-outs) behaves exactly as pre-cache. Best-effort —
        # any cache fault inside the stages degrades to a live call.
        _uf_llm_cache = getattr(ctx, "cache_backend", None)
        write_stage_input(run_dir, 6, "uf_splitter", {
            "user_flows": user_flows,
            "bipartite_flows": list(bipartite.flows),
            "ctx": ctx,
            "scan_meta": scan_meta,
        })
        with StageLogger(run_dir, 6, "uf_splitter") as log6_7c:
            user_flows, uf_split_telemetry = split_mega_user_flows(
                user_flows,
                bipartite.flows,
                cost_tracker=tracker,
                log=log6_7c,
                llm_health=llm_health,
                cache=_uf_llm_cache,
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
        write_stage_input(run_dir, 6, "uf_refiner", {
            "user_flows": user_flows,
            "bipartite_flows": list(bipartite.flows),
            "model_id": model_id,
            "ctx": ctx,
            "scan_meta": scan_meta,
            "features": features,
            "repo_path": str(repo_path),
            "is_full_scan": is_full_scan,
        })
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
                llm_health=llm_health,
                product_strings=product_strings,
                cache=_uf_llm_cache,
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

    # ── Stage 6.7d — LLM product/journey abstraction (opt-in, OFF) ──
    # Crosses the code-grain → product-grain gap the deterministic stages
    # structurally cannot: REWRITES user_flows[] + product_features[] at
    # journey/capability grain via a Sonnet call (abstraction) + a Haiku call
    # (re-attribution) over a code-grounded
    # digest (NO README). Output-layer only — the central flows[] graph is
    # untouched. Default OFF; FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION=1. On any
    # LLM failure the deterministic arrays pass through byte-identical.
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        is_enabled as _s67d_enabled,
        run_journey_abstraction,
    )
    if _s67d_enabled() and not uf_suppressed:
        write_stage_input(run_dir, 6, "journey_abstraction", {
            "user_flows": user_flows,
            "product_features": product_features,
            "features": features,
            "routes_index": lineage_result.routes_index,
            "model_id": model_id,
            "scan_meta": scan_meta,
            "repo_path": str(repo_path),
        })
        with StageLogger(run_dir, 6, "journey_abstraction") as log6_7d:
            # A content-hash cache backend makes a re-scan of an unchanged repo
            # byte-identical (same digest + models → same key → replayed LLM
            # answers). Best-effort — a cache fault degrades to a live call.
            from faultline.cache import get_cache_backend
            try:
                _s67d_cache = get_cache_backend()
            except Exception:  # noqa: BLE001 — caching is best-effort, never fatal
                _s67d_cache = None
            # Phase 2 anchor-ingest (OPT-IN): mine deterministic product-capability
            # anchors → clean alignment pool ONLY when align is enabled (default OFF
            # — align degrades stability on noisy pools). Skip the file-walk cost on
            # the default free-gen path; extraction failure must never crash a scan.
            _s67d_anchors = None
            try:
                from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
                    align_enabled,
                )
                if align_enabled():
                    from faultline.pipeline_v2.anchor_extractors import (
                        build_alignment_pool, extract_raw_anchors,
                    )
                    _s67d_anchors = build_alignment_pool(extract_raw_anchors(repo_path))
            except Exception:  # noqa: BLE001 — anchors are optional; degrade to free-gen
                _s67d_anchors = None
            (
                user_flows,
                product_features,
                s67d_dev_map,
                s67d_telemetry,
            ) = run_journey_abstraction(
                user_flows,
                product_features,
                features,
                lineage_result.routes_index,
                product_anchors=_s67d_anchors,
                model=model_id,
                cost_tracker=tracker,
                cache=_s67d_cache,
                log=log6_7d,
                llm_health=llm_health,
            )
            # Re-stamp dev features' product_feature_id so the bipartite /
            # output linkage stays coherent with the rewritten product layer.
            if s67d_dev_map:
                for _dev in features:
                    _slugs = s67d_dev_map.get(getattr(_dev, "name", None))
                    if _slugs:
                        _dev.product_feature_id = _slugs[0]
            write_stage_artifact(
                ctx.repo_path,
                stage_index=6,
                stage_name="journey_abstraction",
                payload={
                    **s67d_telemetry,
                    "user_flows": [uf.model_dump() for uf in user_flows],
                    "product_features": [
                        {"name": pf.name, "display_name": pf.display_name,
                         "n_paths": len(pf.paths)}
                        for pf in product_features
                    ],
                },
                run_dir=run_dir,
            )
        # ``first_draw_spec`` (mission-92 retry observability) is artifact +
        # llm-cache ONLY — keep it out of scan_meta so the final scan JSON
        # stays lean and byte-unchanged even on keyed runs that retried.
        scan_meta["stage_6_7d_journey_abstraction"] = {
            k: v for k, v in s67d_telemetry.items() if k != "first_draw_spec"
        }

    # ── Phase 3 — DUAL-EVIDENCE + confidence (OPT-IN, deterministic, $0 LLM) ──
    # Attach code + product-source anchor corroboration + a confidence score to
    # the final product features / user flows. Anchors are EVIDENCE here (a match
    # confirms a name), not a naming constraint — safe on noisy pools. Best-effort.
    from faultline.pipeline_v2.dual_evidence import dual_evidence_enabled
    if dual_evidence_enabled():
        write_stage_input(run_dir, 6, "dual_evidence", {
            "product_features": product_features,
            "user_flows": user_flows,
            "scan_meta": scan_meta,
            "repo_path": str(repo_path),
        })
        with StageLogger(run_dir, 6, "dual_evidence") as log_de:
            try:
                from faultline.pipeline_v2.anchor_extractors import (
                    build_alignment_pool, extract_raw_anchors,
                )
                from faultline.pipeline_v2.dual_evidence import attach_dual_evidence
                _de_anchors = build_alignment_pool(extract_raw_anchors(repo_path))
                de_stats = attach_dual_evidence(product_features, user_flows, _de_anchors)
                scan_meta["stage_dual_evidence"] = dict(de_stats)
                log_de.info(
                    "dual_evidence: pf %d/%d, uf %d/%d corroborated" % (
                        de_stats["pf_corroborated"], de_stats["pf"],
                        de_stats["uf_corroborated"], de_stats["uf"]),
                    feature=None)
            except Exception as _de_exc:  # noqa: BLE001 — evidence is best-effort, never fatal
                log_de.info(f"dual_evidence skipped: {_de_exc}", feature=None)

    # ── Stage 6.95 — per-entity git-history timeline ────────────────
    # Deterministic, pure, $0 LLM. Runs LAST before output by design:
    # product-feature ``paths`` are final (Stage 8 + 8.5) and user-flow
    # membership is final (6.7 + 6.7c). Buckets the IN-MEMORY commit
    # list (no new git calls) into ISO-week series + events + a
    # test-efficacy verdict per product feature / user flow. Additive —
    # only the new ``history`` field is written. ``--no-feature-history``
    # skips the stage entirely (telemetry records the skip).
    from faultline.pipeline_v2.stage_6_95_history import stage_6_95_history
    history_telemetry: dict[str, Any] = {"skipped": True}
    write_stage_input(run_dir, 6, "history", {
        "product_features": product_features,
        "user_flows": user_flows,
        "bipartite_flows": list(bipartite.flows),
        "features": features,
        "ctx": ctx,
        "feature_history": feature_history,
        "scan_meta": scan_meta,
    })
    with StageLogger(run_dir, 6, "history") as log6_95:
        if feature_history:
            history_telemetry = stage_6_95_history(
                product_features,
                user_flows,
                list(bipartite.flows),
                features,
                ctx.commits,
            )
            log6_95.info(
                "history: pf_scored=%d/%d uf_scored=%d/%d "
                "gated(pf=%d uf=%d) verdicts=%s "
                "cross_cut(events=%d entities=%d capped=%d) elapsed=%ss"
                % (
                    history_telemetry["product_features_scored"],
                    history_telemetry["product_features_total"],
                    history_telemetry["user_flows_scored"],
                    history_telemetry["user_flows_total"],
                    history_telemetry["product_features_gated"],
                    history_telemetry["user_flows_gated"],
                    history_telemetry["verdicts"],
                    history_telemetry["cross_cut_events_emitted"],
                    history_telemetry["cross_cut_entities_affected"],
                    history_telemetry["cross_cut_capped_out"],
                    history_telemetry["elapsed_sec"],
                ),
            )
        else:
            log6_95.info("history: skipped via --no-feature-history")
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="history",
            payload=history_telemetry,
            run_dir=run_dir,
        )
    scan_meta["stage_6_95"] = dict(history_telemetry)

    # ── Stage 6.96 — impact-over-time (import-graph blast radius) ───
    # Sibling of 6.95, same ``feature_history`` gate (no new CLI flag).
    # Materialises N evenly-spaced historical snapshots via short-lived
    # ``git worktree`` checkouts and records each history-bearing
    # entity's external-importer count per snapshot
    # (``history.impact``), plus coupling_spike / decoupled events and
    # an impact_trend — all scale-invariant, deterministic, $0 LLM.
    # Failure-proof by contract: per-snapshot failures and the wall
    # budget degrade to fewer points, never a failed scan.
    from faultline.pipeline_v2.stage_6_96_impact import stage_6_96_impact
    impact_telemetry: dict[str, Any] = {"skipped": True}
    write_stage_input(run_dir, 6, "impact", {
        "product_features": product_features,
        "user_flows": user_flows,
        "bipartite_flows": list(bipartite.flows),
        "features": features,
        "ctx": ctx,
        "repo_path": str(repo_path),
        "feature_history": feature_history,
        "scan_meta": scan_meta,
    })
    with StageLogger(run_dir, 6, "impact") as log6_96:
        if feature_history:
            impact_telemetry = stage_6_96_impact(
                product_features,
                user_flows,
                list(bipartite.flows),
                features,
                ctx.commits,
                repo_path,
                subpath=getattr(ctx, "subpath", None),
                log=log6_96,
            )
            log6_96.info(
                "impact: snapshots=%s/%s entities=%s/%s "
                "spikes=%s decoupled=%s trends=%s "
                "budget_exceeded=%s elapsed=%ss"
                % (
                    impact_telemetry.get("impact_snapshots", 0),
                    impact_telemetry.get("planned_snapshots", 0),
                    impact_telemetry.get("entities_with_impact", 0),
                    impact_telemetry.get("entities_total", 0),
                    impact_telemetry.get("coupling_spike_events", 0),
                    impact_telemetry.get("decoupled_events", 0),
                    impact_telemetry.get("trends"),
                    impact_telemetry.get("impact_budget_exceeded", False),
                    impact_telemetry.get("elapsed_sec"),
                ),
            )
        else:
            log6_96.info("impact: skipped via --no-feature-history")
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="impact",
            payload=impact_telemetry,
            run_dir=run_dir,
        )
    scan_meta["stage_6_96_impact"] = dict(impact_telemetry)

    # ── LLM-health stamp (fail LOUD on auth-dead scans) ────────────
    # Must run AFTER the last LLM-bearing stage (6.7b above) and BEFORE
    # Stage 7 writes the artifact, so ``scan_meta.llm_degraded`` + the
    # warning land in the persisted JSON. Additive — absent on healthy
    # scans. See ``llm_health`` module docstring for the
    # degrade-visibly-never-abort decision.
    if llm_health is not None:
        stamp_llm_degraded(scan_meta, llm_health)

    # ── Stage 6.6 — Monorepo Assembly View ($0, deterministic, additive) ──
    #    Re-project the flat ``features`` into a per-project structure +
    #    cross-project dependency graph. Gated on ``is_monorepo`` (single
    #    repos get the trivial ``{"is_monorepo": False}``). Never mutates
    #    ``features``. Defensive: an assembly failure degrades to ``{}`` +
    #    a warning so it can NEVER break a scan.
    monorepo_view: dict[str, Any] = {}
    write_stage_input(run_dir, 6, "monorepo_assembly", {
        "ctx": ctx,
        "features": features,
        "scan_meta": scan_meta,
    })
    with StageLogger(run_dir, 6, "monorepo_assembly") as log66:
        try:
            from faultline.pipeline_v2.stage_6_6_monorepo_assembly import (
                build_monorepo_assembly,
            )

            monorepo_view = build_monorepo_assembly(ctx, features)
            stats = monorepo_view.get("stats", {})
            log66.info(
                "monorepo=%s projects=%s edges=%s assigned=%.0f%%"
                % (
                    monorepo_view.get("is_monorepo", False),
                    stats.get("project_count", 0),
                    stats.get("edge_count", 0),
                    100.0 * float(stats.get("assigned_pct", 0.0)),
                ),
                feature=None,
            )
            write_stage_artifact(
                ctx.repo_path,
                stage_index=6,
                stage_name="monorepo_assembly",
                payload=monorepo_view,
                run_dir=run_dir,
            )
        except Exception as exc:  # noqa: BLE001 — never fail a scan on the view
            log66.info(f"monorepo assembly skipped ({exc})", feature=None)
            monorepo_view = {}
    if monorepo_view:
        scan_meta["monorepo_assembly"] = {
            k: v for k, v in monorepo_view.get("stats", {}).items()
        }

    # ── UF identity keeper (opt-in, output layer, $0 LLM) ──────────
    # Pins user-flow ``id`` + ``name`` across rescans against an
    # EXPLICITLY provided previous scan artifact (``--prev-scan`` /
    # ``prev_scan_json``). Per ``rule-cold-scan`` there is NO ambient
    # discovery: when the input is absent this block is skipped and the
    # scan output is byte-identical to today. Deterministic matching
    # only (member/route Jaccard + unique (resource,intent) key);
    # disappeared UFs are RETIRED in telemetry, never resurrected.
    if prev_scan_json is not None:
        from faultline.pipeline_v2.uf_identity_keeper import (
            apply_identity_keeper,
        )
        write_stage_input(run_dir, 7, "uf_identity", {
            "user_flows": user_flows,
            "bipartite_flows": list(bipartite.flows),
            "prev_scan_json": prev_scan_json,
            "scan_meta": scan_meta,
        })
        with StageLogger(run_dir, 7, "uf_identity") as log_uid:
            try:
                uid_telemetry = apply_identity_keeper(
                    user_flows, list(bipartite.flows), prev_scan_json,
                )
                scan_meta["uf_identity"] = uid_telemetry
                log_uid.info(
                    "uf_identity: pinned %d/%d new UFs (prev %d, "
                    "pin_rate %.2f, renames_prevented %d, retired %d, "
                    "fk_remapped %d, basis %s)"
                    % (
                        uid_telemetry["pinned"],
                        uid_telemetry["new_total"],
                        uid_telemetry["prev_total"],
                        uid_telemetry["pin_rate"],
                        uid_telemetry["renames_prevented"],
                        len(uid_telemetry["retired"]),
                        uid_telemetry["fk_remapped"],
                        uid_telemetry["basis_counts"],
                    ),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — identity is best-effort, never fatal
                scan_meta["uf_identity"] = {"enabled": False, "error": str(exc)}
                scan_meta.setdefault("warnings", []).append(
                    f"uf-identity-keeper failed ({exc}); scan continued unpinned"
                )
                log_uid.info(
                    f"uf_identity: FAILED ({exc}) — continuing unpinned",
                    feature=None,
                )
            write_stage_artifact(
                ctx.repo_path,
                stage_index=7,
                stage_name="uf_identity",
                payload=dict(scan_meta.get("uf_identity") or {}),
                run_dir=run_dir,
            )

    # ── Stage 6.97 — feature-level LOC ($0, deterministic, additive) ──
    #    Flat ``loc`` on every dev feature (sum of owned-file line counts;
    #    test/generated/lockfile/binary files excluded from the count) +
    #    dedup rollup on product features. Output-layer metric: NEVER
    #    mutates paths/flows. Defensive: a failure degrades to loc=None
    #    + a warning so it can NEVER break a scan.
    from faultline.pipeline_v2.stage_6_97_feature_loc import (
        apply_feature_loc,
        stage_6_97_enabled,
    )
    if stage_6_97_enabled():
        write_stage_input(run_dir, 6, "feature_loc", {
            "ctx": ctx,
            "features": features,
            "product_features": product_features,
            "scan_meta": scan_meta,
        })
        with StageLogger(run_dir, 6, "feature_loc") as log697:
            try:
                loc_telemetry = apply_feature_loc(
                    features, product_features, ctx.repo_path,
                )
                scan_meta["feature_loc"] = loc_telemetry
                log697.info(
                    "feature_loc: %d/%d dev features with loc>0 "
                    "(%d zero-loc-with-paths), %d files counted"
                    % (
                        loc_telemetry["features_with_loc"],
                        loc_telemetry["features_total"],
                        loc_telemetry["features_zero_loc_with_paths"],
                        loc_telemetry["files_counted"],
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=6,
                    stage_name="feature_loc",
                    payload=loc_telemetry,
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — metric must never break a scan
                scan_meta["feature_loc"] = {"enabled": False, "error": str(exc)}
                scan_meta.setdefault("warnings", []).append(
                    f"feature-loc stage failed ({exc}); loc fields left unset"
                )
                log697.info(
                    f"feature_loc: FAILED ({exc}) — continuing without loc",
                    feature=None,
                )

    # ── Stage 7 — output ───────────────────────────────────────────
    from faultline import __version__ as _engine_version  # late import
    write_stage_input(run_dir, 7, "output", {
        "features": features,
        "ctx": ctx,
        "scan_meta": scan_meta,
        "bipartite_flows": list(bipartite.flows),
        "bipartite_edges": list(bipartite.edges),
        "product_features": product_features,
        "user_flows": user_flows,
        "path_index": lineage_result.path_index,
        "routes_index": lineage_result.routes_index,
        "is_full_scan": is_full_scan,
        "since": since,
        "head": head,
        "days": days,
        "monorepo_view": monorepo_view,
    })
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
            monorepo=monorepo_view,
        )
        log7.info(f"wrote feature map to {out}", feature=None)

    return out


__all__ = [
    "run_finalize_phase",
]
