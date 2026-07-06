"""Orchestration tests for the run.py decomposition.

Three guards added with refactor/run-decomposition:

  1. An ordered ``(stage_index, stage_name)`` artifact-sequence snapshot
     for a mocked minimal run — pins the stage order + StageLogger
     indexes/names + artifact filenames across the phase modules.
  2. A ``scan_meta`` assembly unit test over pure stubs — pins the
     load-bearing key set the dashboards / replay tooling consume.
  3. Incremental-wiring unit tests — the splice partition rule
     (never double-emit on name collision) and the Layer-2 no-op
     decision (ALWAYS False on a full / cold scan).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.incremental_wiring import (
    is_layer2_noop,
    splice_untouched_features,
)
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.scan_meta import (
    FallbackShare,
    assemble_scan_meta,
    compute_fallback_share,
    extractor_hits_from_stage1,
)
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result


def _git_init_with_one_commit(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: initial"], cwd=repo, check=True,
    )


def _patch_llm_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 3 + Stage 4 doubles — no network, canned empty results."""

    def _fake_stage_3(features, ctx, *, model, cost_tracker, **_kw):
        from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="patched")
                for f in features
            ],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
        )

    def _fake_stage_4(unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
            clusters_total=0,
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
        )

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)


# ── 1. Ordered artifact-sequence snapshot ───────────────────────────────

# The orchestrated write_stage_artifact sequence for a FULL (cold) scan.
# This is the decomposition contract: stage order, StageLogger indexes,
# and artifact filenames must survive any further refactor unchanged.
EXPECTED_ARTIFACT_SEQUENCE: list[tuple[int, str]] = [
    (0, "intake"),
    (0, "auditor"),
    (1, "extractors"),
    (2, "reconcile"),
    (2, "membership_closure"),
    (3, "flows"),
    (4, "residual"),
    (5, "postprocess"),
    (5, "sibling_collapse"),
    (5, "cross_flow_dedup"),
    (5, "bipartite"),
    (6, "metrics"),
    (6, "product_clusterer"),
    (6, "import_tree"),
    (6, "framework_enrich"),
    (6, "branch_slicer"),
    (8, "marketing_clusterer"),
    (8, "member_backfill"),
    (8, "nonsource_drop"),
    (8, "scaffold_filter"),
    (8, "di_attribution"),
    (8, "anchor_desink"),
    (8, "shared_members"),
    (8, "anchor_subdecompose"),
    (8, "llm_component_split"),
    (8, "domain_member_attribution"),
    # Stage 8.9.7 (2026-07-05) — per-vendor connector split. Deterministic,
    # $0; default ON since Product-Spine Wave 1 (2026-07-06, opt-out =0).
    (8, "vendor_connector_split"),
    # Stage 8.9.8 (2026-07-06, Product-Spine §4.4) — hub/child PF binding:
    # connector-hub members land on ONE product feature (sibling parity).
    (8, "hub_pf_binding"),
    (3, "flow_expansion"),
    (6, "test_strip"),
    (6, "generated_strip"),
    # Stage 6.86 (Wave 2b, 2026-07-06, Product-Spine §4.3) — anchored PF
    # minting: dev→PF from anchor lineage, PF candidates from ranked
    # anchor sources, platform_infrastructure[] residual lane. $0,
    # deterministic, default ON (FAULTLINE_SPINE_ANCHORED_MINT=0 off).
    # See stage_6_86_anchored_mint.py.
    (6, "anchored_mint"),
    (6, "user_flows"),
    (6, "uf_splitter"),
    (6, "uf_refiner"),
    (6, "history"),
    (6, "impact"),
    # Stage 6.6 (2026-06) — Monorepo Assembly View. Deterministic, $0,
    # additive; emitted on every run just before Stage 7 output (a trivial
    # {"is_monorepo": False} view for single repos). See
    # stage_6_6_monorepo_assembly.py.
    (6, "monorepo_assembly"),
    # Stage 6.97 (2026-07-05) — deterministic feature-level LOC. $0,
    # additive flat ``loc`` on dev features + dedup PF rollup; telemetry
    # artifact emitted on every run (default ON). See
    # stage_6_97_feature_loc.py.
    (6, "feature_loc"),
    # Stage 6.85 emission lane (Wave 2a, 2026-07-06) — product-surface
    # taxonomy: UF/PF tags + non_product_surfaces[] lane split + info-page
    # dissolution + shared_reason stamping. $0, deterministic, default ON;
    # telemetry artifact emitted on every run. See surface_taxonomy.py.
    (6, "surface_taxonomy"),
    # Emission integrity (2026-07-05) — referential round-trip guarantee.
    # $0, deterministic, runs LAST before Stage 7 output: I2 phantom drop,
    # I12 UF→PF ref reconcile, I14 flow-backpointer rewrite. Telemetry
    # artifact emitted on every run. See emission_integrity.py.
    (7, "emission_integrity"),
]


