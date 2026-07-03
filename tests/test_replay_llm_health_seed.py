"""Replay fidelity for scan-wide LLM health (WS1 identity gate).

``LlmHealth`` is sticky across a live scan — one auth-class failure
flips ``should_call()`` False for every later stage. Stage input
artifacts do not carry that service state, so ``replay()`` must seed
the recorded auth-dead state for targets strictly DOWNSTREAM of the
recorded death point (``scan_meta.llm_degraded`` in the source run's
``07-stage-output.json``), and must NOT seed at/before it.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.replay.registry import STAGES
from faultline.replay.runner import ReplayEnv, _seed_recorded_llm_health

_SPEC = {s.key: s for s in STAGES}


def _write_output_artifact(run_dir: Path, degraded: dict | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    scan_meta = {"llm_degraded": degraded} if degraded is not None else {}
    (run_dir / "07-stage-output.json").write_text(
        json.dumps({"feature_count": 1, "scan_meta": scan_meta}),
    )


def _env(tmp_path: Path) -> ReplayEnv:
    return ReplayEnv(run_dir=tmp_path / "new-run", run_id="replay-test-1")


AUTH_DEATH_AT_FLOWS = {
    "reason": "auth_error",
    "first_stage": "stage_3_flows",
    "detail": "Error code: 401 - invalid x-api-key (req_test)",
}


def test_seed_auth_failure_marks_health_dead_idempotently() -> None:
    health = LlmHealth()
    assert health.should_call()
    health.seed_auth_failure(stage="stage_3_flows", detail="401")
    assert not health.should_call()
    assert health.degraded() == {
        "reason": "auth_error",
        "first_stage": "stage_3_flows",
        "detail": "401",
    }
    # Second seed never overwrites the first record.
    health.seed_auth_failure(stage="stage_4_residual", detail="other")
    assert health.degraded()["first_stage"] == "stage_3_flows"


def test_downstream_target_is_seeded_dead(tmp_path: Path) -> None:
    source = tmp_path / "src-run"
    _write_output_artifact(source, AUTH_DEATH_AT_FLOWS)
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["residual"], env)
    assert not env.llm_health.should_call()
    assert env.llm_health.degraded()["first_stage"] == "stage_3_flows"


def test_death_stage_itself_starts_healthy(tmp_path: Path) -> None:
    """The target where auth died entered healthy live — replay must
    re-derive the failure, not inherit it."""
    source = tmp_path / "src-run"
    _write_output_artifact(source, AUTH_DEATH_AT_FLOWS)
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["flows"], env)
    assert env.llm_health.should_call()


def test_upstream_target_starts_healthy(tmp_path: Path) -> None:
    source = tmp_path / "src-run"
    _write_output_artifact(source, AUTH_DEATH_AT_FLOWS)
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["reconcile"], env)
    assert env.llm_health.should_call()


def test_healthy_source_run_never_seeds(tmp_path: Path) -> None:
    source = tmp_path / "src-run"
    _write_output_artifact(source, None)
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["output"], env)
    assert env.llm_health.should_call()


def test_rate_limited_degradation_never_seeds(tmp_path: Path) -> None:
    """Rate-limit degradation is not sticky live — never seeded."""
    source = tmp_path / "src-run"
    _write_output_artifact(
        source,
        {"reason": "rate_limited", "first_stage": "stage_3_flows", "detail": "429"},
    )
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["residual"], env)
    assert env.llm_health.should_call()


def test_unknown_death_label_never_seeds(tmp_path: Path) -> None:
    source = tmp_path / "src-run"
    _write_output_artifact(
        source,
        {"reason": "auth_error", "first_stage": "not_a_stage", "detail": "401"},
    )
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["output"], env)
    assert env.llm_health.should_call()


def test_missing_output_artifact_never_seeds(tmp_path: Path) -> None:
    source = tmp_path / "src-run"
    source.mkdir()
    env = _env(tmp_path)
    _seed_recorded_llm_health(source, _SPEC["output"], env)
    assert env.llm_health.should_call()


def test_every_health_label_maps_to_a_registry_key() -> None:
    """The label→spec map must stay in sync with record_failure call
    sites AND the registry."""
    from faultline.replay.runner import _HEALTH_LABEL_TO_SPEC_KEY

    for label, key in _HEALTH_LABEL_TO_SPEC_KEY.items():
        assert key in _SPEC, f"{label} maps to unknown spec key {key}"
