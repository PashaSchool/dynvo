"""Unit tests for stage-input persistence (write/load, gzip guard,
kill-switch, missing-artifact error, normalizer adapter)."""

from __future__ import annotations

import gzip
import json

import pytest

from faultline.replay import capture
from faultline.replay.capture import (
    MissingStageInputError,
    load_stage_input,
    stage_input_path,
    write_stage_input,
)
from faultline.replay.compare import (
    diff_summary,
    normalize_stage_artifact,
)


@pytest.fixture(autouse=True)
def _sync_capture_path():
    """These tests assert the SYNC write path. A scan run earlier in the
    same pytest process installs the R1 background writer; uninstall it so
    file-on-disk assertions here are order-independent (run_pipeline_v2
    now uninstalls at scan end, but an in-scan abort path must never be
    able to break these tests)."""
    capture.uninstall_async_writer()
    yield
    capture.uninstall_async_writer()


def test_write_and_load_roundtrip(tmp_path):
    state = {"features": [1, 2, 3], "ctx": {"stack": "next-app-router"}}
    path = write_stage_input(tmp_path, 3, "flows", state)
    assert path is not None and path.name == "03-stage-flows-input.json"
    loaded = load_stage_input(tmp_path, 3, "flows")
    assert loaded == state


def test_missing_artifact_names_the_file(tmp_path):
    with pytest.raises(MissingStageInputError) as exc:
        load_stage_input(tmp_path, 4, "residual")
    assert "04-stage-residual-input.json" in str(exc.value)


def test_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_INPUTS", "0")
    assert write_stage_input(tmp_path, 3, "flows", {"a": 1}) is None
    assert not list(tmp_path.iterdir())


def test_none_run_dir_is_noop():
    assert write_stage_input(None, 3, "flows", {"a": 1}) is None


def test_gzip_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "GZIP_THRESHOLD_BYTES", 64)
    state = {"blob": ["x" * 40] * 20}
    path = write_stage_input(tmp_path, 6, "metrics", state)
    assert path is not None and path.name.endswith("-input.json.gz")
    # gzip bytes are stable for identical content (mtime pinned to 0).
    raw1 = path.read_bytes()
    path2 = write_stage_input(tmp_path, 6, "metrics", state)
    assert path2.read_bytes() == raw1
    assert load_stage_input(tmp_path, 6, "metrics") == state
    # resolver finds the gz twin
    assert stage_input_path(tmp_path, 6, "metrics").suffix == ".gz"


def test_gzip_drops_stale_plain_twin(tmp_path, monkeypatch):
    write_stage_input(tmp_path, 6, "metrics", {"small": 1})
    monkeypatch.setattr(capture, "GZIP_THRESHOLD_BYTES", 16)
    write_stage_input(tmp_path, 6, "metrics", {"blob": "y" * 64})
    assert load_stage_input(tmp_path, 6, "metrics") == {"blob": "y" * 64}
    assert not (tmp_path / "06-stage-metrics-input.json").exists()


def test_schema_version_mismatch(tmp_path):
    target = tmp_path / "03-stage-flows-input.json"
    target.write_text(json.dumps({"input_schema_version": 999, "state": {}}))
    with pytest.raises(ValueError, match="input_schema_version"):
        load_stage_input(tmp_path, 3, "flows")


def test_capture_never_raises_on_unserializable(tmp_path):
    # A live handle in the state must degrade to a logged warning,
    # never a scan failure.
    assert write_stage_input(tmp_path, 3, "flows", {"bad": object()}) is None


# ── normalizer adapter ──────────────────────────────────────────────────


def test_normalize_stage_artifact_strips_volatile_everywhere():
    doc = {
        "run_id": "20260703T0-af198c56",
        "feature_count": 3,
        "cost_usd": 0.12,
        "llm_calls": 4,
        "cache_hits": 0,
        "telemetry": {"elapsed_sec": 1.23, "stage_x_elapsed_sec": 4, "kept": 1},
        "guard_drops_sample": ["a"],
    }
    out = normalize_stage_artifact(doc)
    assert out == {"feature_count": 3, "telemetry": {"kept": 1}}


def test_diff_summary_identical_and_differing():
    a = {"x": 1, "cost_usd": 5}
    b = {"x": 1, "cost_usd": 9}
    assert diff_summary(a, b) == []
    c = {"x": 2}
    assert diff_summary(a, c) != []
