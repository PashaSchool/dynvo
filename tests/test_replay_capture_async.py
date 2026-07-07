"""R1 — background capture writer.

``write_stage_input`` semantics are unchanged (same artifact names, same
gzip threshold, same never-break-the-scan contract, capture still ON by
default). What changed: (a) capture documents are compact JSON (readers
``json.load`` them back — indent was pure hot-path cost), and (b) during
a pipeline run the dumps+gzip+write happens on ONE background writer
thread with a bounded queue, drained at scan end. ``to_jsonable`` stays
synchronous so the captured state is snapshotted before the stage runs.
"""

from __future__ import annotations

import json

import pytest

from faultline.replay import capture
from faultline.replay.capture import (
    drain_async_writer,
    install_async_writer,
    load_stage_input,
    uninstall_async_writer,
    write_stage_input,
)


@pytest.fixture(autouse=True)
def _clean_writer_state():
    """Order-independence: a scan run earlier in the same pytest process
    must not leak an installed writer into the sync-path assertions here
    (and these tests must not leak one out)."""
    capture.uninstall_async_writer()
    yield
    capture.uninstall_async_writer()


def test_sync_path_roundtrip_compact(tmp_path) -> None:
    state = {"features": [{"name": "auth"}], "n": 3}
    path = write_stage_input(tmp_path, 4, "residual", state)
    assert path is not None and path.exists()
    # Compact serialization (no indent) — still plain JSON.
    doc = json.loads(path.read_bytes())
    assert doc["stage_name"] == "residual"
    loaded = load_stage_input(tmp_path, 4, "residual")
    assert loaded == state


def test_async_writer_drain_flushes_all_writes(tmp_path) -> None:
    install_async_writer()
    try:
        for i in range(8):
            write_stage_input(tmp_path, i, f"stage{i}", {"i": i})
        drain_async_writer()
        for i in range(8):
            assert load_stage_input(tmp_path, i, f"stage{i}") == {"i": i}
    finally:
        uninstall_async_writer()


def test_async_snapshot_taken_at_call_time(tmp_path) -> None:
    """Mutating the state AFTER write_stage_input returns must not leak
    into the artifact (to_jsonable snapshot happens synchronously)."""
    install_async_writer()
    try:
        state = {"features": [{"name": "before"}]}
        write_stage_input(tmp_path, 1, "extractors", state)
        state["features"][0]["name"] = "after"  # mutate post-submit
        drain_async_writer()
        loaded = load_stage_input(tmp_path, 1, "extractors")
        assert loaded["features"][0]["name"] == "before"
    finally:
        uninstall_async_writer()


def test_async_write_failure_never_raises(tmp_path, caplog) -> None:
    install_async_writer()
    try:
        # Unserializable object → encode error caught synchronously,
        # returns None, never raises (contract unchanged).
        class Boom:  # noqa: B903
            pass

        result = write_stage_input(tmp_path, 2, "reconcile", {"x": Boom()})
        assert result is None
        drain_async_writer()
    finally:
        uninstall_async_writer()


def test_gzip_threshold_still_applies_async(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "GZIP_THRESHOLD_BYTES", 64)
    install_async_writer()
    try:
        big = {"blob": "x" * 4096}
        write_stage_input(tmp_path, 3, "flows", big)
        drain_async_writer()
        gz = tmp_path / "03-stage-flows-input.json.gz"
        assert gz.exists()
        assert load_stage_input(tmp_path, 3, "flows") == big
    finally:
        uninstall_async_writer()
