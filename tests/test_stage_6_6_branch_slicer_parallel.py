"""Tests for Sprint F (2026-05-20) parallelism + budget in Stage 6.6."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.models.types import Feature, FlowSymbolAttribution
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2 import stage_6_6_branch_slicer as branch_slicer
from faultline.pipeline_v2.stage_6_6_branch_slicer import (
    DEFAULT_MAX_WORKERS,
    is_active,
    reset_caches,
    run_stage_6_6,
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


def _write(tmp_path: Path, rel: str, src: str) -> None:
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src, encoding="utf-8")


def _slicy_source(symbol: str) -> str:
    """A source body with one ``if`` and one ``ternary`` so the slicer
    has work to do.
    """
    return f"""\
export function {symbol}(x: number): number {{
  if (x > 0) {{
    return x + 1;
  }}
  const y = x === 0 ? 0 : -1;
  return y;
}}
"""


def _build_n_features(
    tmp_path: Path, n: int,
) -> tuple[SimpleNamespace, list[Feature]]:
    """Make N features, each with one symbol-attribution pointing to a
    file containing branchable conditionals.
    """
    feats: list[Feature] = []
    for i in range(n):
        rel = f"src/file{i}.ts"
        _write(tmp_path, rel, _slicy_source(f"fn{i}"))
        line_count = (tmp_path / rel).read_text().count("\n") + 1
        attr = FlowSymbolAttribution(
            file=rel, symbol=f"fn{i}",
            line_start=1, line_end=line_count,
            role="entry",
        )
        f = Feature(
            name=f"feat{i}", paths=[rel],
            authors=[], total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=datetime.now(timezone.utc),
            health_score=80.0, layer="developer",
        )
        f.symbol_attributions = [attr]
        feats.append(f)
    return _ctx(tmp_path), feats


TS_AVAILABLE = branch_slicer.TREE_SITTER_AVAILABLE and is_active()
requires_ts = pytest.mark.skipif(
    not TS_AVAILABLE,
    reason="tree-sitter language bindings not installed",
)


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_caches()
    yield
    reset_caches()


# ── Tests ─────────────────────────────────────────────────────────────────


@requires_ts
def test_parallel_emits_same_branches_as_serial(tmp_path: Path) -> None:
    """4 workers on independent files emits the same branches in total."""
    ctx_s, feats_s = _build_n_features(tmp_path / "serial", n=4)
    ctx_p, feats_p = _build_n_features(tmp_path / "parallel", n=4)

    with StageLogger(tmp_path / "s_logs", 6, "bs_serial") as log_s:
        res_s = run_stage_6_6(
            ctx_s, feats_s, log_s,
            max_workers=1, wall_budget_sec=0,
        )
    with StageLogger(tmp_path / "p_logs", 6, "bs_parallel") as log_p:
        res_p = run_stage_6_6(
            ctx_p, feats_p, log_p,
            max_workers=4, wall_budget_sec=0,
        )

    assert res_s.symbols_analyzed == res_p.symbols_analyzed
    assert res_s.branches_emitted == res_p.branches_emitted
    assert res_s.branch_kinds == res_p.branch_kinds
    # Per-feature attribution count match (compare counts not exact
    # objects — symbol order can differ between threads).
    for fs, fp in zip(feats_s, feats_p):
        assert len(fs.symbol_attributions) == len(fp.symbol_attributions)
    assert res_p.max_workers == 4
    assert res_s.max_workers == 1


@requires_ts
def test_budget_exceeded_triggers_skip(tmp_path: Path) -> None:
    """A tight wall-clock budget marks remaining features ``budget_skipped``."""
    ctx, feats = _build_n_features(tmp_path, n=6)
    with StageLogger(tmp_path, 6, "bs_budget") as log:
        res = run_stage_6_6(
            ctx, feats, log,
            max_workers=1, wall_budget_sec=0.001,
        )
    assert res.budget_exceeded is True
    assert res.features_budget_skipped >= 1


@requires_ts
def test_telemetry_carries_concurrency_block(tmp_path: Path) -> None:
    ctx, feats = _build_n_features(tmp_path, n=2)
    with StageLogger(tmp_path, 6, "bs_tel") as log:
        # A clearly-generous budget: under the deterministic seconds→count
        # semantics it affords floor(1000 / 6.0) = 166 features >> 2, so
        # nothing is skipped. (The value is arbitrary for this telemetry
        # check; the old wall-clock semantics read it as "plenty of wall".)
        res = run_stage_6_6(
            ctx, feats, log,
            max_workers=2, wall_budget_sec=1000.0,
        )
    tel = res.telemetry()
    assert tel["active"] is True
    assert "concurrency" in tel
    assert tel["concurrency"]["max_workers"] == 2
    assert tel["concurrency"]["budget_sec"] == 1000.0
    assert tel["concurrency"]["budget_exceeded"] is False


@requires_ts
def test_thread_local_parser_cache_works_across_threads(tmp_path: Path) -> None:
    """The thread-local parser cache must build a parser per worker
    without crashing. Smoke test: 8 features, 4 workers — every feature
    yields at least one slice.
    """
    ctx, feats = _build_n_features(tmp_path, n=8)
    with StageLogger(tmp_path, 6, "bs_tls") as log:
        res = run_stage_6_6(
            ctx, feats, log,
            max_workers=4, wall_budget_sec=0,
        )
    assert res.active is True
    assert res.parse_failures == 0
    # Each feature emits 1 ``if`` + 1 ``ternary`` = 2 branches.
    assert res.branches_emitted >= 8
