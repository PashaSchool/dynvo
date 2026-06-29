"""Tests for structured scan-degradation events (``scan_meta.degradations[]``).

Covers the canonical-schema builders and the ``build_degradations`` aggregator
(the typed sibling of ``build_warnings``).
"""

from __future__ import annotations

from types import SimpleNamespace

from faultline.pipeline_v2 import degradations as deg
from faultline.pipeline_v2.scan_meta import build_degradations


# ── builders emit the canonical schema ──────────────────────────────────────


def _assert_schema(d: dict) -> None:
    assert set(d) == {"type", "stage", "severity", "detail", "metrics"}
    assert isinstance(d["type"], str) and d["type"]
    assert isinstance(d["stage"], str) and d["stage"]
    assert d["severity"] in {"partial", "degraded", "failed"}
    assert isinstance(d["detail"], str) and d["detail"]
    assert isinstance(d["metrics"], dict)


def test_flow_walltime_exceeded_schema():
    d = deg.flow_walltime_exceeded(budget_s=300, affected=85, total=138)
    _assert_schema(d)
    assert d["type"] == deg.TYPE_FLOW_WALLTIME_EXCEEDED
    assert d["stage"] == "stage_3_flows"
    assert d["severity"] == "partial"
    assert d["metrics"] == {"budget_s": 300, "affected": 85, "total": 138}
    assert "300s" in d["detail"] and "85/138" in d["detail"]


def test_budget_exceeded_schema():
    d = deg.budget_exceeded(
        stage="stage_6_3_import_tree",
        budget_sec=120, features_skipped=12, elapsed_sec=121.4,
    )
    _assert_schema(d)
    assert d["type"] == deg.TYPE_BUDGET_EXCEEDED
    assert d["metrics"]["features_skipped"] == 12


def test_llm_degraded_schema():
    d = deg.llm_degraded(stage="stage_3_flows", detail="no ANTHROPIC_API_KEY")
    _assert_schema(d)
    assert d["type"] == deg.TYPE_LLM_DEGRADED
    assert d["severity"] == "degraded"


def test_high_llm_fallback_schema():
    d = deg.high_llm_fallback(share=0.73, threshold=0.5)
    _assert_schema(d)
    assert d["type"] == deg.TYPE_HIGH_LLM_FALLBACK
    assert d["metrics"]["share"] == 0.73
    assert "73%" in d["detail"]


# ── aggregator ──────────────────────────────────────────────────────────────


def _empty():
    return SimpleNamespace(degradations=[], budget_exceeded=False)


def test_build_degradations_collects_stage3():
    walltime = deg.flow_walltime_exceeded(budget_s=300, affected=85, total=138)
    stage3 = SimpleNamespace(degradations=[walltime])
    out = build_degradations(
        stage3=stage3, stage4=_empty(), enrichment=_empty(),
        enrich_result=_empty(), branch_result=_empty(), llm_share=0.1,
    )
    assert out == [walltime]


def test_build_degradations_budget_events():
    enrichment = SimpleNamespace(
        budget_exceeded=True, budget_sec=120,
        features_budget_skipped=7, elapsed_sec=121.0,
    )
    out = build_degradations(
        stage3=_empty(), stage4=_empty(), enrichment=enrichment,
        enrich_result=_empty(), branch_result=_empty(), llm_share=0.1,
    )
    assert len(out) == 1
    assert out[0]["type"] == deg.TYPE_BUDGET_EXCEEDED
    assert out[0]["stage"] == "stage_6_3_import_tree"
    assert out[0]["metrics"]["features_skipped"] == 7


def test_build_degradations_high_fallback():
    out = build_degradations(
        stage3=_empty(), stage4=_empty(), enrichment=_empty(),
        enrich_result=_empty(), branch_result=_empty(), llm_share=0.8,
    )
    assert [r["type"] for r in out] == [deg.TYPE_HIGH_LLM_FALLBACK]


def test_build_degradations_clean_scan_is_empty():
    out = build_degradations(
        stage3=_empty(), stage4=_empty(), enrichment=_empty(),
        enrich_result=_empty(), branch_result=_empty(), llm_share=0.1,
    )
    assert out == []


def test_build_degradations_multiple():
    walltime = deg.flow_walltime_exceeded(budget_s=300, affected=85, total=138)
    branch = SimpleNamespace(
        budget_exceeded=True, budget_sec=60,
        features_budget_skipped=3, elapsed_sec=61.0,
    )
    out = build_degradations(
        stage3=SimpleNamespace(degradations=[walltime]), stage4=_empty(),
        enrichment=_empty(), enrich_result=_empty(),
        branch_result=branch, llm_share=0.9,
    )
    types = sorted(r["type"] for r in out)
    assert types == sorted([
        deg.TYPE_FLOW_WALLTIME_EXCEEDED,
        deg.TYPE_BUDGET_EXCEEDED,
        deg.TYPE_HIGH_LLM_FALLBACK,
    ])
