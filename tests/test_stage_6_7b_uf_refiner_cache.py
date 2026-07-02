"""Unit tests for the Stage 6.7b content-hash LLM cache (CacheKind.LLM_UF_REFINE).

Contract (mirrors tests/test_stage_3_flows_cache.py + the 6.7d cache tests):
  (a) a live call populates the cache;
  (b) a second run over UNCHANGED inputs replays byte-identically with a
      RAISING client (proving no LLM call) + cache_hit telemetry;
  (c) a cache backend fault falls through to the live call (never-worse);
  (d) a different model / different input produces a different key — no
      false hits;
  (e) FAULTLINE_STAGE_6_7B_CACHE=0 opts out;
  (f) failures (bad JSON) are never cached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.cache.backend import CacheKind
from faultline.models.types import Flow, FlowParticipant, UserFlow
from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows


# ── Fake Anthropic client (mirrors tests/test_stage_6_7b_uf_refiner.py) ─────


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
                raise AssertionError("LLM must NOT be called on a warm cache")

        messages = _Messages()

    return _Client()


# ── In-memory CacheBackend (mirrors test_stage_6_7d's _MemCache) ─────────────


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


class _BadCache(_MemCache):
    def get(self, kind: str, key: str) -> Any:
        raise RuntimeError("cache down")

    def set(self, kind: str, key: str, value: Any, *, ttl_seconds: Any = None) -> None:
        raise RuntimeError("cache down")


# ── Fixtures (mirrors tests/test_stage_6_7b_uf_refiner.py) ──────────────────


def _flow(name: str, *, uuid: str = "", ui_path: str | None = None) -> Flow:
    participants = []
    if ui_path is not None:
        participants.append(FlowParticipant(path=ui_path, layer="ui"))
    return Flow(
        name=name,
        uuid=uuid or name,
        paths=["backend/routers/detectors.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
        test_files=[],
        participants=participants,
    )


def _uf(uf_id: str, name: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id,
        name=name,
        domain="detector",
        product_feature_id="detector",
        intent="author",
        resource="detector",
        member_flow_ids=members,
        member_count=len(members),
        routes=[],
    )


def _fixture() -> tuple[list[UserFlow], list[Flow]]:
    """Fresh identical objects per call — models an UNCHANGED repo re-scan
    (the stage mutates UFs in place, so each run needs its own copies)."""
    flows = [_flow("create-detector-flow", ui_path="frontend/src/DetectorForm.tsx")]
    ufs = [_uf("UF-001", "Create & edit detectors", ["create-detector-flow"])]
    return ufs, flows


_RESP = json.dumps({"user_flows": [{
    "id": "UF-001",
    "name": "Create a detector",
    "description": "User defines a new detector and saves it.",
    "intent": "author",
    "ui_tier": "full-page",
    "acceptance": [],
}]})


# ── (a) live call populates the cache ────────────────────────────────────────


def test_live_call_populates_cache() -> None:
    cache = _MemCache()
    ufs, flows = _fixture()
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(_RESP), cache=cache)
    assert out[0].refined is True
    assert tel["llm_calls"] == 1
    assert tel["cache_hits"] == 0
    kinds = {kind for kind, _ in cache.store}
    assert kinds == {CacheKind.LLM_UF_REFINE.value}
    assert len(cache.store) == 1
    (stored,) = cache.store.values()
    assert stored["v"] == "v1"
    assert "UF-001" in stored["user_flows"]


# ── (b) warm cache replays identically with a RAISING client ────────────────


def test_warm_cache_replays_identically_with_raising_client() -> None:
    cache = _MemCache()
    ufs1, flows1 = _fixture()
    out1, tel1 = refine_user_flows(
        ufs1, flows1, client=_client_returning(_RESP), cache=cache,
    )
    assert tel1["cache_hits"] == 0 and tel1["llm_calls"] == 1

    ufs2, flows2 = _fixture()
    out2, tel2 = refine_user_flows(
        ufs2, flows2, client=_raising_client(), cache=cache,
    )
    # No LLM call, $0, one hit — and the refinement is byte-identical.
    assert tel2["cache_hits"] == 1
    assert tel2["llm_calls"] == 0
    assert tel2["cost_usd"] == 0.0
    assert tel2["uf_refined"] == 1
    assert tel2["domains_degraded"] == 0
    assert [u.model_dump() for u in out2] == [u.model_dump() for u in out1]


# ── (c) cache fault falls through to live (never-worse) ─────────────────────


def test_cache_fault_falls_through_to_live() -> None:
    ufs, flows = _fixture()
    out, tel = refine_user_flows(
        ufs, flows, client=_client_returning(_RESP), cache=_BadCache(),
    )
    assert out[0].refined is True
    assert out[0].name == "Create a detector"
    assert tel["cache_hits"] == 0
    assert tel["llm_calls"] == 1


# ── (d) different model / input → different key (no false hits) ─────────────


def test_different_model_misses_cache() -> None:
    cache = _MemCache()
    ufs1, flows1 = _fixture()
    refine_user_flows(ufs1, flows1, client=_client_returning(_RESP), cache=cache)
    assert len(cache.store) == 1

    # Same inputs, DIFFERENT model → key miss → the raising client would be
    # called; the per-domain worker degrades (worker_error) instead of
    # serving the other model's answer.
    ufs2, flows2 = _fixture()
    out2, tel2 = refine_user_flows(
        ufs2, flows2, client=_raising_client(), model="claude-sonnet-4-6",
        cache=cache,
    )
    assert tel2["cache_hits"] == 0
    assert tel2["domains_degraded"] == 1
    assert out2[0].refined is False
    assert out2[0].name == "Create & edit detectors"  # deterministic name kept


def test_different_input_misses_cache() -> None:
    cache = _MemCache()
    ufs1, flows1 = _fixture()
    refine_user_flows(ufs1, flows1, client=_client_returning(_RESP), cache=cache)

    # A changed member flow name changes the UF payload → different key.
    flows2 = [_flow("delete-detector-flow", ui_path="frontend/src/DetectorForm.tsx")]
    ufs2 = [_uf("UF-001", "Create & edit detectors", ["delete-detector-flow"])]
    _out2, tel2 = refine_user_flows(
        ufs2, flows2, client=_raising_client(), cache=cache,
    )
    assert tel2["cache_hits"] == 0
    assert tel2["domains_degraded"] == 1


# ── (e) env opt-out ──────────────────────────────────────────────────────────


def test_env_opt_out_disables_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _MemCache()
    ufs1, flows1 = _fixture()
    refine_user_flows(ufs1, flows1, client=_client_returning(_RESP), cache=cache)
    assert len(cache.store) == 1

    monkeypatch.setenv("FAULTLINE_STAGE_6_7B_CACHE", "0")
    ufs2, flows2 = _fixture()
    _out2, tel2 = refine_user_flows(
        ufs2, flows2, client=_raising_client(), cache=cache,
    )
    # Warm entry ignored → live path (which raises) → degrade, no hit.
    assert tel2["cache_hits"] == 0
    assert tel2["domains_degraded"] == 1
    assert len(cache.store) == 1  # and nothing new written


# ── (f) failures are never cached ────────────────────────────────────────────


def test_parse_failure_not_cached() -> None:
    cache = _MemCache()
    ufs, flows = _fixture()
    _out, tel = refine_user_flows(
        ufs, flows, client=_client_returning("not json"), cache=cache,
    )
    assert tel["domains_degraded"] == 1
    assert cache.store == {}
