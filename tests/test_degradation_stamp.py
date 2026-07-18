"""S2 Seg D — degradation-honesty stamp (``FAULTLINE_DEGRADATION_STAMP``).

The fail-open class (probe 2026-07-18): a scan whose LLM layer visibly died
mid-run self-reports healthy — ``scan_meta.degradations == []`` in BOTH the
degraded and the healthy Soc0 board, so no proof gate can see the difference.

The signature fixtures below are REPRODUCED VERBATIM from the recorded run
artifacts (``~/.faultline/logs/Soc0/``):

* DEAD-KEY run ``20260717T030252Z-3b07142a`` — 06-stage-uf_refiner.json:
  domains_total=82, domains_degraded=66, domains_refined=16, cost_usd=0.0,
  llm_calls=66, cache_hits=16 (every FRESH call failed at $0 — the key died
  mid-scan; the 16 refined domains were warm-cache replays);
  06-stage-journey_abstraction.json: enabled=True, applied=False,
  degraded_reason="abstraction_parse_failed", cost_usd=0.0.
* HEALTHY run ``20260717T132518Z-9b994a79`` (13:25Z, live key) —
  domains_total=82, domains_degraded=3, domains_refined=79,
  cost_usd=0.278144, llm_calls=78, cache_hits=22; 6.7d enabled=True,
  applied=True, cost_usd=0.18945. The 3 degraded domains are the Seg B'
  content bug (token truncation), NOT a dishonest scan — the stamp must
  stay EMPTY here (anti-case).
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2 import degradations as deg
from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS


# ── artifact-derived signature fixtures ─────────────────────────────────────

# 20260717T030252Z-3b07142a / 06-stage-uf_refiner.json (dead key mid-scan).
COLD_SOC0_REFINER = {
    "enabled": True,
    "domains_total": 82,
    "domains_refined": 16,
    "domains_degraded": 66,
    "cost_usd": 0.0,
    "llm_calls": 66,
    "cache_hits": 16,
}

# 20260717T030252Z-3b07142a / 06-stage-journey_abstraction.json.
COLD_SOC0_S67D = {
    "enabled": True,
    "applied": False,
    "degraded_reason": "abstraction_parse_failed",
    "cost_usd": 0.0,
}

# 20260717T132518Z-9b994a79 (13:25Z healthy anti-case).
HEALTHY_REFINER = {
    "enabled": True,
    "domains_total": 82,
    "domains_refined": 79,
    "domains_degraded": 3,
    "cost_usd": 0.278144,
    "llm_calls": 78,
    "cache_hits": 22,
}

HEALTHY_S67D = {
    "enabled": True,
    "applied": True,
    "degraded_reason": None,
    "cost_usd": 0.18945,
}


def _assert_schema(d: dict) -> None:
    assert set(d) == {"type", "stage", "severity", "detail", "metrics"}
    assert isinstance(d["type"], str) and d["type"]
    assert isinstance(d["stage"], str) and d["stage"]
    assert d["severity"] in {"partial", "degraded", "failed"}
    assert isinstance(d["detail"], str) and d["detail"]
    assert isinstance(d["metrics"], dict)


# ── cold-Soc0 signature reproduction (the fail-open board) ──────────────────


def test_cold_soc0_dead_key_refiner_signature_stamps_fatal() -> None:
    """66/66 fresh calls degraded at $0.00 = the dead-key subclass."""
    rec = deg.classify_refiner_degradation(COLD_SOC0_REFINER)
    assert rec is not None
    _assert_schema(rec)
    assert rec["type"] == deg.TYPE_LLM_BATCH_DEGRADED
    assert rec["stage"] == "stage_6_7b_uf_refiner"
    assert rec["severity"] == deg.SEVERITY_FAILED
    assert rec["metrics"]["cost_signature"] == "zero_cost_fresh_fail"
    assert rec["metrics"]["domains_degraded"] == 66
    assert rec["metrics"]["domains_total"] == 82
    assert rec["metrics"]["cost_usd"] == 0.0
    assert rec["metrics"]["llm_calls"] == 66
    assert "66/82" in rec["detail"]


def test_cold_soc0_67d_applied_false_stamps_fatal() -> None:
    """6.7d enabled + applied=False for an LLM/parse reason = FATAL."""
    rec = deg.classify_journey_abstraction_degradation(COLD_SOC0_S67D)
    assert rec is not None
    _assert_schema(rec)
    assert rec["type"] == deg.TYPE_JOURNEY_ABSTRACTION_FAILED
    assert rec["stage"] == "stage_6_7d_journey_abstraction"
    assert rec["severity"] == deg.SEVERITY_FAILED
    assert rec["metrics"]["reason"] == "abstraction_parse_failed"
    assert rec["metrics"]["cost_signature"] == "zero_cost"
    assert "applied=False" in rec["detail"]


def test_cold_soc0_full_board_stamps_both_records() -> None:
    out = deg.detect_finalize_degradations(
        refiner=COLD_SOC0_REFINER, journey_abstraction=COLD_SOC0_S67D,
    )
    assert [r["type"] for r in out] == [
        deg.TYPE_LLM_BATCH_DEGRADED,
        deg.TYPE_JOURNEY_ABSTRACTION_FAILED,
    ]
    assert all(r["severity"] == deg.SEVERITY_FAILED for r in out)


# ── healthy 13:25Z anti-case: the stamp stays EMPTY ─────────────────────────


def test_healthy_run_1325z_signature_is_clean() -> None:
    """3/82 degraded at $0.278 with 6.7d applied=True → NO stamp.

    The 3 degraded domains are the Seg B' truncation content bug — a
    per-domain partial, not a dishonest scan. Stamping it FATAL would make
    every large-domain repo unshippable; the anti-case is load-bearing.
    """
    assert deg.classify_refiner_degradation(HEALTHY_REFINER) is None
    assert deg.classify_journey_abstraction_degradation(HEALTHY_S67D) is None
    assert deg.detect_finalize_degradations(
        refiner=HEALTHY_REFINER, journey_abstraction=HEALTHY_S67D,
    ) == []


# ── subclass boundaries (scale-invariant, no magic counts) ──────────────────


def test_any_fresh_fail_at_zero_cost_is_dead_key_subclass() -> None:
    """EVEN ONE degraded domain trips the stamp when fresh calls billed $0 —
    the floor is structural (a working key never returns $0 on a fresh call),
    not a tuned count."""
    rec = deg.classify_refiner_degradation({
        "enabled": True, "domains_total": 82, "domains_degraded": 1,
        "cost_usd": 0.0, "llm_calls": 1, "cache_hits": 81,
    })
    assert rec is not None
    assert rec["metrics"]["cost_signature"] == "zero_cost_fresh_fail"


def test_majority_degraded_at_nonzero_cost_is_batch_failure() -> None:
    """>50% of the batch degraded even though some domains billed — a
    systemic batch failure (scale-invariant ratio of the total)."""
    rec = deg.classify_refiner_degradation({
        "enabled": True, "domains_total": 82, "domains_degraded": 42,
        "cost_usd": 0.11, "llm_calls": 82, "cache_hits": 0,
    })
    assert rec is not None
    assert rec["metrics"]["cost_signature"] == "majority_degraded"


def test_minority_degraded_at_nonzero_cost_is_clean() -> None:
    """Exactly half (or fewer) degraded at non-zero cost → partial, no stamp."""
    assert deg.classify_refiner_degradation({
        "enabled": True, "domains_total": 82, "domains_degraded": 41,
        "cost_usd": 0.11, "llm_calls": 82, "cache_hits": 0,
    }) is None


def test_worker_error_mass_degrade_without_calls_stamps_majority() -> None:
    """All-worker_error degrade (exceptions, no tokens recorded): llm_calls==0
    so the dead-key rule can't fire, but the majority ratio still does."""
    rec = deg.classify_refiner_degradation({
        "enabled": True, "domains_total": 82, "domains_degraded": 66,
        "cost_usd": 0.0, "llm_calls": 0, "cache_hits": 16,
    })
    assert rec is not None
    assert rec["metrics"]["cost_signature"] == "majority_degraded"


