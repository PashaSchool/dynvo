"""Tests for Sprint F (2026-05-20) parallelism + budget in Stage 6.4."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.framework_linkers.base import FrameworkLink
from faultline.models.types import Feature
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_6_4_framework_enrich import (
    DEFAULT_MAX_WORKERS,
    run_stage_6_4,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo_path=tmp_path,
        tracked_files=tuple(),
        run_dir=None,
        stack="next-app-router",
        audited_stack="next-app-router",
        secondary_stacks=(),
        monorepo=False,
        workspaces=[],
    )


def _new_feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
    )


class _CannedLinker:
    """Emits one link per feature; ``sleep_sec`` simulates slow IO."""

    name = "canned"

    def __init__(self, sleep_sec: float = 0.0) -> None:
        self.sleep_sec = sleep_sec
        self.calls: list[str] = []

    def is_active(self, ctx) -> bool:  # noqa: ANN001
        return True

    def link_for_feature(self, feature, ctx, log) -> list[FrameworkLink]:  # noqa: ANN001
        if self.sleep_sec:
            time.sleep(self.sleep_sec)
        self.calls.append(feature.name)
        return [
            FrameworkLink(
                source_file=feature.paths[0] if feature.paths else "<unknown>",
                source_symbol="<module>",
                source_line=1,
                target_file=f"api/{feature.name}/route.ts",
                target_symbol="POST",
                target_line_start=1,
                target_line_end=3,
                linker=self.name,
                link_kind="http-route",
                confidence=1.0,
                reason="canned",
            ),
        ]


# ── Tests ─────────────────────────────────────────────────────────────────


def test_parallel_emits_same_links_as_serial(tmp_path: Path) -> None:
    """Parallel runner produces the same attribution count as serial."""
    feats_s = [_new_feature(f"f{i}", [f"app/f{i}/page.tsx"]) for i in range(6)]
    feats_p = [_new_feature(f"f{i}", [f"app/f{i}/page.tsx"]) for i in range(6)]

    with StageLogger(tmp_path / "s", 6, "fe_serial") as log_s:
        res_s = run_stage_6_4(
            _ctx(tmp_path), feats_s, log_s,
            linkers=[_CannedLinker()],
            max_workers=1, wall_budget_sec=0,
        )
    with StageLogger(tmp_path / "p", 6, "fe_parallel") as log_p:
        res_p = run_stage_6_4(
            _ctx(tmp_path), feats_p, log_p,
            linkers=[_CannedLinker()],
            max_workers=4, wall_budget_sec=0,
        )

    assert res_s.links_emitted_total == res_p.links_emitted_total
    s_sigs = [
        sorted((a.file, a.symbol) for a in f.symbol_attributions)
        for f in feats_s
    ]
    p_sigs = [
        sorted((a.file, a.symbol) for a in f.symbol_attributions)
        for f in feats_p
    ]
    assert s_sigs == p_sigs
    assert res_p.max_workers == 4
    assert res_s.max_workers == 1


def test_parallel_is_faster_with_slow_linker(tmp_path: Path) -> None:
    """A linker that sleeps 50ms per feature on 6 features finishes
    faster with 4 workers than with 1 (sanity check that ThreadPool
    actually parallelises blocking IO).
    """
    sleep_each = 0.05
    n = 6

    feats_s = [_new_feature(f"f{i}", [f"app/f{i}.tsx"]) for i in range(n)]
    feats_p = [_new_feature(f"f{i}", [f"app/f{i}.tsx"]) for i in range(n)]

    with StageLogger(tmp_path / "s", 6, "fe_serial") as log_s:
        t0 = time.monotonic()
        run_stage_6_4(
            _ctx(tmp_path), feats_s, log_s,
            linkers=[_CannedLinker(sleep_sec=sleep_each)],
            max_workers=1, wall_budget_sec=0,
        )
        serial_elapsed = time.monotonic() - t0

    with StageLogger(tmp_path / "p", 6, "fe_parallel") as log_p:
        t0 = time.monotonic()
        run_stage_6_4(
            _ctx(tmp_path), feats_p, log_p,
            linkers=[_CannedLinker(sleep_sec=sleep_each)],
            max_workers=4, wall_budget_sec=0,
        )
        parallel_elapsed = time.monotonic() - t0

    # Parallel must shave at least 30% off the serial wall (loose
    # bound to avoid flakiness on slow CI hardware).
    assert parallel_elapsed < serial_elapsed * 0.7, (
        f"parallel={parallel_elapsed:.3f}s serial={serial_elapsed:.3f}s"
    )


def test_budget_exceeded_warns_and_skips(tmp_path: Path) -> None:
    """A tiny budget trips immediately; remaining features are
    recorded as ``budget_skipped`` in telemetry.
    """
    feats = [_new_feature(f"f{i}", [f"app/f{i}.tsx"]) for i in range(6)]
    with StageLogger(tmp_path, 6, "fe_budget") as log:
        res = run_stage_6_4(
            _ctx(tmp_path), feats, log,
            linkers=[_CannedLinker(sleep_sec=0.02)],
            max_workers=1, wall_budget_sec=0.001,
        )
    assert res.budget_exceeded is True
    assert res.features_budget_skipped >= 1
    # Telemetry surfaces the per-linker skip count too.
    canned_block = res.per_linker.get("canned", {})
    assert "budget_skipped" in canned_block


def test_default_budget_is_scale_invariant_no_skip(tmp_path: Path) -> None:
    """With the default budget (wall_budget_sec=None) the wall scales
    with (feature x active-linker) work units, so nothing is skipped.
    """
    from faultline.pipeline_v2.stage_6_4_framework_enrich import (
        DEFAULT_PER_UNIT_BUDGET_SEC,
    )

    feats = [_new_feature(f"f{i}", [f"app/f{i}.tsx"]) for i in range(8)]
    with StageLogger(tmp_path, 6, "fe_scale") as log:
        res = run_stage_6_4(
            _ctx(tmp_path), feats, log,
            linkers=[_CannedLinker()],
            max_workers=4,  # wall_budget_sec=None → scale-invariant default
        )
    assert res.budget_exceeded is False
    assert res.features_budget_skipped == 0
    # 1 active linker x 8 features.
    assert res.budget_sec == DEFAULT_PER_UNIT_BUDGET_SEC * len(feats) * 1


def test_telemetry_carries_concurrency_block(tmp_path: Path) -> None:
    """The :meth:`EnrichmentResult.telemetry` payload contains the
    Sprint F concurrency block.
    """
    feats = [_new_feature("f0", ["app/f0.tsx"])]
    with StageLogger(tmp_path, 6, "fe_tel") as log:
        res = run_stage_6_4(
            _ctx(tmp_path), feats, log,
            linkers=[_CannedLinker()],
            max_workers=2, wall_budget_sec=5.0,
        )
    tel = res.telemetry()
    assert "concurrency" in tel
    assert tel["concurrency"]["max_workers"] == 2
    assert tel["concurrency"]["budget_sec"] == 5.0
    assert tel["concurrency"]["budget_exceeded"] is False
