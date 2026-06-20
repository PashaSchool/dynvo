"""Tests for Stage 8.8 — shared-member enrichment of the de-sink residual.

Builds a tiny on-disk TS project (Stage 8.8 resolves real imports) with one
specific feature importing a shared file held by a workspace anchor. Verifies
the shared file is attached as a role="shared" member_file on the importer,
that `paths` are never touched, the no-importer case stays residual, and the
env toggle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_8_shared_members import enrich_shared_members

_WS = "[package] workspace anchor 'frontend' from monorepo package 'frontend'"
_ROUTE = "[route] route convention slug 'auth' derived from 1 routing file(s)"


def _feat(name, paths, *, description=None, layer="developer"):
    return Feature(
        name=name, description=description, paths=list(paths), authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=100.0, layer=layer,
    )


def _write_project(root):
    """A specific feature file imports one shared component + one shared hook;
    a second shared component is imported by nobody (stays residual)."""
    (root / "src/routes").mkdir(parents=True)
    (root / "src/components").mkdir(parents=True)
    (root / "src/hooks").mkdir(parents=True)
    (root / "src/routes/auth.ts").write_text(
        "import { Button } from '../components/Button';\n"
        "import { useAuth } from '../hooks/useAuth';\n"
        "export const route = () => Button(useAuth());\n"
    )
    (root / "src/components/Button.tsx").write_text("export const Button = () => null;\n")
    (root / "src/hooks/useAuth.ts").write_text("export const useAuth = () => 1;\n")
    (root / "src/components/Orphan.tsx").write_text("export const Orphan = () => null;\n")


def _ctx(root):
    tracked = [
        "src/routes/auth.ts", "src/components/Button.tsx",
        "src/hooks/useAuth.ts", "src/components/Orphan.tsx",
    ]
    return SimpleNamespace(repo_path=str(root), tracked_files=tracked)


def _fixture(tmp_path):
    _write_project(tmp_path)
    anchor = _feat("frontend", [
        "src/components/Button.tsx", "src/hooks/useAuth.ts",
        "src/components/Orphan.tsx",
    ], description=_WS)
    auth = _feat("auth", ["src/routes/auth.ts"], description=_ROUTE)
    return _ctx(tmp_path), [anchor, auth], anchor, auth


def test_attaches_imported_residual_as_shared_member(tmp_path):
    ctx, feats, anchor, auth = _fixture(tmp_path)
    res = enrich_shared_members(ctx, feats)
    shared = {m.path: m for m in auth.member_files}
    # the two files auth imports are attached as role="shared"
    assert "src/components/Button.tsx" in shared
    assert "src/hooks/useAuth.ts" in shared
    assert all(m.role == "shared" and not m.primary for m in shared.values())
    # the un-imported residual file is NOT attached anywhere
    assert "src/components/Orphan.tsx" not in shared
    assert res.edges == 2
    assert res.features_enriched == 1
    assert res.residual_attached == 2


def test_never_touches_paths(tmp_path):
    ctx, feats, anchor, auth = _fixture(tmp_path)
    anchor_paths_before = list(anchor.paths)
    auth_paths_before = list(auth.paths)
    enrich_shared_members(ctx, feats)
    # paths are the exclusive primary surface — must be byte-stable
    assert anchor.paths == anchor_paths_before
    assert auth.paths == auth_paths_before


def test_no_anchor_is_noop(tmp_path):
    _write_project(tmp_path)
    auth = _feat("auth", ["src/routes/auth.ts"], description=_ROUTE)
    res = enrich_shared_members(_ctx(tmp_path), [auth])
    assert res.residual_files == 0
    assert res.edges == 0
    assert auth.member_files == []


def test_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_8_8_SHARED_MEMBERS", "0")
    ctx, feats, anchor, auth = _fixture(tmp_path)
    res = enrich_shared_members(ctx, feats)
    assert res.enabled is False
    assert auth.member_files == []


def test_telemetry_shape(tmp_path):
    ctx, feats, *_ = _fixture(tmp_path)
    tele = enrich_shared_members(ctx, feats).as_telemetry()
    assert set(tele) == {
        "enabled", "residual_files", "residual_attached", "edges",
        "features_enriched", "coverage_pct", "fanout_cap", "conduit_files",
        "sample",
    }
    assert 0.0 <= tele["coverage_pct"] <= 1.0


# ── Barrel / hub conduit guard ───────────────────────────────────────────

from faultline.pipeline_v2.stage_8_8_shared_members import (  # noqa: E402
    _FANOUT_FLOOR,
    _fanout_cap,
    _nearest_rank,
)


def test_fanout_cap_is_scale_invariant():
    """The cap is max(floor, P90, ceil(0.10*n_features)) at every scale."""
    # Tiny repo, no conduit: floor dominates (P90 and 10%-floor both small).
    assert _fanout_cap([1, 1, 2, 1], n_features=4) == _FANOUT_FLOOR
    # Medium repo: 10%-frac floor dominates a flat low-fan-in distribution.
    #   n_features=200 → ceil(0.10*200)=20; P90 of all-1s is 1; floor=3 → 20.
    assert _fanout_cap([1] * 50, n_features=200) == 20
    # Large repo WITH a barrel: P90 catches the outlier hub.
    #   distribution: 90 files @1 importer, 10 files @150 (the barrel cluster)
    #   P90 (nearest-rank, 100 values) = the 90th value = 1 ... so frac floor
    #   or P90 — here a genuine high-fan-in tail lifts P90 once dense:
    counts = [1] * 80 + [150] * 20  # 20% of files are hubs
    cap = _fanout_cap(counts, n_features=300)
    # P90 of this sorted list (rank 90 of 100) = 150 (hub); frac floor=30.
    assert cap == 150


def test_nearest_rank_percentile():
    assert _nearest_rank([], 0.90) == 0
    assert _nearest_rank([5], 0.90) == 5
    # 10 values 1..10, P90 nearest-rank = rank ceil(0.9*10)=9 → value 9.
    assert _nearest_rank(list(range(1, 11)), 0.90) == 9


def _write_barrel_project(root):
    """Many features each import the SAME barrel index.ts (the conduit) plus
    one feature also imports a genuinely-pairwise-shared util."""
    (root / "src/features").mkdir(parents=True)
    (root / "src/shared").mkdir(parents=True)
    # The barrel: a residual file imported by EVERY feature.
    (root / "src/shared/index.ts").write_text("export const hub = 1;\n")
    # A low-fan-in util: imported by exactly one feature (legit pairwise share).
    (root / "src/shared/util.ts").write_text("export const util = 2;\n")
    n = 30
    for i in range(n):
        (root / f"src/features/f{i}.ts").write_text(
            "import { hub } from '../shared/index';\n"
            + ("import { util } from '../shared/util';\n" if i == 0 else "")
            + "export const x = hub;\n"
        )
    return n


def _barrel_fixture(tmp_path):
    n = _write_barrel_project(tmp_path)
    tracked = (
        ["src/shared/index.ts", "src/shared/util.ts"]
        + [f"src/features/f{i}.ts" for i in range(n)]
    )
    ctx = SimpleNamespace(repo_path=str(tmp_path), tracked_files=tracked)
    # anchor OWNS the two shared files (the de-sink residual);
    # each feature owns exactly its own file.
    anchor = _feat("frontend", ["src/shared/index.ts", "src/shared/util.ts"],
                   description=_WS)
    feats = [anchor] + [
        _feat(f"feature-{i}", [f"src/features/f{i}.ts"], description=_ROUTE)
        for i in range(n)
    ]
    return ctx, feats, n


def test_barrel_conduit_is_skipped(tmp_path):
    """A residual file imported by an outlier number of features (the barrel)
    is NOT propagated N:M; the low-fan-in util still attaches."""
    ctx, feats, n = _barrel_fixture(tmp_path)
    res = enrich_shared_members(ctx, feats)
    # The barrel reached all 30 features → conduit, skipped on every one.
    for f in feats[1:]:
        paths = {m.path for m in f.member_files}
        assert "src/shared/index.ts" not in paths, (
            f"barrel leaked onto {f.name} → phantom duplicate member-set"
        )
    # The single-importer util is a legit pairwise share → still attached.
    f0 = next(f for f in feats if f.name == "feature-0")
    assert "src/shared/util.ts" in {m.path for m in f0.member_files}
    assert res.conduit_files == 1
    assert res.fanout_cap <= n


def test_no_conduit_repo_is_noop_for_guard(tmp_path):
    """When no file is an outlier hub, the guard skips nothing (the original
    enrichment behaviour is byte-preserved)."""
    ctx, feats, anchor, auth = _fixture(tmp_path)
    res = enrich_shared_members(ctx, feats)
    # The base fixture has no barrel → no conduit dropped, both imports kept.
    assert res.conduit_files == 0
    shared = {m.path for m in auth.member_files}
    assert "src/components/Button.tsx" in shared
    assert "src/hooks/useAuth.ts" in shared
    assert res.edges == 2
