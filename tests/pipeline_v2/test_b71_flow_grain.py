"""B71 Seg D — flow-grain laws T1-T4 (``FAULTLINE_FLOW_GRAIN``).

Every fixture reproduces a NAMED census exhibit (verified span-shapes from the
armed keyed boards) or the survivor anti-case. Fixtures are synthetic — they
hold the MECHANISM; the corpus deltas are the operator's flow-census + keyed A/B.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from faultline.models.types import Flow, FlowLineRange
from faultline.pipeline_v2.flow_grain import (
    flow_grain_enabled,
    is_barrel_file,
    plan_flow_grain,
    span_subset,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _flow(
    name: str,
    *,
    entry: str | None = None,
    ranges: list[tuple[str, int, int]] | None = None,
    loc: int | None = None,
) -> Flow:
    fl = Flow(
        name=name, uuid=name, entry_point_file=entry, paths=[],
        authors=[], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=90.0, loc=loc,
    )
    if ranges:
        fl.line_ranges = [
            FlowLineRange(path=p, start_line=s, end_line=e) for p, s, e in ranges
        ]
    return fl


# ── OFF byte-identity ───────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ON (horizon-1 flip); explicit 0/false disable — the OFF path
    never calls the planner, so output is byte-identical."""
    # SEMANTIC (horizon-1 flip): unset now defaults ON.
    monkeypatch.delenv("FAULTLINE_FLOW_GRAIN", raising=False)
    assert flow_grain_enabled() is True
    monkeypatch.setenv("FAULTLINE_FLOW_GRAIN", "0")
    assert flow_grain_enabled() is False
    monkeypatch.setenv("FAULTLINE_FLOW_GRAIN", "1")
    assert flow_grain_enabled() is True


def test_inverted_killswitch_flow_grain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inverted kill-switch: unset ≡ explicit ``1`` (default ON); explicit
    ``0``/``false`` == the pre-B71 OFF behaviour (planner never runs)."""
    monkeypatch.delenv("FAULTLINE_FLOW_GRAIN", raising=False)
    unset = flow_grain_enabled()
    monkeypatch.setenv("FAULTLINE_FLOW_GRAIN", "1")
    assert flow_grain_enabled() is unset is True
    monkeypatch.setenv("FAULTLINE_FLOW_GRAIN", "0")
    assert flow_grain_enabled() is False
    monkeypatch.setenv("FAULTLINE_FLOW_GRAIN", "false")
    assert flow_grain_enabled() is False


# ── T1: empty span ───────────────────────────────────────────────────────────


def test_t1_empty_span_dropped() -> None:
    """T1 EXHIBIT: a flow with no resolvable coordinate (empty span-set) breaks
    reverse-lookup — dropped."""
    empty = _flow("barrel-reexport-flow", entry="pkg/index.ts", ranges=None, loc=0)
    real = _flow("do-real-work-flow", entry="pkg/work.ts", ranges=[("work.ts", 10, 40)], loc=30)
    plan = plan_flow_grain([empty, real])
    assert "barrel-reexport-flow" in plan.drop
    assert plan.reasons["barrel-reexport-flow"] == "t1_empty_span"
    assert real in plan.survivors([empty, real])
    assert empty not in plan.survivors([empty, real])


# ── T2: barrel re-anchor (cal platform/libraries/index.ts, hopp kernel index) ─


def test_t2_barrel_reanchor_to_definition_site() -> None:
    """T2 EXHIBIT: a hopp ``kernel/src/index.ts`` barrel entry that owns 0 lines
    but carries a real span in a NON-barrel file re-anchors onto that definition
    site — kept, never renamed."""
    f = _flow(
        "extend-store-flow",
        entry="packages/hoppscotch-kernel/src/index.ts",
        ranges=[
            ("packages/hoppscotch-kernel/src/index.ts", 133, 136),  # re-export line
            ("packages/hoppscotch-kernel/src/store/impl.ts", 20, 80),  # real def
        ],
        loc=0,
    )
    plan = plan_flow_grain([f])
    assert plan.reanchor["extend-store-flow"] == "packages/hoppscotch-kernel/src/store/impl.ts"
    assert plan.reasons["extend-store-flow"] == "t2_barrel_reanchor"
    assert "extend-store-flow" not in plan.drop  # kept


def test_t2_barrel_basename_detection() -> None:
    """``is_barrel_file`` reads the basename convention (structural, not a list
    of repos): index.*, mod.rs, __init__.py are barrels; a real module is not."""
    assert is_barrel_file("cal/packages/platform/libraries/index.ts") is True
    assert is_barrel_file("packages/hoppscotch-kernel/src/index.ts") is True
    assert is_barrel_file("plane/api/urls/__init__.py") is True
    assert is_barrel_file("packages/lib/rate-limit/rate-limits.ts") is False


# ── T3: containment (Soc0 cases.py, hopp index.ts identical twins) ────────────


def test_t3_soc0_cases_containment_fold() -> None:
    """T3 EXHIBIT (Soc0 ``cases.py`` x4): a loc=0 flow whose span-set is a subset
    of a richer sibling's span-set (same entry file) folds into the container."""
    bulk = _flow(
        "bulk-update-cases-flow", entry="app/cases.py",
        ranges=[("cases.py", 1221, 1335)], loc=0,
    )
    export = _flow(
        "export-cases-flow", entry="app/cases.py",
        ranges=[("cases.py", 480, 500), ("cases.py", 1221, 1335), ("case_export.py", 132, 193)],
        loc=134,
    )
    plan = plan_flow_grain([bulk, export])
    assert plan.fold.get("bulk-update-cases-flow") == "export-cases-flow"
    assert plan.reasons["bulk-update-cases-flow"] == "t3_containment"
    assert plan.survivors([bulk, export]) == [export]


