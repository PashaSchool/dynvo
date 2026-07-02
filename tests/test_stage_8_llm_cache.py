"""Unit tests for the Stage 8 content-hash LLM cache (CacheKind.LLM_PRODUCT_CLUSTER).

Covers BOTH Stage-8 modes (they share the key/get/put helpers):
  * the Haiku label-mapper (``cluster_via_haiku`` in
    stage_8_marketing_clusterer) — explicit contract tests (a)-(f);
  * the Sonnet analyst (``run_stage_8_analyst``) — end-to-end replay with a
    raising client, cache-fault fall-through, and model-key discrimination;
  * the analyst's name-validator rename retry (``_call_rename_retry``).

Contract (mirrors tests/test_stage_3_flows_cache.py + the 6.7d cache tests):
  (a) a live call populates the cache;
  (b) a second run over UNCHANGED inputs replays byte-identically with a
      RAISING client (proving no LLM call) + cache_hit telemetry;
  (c) a cache backend fault falls through to the live call (never-worse);
  (d) a different model / different input produces a different key — no
      false hits;
  (e) FAULTLINE_STAGE_8_CACHE=0 opts out;
  (f) failures (unparseable output) are never cached.

NOTE: the pre-existing marketing-PAGE cache (kind ``marketing``, slug-keyed,
7-day TTL) is untouched by this feature and not under test here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.analyzer.marketing_fetcher import MarketingTaxonomy
from faultline.cache.backend import CacheKind
from faultline.models.types import Feature
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_8_analyst import (
    _call_rename_retry,
    run_stage_8_analyst,
)
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    cluster_via_haiku,
)


# ── Fakes (mirror tests/test_stage_8_marketing_clusterer.py) ────────────


class _FakeUsage:
    def __init__(self, in_t: int, out_t: int) -> None:
        self.input_tokens = in_t
        self.output_tokens = out_t


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(self, text: str, in_t: int = 1000, out_t: int = 200) -> None:
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage(in_t, out_t)


class _FakeMessages:
    def __init__(self, response_text: str) -> None:
        self._text = response_text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        return _FakeMessage(self._text)


class _FakeClient:
    def __init__(self, response_text: str) -> None:
        self.messages = _FakeMessages(response_text)


class _RaisingClient:
    class _Msgs:
        def create(self, **kwargs: Any) -> Any:
            raise AssertionError("LLM must NOT be called on a warm cache")

    def __init__(self) -> None:
        self.messages = self._Msgs()


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


# ── Fixtures ─────────────────────────────────────────────────────────────


# Fixed timestamp: models an UNCHANGED repo (dev-feature git metadata is
# identical across the two runs of a replay test).
_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _feat(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["alice"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=_TS,
        health_score=80.0,
        flows=[],
        layer="developer",
    )


def _taxonomy() -> MarketingTaxonomy:
    return MarketingTaxonomy(
        repo_slug="myrepo",
        source_url="https://example.com",
        fetched_at="2026-05-21T00:00:00+00:00",
        product_features=("Authentication", "Billing", "Surveys"),
        confidence=0.9,
        notes="test",
    )


def _dev_feats() -> list[Feature]:
    return [
        _feat("auth-handlers", [
            "packages/core/auth.ts",
            "packages/core/oauth.ts",
            "packages/core/email-login.ts",
        ]),
        _feat("billing", [
            "apps/billing/stripe.ts",
            "apps/billing/subscriptions.ts",
        ]),
    ]


_HAIKU_RESP = json.dumps({"mappings": [
    {"developer": "auth-handlers", "product": "Authentication"},
    {"developer": "billing", "product": "Billing"},
]})


@pytest.fixture(autouse=True)
def _isolated_base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the engine base dir so tests never touch ~/.faultline."""
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(tmp_path))


# ═══ Haiku label-mapper (cluster_via_haiku) ══════════════════════════════


def test_haiku_live_call_populates_cache() -> None:
    cache = _MemCache()
    mapping, tel = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_FakeClient(_HAIKU_RESP), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert mapping == {"auth-handlers": "Authentication", "billing": "Billing"}
    assert tel["llm_calls"] == 1
    assert tel["cache_hits"] == 0
    kinds = {kind for kind, _ in cache.store}
    assert kinds == {CacheKind.LLM_PRODUCT_CLUSTER.value}
    (stored,) = cache.store.values()
    assert stored["v"] == "v1"
    assert stored["mappings"]["auth-handlers"] == "Authentication"


