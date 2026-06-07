"""Unit tests for Stage 6.7c — Mega-UF semantic split.

The LLM is always mocked. These assert: the mega gate (only UFs with many
members AND many distinct journey names are split), members re-assigned to
per-journey sub-UFs, ``Flow.user_flow_id`` re-stamped, recall-safety (unplaced
members land in a residual sub-UF — no flow dropped), and graceful degrade
(mega-UF kept) on no-client / bad-JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.stage_6_7c_uf_splitter import split_mega_user_flows


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeBlock:
    text: str


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text=text)]
        self.usage = _FakeUsage(input_tokens=300, output_tokens=120)


def _client_returning(text: str) -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text)

        messages = _Messages()

    return _Client()


def _flow(name: str, uuid: str) -> Flow:
    return Flow(
        name=name,
        uuid=uuid,
        paths=["backend/routers/x.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
    )


def _uf(uf_id: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name="bookings", domain="booking", product_feature_id="booking",
        intent="other", resource="booking",
        member_flow_ids=members, member_count=len(members),
    )


def _mega_fixture() -> tuple[list[UserFlow], list[Flow], list[str]]:
    """21 members across 7 distinct journey names (3 each) → mega-mixed."""
    names = [f"journey-{c}-flow" for c in "abcdefg"]
    flows: list[Flow] = []
    member_ids: list[str] = []
    for n in names:
        for i in range(3):
            uid = f"{n}-{i}"
            flows.append(_flow(n, uid))
            member_ids.append(uid)
    return [_uf("UF-001", member_ids)], flows, names


def test_non_mega_uf_untouched() -> None:
    flows = [_flow("a-flow", "a1"), _flow("b-flow", "b1")]
    ufs = [_uf("UF-001", ["a1", "b1"])]
    out, tel = split_mega_user_flows(ufs, flows, client=_client_returning("{}"))
    assert out == ufs
    assert tel["mega_detected"] == 0
    assert tel["mega_split"] == 0


def test_mega_uf_split_into_journeys() -> None:
    ufs, flows, names = _mega_fixture()
    resp = json.dumps({"journeys": [
        {"name": "First journey", "members": names[:3]},
        {"name": "Second journey", "members": names[3:]},
    ]})
    out, tel = split_mega_user_flows(ufs, flows, client=_client_returning(resp))

    assert tel["mega_detected"] == 1
    assert tel["mega_split"] == 1
    # original mega-UF replaced by 2 sub-UFs
    assert [u.id for u in out] == ["UF-001-1", "UF-001-2"]
    assert out[0].member_count == 9 and out[1].member_count == 12
    # no flow dropped
    assert sum(u.member_count for u in out) == 21
    # Flow.user_flow_id re-stamped to the sub-UF that owns each member
    by_uid = {f.uuid: f for f in flows}
    assert by_uid["journey-a-flow-0"].user_flow_id == "UF-001-1"
    assert by_uid["journey-g-flow-2"].user_flow_id == "UF-001-2"
    assert tel["cost_usd"] > 0.0


def test_recall_safe_residual() -> None:
    ufs, flows, names = _mega_fixture()
    # LLM places only the first 2 names; the rest must survive in a residual.
    resp = json.dumps({"journeys": [{"name": "Only journey", "members": names[:2]}]})
    out, tel = split_mega_user_flows(ufs, flows, client=_client_returning(resp))
    assert sum(u.member_count for u in out) == 21  # nothing dropped
    # one named sub-UF + one residual
    assert len(out) == 2
    assert out[0].member_count == 6  # 2 names × 3
    assert out[1].member_count == 15  # residual: 5 names × 3
    # every member flow still carries a user_flow_id
    assert all(f.user_flow_id for f in flows)


def test_degrade_no_client() -> None:
    ufs, flows, _ = _mega_fixture()
    out, tel = split_mega_user_flows(
        ufs, flows, client=None, _client_factory=lambda: None,
    )
    assert out == ufs
    assert tel["enabled"] is False
    assert tel["fallback_reason"] == "no_anthropic_client"


def test_degrade_bad_json_keeps_mega() -> None:
    ufs, flows, _ = _mega_fixture()
    out, tel = split_mega_user_flows(
        ufs, flows, client=_client_returning("not json at all"),
    )
    assert out == ufs  # mega-UF kept intact
    assert tel["mega_detected"] == 1
    assert tel["mega_split"] == 0
