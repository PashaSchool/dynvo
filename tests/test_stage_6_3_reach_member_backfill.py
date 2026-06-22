"""Tests for the Stage-6.3 reach member-file backfill (2026-06).

Stage 6.3 grows ``feature.paths`` by the reverse-import reach set but
historically wrote nothing to ``feature.member_files`` — breaking the
:class:`faultline.models.types.MemberFile` invariant (``member_files`` is
the complete provenance ledger; a file is ``primary=True`` on exactly the
feature whose ``paths`` carries it). On package-anchor features (NestJS
``ai`` / ``billing`` / ``i18n`` dependency anchors that start on a single
directory path) this produced a 1-member-file feature with a 95-file
``paths`` projection, which the blob / coverage evaluators mis-measure.

These tests pin:
  * the invariant is restored (every reach file gets a member-file);
  * the scale-invariant fan-in cap classifies genuine distinct
    membership as owned (``role="closure"`` / ``primary``) and
    cross-cutting shared reach as ``role="shared"`` / non-primary
    (so the owned-blob metric cannot explode on barrel-heavy stacks);
  * the behaviour holds at tiny / medium / large scale (no magic
    numbers);
  * the env escape hatch disables it cleanly (byte-compatible legacy);
  * existing member-file records (Stage 2.6 anchors) are not duplicated.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_6_3_import_tree import (
    _backfill_reach_member_files,
    _reach_nearest_rank,
    enrich_with_import_tree,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path) -> SimpleNamespace:
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            try:
                tracked.append(f.relative_to(repo).as_posix())
            except ValueError:
                continue
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        run_dir=None,
        stack="nestjs",
        monorepo=False,
        workspaces=[],
    )


def _feature(
    name: str,
    paths: Iterable[str],
    *,
    description: str | None = None,
    member_files: list[MemberFile] | None = None,
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
        flows=[],
        layer="developer",
        member_files=member_files or [],
    )


def _mf_index(feature: Feature) -> dict[str, MemberFile]:
    return {m.path: m for m in feature.member_files}


# ── Unit tests on the classifier (synthetic, no filesystem) ──────────────


def test_single_claimant_reach_is_owned() -> None:
    """A reach file claimed by ONE feature is genuine distinct membership
    → ``role="closure"`` / ``primary=True`` (the NestJS case)."""
    feats = [
        _feature("ai", ["server"]),
        _feature("billing", ["server"]),
    ]
    reached = {
        0: ["server/ai/a.ts", "server/ai/b.ts"],
        1: ["server/billing/c.ts"],
    }
    tele = _backfill_reach_member_files(feats, reached)
    assert tele["reach_owned"] == 3
    assert tele["reach_shared"] == 0
    for f in feats:
        idx = _mf_index(f)
        for p in reached[feats.index(f)]:
            assert idx[p].role == "closure"
            assert idx[p].primary is True


def test_high_fan_in_reach_is_shared_not_owned() -> None:
    """A file reached by MANY features (barrel / hub re-export) is shared
    infrastructure → ``role="shared"`` / ``primary=False`` on every
    claimant, so it cannot inflate any single feature's OWNED share."""
    # 6 features all reach the same hub file; each also reaches one
    # private file. With 6 claimants the hub is far above the floor.
    feats = [_feature(f"f{i}", ["src"]) for i in range(6)]
    reached = {i: [f"src/f{i}/own.ts", "src/shared/hub.ts"] for i in range(6)}
    tele = _backfill_reach_member_files(feats, reached)
    # Each private file owned (1 claimant); the hub shared (6 claimants).
    assert tele["reach_owned"] == 6
    assert tele["reach_shared"] == 6  # hub recorded on all 6 as shared
    for f in feats:
        idx = _mf_index(f)
        own = next(p for p in idx if p.endswith("own.ts"))
        assert idx[own].role == "closure" and idx[own].primary is True
        assert idx["src/shared/hub.ts"].role == "shared"
        assert idx["src/shared/hub.ts"].primary is False


