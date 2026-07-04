"""Stage 6.7d — two-sided abstraction-contract BAND + input-scaled digest
(MISSION-92 recall-at-depth fixes 1 + 3).

Fix 1: golden journey grain empirically sits at resource x intent
(diagnostician trace: first draws landed AT golden grain — dub 96 vs 99,
cal-com 126 vs 102 — and the one-sided contract retry crushed them,
dub 96→40). A first draw whose journey count is at-or-below the digest's
distinct (resource, intent) pair count must be ACCEPTED: neither prong
may arm on it, and the correctives target the band edge ("consolidate
only same-resource same-intent variants"), never "merge to ~1 per
capability".

Fix 3: the digest caps scale with the input (show all UFs/routes — the
old fixed 120/160 hid most of the pre-UF surface on big repos) and the
Call-1 output budget scales with the digest size (floor 16k, structural
ceiling).

The LLM is always mocked. Determinism of the scaled digest is asserted
directly (same input, shuffled order → identical JSON).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    ABSTRACTION_MAX_TOKENS,
    ABSTRACTION_MAX_TOKENS_CEILING,
    CONTRACT_PASS,
    CONTRACT_PASS_AFTER_RETRY,
    _abstraction_max_tokens,
    _band_pairs,
    _build_digest,
    run_journey_abstraction,
)


# ── fakes / fixtures ─────────────────────────────────────────────────────────

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
    records every abstraction call's kwargs in ``state['calls']``."""
    state: dict[str, Any] = {"i": 0, "systems": [], "calls": []}

    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                if "assign each developer feature" in sysp:
                    return _Msg(reattrib)
                state["systems"].append(sysp)
                state["calls"].append(kw)
                i = min(state["i"], len(abstraction_payloads) - 1)
                state["i"] += 1
                return _Msg(abstraction_payloads[i])
        messages = _M()

    c = _C()
    c.state = state  # type: ignore[attr-defined]
    return c


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
    ]


def _uf(uf_id: str, name: str, resource: str, intent: str) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent=intent, resource=resource,
        member_flow_ids=[uf_id.lower()], member_count=1, routes=[f"/{resource}"],
    )


def _banded_ufs() -> list[UserFlow]:
    """6 digest UFs = 2 resources x 3 intent classes → band = 6 pairs,
    but only 2 distinct resources → same-resource redundancy exists
    (2 < 0.9*6), so the OLD one-sided gate arms and would reject any
    draw emitting >= 0.9*6 = 5.4 journeys."""
    out = []
    i = 0
    for res in ("invoice", "member"):
        for intent in ("author", "browse", "lifecycle"):
            i += 1
            out.append(_uf(f"UF-{i:03d}", f"{intent} {res}", res, intent))
    return out


def _payload(n_ufs: int) -> str:
    pfs = [{"name": "Billing", "description": "b"},
           {"name": "Team", "description": "t"}]
    ufs = [
        {"name": f"Journey {i}", "resource": "invoice" if i % 2 else "member",
         "product_feature": "Billing" if i % 2 else "Team",
         "from_flows": [f"UF-{(i % 6) + 1:03d}"]}
        for i in range(n_ufs)
    ]
    return json.dumps({"product_features": pfs, "user_flows": ufs})


_MAP = json.dumps({"map": {"billing": "Billing", "auth": "Team"}})


# ── _band_pairs unit surface ─────────────────────────────────────────────────

def test_band_pairs_resource_x_intent() -> None:
    digest = {"current_user_flows": [
        {"id": "a", "name": "A", "resource": "invoice", "intent": "author"},
        {"id": "b", "name": "B", "resource": "invoice", "intent": "browse"},
        {"id": "c", "name": "C", "resource": "invoice", "intent": "browse"},  # dup pair
        {"id": "d", "name": "D", "resource": "member", "intent": "author"},
    ]}
    assert _band_pairs(digest) == 3


def test_band_pairs_missing_resource_counts_as_own_pair() -> None:
    digest = {"current_user_flows": [
        {"id": "a", "name": "A", "resource": "", "intent": "author"},
        {"id": "b", "name": "B", "resource": None, "intent": "author"},
        {"id": "c", "name": "C", "resource": "invoice", "intent": None},
    ]}
    # two name-keyed pairs + one ("invoice","") pair
    assert _band_pairs(digest) == 3


def test_band_pairs_empty_digest() -> None:
    assert _band_pairs({"current_user_flows": []}) == 0


# ── end-to-end band acceptance ───────────────────────────────────────────────

def test_first_draw_at_golden_grain_accepted_no_retry() -> None:
    """THE dub-crush regression test: a first draw whose journey count sits
    at the band (6 emitted == 6 distinct (resource,intent) pairs) is
    ACCEPTED even though the old one-sided ratio (6 >= 0.9*6, redundancy
    armed) would have rejected and crushed it."""
    cli = _seq_client([_payload(6)], _MAP)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _banded_ufs(), [_feat("web", ["app/x.ts"])], _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert tel["contract_band_pairs"] == 6
    assert tel["contract_band_accepted"] is True
    assert "contract_armed_by" not in tel
    assert tel["llm_calls"] == 2                        # no retry issued
    assert len(cli.state["systems"]) == 1


