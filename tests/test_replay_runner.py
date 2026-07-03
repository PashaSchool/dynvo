"""Unit tests for the replay executor helpers (no pipeline execution)."""

from __future__ import annotations

import pytest

from faultline.replay.registry import (
    STAGES,
    pipeline_slice,
    stage_by_key,
)
from faultline.replay.runner import (
    _fresh_llm_bust,
    _new_run_id,
    resolve_run_dir,
)


# ── registry shape ──────────────────────────────────────────────────────


def test_registry_orders_are_strictly_increasing_and_keys_unique():
    orders = [s.order for s in STAGES]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
    keys = [s.key for s in STAGES]
    assert len(set(keys)) == len(keys)


def test_stage_by_key_accepts_all_spellings():
    assert stage_by_key("flows").key == "flows"
    assert stage_by_key("03-flows").key == "flows"
    assert stage_by_key("03-stage-flows").key == "flows"
    with pytest.raises(KeyError, match="valid stages"):
        stage_by_key("nope")


def test_pipeline_slice_bounds():
    only = pipeline_slice("metrics", None)
    assert [s.key for s in only] == ["metrics"]
    chain = pipeline_slice("bipartite", "metrics")
    assert [s.key for s in chain] == ["bipartite", "metrics"]
    with pytest.raises(ValueError, match="upstream"):
        pipeline_slice("metrics", "bipartite")


def test_llm_cache_dirs_cover_every_llm_stage():
    expected = {
        "auditor": "auditor",
        "flows": "flows",
        "residual": "residual",
        "marketing_clusterer": "product-cluster",
        "uf_splitter": "uf-split",
        "uf_refiner": "uf-refine",
        "journey_abstraction": "abstraction",
        "llm_component_split": "llm-component-split",
    }
    actual = {s.key: s.llm_cache_dir for s in STAGES if s.llm_cache_dir}
    assert actual == expected


# ── runner helpers ──────────────────────────────────────────────────────


def test_fresh_llm_bust_clears_only_target_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(tmp_path))
    flows = tmp_path / "llm-cache" / "flows"
    refine = tmp_path / "llm-cache" / "uf-refine"
    for d in (flows, refine):
        d.mkdir(parents=True)
        (d / "k.json").write_text("{}")
    busted = _fresh_llm_bust(stage_by_key("flows"))
    assert busted == str(flows)
    assert not flows.exists()
    assert (refine / "k.json").exists()  # other kinds untouched
    # non-LLM stage → no-op
    assert _fresh_llm_bust(stage_by_key("metrics")) is None


def test_new_run_id_is_sequential_not_wall_clock(tmp_path):
    src = tmp_path / "slug" / "myrun"
    src.mkdir(parents=True)
    rid1 = _new_run_id(src, "flows")
    assert rid1 == "replay-myrun-flows-1"
    (src.parent / rid1).mkdir()
    assert _new_run_id(src, "flows") == "replay-myrun-flows-2"


def test_resolve_run_dir_path_and_slug(tmp_path, monkeypatch):
    direct = tmp_path / "somewhere" / "run1"
    direct.mkdir(parents=True)
    assert resolve_run_dir(str(direct)) == direct.resolve()
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(tmp_path))
    (tmp_path / "logs" / "chi" / "run2").mkdir(parents=True)
    assert resolve_run_dir("chi/run2").name == "run2"
    with pytest.raises(FileNotFoundError, match="neither a directory"):
        resolve_run_dir("chi/missing-run")
