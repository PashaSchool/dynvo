"""Determinism regression tests for the parallel-enrichment budget guard.

FOUNDATION-AUDIT #3 (D-CLUSTER) — the 3rd nondeterminism class of the anchor
arc: Stages 6.3 / 6.4 / 6.6 run per-feature work in a ``ThreadPoolExecutor``
and, when a WALL-CLOCK budget fires mid-loop, cancel *whichever* futures happen
not to be ``done()`` yet. WHICH features become ``budget_skipped`` therefore
depends on OS-scheduler thread-completion timing — two runs on the SAME machine
emit different skip-sets. Byte-identity double-runs on fast machines only mask
it because the budget never fires there.

These tests force the degradation path under heavy thread contention and assert
the ``budget_skipped`` SET is a pure function of input (identical across many
runs). Before the fix they FAIL (the set wobbles run to run); after the fix
they PASS (the cut is computed deterministically from budget / per-unit / n and
the skipped features are always a canonical suffix of the input order).

The skip-set is read from OBSERVABLE per-feature side-effects (not a new field)
so the exact same test body exercises both the pre-fix and post-fix engine.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.framework_linkers.base import FrameworkLink
from faultline.models.types import Feature, FlowSymbolAttribution
from faultline.pipeline_v2.run_logger import StageLogger

# ── shared knobs ─────────────────────────────────────────────────────────────
# Enough queued work + workers that the "freed-worker-grabs-next vs main-thread-
# cancels-it" race has a wide boundary region; enough repeats that a wobbling
# skip-set is overwhelmingly likely to reveal itself.
N_FEATURES = 32
MAX_WORKERS = 8
N_RUNS = 40
# A tiny wall budget: fires on the very first completion so the cancellation
# path is always taken. Post-fix it deterministically maps to keep_count=0.
TINY_BUDGET_SEC = 0.004


def _jitter() -> None:
    """A short, randomised yield so thread completion order genuinely varies
    from run to run (the substrate the bug feeds on). Light + variable so
    workers cycle many times before the budget fires — maximising the racy
    boundary region between 'freed worker grabs next' and 'main thread cancels
    it'."""
    time.sleep(random.uniform(0.0002, 0.004))