def test_keyless_refiner_disabled_is_clean() -> None:
    """Keyless / no-client scans (enabled=False) never stamp — the refiner
    never ran a batch, so there is nothing dishonest to record."""
    assert deg.classify_refiner_degradation({
        "enabled": False, "fallback_reason": "no_anthropic_client",
        "domains_total": 0, "domains_degraded": 0, "cost_usd": 0.0,
        "llm_calls": 0,
    }) is None
    assert deg.classify_refiner_degradation({}) is None
    assert deg.classify_refiner_degradation(None) is None  # type: ignore[arg-type]


def test_67d_structural_noop_reasons_are_clean() -> None:
    """no_client / no_dev_features / no_candidates / disabled are EXPECTED
    no-ops (keyless channel), never a dishonest degradation."""
    for reason in ("no_client", "no_dev_features", "no_candidates", "disabled"):
        assert deg.classify_journey_abstraction_degradation({
            "enabled": True, "applied": False,
            "degraded_reason": reason, "cost_usd": 0.0,
        }) is None


def test_67d_applied_true_is_clean() -> None:
    assert deg.classify_journey_abstraction_degradation(HEALTHY_S67D) is None
    assert deg.classify_journey_abstraction_degradation({}) is None
    assert deg.classify_journey_abstraction_degradation(
        {"enabled": False},
    ) is None


def test_67d_nonzero_cost_failure_carries_cost_signature() -> None:
    rec = deg.classify_journey_abstraction_degradation({
        "enabled": True, "applied": False,
        "degraded_reason": "reconstruct_empty", "cost_usd": 0.21,
    })
    assert rec is not None
    assert rec["metrics"]["cost_signature"] == "nonzero_cost"


# ── kill-switch: flag default OFF, registered, honest values ────────────────


def test_stamp_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(deg.DEGRADATION_STAMP_ENV, raising=False)
    assert deg.degradation_stamp_enabled() is False


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_stamp_flag_kill_switch_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv(deg.DEGRADATION_STAMP_ENV, val)
    assert deg.degradation_stamp_enabled() is False


def test_stamp_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(deg.DEGRADATION_STAMP_ENV, "1")
    assert deg.degradation_stamp_enabled() is True


def test_stamp_flag_registered_in_env_output_flags() -> None:
    """Cache-key correctness: the flag shapes output → must be keyed
    (append-only, no KEY_SCHEMA bump — reconciled at merge)."""
    assert "FAULTLINE_DEGRADATION_STAMP" in ENV_OUTPUT_FLAGS
