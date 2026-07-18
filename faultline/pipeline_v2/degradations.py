"""Structured scan-degradation events — ``scan_meta.degradations[]``.

A *degradation* is a MACHINE-READABLE record of a scan finishing in a
visibly-degraded state: a partial stage (a wall-time cap hit, so some features
defaulted to ``flows=[]``), a skipped LLM stage, an unusually high LLM-fallback
share. It is the typed sibling of the free-text ``scan_meta.warnings`` — emitted
so workers / dashboards can aggregate WHERE and HOW OFTEN each kind happens
(group by ``type``), and a kanban board can dedup actionable triage cards by
``(repo, type)`` — instead of grepping prose.

Schema (STABLE — workers + boards depend on it; additive changes only):

    {
      "type":     str,    # stable enum, see the TYPE_* constants below
      "stage":    str,    # the pipeline stage that emitted it
      "severity": str,    # "partial" | "degraded" | "failed"
      "detail":   str,    # human-readable one-liner (same text as the warning)
      "metrics":  dict,   # type-specific numbers (budget_s, affected, total, …)
    }

Severity ladder:
    partial  — the scan completed but some output is thin/missing (a subset of
               features defaulted to ``flows=[]`` after a wall-time cap).
    degraded — a whole capability was skipped (all LLM stages off → no flows /
               naming) but the deterministic output still stands.
    failed   — the stage could not produce its core output at all.

Deterministic, no LLM, no network. Adding a new degradation = add a builder here
+ emit it from the stage; a consumer that doesn't recognise a ``type`` simply
groups it under its raw string (forward-compatible). Worker remediation (e.g.
retry ``flow_walltime_exceeded`` with a bigger budget) keys off ``type``.
"""

from __future__ import annotations

import os
from typing import Any

# ── stable type taxonomy (group / dedup / remediate by these) ───────────────
TYPE_FLOW_WALLTIME_EXCEEDED = "flow_walltime_exceeded"
TYPE_BUDGET_EXCEEDED = "budget_exceeded"
TYPE_LLM_DEGRADED = "llm_degraded"
TYPE_HIGH_LLM_FALLBACK = "high_llm_fallback"
# S2 Seg D — the FATAL honesty-breaking degradations (severity="failed"). They
# describe a scan whose LLM layer could not produce its core output at all yet
# self-reported healthy (empty degradations[]): the refiner's whole fresh batch
# failing at cost==0 (a dead key mid-scan), and the 6.7d journey abstraction
# leaving applied=False with real candidates present. A proof gate keys off
# severity=="failed" to reject such a board rather than score it.
TYPE_LLM_BATCH_DEGRADED = "llm_batch_degraded"
TYPE_JOURNEY_ABSTRACTION_FAILED = "journey_abstraction_failed"

# ── severity ladder ─────────────────────────────────────────────────────────
SEVERITY_PARTIAL = "partial"
SEVERITY_DEGRADED = "degraded"
SEVERITY_FAILED = "failed"

Degradation = dict[str, Any]


def make(
    type_: str, *, stage: str, severity: str, detail: str, **metrics: Any,
) -> Degradation:
    """Build one degradation record in the canonical schema."""
    return {
        "type": type_,
        "stage": stage,
        "severity": severity,
        "detail": detail,
        "metrics": dict(metrics),
    }


def flow_walltime_exceeded(
    *, budget_s: int, affected: int, total: int,
) -> Degradation:
    """Stage 3 flow detection hit its wall-time cap; ``affected`` of ``total``
    features defaulted to ``flows=[]``. The single most common partial-scan
    event; the worker may remediate by retrying with a larger flow budget."""
    return make(
        TYPE_FLOW_WALLTIME_EXCEEDED,
        stage="stage_3_flows",
        severity=SEVERITY_PARTIAL,
        detail=(
            f"flow detection hit the {budget_s}s wall-time cap; "
            f"{affected}/{total} features defaulted to flows=[]"
        ),
        budget_s=budget_s,
        affected=affected,
        total=total,
    )


def budget_exceeded(
    *, stage: str, budget_sec: float, features_skipped: int, elapsed_sec: float,
) -> Degradation:
    """A per-stage enrichment budget (stages 6.3 / 6.4 / 6.6) was exceeded, so
    ``features_skipped`` features (a deterministic canonical suffix) were left
    un-enriched. The budget is applied as a DETERMINISTIC per-feature-allowance
    COUNT, not a wall-clock deadline, so the skipped set is a pure function of
    input; ``elapsed_sec`` is retained only as informational telemetry (it is a
    volatile measurement and is scrubbed from the byte-identity digest — it must
    never appear in ``detail``)."""
    return make(
        TYPE_BUDGET_EXCEEDED,
        stage=stage,
        severity=SEVERITY_PARTIAL,
        detail=(
            f"{stage} enrichment budget exceeded "
            f"(budget_sec={budget_sec}); {features_skipped} feature(s) skipped"
        ),
        budget_sec=budget_sec,
        features_skipped=features_skipped,
        elapsed_sec=elapsed_sec,
    )


