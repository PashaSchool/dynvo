"""S2 Seg A — deterministic UF pre-clustering (``FAULTLINE_UF_DET_AGGREGATION``).

The two probe-verified structure failures this kills (2026-07-18):
FAIL-OPEN (LLM dies mid-scan → raw rollup passes through: 264 UFs on the
degraded Soc0 board vs 78-82 healthy) and RESAMPLE (1 drifting flow of 980
flips the whole-batch key → −26% UF count). Under the flag the journey
STRUCTURE is deterministic (one conservation-complete cluster per rollup
domain) and the LLM layer only NAMES it, so UF-COUNT is invariant to both.

Calibration (real 13:25Z artifacts, run outside these tests, $0): 292 rollup
UFs → 82 clusters vs the healthy run's 82 journey UFs (grain exact);
member-universe conserved (966 uuids, scattered=0); member-set Jaccard vs the
Sonnet partition mean 0.52 restricted / 0.36 raw — the 0.8 target is
structurally unreachable for ANY conserving clusterer (the healthy 6.7d run
drops 186/292 rollup UFs semantically; oracle atomic grouping tops at 63%).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
    DET_AGGREGATION_ENV,
    aggregate_user_flows,
    det_aggregation_enabled,
)
from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS


def _uf(
    uf_id: str,
    domain: str,
    members: list[str],
    *,
    intent: str = "manage",
    resource: str = "thing",
    pf: str | None = "pf-a",
    routes: list[str] | None = None,
) -> UserFlow:
    return UserFlow(
        id=uf_id,
        name=f"Manage {resource} ({uf_id})",
        domain=domain,
        product_feature_id=pf,
        intent=intent,
        resource=resource,
        member_flow_ids=members,
        member_count=len(members),
        routes=routes or [],
    )


def _fixture() -> list[UserFlow]:
    """3 domains: alpha (3 UFs), beta (2 UFs), gamma (1 UF) — 6 rollup UFs,
    like a miniature of the Soc0 292→82 shape."""
    return [
        _uf("UF-001", "alpha", ["f1", "f2"], intent="browse", resource="alpha-list"),
        _uf("UF-002", "alpha", ["f3"], intent="manage", resource="alpha-item"),
        _uf("UF-003", "alpha", ["f4", "f5", "f6"], intent="manage",
            resource="alpha-bulk", routes=["api/alpha.py"]),
        _uf("UF-004", "beta", ["g1", "g2"], intent="author", resource="beta-doc"),
        _uf("UF-005", "beta", ["g3"], intent="author", resource="beta-draft"),
        _uf("UF-006", "gamma", ["h1"], intent="execute", resource="gamma-run", pf=None),
    ]


# ── grain: one cluster per rollup domain ────────────────────────────────────


def test_domain_grain_cluster_count() -> None:
    clusters, tele = aggregate_user_flows(_fixture())
    assert len(clusters) == 3            # alpha, beta, gamma
    assert tele["clusters"] == 3
    assert tele["domains"] == 3
    assert tele["input_ufs"] == 6
    assert tele["merged_clusters"] == 2  # alpha(3), beta(2)
    assert tele["singleton_clusters"] == 1


def test_cluster_fields_are_deterministically_grounded() -> None:
    clusters, _ = aggregate_user_flows(_fixture())
    by_dom = {c.domain: c for c in clusters}
    alpha = by_dom["alpha"]
    # dominant constituent = largest member_count (UF-003, mc=3)
    assert alpha.name == "Manage alpha-bulk (UF-003)"
    assert alpha.resource == "alpha-bulk"
    assert alpha.member_flow_ids == ["f1", "f2", "f3", "f4", "f5", "f6"]
    assert alpha.member_count == 6
    assert alpha.intent == "manage"      # majority 2/3
    assert alpha.routes == ["api/alpha.py"]
    assert alpha.refined is False and alpha.ui_tier is None
    # ids renumbered canonically over sorted domains
    assert [c.id for c in clusters] == ["UF-001", "UF-002", "UF-003"]
    assert [c.domain for c in clusters] == ["alpha", "beta", "gamma"]


# ── fail-open: LLM dead → count invariant (the 264-vs-78 class dies) ────────


def _dead_client() -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                raise RuntimeError("dead key")

        messages = _Messages()

    return _Client()


def _flow(name: str) -> Flow:
    return Flow(
        name=name, uuid=name, paths=["x.py"], authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=100.0,
        test_files=[], participants=[],
    )


def test_fail_open_llm_dead_count_invariant() -> None:
    """The refiner (naming layer) dying does NOT change the UF count: the
    clusters pass through with deterministic names — the fail-open class
    (raw 292/264 leaking to the board) is structurally impossible."""
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    clusters, _ = aggregate_user_flows(_fixture())
    n_structural = len(clusters)
    flows = [_flow(m) for c in clusters for m in c.member_flow_ids]
    refined, tel = refine_user_flows(clusters, flows, client=_dead_client())
    assert len(refined) == n_structural           # count invariant
    assert tel["domains_degraded"] == tel["domains_total"]
    assert all(uf.refined is False for uf in refined)
    assert all(uf.member_count > 0 for uf in refined)


def test_fail_open_count_equals_healthy_grain_not_raw() -> None:
    """The structural count = domain count (the healthy grain), NOT the raw
    rollup count — with a dead LLM the board gets 3 UFs here, not 6."""
    ufs = _fixture()
    clusters, _ = aggregate_user_flows(ufs)
    assert len(clusters) == 3 and len(ufs) == 6


# ── resample: structure invariant to input order / draw order ───────────────


def test_resample_invariance_input_order() -> None:
    """Any input permutation yields byte-identical clusters — the −26%
    resample class (order/draw-dependent structure) is dead."""
    base = aggregate_user_flows(_fixture())[0]
    for perm in (list(reversed(_fixture())),
                 sorted(_fixture(), key=lambda u: u.resource),
                 _fixture()[3:] + _fixture()[:3]):
        got = aggregate_user_flows(perm)[0]
        assert [c.model_dump() for c in got] == [c.model_dump() for c in base]


def test_pythonhashseed_invariance() -> None:
    """Cluster output is identical under PYTHONHASHSEED=0 and =1 (set-iteration
    noise law: sorted() everywhere)."""
    prog = (
        "import json;"
        "from tests.test_det_aggregation import _fixture;"
        "from faultline.pipeline_v2.stage_6_7a_det_aggregation import aggregate_user_flows;"
        "cs, t = aggregate_user_flows(_fixture());"
        "print(json.dumps([c.model_dump(mode='json') for c in cs], sort_keys=True))"
    )
    outs = []
    for seed in ("0", "1"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run(
            [sys.executable, "-c", prog], capture_output=True, text=True,
            env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout)
    assert outs[0] == outs[1]


# ── conservation: fate-tally zero scattered ─────────────────────────────────


def test_conservation_fate_tally_zero_scattered() -> None:
    ufs = _fixture()
    clusters, tele = aggregate_user_flows(ufs)
    assert tele["scattered"] == 0
    # every input UF is named in the fate map
    assert set(tele["fate"]) == {u.id for u in ufs}
    # member universe conserved exactly
    u_in = set().union(*(set(u.member_flow_ids) for u in ufs))
    u_out = set().union(*(set(c.member_flow_ids) for c in clusters))
    assert u_in == u_out
    assert tele["members_in"] == tele["members_out"] == len(u_in)


def test_empty_input_is_clean() -> None:
    clusters, tele = aggregate_user_flows([])
    assert clusters == [] and tele["clusters"] == 0 and tele["scattered"] == 0


# ── Class 1 (wave-gauntlet, 2026-07-18) — channel rows pass through ─────────
#
# The rollup input carries member-less system seeds
# (synthesis_reason="system_flow_recall", 'Run <domain>'), coverage markers
# and other synthesized rows. Folding them stripped synthesis_reason so the
# downstream demote/gap machinery went blind: the board shipped mc=0 rows
# without is_coverage_marker (B23-contract violation, Soc0 wave 0->9).
# Channel rows are NOT journey structure — they pass through verbatim.


def _seed(uf_id: str, domain: str) -> UserFlow:
    return UserFlow(
        id=uf_id, name=f"Run {domain}", domain=domain,
        product_feature_id="pf-a", intent="execute", resource=domain,
        member_flow_ids=[], member_count=0,
        category="system", trigger="queue",
        synthesized=True, synthesis_reason="system_flow_recall",
    )


def test_memberless_system_seed_passes_through_with_channel_fields() -> None:
    """The 'Run <domain>' seed survives UNFOLDED with synthesis_reason,
    category and trigger intact — the synth-quality demote recognises it."""
    ufs = _fixture() + [_seed("UF-142", "articles")]
    out, tele = aggregate_user_flows(ufs)
    assert tele["passthrough_channel_rows"] == 1
    seeds = [u for u in out if u.synthesis_reason == "system_flow_recall"]
    assert len(seeds) == 1
    s = seeds[0]
    assert s.name == "Run articles" and s.member_count == 0
    assert s.category == "system" and s.trigger == "queue"
    assert s.synthesized is True
    # its domain minted NO cluster (it was the domain's only row)
    assert not any(
        c.domain == "articles" and c.synthesis_reason is None for c in out
    )


def test_marker_row_passes_through_flagged() -> None:
    marker = UserFlow(
        id="UF-090", name="Uncovered: alpha routes", domain="alpha",
        product_feature_id="pf-a", intent="other", resource="alpha",
        member_flow_ids=[], member_count=0,
        synthesized=True, is_coverage_marker=True,
    )
    out, tele = aggregate_user_flows(_fixture() + [marker])
    kept = [u for u in out if u.is_coverage_marker]
    assert len(kept) == 1 and kept[0].member_count == 0
    assert tele["passthrough_channel_rows"] == 1


def test_no_memberless_cluster_can_exist() -> None:
    """A seed sharing a domain with organic UFs: the organics cluster, the
    seed passes through — every emitted cluster has members (mc=0 in
    user_flows[] can only be a flagged/tagged channel row)."""
    ufs = _fixture() + [_seed("UF-143", "alpha")]
    out, _ = aggregate_user_flows(ufs)
    for u in out:
        if not (u.synthesis_reason or u.is_coverage_marker):
            assert u.member_count > 0 and u.member_flow_ids


def test_synthesized_memberful_row_passes_through_intact() -> None:
    """A member-FUL synthesized row (e2e recall class) is a channel row too —
    its members and reason survive verbatim (never absorbed)."""
    recall = UserFlow(
        id="UF-150", name="Sign up and onboard", domain="alpha",
        product_feature_id="pf-a", intent="author", resource="onboarding",
        member_flow_ids=["e1", "e2"], member_count=2,
        synthesized=True, synthesis_reason="e2e_journey_recall",
    )
    out, _ = aggregate_user_flows(_fixture() + [recall])
    kept = [u for u in out if u.synthesis_reason == "e2e_journey_recall"]
    assert len(kept) == 1
    assert set(kept[0].member_flow_ids) == {"e1", "e2"}
    # alpha's organic cluster did NOT absorb the recall members
    alpha = next(u for u in out
                 if u.domain == "alpha" and not u.synthesis_reason)
    assert not ({"e1", "e2"} & set(alpha.member_flow_ids))


def test_passthrough_ids_unique_and_canonical() -> None:
    ufs = _fixture() + [_seed("UF-142", "articles"), _seed("UF-143", "zeta")]
    out, tele = aggregate_user_flows(ufs)
    ids = [u.id for u in out]
    assert len(ids) == len(set(ids))                    # no collisions
    assert all(i.startswith("UF-") for i in ids)        # canonical format
    # fate covers EVERY input row (clusters + passthrough)
    assert set(tele["fate"]) == {u.id for u in ufs}
    assert tele["scattered"] == 0


# ── kill-switch + registration ──────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DET_AGGREGATION_ENV, raising=False)
    assert det_aggregation_enabled() is False


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_flag_kill_switch_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv(DET_AGGREGATION_ENV, val)
    assert det_aggregation_enabled() is False


def test_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DET_AGGREGATION_ENV, "1")
    assert det_aggregation_enabled() is True


def test_flag_registered_in_env_output_flags() -> None:
    assert "FAULTLINE_UF_DET_AGGREGATION" in ENV_OUTPUT_FLAGS


# ── iter-3 (panel-spot blockers, 2026-07-18) — readability regrain ──────────

from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
    READABILITY_MC_BAR,
    collapse_crud_action_children,
    readability_regrain,
    sanitize_lattice_domains,
    split_oversized_ufs,
)
from faultline.models.types import Flow as _Flow
from datetime import datetime as _dt, timezone as _tz


def _verbs():
    from faultline.pipeline_v2.naming_contract import (
        _verb_class_tokens, load_naming_vocab,
    )
    return _verb_class_tokens(load_naming_vocab())


def _mkflow(name: str, entry: str = "backend/routers/cases.py") -> _Flow:
    return _Flow(
        name=name, uuid=name, paths=[entry], authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_dt(2026, 1, 1, tzinfo=_tz.utc), health_score=90.0,
        test_files=[], entry_point_file=entry,
    )


def _big_cluster(uf_id: str, name: str, flow_names: list[str],
                 *, pf: str = "cases", domain: str = "case") -> tuple[UserFlow, list[_Flow]]:
    flows = [_mkflow(n) for n in flow_names]
    uf = UserFlow(
        id=uf_id, name=name, domain=domain, product_feature_id=pf,
        intent="manage", resource=domain,
        member_flow_ids=[f.uuid for f in flows],
        member_count=len(flows),
    )
    return uf, flows


_SEND_CASES_MEMBERS = [
    # the exact exhibit shape: core CRUD + bulk/export/comments/remediation
    "create-cases-flow", "list-cases-flow", "search-cases-flow",
    "update-case-by-case-id-flow", "delete-case-by-case-id-flow",
    "view-case-by-case-id-flow", "browse-case-signals-flow",
    "view-case-signal-by-signal-id-flow",
    "create-cases-bulk-flow", "create-cases-bulk-update-flow",
    "export-cases-flow", "view-case-export-flow",
    "browse-cases-export-artifacts-flow",
    "create-case-comments-flow", "browse-case-comments-flow",
    "create-case-remediation-flow", "update-case-remediation-by-item-id-flow",
    "view-case-remediation-flow", "view-case-timeline-flow",
    "create-case-timeline-flow", "view-case-activity-flow",
    "view-cases-summary-flow", "browse-cases-pivot-counts-flow",
    "update-case-verdict-flow", "view-case-investigation-status-flow",
    "view-case-related-flow", "search-case-content-flow",
]


def test_send_cases_shape_split_names_reflect_bulk_export() -> None:
    """Panel B1 exhibit: the 'Send cases' 33-member whole-router cluster
    splits into member-named families — bulk and export are VISIBLE as
    first-class rows, and the residual is renamed from ITS OWN members
    ('Manage cases'), never keeping the one-member misname."""
    uf, flows = _big_cluster("UF-L-d1d1bc65b3", "Send cases", _SEND_CASES_MEMBERS)
    ufs = [uf]
    tele = readability_regrain(ufs, flows, _verbs())
    names = {u.name for u in ufs}
    assert any("export" in n.lower() for n in names)     # export family visible
    assert any("bulk" in n.lower() for n in names)       # bulk family visible
    assert "Send cases" not in names                     # misname dead
    parent = next(u for u in ufs if u.id == "UF-L-d1d1bc65b3")
    assert parent.name == "Manage cases"                 # renamed from members
    assert all((u.member_count or 0) < READABILITY_MC_BAR for u in ufs)
    assert tele["members_lost"] == 0


def test_findings_detector_misfile_family_surfaces_detector_name() -> None:
    """Panel B1 exhibit 2: detector-ops flows living inside a findings-PF
    cluster surface under their HONEST object name ('… detectors'), and no
    surviving row named '… findings' holds a detector majority."""
    members = (
        [f"view-finding-variant-{i}-flow" for i in range(13)]
        + ["create-detector-bulk-approve-flow", "update-detector-status-flow",
           "create-detector-deploy-flow", "view-detector-overview-flow",
           "create-detector-resync-flow", "update-detector-rules-flow",
           "view-detector-runs-flow", "create-detector-suppress-flow",
           "view-detector-metrics-flow", "update-detector-config-flow",
           "create-detector-test-flow", "view-detector-health-flow",
           "browse-detector-alerts-flow"]
    )
    uf, flows = _big_cluster(
        "UF-L-361956e671", "Create findings", members,
        pf="findings", domain="finding",
    )
    ufs = [uf]
    readability_regrain(ufs, flows, _verbs())
    det_rows = [u for u in ufs if "detector" in (u.name or "").lower()]
    assert det_rows, "detector family must surface first-class"
    fname = {f.uuid: f.name for f in flows}
    for u in ufs:
        if "finding" in (u.name or "").lower():
            det = sum(1 for m in u.member_flow_ids
                      if "detector" in fname.get(m, ""))
            assert det * 2 <= len(u.member_flow_ids)   # no detector-majority row named findings


def test_crud_leaves_collapse_to_manage_resource() -> None:
    """Panel B2: lattice ACTION-axis siblings (create/update/delete of one
    resource, same router file) collapse into ONE 'Manage <resource>s'."""
    kids = []
    all_flows: list[_Flow] = []
    for verb, n in (("create", 3), ("update", 2), ("delete", 2)):
        names = [f"{verb}-finding-item-{i}-flow" for i in range(n)]
        uf, fl = _big_cluster(
            f"UF-L-{verb}f", f"{verb.title()} findings", names,
            pf="findings", domain=f"lattice:action:finding-{verb}",
        )
        kids.append(uf); all_flows.extend(fl)
    ufs = list(kids)
    fbid = {f.uuid: f for f in all_flows}
    tele = collapse_crud_action_children(ufs, fbid, _verbs())
    assert tele["collapsed_children"] == 2
    assert len(ufs) == 1
    assert ufs[0].name == "Manage findings"
    assert ufs[0].member_count == 7                     # conservation: union


def test_route_anchored_crud_leaf_survives_collapse() -> None:
    """Panel B2 anti-case: a sibling whose members live in ITS OWN router
    file (a genuinely separate surface) is route-anchored and SURVIVES."""
    uf_a, fl_a = _big_cluster(
        "UF-L-createx", "Create exports", ["create-export-item-1-flow",
                                          "create-export-item-2-flow"],
        pf="exports", domain="lattice:action:export-create",
    )
    uf_c, fl_c = _big_cluster(
        "UF-L-updx", "Update exports", ["update-export-item-1-flow",
                                        "update-export-item-2-flow"],
        pf="exports", domain="lattice:action:export-update",
    )
    anchored_names = ["delete-export-purge-1-flow", "delete-export-purge-2-flow"]
    fl_b = [_mkflow(n, entry="backend/routers/purge.py") for n in anchored_names]
    uf_b = UserFlow(
        id="UF-L-delx", name="Delete exports",
        domain="lattice:action:export-delete", product_feature_id="exports",
        intent="manage", resource="export",
        member_flow_ids=[f.uuid for f in fl_b], member_count=2,
    )
    ufs = [uf_a, uf_c, uf_b]
    fbid = {f.uuid: f for f in fl_a + fl_c + fl_b}
    tele = collapse_crud_action_children(ufs, fbid, _verbs())
    assert tele["route_anchored_kept"] == 1               # only the purge leaf
    ids = {u.id for u in ufs}
    assert "UF-L-delx" in ids                             # anchored survives
    assert len(ids) == 2                                  # create+update merged
    merged = next(u for u in ufs if u.id != "UF-L-delx")
    assert merged.name == "Manage exports" and merged.member_count == 4


def test_lattice_token_never_ships_in_domain() -> None:
    """Panel B2 display law: no shipped row carries the raw 'lattice:*'
    domain token; the resource takes its place."""
    uf = UserFlow(
        id="UF-L-x1", name="Manage API keys", domain="lattice:route:api-key",
        product_feature_id="mssp", intent="manage", resource="api-key",
        member_flow_ids=["m1"], member_count=1,
    )
    n = sanitize_lattice_domains([uf])
    assert n == 1 and uf.domain == "api-key"
    # and through the full pass (incl. the hardened case where the RESOURCE
    # itself carries the raw token — never copied into domain):
    uf2, flows = _big_cluster("UF-L-y2", "Manage hunts", ["view-hunt-a-flow"],
                              domain="lattice:dir:threat-hunt")
    uf2.resource = "threat-hunt"
    uf3 = UserFlow(
        id="UF-L-y3", name="Manage keys", domain="lattice:route:api-key",
        product_feature_id="mssp", intent="manage",
        resource="lattice:route:api-key",
        member_flow_ids=["k1"], member_count=1,
    )
    ufs = [uf2, uf3]
    readability_regrain(ufs, flows, _verbs())
    assert not any(str(u.domain or "").startswith("lattice:") for u in ufs)
    assert uf3.domain is None


def test_autopilot_spine_family_surfaces_first_class() -> None:
    """Panel B3: a spine family (autopilot) buried inside an oversized
    cluster resurfaces as a FIRST-CLASS row named under its own object."""
    members = (
        [f"view-setting-panel-{i}-flow" for i in range(24)]
        + ["update-autopilot-mode-flow", "view-autopilot-status-flow",
           "create-autopilot-exclusion-flow"]
    )
    uf, flows = _big_cluster("UF-030", "Manage settings", members,
                             pf="settings", domain="setting")
    ufs = [uf]
    readability_regrain(ufs, flows, _verbs())
    auto = [u for u in ufs if "autopilot" in (u.name or "").lower()]
    assert auto and auto[0].member_count == 3


def test_regrain_conservation_and_channel_rows_untouched() -> None:
    """Fate law: members before == after; a marker/seed row is never split
    or renamed by the regrain."""
    uf, flows = _big_cluster("UF-100", "Send cases", _SEND_CASES_MEMBERS)
    seed = _seed("UF-142", "articles")
    ufs = [uf, seed]
    tele = readability_regrain(ufs, flows, _verbs())
    assert tele["members_lost"] == 0
    kept_seed = next(u for u in ufs if u.synthesis_reason)
    assert kept_seed.name == "Run articles" and kept_seed.member_count == 0


def test_api_prefix_raw_names_split_at_object_grain() -> None:
    """Fresh-pipeline regression (it3): at regrain time flow names are
    PRE-rename route derivations ('api-admin-chat…'), so 'api' must be
    stripped as route scaffolding — the split lands at the OBJECT grain
    (admin/chat…), never minting a 41-member 'Manage API admins' blob."""
    members = (
        [f"view-api-admin-chat-conversation-{i}-flow" for i in range(10)]
        + [f"create-api-admin-exec-brief-demo-{i}-flow" for i in range(9)]
        + [f"update-api-admin-mock-investigation-{i}-flow" for i in range(8)]
    )
    uf, flows = _big_cluster("UF-001", "API admin ops", members,
                             pf="network-security", domain="admin")
    ufs = [uf]
    readability_regrain(ufs, flows, _verbs())
    assert all((u.member_count or 0) < READABILITY_MC_BAR for u in ufs)
    names = " | ".join(u.name for u in ufs)
    assert "API" not in names.replace("API admin ops", "")  # api never a family
    low = names.lower()
    assert "chat" in low and ("exec" in low or "brief" in low)


def test_threat_hunt_single_family_deepens_to_workflow_grain() -> None:
    """Panel B1 exhibit 3: 'Manage threat hunts' hid the hypothesis/article/
    phase workflow — a DEGENERATE depth-0 partition (every member families
    at 'hunt') advances one token and surfaces the workflow families."""
    members = (
        [f"view-threat-hunt-article-{i}-flow" for i in range(10)]
        + [f"create-threat-hunt-hypothesis-{i}-flow" for i in range(9)]
        + [f"update-threat-hunt-phase-{i}-flow" for i in range(8)]
    )
    uf, flows = _big_cluster("UF-057", "Manage threat hunts", members,
                             pf="threat-hunts", domain="threat_hunt")
    ufs = [uf]
    readability_regrain(ufs, flows, _verbs())
    assert all((u.member_count or 0) < READABILITY_MC_BAR for u in ufs)
    low = " | ".join(u.name for u in ufs).lower()
    assert "article" in low and "hypothes" in low and "phase" in low
    # the qualifier path survives in the child names ('threat hunt …')
    assert "threat hunt article" in low


# ── it5 (panel re-spot, 2026-07-18) — three named punch-list items ──────────

from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
    merge_same_object_siblings,
    reclassify_service_internals,
    rename_raw_resource_rows,
)


def test_mc32_shape_forensics_fold_respects_readability_bar() -> None:
    """it5-1a forensics lock: UF-058 was a readable 19 at regrain time and
    grew to 32 via bar-blind B71 folds AFTER the split had run. Under the
    Seg A world apply_uf_synth_fold refuses any fold whose union crosses
    READABILITY_MC_BAR — the loser survives, no post-regrain giant."""
    from faultline.pipeline_v2.uf_synth_fold import apply_uf_synth_fold
    from faultline.pipeline_v2.naming_contract import (
        _verb_class_tokens, load_naming_vocab,
    )
    verbs = _verb_class_tokens(load_naming_vocab())
    big = UserFlow(
        id="UF-058", name="Manage threat hunts", product_feature_id="threat-hunts",
        intent="manage", resource="threat-hunt",
        member_flow_ids=[f"th-{i}" for i in range(19)], member_count=19,
    )
    echo = UserFlow(
        id="UF-070", name="Threat hunts", product_feature_id="threat-hunts",
        intent="manage", resource="threat-hunt",
        member_flow_ids=[f"da-{i}" for i in range(13)], member_count=13,
    )
    from types import SimpleNamespace
    pf = SimpleNamespace(name="threat-hunts", display_name="Threat hunts")
    ufs = [big, echo]
    tele = apply_uf_synth_fold(ufs, [], [pf], verbs,
                               max_winner_members=READABILITY_MC_BAR)
    assert tele["folds_refused_readability_bar"] == 1
    assert {u.id for u in ufs} == {"UF-058", "UF-070"}     # both survive
    assert big.member_count == 19                          # no giant re-mint
    # unlimited (non-Seg-A callers): byte-identical legacy fold
    ufs2 = [
        big.model_copy(deep=True), echo.model_copy(deep=True),
    ]
    tele2 = apply_uf_synth_fold(ufs2, [], [pf], verbs)
    assert tele2["folds_refused_readability_bar"] == 0
    assert len(ufs2) == 1 and ufs2[0].member_count == 32   # the it4 defect shape


def test_raw_route_resource_renames_from_all_members() -> None:
    """it5-1b: a row whose resource is a raw deep route id (the tell that ONE
    member captured the identity) is renamed from ALL members —
    'Manage threat hunts, <top-fam> & <top-fam>' class."""
    names = (
        [f"create-threat-hunts-feed-poll-{i}-flow" for i in range(5)]
        + [f"browse-threat-hunts-feeds-{i}-flow" for i in range(4)]
        + [f"browse-threat-hunts-articles-{i}-flow" for i in range(5)]
        + [f"create-threat-hunts-hunt-run-{i}-flow" for i in range(3)]
        + ["create-threat-hunts-article-adopt-detectors-flow"]
    )
    uf, flows = _big_cluster("UF-058", "Create detectors from threat hunt articles",
                             names, pf="threat-hunts", domain="threat_hunt")
    uf.resource = "api-threat-hunt-article-article-id-adopt-detector"
    fbid = {f.uuid: f for f in flows}
    tele = rename_raw_resource_rows([uf], fbid, _verbs())
    assert tele["renamed"] == 1
    assert uf.resource == "threat-hunts"                # compound object root
    # THE panel-requested shape, verbatim class:
    assert uf.name == "Manage threat hunts, feeds & articles"
    low = uf.name.lower()
    assert "adopt" not in low and "detector" not in low  # the ONE member lost the title


def test_handles_internals_reclassified_with_fate() -> None:
    """it5-2: a sizeable ROUTELESS row spanning heterogeneous service
    subsystems (the 'Manage handles' black hole) reclassifies to
    category=system with an honest 'Internal <domain> operations' name;
    members conserved, fate recorded."""
    names = [
        "handle-genai-queries-flow", "handle-filter-queries-flow",
        "handle-inventory-queries-flow", "fetch-threat-intelligence-flow",
        "analyze-ai-signals-flow", "run-focus-agent-flow",
        "tune-detector-beam-flow", "adopt-mitre-pack-flow",
    ]
    uf, flows = _big_cluster("UF-056", "Manage handles", names,
                             pf="insights", domain="service")
    uf.routes = []
    fbid = {f.uuid: f for f in flows}
    before = set(uf.member_flow_ids)
    tele = reclassify_service_internals([uf], fbid, _verbs())
    assert tele["reclassified"] == 1
    assert tele["fate"] == {"UF-056": "system-internals"}
    assert uf.category == "system" and uf.ui_tier == "no-ui"
    assert uf.name == "Internal service operations"
    assert set(uf.member_flow_ids) == before             # conservation
    # anti-case: a ROUTE-backed row of one dominant surface is untouched
    uf2, flows2 = _big_cluster("UF-007", "Browse cases",
                               ["browse-cases-1-flow", "browse-cases-2-flow",
                                "view-cases-3-flow", "list-cases-4-flow"])
    uf2.routes = ["/api/cases"]
    tele2 = reclassify_service_internals([uf2], {f.uuid: f for f in flows2}, _verbs())
    assert tele2["reclassified"] == 0 and uf2.category == "interactive"


def test_same_object_siblings_collapse_b31() -> None:
    """it5-3: two lattice-born rows of one PF whose members share ONE object
    root (the 'Manage detectors' + 'Manage detectors (Findings)' byte-dup)
    merge into a single 'Manage detectors' row — no collision remains for
    naming to qualify with a suffix."""
    a_names = [f"create-detector-item-{i}-flow" for i in range(8)]
    b_names = [f"view-detector-thing-{i}-flow" for i in range(6)]
    uf_a, fl_a = _big_cluster("UF-L-361956e671", "Manage findings", a_names,
                              pf="findings", domain="finding")
    uf_a.resource = "detector"          # the late-time truth (post-fold state)
    uf_b, fl_b = _big_cluster("UF-L-raa7821d504", "Manage detectors", b_names,
                              pf="findings", domain="detector")
    # a qualifier-family child of the same object must NOT join the merge:
    uf_c, fl_c = _big_cluster("UF-L-r7e4988deb5", "Manage detector coverages",
                              ["view-detector-coverage-1-flow",
                               "view-detector-coverage-2-flow"],
                              pf="findings", domain="detector")
    uf_c.resource = "coverage"
    ufs = [uf_a, uf_b, uf_c]
    fbid = {f.uuid: f for f in fl_a + fl_b + fl_c}
    tele = merge_same_object_siblings(ufs, fbid, _verbs())
    assert tele["merged_rows"] == 1
    ids = {u.id for u in ufs}
    assert "UF-L-r7e4988deb5" in ids                     # child untouched
    assert len(ufs) == 2
    merged = next(u for u in ufs if u.id != "UF-L-r7e4988deb5")
    assert merged.name == "Manage detectors" and "(" not in merged.name
    assert merged.member_count == 14                     # union, conserved


def test_different_route_objects_stay_separate() -> None:
    """it5-3 anti-case: lattice siblings over DIFFERENT objects never merge."""
    uf_a, fl_a = _big_cluster("UF-L-obj1", "Manage cases",
                              [f"create-case-{i}-flow" for i in range(4)],
                              pf="ops", domain="case")
    uf_b, fl_b = _big_cluster("UF-L-obj2", "Manage findings",
                              [f"view-finding-{i}-flow" for i in range(4)],
                              pf="ops", domain="finding")
    ufs = [uf_a, uf_b]
    tele = merge_same_object_siblings(
        ufs, {f.uuid: f for f in fl_a + fl_b}, _verbs(),
    )
    assert tele["merged_rows"] == 0 and len(ufs) == 2


# ── it6 (panel round 3) — user-reachable veto on internals reclassify ───────


def test_integrations_frontend_shape_never_reclassified() -> None:
    """it6-1: the panel overshoot exhibit — 20 user-facing config flows
    anchored in frontend/src/features (+ *Page.tsx) form a CORE product
    capability; routeless + heterogeneous-root or not, the row is NEVER
    reclassified to system (post-UF demote must not eat user-facing)."""
    vendor = ["crowdstrike", "defender", "sentinel", "zscaler", "aws",
              "servicenow", "cortex", "deepseas", "claroty", "okta"]
    names = [f"configure-{v}-integration-flow" for v in vendor] \
        + [f"load-{v}-integrations-flow" for v in vendor[:9]] \
        + ["load-integrations-flow"]
    flows = [
        _mkflow(n, entry=f"frontend/src/features/integrations/dialogs/D{i}.tsx")
        for i, n in enumerate(names)
    ]
    flows[-1] = _mkflow(names[-1], entry="frontend/src/features/integrations/IntegrationsPage.tsx")
    uf = UserFlow(
        id="UF-020", name="Configure and manage integrations",
        domain="features_integration", product_feature_id="integrations",
        intent="manage", resource="integration",
        member_flow_ids=[f.uuid for f in flows], member_count=len(flows),
        routes=[],
    )
    fbid = {f.uuid: f for f in flows}
    tele = reclassify_service_internals([uf], fbid, _verbs())
    assert tele["reclassified"] == 0
    assert tele["ui_vetoed"] == 1
    assert uf.category == "interactive"
    assert uf.name == "Configure and manage integrations"   # name untouched


def test_handles_backend_shape_still_reclassifies_post_veto() -> None:
    """it6-1 anti-case: the handles black hole (backend/services roots,
    zero UI anchors) STILL reclassifies to system after the veto."""
    names = [
        "handle-genai-queries-flow", "handle-filter-queries-flow",
        "handle-inventory-queries-flow", "fetch-threat-intelligence-flow",
        "analyze-ai-signals-flow", "run-focus-agent-flow",
        "tune-detector-beam-flow", "adopt-mitre-pack-flow",
    ]
    flows = [
        _mkflow(n, entry=f"backend/services/sub{i}/impl.py")
        for i, n in enumerate(names)
    ]
    uf = UserFlow(
        id="UF-051", name="Manage handles", domain="service",
        product_feature_id="insights", intent="manage", resource="handle",
        member_flow_ids=[f.uuid for f in flows], member_count=len(flows),
        routes=[],
    )
    tele = reclassify_service_internals([uf], {f.uuid: f for f in flows}, _verbs())
    assert tele["reclassified"] == 1 and tele["ui_vetoed"] == 0
    assert uf.category == "system"
    assert uf.name == "Internal service operations"
