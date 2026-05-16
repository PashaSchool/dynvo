"""Tests for the Sprint 9f replay registry."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.replay import (
    StageContext,
    list_stages,
    load_artifact,
    run_chain,
    run_stage,
    save_artifact,
)
from faultline.replay.registry import _unwrap_feature_map


def _feature_dict(name="auth", paths=None, protected=False):
    return {
        "name": name,
        "display_name": name.capitalize(),
        "paths": paths or ["a.ts"],
        "authors": [],
        "total_commits": 5,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "last_modified": datetime.now(tz=timezone.utc).isoformat(),
        "health_score": 95.0,
        "flows": [],
        "protected": protected,
        "protection_reason": None,
    }


def _feature_map_dict(features=None):
    return {
        "repo_path": "/tmp/x",
        "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_commits": 0,
        "date_range_days": 365,
        "features": features or [_feature_dict()],
    }


# ── unwrap_feature_map ──────────────────────────────────────────────


def test_unwrap_raw_feature_map():
    fm = _feature_map_dict()
    assert _unwrap_feature_map(fm) is fm


def test_unwrap_final_artifact_wrapper():
    fm = _feature_map_dict()
    wrapped = {"scan_id": "x", "feature_map": fm}
    assert _unwrap_feature_map(wrapped) is fm


def test_unwrap_stage_wrapper_with_data_feature_map():
    fm = _feature_map_dict()
    wrapped = {"stage": "x", "data": {"feature_map": fm}}
    assert _unwrap_feature_map(wrapped) is fm


def test_unwrap_raises_on_garbage():
    with pytest.raises(ValueError):
        _unwrap_feature_map({"random": "blob"})


# ── load_artifact / save_artifact ───────────────────────────────────


def test_save_then_load_roundtrip(tmp_path: Path):
    fm_dict = _feature_map_dict([_feature_dict("billing", paths=["b.ts"])])
    p = tmp_path / "in.json"
    p.write_text(json.dumps(fm_dict))
    fm = load_artifact(p)
    assert len(fm.features) == 1
    assert fm.features[0].name == "billing"

    out = tmp_path / "out.json"
    save_artifact(fm, out)
    re_loaded = load_artifact(out)
    assert re_loaded.features[0].name == "billing"


def test_load_handles_wrapped_artifact(tmp_path: Path):
    fm_dict = _feature_map_dict()
    wrapped = {"scan_id": "abc", "feature_map": fm_dict}
    p = tmp_path / "wrapped.json"
    p.write_text(json.dumps(wrapped))
    fm = load_artifact(p)
    assert len(fm.features) == 1


# ── list_stages ─────────────────────────────────────────────────────


def test_list_stages_contains_known_stages():
    stages = list_stages()
    assert "feature-protection" in stages
    assert "feature-dedup" in stages
    assert "auto-split" in stages


# ── run_stage ───────────────────────────────────────────────────────


def test_run_stage_unknown_raises():
    fm_dict = _feature_map_dict()
    from faultline.models.types import FeatureMap
    fm = FeatureMap.model_validate(fm_dict)
    with pytest.raises(KeyError):
        run_stage("nonexistent", fm)


def test_run_stage_feature_protection_marks_protected():
    """Sprint 10a — run_stage now returns a NEW FeatureMap (input
    isolated). Assert against the return value, not the input."""
    fm_dict = _feature_map_dict([
        _feature_dict("templates", paths=["packages/trpc/server/templates-router/router.ts"])
    ])
    from faultline.models.types import FeatureMap
    fm = FeatureMap.model_validate(fm_dict)
    out = run_stage("feature-protection", fm)
    assert out.features[0].protected is True
    # Input is isolated — not mutated
    assert fm.features[0].protected is False


def test_run_stage_auto_split_below_threshold_no_change():
    fm_dict = _feature_map_dict()
    from faultline.models.types import FeatureMap
    fm = FeatureMap.model_validate(fm_dict)
    n_before = len(fm.features)
    run_stage("auto-split", fm)
    assert len(fm.features) == n_before


def test_run_stage_feature_dedup_without_llm_merges_obvious():
    fm_dict = _feature_map_dict([
        _feature_dict("auth", paths=["a.ts"]),
        _feature_dict("auth", paths=["b.ts"]),
        _feature_dict("billing", paths=["c.ts"]),
    ])
    from faultline.models.types import FeatureMap
    fm = FeatureMap.model_validate(fm_dict)
    out = run_stage("feature-dedup", fm)
    assert len(out.features) == 2  # auth+auth merged on output
    assert len(fm.features) == 3   # input untouched


# ── run_chain ───────────────────────────────────────────────────────


def test_run_chain_executes_sequence():
    fm_dict = _feature_map_dict([
        _feature_dict("templates", paths=["packages/trpc/server/templates-router/router.ts"]),
        _feature_dict("templates", paths=["packages/lib/server-only/templates/x.ts"]),
    ])
    from faultline.models.types import FeatureMap
    fm = FeatureMap.model_validate(fm_dict)
    out = run_chain(["feature-protection", "feature-dedup"], fm)
    # protection fires, then dedup collapses the two "templates"
    assert len(out.features) == 1
    assert out.features[0].protected is True
