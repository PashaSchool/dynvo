"""Unit tests for Stage 6.7b — User-Flow LLM refiner.

The LLM is always mocked here (a fake Anthropic client). These tests
assert: refined names applied, intent="other" resolved, ui_tier set,
AC drafted from test_files (count = tested members), and graceful
degrade (deterministic name/intent kept) on LLM error / bad JSON / no
client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Flow, FlowParticipant, UserFlow
from faultline.pipeline_v2.stage_6_7b_uf_refiner import (
    COST_CAP_USD_PER_DOMAIN,
    refine_user_flows,
)


# ── Fake Anthropic client (mirrors tests/test_stack_auditor.py) ─────────────


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeBlock:
    text: str


class _FakeMsg:
    def __init__(self, *, text: str, in_tokens: int, out_tokens: int) -> None:
        self.content = [_FakeBlock(text=text)]
        self.usage = _FakeUsage(input_tokens=in_tokens, output_tokens=out_tokens)


def _client_returning(text: str, *, in_tokens: int = 400, out_tokens: int = 150) -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text=text, in_tokens=in_tokens, out_tokens=out_tokens)

        messages = _Messages()

    return _Client()


def _raising_client() -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                raise RuntimeError("boom")

        messages = _Messages()

    return _Client()


# ── Fixtures ────────────────────────────────────────────────────────────────


def _flow(
    name: str,
    *,
    uuid: str = "",
    paths: list[str] | None = None,
    test_files: list[str] | None = None,
    ui_path: str | None = None,
) -> Flow:
    participants = []
    if ui_path is not None:
        participants.append(FlowParticipant(path=ui_path, layer="ui"))
    return Flow(
        name=name,
        uuid=uuid or name,
        paths=paths or [f"backend/routers/detectors.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
        test_files=test_files or [],
        participants=participants,
    )


def _uf(
    uf_id: str,
    name: str,
    intent: str,
    *,
    domain: str | None = "detector",
    members: list[str] | None = None,
    routes: list[str] | None = None,
) -> UserFlow:
    members = members or []
    return UserFlow(
        id=uf_id,
        name=name,
        product_feature_id=domain,
        intent=intent,
        resource="detector",
        member_flow_ids=members,
        member_count=len(members),
        routes=routes or [],
    )


# ── Tests ───────────────────────────────────────────────────────────────────


def test_refined_names_and_description_applied() -> None:
    flows = [
        _flow("create-detector-flow", ui_path="frontend/src/DetectorForm.tsx"),
    ]
    ufs = [_uf("UF-001", "Create & edit detectors", "author",
               members=["create-detector-flow"])]
    resp = json.dumps({"user_flows": [{
        "id": "UF-001",
        "name": "Create a detector",
        "description": "User defines a new detector and saves it.",
        "intent": "author",
        "ui_tier": "full-page",
        "acceptance": [],
    }]})
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].name == "Create a detector"
    assert out[0].description.startswith("User defines")
    assert out[0].refined is True
    assert tel["uf_refined"] == 1
    assert tel["cost_usd"] > 0.0


def test_intent_other_resolved() -> None:
    flows = [_flow("validate-detector-flow")]
    ufs = [_uf("UF-002", "detector", "other", members=["validate-detector-flow"])]
    resp = json.dumps({"user_flows": [{
        "id": "UF-002",
        "name": "Validate a detector",
        "intent": "manage",
        "ui_tier": "no-ui",
    }]})
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].intent == "manage"
    assert tel["intent_other_before"] == 1
    assert tel["intent_other_after"] == 0


def test_intent_kept_when_valid_given() -> None:
    """LLM is told to keep a valid given intent; we don't overwrite it
    even if the model echoes a different valid class."""
    flows = [_flow("list-detector-flow")]
    ufs = [_uf("UF-003", "Browse & filter detectors", "browse",
               members=["list-detector-flow"])]
    resp = json.dumps({"user_flows": [{
        "id": "UF-003", "name": "Browse detectors", "intent": "execute",
        "ui_tier": "full-page",
    }]})
    out, _ = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].intent == "browse"  # not overwritten


def test_ui_tier_set_from_llm() -> None:
    flows = [_flow("update-detector-flow", paths=["frontend/settings/detector.tsx"])]
    ufs = [_uf("UF-004", "x", "author", members=["update-detector-flow"])]
    resp = json.dumps({"user_flows": [{
        "id": "UF-004", "name": "Edit detector settings", "intent": "author",
        "ui_tier": "settings",
    }]})
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].ui_tier == "settings"
    assert tel["ui_tier_set"] == 1


def test_acceptance_drafted_from_test_files() -> None:
    """AC count == number of test-reached members; capped structurally."""
    flows = [
        _flow("create-detector-flow", uuid="f1", test_files=["t_create.py"]),
        _flow("delete-detector-flow", uuid="f2", test_files=["t_delete.py"]),
        _flow("list-detector-flow", uuid="f3"),  # no test_files
    ]
    ufs = [_uf("UF-005", "x", "author", members=["f1", "f2", "f3"])]
    # LLM returns 3 ACs but only 2 members are test-reached -> trimmed to 2.
    resp = json.dumps({"user_flows": [{
        "id": "UF-005", "name": "Manage detectors", "intent": "author",
        "ui_tier": "no-ui",
        "acceptance": ["can create", "can delete", "extra hallucinated"],
    }]})
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert len(out[0].acceptance) == 2
    assert out[0].acceptance[0].startswith("AC-1:")
    assert out[0].acceptance[1].startswith("AC-2:")
    assert tel["acceptance_total"] == 2


def test_graceful_degrade_on_llm_error() -> None:
    flows = [_flow("create-detector-flow")]
    ufs = [_uf("UF-006", "Create & edit detectors", "author",
               members=["create-detector-flow"])]
    out, tel = refine_user_flows(ufs, flows, client=_raising_client())
    # Deterministic name + intent kept; not marked refined.
    assert out[0].name == "Create & edit detectors"
    assert out[0].intent == "author"
    assert out[0].refined is False
    assert tel["domains_degraded"] == 1
    # ui_tier still filled deterministically (never null).
    assert out[0].ui_tier is not None


def test_graceful_degrade_on_bad_json() -> None:
    flows = [_flow("create-detector-flow")]
    ufs = [_uf("UF-007", "Create & edit detectors", "author",
               members=["create-detector-flow"])]
    out, tel = refine_user_flows(
        ufs, flows, client=_client_returning("not json at all"),
    )
    assert out[0].name == "Create & edit detectors"
    assert out[0].refined is False
    assert tel["domains_degraded"] == 1


def test_no_client_keeps_deterministic_and_fills_ui_tier() -> None:
    flows = [_flow("create-detector-flow")]
    ufs = [_uf("UF-008", "Create & edit detectors", "author",
               members=["create-detector-flow"])]
    out, tel = refine_user_flows(
        ufs, flows, client=None, _client_factory=lambda: None,
    )
    assert tel["enabled"] is False
    assert tel["fallback_reason"] == "no_anthropic_client"
    assert out[0].name == "Create & edit detectors"
    assert out[0].ui_tier is not None  # deterministic fill
    assert tel["cost_usd"] == 0.0


def test_cost_cap_degrades_domain() -> None:
    flows = [_flow("create-detector-flow")]
    ufs = [_uf("UF-009", "Create & edit detectors", "author",
               members=["create-detector-flow"])]
    resp = json.dumps({"user_flows": [{
        "id": "UF-009", "name": "Create a detector", "intent": "author",
        "ui_tier": "full-page",
    }]})
    # Huge token counts blow past the per-domain cap -> degrade.
    huge = _client_returning(resp, in_tokens=5_000_000, out_tokens=2_000_000)
    out, tel = refine_user_flows(ufs, flows, client=huge)
    assert tel["domains_degraded"] == 1
    assert out[0].name == "Create & edit detectors"  # kept deterministic
    assert tel["cost_usd"] > COST_CAP_USD_PER_DOMAIN


def test_one_call_per_domain() -> None:
    """Two domains -> two LLM calls; membership/grain untouched."""
    flows = [_flow("create-detector-flow"), _flow("create-alert-flow",
             paths=["backend/routers/alerts.py"])]
    ufs = [
        _uf("UF-010", "x", "author", domain="detector", members=["create-detector-flow"]),
        _uf("UF-011", "y", "author", domain="alert", members=["create-alert-flow"]),
    ]
    calls: list[dict] = []

    class _Client:
        class _Messages:
            def create(self, **kw: Any) -> Any:
                calls.append(kw)
                # Echo back both ids; only the matching domain's id applies.
                return _FakeMsg(
                    text=json.dumps({"user_flows": [
                        {"id": "UF-010", "name": "Create a detector",
                         "intent": "author", "ui_tier": "no-ui"},
                        {"id": "UF-011", "name": "Create an alert",
                         "intent": "author", "ui_tier": "no-ui"},
                    ]}),
                    in_tokens=300, out_tokens=100,
                )

        messages = _Messages()

    out, tel = refine_user_flows(ufs, flows, client=_Client())
    assert len(calls) == 2  # one per domain
    assert tel["domains_total"] == 2
    # Membership preserved.
    assert out[0].member_flow_ids == ["create-detector-flow"]
    assert out[1].member_flow_ids == ["create-alert-flow"]