class _RanRecorder:
    """Thread-safe record of which feature NAMES a worker actually executed.

    A feature runs ⟺ its future was NOT cancelled by the budget guard, so the
    recorded set is exactly the *processed* set and its complement is the
    *budget_skipped* set — the quantity whose determinism we are testing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.names: set[str] = set()

    def mark(self, name: str) -> None:
        with self._lock:
            self.names.add(name)

    def skip_set(self, feats: list[Feature]) -> frozenset[int]:
        with self._lock:
            ran = set(self.names)
        return frozenset(i for i, f in enumerate(feats) if f.name not in ran)


def _assert_single_skip_set(skip_sets: list[frozenset[int]], stage: str) -> None:
    distinct = set(skip_sets)
    assert len(distinct) == 1, (
        f"{stage}: budget_skipped set is NOT deterministic under thread "
        f"contention — saw {len(distinct)} distinct skip-sets across "
        f"{len(skip_sets)} runs. Sample: "
        f"{sorted(sorted(s) for s in list(distinct)[:4])}"
    )


# ── Stage 6.3 ────────────────────────────────────────────────────────────────


def _ctx_63(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo_path=repo, tracked_files=tuple(), run_dir=None,
        stack="next-app-router", monorepo=False, workspaces=[],
    )


def _feat(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name, paths=list(paths), authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=80.0,
        layer="developer",
    )


def test_stage_6_3_budget_skip_set_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    import faultline.pipeline_v2.stage_6_3_import_tree as mod

    original = mod._compute_one_feature
    rec = _RanRecorder()

    def slow(feature, feature_index, **kwargs):  # noqa: ANN001, ANN003
        rec.mark(feature.name)
        _jitter()
        return original(feature, feature_index, **kwargs)

    monkeypatch.setattr(mod, "_compute_one_feature", slow)

    skip_sets: list[frozenset[int]] = []
    for _ in range(N_RUNS):
        rec.names.clear()
        feats = [_feat(f"f{i}", [f"app/f{i}/page.tsx"]) for i in range(N_FEATURES)]
        mod.enrich_with_import_tree(
            _ctx_63(tmp_path), feats,
            max_workers=MAX_WORKERS, wall_budget_sec=TINY_BUDGET_SEC,
        )
        skip_sets.append(rec.skip_set(feats))

    _assert_single_skip_set(skip_sets, "stage_6_3")


# ── Stage 6.4 ────────────────────────────────────────────────────────────────


def _ctx_64(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo_path=tmp_path, tracked_files=tuple(), run_dir=None,
        stack="next-app-router", audited_stack="next-app-router",
        secondary_stacks=(), monorepo=False, workspaces=[],
    )


class _SlowLinker:
    """Light, jittery linker that records which features actually ran."""

    name = "slow"

    def __init__(self, recorder: _RanRecorder | None = None) -> None:
        self._rec = recorder

    def is_active(self, ctx) -> bool:  # noqa: ANN001
        return True

    def link_for_feature(self, feature, ctx, log) -> list[FrameworkLink]:  # noqa: ANN001
        if self._rec is not None:
            self._rec.mark(feature.name)
        _jitter()
        return [
            FrameworkLink(
                source_file=feature.paths[0] if feature.paths else "<unknown>",
                source_symbol="<module>", source_line=1,
                target_file=f"api/{feature.name}/route.ts",
                target_symbol="POST", target_line_start=1, target_line_end=3,
                linker=self.name, link_kind="http-route",
                confidence=1.0, reason="slow-canned",
            ),
        ]


def test_stage_6_4_budget_skip_set_is_deterministic(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_4_framework_enrich import run_stage_6_4

    rec = _RanRecorder()
    skip_sets: list[frozenset[int]] = []
    for run_i in range(N_RUNS):
        rec.names.clear()
        feats = [_feat(f"f{i}", [f"app/f{i}.tsx"]) for i in range(N_FEATURES)]
        with StageLogger(tmp_path / f"r{run_i}", 6, "fe_det") as log:
            run_stage_6_4(
                _ctx_64(tmp_path), feats, log,
                linkers=[_SlowLinker(rec)],
                max_workers=MAX_WORKERS, wall_budget_sec=TINY_BUDGET_SEC,
            )
        skip_sets.append(rec.skip_set(feats))

    _assert_single_skip_set(skip_sets, "stage_6_4")


# ── Stage 6.6 ────────────────────────────────────────────────────────────────

from faultline.pipeline_v2 import stage_6_6_branch_slicer as _bs  # noqa: E402

TS_AVAILABLE = _bs.TREE_SITTER_AVAILABLE and _bs.is_active()
requires_ts = pytest.mark.skipif(
    not TS_AVAILABLE, reason="tree-sitter language bindings not installed"
)


def _slicy(symbol: str) -> str:
    return (
        f"export function {symbol}(x: number): number {{\n"
        f"  if (x > 0) {{ return x + 1; }}\n"
        f"  const y = x === 0 ? 0 : -1;\n"
        f"  return y;\n"
        f"}}\n"
    )


def _build_6_6(tmp_path: Path, n: int) -> tuple[SimpleNamespace, list[Feature]]:
    feats: list[Feature] = []
    for i in range(n):
        rel = f"src/file{i}.ts"
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_slicy(f"fn{i}"), encoding="utf-8")
        f = _feat(f"feat{i}", [rel])
        f.symbol_attributions = [
            FlowSymbolAttribution(
                file=rel, symbol=f"fn{i}", line_start=1, line_end=5, role="entry",
            )
        ]
        feats.append(f)
    return _ctx_64(tmp_path), feats


@requires_ts
def test_stage_6_6_budget_skip_set_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    # Replace the (heavy) tree-sitter worker with a light, jittery stub so
    # workers cycle and the cancel-race is exercised the same way as 6.3/6.4;
    # the stub records which features actually ran.
    rec = _RanRecorder()

    def slow(feature, feature_index, **kwargs):  # noqa: ANN001, ANN003
        rec.mark(feature.name)
        _jitter()
        return _bs._PerFeatureBranchResult(
            feature_index=feature_index, feat_new=[], flow_new=[],
        )

    monkeypatch.setattr(_bs, "_slice_for_feature", slow)

    ctx, base_feats = _build_6_6(tmp_path, N_FEATURES)
    skip_sets: list[frozenset[int]] = []
    for run_i in range(N_RUNS):
        rec.names.clear()
        feats = [_feat(f.name, list(f.paths)) for f in base_feats]
        for f in feats:
            f.symbol_attributions = [
                FlowSymbolAttribution(
                    file=f.paths[0], symbol=f.name.replace("feat", "fn"),
                    line_start=1, line_end=5, role="entry",
                )
            ]
        with StageLogger(tmp_path / f"r{run_i}", 6, "bs_det") as log:
            _bs.run_stage_6_6(
                ctx, feats, log,
                max_workers=MAX_WORKERS, wall_budget_sec=TINY_BUDGET_SEC,
            )
        skip_sets.append(rec.skip_set(feats))

    _assert_single_skip_set(skip_sets, "stage_6_6")


# ── canonical-suffix correctness (post-fix behaviour contract) ───────────────
# These assert the SHAPE the fix guarantees: a middle-sized budget keeps a
# canonical PREFIX and skips the exact complementary SUFFIX — a pure function of
# input. Deterministic post-fix; documents the new seconds→count contract.


def test_stage_6_3_skip_is_canonical_suffix(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        DEFAULT_PER_FEATURE_BUDGET_SEC, enrich_with_import_tree,
    )

    n = 10
    keep = 4
    budget = DEFAULT_PER_FEATURE_BUDGET_SEC * keep
    feats = [_feat(f"f{i}", [f"app/f{i}/page.tsx"]) for i in range(n)]
    res = enrich_with_import_tree(
        _ctx_63(tmp_path), feats, max_workers=MAX_WORKERS, wall_budget_sec=budget,
    )
    skip = sorted(
        i for i, e in enumerate(res.per_feature)
        if e.source_kind == "budget_skipped"
    )
    assert skip == list(range(keep, n))
    assert res.budget_exceeded is True
    assert res.features_budget_skipped == n - keep


def test_stage_6_4_skip_is_canonical_suffix(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_4_framework_enrich import (
        DEFAULT_PER_UNIT_BUDGET_SEC, run_stage_6_4,
    )

    n = 10
    keep = 4  # 1 active linker → work-units == features
    budget = DEFAULT_PER_UNIT_BUDGET_SEC * keep
    feats = [_feat(f"f{i}", [f"app/f{i}.tsx"]) for i in range(n)]
    with StageLogger(tmp_path, 6, "fe_suffix") as log:
        res = run_stage_6_4(
            _ctx_64(tmp_path), feats, log,
            linkers=[_SlowLinker()], max_workers=MAX_WORKERS, wall_budget_sec=budget,
        )
    skip = sorted(i for i, f in enumerate(feats) if not f.symbol_attributions)
    assert skip == list(range(keep, n))
    assert res.budget_exceeded is True
    assert res.features_budget_skipped == n - keep


@requires_ts
def test_stage_6_6_skip_is_canonical_suffix(tmp_path: Path, monkeypatch) -> None:
    # Detect the kept/skipped boundary via a recorder (6.6 stores branch
    # slices out of band, so an attribution-length side-effect is unreliable).
    n = 10
    keep = 4
    budget = _bs.DEFAULT_PER_FEATURE_BUDGET_SEC * keep
    rec = _RanRecorder()

    def rec_slice(feature, feature_index, **kwargs):  # noqa: ANN001, ANN003
        rec.mark(feature.name)
        return _bs._PerFeatureBranchResult(
            feature_index=feature_index, feat_new=[], flow_new=[],
        )

    monkeypatch.setattr(_bs, "_slice_for_feature", rec_slice)
    _bs.reset_caches()
    ctx, feats = _build_6_6(tmp_path, n)
    with StageLogger(tmp_path, 6, "bs_suffix") as log:
        res = _bs.run_stage_6_6(
            ctx, feats, log, max_workers=MAX_WORKERS, wall_budget_sec=budget,
        )
    assert rec.skip_set(feats) == frozenset(range(keep, n))
    assert res.budget_exceeded is True
    assert res.features_budget_skipped == n - keep


# ── content-keyed selection (robust to a nondeterministic INPUT order) ───────
# The kept set must be a pure function of feature CONTENT, not of the input
# list order. We SHUFFLE the input each run; a position-based cut would keep a
# different set every time, a content-keyed cut keeps the canonically-lowest
# names regardless. Names f0..f10 are chosen so lexical order (f0,f1,f10,f2,…)
# differs from index order — proving selection is not merely "first N".

_ROBUST_N = 11
_ROBUST_KEEP = 5
_ROBUST_NAMES = [f"f{i}" for i in range(_ROBUST_N)]
_ROBUST_EXPECTED = frozenset(sorted(_ROBUST_NAMES)[:_ROBUST_KEEP])  # 5 lexicographically-lowest


def test_stage_6_3_kept_set_is_content_keyed(tmp_path: Path, monkeypatch) -> None:
    import faultline.pipeline_v2.stage_6_3_import_tree as mod

    original = mod._compute_one_feature
    rec = _RanRecorder()

    def slow(feature, feature_index, **kwargs):  # noqa: ANN001, ANN003
        rec.mark(feature.name)
        return original(feature, feature_index, **kwargs)

    monkeypatch.setattr(mod, "_compute_one_feature", slow)
    budget = mod.DEFAULT_PER_FEATURE_BUDGET_SEC * _ROBUST_KEEP  # keep 5
    seen: list[frozenset[str]] = []
    for _ in range(12):
        rec.names.clear()
        feats = [_feat(nm, [f"app/{nm}/page.tsx"]) for nm in _ROBUST_NAMES]
        random.shuffle(feats)  # nondeterministic INPUT order
        mod.enrich_with_import_tree(
            _ctx_63(tmp_path), feats, max_workers=MAX_WORKERS, wall_budget_sec=budget,
        )
        seen.append(frozenset(rec.names))
    assert len(set(seen)) == 1, f"stage_6_3 kept set varied with input order: {seen[:3]}"
    assert seen[0] == _ROBUST_EXPECTED


def test_stage_6_4_kept_set_is_content_keyed(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_4_framework_enrich import (
        DEFAULT_PER_UNIT_BUDGET_SEC, run_stage_6_4,
    )

    rec = _RanRecorder()
    budget = DEFAULT_PER_UNIT_BUDGET_SEC * _ROBUST_KEEP  # 1 linker → keep 5 features
    seen: list[frozenset[str]] = []
    for run_i in range(12):
        rec.names.clear()
        feats = [_feat(nm, [f"app/{nm}.tsx"]) for nm in _ROBUST_NAMES]
        random.shuffle(feats)
        with StageLogger(tmp_path / f"r{run_i}", 6, "fe_ck") as log:
            run_stage_6_4(
                _ctx_64(tmp_path), feats, log,
                linkers=[_SlowLinker(rec)], max_workers=MAX_WORKERS, wall_budget_sec=budget,
            )
        seen.append(frozenset(rec.names))
    assert len(set(seen)) == 1, f"stage_6_4 kept set varied with input order: {seen[:3]}"
    assert seen[0] == _ROBUST_EXPECTED


@requires_ts
def test_stage_6_6_kept_set_is_content_keyed(tmp_path: Path, monkeypatch) -> None:
    rec = _RanRecorder()

    def rec_slice(feature, feature_index, **kwargs):  # noqa: ANN001, ANN003
        rec.mark(feature.name)
        return _bs._PerFeatureBranchResult(
            feature_index=feature_index, feat_new=[], flow_new=[],
        )

    monkeypatch.setattr(_bs, "_slice_for_feature", rec_slice)
    budget = _bs.DEFAULT_PER_FEATURE_BUDGET_SEC * _ROBUST_KEEP  # keep 5
    seen: list[frozenset[str]] = []
    for run_i in range(12):
        rec.names.clear()
        _bs.reset_caches()
        feats = [_feat(nm, [f"src/{nm}.ts"]) for nm in _ROBUST_NAMES]
        for f in feats:
            f.symbol_attributions = [
                FlowSymbolAttribution(
                    file=f.paths[0], symbol=f.name, line_start=1, line_end=5, role="entry",
                )
            ]
        random.shuffle(feats)
        with StageLogger(tmp_path / f"r{run_i}", 6, "bs_ck") as log:
            _bs.run_stage_6_6(
                _ctx_64(tmp_path), feats, log,
                max_workers=MAX_WORKERS, wall_budget_sec=budget,
            )
        seen.append(frozenset(rec.names))
    assert len(set(seen)) == 1, f"stage_6_6 kept set varied with input order: {seen[:3]}"
    assert seen[0] == _ROBUST_EXPECTED
