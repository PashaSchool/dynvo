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
        "features_enriched", "coverage_pct", "sample",
    }
    assert 0.0 <= tele["coverage_pct"] <= 1.0
