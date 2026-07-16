"""B71 add-on — degraded-scan confidence scoping (FAULTLINE_NAMING_PACK).

Diagnosis (ledger §cache-key 2026-07-16, VERIFIED by the experimenter): on
``llm_health.auth_failed`` the 6.7b refiner blanket-stamps name_confidence="low"
on EVERY user_flow — even domains whose names were validated FROM CACHE this run
(a dead key mid-scan does not un-validate a cache hit). The fix scopes the
downgrade to the domains that were NOT name-validated this run.

Unit: 2 domains validated from cache + 1 auth-fail domain -> low ONLY on the
non-validated domain; OFF keeps the blanket downgrade (byte-identical).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
        self.usage = _FakeUsage(input_tokens=400, output_tokens=150)


def _client_returning(text: str) -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text)

        messages = _Messages()

    return _Client()


def _raising_client() -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                raise AssertionError("no LLM call once the key is dead")

        messages = _Messages()

    return _Client()


class _MemCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}

    def get(self, kind: str, key: str) -> Any:
        return self.store.get((kind, key))

    def set(self, kind: str, key: str, value: Any, *, ttl_seconds: Any = None) -> None:
        self.store[(kind, key)] = value

    def delete(self, kind: str, key: str) -> None:
        self.store.pop((kind, key), None)

    def load_namespace(self, kind: str) -> dict[str, Any]:
        return {}

    def flush(self) -> None:
        pass


def _flow(name: str, domain: str) -> Flow:
    return Flow(
        name=name, uuid=name, paths=[f"backend/{domain}/handler.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _uf(uf_id: str, domain: str) -> UserFlow:
    return UserFlow(
        id=uf_id, name=f"Manage {domain}", domain=domain,
        product_feature_id=domain, intent="manage", resource=domain,
        member_flow_ids=[f"{domain}-flow"], member_count=1, routes=[],
    )


def _resp(uf_id: str, domain: str) -> str:
    # Name evidenced by the domain/resource so anti-hallucination validation
    # passes — the validated domains are NOT low for an unrelated reason, which
    # isolates the auth-fail stamp effect under test.
    return json.dumps({"user_flows": [{
        "id": uf_id, "name": f"Manage {domain}", "intent": "manage",
        "ui_tier": "full-page", "acceptance": [],
    }]})


def _warm(cache: _MemCache, domain: str, uf_id: str) -> None:
    """Populate the cache for one domain with a healthy run (should_call True)."""
    refine_user_flows(
        [_uf(uf_id, domain)], [_flow(f"{domain}-flow", domain)],
        client=_client_returning(_resp(uf_id, domain)), cache=cache,
        llm_health=LlmHealth(),
    )


def _run_authfail(cache: _MemCache) -> list[UserFlow]:
    """A run where the key is dead from the start: billing/auth hit the warm
    cache (validated), reports misses -> degraded."""
    ufs = [_uf("UF-B", "billing"), _uf("UF-A", "auth"), _uf("UF-R", "reports")]
    flows = [_flow("billing-flow", "billing"), _flow("auth-flow", "auth"),
             _flow("reports-flow", "reports")]
    dead = LlmHealth()
    dead.seed_auth_failure(stage="test")
    refine_user_flows(
        ufs, flows, client=_raising_client(), cache=cache, llm_health=dead,
    )
    return ufs


def _by_id(ufs: list[UserFlow]) -> dict[str, UserFlow]:
    return {u.id: u for u in ufs}


def test_authfail_scopes_downgrade_to_unvalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    """NAMING_PACK ON: cache-validated billing + auth KEEP their verdict; only
    the degraded reports domain is stamped low."""
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "1")
    cache = _MemCache()
    _warm(cache, "billing", "UF-B")
    _warm(cache, "auth", "UF-A")
    by = _by_id(_run_authfail(cache))
    assert by["UF-R"].name_confidence == "low"          # not validated -> low
    assert by["UF-B"].name_confidence != "low"          # cache-validated -> kept
    assert by["UF-A"].name_confidence != "low"


def test_authfail_blanket_when_pack_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """NAMING_PACK OFF: the original blanket downgrade — ALL low
    (byte-identical to the pre-B71 behaviour)."""
    # MECHANICAL (horizon-1 flip): explicit "0" (unset now defaults ON).
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "0")
    cache = _MemCache()
    _warm(cache, "billing", "UF-B")
    _warm(cache, "auth", "UF-A")
    by = _by_id(_run_authfail(cache))
    assert by["UF-B"].name_confidence == "low"
    assert by["UF-A"].name_confidence == "low"
    assert by["UF-R"].name_confidence == "low"
