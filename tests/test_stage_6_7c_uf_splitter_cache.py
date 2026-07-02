"""Unit tests for the Stage 6.7c content-hash LLM cache (CacheKind.LLM_UF_SPLIT).

Contract (mirrors tests/test_stage_3_flows_cache.py + the 6.7d cache tests):
  (a) a live call populates the cache;
  (b) a second run over UNCHANGED inputs replays byte-identically with a
      RAISING client (proving no LLM call) + cache_hit telemetry;
  (c) a cache backend fault falls through to the live call (never-worse);
  (d) a different model / different input produces a different key — no
      false hits;
  (e) FAULTLINE_STAGE_6_7C_CACHE=0 opts out;
  (f) failures (bad JSON) are never cached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.cache.backend import CacheKind
from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.stage_6_7c_uf_splitter import split_mega_user_flows


# ── Fake Anthropic client (mirrors tests/test_stage_6_7c_uf_splitter.py) ────


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


# ── Fixtures (mirrors tests/test_stage_6_7c_uf_splitter.py) ─────────────────


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
    """21 members across 7 distinct journey names (3 each) → mega-mixed.
    Fresh objects per call — models an UNCHANGED repo re-scan (the stage
    mutates ``Flow.user_flow_id`` in place)."""
    names = [f"journey-{c}-flow" for c in "abcdefg"]
    flows: list[Flow] = []
    member_ids: list[str] = []
    for n in names:
        for i in range(3):
            uid = f"{n}-{i}"
            flows.append(_flow(n, uid))
            member_ids.append(uid)
    return [_uf("UF-001", member_ids)], flows, names


def _resp(names: list[str]) -> str:
    return json.dumps({"journeys": [
        {"name": "First journey", "members": names[:3]},
        {"name": "Second journey", "members": names[3:]},
    ]})


# ── (a) live call populates the cache ────────────────────────────────────────


def test_live_call_populates_cache() -> None:
    cache = _MemCache()
    ufs, flows, names = _mega_fixture()
    out, tel = split_mega_user_flows(
        ufs, flows, client=_client_returning(_resp(names)), cache=cache,
    )
    assert tel["mega_split"] == 1
    assert tel["llm_calls"] == 1
    assert tel["cache_hits"] == 0
    kinds = {kind for kind, _ in cache.store}
    assert kinds == {CacheKind.LLM_UF_SPLIT.value}
    assert len(cache.store) == 1
    (stored,) = cache.store.values()
    assert stored["v"] == "v1"
    assert isinstance(stored["journeys"], list) and len(stored["journeys"]) == 2
    assert len(out) == 2  # two sub-UFs replaced the mega-UF (no residual)


# ── (b) warm cache replays identically with a RAISING client ────────────────


def test_warm_cache_replays_identically_with_raising_client() -> None:
    cache = _MemCache()
    ufs1, flows1, names = _mega_fixture()
    out1, tel1 = split_mega_user_flows(
        ufs1, flows1, client=_client_returning(_resp(names)), cache=cache,
    )
    assert tel1["cache_hits"] == 0 and tel1["llm_calls"] == 1

    ufs2, flows2, _ = _mega_fixture()
    out2, tel2 = split_mega_user_flows(
        ufs2, flows2, client=_raising_client(), cache=cache,
    )
    # No LLM call, $0, one hit — and the partition is byte-identical.
    assert tel2["cache_hits"] == 1
    assert tel2["llm_calls"] == 0
    assert tel2["cost_usd"] == 0.0
    assert tel2["mega_split"] == 1
    assert [u.model_dump() for u in out2] == [u.model_dump() for u in out1]
    # Flow.user_flow_id re-stamps replay identically too.
    stamps1 = {f.uuid: f.user_flow_id for f in flows1}
    stamps2 = {f.uuid: f.user_flow_id for f in flows2}
    assert stamps2 == stamps1


# ── (c) cache fault falls through to live (never-worse) ─────────────────────


def test_cache_fault_falls_through_to_live() -> None:
    ufs, flows, names = _mega_fixture()
    out, tel = split_mega_user_flows(
        ufs, flows, client=_client_returning(_resp(names)), cache=_BadCache(),
    )
    assert tel["mega_split"] == 1
    assert tel["cache_hits"] == 0
    assert tel["llm_calls"] == 1
    assert len(out) == 2


# ── (d) different model / input → different key (no false hits) ─────────────


def test_different_model_misses_cache() -> None:
    cache = _MemCache()
    ufs1, flows1, names = _mega_fixture()
    split_mega_user_flows(
        ufs1, flows1, client=_client_returning(_resp(names)), cache=cache,
    )
    assert len(cache.store) == 1

    # Same inputs, DIFFERENT model → key miss → the raising client degrades
    # (mega-UF kept) instead of serving the other model's partition.
    ufs2, flows2, _ = _mega_fixture()
    out2, tel2 = split_mega_user_flows(
        ufs2, flows2, client=_raising_client(),
        model="claude-haiku-4-5-20251001", cache=cache,
    )
    assert tel2["cache_hits"] == 0
    assert tel2["mega_split"] == 0
    assert [u.id for u in out2] == ["UF-001"]  # mega-UF kept


def test_different_input_misses_cache() -> None:
    cache = _MemCache()
    ufs1, flows1, names = _mega_fixture()
    split_mega_user_flows(
        ufs1, flows1, client=_client_returning(_resp(names)), cache=cache,
    )

    # A changed member journey name changes the prompt → different key.
    other_names = [f"other-{c}-flow" for c in "abcdefg"]
    flows2: list[Flow] = []
    member_ids: list[str] = []
    for n in other_names:
        for i in range(3):
            uid = f"{n}-{i}"
            flows2.append(_flow(n, uid))
            member_ids.append(uid)
    ufs2 = [_uf("UF-001", member_ids)]
    out2, tel2 = split_mega_user_flows(
        ufs2, flows2, client=_raising_client(), cache=cache,
    )
    assert tel2["cache_hits"] == 0
    assert tel2["mega_split"] == 0


# ── (e) env opt-out ──────────────────────────────────────────────────────────


def test_env_opt_out_disables_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _MemCache()
    ufs1, flows1, names = _mega_fixture()
    split_mega_user_flows(
        ufs1, flows1, client=_client_returning(_resp(names)), cache=cache,
    )
    assert len(cache.store) == 1

    monkeypatch.setenv("FAULTLINE_STAGE_6_7C_CACHE", "0")
    ufs2, flows2, _ = _mega_fixture()
    _out2, tel2 = split_mega_user_flows(
        ufs2, flows2, client=_raising_client(), cache=cache,
    )
    # Warm entry ignored → live path (which raises) → degrade, no hit.
    assert tel2["cache_hits"] == 0
    assert tel2["mega_split"] == 0
    assert len(cache.store) == 1  # and nothing new written


# ── (f) failures are never cached ────────────────────────────────────────────


def test_parse_failure_not_cached() -> None:
    cache = _MemCache()
    ufs, flows, _names = _mega_fixture()
    _out, tel = split_mega_user_flows(
        ufs, flows, client=_client_returning("not json"), cache=cache,
    )
    assert tel["mega_split"] == 0
    assert cache.store == {}
