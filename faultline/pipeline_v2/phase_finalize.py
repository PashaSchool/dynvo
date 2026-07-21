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

import re
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
from faultline.pipeline_v2.overturn_ledger import (
    flush_pending as _arb_flush,
)


def _recover_uncovered_donors(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
    *,
    loc_only: bool = False,
) -> dict[str, Any] | None:
    """W1.1 donor re-cover — the post-finalize-conservation backstop run.

    The finalize conservation pass resettles by span-LOC majority and can
    leave a flowful PF with zero journeys (the in-6.7d backstop ran
    BEFORE it); W1 §E predicted the class and the 2026-07-06 validation
    wave shipped it (supabase ×4, midday 'Support' — validator I8).
    Re-runs the 6.7d backstop over the STAMPED dev→PF state: its
    reassign arm is conservation-compatible since W1.1 (same ruler as
    the recheck the caller runs after this), its synthesize arm mints
    ``synthesized``-tagged journeys the conservation ladder exempts —
    so the follow-up recheck can only confirm, never undo (fixpoint in
    one pass, deterministic). Returns telemetry, or ``None`` when the
    backstop kill-switch is off.
    """
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _backstop_uncovered_pfs,
        _pf_uf_backstop_enabled,
    )

    if not _pf_uf_backstop_enabled():
        return None
    stamped_map = {
        f.name: (f.product_feature_id,)
        for f in features
        if getattr(f, "layer", "developer") == "developer"
        and getattr(f, "name", None)
        and getattr(f, "product_feature_id", None)
    }
    bs_tele = _backstop_uncovered_pfs(
        user_flows, product_features, stamped_map, features, set(),
        loc_only=loc_only,
    )
    # Provisional ids → continue the stable numbering (content-sorted
    # among the new synths, appended after the existing UF-xxx block).
    max_id = 0
    for uf in user_flows:
        m = re.match(r"^UF-(\d+)$", str(uf.id or ""))
        if m:
            max_id = max(max_id, int(m.group(1)))
    fresh = [u for u in user_flows if u.id == "UF-000"]
    fresh.sort(key=lambda u: ((u.name or "").lower(),
                              str(u.resource or "")))
    for i, uf in enumerate(fresh, start=1):
        uf.id = f"UF-{max_id + i:03d}"
    return {
        "uncovered": bs_tele.get("pf_backstop_uncovered", 0),
        "reassigned_ufs": bs_tele.get("pf_backstop_reassigned_ufs", 0),
        "synthesized": bs_tele.get("pf_backstop_synthesized", 0),
        "locworthy": bs_tele.get("pf_backstop_locworthy", 0),
        "resolutions": bs_tele.get("pf_backstop_resolutions", []),
    }


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
    # B74 Seg B — the Stage-3 unit snapshot (name → sorted owned paths
    # at flow-derivation time) for the post-grain re-derivation cohort
    # selector. ``None`` (legacy callers / replay of older captures)
    # keeps Stage 6.865 un-entered.
    stage3_unit_snapshot: dict[str, list[str]] | None = None,
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

    # ── Stage 6.85 — product-surface taxonomy, Layer-1 tagging ─────────
    # Product-Spine §4.2 (Wave 2a): stamp ``surface_scope`` on every
    # routes_index entry + developer feature (product | marketing | docs |
    # legal | system | dev_tooling | shell). Runs AFTER 6.8b so the system
    # trigger verdicts exist; BEFORE the UF/PF stages so 6.7d's residual
    # ladder + container guard can consume the dev tags. Deterministic,
    # $0 LLM. Kill-switch FAULTLINE_SURFACE_TAXONOMY=0 (tags absent →
    # every consumer no-ops; the omit-unset serializers keep output
    # byte-identical to pre-W2a engines).
    from faultline.pipeline_v2.surface_taxonomy import tag_layer1
    scan_meta["surface_taxonomy"] = tag_layer1(
        features, lineage_result.routes_index, repo_path=repo_path,
    )

    # ── Stage 6.55 — page-interior structure (Product-Spine §4.6, W4) ──
    # Tree-sitter parse of PAGE route files into their interior render
    # tree (product components vs design-system primitives, labels,
    # definition spans). Runs AFTER 6.8 (needs routes_index), BEFORE
    # Stage 3.5 so the refined ``role="interior"`` attributions ride the
    # expansion. Deterministic, $0 LLM, content-hash cached; inactive
    # (byte-identical scans) when tree-sitter isn't installed.
    # Kill-switch FAULTLINE_STAGE_6_55=0.
    from faultline.pipeline_v2.stage_6_55_page_interior import (
        degenerate_span_stats,
        inject_interior_nodes,
        refine_flow_spans,
        run_stage_6_55,
    )
    write_stage_input(run_dir, 6, "page_interior", {
        "routes_index": lineage_result.routes_index,
        "ctx": ctx,
    })
    with StageLogger(run_dir, 6, "page_interior") as log6_55:
        interior_result = run_stage_6_55(
            ctx, lineage_result.routes_index, log6_55,
        )
        interior_telemetry: dict[str, Any] = {
            "active": interior_result.active,
        }
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
            ctx.repo_path,
            stage_index=6,
            stage_name="page_interior",
            payload=interior_telemetry,
            run_dir=run_dir,
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

    # ── Stage 6.55 (part 2) — interior nodes onto the expanded graph ──
    # The ``role="interior"`` attributions become FlowNodes (so
    # ``line_ranges`` / on-flow LOC accounting see real component spans)
    # and whole-file support nodes covering a resolved component source
    # are TIGHTENED to the definition span. Runs immediately after the
    # Stage 3.5 expansion that built the node graph; re-projects the
    # Phase-5 LOC views (idempotent). No-op when 6.55 was inactive.
    if interior_result.active:
        interior_telemetry["node_inject"] = inject_interior_nodes(
            features, interior_result,
        )
        interior_telemetry["degenerate_spans_after"] = (
            degenerate_span_stats(features)
        )
    scan_meta["stage_6_55_page_interior"] = dict(interior_telemetry)

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
                repo_root=getattr(ctx, "repo_path", None),
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

    # ── Stage 6.9c — schema-monolith member strip (B58-v3 Seg C) ────────
    # A whole-DB schema monolith (``schema.prisma`` / ``db/schema.rb``)
    # annexed onto a FOREIGN-anchored feature (the stage-2 route,schema
    # join + the 6.97 primary-owner tiebreak dumping the schema package's
    # shared plumbing onto a leaf route — documenso team.verify 1,202 LOC
    # where 152 exist) is stripped from every foreign claimant; the
    # schema package's own feature (majority-inside) keeps its claims.
    # Runs HERE — after the 6.9/6.9b strips (same output-tree contract),
    # BEFORE the 6.86 mint (PF member_files are carried from dev ledgers,
    # spine anchors read owned_paths_of — PF rows form clean) and BEFORE
    # Stage 6.97 (owned LOC re-truths). Monoliths carry no flows, so the
    # journey layer is structurally unaffected. Kill-switch: the pass
    # only runs under FAULTLINE_GRAIN_WAVE=1 (default OFF) — unset/=0 is
    # byte-identical (no scan_meta key, no artifact).
    _sms_dropped = 0
    from faultline.pipeline_v2.schema_member_strip import (
        grain_wave_enabled,
        strip_schema_monolith_members,
    )
    if grain_wave_enabled():
        with StageLogger(run_dir, 6, "schema_member_strip") as log6_9c:
            try:
                sms_tele = strip_schema_monolith_members(
                    features, product_features,
                )
                _sms_dropped = sms_tele.get("features_dropped", 0)
                if sms_tele.get("paths_removed") or sms_tele.get("no_home"):
                    scan_meta["schema_member_strip"] = sms_tele
                log6_9c.info(
                    "schema_member_strip: pkgs=%d paths_removed=%d "
                    "features_stripped=%d dropped=%d no_home=%d" % (
                        len(sms_tele.get("packages", [])),
                        sms_tele.get("paths_removed", 0),
                        sms_tele.get("features_stripped", 0),
                        _sms_dropped,
                        len(sms_tele.get("no_home", [])),
                    ),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log6_9c.info(
                    f"schema_member_strip: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # Reconcile Layer-2 after the strips. A product feature whose only member
    # was a test-only / generated-only developer feature is now empty — the
    # strips dropped that feature here in the finalize phase, AFTER Stage 8.6's
    # phantom drop ran. Re-apply the same deterministic, path-preserving rule now
    # that the developer-feature set is final, so a content-less duplicate row
    # (e.g. an "Integrations" cluster pointing only at e2e/ + tests/ paths) never
    # reaches output. No-op when neither strip dropped a feature.
    if test_strip_telemetry.get("features_dropped") or generated_strip_telemetry.get(
        "features_dropped"
    ) or _sms_dropped:
        from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
            drop_phantom_product_features,
        )
        product_features, pf_phantom_post = drop_phantom_product_features(
            features, product_features,
        )
        scan_meta["stage_6_9_test_strip"]["pf_dropped_phantom"] = pf_phantom_post

    # ── Stage 6.86 — Anchored PF minting (Product-Spine §4.3, Wave 2b) ──
    # THE membership spine: PF candidates come ONLY from ranked anchor
    # sources (route subtrees / workspaces / schema domains / hub
    # families / authored feature-dirs / service-dirs); dev→PF derives
    # deterministically from anchor lineage (θ=0.5 majority +
    # specificity reduction + fixed source-rank near-ties). REPLACES the
    # Stage 6.5/8 product layer wholesale and retires 6.7d Call-2 as the
    # membership oracle. Runs AFTER the 6.9/6.9b strips (final dev
    # membership) and BEFORE the UF stages so 6.7's UF→PF vote, the
    # conservation law, and 6.7d all consume anchored stamps. Residual
    # devs go to the platform_infrastructure[] lane (operator amendment
    # 2026-07-06: the Shared Platform PF no longer exists on this path).
    # Deterministic, $0 LLM. Kill-switch FAULTLINE_SPINE_ANCHORED_MINT=0
    # restores the old path byte-identically.
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        anchored_mint_enabled,
        run_anchored_mint,
    )

    anchored_mint_applied = False
    anchored_hub_stamps: dict[str, str] = {}
    instrument_dirs: frozenset[str] = frozenset()  # W4.2 Fix 1
    dev_artifact_units: frozenset[str] = frozenset()  # B28 P-D marks
    _hh_anchor_registry: dict[str, Any] | None = None  # B69-v2 (6.99b rail)
    if anchored_mint_enabled():
        write_stage_input(run_dir, 6, "anchored_mint", {
            "features": features,
            "product_features": product_features,
            "routes_index": lineage_result.routes_index,
            "stage1_out": stage1_out,
            "ctx": ctx,
            "scan_meta": scan_meta,
        })
        with StageLogger(run_dir, 6, "anchored_mint") as log_mint:
            try:
                # Nav labels confirm anchors (ranking evidence only) —
                # normalized first meaningful href segments from the
                # deterministic product-string collector's nav pairs.
                _nav_keys: set[str] = set()
                try:
                    from faultline.pipeline_v2.product_strings import (
                        collect_product_strings,
                        normalize_href,
                    )
                    from faultline.pipeline_v2.spine_anchors import (
                        normalize_anchor_key,
                    )
                    _nav_candidates: set[str] = set()
                    for _f in features:
                        _nav_candidates.update(_f.paths or [])
                    _nav_index = collect_product_strings(
                        repo_path, _nav_candidates)
                    for _pairs in _nav_index.nav_pairs_by_file.values():
                        for _label, _href in _pairs:
                            if not _href:
                                continue
                            norm = normalize_href(str(_href)) or ""
                            for seg in norm.strip("/").split("/"):
                                if seg and not seg.startswith(":"):
                                    _nav_keys.add(normalize_anchor_key(seg))
                                    break
                except Exception:  # noqa: BLE001 — confirmers are optional
                    _nav_keys = set()
                mint_pfs, mint_tele = run_anchored_mint(
                    features,
                    lineage_result.routes_index,
                    ctx,
                    extractor_signals=stage1_out,
                    nav_keys=frozenset(_nav_keys),
                )
                # B69-v2 — pop the anchor-registry side-channel BEFORE the
                # tele reaches scan_meta / the stage artifact (live
                # SpineAnchor objects, consumed by the Stage 6.99b post-UF
                # rehome rail below; present only under
                # FAULTLINE_HOMING_HYGIENE=1).
                _hh_anchor_registry = mint_tele.pop(
                    "homing_hygiene_anchor_registry", None)
                if mint_tele.get("applied"):
                    product_features = mint_pfs
                    anchored_mint_applied = True
                    anchored_hub_stamps = dict(
                        mint_tele.get("hub_family_stamps") or {})
                    # W4.2 Fix 1 — instrument dirs feed the emission
                    # classifier (dev_tooling scope) + the seed guards.
                    instrument_dirs = frozenset(
                        (mint_tele.get("technology_instruments") or {})
                        .get("dirs") or []
                    )
                    # B28 P-D — hub-fixture marks ride the same tele to
                    # the emission taxonomy (mark-only at 6.86; the
                    # R1/R2 rails + lane consumption live emission-side).
                    dev_artifact_units = frozenset(
                        (mint_tele.get("technology_instruments") or {})
                        .get("dev_artifact_units") or ()
                    )
                scan_meta["stage_6_86_anchored_mint"] = {
                    k: v for k, v in mint_tele.items()
                    if k != "hub_family_stamps"
                }
                log_mint.info(
                    "anchored_mint: applied=%s anchors=%d pf=%d "
                    "U=%d(cap=%d shell=%d) tie=%d none=%d folds(u=%d p=%d "
                    "i=%d) infra=%d churn=%.1f%%"
                    % (
                        mint_tele.get("applied"),
                        mint_tele.get("anchors_total", 0),
                        mint_tele.get("pf_minted", 0),
                        mint_tele.get("unique", 0),
                        mint_tele.get("unique_capability", 0),
                        mint_tele.get("unique_shell", 0),
                        mint_tele.get("near_tie", 0),
                        mint_tele.get("none", 0),
                        mint_tele.get("fold_union_plurality", 0),
                        mint_tele.get("fold_parent", 0),
                        mint_tele.get("fold_import", 0),
                        mint_tele.get("infra_lane", 0),
                        100.0 * mint_tele.get("churn_pct", 0.0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=6,
                    stage_name="anchored_mint",
                    payload={k: v for k, v in mint_tele.items()
                             if k != "hub_family_stamps"},
                    run_dir=run_dir,
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

    # ── W4.3 — lane excavation (Product-Spine, w43-diagnosis) ──────────
    # With the lane stamped, lift PRODUCT back out of it: app-shell lane
    # groups grow domain-dir candidate anchors from their OWN content,
    # merged with the existing anchor set; every candidate faces the
    # SAME Stage-6.86 mint bar (incl. the W4.2 instrument dirs). Runs
    # BEFORE the flow-span split / UF family so every downstream stage
    # sees the excavated PFs. Kill-switch FAULTLINE_LANE_EXCAVATION=0.
    if anchored_mint_applied:
        from faultline.pipeline_v2.lane_excavation import (
            lane_excavation_enabled,
            run_lane_excavation,
        )
        if lane_excavation_enabled():
            with StageLogger(run_dir, 6, "lane_excavation") as log_exc:
                try:
                    exc_tele = run_lane_excavation(
                        features, product_features,
                        lineage_result.routes_index, ctx,
                        extractor_signals=stage1_out,
                        instrument_dirs=instrument_dirs,
                        feature_flow_edges=list(bipartite.edges),
                    )
                    if exc_tele.get("groups"):
                        scan_meta["lane_excavation"] = exc_tele
                    log_exc.info(
                        "lane_excavation: groups=%d candidates=%d "
                        "minted=%d widened=%d moved=%d carved=%d "
                        "chunks=%d flows=%d loc=%d"
                        % (
                            exc_tele.get("groups", 0),
                            exc_tele.get("candidates", 0),
                            exc_tele.get("pfs_minted", 0),
                            exc_tele.get("pfs_widened", 0),
                            exc_tele.get("devs_moved", 0),
                            exc_tele.get("devs_carved", 0),
                            exc_tele.get("chunks", 0),
                            exc_tele.get("flows_excavated", 0),
                            exc_tele.get("loc_excavated", 0),
                        ),
                        feature=None,
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    scan_meta.setdefault("warnings", []).append(
                        f"lane-excavation failed ({exc}); lane left as-is"
                    )
                    log_exc.info(
                        f"lane_excavation: FAILED ({exc}) — lane left as-is",
                        feature=None,
                    )

    # ── W4 — cross-PF flow-attribution split (Product-Spine §4.6) ──────
    # With the anchored mint's total dev→PF stamps in place, split every
    # flow whose file surface spans multiple PFs' anchors: primary =
    # home-PF files (entry-owner, dev fallback); other PFs' files move
    # to the labeled ``Flow.shared_paths[]`` ledger; foreign whole-file
    # span guesses leave the node surface. Runs BEFORE the UF family so
    # journey attach (I15/I16) and on-flow accounting (I19) consume the
    # split projection. Deterministic, $0 LLM; conservation-counted.
    # Kill-switch FAULTLINE_FLOW_SPAN_SPLIT=0; anchored-mint-only.
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
        product_strings = None  # UF family skipped — no string index built
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

    # ── Stage 6.865 — B74 Seg B post-grain flow re-derivation (F3′) ────
    # Stage-8-born / re-membered dev features (path-set ≠ the unit Stage
    # 3 derived flows on — twenty object-record-2 49K loc / activities /
    # workflow-2 with 0 flows) re-run the EXISTING Stage-3 machinery
    # here, BETWEEN the 6.86 mint window and the Stage 6.7 UF family, so
    # the single existing mint sees the flows naturally (F3′ — zero new
    # mint channels). Flows land in feature.flows[] + the bipartite
    # mirror with Stage-3/5.5 writer identity. Full scans only (the
    # snapshot excludes spliced untouched features; cold-scan rule);
    # keyless scans run the deterministic cohort selection + candidate
    # enumeration and report them in scan_meta (honest no-client
    # degrade). Default ON since the 2026-07-21 pack-3 flip
    # (FAULTLINE_FLOW_REDERIVE_POSTGRAIN, KEY_SCHEMA 34);
    # explicit =0 keeps the stage un-entered, byte-identical. Telemetry
    # key + artifact only when the causal gate fires (Seg C openstatus
    # inertness law).
    from faultline.pipeline_v2.flow_rederive import (
        flow_rederive_enabled,
        run_flow_rederive,
    )
    if (
        flow_rederive_enabled()
        and not uf_suppressed
        and is_full_scan
        and stage3_unit_snapshot is not None
    ):
        write_stage_input(run_dir, 6, "flow_rederive", {
            "features": features,
            "bipartite_flows": list(bipartite.flows),
            "bipartite_edges": list(bipartite.edges),
            "routes_index": lineage_result.routes_index,
            "scan_meta": scan_meta,
            "ctx": ctx,
            "stage3_unit_snapshot": stage3_unit_snapshot,
            "repo_path": str(repo_path),
            "model_id": model_id,
        })
        with StageLogger(run_dir, 6, "flow_rederive") as log_frd:
            try:
                frd_tele = run_flow_rederive(
                    features, bipartite.flows, bipartite.edges, ctx,
                    stage3_unit_snapshot=stage3_unit_snapshot,
                    model=model_id,
                    routes_index=lineage_result.routes_index,
                    tracker=tracker,
                    llm_health=llm_health,
                    log=log_frd,
                )
                if frd_tele is not None:
                    scan_meta["flow_rederive"] = frd_tele
                    write_stage_artifact(
                        ctx.repo_path,
                        stage_index=6,
                        stage_name="flow_rederive",
                        payload=frd_tele,
                        run_dir=run_dir,
                    )
                else:
                    log_frd.info(
                        "flow_rederive: no causal candidates — inert "
                        "(armed no-fire boards stay byte-identical)",
                        feature=None,
                    )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flow-rederive failed ({exc}); "
                    f"pre-rederive flow store kept"
                )
                log_frd.info(
                    f"flow_rederive: FAILED ({exc}) — flow store kept",
                    feature=None,
                )

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

        # ── Stage 6.7a — deterministic UF pre-clustering (S2 Seg A, OFF) ─
        # Under FAULTLINE_UF_DET_AGGREGATION the journey STRUCTURE is folded
        # deterministically (one cluster per rollup domain, conservation-
        # complete member unions) and the LLM layer may ONLY NAME it: the
        # 6.7b refiner still refines presentation per domain (its contract
        # forbids membership changes), while the structural LLM stages —
        # 6.7c mega-split and the 6.7d journey rewrite — are SKIPPED below.
        # Consequence (probe 2026-07-18): UF-COUNT invariant to LLM death
        # (fail-open 264-vs-78 class dies structurally) and to resampling
        # (−26% whole-batch class). Default OFF → byte-identical.
        from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
            aggregate_user_flows as _det_aggregate,
            det_aggregation_enabled as _det_agg_on,
        )
        if _det_agg_on() and user_flows:
            write_stage_input(run_dir, 6, "det_aggregation", {
                "user_flows": user_flows,
                "scan_meta": scan_meta,
            })
            with StageLogger(run_dir, 6, "det_aggregation") as log6_7a:
                user_flows, det_agg_telemetry = _det_aggregate(user_flows)
                log6_7a.info(
                    "det_aggregation: %d rollup UFs -> %d domain clusters "
                    "(%d merged, %d singleton; members %d -> %d; "
                    "scattered=%d)" % (
                        det_agg_telemetry["input_ufs"],
                        det_agg_telemetry["clusters"],
                        det_agg_telemetry["merged_clusters"],
                        det_agg_telemetry["singleton_clusters"],
                        det_agg_telemetry["members_in"],
                        det_agg_telemetry["members_out"],
                        det_agg_telemetry["scattered"],
                    ),
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=6,
                    stage_name="det_aggregation",
                    payload={
                        **det_agg_telemetry,
                        "user_flows": [uf.model_dump() for uf in user_flows],
                    },
                    run_dir=run_dir,
                )
            # The fate map is per-input-UF detail — artifact-only; scan_meta
            # carries the counters (lean output law).
            scan_meta["stage_6_7a_det_aggregation"] = {
                k: v for k, v in det_agg_telemetry.items() if k != "fate"
            }

        # ── Stage 6.7c — Mega-UF semantic split (additive Sonnet) ──────
        # 6.7's deterministic clusterer over-merges genuinely-distinct journeys
        # into a few mega-UFs (cal.com: one 'availability' UF spanned 33
        # journeys). A handful of LLM calls partition ONLY those mega-mixed UFs
        # into per-journey sub-UFs (recall-safe — unplaced members fall to a
        # residual sub-UF, no flow dropped). Runs BEFORE 6.7b so the refiner
        # names the split UFs. Shared CostTracker; graceful degrade keeps the
        # mega-UF on any LLM failure. Measured F1 64→74 on cal.com vs uf-golden.
        # S2 Seg A: skipped under FAULTLINE_UF_DET_AGGREGATION — the split is a
        # STRUCTURAL LLM decision; under det-aggregation the LLM only names.
        from faultline.pipeline_v2.stage_6_7c_uf_splitter import split_mega_user_flows
        # Content-hash LLM cache for the UF-path stages (6.7c split + 6.7b
        # refine): same backend Stage 3 / Stage 8 use (threaded on the scan
        # context by run.py). A warm entry replays the stage's PARSED LLM
        # output byte-identically at $0 on an unchanged repo; ``None`` (or the
        # per-stage env opt-outs) behaves exactly as pre-cache. Best-effort —
        # any cache fault inside the stages degrades to a live call.
        _uf_llm_cache = getattr(ctx, "cache_backend", None)
        if not _det_agg_on():
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

    # ── §4.5 conservation on the ANCHORED KEYLESS path ($0) ─────────
    # With the anchored mint applied and 6.7d disabled (keyless scans),
    # the deterministic UF set must still obey the conservation law
    # against the anchored stamps: member flows' entries + span majority
    # inside the UF's PF closure, violators resettle, and no journey may
    # ride a lane resident (product_feature_id=None devs vote nothing —
    # terminal home binds the leftovers). The keyed path runs the same
    # law inside/after 6.7d, unchanged.
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        is_enabled as _s67d_enabled_probe,
    )
    from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
        det_aggregation_enabled as _det_agg_probe,
    )
    # S2 Seg A: with det-aggregation ON the 6.7d rewrite is skipped even on
    # the keyed channel, so the §4.5 conservation law must run HERE against
    # the deterministic clusters (same law the keyless path enforces).
    if (anchored_mint_applied and not uf_suppressed
            and (not _s67d_enabled_probe() or _det_agg_probe())
            and user_flows):
        from faultline.pipeline_v2.conservation import apply_uf_conservation
        with StageLogger(run_dir, 6, "spine_conservation") as log_sc:
            try:
                sc_tele = apply_uf_conservation(
                    user_flows, features, product_features,
                    null_shared_without_signal=True,
                )
                scan_meta["spine_conservation_keyless"] = sc_tele
                log_sc.info(
                    "spine_conservation (keyless): %s" % (sc_tele,),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log_sc.info(
                    f"spine_conservation: FAILED ({exc}) — continuing",
                    feature=None,
                )

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
    # S2 Seg A: under FAULTLINE_UF_DET_AGGREGATION the 6.7d structural rewrite
    # is skipped — the journey structure is the deterministic 6.7a clusters and
    # the LLM layer (6.7b refiner) only NAMES them (fit = output-layer law).
    if _s67d_enabled() and not uf_suppressed and not _det_agg_probe():
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
            # W4 §4.6 — page-interior sections extend the constrained
            # Call-1's citation vocabulary (anchored mode only). None
            # when Stage 6.55 was inactive → digest/prompt/cache stay
            # byte-identical to pre-W4.
            _s67d_interior = None
            if anchored_mint_applied and interior_result.active:
                try:
                    from faultline.pipeline_v2.stage_6_55_page_interior import (
                        build_interior_evidence,
                    )
                    _s67d_interior = build_interior_evidence(
                        interior_result, features, product_features,
                    )
                except Exception:  # noqa: BLE001 — evidence is optional
                    _s67d_interior = None
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
                # Product-Spine §4.3 (Wave 2b): with the anchored mint
                # applied, the PF universe is FIXED (Call-1 constrained
                # to cite it) and dev→PF comes from the lineage stamps —
                # Call-2, the per-item membership oracle (RC1), retires.
                anchored=anchored_mint_applied,
                interior_evidence=_s67d_interior,
            )
            # Re-stamp dev features' product_feature_id so the bipartite /
            # output linkage stays coherent with the rewritten product layer.
            if s67d_dev_map:
                for _dev in features:
                    _slugs = s67d_dev_map.get(getattr(_dev, "name", None))
                    if _slugs:
                        _dev.product_feature_id = _slugs[0]
            # Product-Spine §4.4 — re-enforce the hub/child relation on the
            # REWRITTEN product layer. ANCHORED path (operator amendment
            # 2026-07-06): every integration is its OWN sibling PF — the
            # mint's per-vendor family stamps are construction law, so we
            # re-assert THEM (never the W1 children-inherit-one-PF rule,
            # which would re-collapse the vendor grain). =0 path keeps the
            # W1 binding byte-identically.
            if s67d_telemetry.get("applied") and anchored_mint_applied:
                from faultline.pipeline_v2.stage_6_86_anchored_mint import (
                    enforce_hub_family_parity as _hub_parity,
                )
                _par_tele = _hub_parity(
                    features, product_features, anchored_hub_stamps)
                if _par_tele.get("checked"):
                    s67d_telemetry["hub_family_parity_post_67d"] = _par_tele
            elif s67d_telemetry.get("applied"):
                from faultline.pipeline_v2.hub_relation import (
                    apply_hub_pf_binding as _hub_bind,
                )
                _hub_tele = _hub_bind(features, product_features)
                if _hub_tele.get("hubs"):
                    s67d_telemetry["hub_binding_post_67d"] = _hub_tele
            if s67d_telemetry.get("applied"):
                # W1.1 — §4.5 at DEV grain (validator I9): Call-2 can
                # scatter a small flowful surface dev into the shared
                # bucket even when its flows' spans/entries sit inside ONE
                # real PF (Soc0 dev 'api' → labels, 2026-07-06). Re-home
                # on the conservation ACCEPT bar BEFORE the UF pass so the
                # resettles below see the corrected ownership.
                from faultline.pipeline_v2.conservation import (
                    apply_uf_conservation as _apply_cons,
                    rehome_shared_flowful_devs as _rehome,
                )
                _rehome_tele = _rehome(features, product_features)
                if _rehome_tele.get("checked"):
                    s67d_telemetry["dev_rehome_finalize"] = _rehome_tele
                # Product-Spine §4.5 — final conservation pass: the hub
                # binding above moves dev→PF, so UF↔PF closures moved with
                # it; re-settle violators and null any residual
                # Shared-Platform attachment that no real PF's code
                # supports (a UF may never ship attached to shared).
                _cons_tele = _apply_cons(
                    user_flows, features, product_features,
                    null_shared_without_signal=True,
                )
                s67d_telemetry["conservation_finalize"] = _cons_tele
                # W1.1 — donor re-cover (W1 §E residual, predicted): the
                # conservation pass above can leave a flowful PF with zero
                # journeys (it resettles by span-LOC; the in-6.7d backstop
                # ran BEFORE this pass) — the 2026-07-06 validation wave
                # shipped 4 such donors on supabase + 1 on midday (I8).
                # Re-run the backstop AFTER conservation + hub binding,
                # then recheck: fixpoint in one pass (see
                # _recover_uncovered_donors).
                _donor_tele = _recover_uncovered_donors(
                    user_flows, features, product_features,
                )
                if _donor_tele is not None:
                    s67d_telemetry["donor_backstop_finalize"] = _donor_tele
                    # Fixpoint recheck — expected no-op (synthesized
                    # journeys are conservation-exempt; reassignments
                    # passed the same ladder on the same ownership state).
                    _recheck = _apply_cons(
                        user_flows, features, product_features,
                        null_shared_without_signal=True,
                    )
                    s67d_telemetry["conservation_recheck"] = _recheck
                # W4 §4.6 — post-UF span-split second pass: LANE-homed
                # member flows (no ownership evidence at the first pass)
                # adopt their conservation-settled journey's capability
                # as home; other PFs' files leave paths for the labeled
                # shared ledger. Runs AFTER the final conservation /
                # donor recheck so the adopted homes are settled.
                if anchored_mint_applied and flow_span_split_enabled():
                    try:
                        _lane_home: dict[str, str] = {}
                        for _uf in user_flows:
                            _pfid = getattr(_uf, "product_feature_id", None)
                            if not _pfid:
                                continue
                            for _mid in _uf.member_flow_ids or []:
                                _lane_home.setdefault(_mid, _pfid)
                        scan_meta["flow_span_split_post_uf"] = (
                            split_cross_pf_flow_attribution(
                                features, product_features,
                                home_override=_lane_home,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        scan_meta.setdefault("warnings", []).append(
                            f"post-UF flow-span split failed ({exc})"
                        )
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

    # ── W3.1 D6 — route-group journey recall seeds (BOTH paths) ─────────
    # A >= 2-route product group no UF touches (tracecat tables/chat, the
    # comp Auditor class, supabase studio holes — validator I24) gets ONE
    # thin tagged seed journey built from the flows entering its own
    # files. Output-only (user_flows[] append), synthesized + low-
    # confidence tagged, real-PF-home required; groups with no flow
    # evidence or no PF home stay honest holes. Runs on the keyed AND
    # keyless paths — the hole class is path-independent.
    from faultline.pipeline_v2.route_group_recall import (
        route_group_seeds_enabled,
        seed_route_group_journeys,
    )
    # W4.2 Fix 2 — seed surface-guard: one classifier instance serves the
    # D6 + D9 seed channels (home-PF scope + instrument dirs). ``None``
    # under the taxonomy kill-switch → both guards no-op (pre-W4.2 path).
    from faultline.pipeline_v2.surface_taxonomy import (
        SurfaceScopeClassifier as _SeedClf,
        _route_by_file as _seed_rbf_of,
        taxonomy_enabled as _seed_taxonomy_enabled,
    )
    seed_clf = None
    seed_rbf: dict[str, Any] = {}
    if _seed_taxonomy_enabled():
        try:
            seed_clf = _SeedClf(
                repo_path=repo_path,
                routes_index=lineage_result.routes_index,
                instrument_dirs=instrument_dirs,
            )
            seed_rbf = dict(_seed_rbf_of(lineage_result.routes_index))
        except Exception:  # noqa: BLE001 — guard is best-effort
            seed_clf = None
    if route_group_seeds_enabled():
        # B69-v2 SPLIT ruling — the seed-birth hygiene pair (same-
        # (pf,resource) coalescence + method-derived intent) is the
        # SEED_HYGIENE family: board-wide blast radius at seeding, its
        # own flag and its own cycle, independent of the surgical 6.99b
        # HOMING rail (the keyed A/B showed the pair driving board churn
        # on both repos while the rail itself was exactly-one-action).
        from faultline.pipeline_v2.route_group_recall import (
            seed_hygiene_enabled as _seed_hh,
        )
        with StageLogger(run_dir, 6, "route_group_recall") as log_rgr:
            rgr_tele = seed_route_group_journeys(
                user_flows, features, product_features,
                list(bipartite.flows), lineage_result.routes_index,
                scope_classifier=seed_clf, route_by_file=seed_rbf,
                coalesce_same_pf_resource=_seed_hh(),
                derive_seed_intent=_seed_hh(),
            )
            if rgr_tele.get("holes") or rgr_tele.get("seeded"):
                scan_meta["route_group_recall"] = rgr_tele
            log_rgr.info(
                f"route-group recall: groups>=2 {rgr_tele.get('groups_ge2')}"
                f" holes {rgr_tele.get('holes')} seeded {rgr_tele.get('seeded')}"
                f" (no-flows {rgr_tele.get('skipped_no_flows')},"
                f" no-pf {rgr_tele.get('skipped_no_pf')},"
                f" non-product {rgr_tele.get('skipped_non_product_home')})",
            )

    # ── W3.2 — UF-evidence lane re-homing (anchored paths, BOTH) ───────
    # The W3.1 sink-kill parked the freed mass in the lane (corpus 85K →
    # 1.94M LOC); the journey-evidenced slice of it (final UFs citing a
    # lane dev's files, one-PF majority, self-evident, capacity-capped)
    # re-homes to the capability its journeys ride, provenance
    # ``fold:uf-evidence``. Runs AFTER 6.7d + route-group seeds (the
    # citations must reflect the FINAL journey layer) and BEFORE 6.97 so
    # loc accounting stamps the moved membership (loc-truth I13).
    # Deterministic, $0. Kill-switch: FAULTLINE_SPINE_LANE_REHOME=0.
    if anchored_mint_applied and not uf_suppressed:
        from faultline.pipeline_v2.lane_rehome import (
            lane_rehome_enabled,
            rehome_uf_cited_lane_devs,
        )
        if lane_rehome_enabled():
            with StageLogger(run_dir, 6, "lane_rehome") as log_lr:
                try:
                    lr_tele = rehome_uf_cited_lane_devs(
                        features, product_features, user_flows,
                        list(bipartite.flows), repo_path=repo_path,
                    )
                    scan_meta["lane_rehome"] = lr_tele
                    log_lr.info(
                        "lane_rehome: checked %d rehomed %d (%d LOC) "
                        "blocked conc=%d self=%d target=%d cap=%d" % (
                            lr_tele.get("checked", 0),
                            lr_tele.get("rehomed", 0),
                            lr_tele.get("rehomed_loc", 0),
                            lr_tele.get("blocked_concentration", 0),
                            lr_tele.get("blocked_self_evidence", 0),
                            lr_tele.get("blocked_target", 0),
                            lr_tele.get("blocked_cap", 0),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    log_lr.info(
                        f"lane_rehome: FAILED ({exc}) — lane left as-is",
                        feature=None,
                    )

    # S3 arbiter — apply deferred proposals at this pass boundary
    # (rung-priority == pass order; no-op when the flag is OFF).
    _arb_flush(product_features, note="post-lane-rehome")

    # ── Track-A A1 — provenance re-home (import-graph channel) ─────────
    # Complements lane_rehome's UF-CITATION channel with the ts_ast
    # IMPORT-GRAPH channel: a lane entry file re-homes to the journey PF it
    # provenance-connects to (imports that PF's first-party domain package)
    # ONLY when the journey layer unanimously agrees (confirmation-gated →
    # can only turn a FOREIGN entry NATIVE, never the reverse). Same window
    # as lane_rehome — AFTER the final journey layer, BEFORE 6.97 loc so the
    # moved membership is loc-stamped. Kill-switch FAULTLINE_PROV_ATTACH=0.
    if anchored_mint_applied and not uf_suppressed:
        from faultline.pipeline_v2.provenance_rehome import (
            prov_attach_enabled,
            run_provenance_rehome,
        )
        if prov_attach_enabled():
            with StageLogger(run_dir, 6, "provenance_rehome") as log_prh:
                try:
                    prh_tele = run_provenance_rehome(
                        user_flows, features, product_features, ctx,
                    )
                    if prh_tele.get("entries_rehomed"):
                        scan_meta["provenance_rehome"] = prh_tele
                    log_prh.info(
                        "provenance_rehome: confirmed %d rehomed %d "
                        "pfs_widened %d skip(conflict=%d owned=%d) ties=%d" % (
                            prh_tele.get("entries_confirmed", 0),
                            prh_tele.get("entries_rehomed", 0),
                            prh_tele.get("pfs_widened", 0),
                            prh_tele.get("skipped_journey_conflict", 0),
                            prh_tele.get("skipped_owned", 0),
                            prh_tele.get("abstained_ties", 0),
                        ),
                        feature=None,
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    log_prh.info(
                        f"provenance_rehome: FAILED ({exc}) — lane left as-is",
                        feature=None,
                    )

    # ── Stage 6.88 — sibling-anchor capability unification (B16 Part 2) ──
    # Collapse co-identity sibling route PFs (Soc0 investigation /
    # investigations-page / investigation-flow -> ONE) BEFORE 6.97 so the
    # merged body is loc-stamped / role-lane'd / path_index'd / I23-read as one
    # PF. Anchored path only; kill-switch FAULTLINE_PF_SIBLING_UNIFY=0.
    if anchored_mint_applied:
        from faultline.pipeline_v2.stage_6_88_sibling_unify import (
            sibling_unify_enabled,
            unify_sibling_anchors,
        )
        if sibling_unify_enabled():
            try:
                su_tele = unify_sibling_anchors(
                    user_flows, features, product_features)
                if su_tele.get("merged_away"):
                    scan_meta["sibling_unify"] = su_tele
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"sibling-unify failed ({exc}); PFs left separate")

    # S3 arbiter — apply deferred proposals at this pass boundary
    # (rung-priority == pass order; no-op when the flag is OFF).
    _arb_flush(product_features, note="post-6.88-unify")

    # ── W3.2 D9 — system journeys survive the keyed rewrite (BOTH paths) ──
    # wave31: 6.8b stamped system routes on 6/10 repos yet output carried
    # ZERO system-category UFs — the 6.7d rewrite rebuilds user_flows[]
    # from LLM journey specs and eats the thin member-less system seeds
    # the rollup minted (Soc0's 11 flow-less inngest jobs: matched, minted,
    # dropped). Same post-6.7d slot that keeps the route-group seeds
    # alive: re-mint what the rewrite dropped (dedup-aware — a keyless
    # pipeline that kept the rollup output no-ops) and re-stamp the
    # deterministic trigger verdicts onto rebuilt journeys whose member
    # flows ride system routes (unanimous-evidence bar). Deterministic,
    # $0 LLM. Kill-switch: FAULTLINE_SEED_SYSTEM_UFS=0 (shared with the
    # rollup synthesis).
    if not uf_suppressed:
        from faultline.pipeline_v2.stage_6_7_user_flows import (
            restamp_system_triggers,
            resynthesize_system_ufs,
        )
        with StageLogger(run_dir, 6, "system_uf_recall") as log_sys:
            try:
                sys_stamp_tele = restamp_system_triggers(
                    user_flows, list(bipartite.flows),
                    lineage_result.routes_index,
                )
                sys_mint_tele = resynthesize_system_ufs(
                    user_flows, list(bipartite.flows), features,
                    lineage_result.routes_index,
                    instrument_dirs=instrument_dirs,
                    scope_classifier=seed_clf,
                    route_by_file=seed_rbf,
                    product_features=product_features,
                )
                if (sys_mint_tele.get("minted")
                        or sys_mint_tele.get("skipped_existing")
                        or sys_stamp_tele.get("stamped")):
                    scan_meta["system_uf_recall"] = {
                        **sys_mint_tele, **sys_stamp_tele,
                    }
                log_sys.info(
                    "system_uf_recall: minted %d (skipped_existing %d, "
                    "instrument %d, non-product %d), "
                    "triggers re-stamped %d" % (
                        sys_mint_tele.get("minted", 0),
                        sys_mint_tele.get("skipped_existing", 0),
                        sys_mint_tele.get("skipped_instrument", 0),
                        sys_mint_tele.get("skipped_non_product_home", 0),
                        sys_stamp_tele.get("stamped", 0),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log_sys.info(
                    f"system_uf_recall: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── W4.2 — post-UF vendor-husk fold (operator exhibit: midday Enable
    # Banking I8). After 6.7d + EVERY seed channel the journey layer is
    # settled — a flowless hub-vendor child no journey cites folds under
    # its hub core / nearest enclosing minted capability (the same place
    # D4 sends sub-floor husks at mint time). Runs BEFORE Stage 6.97 so
    # the dual-LOC accounting re-truths itself. Deterministic, $0.
    # Kill-switch: FAULTLINE_HUSK_POST_UF_FOLD=0.
    if anchored_mint_applied and not uf_suppressed:
        from faultline.pipeline_v2.stage_6_86_anchored_mint import (
            fold_unreferenced_vendor_husks,
            husk_post_uf_fold_enabled,
        )
        if husk_post_uf_fold_enabled():
            with StageLogger(run_dir, 6, "husk_post_uf_fold") as log_hf:
                try:
                    hf_tele = fold_unreferenced_vendor_husks(
                        features, product_features, user_flows,
                    )
                    if hf_tele.get("folded") or hf_tele.get("no_target"):
                        scan_meta["husk_post_uf_fold"] = hf_tele
                    log_hf.info(
                        "husk_post_uf_fold: %d folded / %d checked "
                        "(no_target %d)" % (
                            len(hf_tele.get("folded", [])),
                            hf_tele.get("checked", 0),
                            hf_tele.get("no_target", 0),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    log_hf.info(
                        f"husk_post_uf_fold: FAILED ({exc}) — continuing",
                        feature=None,
                    )

    # ── Stage 6.885b — ws-app blob domain-dir member drain (B53 Seg A) ──
    # Re-attribute a ws-blob donor's internal domain-dir members
    # (``<pkg>/<container>/<domain>/**``) onto the EXISTING PF whose identity
    # the domain name echoes (same NamespaceEcho matcher). Runs HERE — v3:
    # AFTER the post-UF vendor-husk fold above, so the drain's target set ==
    # the OFF-emission PF SURVIVOR set by construction (PF-survival
    # invariance: typebot 'popup' was a foldable flowless husk the v2 drain
    # fattened +203 LOC BEFORE the fold, sparing a PF row that does not
    # exist on the OFF board — the drain must only re-attribute members of
    # PFs that would exist anyway, never change which PFs survive). Still
    # BEFORE the journey lattice / Stage 6.97 LOC (owned-LOC reflects the
    # drain) / the path_index rebuild + Stage 6.99 I16 re-home (which
    # carries the drained journeys onto their real PF via path_index → dev
    # → pfid; NO new journey mover, B52 law). NO mints; the package tile
    # survives (B42 territory). Kill-switch
    # FAULTLINE_WS_BLOB_DOMAIN_DRAIN=0 (default) → byte-identical.
    if anchored_mint_applied and not uf_suppressed:
        from faultline.pipeline_v2.stage_6_86_anchored_mint import (
            sameunit_domain_cap_enabled,
        )
        from faultline.pipeline_v2.ws_blob_domain_drain import (
            run_ws_blob_domain_drain,
            ws_blob_domain_drain_enabled,
        )
        # B58-v2: the same pass carries the same-unit domain-dir cap
        # (donor class 2) — either flag alone arms the call; each class
        # runs only under its own switch (independent kill-switches).
        _wbd_on = ws_blob_domain_drain_enabled()
        _cap_on = sameunit_domain_cap_enabled()
        if _wbd_on or _cap_on:
            with StageLogger(run_dir, 6, "ws_blob_drain") as log_wbd:
                try:
                    wbd_tele = run_ws_blob_domain_drain(
                        features, product_features, user_flows,
                        list(bipartite.flows), ctx,
                        # v2: the literal 6.8 index — a file carrying a
                        # NON-donor entry is never touched; ABSENCE is the
                        # unattributed subtree mass that rolls to the blob.
                        path_index=lineage_result.path_index,
                        ws_blob=_wbd_on,
                        sameunit_cap=_cap_on,
                        # B58-v2 nav-only telemetry rung: the SAME
                        # normalized nav keys the mint received (bound
                        # above whenever anchored_mint_applied is True).
                        nav_keys=frozenset(_nav_keys),
                    )
                    if wbd_tele.get("files_moved"):
                        scan_meta["ws_blob_drain"] = wbd_tele
                    log_wbd.info(
                        "ws_blob_drain: donors=%d dirs=%d files=%d loc=%d "
                        "ufs(proj)=%d skip(gen=%d ambig=%d)" % (
                            len(wbd_tele.get("donors", [])),
                            len(wbd_tele.get("matched_dirs", {})),
                            wbd_tele.get("files_moved", 0),
                            wbd_tele.get("loc_moved", 0),
                            wbd_tele.get("ufs_rehomed", 0),
                            wbd_tele.get("skipped_generic", 0),
                            wbd_tele.get("skipped_ambig", 0),
                        ),
                        feature=None,
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    log_wbd.info(
                        f"ws_blob_drain: FAILED ({exc}) — blob left as-is",
                        feature=None,
                    )

    # ── Stage 6.88 — journey lattice (Product-Spine W5) ────────────────
    # Post-abstraction DETERMINISTIC partition of catch-all journeys +
    # exact subset-duplicate merge (the 6.7d prior is "one journey per
    # capability" — its jpf corrective says so verbatim — so 47-member
    # catch-alls ship unrecognizable; A3 panel SEV-1 class). Runs AFTER
    # 6.7d + every seed channel + the husk fold (the journey layer is
    # settled) and BEFORE dual-evidence / keeper / taxonomy / naming so
    # the new journeys ride every downstream polish (scoping, display
    # laws, the I14 backpointer rewrite). Membership only ever
    # PARTITIONS existing journeys — flows keep their spans/LOC (W1
    # law); the keyed personas only NAME (PM Labeler) and REVIEW a
    # split plan (Draft Verifier; reject → the catch-all survives
    # untouched). Deterministic + $0 keyless. Kill-switch:
    # FAULTLINE_JOURNEY_LATTICE=0 (pre-W5 output byte-identical).
    from faultline.pipeline_v2.journey_lattice import (
        dedup_lattice_journeys,
        fold_thin_lattice_children,
        journey_lattice_enabled,
        run_journey_lattice,
    )
    if not uf_suppressed and journey_lattice_enabled():
        write_stage_input(run_dir, 6, "journey_lattice", {
            "user_flows": user_flows,
            "product_features": product_features,
            "scan_meta": scan_meta,
        })
        with StageLogger(run_dir, 6, "journey_lattice") as log_jl:
            try:
                _jl_interior = None
                if interior_result.active:
                    from faultline.pipeline_v2.stage_6_55_page_interior import (
                        build_interior_evidence as _jl_evidence,
                    )
                    try:
                        _jl_interior = _jl_evidence(
                            interior_result, features, product_features,
                        )
                    except Exception:  # noqa: BLE001 — evidence is optional
                        _jl_interior = None
                # Wave-3 personas (keyed scans only; keyless builders
                # return None → deterministic templates, unreviewed
                # splits — the engine is deterministic by construction).
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
                        cost_tracker=tracker,
                        cache=_jl_cache,
                        llm_health=llm_health,
                        log=log_jl,
                    )
                    _jl_labeler = _jl_bpl(
                        model_id=model_id,
                        cost_tracker=tracker,
                        cache=_jl_cache,
                        llm_health=llm_health,
                        log=log_jl,
                        thesis=scan_meta.get("product_thesis"),
                        verifier=_jl_verifier,
                    )
                except Exception:  # noqa: BLE001 — personas are optional
                    _jl_labeler = None
                    _jl_verifier = None
                jl_tele = run_journey_lattice(
                    user_flows, features, product_features,
                    lineage_result.routes_index,
                    interior_evidence=_jl_interior,
                    labeler=_jl_labeler,
                    verifier=_jl_verifier,
                )
                if jl_tele.get("applied"):
                    # §4.5 — children resettle to the capability their
                    # member spans live in (the same ruler as the 6.7d
                    # finalize pass), then the post-resettle same-key
                    # dedup, then the donor backstop (a dissolved
                    # catch-all must never leave a flowful PF with zero
                    # journeys — I8 stays green by construction).
                    from faultline.pipeline_v2.conservation import (
                        apply_uf_conservation as _jl_cons,
                    )
                    jl_tele["conservation_after"] = _jl_cons(
                        user_flows, features, product_features,
                        null_shared_without_signal=True,
                    )
                    jl_tele["dedup_after"] = dedup_lattice_journeys(
                        user_flows)
                    # W5.1 — a child the resettle/dedup stripped to a single
                    # sub-150-LOC member is a shred; fold it back into a
                    # sibling of the same PF (conservation-safe, never the
                    # PF's only journey).
                    jl_tele["thin_fold_after"] = fold_thin_lattice_children(
                        user_flows, list(bipartite.flows))
                    _jl_donor = _recover_uncovered_donors(
                        user_flows, features, product_features,
                    )
                    if _jl_donor is not None:
                        jl_tele["donor_backstop_after"] = _jl_donor
                scan_meta["journey_lattice"] = jl_tele
                log_jl.info(
                    "journey_lattice: pfs %d, subset_merged %d, "
                    "catchalls %d detected / %d split, +%d journeys "
                    "(%d members), dissolved %d, residual-kept %d, "
                    "verifier_rejects %d" % (
                        jl_tele.get("pfs_scanned", 0),
                        jl_tele.get("subset_merged", 0),
                        jl_tele.get("catchalls_detected", 0),
                        jl_tele.get("catchalls_split", 0),
                        jl_tele.get("journeys_created", 0),
                        jl_tele.get("members_moved", 0),
                        jl_tele.get("parents_dissolved", 0),
                        jl_tele.get("parents_kept_residual", 0),
                        jl_tele.get("verifier_rejects", 0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=6,
                    stage_name="journey_lattice",
                    payload=dict(jl_tele),
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — lattice never breaks a scan
                scan_meta.setdefault("warnings", []).append(
                    f"journey-lattice failed ({exc}); journeys unpartitioned"
                )
                log_jl.info(
                    f"journey_lattice: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── S2 Seg A iter-3 — readability regrain (panel-spot blockers) ─────
    # Deterministic, $0, right after the lattice, ONLY under
    # FAULTLINE_UF_DET_AGGREGATION: (B2) lattice ACTION-axis CRUD leaves of
    # one (pf, resource) collapse into 'Manage <resource>' unless a leaf is
    # route-anchored; (B1/B3) any journey over READABILITY_MC_BAR splits by
    # member name-token families, children named from their OWN members (the
    # 'Send cases' mc=33 misname class dies; buried spine families resurface
    # as first-class rows); (B2 display) the raw 'lattice:*' token never
    # ships in the domain field. Unset -> pass never runs -> byte-identical.
    if _det_agg_probe() and user_flows:
        from faultline.pipeline_v2.naming_contract import (
            _verb_class_tokens as _rg_verbs,
            load_naming_vocab as _rg_vocab,
        )
        from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
            readability_regrain,
        )
        with StageLogger(run_dir, 6, "readability_regrain") as log_rg:
            try:
                _rg_tele = readability_regrain(
                    user_flows, list(bipartite.flows), _rg_verbs(_rg_vocab()),
                )
                scan_meta["stage_6_7a_readability_regrain"] = _rg_tele
                log_rg.info(
                    "readability_regrain: crud collapsed %d, %d parents "
                    "split -> %d children (%d members), domains sanitized "
                    "%d, members_lost=%d" % (
                        _rg_tele["crud_collapse"]["collapsed_children"],
                        _rg_tele["oversplit"]["parents_split"],
                        _rg_tele["oversplit"]["children_minted"],
                        _rg_tele["oversplit"]["members_moved"],
                        _rg_tele["domains_sanitized"],
                        _rg_tele["members_lost"],
                    ),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — regrain never breaks a scan
                log_rg.info(
                    f"readability_regrain: FAILED ({exc}) — continuing",
                    feature=None,
                )

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

    # ── Stage 6.98 — E2E-journey truth (deterministic, $0 LLM) ──────
    # Maintainer-authored playwright/cypress journeys as UF evidence:
    # matched journeys CONFIRM UFs (uf_e2e_evidence in the stage
    # artifact); journeys no UF claims are NAMED recall holes
    # (orphan_journeys[]). Runs after the journey layer is fully
    # settled (post 6.7d/seeds/husk-fold — same vantage as
    # dual_evidence, BEFORE the 6.97 LOC prefetch below). Repos without
    # e2e specs report e2e_absent, zero impact. Kill-switch
    # FAULTLINE_E2E_TRUTH=0 ⇒ byte-identical.
    from faultline.pipeline_v2.e2e_truth import (
        e2e_truth_enabled, matched_authored_names, orphan_uf_enabled,
        run_e2e_truth, scan_meta_view, synthesize_orphan_journeys,
    )
    # Track C — maintainer-authored journey display names ({uf_id: [labels]}),
    # consumed by the naming contract's authored channel at Stage 7 (below).
    _e2e_authored_names: dict[str, list[str]] = {}
    e2e_payload: dict[str, Any] | None = None
    if e2e_truth_enabled():
        with StageLogger(run_dir, 6, "e2e_truth") as log_e2e:
            try:
                e2e_payload = run_e2e_truth(
                    repo_path, user_flows,
                    routes_index=lineage_result.routes_index,
                    flows=list(bipartite.flows),
                )
                scan_meta["e2e_truth"] = scan_meta_view(e2e_payload)
                write_stage_artifact(
                    repo_path, 6, "e2e_truth", e2e_payload,
                    run_dir=run_dir,
                )
                log_e2e.info(
                    "e2e_truth: specs=%d journeys=%d matched=%d "
                    "orphans=%d absent=%s" % (
                        e2e_payload["spec_files"],
                        e2e_payload["journeys"],
                        e2e_payload["counts"]["matched"],
                        e2e_payload["counts"]["orphans"],
                        e2e_payload["e2e_absent"],
                    ),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log_e2e.info(
                    f"e2e_truth: FAILED ({exc}) — continuing", feature=None,
                )

    # ── Stage 6.98b — orphan-journey → UF synthesis (Track C, recall) ──
    # Each groundable orphan journey (maintainer-named recall hole) becomes
    # a tagged, PF-bound member-less UserFlow so the board/panel surfaces it.
    # Deterministic, $0 LLM, additive. Runs immediately after e2e_truth (its
    # payload feeds this). Also captures authored display names for MATCHED
    # UFs (route-evidence only) for the naming contract. Kill-switch
    # FAULTLINE_E2E_ORPHAN_UF=0 ⇒ output byte-identical to e2e-truth-only.
    if (e2e_truth_enabled() and orphan_uf_enabled()
            and e2e_payload is not None and not e2e_payload.get("e2e_absent")):
        with StageLogger(run_dir, 6, "e2e_orphan_uf") as log_orph:
            try:
                _synth = synthesize_orphan_journeys(
                    e2e_payload, product_features, features,
                    lineage_result.routes_index, user_flows,
                    flows=list(bipartite.flows),
                )
                minted = _synth["minted"]
                if minted:
                    max_id = 0
                    for uf in user_flows:
                        _m = re.match(r"^UF-(\d+)$", str(uf.id or ""))
                        if _m:
                            max_id = max(max_id, int(_m.group(1)))
                    fresh = [uf for uf, _titles in minted]
                    fresh.sort(key=lambda u: ((u.name or "").lower(),
                                              str(u.resource or "")))
                    for i, uf in enumerate(fresh, start=1):
                        uf.id = f"UF-{max_id + i:03d}"
                    for uf, _titles in minted:
                        user_flows.append(uf)
                        _e2e_authored_names[uf.id] = [uf.name]
                # C2 — authored names for MATCHED UFs (route-evidence only).
                for uid, labels in matched_authored_names(e2e_payload).items():
                    _e2e_authored_names.setdefault(uid, list(labels))
                scan_meta["e2e_orphan_uf"] = _synth["tele"]
                log_orph.info(
                    "e2e_orphan_uf: minted=%d (groups=%d, "
                    "filtered_neg=%d, dropped_no_route=%d, dropped_unbound=%d), "
                    "matched_authored=%d" % (
                        _synth["tele"]["minted"],
                        _synth["tele"]["groups"],
                        _synth["tele"]["filtered_negative"],
                        _synth["tele"]["dropped_no_route_ev"],
                        _synth["tele"]["dropped_unbound_pf"],
                        len(_e2e_authored_names),
                    ),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                log_orph.info(
                    f"e2e_orphan_uf: FAILED ({exc}) — continuing", feature=None,
                )

    # ── Stage 6.985 — transport-lane journey-conservation handoff (B22) ──
    # The transport prong (B19, default OFF) no longer lanes at mint —
    # 6.86 emitted candidate MARKS instead — and THIS stage resolves them
    # after the LAST journey producer (6.98b above) and BEFORE the 6.97
    # LOC prefetch (the lane_rehome slot family: loc-truth I13 + the lane
    # accounting below hold with zero extra plumbing; the emission
    # path_index refresh picks the moved devs up automatically). Per
    # candidate PF: every homed journey re-homes to the product PF it
    # serves (strict-majority span vote → deterministic consumer
    # completion → flagged plurality), route-group targets are excavated
    # at the SAME grain the vote used, devs follow, and only then the PF
    # converts to a platform-infrastructure lane resident. CONSERVATION
    # GATE (operator law): ANY unresolved journey → the PF does NOT lane
    # (exact flag-OFF output + blocked telemetry) — no journey is EVER
    # dissolved. Deterministic, $0 LLM; inert (no scan_meta key, no
    # output change) unless candidates exist. Kill-switch
    # FAULTLINE_TRANSPORT_LANE_HANDOFF=0 restores B19 mint-time laning.
    from faultline.pipeline_v2.transport_handoff import (
        run_transport_handoff,
        transport_handoff_enabled,
    )
    _transport_candidates = dict(
        ((scan_meta.get("stage_6_86_anchored_mint") or {})
         .get("technology_instruments") or {})
        .get("transport_candidates") or {}
    )
    if transport_handoff_enabled() and _transport_candidates:
        with StageLogger(run_dir, 6, "transport_handoff") as log_th:
            try:
                th_tele = run_transport_handoff(
                    features, product_features, user_flows,
                    list(bipartite.flows), lineage_result.routes_index,
                    ctx, _transport_candidates,
                    extractor_signals=stage1_out,
                    feature_flow_edges=list(bipartite.edges),
                    nav_keys=frozenset(_nav_keys),
                )
                scan_meta["transport_handoff"] = th_tele
                log_th.info(
                    "transport_handoff: candidates=%d laned=%d blocked=%d "
                    "ufs_rehomed=%d devs(rehomed=%d laned=%d) minted=%d"
                    % (
                        len(th_tele.get("candidates") or []),
                        len(th_tele.get("laned") or []),
                        len(th_tele.get("conservation_blocked") or {}),
                        th_tele.get("ufs_rehomed", 0),
                        th_tele.get("devs_rehomed", 0),
                        th_tele.get("devs_laned", 0),
                        th_tele.get("pfs_minted", 0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=6,
                    stage_name="transport_handoff",
                    payload=dict(th_tele),
                    run_dir=run_dir,
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

    # ── Stage 6.986 — mega-PF nav-area journey re-home + mint (B24) ──
    # A board-dominating umbrella PF (>=25% of homed journeys) whose
    # journeys strict-majority-cluster into >=3 distinct nav-area route
    # groups re-homes those journeys onto their EXISTING sibling PFs
    # (attach-floor + all-rung I16 rail + surface/same-app rails) and
    # mints a group with no sibling ONLY above the lattice floor
    # (>=3 UFs / >=3 flows) — supabase 'projects' class. Runs AFTER
    # 6.985 (journey layer final, transport folds resolved; candidates
    # excluded as sources/targets) and BEFORE the 6.97 LOC prefetch so
    # mint + carve are loc-stamped/path_indexed like any other PF.
    # Conservation: re-home ONLY (unresolved journeys stay; source
    # keeps >=1). Deterministic, $0 LLM; default OFF; inert (no
    # scan_meta key) unless the trigger fires.
    from faultline.pipeline_v2.mega_pf_nav_rehome import (
        mega_pf_nav_rehome_enabled,
        run_mega_pf_nav_rehome,
    )
    if (mega_pf_nav_rehome_enabled() and anchored_mint_applied
            and not uf_suppressed):
        with StageLogger(run_dir, 6, "mega_pf_nav_rehome") as log_b24:
            try:
                b24_tele = run_mega_pf_nav_rehome(
                    features, product_features, user_flows,
                    list(bipartite.flows), lineage_result.routes_index,
                    ctx,
                    extractor_signals=stage1_out,
                    feature_flow_edges=list(bipartite.edges),
                    transport_candidate_units=set(_transport_candidates),
                )
                # S5a: ``armed_sources`` exists only in the ARMED world —
                # emitting the selection census there keeps the unarmed
                # inertness convention byte-identical while making the
                # armed trigger numbers observable even on no-fire boards.
                if b24_tele.get("triggered") or b24_tele.get("armed_sources"):
                    scan_meta["mega_pf_nav_rehome"] = b24_tele
                    write_stage_artifact(
                        ctx.repo_path,
                        stage_index=6,
                        stage_name="mega_pf_nav_rehome",
                        payload=dict(b24_tele),
                        run_dir=run_dir,
                    )
                log_b24.info(
                    "mega_pf_nav_rehome: triggered=%s ufs_rehomed=%d "
                    "minted=%d carved=%d floor_drops=%d qual=%d census=%s"
                    % (
                        ",".join(b24_tele.get("triggered") or []) or "-",
                        b24_tele.get("ufs_rehomed", 0),
                        b24_tele.get("pfs_minted", 0),
                        b24_tele.get("devs_carved", 0),
                        len(b24_tele.get("floor_drops") or []),
                        len(b24_tele.get("qualifying_groups") or []),
                        ";".join(
                            f"{c['pf']}:{c['ufs']}:{c['share']}"
                            for c in (b24_tele.get("census") or [])[:3]),
                    ),
                    feature=None,
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

    # ── Stage 6.987 — devgrain-leaf PF demote (B33 v2) ─────────────────
    # A route:/fdir:-anchored PF whose leaf names a plumbing screen /
    # journey step (closed YAML set), is NOT nav-declared, and whose
    # FINAL journey profile is micro (<=2 UFs, every member_count <=3)
    # demotes: PF row removed, its synthesized micro-UFs drop, devs
    # re-point to the nearest surviving ancestor PF (else stay L1).
    # Runs AFTER the journey layer is final (6.7*/lattice/e2e/6.985/
    # 6.986) so a rich journey set always vetoes the demote
    # (conservation by construction — the twenty Onboarding lesson),
    # and BEFORE the 6.97 LOC prefetch / marker backstops / emission
    # integrity so no marker is synthesized for a demoted PF. Empty
    # nav_keys ⇒ board-wide honest abstain. Deterministic, $0 LLM;
    # default ON since the 2026-07-12 flip (KEY_SCHEMA v28).
    from faultline.pipeline_v2.devgrain_demote import (
        fdir_devgrain_gate_enabled,
        run_devgrain_demote,
    )
    if (fdir_devgrain_gate_enabled() and anchored_mint_applied
            and not uf_suppressed):
        with StageLogger(run_dir, 6, "devgrain_demote") as log_dg:
            try:
                dg_tele = run_devgrain_demote(
                    features, product_features, user_flows,
                    nav_keys=frozenset(_nav_keys),
                )
                scan_meta["devgrain_demote"] = dg_tele
                log_dg.info(
                    "devgrain_demote: eligible=%d demoted=%d abstained=%d "
                    "nav_skipped=%d board_abstain=%s" % (
                        dg_tele.get("eligible", 0),
                        len(dg_tele.get("demoted", [])),
                        len(dg_tele.get("abstained", [])),
                        len(dg_tele.get("nav_declared_skipped", [])),
                        dg_tele.get("journey_step_leaf_abstained", False),
                    ),
                    feature=None,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"devgrain demote failed ({exc}); PFs left untouched"
                )
                log_dg.info(
                    f"devgrain_demote: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # S3 arbiter — apply deferred proposals at this pass boundary
    # (rung-priority == pass order; no-op when the flag is OFF).
    _arb_flush(product_features, note="post-devgrain")

    # ── Perf wave 2 (R5b) — 6.97 LOC prefetch overlaps the 6.95→6.96 chain ──
    # DAG verified at this base: Stage 6.97 feature-LOC reads only
    # {feature/PF paths + member_files + the checked-out tree,
    # uf.product_feature_id, uf.member_flow_ids, flow line_ranges} —
    # none of which 6.95 history / 6.96 impact (entity ``history`` only),
    # the llm-health stamp (scan_meta), 6.6 monorepo assembly (never
    # mutates features) or the uf-identity keeper (uf.id / flow
    # backpointers only) write. A FULL stage relocation is still unsafe:
    # the write_stage_input captures inside this window serialize
    # features / product_features / scan_meta while a sibling thread
    # would be inserting ``history``/``loc`` attributes (dict-changed-
    # during-iteration + nondeterministic capture bytes), and scan_meta
    # key INSERTION ORDER feeds the output JSON. So the concurrency is
    # scoped to 6.97's dominant cost — the pure per-file LOC counting —
    # prefetched here in one worker thread (pure reads: feature paths +
    # disk; no shared-state mutation, no StageLogger events, no scan_meta
    # keys) and consumed at the stage's UNCHANGED canonical position
    # below. Values are pure functions of the tree, so output bytes are
    # identical by construction; on any prefetch failure the stage
    # computes inline exactly as before.
    import threading as _threading

    from faultline.pipeline_v2.stage_6_97_feature_loc import (
        prefetch_loc_cache as _prefetch_loc_cache,
        stage_6_97_enabled as _stage_6_97_enabled_early,
    )

    _loc_prefetch: dict[str, Any] = {}
    _loc_prefetch_thread: Any = None
    if _stage_6_97_enabled_early():
        def _run_loc_prefetch() -> None:
            try:
                _loc_prefetch["cache"] = _prefetch_loc_cache(
                    features, product_features, ctx.repo_path,
                )
            except Exception:  # noqa: BLE001 — prefetch is best-effort
                _loc_prefetch.pop("cache", None)

        _loc_prefetch_thread = _threading.Thread(
            target=_run_loc_prefetch,
            name="faultline-6-97-loc-prefetch",
            daemon=True,
        )
        _loc_prefetch_thread.start()

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
    # W3 (§4.8): keeper default-ON on the production path (worker passes
    # the prev scan) — ``FAULTLINE_KEEPER=0`` disables pinning even with
    # input present (eval scrub-mode; rule-cold-scan stays provable).
    from faultline.pipeline_v2.uf_identity_keeper import (
        keeper_enabled as _keeper_enabled,
    )
    if prev_scan_json is not None and not _keeper_enabled():
        scan_meta["uf_identity"] = {
            "enabled": False, "reason": "FAULTLINE_KEEPER=0",
        }
    if prev_scan_json is not None and _keeper_enabled():
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

    # ── FILELANE — file-level shared-infrastructure lane ──────────────────
    # Reclassifies unowned high-fan-in shared-infra files into the lane
    # (the dominant I15 SPILL->unowned blocker). Runs HERE — after the
    # membership + user-flow layer settles, BEFORE Stage 6.97 — so the
    # normal feature_loc pass counts the new lane devs' LOC and folds it
    # into repo_loc (conservation holds BY CONSTRUCTION, the same window
    # lane_rehome / provenance_rehome use). The post-emission path_index
    # rebuild then reads the files as lane-owned and
    # build_platform_infrastructure_lane emits them. Strictly additive —
    # only NEW pfid=None lane devs are appended; existing PFs/devs are
    # untouched, and resolve_flowless_shells (pfid-keyed) +
    # emission_integrity (phantom-drop) leave flowless pfid=None lane
    # residents intact (the existing no_anchor_lineage lane proves it).
    # Gated on anchored_mint_applied: no lane schema to extend otherwise.
    # Kill-switch FAULTLINE_FILE_LANE=0 → no devs appended → byte-identical.
    from faultline.pipeline_v2.file_lane import (
        data_leaf_enabled,
        enforce_data_leaf_shared,
        enforce_shared_leaf_consistency,
        file_lane_enabled,
        run_file_lane_infra,
        shared_leaf_consistency_enabled,
    )
    if file_lane_enabled() and anchored_mint_applied:
        with StageLogger(run_dir, 6, "file_lane") as log_fl:
            try:
                fl_tele = run_file_lane_infra(
                    features, product_features,
                    lineage_result.routes_index, ctx,
                )
                scan_meta["file_lane"] = fl_tele
                log_fl.info(
                    "file_lane: laned %d files -> %d lane devs (%d LOC), "
                    "threshold %d = ceil(%.3f * %d PFs); blocked "
                    "owned=%d pf_paths=%d surface=%d low_fanin=%d"
                    % (
                        fl_tele.get("laned_files", 0),
                        fl_tele.get("laned_devs", 0),
                        fl_tele.get("laned_loc", 0),
                        fl_tele.get("threshold", 0),
                        fl_tele.get("pct", 0.0),
                        fl_tele.get("num_product_features", 0),
                        fl_tele.get("blocked_owned", 0),
                        fl_tele.get("blocked_pf_paths", 0),
                        fl_tele.get("blocked_surface", 0),
                        fl_tele.get("blocked_low_fanin", 0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path, stage_index=6, stage_name="file_lane",
                    payload=dict(fl_tele), run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — lane must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"file-lane failed ({exc}); no infra reclassified"
                )
                log_fl.info(
                    f"file_lane: FAILED ({exc}) — continuing", feature=None)

    # ── B15 — shared-leaf role consistency (post file_lane, pre path_index) ──
    #    Force high-cross-PF-fan-in, no-surface, already-shared-somewhere member
    #    files to role="shared" everywhere (the i18n-locale class re-attributed
    #    as closure body). Runs here so the change lands BEFORE the path_index
    #    rebuild + I23 body read. Kill-switch FAULTLINE_SHARED_LEAF_CONSISTENCY=0
    #    -> no roles changed -> byte-identical. Never breaks a scan.
    if shared_leaf_consistency_enabled() and anchored_mint_applied:
        try:
            slc_tele = enforce_shared_leaf_consistency(
                features, product_features, lineage_result.routes_index)
            scan_meta["shared_leaf_consistency"] = slc_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"shared-leaf-consistency failed ({exc}); no roles changed")

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
        # ``user_flows``/``bipartite_flows`` joined this capture in perf
        # wave 2: the live call threads them into the §4.5 flow
        # accounting, but the capture (and the registry runner) predated
        # that — single-stage replay silently dropped the
        # ``loc_accounting`` flow keys (found by this wave's replay
        # proof; reproduced from a b7351b6-era capture, i.e. pre-existing
        # drift, not introduced here).
        write_stage_input(run_dir, 6, "feature_loc", {
            "ctx": ctx,
            "features": features,
            "product_features": product_features,
            "scan_meta": scan_meta,
            "user_flows": user_flows,
            "bipartite_flows": list(bipartite.flows),
        })
        with StageLogger(run_dir, 6, "feature_loc") as log697:
            try:
                # R5b — adopt the prefetched per-file counts (value
                # source only; key discipline in _PrewarmedCache). The
                # join is here, right before the single consumer.
                _prewarmed_loc = None
                if _loc_prefetch_thread is not None:
                    _loc_prefetch_thread.join()
                    _prewarmed_loc = _loc_prefetch.get("cache")
                loc_telemetry = apply_feature_loc(
                    features, product_features, ctx.repo_path,
                    # §4.5 conservation flow accounting (on-flow ≤ 1 by
                    # construction) — needs the final journey attachments.
                    user_flows=user_flows,
                    flows=list(bipartite.flows),
                    prewarmed_loc=_prewarmed_loc,
                )
                scan_meta["feature_loc"] = loc_telemetry
                if loc_telemetry.get("loc_accounting"):
                    scan_meta["loc_accounting"] = loc_telemetry["loc_accounting"]
                # B59 — artifact-ink lane aggregate (absent when the flag is
                # OFF, so default scan_meta is byte-identical to main).
                if loc_telemetry.get("artifact_ink"):
                    scan_meta["artifact_ink"] = loc_telemetry["artifact_ink"]
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

    # ── B15b — data-file shared-leaf rail (§4b, runs AFTER 6.97 so repo_loc is
    #    available for the scale-invariant LOC floor; before the path_index
    #    rebuild + I23 body read). Force role="shared" on large shared-DATA leaf
    #    files (i18n locale packs, template JSON) the closure attributed as body.
    #    Kill-switch FAULTLINE_DATA_LEAF=0 -> byte-identical. Never breaks a scan.
    if data_leaf_enabled() and anchored_mint_applied:
        try:
            _repo_loc = (scan_meta.get("loc_accounting") or {}).get("repo_loc")
            dl_tele = enforce_data_leaf_shared(
                features, product_features, lineage_result.routes_index,
                _repo_loc)
            scan_meta["data_leaf"] = dl_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"data-leaf rail failed ({exc}); no roles changed")

    # ── Draw-native flowless-shell resolution (RC2 fix-3 Part B, $0) ──
    # A 6.7d Call-1 draw can emit a capability whose devs own >= 1k LOC but
    # ZERO flows (validator I8 LOC prong). Resettle those shells — absorb a
    # footprint-matched residual dev, JOIN a token-family flowful PF, or DEMOTE
    # to the shared bucket + DROP the shell. Runs HERE (post-feature_loc) because
    # the >= 1k-LOC prong needs owned ``loc`` (Stage 6.97 populates it), and ONLY
    # when 6.7d actually applied — so keyless scans (no draw, no shells) stay
    # byte-inert (snapshot gate). Before emission-integrity so the dropped PF
    # never reaches the referential round-trip.
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _shell_absorb_enabled,
        resolve_flowless_shells,
    )
    # NEVER on the ANCHORED path (Wave 2b review F1/F5, 2026-07-06): this
    # ladder is a cure for FREE-GEN draw shells and both of its moving
    # rungs violate the spine — the JOIN re-homes a minted anchor's devs
    # by name-family against their own lineage (F5), and the DEMOTE
    # resurrects the ABOLISHED "Shared Platform" PF via _ensure_shared_pf
    # (F1 — the 18:2x amendment kills that bucket on every code path; the
    # keyed leak survived the keyless gates precisely because this stage
    # is 6.7d-gated). An anchored flowless >=1k-LOC PF is a REAL
    # capability whose flows weren't detected — it stays, and validator
    # I8's LOC prong honestly reports the flow-detection gap.
    if (_shell_absorb_enabled()
            and not anchored_mint_applied
            and (scan_meta.get("stage_6_7d_journey_abstraction") or {}).get("applied")):
        with StageLogger(run_dir, 6, "flowless_shells") as log_shell:
            try:
                product_features, shell_tele = resolve_flowless_shells(
                    features, product_features)
                scan_meta["flowless_shells"] = shell_tele
                log_shell.info(
                    "flowless_shells: absorbed=%d joined=%d demoted=%d"
                    % (shell_tele["shell_absorbed"], shell_tele["shell_joined"],
                       shell_tele["shell_demoted"]),
                    feature=None)
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flowless-shell resolution failed ({exc}); shells left as-is")
                log_shell.info(
                    f"flowless_shells: FAILED ({exc}) — continuing", feature=None)

    # ── Stage 6.85 — product-surface taxonomy, emission lane ($0) ──────
    # Product-Spine §4.2 (Wave 2a) consequences on the FINAL arrays:
    # tag UFs + PFs; move marketing/docs/legal/dev_tooling/shell PFs (and
    # their journeys) into the additive non_product_surfaces[] lane
    # (validator I20); dissolve info-page journeys into their hosting UF
    # (consequence b); re-bind non-product shared devs to their lane
    # surface; stamp shared_reason on every shared-bucket resident
    # (validator I22). Runs AFTER 6.97 LOC (lane rows carry loc) and the
    # flowless-shell resolution (final PF set), BEFORE emission integrity
    # (which then reconciles refs against the surviving product list).
    from faultline.pipeline_v2.surface_taxonomy import apply_emission_taxonomy
    non_product_surfaces: list[dict[str, Any]] = []
    with StageLogger(run_dir, 6, "surface_taxonomy") as log_st:
        try:
            # Surface Adjudicator (W3 §4.7): keyed scans resolve the
            # conflicting-signal PF minority; keyless / kill-switch →
            # None → the deterministic verdicts stand byte-identically.
            _st_adjudicator = None
            try:
                from faultline.pipeline_v2.personas import (
                    build_surface_adjudicator,
                )
                _st_adjudicator = build_surface_adjudicator(
                    model_id=model_id,
                    cost_tracker=tracker,
                    cache=getattr(ctx, "cache_backend", None),
                    llm_health=llm_health,
                    log=log_st,
                    thesis=scan_meta.get("product_thesis"),
                )
            except Exception:  # noqa: BLE001 — persona is optional
                _st_adjudicator = None
            st_tele, non_product_surfaces, product_features = (
                apply_emission_taxonomy(
                    features, product_features, user_flows,
                    list(bipartite.flows), lineage_result.routes_index,
                    repo_path=repo_path,
                    adjudicator=_st_adjudicator,
                    instrument_dirs=instrument_dirs,
                    dev_artifact_units=dev_artifact_units,  # B28 P-D
                )
            )
            scan_meta["surface_taxonomy_emission"] = st_tele
            log_st.info(
                "surface_taxonomy: pf_scopes=%s lane=%d ufs_moved=%d "
                "info_dissolved=%d rebound=%d shared_reasons=%s"
                % (
                    st_tele.get("pf_scopes"),
                    st_tele.get("pfs_moved_to_lane", 0),
                    st_tele.get("ufs_moved_to_lane", 0),
                    st_tele.get("info_ufs_dissolved", 0),
                    st_tele.get("devs_rebound_to_lane", 0),
                    st_tele.get("shared_reasons"),
                ),
                feature=None,
            )
            write_stage_artifact(
                ctx.repo_path,
                stage_index=6,
                stage_name="surface_taxonomy",
                payload={**st_tele, "non_product_surfaces": [
                    {k: v for k, v in e.items() if k != "user_flows"}
                    for e in non_product_surfaces
                ]},
                run_dir=run_dir,
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

    # ── Stage 6.987 — S5b leaf-route dissolution + platform promotion ──
    # ONE arbiter wave (spec fixs5b §ПРОБИ-ФОРМИ): Seg B dissolves a
    # ``route:``-leaf black hole (flat-leaf anchor whose mass >> its own
    # subtree — novu ``duplicate-workflow`` 315/2) by re-homing its foreign
    # member devs to real capability siblings (S5a dev-identity bridge +
    # file-grain majority segment); unhomed devs lane (NOT forced), which
    # FREES their pages. Seg C then promotes buried product surfaces —
    # platform-lane residents carrying PAGE evidence (a-lite mirror; P1
    # page-cohort ∪ P2 lane-token↔freed-page bridge) — by birth (S5a
    # birth-law) or merge-into-sibling. Runs AFTER the emission taxonomy
    # established the lane (pfid=None + shared_reason residents) + 6.97 LOC,
    # BEFORE emission integrity (which reconciles the births' UF refs I12 +
    # drops any phantom) and BEFORE the platform lane is assembled (so
    # promoted residents leave the lane). Every dev/UF move is an
    # overturn-ledger proposal (rung ``leafroute``). Kill-switch
    # FAULTLINE_LEAFROUTE_PROMOTION unset/0 → stage not entered →
    # byte-identical.
    from faultline.pipeline_v2.leafroute_promotion import (
        leafroute_promotion_enabled,
        run_leafroute_promotion,
    )
    if leafroute_promotion_enabled():
        with StageLogger(run_dir, 6, "leafroute_promotion") as log_lrp:
            try:
                lrp_tele = run_leafroute_promotion(
                    features, product_features, user_flows,
                    list(bipartite.flows), lineage_result.routes_index, ctx,
                    feature_flow_edges=list(bipartite.edges),
                )
                if (lrp_tele.get("leaf_dissolved") or lrp_tele.get("births")
                        or lrp_tele.get("merged")):
                    scan_meta["leafroute_promotion"] = lrp_tele
                log_lrp.info(
                    "leafroute_promotion: leaf_fired=%d devs_rehomed=%d "
                    "devs_freed=%d pfs_born=%d devs_merged=%d p2_pages=%d"
                    % (
                        len(lrp_tele.get("leaf_fired", [])),
                        lrp_tele.get("devs_rehomed", 0),
                        lrp_tele.get("devs_freed", 0),
                        lrp_tele.get("pfs_born", 0),
                        lrp_tele.get("devs_merged", 0),
                        lrp_tele.get("p2_pages_bridged", 0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=6,
                    stage_name="leafroute_promotion",
                    payload=lrp_tele,
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"leafroute-promotion failed ({exc}); board left as-is")
                log_lrp.info(
                    f"leafroute_promotion: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── W5.1 — LOC-worthy PF backstop (final I8 close, $0) ─────────────
    # Excavation (Stage 6.87) mints FLOWLESS high-LOC surfaces (supabase
    # Settings 27.5K, Query Performance, …); the flow-based PF-UF backstop
    # cannot seed them (no member flows). Validator I8 still demands a
    # journey (``pf_loc >= 1000``). This runs HERE — AFTER Stage 6.97 stamps
    # ``loc`` (the LOC arm no-ops before that) and AFTER the emission
    # taxonomy finalised the product list (so it targets the EXACT I8 PF
    # set) — and appends a member-LESS system-seed (the sole I7-exempt cover
    # for a flowless surface). BEFORE emission integrity, which then
    # reconciles the new UF refs (I12) + backpointers (I14). Gated on the
    # journey layer being EXPECTED (``not uf_suppressed``) — NOT on 6.7d
    # abstraction ``applied``: validator I8 scores the raw rollup too (6.7d
    # is LLM-only, degrades keyless), so the close must run in both the
    # abstracted and the degraded/keyless journey layers. Suppressed repos
    # (libraries / CLIs) never get journeys, so they are skipped.
    if not uf_suppressed:
        _lw_tele = _recover_uncovered_donors(
            user_flows, features, product_features, loc_only=True,
        )
        if _lw_tele is not None:
            scan_meta["loc_worthy_backstop"] = _lw_tele

    # platform_infrastructure[] declared here; ASSEMBLED after emission
    # integrity (the I2 phantom drop must not leave stale lane rows).
    platform_infrastructure: list[dict[str, Any]] | None = None

    # ── Emission integrity — referential round-trip guarantee ($0) ──
    # Runs LAST, after every UF / PF / flow / loc mutation, so the emitted
    # JSON is self-consistent by construction:
    #   * I2  — drop phantom features (marker-only paths, 0 loc/loc_shared,
    #           0 flows); workspace anchors + shared-platform are exempt.
    #   * I12 — every user_flows[].product_feature_id ∈ emitted PF key-set
    #           (canonical re-match else null).
    #   * I14 — flows[].user_flow_id re-derived from final user_flows[]
    #           membership (first owning UF in emit order wins).
    # Deterministic, output-layer only; never touches the flow graph.
    from faultline.pipeline_v2.emission_integrity import (
        enforce_emission_integrity,
    )
    with StageLogger(run_dir, 7, "emission_integrity") as log_ei:
        features, product_features, ei_result = enforce_emission_integrity(
            features, product_features, user_flows, bipartite.flows,
        )
        scan_meta["emission_integrity"] = ei_result.as_dict()
        log_ei.info(
            "emission_integrity: dropped %d dev + %d pf phantoms, "
            "relinked %d / nulled %d uf→pf refs, "
            "rewrote %d / nulled %d flow backpointers"
            % (
                len(ei_result.phantom_features_dropped),
                len(ei_result.phantom_product_features_dropped),
                ei_result.uf_pf_refs_relinked,
                ei_result.uf_pf_refs_nulled,
                ei_result.flow_backpointers_rewritten,
                ei_result.flow_backpointers_nulled,
            ),
            feature=None,
        )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=7,
            stage_name="emission_integrity",
            payload=ei_result.as_dict(),
            run_dir=run_dir,
        )

        # W1.1 — path_index emission refresh. Lineage builds the index at
        # the TOP of this phase, but 6.9 test-strip / 6.9b generated-strip
        # / the 8.9.7 carve legitimately remove paths from features AFTER
        # that, so the shipped index pointed files at features that no
        # longer list them (Soc0 validation scan 2026-07-06: 551/2081
        # entries stale, incl. the I18 offender — a stripped __tests__
        # file still "owned" by the shared-resident workspace anchor).
        # Rebuild from the FINAL features + flows: same builder, same
        # first-claimant rule, so live ownership is untouched; entries
        # whose only claimant vanished keep their flow join (flow_uuids)
        # with feature_uuid="" or drop out entirely — path_index is part
        # of the emitted output tree and must obey the same integrity
        # contract as everything else in this block.
        from faultline.pipeline_v2.indexes import build_path_index

        # ── S1 owner-oracle (flag-gated, default OFF) ───────────────────
        # Compute the deterministic owner election ONCE here — post-devgrain,
        # post-emission-integrity, so it runs over the SETTLED final dev
        # membership — and feed it to the path_index refresh (R1) and the
        # terminal-home conservation votes (R2) below. i16 / dispatch read the
        # refreshed path_index, so they inherit the same owner transitively.
        # OFF (unset / ``0``) → ``_owner_election`` is None → every consumer
        # keeps first-claimant → byte-identical.
        from faultline.pipeline_v2.owner_oracle import (
            build_owner_election_from,
            owner_oracle_enabled,
        )
        _owner_election = None
        _owner_file_uuid: dict[str, str] | None = None
        if owner_oracle_enabled():
            try:
                _owner_election = build_owner_election_from(
                    features, ctx.repo_path,
                )
                _owner_file_uuid = _owner_election.file_owner_uuid_map()
                scan_meta["emission_integrity"]["owner_oracle"] = {
                    "enabled": True,
                    "files_elected": len(_owner_file_uuid),
                }
            except Exception as exc:  # noqa: BLE001 — never break a scan
                _owner_election = None
                _owner_file_uuid = None
                scan_meta.setdefault("warnings", []).append(
                    f"owner-oracle election failed ({exc}); "
                    f"first-claimant owners kept"
                )

        _stale_index = lineage_result.path_index
        lineage_result.path_index = build_path_index(
            [{"uuid": f.uuid, "paths": list(f.paths)} for f in features],
            [{"uuid": fl.uuid, "paths": list(fl.paths)}
             for fl in bipartite.flows],
            file_owner=_owner_file_uuid,
        )
        _refresh_stats = {
            "entries_before": len(_stale_index),
            "entries_after": len(lineage_result.path_index),
            "owners_changed": sum(
                1 for p, e in lineage_result.path_index.items()
                if (_stale_index.get(p) or {}).get("feature_uuid")
                != e.get("feature_uuid")
            ),
        }
        scan_meta["emission_integrity"]["path_index_refresh"] = _refresh_stats
        log_ei.info(
            "path_index refresh: %d -> %d entries, %d owners changed"
            % (
                _refresh_stats["entries_before"],
                _refresh_stats["entries_after"],
                _refresh_stats["owners_changed"],
            ),
            feature=None,
        )
        # ── No-signal UF terminal home (Wave 2a, validator I21) ────────
        # Runs AFTER the integrity passes so it binds only SURVIVING
        # product-list keys and nothing downstream can re-null its work:
        # every journey the conservation nulling / I12 repair left orphan
        # gets a deterministic real-PF home (system-scope preference →
        # ownership argmax → nearest-directory argmax), tagged
        # binding_confidence="low". A UF never ships null / shared.
        from faultline.pipeline_v2.uf_terminal_home import (
            assign_terminal_homes,
            terminal_home_enabled,
        )
        if terminal_home_enabled():
            th_tele = assign_terminal_homes(
                user_flows, features, product_features,
                owner_election=_owner_election,
            )
            scan_meta["uf_terminal_home"] = th_tele
            log_ei.info(
                "uf_terminal_home: orphans=%d homed(votes=%d system=%d "
                "dir=%d) unhomed=%d"
                % (
                    th_tele.get("orphans", 0),
                    th_tele.get("homed_votes", 0),
                    th_tele.get("homed_system", 0),
                    th_tele.get("homed_dir", 0),
                    th_tele.get("unhomed", 0),
                ),
                feature=None,
            )

    # S3 arbiter — apply deferred proposals at this pass boundary
    # (rung-priority == pass order; no-op when the flag is OFF).
    _arb_flush(product_features, note="post-terminal-home")

    # ── Stage 6.87 — display-name contract (Product-Spine §4.8, W3) ────
    # SELECTION-not-generation display polish on the FINAL product list:
    # laws (single-letter / param / file-stem / PF==UF twin / acronym
    # casing / display collisions), keeper pin channel (content-derived
    # prev-scan join by anchor_id/slug, FAULTLINE_KEEPER-gated), ranked
    # deterministic candidates, and the PM-Labeler persona seam (keyed
    # scans). Runs AFTER emission integrity + terminal home (the PF set
    # is final — no naming spend on phantoms) and BEFORE the platform
    # lane + Stage-7 write. Writes ONLY the display channel
    # (Feature.display_name / UserFlow.name) — identity is untouched.
    # Kill-switch: FAULTLINE_NAMING_CONTRACT=0 (pre-W3 output
    # byte-identical).
    from faultline.pipeline_v2.naming_contract import (
        naming_contract_enabled,
        run_naming_contract,
    )
    from faultline.pipeline_v2.uf_identity_keeper import keeper_enabled
    if naming_contract_enabled():
        write_stage_input(run_dir, 7, "naming_contract", {
            "product_features": product_features,
            "user_flows": user_flows,
            "bipartite_flows": list(bipartite.flows),
            "prev_scan_json": prev_scan_json,
            "scan_meta": scan_meta,
        })
        with StageLogger(run_dir, 7, "naming_contract") as log_nc:
            try:
                # PM Labeler + Draft Verifier (Wave-3 personas): keyed
                # scans only — the deterministic top choice is the
                # keyless display path. The thesis feeds NAMES only
                # (the reviewed §4.7 consumer seam of the write-only
                # product_thesis scan_meta key — never membership).
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
                        cost_tracker=tracker,
                        cache=_nc_cache,
                        llm_health=llm_health,
                        log=log_nc,
                    )
                    _nc_labeler = build_pm_labeler(
                        model_id=model_id,
                        cost_tracker=tracker,
                        cache=_nc_cache,
                        llm_health=llm_health,
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
                    list(bipartite.flows),
                    prev_scan=prev_scan_json,
                    keeper_on=keeper_enabled(),
                    product_strings=product_strings,
                    routes_index=lineage_result.routes_index,
                    uf_authored_names=_e2e_authored_names,
                    labeler=_nc_labeler,
                    verifier=_nc_verifier,
                    # B27 — arms the package-manifest display channel: a
                    # package-dir-anchored PF takes the display name the
                    # package DECLARES in its own metadata (config.json /
                    # metadata module / package.json), word-split fallback
                    # below. FAULTLINE_PF_MANIFEST_NAME=0 restores pre-B27.
                    repo_root=ctx.repo_path,
                )
                scan_meta["naming_contract"] = nc_tele
                log_nc.info(
                    "naming_contract: pf %d (renamed %d, pinned %d, "
                    "cased %d), uf %d (renamed %d, twins %d, synth %d), "
                    "laws %s, labeler_pending %d"
                    % (
                        nc_tele.get("pf_total", 0),
                        nc_tele.get("pf_renamed", 0),
                        nc_tele.get("pf_pinned", 0),
                        nc_tele.get("casing_polished", 0),
                        nc_tele.get("uf_total", 0),
                        nc_tele.get("uf_renamed", 0),
                        nc_tele.get("uf_twins_resolved", 0),
                        nc_tele.get("uf_synth_named", 0),
                        nc_tele.get("laws_fixed"),
                        nc_tele.get("labeler_pending", 0),
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=7,
                    stage_name="naming_contract",
                    payload=dict(nc_tele),
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — naming must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"naming-contract failed ({exc}); displays unpolished"
                )
                log_nc.info(
                    f"naming_contract: FAILED ({exc}) — continuing",
                    feature=None,
                )

    # ── S2-A-v3 spray-generalization (FAULTLINE_SPRAY_GENERALIZED, OFF) ──
    # Structural absorption of the generalized R5-2 tech-dir-suffix spray
    # ('Manage setting AI components/constants/…' — the det-aggregation
    # regrain class) over the FINAL display names the naming contract just
    # emitted (probe canon: the boards it judged carried post-contract
    # names). Predicate + parent-name derivation live in naming_contract
    # (beside R5-2, whose paren class the G0 boundary protects); the apply
    # (member union, row drops, I14 repoints) is this seam — the naming
    # module's §4.8 identity law forbids structure writes inside it.
    # Flag OFF/unset ⇒ run_spray_generalization returns None before any
    # mutation ⇒ no scan_meta key ⇒ byte-identical (KS 4-way gate).
    try:
        from faultline.pipeline_v2.spray_absorption import (
            run_spray_generalization,
        )
        _spray_tele = run_spray_generalization(
            user_flows, list(bipartite.flows))
        if _spray_tele is not None:
            scan_meta["spray_generalized"] = _spray_tele
    except Exception as exc:  # noqa: BLE001 — absorption must never break a scan
        scan_meta.setdefault("warnings", []).append(
            f"spray-generalization failed ({exc}); rows unabsorbed"
        )

    # ── B75 — UF-giant cases-split (FAULTLINE_UF_CASES_SPLIT, OFF) ──────
    # Dir-tree case re-grain of giant catch-all journeys (mc >= census
    # band edge 30): >= K qualified surface cases extract as children;
    # the parent row survives as the residual (survivor-id law). Seam
    # rationale: AFTER the naming contract so the giant predicate + the
    # minted child names judge/mint against FINAL display names (the
    # spray precedent — the probe canon boards carried post-contract
    # names); AFTER spray-generalization so a spray-built union parent
    # is visible to the split (same structural-writer family, single
    # writer at a time); BEFORE 6.7e/dispatch-homing/I16-rehome/
    # synth_quality/6.97b-LOC/6.995 so children are ordinary rows to
    # every downstream hygiene pass (adjudication, homing, honest loc>0
    # from member spans, terminal classification). The stage repoints
    # extracted members' flow backpointers itself (I14) — the global
    # emission-integrity pass already ran above this seam.
    # Flag OFF/unset ⇒ run_uf_cases_split returns None before any
    # mutation ⇒ no scan_meta key ⇒ byte-identical (KS 4-way gate).
    # Armed 0-giant boards write no scan_meta key either (giants_seen ==
    # 0 ⇒ nothing was read or written — the Seg C inertness law; the
    # openstatus byte-ident gate).
    try:
        from faultline.pipeline_v2.uf_cases_split import (
            run_uf_cases_split,
        )
        _cs_tele = run_uf_cases_split(
            user_flows, list(bipartite.flows),
            routes_index=lineage_result.routes_index,
            repo_root=ctx.repo_path,
        )
        if _cs_tele is not None and _cs_tele.get("giants_seen"):
            scan_meta["uf_cases_split"] = _cs_tele
    except Exception as exc:  # noqa: BLE001 — split must never break a scan
        scan_meta.setdefault("warnings", []).append(
            f"uf-cases-split failed ({exc}); giants kept"
        )

    # ── Stage 6.7e — B57 Seg2 journey-evidence adjudicator (keyed-only) ─
    # Runs immediately AFTER the naming contract (selection needs Law C v1
    # name_confidence) and BEFORE synth_quality/emit_coverage_gaps so an
    # adjudicated demote lands as a typed coverage_gaps[] entry in THIS
    # scan. Keyless (no client) ⇒ hard no-op before any mutation and NO
    # scan_meta key — the byte-identity law. Confidence is re-scored only
    # through Law C (rescore_uf_confidence); the stage itself never writes
    # name_confidence. Kill-switch FAULTLINE_STAGE_6_7E_ADJUDICATOR
    # (default OFF).
    from faultline.pipeline_v2.stage_6_7e_adjudicator import (
        adjudicator_6_7e_enabled,
        run_stage_6_7e,
    )
    adjudicated_gaps: list[Any] = []
    if adjudicator_6_7e_enabled():
        try:
            adj_tele, adjudicated_gaps = run_stage_6_7e(
                user_flows,
                list(bipartite.flows),
                product_features,
                repo_root=ctx.repo_path,
                product_strings=product_strings,
                routes_index=lineage_result.routes_index,
                uf_authored_names=_e2e_authored_names,
                keeper_on=keeper_enabled(),
                model_id=model_id,
                cost_tracker=tracker,
                cache=getattr(ctx, "cache_backend", None),
                llm_health=llm_health,
            )
            if adj_tele.get("ran"):
                scan_meta["adjudicator_6_7e"] = adj_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            adjudicated_gaps = []
            scan_meta.setdefault("warnings", []).append(
                f"stage-6.7e adjudicator failed ({exc}); journeys unchanged")

    # ── Stage 6.985d — B37-ph2 dispatch-mint homing ($0, default ON) ───
    # Re-home each predominantly-dispatch user flow to the PF that OWNS the
    # mint's target file (path_index dev→PF first, anchor-chain fallback —
    # NOT the dev-of-first-attribution). Runs AFTER the final path_index
    # refresh + the flowless-PF backstops so it reads the same owner i16
    # will (no ping-pong) and a source the move leaves flowless never spawns
    # a NEW marker (the backstop already passed while it was covered); runs
    # BEFORE synth_quality so the target PF's marker demotes (its gap
    # dissolves — cal.com Cal Video). Mutates ONLY user_flows[].
    # product_feature_id. Kill-switch FAULTLINE_DISPATCH_HOMING_B37P2=0.
    from faultline.pipeline_v2.dispatch_homing import (
        dispatch_homing_enabled,
        home_dispatch_mints,
    )
    if dispatch_homing_enabled():
        try:
            dh_tele = home_dispatch_mints(
                user_flows, features, product_features,
                path_index=lineage_result.path_index)
            if dh_tele.get("rehomed"):
                scan_meta["dispatch_homing"] = dh_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"dispatch-homing failed ({exc}); UF homes left as-is")

    # S3 arbiter — apply deferred proposals at this pass boundary
    # (rung-priority == pass order; no-op when the flag is OFF).
    _arb_flush(product_features, note="post-dispatch")

    # ── B4 synthesized-journey quality (Stage 6.98) ────────────────────
    # Runs AFTER the naming contract (so demoted seeds/regrounded backstops
    # carry their final display names) and BEFORE the lane + Stage-7 write.
    # (a) Demote member-less ``system_flow_recall`` seeds out of user_flows[]
    #     into scan_meta["system_flow_seeds"] — a hollow journey (0 members,
    #     0 loc) is not a journey. (b) Reground single-member GENERIC backstop
    #     names from their member flow. OUTPUT-ONLY: mutates user_flows[] in
    #     place + flow backpointers + the display channel; validator-neutral
    #     (I7 tracked-only, I24/I13 untouched, I14 backpointers nulled).
    #     Kill-switch: FAULTLINE_SYNTH_QUALITY=0 restores pre-B4 output
    #     byte-identically.
    # B23 (behind FAULTLINE_MARKER_SURFACE_COORDS, default ON): the pass
    #     additionally (c) preserves the maintainer-authored labels of
    #     Track-C e2e markers (no more 13-18 identical 'Uncovered: <PF>
    #     routes' rows per board) and (d) attaches REAL surface coordinates
    #     (``surface_files`` whole-file spans) to member-less coverage
    #     markers from the mint-carried resolver files / home-PF member
    #     files — Stage 6.97b (below, runs later) then stamps an honest
    #     loc>0 from those spans. ``developer_features`` feeds the per-file
    #     LOC ledger and stays untouched.
    from faultline.pipeline_v2.synth_quality import (
        run_synth_quality,
        synth_quality_enabled,
    )
    # B45 — the typed coverage-gap channel. None (key absent from the result)
    # unless FAULTLINE_COVERAGE_GAP_CHANNEL is dual/full, keeping the off path
    # byte-identical to pre-B45. Threaded into the Stage-7 result below.
    coverage_gaps: list[Any] | None = None
    if synth_quality_enabled():
        try:
            _sq_tele = run_synth_quality(
                user_flows,
                list(bipartite.flows),
                product_features,
                scan_meta,
                developer_features=features,
            )
            _gaps = _sq_tele.get("coverage_gaps")
            # KEY-PRESENCE contract: the pass returns None in off mode (key
            # absent — byte-identity) and a LIST — possibly EMPTY — in
            # dual/full. A zero-gap board still ships "coverage_gaps": [] so
            # consumers ("coverage_gaps" in scan — warden gap-channel-leak
            # class, flowless-silent gap exemption) can detect the channel.
            if _gaps is not None:
                coverage_gaps = list(_gaps)
        except Exception as exc:  # noqa: BLE001 — quality pass never breaks a scan
            scan_meta.setdefault("warnings", []).append(
                f"synth-quality pass failed ({exc}); journeys unchanged"
            )

    # B57 Seg2 — adjudicated demote gaps join the typed channel. The stage
    # gates demotes on the gap channel being ACTIVE, so a non-empty list
    # here implies dual/full mode; merging AFTER emit keeps the B45
    # bijection telemetry intact, and the union re-sorts to the emit order
    # ((product_feature_id, id) — deterministic). Even if the synth-quality
    # pass faulted, the demoted rows' gaps still ship (no silent drop).
    if adjudicated_gaps:
        coverage_gaps = list(coverage_gaps or []) + list(adjudicated_gaps)
        coverage_gaps.sort(key=lambda g: (
            str(getattr(g, "product_feature_id", "") or ""),
            str(getattr(g, "id", "") or ""),
        ))

    # ── platform_infrastructure[] lane (Wave 2b, operator amendment) ───
    # The anchored path's residual surface: one row per lane resident
    # (product_feature_id=None + shared_reason). Assembled AFTER emission
    # integrity (the I2 phantom drop must not leave stale lane rows) and
    # after 6.97 (rows carry loc). None (omitted from output) when the
    # anchored mint did not run — the =0 A/B path stays byte-identical.
    if anchored_mint_applied:
        from faultline.pipeline_v2.stage_6_86_anchored_mint import (
            build_platform_infrastructure_lane,
        )
        try:
            # B52 — user_flows ride along so a flow-bearing lane row can
            # list its lane_ref journeys (additive; OFF byte-identical).
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

    # ── Stage 6.99 — path_index-aware I16 journey re-home (B20) ────────
    # Re-home each majority-foreign UF to its STRICT-MAJORITY entry-owner PF,
    # applying the validator's own I16 ruler AFTER the final path_index (2264)
    # AND the lane (above) exist — so the owner map matches the validator
    # exactly and the strict-majority guarantees I16 clears. Mutates ONLY
    # user_flows[].product_feature_id. Kill-switch FAULTLINE_I16_REHOME_B20=0.
    from faultline.pipeline_v2.stage_6_99_i16_rehome import (
        i16_rehome_enabled,
        rehome_foreign_entry_ufs,
    )
    if i16_rehome_enabled():
        try:
            rh_tele = rehome_foreign_entry_ufs(
                user_flows, features, product_features,
                lineage_result.path_index, platform_infrastructure)
            if rh_tele.get("rehomed"):
                scan_meta["i16_rehome"] = rh_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"i16-rehome failed ({exc}); UF homes left as-is")

    # S3 arbiter — apply deferred proposals at this pass boundary
    # (rung-priority == pass order; no-op when the flag is OFF).
    _arb_flush(product_features, note="post-i16")

    # ── Stage 6.99b — B69-v2 post-UF PF-homing hygiene rehome ──────────
    # Anchor-breadth ruler over member entries (mint-time structural truth,
    # via the 6.86 side-channel) — NOT the path_index owner map, which the
    # annexation being cured has itself poisoned (why 6.99/I16 stays blind
    # to this class). Runs AFTER naming/labeler/B31 so the only rows that
    # change are the rows it touches (surgical; the mint-time variant was
    # refuted for full-board redraw — banked fix/b69-pf-homing). Telemetry
    # is written WHENEVER the flag is armed (the cal.com strict-no-op gate
    # needs proof the rail evaluated and stayed inert). Default OFF ⇒
    # byte-identical.
    from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
        homing_hygiene_enabled as _hh_enabled,
        run_post_uf_rehome,
    )
    if _hh_enabled():
        try:
            # B73 it2 guard 2 (prior-hold) — rows the B24 nav-rehome stage
            # already adjudicated (stays[] holds + its own moves); consumed
            # ONLY by the flag-gated organic-move lane, inert otherwise.
            _b24_tele = scan_meta.get("mega_pf_nav_rehome") or {}
            _mega_holds: dict[str, str] = {}
            for _s in (_b24_tele.get("stays") or []):
                if isinstance(_s, dict) and _s.get("uf"):
                    _mega_holds[str(_s["uf"])] = str(_s.get("reason") or "held")
            for _mv in (_b24_tele.get("moves") or []):
                if isinstance(_mv, dict) and _mv.get("uf"):
                    _mega_holds.setdefault(
                        str(_mv["uf"]), f"moved:{_mv.get('to') or ''}")
            _hh_tele = run_post_uf_rehome(
                user_flows, features, product_features, _hh_anchor_registry,
                mega_holds=_mega_holds or None)
            scan_meta["post_uf_rehome"] = _hh_tele
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"post-uf-rehome failed ({exc}); UF homes left as-is")

    # ── Stage 6.998 — B76 metrics recompute-on-emission ────────────────
    # Stage 6 stamped commit metrics ONCE, before the whole Layer-2
    # surgery; every post-Stage-6 mint (8.9 subdecompose / 6.87
    # excavation / file-lane / provenance re-home) shipped tc=0 rows
    # wearing inherited authors/health, and the PF layer summed stale
    # contributor numbers (Alerts 5 vs ~63 real). Re-run the SAME
    # deterministic sweep over the FINAL membership here — after the
    # last membership-mutating stage of the phase (emission integrity's
    # phantom drops + path_index refresh, spray absorption, and the
    # 6.99/6.99b journey re-homes are all behind us; everything below
    # is display / LOC / telemetry channels) and before the Stage-7
    # write. PF metrics come from each PF's OWN path-set with
    # per-commit dedup — never sum-over-contributors. Metric fields
    # only: membership and hotspot_files are never touched.
    # Kill-switch FAULTLINE_METRICS_RECOMPUTE (default OFF) — unset/=0
    # never enters the pass ⇒ no scan_meta key ⇒ byte-identical.
    from faultline.pipeline_v2.metrics_recompute import (
        metrics_recompute_enabled,
        run_metrics_recompute,
    )
    if metrics_recompute_enabled():
        with StageLogger(run_dir, 7, "metrics_recompute") as log_mr:
            try:
                mr_tele = run_metrics_recompute(
                    features, product_features, ctx.commits,
                )
                scan_meta["metrics_recompute"] = mr_tele
                log_mr.info(
                    "metrics_recompute: dev %d rows (impossible %d -> %d), "
                    "pf %d rows (impossible %d -> %d)" % (
                        mr_tele["dev_rows"],
                        mr_tele["impossible_dev_before"],
                        mr_tele["impossible_dev_after"],
                        mr_tele["pf_rows"],
                        mr_tele["impossible_pf_before"],
                        mr_tele["impossible_pf_after"],
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=7,
                    stage_name="metrics_recompute",
                    payload=dict(mr_tele),
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — metrics must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"metrics-recompute failed ({exc}); metrics left as-is"
                )
                log_mr.info(
                    f"metrics_recompute: FAILED ({exc}) — continuing "
                    f"with Stage-6 values",
                    feature=None,
                )

    # ── W3 rider — full-bill LLM cost refresh (chain4 finding) ─────
    # ``run.py`` snapshots ``cost_usd``/``calls`` into scan_meta BEFORE
    # this phase runs, so every finalize-phase LLM call (6.7c splitter,
    # 6.7b refiner, 6.7d journey abstraction — the ANCHORED Call-1
    # included — and the W3 personas) was invisible to the output JSON,
    # the CLI cost line, and the wave-runner ledger: chain4 measured the
    # CLI reporting $0.0000 while 6.7d telemetry carried cost_usd=0.147.
    # The shared CostTracker records every one of those calls (standing
    # cost law) — re-snapshot it HERE, after the last LLM-bearing stage
    # and before the output writer consumes scan_meta. ``calls`` becomes
    # the tracker's full call count (per-stage detail keys are
    # unchanged); keyless scans stay byte-identical (0 == 0).
    scan_meta["cost_usd"] = round(tracker.total_cost_usd, 4)
    scan_meta["calls"] = tracker.call_count

    # ── S2 Seg D — degradation-honesty stamp (flag-gated, additive) ──────
    # The refiner (6.7b) and journey abstraction (6.7d) telemetry blocks are
    # both in scan_meta by now. A fail-open scan (the whole fresh refiner batch
    # failing at cost==0 = a dead key mid-scan, or 6.7d applied=False with real
    # candidates) currently self-reports healthy (degradations[]==[] in BOTH the
    # good and bad Soc0 board — probe 2026-07-18). Under FAULTLINE_DEGRADATION_
    # STAMP, append the typed FATAL records so validate_scan / keyed_proof FAIL
    # the proof gate. Default OFF → nothing appended → degradations[] byte-ident.
    from faultline.pipeline_v2.degradations import (
        degradation_stamp_enabled,
        detect_finalize_degradations,
    )
    if degradation_stamp_enabled():
        _stamped = detect_finalize_degradations(
            refiner=scan_meta.get("stage_6_7b_uf_refiner"),
            journey_abstraction=scan_meta.get("stage_6_7d_journey_abstraction"),
            # it3 widening — the generic zero-cost-fresh-fail law over the
            # other finalize LLM stages (the 2026-07-18 credit-400 pair:
            # splitter + 6.7e slipped the stamp).
            llm_stages={
                "stage_6_7c_uf_splitter":
                    scan_meta.get("stage_6_7c_uf_splitter"),
                "adjudicator_6_7e": scan_meta.get("adjudicator_6_7e"),
            },
        )
        if _stamped:
            scan_meta.setdefault("degradations", []).extend(_stamped)

    # ── S2 Seg C — LLM batch-canon cache-hit-rate telemetry (flag-gated) ──
    # One rollup ratio over the finalize-phase LLM batch stages (6.7c split /
    # 6.7b refine / 6.7d abstraction) so the canon's effect — volatile-count
    # drifts no longer invalidating keys — is measurable per scan. Additive
    # key, emitted ONLY under FAULTLINE_LLM_BATCH_CANON (OFF → byte-ident).
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        batch_canon_enabled as _batch_canon_on,
    )
    if _batch_canon_on():
        _r = scan_meta.get("stage_6_7b_uf_refiner") or {}
        _s = scan_meta.get("stage_6_7c_uf_splitter") or {}
        _j = scan_meta.get("stage_6_7d_journey_abstraction") or {}
        _hits = (int(_r.get("cache_hits") or 0) + int(_s.get("cache_hits") or 0)
                 + (1 if _j.get("cache_hit") else 0))
        _calls = (int(_r.get("llm_calls") or 0) + int(_s.get("llm_calls") or 0)
                  + int(_j.get("llm_calls") or 0))
        _total = _hits + _calls
        scan_meta["llm_batch_canon"] = {
            "enabled": True,
            "cache_hits": _hits,
            "llm_calls": _calls,
            "hit_rate": round(_hits / _total, 4) if _total else None,
        }

    # ── Stage 6.97b — journey-level LOC (B3, $0, deterministic, additive) ─
    # Operator bug B3: ``user_flows[].loc`` was ``None`` for EVERY journey
    # (the engine never emitted a UF-level LOC), so the dashboard LOC column
    # was blank for all journeys. Stamp ``UserFlow.loc`` = the UNION of the
    # OWNED line-range spans across the UF's member flows (per-file merged;
    # role="interior" + shared_paths-ledger nodes excluded — mirrors the
    # validator's ``_spine_flow_loc_owned`` selection). Runs LAST — after
    # W5.1's member-less backstop appends, emission integrity, terminal
    # home, and naming — so EVERY surviving journey (including mc=0
    # placeholders → honest ``0``) is stamped from FINAL membership.
    # STRICTLY ADDITIVE: the only output-JSON change is the new
    # ``user_flows[].loc`` key (telemetry stays in the side artifact / log,
    # never scan_meta, so the flag-ON vs flag-OFF diff is exactly ``loc``).
    # Kill-switch FAULTLINE_UF_LOC=0 → loc stays None → serializer omits it
    # → byte-identical to the pre-B3 engine. Metric must never break a scan.
    from faultline.pipeline_v2.stage_6_97b_uf_loc import (
        apply_uf_loc,
        uf_loc_enabled,
    )
    if uf_loc_enabled():
        with StageLogger(run_dir, 7, "uf_loc") as log_ufloc:
            try:
                _uf_loc_tele = apply_uf_loc(user_flows, list(bipartite.flows))
                log_ufloc.info(
                    "uf_loc: stamped %d journeys (%d with loc>0, %d zero)"
                    % (
                        _uf_loc_tele["user_flows_total"],
                        _uf_loc_tele["user_flows_with_loc"],
                        _uf_loc_tele["user_flows_zero_loc"],
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=7,
                    stage_name="uf_loc",
                    payload=_uf_loc_tele,
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — metric must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"uf-loc stage failed ({exc}); user_flows[].loc left unset"
                )
                log_ufloc.info(
                    f"uf_loc: FAILED ({exc}) — continuing without uf loc",
                    feature=None,
                )

    # ── Stage 6.97c — flow-level OWNED/SHARED LOC (B11, $0, additive) ─
    # Operator bug B11: distinct flows sharing one file rendered an IDENTICAL
    # file-grain LOC (the reactive-resume email trio all read "113"). Stamp
    # ``flows[].loc`` (owned-EXCLUSIVE span lines — the flow's unique story)
    # and ``flows[].loc_shared`` (owned span lines shared with ≥1 sibling flow
    # — blast-radius). By construction ``loc + loc_shared`` equals the flow's
    # ``_spine_flow_loc_owned`` union (the historical "113"), so it is a pure
    # DISPLAY partition: I13 loc-accounting is unmoved and I19's node-derived
    # owned numerator is untouched (additive fields; the node ledger is not
    # mutated). Runs right after 6.97b (UF loc) — spans are final. STRICTLY
    # ADDITIVE: the only output-JSON change is the two new keys (telemetry
    # lives in the side artifact / log, never scan_meta, so the flag-ON vs
    # flag-OFF diff is exactly ``loc``/``loc_shared``). Kill-switch
    # FAULTLINE_FLOW_LOC=0 → both stay None → serializer omits → byte-identical
    # to the pre-B11 engine. Metric must never break a scan.
    from faultline.pipeline_v2.stage_6_97c_flow_loc import (
        apply_flow_loc,
        flow_loc_enabled,
    )
    if flow_loc_enabled():
        with StageLogger(run_dir, 7, "flow_loc") as log_flowloc:
            try:
                _flow_loc_tele = apply_flow_loc(list(bipartite.flows))
                log_flowloc.info(
                    "flow_loc: stamped %d flows (%d with shared span, %d "
                    "exclusive-only)" % (
                        _flow_loc_tele["flows_total"],
                        _flow_loc_tele["flows_with_shared"],
                        _flow_loc_tele["flows_exclusive_only"],
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=7,
                    stage_name="flow_loc",
                    payload=_flow_loc_tele,
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — metric must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flow-loc stage failed ({exc}); flows[].loc left unset"
                )
                log_flowloc.info(
                    f"flow_loc: FAILED ({exc}) — continuing without flow loc",
                    feature=None,
                )

    # ── B30 — deterministic verb+resource flow naming (name channel) ──
    # Runs LAST (after 6.97c flow-loc, immediately before Stage 7) so
    # every consumer of the old names — UF rollup, lattice, lineage,
    # dedup, ids — already ran: the only output change is
    # ``flows[].name`` + its ``display_name``/``short_label`` kebab
    # mirrors. ``flow.id``/``uuid`` (the join keys) are never touched;
    # the Flow objects are shared with ``developer_features[].flows[]``
    # so one in-place mutation updates both views. Kill-switch
    # FAULTLINE_FLOW_NAME_V2=0 skips the stage → byte-identical output.
    # Telemetry lives in the side artifact/log only (never scan_meta),
    # mirroring the 6.97c precedent, so the flag ON/OFF diff is exactly
    # the name fields.
    from faultline.pipeline_v2.flow_name_v2 import (
        apply_flow_name_v2,
        flow_name_v2_enabled,
    )
    if flow_name_v2_enabled():
        with StageLogger(run_dir, 7, "flow_name_v2") as log_fnv2:
            try:
                _fnv2_tele = apply_flow_name_v2(
                    list(bipartite.flows),
                    routes_index=lineage_result.routes_index,
                    repo_path=ctx.repo_path,
                )
                log_fnv2.info(
                    "flow_name_v2: renamed %d/%d flows (route=%d symbol=%d "
                    "honest-fallback=%d feature-qualified=%d ordinal=%d)" % (
                        _fnv2_tele["renamed_total"],
                        _fnv2_tele["flows_total"],
                        _fnv2_tele["renamed_route"],
                        _fnv2_tele["renamed_symbol"],
                        _fnv2_tele["kept_honest_fallback"],
                        _fnv2_tele["collision_feature_qualified"],
                        _fnv2_tele["collision_ordinal"],
                    ),
                    feature=None,
                )
                write_stage_artifact(
                    ctx.repo_path,
                    stage_index=7,
                    stage_name="flow_name_v2",
                    payload=_fnv2_tele,
                    run_dir=run_dir,
                )
            except Exception as exc:  # noqa: BLE001 — naming must never break a scan
                scan_meta.setdefault("warnings", []).append(
                    f"flow-name-v2 stage failed ({exc}); flow names left as-is"
                )
                log_fnv2.info(
                    f"flow_name_v2: FAILED ({exc}) — continuing with old names",
                    feature=None,
                )

    # ── Stage 6.995 — B68 terminal 4-way classification ────────────
    # Operator doctrine 2026-07-14: the gap channel is an INTERNAL state,
    # never a final board category. Runs LAST — after every gap producer
    # (B45 emit + 6.7e adjudicated merge) and every board mutation — so
    # it judges the exact rows Stage 7 would emit. Mutates ONLY
    # coverage_gaps[] (+ the scan_meta audit trace); ownership channels
    # (path_index / features / user_flows) are read-only inputs.
    # Kill-switch FAULTLINE_TERMINAL_CLASSIFICATION (default OFF) —
    # unset/=0 never runs the pass (byte-identity with main).
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
                ctx.repo_path,
                stage_index=7,
                stage_name="terminal_classification",
                payload=_tc_tele,
                run_dir=run_dir,
            )
        except Exception as exc:  # noqa: BLE001 — never break a scan
            scan_meta.setdefault("warnings", []).append(
                f"terminal-classification stage failed ({exc}); "
                f"coverage_gaps left as-is"
            )

    # ── S3 arbiter — single application point (before Stage 7 output) ──
    # Every product_feature_id proposal for this scan has now been recorded
    # by the observer. Run the arbiter ONCE: its rung-priority replay ==
    # the current pass order, so it reproduces the cascade byte-for-byte
    # (record order == execution order → last-writer-wins), and it emits
    # the overturn census + the post-freeze conflict census into scan_meta
    # (both keys are stripped by normalize_scan — run-forensics, never
    # content). No-op unless FAULTLINE_OVERTURN_ARBITER=1.
    _overturn_ledger = getattr(ctx, "overturn_ledger", None)
    if _overturn_ledger is not None:
        from faultline.pipeline_v2.overturn_ledger import finalize_arbiter
        try:
            finalize_arbiter(
                _overturn_ledger, features, user_flows, scan_meta,
                product_features,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never breaks a scan
            scan_meta.setdefault("warnings", []).append(
                f"overturn-arbiter failed ({exc}); census skipped"
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
        "non_product_surfaces": non_product_surfaces,
        "platform_infrastructure": platform_infrastructure,
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
            non_product_surfaces=non_product_surfaces,
            platform_infrastructure=platform_infrastructure,
            # B45 — None (key omitted) unless the gap channel emitted gaps.
            coverage_gaps=coverage_gaps,
        )
        log7.info(f"wrote feature map to {out}", feature=None)

    return out


__all__ = [
    "run_finalize_phase",
]