def test_artifact_sequence_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mocked minimal run emits the artifacts in EXACTLY this order."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _patch_llm_stages(monkeypatch)

    repo = tmp_path / "seq-app"
    _git_init_with_one_commit(
        repo,
        {
            "package.json": json.dumps(
                {"name": "seq", "dependencies": {"next": "14.0.0"}},
            ),
            "app/foo/page.tsx": "export default function Page() { return null; }\n",
            "app/api/bar/route.ts": "export async function GET() {}\n",
            "next.config.js": "module.exports = {};\n",
        },
    )

    # Record every orchestrated write_stage_artifact call IN ORDER by
    # wrapping the name each phase module bound at import time.
    recorded: list[tuple[int, str]] = []

    import faultline.pipeline_v2.phase_enrich as phase_enrich
    import faultline.pipeline_v2.phase_extract as phase_extract
    import faultline.pipeline_v2.phase_finalize as phase_finalize
    import faultline.pipeline_v2.phase_intake as phase_intake
    import faultline.pipeline_v2.phase_layer2 as phase_layer2
    import faultline.pipeline_v2.phase_postprocess as phase_postprocess
    from faultline.pipeline_v2.stage_7_output import (
        write_stage_artifact as _real_write,
    )

    def _recording_write(repo_path, stage_index, stage_name, payload, **kw):
        recorded.append((stage_index, stage_name))
        return _real_write(repo_path, stage_index, stage_name, payload, **kw)

    for mod in (
        run_module,
        phase_intake,
        phase_extract,
        phase_postprocess,
        phase_enrich,
        phase_layer2,
        phase_finalize,
    ):
        monkeypatch.setattr(mod, "write_stage_artifact", _recording_write)

    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)

    assert recorded == EXPECTED_ARTIFACT_SEQUENCE

    # Every recorded artifact also landed on disk under the run dir
    # with its canonical NN-stage-<name>.json filename.
    run_dir = Path(result["stage_artifact_dir"])
    for idx, name in EXPECTED_ARTIFACT_SEQUENCE:
        artifact = run_dir / f"{idx:02d}-stage-{name}.json"
        assert artifact.is_file(), f"missing artifact: {artifact.name}"


# ── 2. scan_meta assembly unit test ─────────────────────────────────────


def test_compute_fallback_share_partition() -> None:
    """Fallback share counts only residual survivors in the final set."""
    feats = [SimpleNamespace(name=n) for n in ("a", "b", "resid-1")]
    residual = [SimpleNamespace(name="resid-1"), SimpleNamespace(name="resid-2")]
    share = compute_fallback_share(
        stage5_drop_log=[("resid-2", "junk_name: too generic")],
        residual_features=residual,
        features=feats,
    )
    assert share.fallback_count == 1
    assert share.total_features == 3
    assert share.deterministic_count == 2
    assert share.llm_share == pytest.approx(1 / 3)
    assert share.extractor_coverage_pct == pytest.approx(2 / 3)