def llm_degraded(*, stage: str, detail: str) -> Degradation:
    """An LLM-bearing stage was skipped (no key / auth failure / cost cap) so a
    whole capability is missing, though deterministic output stands."""
    return make(
        TYPE_LLM_DEGRADED,
        stage=stage,
        severity=SEVERITY_DEGRADED,
        detail=detail,
    )


def high_llm_fallback(*, share: float, threshold: float) -> Degradation:
    """An unusually high share of features came from the LLM residual fallback
    rather than the deterministic extractors — a coverage smell, not a failure."""
    return make(
        TYPE_HIGH_LLM_FALLBACK,
        stage="stage_4_residual",
        severity=SEVERITY_PARTIAL,
        detail=(
            f"LLM fallback share {share:.0%} exceeds {threshold:.0%} — "
            "deterministic extractors under-covered the repo"
        ),
        share=round(share, 4),
        threshold=round(threshold, 4),
    )


# ── S2 Seg D — degradation-honesty stamp (flag-gated) ───────────────────────
#
# A *fail-open* degradation is a scan that finished with its LLM layer visibly
# broken yet self-reported healthy (``scan_meta.degradations == []`` in BOTH
# the good and the bad Soc0 board — the probe's 264->78 fail-open, 2026-07-18).
# The two signatures below are FATAL (severity="failed"): the stage could not
# produce its core output at all. They are emitted ONLY under
# FAULTLINE_DEGRADATION_STAMP (default OFF → byte-identical), and a proof gate
# keys off severity=="failed" to reject the board instead of scoring it.

DEGRADATION_STAMP_ENV = "FAULTLINE_DEGRADATION_STAMP"

#: 6.7d ``degraded_reason`` values that mean "nothing to abstract" (keyless / no
#: candidates) rather than "the LLM stage failed with candidates present". These
#: are the EXPECTED no-op reasons — never a dishonest degradation.
_S67D_STRUCTURAL_REASONS = frozenset(
    {"no_dev_features", "no_client", "no_candidates", "disabled"}
)


def degradation_stamp_enabled() -> bool:
    """Default OFF — set ``FAULTLINE_DEGRADATION_STAMP=1`` to arm the stamp.

    OFF/unset appends nothing to ``scan_meta.degradations[]`` (byte-identical).
    """
    return os.environ.get(DEGRADATION_STAMP_ENV, "0").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def refiner_batch_degraded(
    *,
    domains_degraded: int,
    domains_total: int,
    cost_usd: float,
    llm_calls: int,
    cost_signature: str,
) -> Degradation:
    """The uf_refiner's per-domain LLM batch failed at scan scale.

    ``cost_signature`` distinguishes the subclass (scale-invariant, no tuned
    count):

    * ``"zero_cost_fresh_fail"`` — DEAD KEY: fresh calls were issued
      (``llm_calls > 0``) yet the whole batch cost ``$0``. A working key returns
      billable tokens; ``$0`` + degradations means every fresh call came back
      empty and the key died mid-scan. ANY such fresh-fail at cost==0 trips it —
      the floor is structural (>=1), not a magic threshold.
    * ``"majority_degraded"`` — more than half the batch degraded even though
      some domains billed (a systemic batch failure at non-zero cost). A
      scale-invariant ratio of the total, not an absolute count.
    """
    return make(
        TYPE_LLM_BATCH_DEGRADED,
        stage="stage_6_7b_uf_refiner",
        severity=SEVERITY_FAILED,
        detail=(
            f"uf_refiner: {domains_degraded}/{domains_total} domains degraded "
            f"at ${cost_usd:.4f} ({cost_signature}) — the LLM naming layer did "
            f"not land this scan"
        ),
        domains_degraded=domains_degraded,
        domains_total=domains_total,
        cost_usd=round(cost_usd, 6),
        llm_calls=llm_calls,
        cost_signature=cost_signature,
    )


