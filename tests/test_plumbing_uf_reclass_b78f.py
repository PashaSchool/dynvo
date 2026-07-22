"""B78 Seg F — plumbing-UF reclassification (Stage 6.996).

Named exhibits (MUST reclassify to category="system") and named anti-cases
(MUST survive as category="interactive") from the fixb78 spec §Seg F +
forensics canon (138-UF corpus, cal 'Run <Vendor> jobs' x25). The flag
(FAULTLINE_PLUMBING_UF_RECLASS) is default OFF — every assertion here arms
it explicitly; the OFF byte-identity is a separate gate (killswitch A/B).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from faultline.models.types import UserFlow
from faultline.pipeline_v2.plumbing_uf_reclass import (
    PLUMBING_UF_RECLASS_ENV,
    load_plumbing_vocab,
    plumbing_name_verdict,
    plumbing_uf_reclass_enabled,
    run_plumbing_uf_reclass,
)
from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS


# ── lightweight flow stub: run_plumbing_uf_reclass reads only these two ──────
@dataclass
class _FlowStub:
    uuid: str
    entry_point_file: str | None = None
    paths: list[str] = field(default_factory=list)


def _uf(
    uf_id: str,
    name: str,
    *,
    members: list[str],
    mc: int | None = None,
    routes: list[str] | None = None,
    category: str = "interactive",
    surface_scope: str | None = "product",
    synthesized: bool = False,
    synthesis_reason: str | None = None,
    is_coverage_marker: bool = False,
    lane_ref: str | None = None,
) -> UserFlow:
    return UserFlow(
        id=uf_id,
        name=name,
        intent="execute",
        resource="thing",
        product_feature_id="pf-x",
        member_flow_ids=members,
        member_count=len(members) if mc is None else mc,
        routes=routes or [],
        category=category,
        surface_scope=surface_scope,
        synthesized=synthesized,
        synthesis_reason=synthesis_reason,
        is_coverage_marker=is_coverage_marker,
        lane_ref=lane_ref,
    )


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PLUMBING_UF_RECLASS_ENV, "1")
    load_plumbing_vocab.cache_clear()


def _run(ufs, flows_by_id=None, page_ri=None):
    return run_plumbing_uf_reclass(ufs, flows_by_id or {}, page_ri or set())


# ════════════════════════════════════════════════════════════════════════
# gate (a) — plumbing_name_verdict vocab branches
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    "Fetch OpenAPI specifications",   # verb_strong: fetch
    "Lookup user by email",           # verb_strong: lookup
    "Check trial status",             # verb_object: check + status
    "Check account status",           # verb_object: check + status
    "Run Stripe jobs",                # verb_object: run + jobs
    "Run 8x8 jobs",                   # vendor-family shape (numeric vendor)
    "Run MCP operations",             # verb_object: run + operations (NAME fires)
])
def test_name_gate_fires_for_plumbing_shapes(name: str) -> None:
    assert plumbing_name_verdict(name) is not None, name


@pytest.mark.parametrize("name", [
    "Run conditional workflows",        # object 'workflows' NOT in vocab
    "Run alls",                         # object 'alls' NOT in vocab
    "Run forms",                        # object 'forms' NOT in vocab
    "Run guest nos",                    # object 'guest'/'nos' NOT in vocab
    "Validate OpenAPI specifications",  # verb 'validate' not a plumbing verb
    "Generate OpenAPI schema",          # verb 'generate' not a plumbing verb
    "Browse and manage OpenAPI actions",
    "Create and manage MCP servers",
    "View billing plans and trial status",
    "Internal app store HubSpot operations",  # 'internal' not a verb
    "Manage MCP tools",
    "",
])
def test_name_gate_silent_for_product_shapes(name: str) -> None:
    assert plumbing_name_verdict(name) is None, name


def test_product_verbs_never_lead_a_plumbing_name() -> None:
    """The YAML _anti_case_product_verbs ledger is executable: no product
    verb ever leads a firing name."""
    from faultline.pipeline_v2.data import load_yaml
    ledger = (load_yaml("plumbing-uf-vocab.yaml")["plumbing_uf"]
              ["_anti_case_product_verbs"])
    for verb in ledger:
        assert plumbing_name_verdict(f"{verb} OpenAPI specifications") is None, verb


# ════════════════════════════════════════════════════════════════════════
# EXHIBITS — must reclassify to category="system"
# ════════════════════════════════════════════════════════════════════════

def test_exhibit_fetch_openapi_specifications_reclassifies(monkeypatch) -> None:
    _arm(monkeypatch)
    uf = _uf("UF-001", "Fetch OpenAPI specifications", members=["f1"])
    flows = {"f1": _FlowStub("f1", "backend/routers/openapi.py",
                             ["backend/routers/openapi.py"])}
    tele = _run([uf], flows)
    assert uf.category == "system"
    assert uf.surface_scope == "system"
    assert tele["count"] == 1
    assert tele["reclassified"][0]["id"] == "UF-001"


def test_exhibit_check_trial_status_reclassifies(monkeypatch) -> None:
    _arm(monkeypatch)
    uf = _uf("UF-002", "Check trial status", members=["f1"])
    flows = {"f1": _FlowStub("f1", "backend/lib/trial.py", ["backend/lib/trial.py"])}
    _run([uf], flows)
    assert uf.category == "system"


def test_exhibit_lookup_user_by_email_reclassifies(monkeypatch) -> None:
    _arm(monkeypatch)
    uf = _uf("UF-003", "Lookup user by email", members=["f1"])
    flows = {"f1": _FlowStub("f1", "backend/lib/users.py", ["backend/lib/users.py"])}
    _run([uf], flows)
    assert uf.category == "system"


def test_exhibit_cal_run_vendor_jobs_shape_reclassifies(monkeypatch) -> None:
    """cal.com 'Run <Vendor> jobs' x25 shape — mc=1, routeless, api entry."""
    _arm(monkeypatch)
    vendors = ["Stripe", "8x8", "Attio", "Telegram", "WhatsApp"]
    ufs, flows = [], {}
    for i, v in enumerate(vendors):
        fid = f"f{i}"
        slug = v.lower().replace(" ", "-")
        flows[fid] = _FlowStub(
            fid, f"packages/app-store/{slug}/api/index.ts",
            [f"packages/app-store/{slug}/api/index.ts"])
        ufs.append(_uf(f"UF-1{i:02d}", f"Run {v} jobs", members=[fid]))
    tele = _run(ufs, flows)
    assert tele["count"] == len(vendors)
    assert all(u.category == "system" for u in ufs)
    assert tele["by_verb"] == {"run": len(vendors)}


# ════════════════════════════════════════════════════════════════════════
# ANTI-CASES — must survive as category="interactive"
# ════════════════════════════════════════════════════════════════════════

def test_anti_run_mcp_operations_survives_on_member_count(monkeypatch) -> None:
    """'Run MCP operations' — the NAME gate FIRES (run + operations) but the
    5-member profile vetoes: the object match is necessary, never sufficient
    (the honest mc<=2 boundary the spec flags)."""
    _arm(monkeypatch)
    uf = _uf("UF-050", "Run MCP operations", members=["a", "b", "c", "d", "e"])
    tele = _run([uf], {f: _FlowStub(f, f"lib/mcp/{f}.ts") for f in "abcde"})
    assert uf.category == "interactive"
    assert tele["count"] == 0
    assert tele["candidates"] == 1  # it WAS a name candidate
    ab = [a for a in tele["abstained"] if a["id"] == "UF-050"]
    assert ab and ab[0]["reason"] == "member_count" and ab[0]["mc"] == 5


def test_anti_run_conditional_workflows_survives_as_product(monkeypatch) -> None:
    """'Run conditional workflows' — object 'workflows' is a PRODUCT object
    (absent from the tech-object vocab): the name gate never fires, so it is
    never even a candidate, regardless of its (micro) profile."""
    _arm(monkeypatch)
    uf = _uf("UF-051", "Run conditional workflows", members=["a"])
    tele = _run([uf], {"a": _FlowStub("a", "lib/wf.ts", ["lib/wf.ts"])})
    assert uf.category == "interactive"
    assert tele["candidates"] == 0


def test_anti_internal_operations_large_mc_untouched(monkeypatch) -> None:
    """'Internal <domain> operations' (reclassify_service_internals output):
    already category="system", large mc — NEVER re-touched (mc>2 territory is
    B78 B/D vacuum-shape, not this class). Unit-pins the mc>2 no-touch."""
    _arm(monkeypatch)
    uf = _uf("UF-052", "Internal app store HubSpot operations",
             members=[f"m{i}" for i in range(24)], category="system",
             surface_scope="system")
    tele = _run([uf], {})
    assert uf.category == "system"          # unchanged
    assert tele["candidates"] == 0          # not interactive → not a candidate
    assert tele["count"] == 0


def test_anti_page_entry_uf_survives(monkeypatch) -> None:
    """Any UF with a member PAGE entry KEEP (failure-archaeology user-reachable
    veto). Plumbing NAME + micro + routeless, but a member is a product page."""
    _arm(monkeypatch)
    uf = _uf("UF-053", "Fetch dashboard data", members=["f1", "f2"])
    flows = {
        "f1": _FlowStub("f1", "backend/api/fetch.py", ["backend/api/fetch.py"]),
        "f2": _FlowStub("f2", "app/dashboard/page.tsx", ["app/dashboard/page.tsx"]),
    }
    tele = _run([uf], flows)
    assert uf.category == "interactive"
    ab = [a for a in tele["abstained"] if a["id"] == "UF-053"]
    assert ab and ab[0]["reason"] == "page_surface"


def test_anti_page_entry_via_routes_index_survives(monkeypatch) -> None:
    """P1 page evidence also fires when a member file is a PAGE-method
    routes_index entry (SPA/module views: cal.com forgot-password-view)."""
    _arm(monkeypatch)
    uf = _uf("UF-054", "Fetch reset password view", members=["f1"])
    entry = "apps/web/modules/auth/forgot-password/forgot-password-view.tsx"
    flows = {"f1": _FlowStub("f1", entry, [entry])}
    tele = _run([uf], flows, page_ri={entry})
    assert uf.category == "interactive"
    assert tele["abstained"][0]["reason"] == "page_surface"


def test_anti_routed_uf_survives(monkeypatch) -> None:
    """A plumbing-named, micro, page-less UF that carries ROUTES is a product
    surface (cal.com 'Run alls' routes=41 / 'Run forms' routes=10): KEEP."""
    _arm(monkeypatch)
    uf = _uf("UF-055", "Run jobs", members=["f1"], routes=["api/a", "api/b"])
    tele = _run([uf], {"f1": _FlowStub("f1", "trpc/jobs.ts", ["trpc/jobs.ts"])})
    assert uf.category == "interactive"
    ab = [a for a in tele["abstained"] if a["id"] == "UF-055"]
    assert ab and ab[0]["reason"] == "routed"


def test_backstop_journey_with_members_reclassifies(monkeypatch) -> None:
    """The cal.com 'Run <Vendor> jobs' x25 are SYNTHESIZED 6.7d backstop
    journeys (synthesis_reason='uncovered_product_feature_backstop') WITH
    members — they ARE the primary Seg F target and MUST reclassify. I8
    stays met (the PF's UF is counted by product_feature_id, not category)."""
    _arm(monkeypatch)
    uf = _uf("UF-115", "Run Alby jobs", members=["f1"], synthesized=True,
             synthesis_reason="uncovered_product_feature_backstop")
    flows = {"f1": _FlowStub("f1", "packages/app-store/alby/api/index.ts")}
    tele = _run([uf], flows)
    assert uf.category == "system"
    assert uf.product_feature_id == "pf-x"      # binding untouched -> I8 met
    assert tele["count"] == 1


def test_anti_marker_lane_e2e_memberless_rows_skipped(monkeypatch) -> None:
    """Coverage markers / lane rows / e2e-recall / member-LESS seeds are
    OTHER channels (never re-typed here). Synthesized backstops WITH members
    are NOT in this skip set (see the reclassify test above)."""
    _arm(monkeypatch)
    ufs = [
        _uf("UF-060", "Fetch specs", members=["f"], is_coverage_marker=True),
        _uf("UF-062", "Fetch specs", members=["f"], lane_ref="lane-1"),
        _uf("UF-063", "Fetch specs", members=["f"], synthesized=True,
            synthesis_reason="e2e_journey_recall"),
        _uf("UF-064", "Fetch specs", members=[], mc=0),   # member-less seed
    ]
    tele = _run(ufs, {"f": _FlowStub("f", "lib/s.ts")})
    assert all(u.category == "interactive" for u in ufs)
    assert tele["candidates"] == 0


# ════════════════════════════════════════════════════════════════════════
# conservation + kill-switch + registration
# ════════════════════════════════════════════════════════════════════════

def test_conservation_membership_untouched(monkeypatch) -> None:
    _arm(monkeypatch)
    members = ["f1"]
    uf = _uf("UF-070", "Run Stripe jobs", members=members)
    before_ids = list(uf.member_flow_ids)
    before_pf = uf.product_feature_id
    _run([uf], {"f1": _FlowStub("f1", "packages/app-store/stripe/api/index.ts")})
    assert uf.category == "system"
    assert uf.member_flow_ids == before_ids       # membership READ-ONLY
    assert uf.product_feature_id == before_pf      # pf binding untouched
    assert uf.name == "Run Stripe jobs"            # name frozen


def test_killswitch_off_is_inert(monkeypatch) -> None:
    monkeypatch.delenv(PLUMBING_UF_RECLASS_ENV, raising=False)
    load_plumbing_vocab.cache_clear()
    assert plumbing_uf_reclass_enabled() is False
    uf = _uf("UF-080", "Run Stripe jobs", members=["f1"])
    tele = _run([uf], {"f1": _FlowStub("f1", "packages/app-store/stripe/api/index.ts")})
    assert uf.category == "interactive"            # untouched
    assert tele == {"enabled": False, "candidates": 0, "reclassified": [],
                    "abstained": [], "by_verb": {}}


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("0", False), ("false", False), ("off", False), ("", False),
])
def test_flag_parsing(monkeypatch, val: str, expected: bool) -> None:
    monkeypatch.setenv(PLUMBING_UF_RECLASS_ENV, val)
    assert plumbing_uf_reclass_enabled() is expected


def test_flag_registered_in_env_output_flags() -> None:
    assert PLUMBING_UF_RECLASS_ENV in ENV_OUTPUT_FLAGS


def test_telemetry_deterministic_order(monkeypatch) -> None:
    _arm(monkeypatch)
    # deliberately out-of-id-order input; telemetry must sort by id
    ufs = [
        _uf("UF-303", "Run Attio jobs", members=["c"]),
        _uf("UF-101", "Run Stripe jobs", members=["a"]),
        _uf("UF-202", "Run MCP operations", members=list("vwxyz")),  # abstain
    ]
    flows = {"a": _FlowStub("a", "packages/app-store/stripe/api/index.ts"),
             "c": _FlowStub("c", "packages/app-store/attio/api/index.ts")}
    flows.update({f: _FlowStub(f, f"lib/{f}.ts") for f in "vwxyz"})
    tele = _run(ufs, flows)
    assert [r["id"] for r in tele["reclassified"]] == ["UF-101", "UF-303"]
    assert [a["id"] for a in tele["abstained"]] == ["UF-202"]
