"""RC2-4 — DOCS/API-SURFACE family rung + synthetic-fixture UF drop
(2026-07-06), ladder steps (iii) and (iv) between the entry-file carve (ii)
and the honest-unresolved fallback (now v).

Diagnosis (midday "Access documented REST API", pfid=shared-platform, 12
member flows): ALL 12 flows anchor in ``apps/api/evals/fixtures.ts`` — an
LLM tool-selection EVAL FIXTURES module (``ToolSelectionFixture[]`` test
data), not a docs/openapi route. The UF's free-text NAME reads like a real
"access the documented API" journey, but its flows are not genuinely
route-grounded in any docs surface — the task's literal DOCS/API-SURFACE
rung therefore correctly DECLINES this case (guarded on flow PATHS, never
the UF name). A second, narrower rung (synthetic-fixture drop) is what
actually resolves midday's I10 violation: every member flow is a
whole-file ``"<file>"`` fallback attribution (no real symbol resolved)
anchored in fixture/mock/eval-convention data and never a detected route —
not a deferred-home journey, never was one. Dropped rather than re-homed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Feature, Flow, FlowNode, MemberFile, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _is_docs_surface_path,
    _is_synthetic_data_path,
    _is_whole_file_fallback_flow,
    _reassign_shared_ufs,
)

SHARED = "shared-platform"


# ── Fixtures ──────────────────────────────────────────────────────────────

def _node(file: str, symbol: str | None = "<file>") -> FlowNode:
    return FlowNode(id=f"{file}#{symbol}", kind="file", file=file,
                     symbol=symbol, role="entry", confidence="medium")


def _flow(uuid: str, entry: str, *, nodes: list[FlowNode] | None = None) -> Flow:
    return Flow(
        name=uuid, uuid=uuid, paths=[entry], entry_point_file=entry,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=80.0,
        nodes=nodes if nodes is not None else [_node(entry)],
    )


def _dev(name: str, flows: list[Flow], *, paths: list[str] | None = None,
         pfid: str | None = None) -> Feature:
    pths = paths or [f"apps/{name}/index.ts"]
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=pths, authors=["a"], total_commits=3, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer", product_feature_id=pfid,
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0)
                      for p in pths],
        flows=flows,
    )


def _uf(name: str, pf_slug: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id="UF-000", name=name, intent="browse", resource=name.lower(),
        product_feature_id=pf_slug, member_flow_ids=members,
        member_count=len(members), routes=[],
    )


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, description=display, paths=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        layer="product", member_files=[], flows=[])


# ── Path predicates ─────────────────────────────────────────────────────────

def test_docs_surface_path_matches_universal_vocabulary() -> None:
    assert _is_docs_surface_path("apps/api/src/index.ts")  is False  # bare file
    assert _is_docs_surface_path("apps/api/openapi.json")
    assert _is_docs_surface_path("apps/web/src/app/api-reference/page.tsx")
    assert _is_docs_surface_path("apps/website/src/app/docs/page.tsx")
    assert _is_docs_surface_path("apps/api/src/scalar-ui.ts")
    assert _is_docs_surface_path(None) is False


def test_synthetic_data_path_matches_fixture_mock_eval_conventions() -> None:
    assert _is_synthetic_data_path("apps/api/evals/fixtures.ts")
    assert _is_synthetic_data_path("apps/api/evals/tool-selection.eval.ts")
    assert _is_synthetic_data_path("test/mocks/user.ts")
    assert _is_synthetic_data_path("apps/api/src/index.ts") is False


def test_synthetic_data_path_does_not_flag_bare_eval_token() -> None:
    """Regression guard for the Soc0 collateral finding: a filename merely
    CONTAINING 'eval' as a compound-word substring (a real evaluator
    service) must NOT match — only the fixture/mock/dummy/sample markers or
    the precise '.eval.ts' / 'evals/' conventions do."""
    assert _is_synthetic_data_path("backend/services/detector_eval.py") is False
    assert _is_synthetic_data_path("backend/scripts/compare_eval.py") is False


def test_whole_file_fallback_flow_detection() -> None:
    fallback = _flow("f1", "apps/api/evals/fixtures.ts")
    assert _is_whole_file_fallback_flow(fallback)
    real = _flow("f2", "apps/api/src/rest/routers/invoices.ts",
                 nodes=[_node("apps/api/src/rest/routers/invoices.ts", "listInvoices")])
    assert not _is_whole_file_fallback_flow(real)
    multi = _flow("f3", "apps/api/src/x.ts",
                  nodes=[_node("apps/api/src/x.ts"), _node("apps/api/src/y.ts")])
    assert not _is_whole_file_fallback_flow(multi)


# ── Ladder step (iii): DOCS/API-SURFACE family ──────────────────────────────

def test_docs_surface_rung_resolves_real_doc_journey() -> None:
    """openstatus-shaped: a UF whose flows majority-anchor in a real
    openapi/docs surface gets its own api-documentation home."""
    api = _dev("api", [
        _flow("d1", "apps/server/src/routes/openapi.ts"),
        _flow("d2", "apps/server/src/app/docs/page.tsx"),
    ])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Access documented REST API", SHARED, ["d1", "d2"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert uf.product_feature_id == "api-documentation"
    assert tele["uf_shared_docs_resolved"] == 1
    assert tele["uf_shared_synthetic_dropped"] == 0
    assert any(p.name == "api-documentation" for p in pfs)


def test_docs_surface_rung_declines_when_flows_not_route_grounded() -> None:
    """midday's actual case: the UF NAME reads like a docs journey, but its
    flows are NOT anchored in any docs surface (they're eval fixtures) — the
    rung must DECLINE (name alone is never sufficient)."""
    api = _dev("api", [
        _flow("e1", "apps/api/evals/fixtures.ts"),
        _flow("e2", "apps/api/evals/fixtures.ts"),
    ])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Access documented REST API", SHARED, ["e1", "e2"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert tele["uf_shared_docs_resolved"] == 0
    assert not any(p.name == "api-documentation" for p in pfs)


# ── Ladder step (iv): synthetic-fixture UF drop ─────────────────────────────

def test_synthetic_fixture_uf_dropped_not_rehomed() -> None:
    """The midday reproduction: 12 (here, 2 for brevity) all-shared flows,
    every one a whole-file fallback anchored in an eval-fixtures file that
    was never a detected route — dropped, not force-homed."""
    api = _dev("api", [
        _flow("e1", "apps/api/evals/fixtures.ts"),
        _flow("e2", "apps/api/evals/fixtures.ts"),
    ])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Access documented REST API", SHARED, ["e1", "e2"])
    new_ufs = [uf]
    tele = _reassign_shared_ufs(new_ufs, devs, d2p, new_pfs=pfs)
    assert tele["uf_shared_synthetic_dropped"] == 1
    assert tele["uf_shared_unresolved"] == 0
    assert new_ufs == []  # dropped from the live list, not left dangling


def test_synthetic_fixture_rung_declines_when_flow_is_a_real_route() -> None:
    """A single member flow whose entry file IS a detected route disqualifies
    the drop for the whole UF — falls through to honest-unresolved."""
    api = _dev("api", [
        _flow("e1", "apps/api/evals/fixtures.ts"),
        _flow("r1", "apps/api/src/rest/routers/invoices.ts"),
    ])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Access documented REST API", SHARED, ["e1", "r1"])
    routes_index = [{"file": "apps/api/src/rest/routers/invoices.ts",
                      "feature_uuid": "u1"}]
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs,
                                routes_index=routes_index)
    assert tele["uf_shared_synthetic_dropped"] == 0
    assert tele["uf_shared_unresolved"] == 1
    assert uf.product_feature_id == SHARED


def test_synthetic_fixture_rung_declines_when_flow_has_real_symbol() -> None:
    """A whole-repo eval-fixtures-adjacent UF whose flows resolve to a REAL
    symbol (not the whole-file fallback) is not synthetic — declines."""
    api = _dev("api", [
        _flow("e1", "apps/api/evals/fixtures.ts",
              nodes=[_node("apps/api/evals/fixtures.ts", "buildFixture")]),
    ])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Access documented REST API", SHARED, ["e1"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert tele["uf_shared_synthetic_dropped"] == 0
    assert tele["uf_shared_unresolved"] == 1


def test_synthetic_fixture_rung_declines_on_non_fixture_naming() -> None:
    """A whole-file-fallback, non-route flow whose file has NO fixture/mock/
    eval naming convention is left honestly unresolved — the naming gate
    guards against over-triggering on ordinary thin files."""
    api = _dev("api", [_flow("p1", "apps/api/src/plain-utility.ts")])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Do something plain", SHARED, ["p1"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert tele["uf_shared_synthetic_dropped"] == 0
    assert tele["uf_shared_unresolved"] == 1


def test_ladder_order_docs_before_synthetic_drop() -> None:
    """When BOTH a docs-surface majority AND fixture-naming coexist across
    members, the docs rung (iii) wins before the drop rung (iv) is even
    consulted — a real journey is never discarded."""
    api = _dev("api", [
        _flow("d1", "apps/api/openapi.json"),
        _flow("d2", "apps/api/src/app/docs/page.tsx"),
        _flow("e1", "apps/api/evals/fixtures.ts"),
    ])
    devs = [api]
    d2p = {"api": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Access documented REST API", SHARED, ["d1", "d2", "e1"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert tele["uf_shared_docs_resolved"] == 1
    assert tele["uf_shared_synthetic_dropped"] == 0
    assert uf.product_feature_id == "api-documentation"
