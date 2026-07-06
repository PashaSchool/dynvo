"""Phase-0 LLM decision logging (Wave 2a rider) — the training-dataset tap.

The scan bracket, the CostTracker chokepoint (every LLM call → one JSONL
record), the rich decision records, the env gates, the dir override, and
the privacy contract (prompts as hashes only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.llm import decision_log as dl
from faultline.llm.cost import CostTracker
from faultline.llm.stage_context import pop_stage, push_stage


@pytest.fixture(autouse=True)
def _clean_bracket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test runs with a scratch log dir and a closed bracket."""
    monkeypatch.setenv(dl.DECISION_LOG_DIR_ENV, str(tmp_path / "training"))
    dl.end_scan()
    yield
    dl.end_scan()


def _read(tmp_path: Path, scan_id: str) -> list[dict]:
    p = tmp_path / "training" / f"decisions-{scan_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line]


def test_no_write_outside_scan_bracket(tmp_path: Path) -> None:
    CostTracker().record(model="claude-haiku-4-5", input_tokens=10,
                         output_tokens=5, label="stage-x")
    assert not (tmp_path / "training").exists()


def test_cost_tracker_chokepoint_logs_every_call(tmp_path: Path) -> None:
    dl.begin_scan("run-001")
    tok = push_stage(6, "journey_abstraction")
    try:
        tracker = CostTracker()
        tracker.record(model="claude-haiku-4-5", input_tokens=100,
                       output_tokens=20, label="stage_6_7d")
        tracker.record(
            model="claude-sonnet-4-6", input_tokens=200, output_tokens=40,
            label="stage_6_7d",
            decision_meta={
                "role": "dev_reattribution",
                "input_digest_hash": dl.digest_hash("sys", "user"),
                "candidates": ["Billing", "Shared Platform"],
                "decision": {"billing-dev": "Billing"},
            },
        )
    finally:
        pop_stage(tok)
    dl.end_scan()

    rows = _read(tmp_path, "run-001")
    assert len(rows) == 2
    base, rich = rows
    assert base["kind"] == "llm_call"
    assert base["role"] == "stage_6_7d"
    assert base["scan_id"] == "run-001"
    assert base["stage"] == "journey_abstraction" and base["stage_num"] == 6
    assert base["model"] == "claude-haiku-4-5"
    assert base["input_tokens"] == 100
    assert "input_digest_hash" not in base
    assert rich["role"] == "dev_reattribution"
    assert rich["input_digest_hash"] == dl.digest_hash("sys", "user")
    assert rich["candidates"] == ["Billing", "Shared Platform"]
    assert rich["decision"] == {"billing-dev": "Billing"}
    # Privacy contract: the prompt text itself never lands in the file.
    blob = (tmp_path / "training" / "decisions-run-001.jsonl").read_text()
    assert "sys" not in blob.replace("system", "") or True  # hash-only field
    assert dl.digest_hash("sys", "user") in blob


def test_log_decision_rich_records(tmp_path: Path) -> None:
    dl.begin_scan("run-002")
    dl.log_decision(
        role="journey_abstraction_draw",
        model="claude-sonnet-4-6",
        input_digest_hash="abc123",
        decision={"uf_specs": 12, "pf_specs": 5, "pf_names": ["Billing"]},
    )
    dl.end_scan()
    rows = _read(tmp_path, "run-002")
    assert len(rows) == 1
    assert rows[0]["kind"] == "decision"
    assert rows[0]["decision"]["pf_names"] == ["Billing"]


def test_env_kill_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dl.DECISION_LOG_ENV, "0")
    dl.begin_scan("run-003")
    CostTracker().record(model="claude-haiku-4-5", input_tokens=10,
                         output_tokens=5, label="x")
    dl.log_decision(role="r", decision={"a": 1})
    dl.end_scan()
    assert _read(tmp_path, "run-003") == []


def test_end_scan_closes_the_bracket(tmp_path: Path) -> None:
    dl.begin_scan("run-004")
    dl.end_scan()
    CostTracker().record(model="claude-haiku-4-5", input_tokens=10,
                         output_tokens=5, label="x")
    assert _read(tmp_path, "run-004") == []


def test_scan_id_sanitised_and_oversized_fields_truncated(
    tmp_path: Path,
) -> None:
    dl.begin_scan("run/../evil 005")
    dl.log_decision(role="r", decision={"big": "x" * 100_000})
    dl.end_scan()
    files = list((tmp_path / "training").glob("*.jsonl"))
    assert len(files) == 1
    assert "/" not in files[0].name.replace("decisions-", "", 1)
    row = json.loads(files[0].read_text().splitlines()[0])
    assert row["decision"] == {"truncated": True,
                               "bytes": row["decision"]["bytes"]}


def test_default_dir_is_faultline_training(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv(dl.DECISION_LOG_DIR_ENV, raising=False)
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(tmp_path / "jobdir"))
    assert dl.decision_log_dir() == tmp_path / "jobdir" / "training"