def test_assemble_scan_meta_keys() -> None:
    """The assembled scan_meta carries the load-bearing key set."""
    ctx = SimpleNamespace(
        run_id="run-1",
        stack="next-app-router",
        monorepo=False,
        workspace_manager=None,
        stack_signals=["next.config.js"],
        subpath=None,
        workspaces=[],
    )
    verdict = SimpleNamespace(
        primary_stack="next-app-router",
        secondary_stacks=[],
        confidence=0.9,
        extractor_hints=["route_files"],
        cost_usd=0.0,
        fallback_used=False,
        reasoning="stub",
        corrections=[],
    )
    validation_drops = SimpleNamespace(
        as_dict=lambda: {
            "filesystem_missing": 0, "anchor_duplicate": 0, "junk_name": 0,
        },
    )
    stage2 = SimpleNamespace(
        zero_path_drops_count=0,
        zero_path_drops_sample=[],
        schema_only_suppressed_count=0,
        schema_only_suppressed_sample=[],
    )
    stage3 = SimpleNamespace(
        cost_usd=0.0, reach_telemetry={}, llm_calls=0, cache_hits=0,
    )
    stage4 = SimpleNamespace(
        cost_usd=0.0,
        clusters_total=0,
        clusters_processed=0,
        singletons_synthesized=0,
        singletons_skipped=0,
        saturation_stopped=False,
        cost_cap_hit=False,
        guard_singletons_dropped=0,
        guard_incoherent_clusters_split=0,
        guard_drops_sample=[],
        guard_noise_path_drops=0,
    )
    stage5_result = SimpleNamespace(dedup_merges=[])
    s53 = SimpleNamespace(collapse_groups=[], features_collapsed=0)
    enrichment = SimpleNamespace(
        alias_map={},
        total_seeds=0,
        total_files_reached=0,
        total_symbols_emitted=0,
        cycles_detected=0,
        depth_capped_events=0,
        external_skipped=0,
        cache_hits=0,
        elapsed_sec=0.0,
    )
    framework_enrich_telemetry: dict[str, Any] = {
        "active_linkers": [],
        "skipped_linkers": [],
        "per_linker": {},
        "links_emitted_total": 0,
        "elapsed_sec": 0.0,
    }
    shape_result = SimpleNamespace(
        shape="single-saas-routed",
        confidence=0.8,
        rationale="stub",
        matched_signals=["routes"],
    )
    share = FallbackShare(
        fallback_count=1,
        total_features=4,
        deterministic_count=3,
        llm_share=0.25,
        extractor_coverage_pct=0.75,
    )

    scan_meta = assemble_scan_meta(
        ctx=ctx,
        verdict=verdict,
        model_id="claude-haiku-4-5-20251001",
        extractor_hits={"route_files": 2},
        workspace_telemetry={"workspace_packages_detected": 0},
        share=share,
        validation_drops=validation_drops,
        stage2=stage2,
        stage3=stage3,
        stage4=stage4,
        stage5_result=stage5_result,
        s53=s53,
        s53_features_pre=4,
        s53_features_post=4,
        s53_collapse_sample=[],
        warnings=["w1"],
        elapsed=1.23,
        cost_usd=0.0,
        llm_calls=0,
        run_dir=Path("/tmp/run-1"),
        llm_reconcile=False,
        bipartite_telemetry={"flows_total": 0},
        product_telemetry={"product_features_total": 0},
        per_ws_telemetry={"stage_1_per_workspace_active": False},
        enrichment=enrichment,
        effective_max_tree_depth=8,
        framework_enrich_telemetry=framework_enrich_telemetry,
        branch_slicer_telemetry={"active": False},
        stage_8_telemetry={"source": "deterministic-only", "haiku_called": False},
        stage_8_rollup_telemetry={"rollup_strategy": "universal-residual"},
        stage_8_5_backfill_telemetry={"attached": 0},
        stage_8_6_telemetry={
            "dropped": 0, "dropped_sample": [],
            "pf_recomputed": 0, "pf_dropped_empty": 0,
        },
        shape_result=shape_result,
    )

    assert scan_meta["pipeline_version"] == "v2"
    assert scan_meta["run_id"] == "run-1"
    assert scan_meta["stack"] == "next-app-router"
    assert scan_meta["model"] == "claude-haiku-4-5-20251001"
    # llm_fallback_pct (legacy) mirrors llm_share (canonical).
    assert scan_meta["llm_fallback_pct"] == scan_meta["llm_share"] == 0.25
    assert scan_meta["extractor_coverage_pct"] == 0.75
    assert scan_meta["deterministic_feature_count"] == 3
    assert scan_meta["residual_feature_count"] == 1
    assert scan_meta["warnings"] == ["w1"]
    assert scan_meta["stage_artifact_dir"] == "/tmp/run-1"
    # Telemetry dicts are spliced/copied, not referenced by surprise keys.
    assert scan_meta["flows_total"] == 0
    assert scan_meta["stage_8"]["source"] == "deterministic-only"
    assert scan_meta["stage_08_rollup"]["rollup_strategy"] == "universal-residual"
    assert scan_meta["stage_8_6_nonsource_drops"] == 0
    assert scan_meta["stage_06"]["shape"] == "single-saas-routed"
    assert scan_meta["stage_06"]["fallback_used"] is False
    assert scan_meta["stage_6_3"]["max_depth_configured"] == 8
    assert scan_meta["audited_stack"] == "next-app-router"
    assert scan_meta["auditor_fallback_used"] is False


def test_extractor_hits_drops_errors_sentinel() -> None:
    stage1_out = {"route_files": [1, 2, 3], "_errors": {"x": "boom"}}
    assert extractor_hits_from_stage1(stage1_out) == {"route_files": 3}


# ── 3. Incremental wiring partition unit tests ─────────────────────────


def test_splice_untouched_features_partition() -> None:
    """Splice appends only non-colliding untouched features, in order."""
    features = [SimpleNamespace(name="touched-a"), SimpleNamespace(name="touched-b")]
    untouched = [
        SimpleNamespace(name="touched-a"),   # collision → fresh one wins
        SimpleNamespace(name="untouched-c"),
        SimpleNamespace(name="untouched-d"),
        SimpleNamespace(name="untouched-c"),  # dup within untouched → once
    ]
    spliced = splice_untouched_features(features, untouched)
    assert spliced == 2
    assert [f.name for f in features] == [
        "touched-a", "touched-b", "untouched-c", "untouched-d",
    ]


def test_is_layer2_noop_decision() -> None:
    """Layer-2 reuse is ALWAYS False on a full scan (cold-scan rule)."""
    base = {"product_features": []}
    # Full scan → never a no-op, even with a base scan + zero touched.
    assert is_layer2_noop(
        is_full_scan=True,
        base_scan=base,
        gate_meta={"incremental_gate_features_touched": 0},
    ) is False
    # Incremental + zero touched + base present → no-op.
    assert is_layer2_noop(
        is_full_scan=False,
        base_scan=base,
        gate_meta={"incremental_gate_features_touched": 0},
    ) is True
    # Incremental but features touched → not a no-op.
    assert is_layer2_noop(
        is_full_scan=False,
        base_scan=base,
        gate_meta={"incremental_gate_features_touched": 3},
    ) is False
    # Incremental without a loaded base scan → not a no-op.
    assert is_layer2_noop(
        is_full_scan=False,
        base_scan=None,
        gate_meta={"incremental_gate_features_touched": 0},
    ) is False