def test_owned_blob_does_not_explode_when_all_share_one_file() -> None:
    """The owned-blob safety property: when every feature's reach is the
    SAME shared set, no feature OWNS those files (all non-primary), so the
    owned union stays empty — the metric cannot blob."""
    n = 8
    shared = [f"src/shared/s{j}.ts" for j in range(5)]
    feats = [_feature(f"f{i}", ["src"]) for i in range(n)]
    # Every feature reaches the SAME 5 shared files (pure over-inclusion).
    reached = {i: list(shared) for i in range(n)}
    _backfill_reach_member_files(feats, reached)
    # Mirror cold_eval._owned_file_set: owned = primary OR role in
    # {anchor, owner}. Over-shared reach must yield ZERO owned files.
    owned_union: set[str] = set()
    for f in feats:
        for m in f.member_files:
            if m.primary or m.role in ("anchor", "owner"):
                owned_union.add(m.path)
    assert owned_union == set(), "shared reach must not become owned (no blob)"


def test_three_claimants_shared_even_below_high_percentile() -> None:
    """REGRESSION (2026-06, the leaked JS case). A file reached by exactly 3
    features must be SHARED even when the repo's 90th-percentile reach fan-in
    is much higher. The earlier ``max(floor, p90)`` left files reached by
    3..p90-1 features OWNED and over-claimed — unsend ``webhook``: 40/73 owned
    files reached by >=3 features pushed owned_max 0.12 -> 0.181 across the
    gate, while NestJS (distinct reach, fan-in ~1) was unaffected. The floor
    IS the threshold; the percentile must not relax it."""
    # Skewed distribution: 3 mega-hubs reached by all 20 features push p90
    # high (~20). A moderately-shared file reached by EXACTLY 3 must not ride
    # that lenient percentile into OWNED.
    feats = [_feature(f"f{i}", ["src"]) for i in range(20)]
    reached: dict[int, list[str]] = {}
    for i in range(20):
        reached[i] = [f"src/f{i}/own.ts", "src/hub/a.ts", "src/hub/b.ts", "src/hub/c.ts"]
    for i in range(3):  # exactly 3 claimants on this one file
        reached[i].append("src/mod/shared3.ts")
    _backfill_reach_member_files(feats, reached)
    for i in range(3):
        idx = _mf_index(feats[i])
        assert idx["src/mod/shared3.ts"].role == "shared"
        assert idx["src/mod/shared3.ts"].primary is False
    # Private files (fan-in 1) stay owned — the NestJS-win property.
    idx0 = _mf_index(feats[0])
    assert idx0["src/f0/own.ts"].role == "closure"
    assert idx0["src/f0/own.ts"].primary is True


def test_threshold_is_scale_invariant_floor() -> None:
    """The fan-in floor (3 claimants) holds regardless of repo size: a
    pairwise (2-feature) share is still owned by the higher-priority
    claimant, not demoted to shared, at tiny AND large scale."""
    for n_features in (3, 30, 300):
        feats = [_feature(f"f{i}", ["src"]) for i in range(n_features)]
        reached: dict[int, list[str]] = {}
        # Each feature reaches a unique private file …
        for i in range(n_features):
            reached[i] = [f"src/f{i}/own.ts"]
        # … and exactly ONE file is shared by just 2 features (pairwise).
        reached[0].append("src/pair.ts")
        reached[1].append("src/pair.ts")
        _backfill_reach_member_files(feats, reached)
        # Pairwise share (2 < floor 3) → owned by both as primary closure.
        for idx in (0, 1):
            m = _mf_index(feats[idx])["src/pair.ts"]
            assert m.role == "closure", f"n={n_features}"
            assert m.primary is True, f"n={n_features}"


def test_nearest_rank_helper_matches_stage_2_6() -> None:
    """The percentile helper is the same nearest-rank contract Stage 2.6
    uses (scale-invariant; P90 of a fan-in distribution)."""
    assert _reach_nearest_rank([], 0.9) == 0
    assert _reach_nearest_rank([1], 0.9) == 1
    # 10 values 1..10 → P90 nearest-rank = 9th value.
    assert _reach_nearest_rank(list(range(1, 11)), 0.90) == 9
    assert _reach_nearest_rank([5, 5, 5], 0.90) == 5


