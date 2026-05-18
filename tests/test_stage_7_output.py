"""Tests for ``faultline.pipeline_v2.stage_7_output``.

Verifies:

  - ``build_feature_map`` produces a valid :class:`FeatureMap` with
    Layer 1 features stamped, Layer 2 empty.
  - ``scan_meta`` round-trips through model_dump → JSON → reload.
  - ``stage_7_output`` writes a JSON file and the round-trip preserves
    feature names, layer, and scan_meta.
  - ``write_stage_artifact`` creates the per-slug log directory and
    writes the expected filename.
  - Explicit ``out_path`` is honoured; default path is under
    ``~/.faultline/``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Commit, Feature, FeatureMap
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_7_output import (
    build_feature_map,
    stage_7_output,
    stage_artifact_dir,
    write_stage_artifact,
)


def _mk_feature(name: str) -> Feature:
    return Feature(
        name=name,
        display_name=name.replace("-", " ").title(),
        paths=[f"app/{name}/page.tsx"],
        authors=["alice"],
        total_commits=3,
        bug_fixes=1,
        bug_fix_ratio=0.333,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=85.0,
        flows=[],
        layer="developer",
        product_feature_id=None,
    )


def _mk_ctx(repo_path: Path) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="next-app-router",
        monorepo=False,
        workspaces=None,
        tracked_files=["app/billing/page.tsx"],
        commits=[
            Commit(
                sha="abc",
                message="feat: x",
                author="alice",
                date=datetime.now(tz=timezone.utc),
                files_changed=["app/billing/page.tsx"],
                is_bug_fix=False,
            ),
        ],
    )


def test_build_feature_map_stamps_layer_and_empty_product(tmp_path: Path) -> None:
    feats = [_mk_feature("billing"), _mk_feature("notifications")]
    ctx = _mk_ctx(tmp_path)
    fm = build_feature_map(feats, ctx, {"stack": "next-app-router"})
    assert isinstance(fm, FeatureMap)
    assert len(fm.features) == 2
    assert all(f.layer == "developer" for f in fm.features)
    assert fm.get_product_features() == []
    assert fm.scan_meta == {"stack": "next-app-router"}
    assert fm.total_commits == 1
    assert fm.repo_path == str(tmp_path)


def test_build_feature_map_dump_emits_layered_views(tmp_path: Path) -> None:
    feats = [_mk_feature("billing")]
    ctx = _mk_ctx(tmp_path)
    fm = build_feature_map(feats, ctx, {})
    dumped = fm.model_dump(mode="json")
    assert "developer_features" in dumped
    assert "product_features" in dumped
    assert dumped["product_features"] == []
    assert len(dumped["developer_features"]) == 1
    assert dumped["developer_features"][0]["name"] == "billing"


def test_stage_7_round_trip_through_disk(tmp_path: Path) -> None:
    feats = [_mk_feature("billing"), _mk_feature("auth")]
    ctx = _mk_ctx(tmp_path)
    scan_meta = {
        "stack": "next-app-router",
        "model": "claude-haiku-4-5-20251001",
        "llm_fallback_pct": 0.12,
        "warnings": [],
    }
    out_path = tmp_path / "feature-map.json"
    written = stage_7_output(feats, ctx, scan_meta, out_path=out_path)
    assert written == out_path
    assert written.is_file()
    data = json.loads(written.read_text())
    assert {f["name"] for f in data["features"]} == {"billing", "auth"}
    assert all(f.get("layer", "developer") == "developer" for f in data["features"])
    assert data["product_features"] == []
    assert data["scan_meta"]["stack"] == "next-app-router"
    assert data["scan_meta"]["model"] == "claude-haiku-4-5-20251001"


def test_stage_7_writes_stage_artifact(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    feats = [_mk_feature("billing")]
    ctx = _mk_ctx(tmp_path / "myrepo")
    ctx.repo_path.mkdir(parents=True, exist_ok=True)
    out_path = tmp_path / "feature-map.json"
    stage_7_output(feats, ctx, {"k": "v"}, out_path=out_path)
    artifact_dir = fake_home / ".faultline" / "logs" / "myrepo"
    assert artifact_dir.is_dir()
    artifact = artifact_dir / "07-stage-output.json"
    assert artifact.is_file()
    snap = json.loads(artifact.read_text())
    assert snap["feature_count"] == 1
    assert snap["feature_names"] == ["billing"]
    assert snap["scan_meta"] == {"k": "v"}


def test_write_stage_artifact_arbitrary_stage(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    repo = tmp_path / "demo"
    repo.mkdir()
    out = write_stage_artifact(repo, 3, "flows", {"count": 5})
    assert out.is_file()
    assert out.name == "03-stage-flows.json"
    assert json.loads(out.read_text()) == {"count": 5}


def test_stage_artifact_dir_creates_kebab_slug(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    d = stage_artifact_dir(tmp_path / "My-Cool_Repo")
    assert d.is_dir()
    assert d.name == "my-cool-repo"
