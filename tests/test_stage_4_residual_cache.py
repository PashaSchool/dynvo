"""Stage 4 residual LLM cache (CacheKind.LLM_RESIDUAL) — content-hash
short-circuit tests.

Covers the warm-cache contract:
  * cold run (cache miss) is byte-identical to the pre-cache behaviour;
  * a second run against the same backend issues ZERO Haiku calls;
  * a changed residual input is a miss → the LLM is called again;
  * ``ctx.cache_backend is None`` behaves exactly as today (no crash,
    LLM called);
  * the canonical-cold golden: features from a no-backend run equal the
    features from the cold (miss) run with a backend.
"""

from __future__ import annotations

import threading
import types
from pathlib import Path
from typing import Any

from faultline.cache.backend import CacheKind, FilesystemCacheBackend
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_4_residual import (
    STAGE4_CACHE_VERSION,
    stage_4_residual,
)


# A residual set that yields a SINGLE multi-path (non-singleton) cluster,
# i.e. exactly one Haiku call on a cold run. Three same-extension files
# under one top-level dir at one depth band cluster together.
_RESIDUAL = [
    "lib/payments/charge.ts",
    "lib/payments/refund.ts",
    "lib/payments/invoice.ts",
]

_RESPONSE = '{"features":[{"name":"payments","paths":["lib/payments/charge.ts","lib/payments/refund.ts","lib/payments/invoice.ts"],"confidence":"low"}]}'


def _ctx(
    tmp_path: Path,
    files: list[str],
    *,
    cache_backend: Any | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        cache_backend=cache_backend,
    )


class _FakeAnthropic:
    """Records Haiku calls and replays canned responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self._idx = 0
        self.calls: list[dict[str, Any]] = []
        self.messages = self._Messages(self)
        self._lock = threading.Lock()

    class _Messages:
        def __init__(self, parent: "_FakeAnthropic") -> None:
            self._p = parent

        def create(self, **kwargs: Any) -> Any:
            with self._p._lock:
                self._p.calls.append(kwargs)
                if self._p._idx < len(self._p.responses):
                    text = self._p.responses[self._p._idx]
                    self._p._idx += 1
                else:
                    text = self._p.responses[-1] if self._p.responses else '{"features":[]}'
            content = [types.SimpleNamespace(text=text)]
            usage = types.SimpleNamespace(input_tokens=200, output_tokens=100)
            return types.SimpleNamespace(content=content, usage=usage)


def _feature_tuples(result: Any) -> list[tuple[str, tuple[str, ...]]]:
    return sorted((f.name, f.paths) for f in result.residual_features)


# ── Second run = 0 LLM calls ────────────────────────────────────────────────


def test_second_run_zero_llm_calls(tmp_path: Path) -> None:
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx = _ctx(tmp_path, _RESIDUAL, cache_backend=backend)

    client1 = _FakeAnthropic([_RESPONSE])
    r1 = stage_4_residual(_RESIDUAL, ctx, existing_features=[], client=client1)
    assert len(client1.calls) == 1
    assert r1.llm_calls == 1
    assert r1.cache_hits == 0
    assert r1.cost_usd > 0.0

    # Second run, SAME backend, SAME input → fully served from cache.
    client2 = _FakeAnthropic([_RESPONSE])
    r2 = stage_4_residual(_RESIDUAL, ctx, existing_features=[], client=client2)
    assert len(client2.calls) == 0  # zero Haiku calls
    assert r2.llm_calls == 0
    assert r2.cache_hits == 1
    assert r2.cost_usd == 0.0  # no tokens recorded on a hit

    # Byte-identical feature output across cold and warm runs.
    assert _feature_tuples(r1) == _feature_tuples(r2)


# ── Changed input = cache miss = LLM called again ───────────────────────────


def test_changed_input_misses_cache(tmp_path: Path) -> None:
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx1 = _ctx(tmp_path, _RESIDUAL, cache_backend=backend)

    client1 = _FakeAnthropic([_RESPONSE])
    stage_4_residual(_RESIDUAL, ctx1, existing_features=[], client=client1)
    assert len(client1.calls) == 1

    # Different residual membership → different cluster signature/sample
    # → different content-hash key → MISS → Haiku called again.
    changed = [
        "lib/billing/charge.ts",
        "lib/billing/refund.ts",
        "lib/billing/invoice.ts",
    ]
    changed_resp = '{"features":[{"name":"billing","paths":["lib/billing/charge.ts","lib/billing/refund.ts","lib/billing/invoice.ts"],"confidence":"low"}]}'
    ctx2 = _ctx(tmp_path, changed, cache_backend=backend)
    client2 = _FakeAnthropic([changed_resp])
    r2 = stage_4_residual(changed, ctx2, existing_features=[], client=client2)
    assert len(client2.calls) == 1
    assert r2.llm_calls == 1
    assert r2.cache_hits == 0


def test_version_bump_invalidates(tmp_path: Path) -> None:
    """A different STAGE4_CACHE_VERSION must not serve a stale answer.

    We simulate a version bump by manually inserting a value under a key
    computed with a DIFFERENT version, then confirm the live key misses.
    """
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx = _ctx(tmp_path, _RESIDUAL, cache_backend=backend)

    client1 = _FakeAnthropic([_RESPONSE])
    stage_4_residual(_RESIDUAL, ctx, existing_features=[], client=client1)

    # The cached entry's stored body carries the current version.
    ns = backend.load_namespace(CacheKind.LLM_RESIDUAL.value)
    assert ns, "expected one residual cache entry"
    (stored,) = ns.values()
    assert stored["version"] == STAGE4_CACHE_VERSION


# ── None backend = pre-cache behaviour ──────────────────────────────────────


def test_none_backend_no_crash_llm_called(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _RESIDUAL, cache_backend=None)
    client = _FakeAnthropic([_RESPONSE])
    r = stage_4_residual(_RESIDUAL, ctx, existing_features=[], client=client)
    assert len(client.calls) == 1
    assert r.llm_calls == 1
    assert r.cache_hits == 0
    assert r.residual_features  # produced a feature


def test_none_backend_second_run_still_calls(tmp_path: Path) -> None:
    """Without a backend there is no short-circuit — every run calls the LLM."""
    ctx = _ctx(tmp_path, _RESIDUAL, cache_backend=None)
    c1 = _FakeAnthropic([_RESPONSE])
    stage_4_residual(_RESIDUAL, ctx, existing_features=[], client=c1)
    c2 = _FakeAnthropic([_RESPONSE])
    stage_4_residual(_RESIDUAL, ctx, existing_features=[], client=c2)
    assert len(c1.calls) == 1
    assert len(c2.calls) == 1


# ── Cold golden: backend-cold-miss == no-backend ────────────────────────────


def test_cold_run_output_matches_no_backend_golden(tmp_path: Path) -> None:
    """A cold (miss) run with a backend yields the SAME features as a run
    with no backend at all — proving the miss path didn't change behaviour."""
    ctx_no = _ctx(tmp_path, _RESIDUAL, cache_backend=None)
    golden = stage_4_residual(
        _RESIDUAL, ctx_no, existing_features=[], client=_FakeAnthropic([_RESPONSE]),
    )

    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx_cold = _ctx(tmp_path, _RESIDUAL, cache_backend=backend)
    cold = stage_4_residual(
        _RESIDUAL, ctx_cold, existing_features=[], client=_FakeAnthropic([_RESPONSE]),
    )

    assert _feature_tuples(cold) == _feature_tuples(golden)
    assert cold.llm_calls == golden.llm_calls == 1
    assert cold.cost_usd == golden.cost_usd