def test_haiku_warm_cache_replays_identically_with_raising_client() -> None:
    cache = _MemCache()
    mapping1, tel1 = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_FakeClient(_HAIKU_RESP), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert tel1["cache_hits"] == 0

    mapping2, tel2 = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_RaisingClient(), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert mapping2 == mapping1
    assert tel2["cache_hits"] == 1
    assert tel2["llm_calls"] == 0
    assert tel2["cost_usd"] == 0.0
    assert tel2["tokens_in"] == 0 and tel2["tokens_out"] == 0
    assert tel2["mappings_accepted"] == tel1["mappings_accepted"]


def test_haiku_cache_fault_falls_through_to_live() -> None:
    mapping, tel = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_FakeClient(_HAIKU_RESP), model="claude-haiku-4-5-20251001",
        cache=_BadCache(),
    )
    assert mapping == {"auth-handlers": "Authentication", "billing": "Billing"}
    assert tel["cache_hits"] == 0
    assert tel["llm_calls"] == 1


def test_haiku_different_model_and_input_miss_cache() -> None:
    cache = _MemCache()
    cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_FakeClient(_HAIKU_RESP), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert len(cache.store) == 1

    # DIFFERENT model → miss → the raising client degrades to an empty
    # mapping instead of serving the other model's answer.
    mapping_m, tel_m = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_RaisingClient(), model="claude-sonnet-4-6",
        cache=cache,
    )
    assert mapping_m == {} and tel_m["cache_hits"] == 0

    # DIFFERENT input (extra dev feature) → miss too.
    feats = _dev_feats() + [_feat("survey-builder", ["apps/web/surveys/b.tsx"])]
    mapping_i, tel_i = cluster_via_haiku(
        feats, _taxonomy(),
        client=_RaisingClient(), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert mapping_i == {} and tel_i["cache_hits"] == 0


def test_haiku_env_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _MemCache()
    cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_FakeClient(_HAIKU_RESP), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert len(cache.store) == 1

    monkeypatch.setenv("FAULTLINE_STAGE_8_CACHE", "0")
    mapping, tel = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_RaisingClient(), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    # Warm entry ignored → live (raising) path → empty mapping, no write.
    assert mapping == {} and tel["cache_hits"] == 0
    assert len(cache.store) == 1


def test_haiku_parse_failure_not_cached() -> None:
    cache = _MemCache()
    mapping, _tel = cluster_via_haiku(
        _dev_feats(), _taxonomy(),
        client=_FakeClient("not json at all"), model="claude-haiku-4-5-20251001",
        cache=cache,
    )
    assert mapping == {}
    assert cache.store == {}


# ═══ Sonnet analyst (run_stage_8_analyst) ════════════════════════════════


def _ctx(repo_path: Path, cache: Any | None) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="next-monorepo",
        monorepo=True,
        workspaces=None,
        tracked_files=[],
        commits=[],
        cache_backend=cache,
    )


