"""Unit tests for the Stage 6.7d JPF structural anchor (MISSION-92 lever #3).

Second arming prong for the grain-contract gate: a Call-1 draw that emits
MORE journeys than the digest's distinct flow resources AND more capabilities
than the deterministic product layer performed no grain lift on either axis
(the journeys-per-capability contract in two-axis form — the naive
"draw j/pf > digest j/pf prior" is gameable because inflated draws inflate
their own PF denominator too; inbox-zero r3: 50 emitted PFs vs digest 31).

Covers: jpf-only arming + the anchor-naming corrective; either-axis lift
disarms (the dub 44<=45 / formbricks 66<=74 shapes); ratio-armed draws keep
the validated merge corrective verbatim; retry-keep + uncompressed flag
(never-worse); cost guard; the sparse-product-layer floor; the
resource-grain (fastapi expansion) protection; ratio-only pass_after_retry
semantics preserved via the restricted re-check; jpf telemetry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2 import stage_6_7d_llm_journey_abstraction as _mod
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    CONTRACT_ARMED_JPF,
    CONTRACT_ARMED_RATIO,
    CONTRACT_PASS,
    CONTRACT_PASS_AFTER_RETRY,
    CONTRACT_UNCOMPRESSED,
    DEFAULT_ABSTRACTION_MODEL,
    _distinct_pf_count,
    _jpf_armed,
    run_journey_abstraction,
)


# ── Fake Anthropic client (sequenced Call-1 draws) ──────────────────────────

@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Block:
    text: str


class _Msg:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text=text)]
        self.usage = _Usage(400, 200)


def _seq_client(abstraction_payloads: list[str], reattrib: str) -> Any:
    """abstraction_payloads[i] on the i-th Call-1 draw (last repeats);
    records every abstraction system prompt in ``state['systems']``."""
    state: dict[str, Any] = {"i": 0, "systems": []}

    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                if "assign each developer feature" in sysp:
                    return _Msg(reattrib)
                state["systems"].append(sysp)
                i = min(state["i"], len(abstraction_payloads) - 1)
                state["i"] += 1
                return _Msg(abstraction_payloads[i])
        messages = _M()

    c = _C()
    c.state = state  # type: ignore[attr-defined]
    return c


# ── Fixtures — scale ABOVE the viability floor (>=8 deterministic PFs) ──────
#
# Digest shape: 12 UFs over 4 distinct resources (redundancy: 4 < 0.9*12,
# ratio prong armed) + 8 deterministic PFs (== _MIN_ANCHORS_FLOOR).
# JPF priors: journeys axis 4 (distinct resources), capabilities axis 8.
# Ratio ceiling: 0.9*12 = 10.8 emitted UFs.

_RESOURCES = ["billing", "auth", "webhook", "team"]


def _feat(name: str, paths: list[str]) -> Feature:
    from faultline.models.types import MemberFile
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=paths, authors=["a"], total_commits=3, bug_fixes=1,
        bug_fix_ratio=0.33, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer",
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0) for p in paths],
    )


def _devs() -> list[Feature]:
    return [
        _feat("billing", ["app/billing/a.ts"]),
        _feat("auth", ["app/auth/login.ts"]),
        _feat("webhooks", ["app/webhooks/w.ts"]),
    ]


def _pfs(n: int = 8) -> list[Feature]:
    return [_feat(f"pf-{i}", [f"app/pf{i}/x.ts"]) for i in range(n)]


def _ufs() -> list[UserFlow]:
    out = []
    for i in range(12):
        out.append(UserFlow(
            id=f"UF-{i + 1:03d}", name=f"Flow {i + 1}", intent="author",
            resource=_RESOURCES[i % 4],
            member_flow_ids=[f"f{i + 1}"], member_count=1,
            routes=[f"/r{i + 1}"],
        ))
    return out


def _payload(n_ufs: int, n_pfs: int) -> str:
    """Draw with n_ufs journeys (each grounded via from_flows) + n_pfs caps."""
    pfs = [{"name": f"Cap {j}", "description": f"cap {j}"} for j in range(n_pfs)]
    ufs = [
        {"name": f"Journey {i}", "resource": _RESOURCES[i % 4],
         "product_feature": f"Cap {i % n_pfs}",
         "from_flows": [f"UF-{i + 1:03d}"]}
        for i in range(n_ufs)
    ]
    return json.dumps({"product_features": pfs, "user_flows": ufs})


# jpf-only inflation: 6 UFs (> 4 resources, but 6 < 10.8 so ratio-OK)
# + 9 caps (> 8 deterministic PFs).
_INFLATED = _payload(6, 9)
# journeys axis lifted: 4 UFs (== 4 resources, not above) — must NOT arm.
_LIFT_UF = _payload(4, 9)
# capabilities axis lifted (the dub shape): 6 UFs but 8 caps (== 8 PFs).
_LIFT_PF = _payload(6, 8)
# ratio violation too: 11 UFs (>= 10.8) + 9 caps -> armed by ratio AND jpf.
_RATIO_FAIL = _payload(11, 9)
# ratio violation with a lifted capability axis: 11 UFs + 8 caps -> ratio only.
_RATIO_ONLY = _payload(11, 8)
# a compressed, capability-anchored retry: 3 UFs / 5 caps.
_COMPRESSED = _payload(3, 5)

_MAP = json.dumps({"map": {
    "billing": "Cap 0", "auth": "Cap 1", "webhooks": "Cap 2",
}})


def _run(cli: Any, pfs: list[Feature] | None = None, ufs: list[UserFlow] | None = None):
    return run_journey_abstraction(
        ufs if ufs is not None else _ufs(),
        pfs if pfs is not None else _pfs(),
        _devs(), [], client=cli)


# ── _jpf_armed unit surface ─────────────────────────────────────────────────

def _digest(n_ufs: int = 12, n_res: int = 4, n_pfs: int = 8) -> dict[str, Any]:
    return {
        "current_user_flows": [
            {"id": f"UF-{i:03d}", "name": f"F{i}", "resource": f"r{i % n_res}"}
            for i in range(n_ufs)
        ],
        "current_product_features": [{"name": f"pf-{i}"} for i in range(n_pfs)],
    }


def _specs(n: int, key: str = "u") -> list[dict[str, Any]]:
    return [{"name": f"{key}{i}"} for i in range(n)]


def test_jpf_armed_both_axes_exceeded() -> None:
    assert _jpf_armed(_specs(6), _specs(9), _digest()) is True


def test_jpf_not_armed_journeys_at_resource_grain_prior() -> None:
    # 4 emitted == 4 distinct resources -> journeys axis lifted -> no arm.
    assert _jpf_armed(_specs(4), _specs(9), _digest()) is False


def test_jpf_not_armed_capabilities_at_deterministic_prior() -> None:
    # the dub shape: journeys exceed, capabilities do not (8 == 8).
    assert _jpf_armed(_specs(6), _specs(8), _digest()) is False


def test_jpf_floor_sparse_product_layer_never_arms() -> None:
    # 3 deterministic PFs < _MIN_ANCHORS_FLOOR -> prior not viable -> inert.
    assert _jpf_armed(_specs(6), _specs(9), _digest(n_pfs=3)) is False


def test_jpf_disarmed_without_digest_redundancy() -> None:
    # every digest UF a distinct resource (library / fastapi class): Call 1
    # legitimately EXPANDS there -> the prong must never fire.
    assert _jpf_armed(_specs(20), _specs(9), _digest(n_res=12)) is False


def test_distinct_pf_count_dedups_echoed_capability() -> None:
    specs = [{"name": "Billing"}, {"name": "billing "}, {"name": "Auth"}]
    assert _distinct_pf_count(specs) == 2


# ── End-to-end gate behaviour ───────────────────────────────────────────────

def test_jpf_prong_arms_and_retries_with_anchor_corrective() -> None:
    """jpf-only inflation (ratio-OK draw) is REJECTED; the ONE retry carries
    the jpf corrective that names the journeys-per-capability anchor, and the
    compressed retry ships with contract=pass_after_retry."""
    cli = _seq_client([_INFLATED, _COMPRESSED], _MAP)
    ufs, pfs, dm, tel = _run(cli)
    assert tel["applied"] is True
    assert tel["contract_armed_by"] == [CONTRACT_ARMED_JPF]
    assert tel["abstraction_retried"] is True
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY
    assert tel["llm_calls"] == 3  # draw + retry + reattrib
    assert tel["uf_specs_emitted"] == 6
    assert tel["uf_specs_emitted_retry"] == 3
    assert len(ufs) == 3  # retry result kept
    systems = cli.state["systems"]
    assert len(systems) == 2
    assert "PREVIOUS ATTEMPT REJECTED" not in systems[0]
    # the corrective names the structural anchor explicitly (mission wording)
    assert "ONE journey per distinct\ncapability" in systems[1].replace("  ", " ") \
        or "ONE journey per distinct" in systems[1]
    assert "distinct flow resources" in systems[1]


def test_jpf_not_armed_when_journey_axis_lifted() -> None:
    cli = _seq_client([_LIFT_UF], _MAP)
    _u, _p, _m, tel = _run(cli)
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert "contract_armed_by" not in tel
    assert tel["llm_calls"] == 2  # no retry


def test_jpf_not_armed_when_capability_axis_lifted() -> None:
    """The dub shape (journeys above resource grain BUT capabilities at the
    deterministic prior) abstracted the capability axis — no arm."""
    cli = _seq_client([_LIFT_PF], _MAP)
    _u, _p, _m, tel = _run(cli)
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert "contract_armed_by" not in tel
    assert tel["llm_calls"] == 2


def test_ratio_armed_keeps_merge_corrective() -> None:
    """A ratio-armed draw (even when jpf also fires) retries with the MERGE
    corrective (band-edge wording — MISSION-92 recall-at-depth fix 1), never
    the jpf one."""
    cli = _seq_client([_RATIO_FAIL, _COMPRESSED], _MAP)
    _u, _p, _m, tel = _run(cli)
    assert tel["contract_armed_by"] == [CONTRACT_ARMED_RATIO, CONTRACT_ARMED_JPF]
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY
    systems = cli.state["systems"]
    assert "one user_flow per input" in systems[1]      # _MERGE_CORRECTIVE
    assert "distinct flow resources" not in systems[1]  # not _JPF_CORRECTIVE
    # the band-edge contract: correctives must NEVER instruct a collapse to
    # ~1 journey per capability (the dub 96->40 crush)
    assert "Merge aggressively" not in systems[1]
    assert "(resource, intent) pair" in systems[1]


def test_ratio_only_pass_after_retry_semantics_preserved() -> None:
    """Restricted re-check: armed by ratio ALONE, the retry is judged on the
    ratio only (exact lever-1 semantics) — even if the retry would trip the
    jpf prong, contract reads pass_after_retry."""
    # retry = _INFLATED: ratio-OK (6 < 10.8) but jpf-armed in isolation.
    cli = _seq_client([_RATIO_ONLY, _INFLATED], _MAP)
    _u, _p, _m, tel = _run(cli)
    assert tel["contract_armed_by"] == [CONTRACT_ARMED_RATIO]
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY


def test_jpf_retry_still_inflated_kept_and_flagged() -> None:
    """Never-worse: the retry is kept even when still jpf-inflated, flagged
    uncompressed for scan_meta."""
    cli = _seq_client([_INFLATED, _INFLATED], _MAP)
    ufs, _p, _m, tel = _run(cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert tel["fallback"] is None
    assert len(ufs) == 6  # retry result kept


def test_jpf_retry_unparseable_keeps_first_draw() -> None:
    cli = _seq_client([_INFLATED, "garbage not json"], _MAP)
    ufs, _p, _m, tel = _run(cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert tel["abstraction_retry_failed"] == "abstraction_parse_failed"
    assert len(ufs) == 6  # first draw kept


def test_jpf_retry_skipped_when_cost_capped(monkeypatch: Any) -> None:
    """Same proportional admission as the ratio prong: a retry is admitted
    only while spend so far is under the single-draw cap; a first draw that
    already consumed it ships flagged, no retry issued."""
    from faultline.llm.cost import estimate_call_cost
    one_call = estimate_call_cost(DEFAULT_ABSTRACTION_MODEL, 400, 200)
    monkeypatch.setattr(_mod, "COST_CAP_USD", one_call * 0.9)
    cli = _seq_client([_INFLATED, _COMPRESSED], _MAP)
    ufs, _p, _m, tel = _run(cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert tel["abstraction_retry_skipped_cost"] is True
    assert len(cli.state["systems"]) == 1  # no retry call issued
    assert len(ufs) == 6


def test_jpf_floor_end_to_end_sparse_product_layer() -> None:
    """3 deterministic PFs (< the viability floor): the same inflated draw
    passes untouched — no retry on repos whose product prior is meaningless."""
    cli = _seq_client([_INFLATED], _MAP)
    _u, _p, _m, tel = _run(cli, pfs=_pfs(3))
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert "contract_armed_by" not in tel
    assert tel["llm_calls"] == 2


def test_jpf_disarmed_at_resource_grain_end_to_end() -> None:
    """Digest at resource grain (every UF a distinct resource — the fastapi
    expansion class): neither prong fires even on an expanding draw."""
    ufs_in = _ufs()
    for i, u in enumerate(ufs_in):
        u.resource = f"res-{i}"  # 12 distinct resources
    cli = _seq_client([_payload(12, 9)], _MAP)  # 1:1 'expansion' draw
    _u, _p, _m, tel = _run(cli, ufs=ufs_in)
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert "contract_armed_by" not in tel
    assert tel["llm_calls"] == 2


def test_jpf_telemetry_always_present_on_live_draw() -> None:
    """jpf_draw (the draw's journeys-per-capability) + jpf_prior (distinct
    resources per deterministic PF) land in scan_meta on every live draw,
    armed or not."""
    cli = _seq_client([_LIFT_PF], _MAP)
    _u, _p, _m, tel = _run(cli)
    assert tel["jpf_draw"] == round(6 / 8, 3)
    assert tel["jpf_prior"] == round(4 / 8, 3)
