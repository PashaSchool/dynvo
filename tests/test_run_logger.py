"""Tests for ``faultline.pipeline_v2.run_logger.StageLogger``.

Verifies the JSONL contract: one record per call, with required
fields ``ts/stage/stage_name/event/feature/reason`` plus any extras
the caller passed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2.run_logger import StageLogger


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_stage_logger_writes_one_record_per_call(tmp_path: Path) -> None:
    log = StageLogger(tmp_path, 4, "residual")
    log.emit("billing", "clustered from 12 paths")
    log.drop("trpc-router", "name in JUNK_NAMES")
    log.warn("llm_share=0.42 — informational")
    log.close()

    records = _read_jsonl(log.path)
    assert len(records) == 3
    assert records[0]["event"] == "emit"
    assert records[0]["feature"] == "billing"
    assert records[0]["reason"] == "clustered from 12 paths"
    assert records[0]["stage"] == 4
    assert records[0]["stage_name"] == "residual"
    assert records[1]["event"] == "drop"
    assert records[1]["feature"] == "trpc-router"
    assert records[2]["event"] == "warn"
    assert records[2]["feature"] is None


def test_stage_logger_extras_are_persisted(tmp_path: Path) -> None:
    log = StageLogger(tmp_path, 1, "extractors")
    log.cluster("merged 3 candidates", before=3, after=1)
    log.close()
    rec = _read_jsonl(log.path)[0]
    assert rec["event"] == "cluster"
    assert rec["before"] == 3
    assert rec["after"] == 1


def test_stage_logger_writes_iso_timestamp(tmp_path: Path) -> None:
    log = StageLogger(tmp_path, 0, "intake")
    log.info("hello")
    log.close()
    rec = _read_jsonl(log.path)[0]
    # ISO8601 with timezone marker — ends with +00:00 or Z.
    assert rec["ts"].endswith("+00:00") or rec["ts"].endswith("Z")


def test_stage_logger_creates_log_at_expected_path(tmp_path: Path) -> None:
    log = StageLogger(tmp_path, 7, "output")
    log.info("ok")
    log.close()
    assert log.path == tmp_path / "07-stage-output.log"
    assert log.path.is_file()


def test_stage_logger_creates_run_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "run-x"
    log = StageLogger(target, 0, "intake")
    log.info("ok")
    log.close()
    assert target.is_dir()
    assert log.path.is_file()


def test_stage_logger_rejects_unknown_event(tmp_path: Path) -> None:
    log = StageLogger(tmp_path, 0, "intake")
    with pytest.raises(ValueError, match="unknown StageLogger event"):
        log._append("bogus", None, "x", {})  # noqa: SLF001 — direct call


def test_stage_logger_context_manager_flushes(tmp_path: Path) -> None:
    """Using the context-manager form writes on __exit__."""
    log_path: Path
    with StageLogger(tmp_path, 3, "flows") as log:
        log.emit("billing", "ok")
        log_path = log.path
        assert not log_path.is_file()  # buffered, not yet flushed
    assert log_path.is_file()
    assert len(_read_jsonl(log_path)) == 1


def test_stage_logger_write_after_close_is_dropped(tmp_path: Path) -> None:
    """Late writes after close are ignored rather than raising."""
    log = StageLogger(tmp_path, 0, "intake")
    log.emit("a", "first")
    log.close()
    log.emit("b", "late — should be dropped")  # no exception
    assert len(_read_jsonl(log.path)) == 1
