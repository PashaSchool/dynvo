"""Tests for Sprint F (2026-05-20) parallelism + budget in Stage 6.3.

The parallel path must produce identical feature mutations as the serial
path, and the wall-clock budget must trigger graceful degradation when
exceeded.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import pytest

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_WALL_BUDGET_SEC,
    enrich_with_import_tree,
)


# ── Helpers (mirror test_stage_6_3_import_tree.py) ────────────────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path) -> SimpleNamespace:
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            try:
                rel = f.relative_to(repo).as_posix()
                tracked.append(rel)
            except ValueError:
                continue
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        run_dir=None,
        stack="next-app-router",
        monorepo=False,
        workspaces=[],
    )


def _new_feature(
    name: str,
    paths: Iterable[str],
    *,
    flows: list[Flow] | None = None,
    description: str | None = None,
) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        description=description,
        flows=flows or [],
        layer="developer",
    )


def _new_flow(name: str, entry_file: str, entry_line: int = 1) -> Flow:
    return Flow(
        name=name,
        entry_point_file=entry_file,
        entry_point_line=entry_line,
        paths=[entry_file],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )


def _build_multi_feature_repo(tmp_path: Path, n_features: int = 6) -> tuple[SimpleNamespace, list[Feature]]:
    """Create N independent forward chains so each feature has its own
    BFS that runs without touching another feature's files. Lets us
    assert the parallel + serial outputs match exactly.
    """
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./*"]},
        },
    }))
    features: list[Feature] = []
    for i in range(n_features):
        page = f"app/feat{i}/page.tsx"
        comp = f"components/Form{i}.tsx"
        hook = f"hooks/use{i}.tsx"
        _w(tmp_path / page, f"""\
import {{ Form{i} }} from "@/components/Form{i}";
export default function Page{i}() {{ return <Form{i} />; }}
""")
        _w(tmp_path / comp, f"""\
import {{ use{i} }} from "@/hooks/use{i}";
export function Form{i}() {{ const v = use{i}(); return <div>{{v}}</div>; }}
""")
        _w(tmp_path / hook, f"""\