def test_existing_member_files_not_duplicated() -> None:
    """A reach file already in member_files (e.g. a Stage-2.6 anchor) is
    skipped — backfill only fills the gap, never double-records."""
    pre = MemberFile(
        path="server/ai/a.ts", role="anchor", confidence=1.0,
        evidence="stage-2 anchor", primary=True,
    )
    f = _feature("ai", ["server", "server/ai/a.ts"], member_files=[pre])
    reached = {0: ["server/ai/a.ts", "server/ai/b.ts"]}
    tele = _backfill_reach_member_files([f], reached)
    # a.ts already recorded → only b.ts newly owned.
    assert tele["reach_owned"] == 1
    a_records = [m for m in f.member_files if m.path == "server/ai/a.ts"]
    assert len(a_records) == 1
    assert a_records[0].role == "anchor"  # untouched


def test_empty_reach_is_noop() -> None:
    f = _feature("x", ["src"])
    tele = _backfill_reach_member_files([f], {})
    assert tele["reach_owned"] == 0 and tele["reach_shared"] == 0
    assert f.member_files == []


# ── End-to-end through enrich_with_import_tree (real BFS) ────────────────


def test_e2e_package_anchor_backfills_member_files(tmp_path: Path) -> None:
    """The NestJS-shape regression: a package-anchor feature seeded from a
    single directory path reaches concrete module files and now records
    them in member_files (was: paths grew, member_files stayed empty)."""
    _w(tmp_path / "server/billing.ts", """\
import Stripe from "stripe";
export function charge(n: number) { return new Stripe("k").charges.create({amount: n}); }
""")
    _w(tmp_path / "server/checkout.ts", """\
import { loadStripe } from "stripe";
export async function start() { return await loadStripe("pk"); }
""")
    _w(tmp_path / "server/unrelated.ts", "export const helper = () => 1;\n")
    ctx = _ctx(tmp_path)
    feature = _feature(
        "billing",
        ["server"],
        description="[package] package anchor 'billing' from deps ['stripe']",
    )
    res = enrich_with_import_tree(ctx, [feature])
    paths = set(feature.paths)
    assert "server/billing.ts" in paths
    assert "server/checkout.ts" in paths
    # The fix: member_files now covers the reached files as owned closure.
    idx = _mf_index(feature)
    assert idx["server/billing.ts"].role == "closure"
    assert idx["server/billing.ts"].primary is True
    assert idx["server/checkout.ts"].primary is True
    assert res.reach_member_backfill["reach_owned"] >= 2

    # INVARIANT restored: every concrete file in paths is in member_files.
    concrete_paths = {p for p in paths if p in set(ctx.tracked_files)}
    member_paths = set(idx)
    assert concrete_paths <= member_paths, (
        f"paths not covered by member_files: {concrete_paths - member_paths}"
    )


def test_e2e_env_disable_is_byte_compatible(tmp_path: Path) -> None:
    """``FAULTLINE_STAGE_6_3_MEMBER_BACKFILL=0`` reverts to the legacy
    behaviour: paths grow, member_files untouched."""
    _w(tmp_path / "server/billing.ts", """\
import Stripe from "stripe";
export function charge() { return new Stripe("k"); }
""")
    ctx = _ctx(tmp_path)
    feature = _feature(
        "billing", ["server"],
        description="[package] package anchor 'billing' from deps ['stripe']",
    )
    prev = os.environ.get("FAULTLINE_STAGE_6_3_MEMBER_BACKFILL")
    os.environ["FAULTLINE_STAGE_6_3_MEMBER_BACKFILL"] = "0"
    try:
        res = enrich_with_import_tree(ctx, [feature])
    finally:
        if prev is None:
            os.environ.pop("FAULTLINE_STAGE_6_3_MEMBER_BACKFILL", None)
        else:
            os.environ["FAULTLINE_STAGE_6_3_MEMBER_BACKFILL"] = prev
    assert "server/billing.ts" in set(feature.paths)
    assert feature.member_files == []
    assert res.reach_member_backfill == {}
