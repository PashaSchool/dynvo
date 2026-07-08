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

from typing import Any

# ── stable type taxonomy (group / dedup / remediate by these) ───────────────
TYPE_FLOW_WALLTIME_EXCEEDED = "flow_walltime_exceeded"
TYPE_BUDGET_EXCEEDED = "budget_exceeded"
TYPE_LLM_DEGRADED = "llm_degraded"
TYPE_HIGH_LLM_FALLBACK = "high_llm_fallback"

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


__all__ = [
    "Degradation",
    "TYPE_FLOW_WALLTIME_EXCEEDED",
    "TYPE_BUDGET_EXCEEDED",
    "TYPE_LLM_DEGRADED",
    "TYPE_HIGH_LLM_FALLBACK",
    "SEVERITY_PARTIAL",
    "SEVERITY_DEGRADED",
    "SEVERITY_FAILED",
    "make",
    "flow_walltime_exceeded",
    "budget_exceeded",
    "llm_degraded",
    "high_llm_fallback",
]