def test_draw_above_band_still_armed_and_retry_judged_by_band() -> None:
    """Above the band the prongs work as before (8 > 6 pairs, ratio arms at
    8 >= 5.4); a retry landing INSIDE the band passes the restricted
    re-check → pass_after_retry, retry kept."""
    cli = _seq_client([_payload(8), _payload(6)], _MAP)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _banded_ufs(), [_feat("web", ["app/x.ts"])], _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["contract_band_accepted"] is False
    assert tel["contract_armed_by"] == ["ratio"]
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY
    assert tel["uf_specs_emitted"] == 8
    assert tel["uf_specs_emitted_retry"] == 6
    # the retry corrective targets the band edge, never per-capability crush
    assert "(resource, intent) pair" in cli.state["systems"][1]
    assert "Merge aggressively" not in cli.state["systems"][1]


# ── input-scaled max_tokens ──────────────────────────────────────────────────

def test_abstraction_max_tokens_floor_scale_ceiling() -> None:
    assert _abstraction_max_tokens(10) == ABSTRACTION_MAX_TOKENS          # floor
    assert _abstraction_max_tokens(222) == 22200                          # scaled
    assert _abstraction_max_tokens(400) == ABSTRACTION_MAX_TOKENS_CEILING # capped
    assert _abstraction_max_tokens(100, 100) == 20000                     # + anchors


def test_scaled_max_tokens_passed_to_call1() -> None:
    big_ufs = [_uf(f"UF-{i:03d}", f"Do thing {i}", f"res{i}", "author")
               for i in range(1, 223)]
    cli = _seq_client([_payload(6)], _MAP)
    _u, _p, _m, tel = run_journey_abstraction(
        big_ufs, [_feat("web", ["app/x.ts"])], _devs(), [], client=cli)
    assert tel["abstraction_max_tokens"] == 22200
    assert cli.state["calls"][0]["max_tokens"] == 22200


def test_streaming_required_reroutes_to_stream() -> None:
    """The SDK rejects non-streaming calls whose max_tokens implies a
    >10-minute operation (which the scaled budget can request). The call
    helper reroutes THAT rejection — and only that one — to
    messages.stream and returns the final message."""
    state: dict[str, Any] = {"streamed": 0}

    class _Stream:
        def __enter__(self):  # noqa: ANN204
            return self
        def __exit__(self, *a):  # noqa: ANN002, ANN204
            return False
        def get_final_message(self):  # noqa: ANN202
            return _Msg(_payload(3))

    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                if "assign each developer feature" in kw.get("system", ""):
                    return _Msg(_MAP)
                raise RuntimeError(
                    "Streaming is required for operations that may take "
                    "longer than 10 minutes.")
            def stream(self, **kw: Any) -> Any:
                state["streamed"] += 1
                return _Stream()
        messages = _M()

    ufs, pfs, dm, tel = run_journey_abstraction(
        _banded_ufs(), [_feat("web", ["app/x.ts"])], _devs(), [], client=_C())
    assert state["streamed"] == 1
    assert tel["applied"] is True
    assert tel["fallback"] is None


def test_other_create_errors_still_degrade() -> None:
    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                raise RuntimeError("boom")
        messages = _M()

    ufs, pfs, dm, tel = run_journey_abstraction(
        _banded_ufs(), [_feat("web", ["app/x.ts"])], _devs(), [], client=_C())
    assert tel["fallback"] == "abstraction_parse_failed"
    assert tel["applied"] is False


# ── input-scaled digest (fix 3) ──────────────────────────────────────────────

def test_digest_shows_all_ufs_and_routes() -> None:
    ufs = [_uf(f"UF-{i:03d}", f"Flow {i}", f"res{i % 40}", "author")
           for i in range(1, 201)]
    routes = [{"pattern": f"/api/r{i}", "method": "GET", "trigger": None}
              for i in range(300)]
    digest = _build_digest(_devs(), [_feat("web", ["app/x.ts"])], ufs, routes)
    assert len(digest["current_user_flows"]) == 200
    assert len(digest["routes"]) == 300


def test_scaled_digest_deterministic_under_input_order() -> None:
    ufs = [_uf(f"UF-{i:03d}", f"Flow {i}", f"res{i % 40}", "author")
           for i in range(1, 201)]
    routes = [{"pattern": f"/api/r{i}", "method": "GET", "trigger": None}
              for i in range(300)]
    d1 = _build_digest(_devs(), [_feat("web", ["app/x.ts"])], ufs, routes)
    d2 = _build_digest(
        list(reversed(_devs())), [_feat("web", ["app/x.ts"])],
        list(reversed(ufs)), routes,
    )
    assert json.dumps(d1["current_user_flows"], sort_keys=True) \
        == json.dumps(d2["current_user_flows"], sort_keys=True)
    assert json.dumps(d1["developer_features"], sort_keys=True) \
        == json.dumps(d2["developer_features"], sort_keys=True)