def test_t3_identical_twin_reexport_pair_folds_one() -> None:
    """T3 EXHIBIT (hopp ``kernel/src/index.ts``): ``extend-store-flow`` and
    ``extend-kernel-store-flow`` carry the IDENTICAL span ``index.ts:133-136``
    (mutual subset) — exactly one folds into the other, deterministically."""
    a = _flow("extend-kernel-store-flow", entry="kernel/src/index.ts",
              ranges=[("kernel/src/index.ts", 133, 136)], loc=0)
    b = _flow("extend-store-flow", entry="kernel/src/index.ts",
              ranges=[("kernel/src/index.ts", 133, 136)], loc=0)
    plan = plan_flow_grain([a, b])
    folded = set(plan.fold)
    assert len(folded) == 1  # exactly one twin folds, never both
    # deterministic winner = lexicographically smaller uuid (equal mass)
    assert plan.fold == {"extend-store-flow": "extend-kernel-store-flow"}


# ── T4: fanout budget (documenso rate-limits.ts x14) ──────────────────────────


def test_t4_documenso_rate_limits_fanout_collapses() -> None:
    """T4 EXHIBIT (documenso ``rate-limits.ts`` x14): every flow shares the
    IDENTICAL ``rate-limit.ts:61-197`` (137 loc) block and differs only by a
    5-6 line config slice — one code path minted N times. Folds to ONE."""
    shared = ("packages/lib/rate-limit/rate-limit.ts", 61, 197)  # 137 loc, in all
    entry = "packages/lib/server-only/rate-limit/rate-limits.ts"
    fam = []
    for i, (nm, s, e) in enumerate([
        ("configure-2fa-rate-limit-flow", 25, 30),
        ("configure-ai-rate-limit-flow", 89, 93),
        ("configure-api-rate-limit-flow", 71, 75),
        ("configure-authentication-rate-limit-flow", 34, 39),
        ("configure-email-verification-rate-limit-flow", 48, 53),
    ]):
        fam.append(_flow(
            nm, entry=entry,
            ranges=[shared, ("packages/lib/server-only/rate-limit/rate-limits.ts", s, e)],
            loc=e - s + 1,
        ))
    plan = plan_flow_grain(fam)
    survivors = plan.survivors(fam)
    assert len(survivors) == 1  # x5 config stubs -> one code path
    assert all(plan.reasons[u] == "t4_fanout" for u in plan.fold)


# ── ANTI-CASE: documenso domain-core flows survive every law ──────────────────


def test_anticase_documenso_domain_core_survives() -> None:
    """ANTI-CASE (census §4, MANDATORY): documenso ``sign-data-flow`` /
    ``verify-signature-flow`` (loc 9, domain core) — distinct entries, disjoint
    spans, not barrel, not contained, no shared-dominant span. T1-T4 must not
    touch them."""
    sign = _flow("sign-data-flow", entry="packages/lib/signing/sign.ts",
                 ranges=[("packages/lib/signing/sign.ts", 40, 48)], loc=9)
    verify = _flow("verify-signature-flow", entry="packages/lib/signing/verify.ts",
                   ranges=[("packages/lib/signing/verify.ts", 12, 20)], loc=9)
    plan = plan_flow_grain([sign, verify])
    for u in ("sign-data-flow", "verify-signature-flow"):
        assert u not in plan.drop
        assert u not in plan.fold
        assert u not in plan.reanchor
    assert plan.survivors([sign, verify]) == [sign, verify]


def test_anticase_shared_header_not_full_subset() -> None:
    """ANTI-CASE guard: two Soc0 flows sharing only the ``cases.py:79-86``
    decorator header but each owning distinct large spans are NOT subsets of
    each other — T3 must leave both alone (a shared header is legal sharing)."""
    a = _flow("view-case-activity-flow", entry="app/cases.py",
              ranges=[("cases.py", 79, 86), ("cases.py", 4518, 4574)], loc=145)
    b = _flow("browse-case-comments-flow", entry="app/cases.py",
              ranges=[("cases.py", 79, 86), ("cases.py", 4442, 4450)], loc=14)
    plan = plan_flow_grain([a, b])
    assert plan.fold == {}
    assert span_subset({"cases.py": [(79, 86), (4442, 4450)]},
                       {"cases.py": [(79, 86), (4518, 4574)]}) is False