@pytest.fixture(autouse=True)
def _block_real_marketing_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op every external fetch + discovery hook (hermetic suite)."""
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.discover_marketing_site",
        lambda repo_path: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_page_text",
        lambda url, timeout_s=15: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_llms_txt_urls",
        lambda primary: [],
    )


_ANALYST_RESP = json.dumps({"product_features": [
    {
        "name": "OAuth + Email Auth",
        "description": "Sessions, magic link, OAuth",
        "member_dev_features": ["auth-handlers"],
        "confidence": 0.92,
        "grounded_in": ["packages/core"],
    },
    {
        "name": "Billing & Subscriptions",
        "description": "Stripe integration",
        "member_dev_features": ["billing"],
        "confidence": 0.88,
        "grounded_in": ["apps/billing"],
    },
]})


def _pf_snapshot(result: Any) -> list[tuple]:
    return [
        (pf.name, pf.display_name, pf.description, tuple(pf.paths), pf.layer)
        for pf in result.product_features
    ]


def test_analyst_live_populates_then_replays_with_raising_client(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cache = _MemCache()

    result1 = run_stage_8_analyst(
        _ctx(repo_root, cache), _dev_feats(), [],
        client=_FakeClient(_ANALYST_RESP), cost_tracker=None,
    )
    assert result1.telemetry["analyst_called"] is True
    assert result1.telemetry["llm_calls"] == 1
    assert result1.telemetry["llm_cache_hits"] == 0
    assert any(
        kind == CacheKind.LLM_PRODUCT_CLUSTER.value for kind, _ in cache.store
    )

    # Second run over IDENTICAL inputs: the raising client proves no LLM
    # call is made; the replayed analysis reconstructs identically.
    result2 = run_stage_8_analyst(
        _ctx(repo_root, cache), _dev_feats(), [],
        client=_RaisingClient(), cost_tracker=None,
    )
    assert result2.telemetry["llm_cache_hits"] == 1
    assert result2.telemetry["llm_calls"] == 0
    assert result2.telemetry["analyst_cost_usd"] == 0.0
    assert result2.telemetry["fallback_used"] is False
    assert _pf_snapshot(result2) == _pf_snapshot(result1)
    assert result2.dev_to_product_map == result1.dev_to_product_map
    assert result2.member_flows_map == result1.member_flows_map


def test_analyst_cache_fault_falls_through_to_live(tmp_path: Path) -> None:
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    result = run_stage_8_analyst(
        _ctx(repo_root, _BadCache()), _dev_feats(), [],
        client=_FakeClient(_ANALYST_RESP), cost_tracker=None,
    )
    assert result.telemetry["analyst_called"] is True
    assert result.telemetry["fallback_used"] is False
    assert result.telemetry["llm_cache_hits"] == 0
    assert result.telemetry["llm_calls"] == 1
    assert len(result.product_features) == 2


def test_analyst_different_model_misses_cache(tmp_path: Path) -> None:
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cache = _MemCache()
    run_stage_8_analyst(
        _ctx(repo_root, cache), _dev_feats(), [],
        client=_FakeClient(_ANALYST_RESP), cost_tracker=None,
    )

    # DIFFERENT model → key miss → the raising client forces the Haiku
    # fallback path instead of serving the default model's cached analysis.
    result = run_stage_8_analyst(
        _ctx(repo_root, cache), _dev_feats(), [],
        client=_RaisingClient(), model="claude-haiku-4-5",
        cost_tracker=None,
    )
    # (fallback telemetry carries no llm_cache_hits key — default 0)
    assert result.telemetry.get("llm_cache_hits", 0) == 0
    assert result.telemetry.get("fallback_used") is True


def test_analyst_env_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cache = _MemCache()
    run_stage_8_analyst(
        _ctx(repo_root, cache), _dev_feats(), [],
        client=_FakeClient(_ANALYST_RESP), cost_tracker=None,
    )
    n_entries = len(cache.store)
    assert n_entries >= 1

    monkeypatch.setenv("FAULTLINE_STAGE_8_CACHE", "0")
    result = run_stage_8_analyst(
        _ctx(repo_root, cache), _dev_feats(), [],
        client=_RaisingClient(), cost_tracker=None,
    )
    # Warm entry ignored → live (raising) → fallback; nothing new written.
    assert result.telemetry.get("llm_cache_hits", 0) == 0
    assert len(cache.store) == n_entries


# ═══ Name-validator rename retry (_call_rename_retry) ════════════════════


_FAILING = [{
    "name": "Quantum Flux Manager",
    "slug": "quantum-flux-manager",
    "prohibited": ["quantum", "flux"],
    "member_dev_features": ["auth-handlers"],
    "sample_paths": ["packages/core/auth.ts"],
    "product_strings": [],
}]

_RENAME_RESP = json.dumps({"renames": [
    {"old": "Quantum Flux Manager", "new": "Auth Handlers"},
]})


def test_rename_retry_populates_then_replays() -> None:
    cache = _MemCache()
    renames1, hit1 = _call_rename_retry(
        _FakeClient(_RENAME_RESP), model="claude-sonnet-4-6",
        failing=_FAILING, cost_tracker=None, llm_health=None, cache=cache,
    )
    assert renames1 == {"Quantum Flux Manager": "Auth Handlers"}
    assert hit1 is False
    assert len(cache.store) == 1

    renames2, hit2 = _call_rename_retry(
        _RaisingClient(), model="claude-sonnet-4-6",
        failing=_FAILING, cost_tracker=None, llm_health=None, cache=cache,
    )
    assert renames2 == renames1
    assert hit2 is True


def test_rename_retry_failure_not_cached_and_fault_tolerant() -> None:
    cache = _MemCache()
    renames, hit = _call_rename_retry(
        _FakeClient("not json"), model="claude-sonnet-4-6",
        failing=_FAILING, cost_tracker=None, llm_health=None, cache=cache,
    )
    assert renames == {} and hit is False
    assert cache.store == {}

    # A raising cache never aborts the call.
    renames_b, hit_b = _call_rename_retry(
        _FakeClient(_RENAME_RESP), model="claude-sonnet-4-6",
        failing=_FAILING, cost_tracker=None, llm_health=None, cache=_BadCache(),
    )
    assert renames_b == {"Quantum Flux Manager": "Auth Handlers"}
    assert hit_b is False