def journey_abstraction_failed(*, reason: str, cost_usd: float) -> Degradation:
    """Stage 6.7d ran with candidates present but left ``applied=False`` (an
    LLM/parse failure, not a structural no-op) — the journey layer never landed,
    so the emitted user_flows[] are the raw pre-abstraction rollup."""
    return make(
        TYPE_JOURNEY_ABSTRACTION_FAILED,
        stage="stage_6_7d_journey_abstraction",
        severity=SEVERITY_FAILED,
        detail=(
            f"6.7d journey abstraction enabled but applied=False "
            f"(reason={reason}) — the journey layer did not land"
        ),
        reason=reason,
        cost_signature="zero_cost" if cost_usd == 0.0 else "nonzero_cost",
        cost_usd=round(cost_usd, 6),
    )


def classify_refiner_degradation(refiner: dict[str, Any]) -> Degradation | None:
    """Detect a fail-open uf_refiner batch from its scan_meta telemetry block.

    Returns a FATAL degradation, or ``None`` for a healthy / keyless / partial
    run (the 13:25Z anti-case: 3/82 degraded at $0.278 → None). Pure; reads only
    the already-emitted board telemetry (no new stage fields), so the OFF path
    stays byte-identical.
    """
    if not refiner or not refiner.get("enabled"):
        # Keyless / no-client: the refiner never ran its batch → not degraded.
        return None
    degraded = int(refiner.get("domains_degraded") or 0)
    total = int(refiner.get("domains_total") or 0)
    if degraded <= 0 or total <= 0:
        return None
    cost = float(refiner.get("cost_usd") or 0.0)
    llm_calls = int(refiner.get("llm_calls") or 0)
    # Dead-key subclass (primary): fresh calls issued, whole batch cost $0.
    if llm_calls > 0 and cost == 0.0:
        return refiner_batch_degraded(
            domains_degraded=degraded, domains_total=total,
            cost_usd=cost, llm_calls=llm_calls,
            cost_signature="zero_cost_fresh_fail",
        )
    # Mass-degrade at non-zero cost: a strict majority of the batch failed.
    # ``degraded * 2 > total`` == "more than half" (scale-invariant ratio).
    if degraded * 2 > total:
        return refiner_batch_degraded(
            domains_degraded=degraded, domains_total=total,
            cost_usd=cost, llm_calls=llm_calls,
            cost_signature="majority_degraded",
        )
    return None


def classify_journey_abstraction_degradation(
    s67d: dict[str, Any],
) -> Degradation | None:
    """Detect a fail-open 6.7d abstraction from its scan_meta telemetry block.

    Returns a FATAL degradation when 6.7d ran (``enabled``) with candidates
    present but left ``applied=False`` for an LLM/parse reason; ``None`` for a
    clean ``applied=True`` run OR a structural no-op (keyless ``no_client`` /
    ``no_dev_features``). Pure.
    """
    if not s67d or not s67d.get("enabled"):
        return None
    if s67d.get("applied"):
        return None
    reason = str(s67d.get("degraded_reason") or "unknown")
    if reason in _S67D_STRUCTURAL_REASONS:
        return None
    return journey_abstraction_failed(
        reason=reason, cost_usd=float(s67d.get("cost_usd") or 0.0),
    )


def detect_finalize_degradations(
    *,
    refiner: dict[str, Any] | None = None,
    journey_abstraction: dict[str, Any] | None = None,
) -> list[Degradation]:
    """Aggregate the finalize-phase fail-open degradations (Seg D).

    Pure function of the two scan_meta telemetry blocks; the caller (phase
    finalize) appends the result to ``scan_meta.degradations[]`` ONLY when
    :func:`degradation_stamp_enabled`. Empty list on a healthy scan.
    """
    out: list[Degradation] = []
    rec = classify_refiner_degradation(refiner or {})
    if rec is not None:
        out.append(rec)
    rec = classify_journey_abstraction_degradation(journey_abstraction or {})
    if rec is not None:
        out.append(rec)
    return out


__all__ = [
    "Degradation",
    "TYPE_FLOW_WALLTIME_EXCEEDED",
    "TYPE_BUDGET_EXCEEDED",
    "TYPE_LLM_DEGRADED",
    "TYPE_HIGH_LLM_FALLBACK",
    "TYPE_LLM_BATCH_DEGRADED",
    "TYPE_JOURNEY_ABSTRACTION_FAILED",
    "SEVERITY_PARTIAL",
    "SEVERITY_DEGRADED",
    "SEVERITY_FAILED",
    "DEGRADATION_STAMP_ENV",
    "make",
    "flow_walltime_exceeded",
    "budget_exceeded",
    "llm_degraded",
    "high_llm_fallback",
    "refiner_batch_degraded",
    "journey_abstraction_failed",
    "classify_refiner_degradation",
    "classify_journey_abstraction_degradation",
    "detect_finalize_degradations",
    "degradation_stamp_enabled",
]