export function use{i}() {{ return {i}; }}
""")
        flow = _new_flow(f"flow{i}", page, entry_line=2)
        features.append(_new_feature(f"feat{i}", [page], flows=[flow]))
    return _ctx(tmp_path), features


def _snapshot(features: list[Feature]) -> list[dict]:
    """Stable per-feature snapshot for equivalence checks."""
    out: list[dict] = []
    for f in features:
        out.append({
            "name": f.name,
            "paths": sorted(f.paths),
            "shared_attrs_files": sorted(a.file_path for a in f.shared_attributions),
            "symbol_attrs": sorted(
                (a.file, a.symbol, a.line_start, a.line_end, a.role)
                for a in f.symbol_attributions
            ),
        })
    return out


# ── Tests ─────────────────────────────────────────────────────────────────


def test_parallel_matches_serial_for_independent_features(tmp_path: Path) -> None:
    """Running with max_workers=4 must yield identical per-feature
    mutations as max_workers=1 when feature graphs are independent.
    """
    ctx_s, feats_s = _build_multi_feature_repo(tmp_path / "serial", n_features=4)
    ctx_p, feats_p = _build_multi_feature_repo(tmp_path / "parallel", n_features=4)

    res_s = enrich_with_import_tree(ctx_s, feats_s, max_workers=1, wall_budget_sec=0)
    res_p = enrich_with_import_tree(ctx_p, feats_p, max_workers=4, wall_budget_sec=0)

    assert _snapshot(feats_s) == _snapshot(feats_p)
    assert res_s.total_seeds == res_p.total_seeds
    assert res_s.total_symbols_emitted == res_p.total_symbols_emitted
    assert res_p.max_workers == 4
    assert res_s.max_workers == 1
    assert res_p.budget_exceeded is False


def test_budget_exceeded_marks_remaining_features_skipped(tmp_path: Path) -> None:
    """A wall-clock budget of 0.001s must trip immediately and mark
    every-or-most features as ``budget_skipped`` with the warning
    surfacing via the logger.
    """
    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=6)
    res = enrich_with_import_tree(
        ctx, feats, max_workers=1, wall_budget_sec=0.001,
    )
    # Budget triggered, telemetry captures the skip count > 0 (serial
    # path will likely process feature 0 before checking the clock,
    # then skip 1..5).
    assert res.budget_exceeded is True
    assert res.features_budget_skipped >= 1
    assert res.budget_sec == pytest.approx(0.001)


def test_budget_disabled_processes_all(tmp_path: Path) -> None:
    """``wall_budget_sec=0`` disables the budget entirely (legacy)."""
    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=3)
    res = enrich_with_import_tree(ctx, feats, max_workers=1, wall_budget_sec=0)
    assert res.budget_exceeded is False
    assert res.features_budget_skipped == 0


def test_default_budget_is_scale_invariant_no_skip(tmp_path: Path) -> None:
    """With the default budget (wall_budget_sec=None) the wall scales
    with feature count, so no feature is skipped on a healthy repo.
    """
    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=6)
    # wall_budget_sec=None → per-feature * len(features) (large enough).
    res = enrich_with_import_tree(ctx, feats, max_workers=4)
    assert res.budget_exceeded is False
    assert res.features_budget_skipped == 0
    # The resolved wall must scale with feature count, not be flat.
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        DEFAULT_PER_FEATURE_BUDGET_SEC,
    )
    assert res.budget_sec == DEFAULT_PER_FEATURE_BUDGET_SEC * len(feats)


def test_per_feature_budget_env_override(tmp_path: Path, monkeypatch) -> None:
    """FAULTLINE_STAGE_6_3_PER_FEATURE_SEC tunes the per-feature wall."""
    monkeypatch.setenv("FAULTLINE_STAGE_6_3_PER_FEATURE_SEC", "3")
    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=4)
    res = enrich_with_import_tree(ctx, feats, max_workers=2)
    assert res.budget_sec == 3.0 * len(feats)
    assert res.features_budget_skipped == 0


def test_worker_exception_does_not_break_stage(tmp_path: Path, monkeypatch) -> None:
    """A worker exception on ONE feature must not break the stage; the
    rest of the features still get enriched.
    """
    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=4)

    # Patch _compute_one_feature to raise on the second feature only.
    import faultline.pipeline_v2.stage_6_3_import_tree as mod

    original = mod._compute_one_feature
    crashed_names: list[str] = []

    def flaky(feature, feature_index, **kwargs):
        if feature.name == "feat1":
            crashed_names.append(feature.name)
            raise RuntimeError("simulated worker crash")
        return original(feature, feature_index, **kwargs)

    monkeypatch.setattr(mod, "_compute_one_feature", flaky)

    res = enrich_with_import_tree(ctx, feats, max_workers=4, wall_budget_sec=0)
    # The other 3 features still got their forward chains.
    feat_by_name = {f.name: f for f in feats}
    assert "components/Form0.tsx" in feat_by_name["feat0"].paths
    assert "components/Form2.tsx" in feat_by_name["feat2"].paths
    assert "components/Form3.tsx" in feat_by_name["feat3"].paths
    # The crashed feature did NOT mutate (stays at its original paths).
    assert feat_by_name["feat1"].paths == ["app/feat1/page.tsx"]
    # Telemetry preserves the failure record (source_kind == "error").
    feat1_entry = next(
        e for e in res.per_feature if e.feature_name == "feat1"
    )
    assert feat1_entry.source_kind == "error"
    assert crashed_names == ["feat1"]


def test_artifact_payload_carries_concurrency_block(tmp_path: Path) -> None:
    """``build_artifact_payload`` must surface the Sprint F concurrency
    + budget telemetry so dashboards can flag silent slowdowns.
    """
    from faultline.pipeline_v2.stage_6_3_import_tree import build_artifact_payload

    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=2)
    # A clearly-generous budget: under the deterministic seconds→count
    # semantics it affords floor(1000 / 6.0) = 166 features >> 2, so nothing
    # is skipped. (The old wall-clock semantics treated any modest value as
    # "plenty of wall time"; the value is arbitrary for this payload check.)
    res = enrich_with_import_tree(ctx, feats, max_workers=2, wall_budget_sec=1000.0)
    payload = build_artifact_payload(
        res,
        max_depth=8, max_files_per_feature=100, max_symbols_per_feature=500,
    )
    assert "concurrency" in payload
    assert payload["concurrency"]["max_workers"] == 2
    assert payload["concurrency"]["budget_sec"] == 1000.0
    assert payload["concurrency"]["budget_exceeded"] is False


def test_env_var_workers_override(tmp_path: Path, monkeypatch) -> None:
    """``FAULTLINE_STAGE_6_3_WORKERS`` env var lets ops override the
    default without a code change.
    """
    monkeypatch.setenv("FAULTLINE_STAGE_6_3_WORKERS", "2")
    monkeypatch.setenv("FAULTLINE_STAGE_6_3_BUDGET_SEC", "0")
    ctx, feats = _build_multi_feature_repo(tmp_path, n_features=2)
    res = enrich_with_import_tree(ctx, feats)
    assert res.max_workers == 2
