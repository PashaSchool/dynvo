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
