"""Stage 3 flow-detection LLM cache (CacheKind.LLM_FLOWS) — content-hash
short-circuit tests.

Stage 3 is the main LLM stage and was the last uncached one, so a re-scan
re-ran it and (temp=0 not being bit-exact on Anthropic) produced divergent
user_flows / product_features. This cache makes an unchanged feature REPLAY
its parsed flows, so the downstream PF/UF are reproducible.

Covers the warm-cache contract, mirroring ``test_stage_4_residual_cache``:

  * cold run (miss) calls the LLM and STORES the parsed flows;
  * a second run against the same backend issues ZERO Haiku calls and
    replays byte-identical flows;
  * the content-hash key is stable across runs for identical inputs and
    DIFFERS when a member file's content signature changes (even when its
    exports/routes are unchanged) or when the model changes;
  * a broken cache backend never crashes a scan (fault swallowed);
  * the final ``flows[]`` order is deterministic regardless of the LLM's
    emission order (belt-and-suspenders ordering).
"""

from __future__ import annotations

import json
import threading
import types
from pathlib import Path
from typing import Any

from faultline.cache.backend import CacheKind, FilesystemCacheBackend
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_3_flows import (
    STAGE3_CACHE_VERSION,
    FlowSpec,
    _flow_cache_key,
    _sorted_flows,
    stage_3_flows,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


# Three exported functions → passes the MIN_EXPORTS flow gate → one Haiku
# call on a cold run.
_BODY = (
    "export function BillingPage() {}\n"
    "export function CheckoutForm() {}\n"
    "export function SubscriptionTable() {}\n"
)

_RESP = json.dumps({
    "flows": [
        {"name": "view-billing-flow", "symbols": ["BillingPage"]},
        {"name": "checkout-flow", "symbols": ["CheckoutForm"]},
    ],
})


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


def _feature(
    name: str, paths: tuple[str, ...], sources: tuple[str, ...] = ("route",),
) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths, sources=list(sources), confidence="medium",
    )


def _make_ts(tmp_path: Path, rel: str, body: str) -> None:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")


class _FakeAnthropic:
    """Records calls and replays canned responses (FIFO, last repeats)."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self._idx = 0
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent: "_FakeAnthropic") -> None:
            self._p = parent

        def create(self, **kwargs: Any) -> Any:
            with self._p._lock:
                self._p.call_count += 1
                self._p.calls.append(kwargs)
                if self._p._idx < len(self._p.responses):
                    text = self._p.responses[self._p._idx]
                    self._p._idx += 1
                else:
                    text = (
                        self._p.responses[-1]
                        if self._p.responses
                        else '{"flows": []}'
                    )
            content = [types.SimpleNamespace(text=text)]
            usage = types.SimpleNamespace(input_tokens=100, output_tokens=50)
            return types.SimpleNamespace(content=content, usage=usage)


class _BoomBackend:
    """A backend whose get/set always raise — proves faults are swallowed."""

    def get(self, kind: str, key: str) -> Any:
        raise RuntimeError("boom get")

    def set(self, kind: str, key: str, value: Any, *, ttl_seconds: Any = None) -> None:
        raise RuntimeError("boom set")

    def delete(self, kind: str, key: str) -> None:
        raise RuntimeError("boom delete")

    def load_namespace(self, kind: str) -> dict[str, Any]:
        return {}

    def flush(self) -> None:
        return None


def _flow_tuples(fwf: Any) -> list[tuple[str, str | None, int | None]]:
    return [
        (f.name, f.entry_point_file, f.entry_point_line) for f in fwf.flows
    ]


# ── Second run = 0 LLM calls, identical flows ───────────────────────────────


def test_second_run_zero_llm_calls_and_replays(tmp_path: Path) -> None:
    _make_ts(tmp_path, "app/billing/page.tsx", _BODY)
    feature = _feature("billing", ("app/billing/page.tsx",))
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx = _ctx(tmp_path, ["app/billing/page.tsx"], cache_backend=backend)

    c1 = _FakeAnthropic([_RESP])
    r1 = stage_3_flows([feature], ctx, client=c1, max_workers=2)
    assert c1.call_count == 1
    assert r1.llm_calls == 1
    assert r1.cache_hits == 0
    assert r1.cost_usd > 0.0
    assert r1.features_with_flows[0].flows  # produced flows

    # Second run, SAME backend + input → fully served from cache.
    c2 = _FakeAnthropic([_RESP])
    r2 = stage_3_flows([feature], ctx, client=c2, max_workers=2)
    assert c2.call_count == 0  # zero Haiku calls
    assert r2.llm_calls == 0
    assert r2.cache_hits == 1
    assert r2.cost_usd == 0.0  # no tokens recorded on a hit

    # Byte-identical flow output across cold and warm runs.
    assert _flow_tuples(r1.features_with_flows[0]) == _flow_tuples(
        r2.features_with_flows[0],
    )


# ── Miss stores the PARSED flows under the versioned key ────────────────────


def test_miss_stores_parsed_flows(tmp_path: Path) -> None:
    _make_ts(tmp_path, "app/billing/page.tsx", _BODY)
    feature = _feature("billing", ("app/billing/page.tsx",))
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx = _ctx(tmp_path, ["app/billing/page.tsx"], cache_backend=backend)

    stage_3_flows([feature], ctx, client=_FakeAnthropic([_RESP]), max_workers=2)

    ns = backend.load_namespace(CacheKind.LLM_FLOWS.value)
    assert len(ns) == 1, "expected exactly one flow-cache entry"
    (stored,) = ns.values()
    assert stored["version"] == STAGE3_CACHE_VERSION
    assert isinstance(stored["flows"], list) and stored["flows"]
    # Stored value is the parsed raw flows — no text / tokens / timestamps.
    assert set(stored.keys()) == {"version", "flows"}


# ── Key is content-sensitive; excludes run-varying values ───────────────────


def test_cache_key_stable_and_content_sensitive() -> None:
    feature = _feature("billing", ("app/b.ts", "app/a.ts"))
    base = dict(
        model="claude-haiku-4-5",
        system="SYS",
        exports=["A", "B", "C"],
        routes=[],
        content_sig={"app/a.ts": "aaa", "app/b.ts": "bbb"},
    )
    k1 = _flow_cache_key(feature, **base)
    k2 = _flow_cache_key(feature, **base)
    assert k1 == k2, "identical inputs must produce a stable key"

    # A file's content signature changes (same exports/routes) → new key.
    changed_sig = _flow_cache_key(
        feature, **{**base, "content_sig": {"app/a.ts": "aaa", "app/b.ts": "ZZZ"}},
    )
    assert k1 != changed_sig

    # Different model → different key (canonical model id is in the key).
    diff_model = _flow_cache_key(feature, **{**base, "model": "other"})
    assert k1 != diff_model

    # Path-tuple ORDER is a run-varying artefact of Stage 2 assembly — the
    # key sorts paths, so a reordered tuple keys identically.
    reordered = _feature("billing", ("app/a.ts", "app/b.ts"))
    assert _flow_cache_key(reordered, **base) == k1


def test_file_content_change_misses_cache(tmp_path: Path) -> None:
    """A byte change to a member file (exports unchanged) misses the cache —
    proving the per-file content signature is part of the key."""
    _make_ts(tmp_path, "app/billing/page.tsx", _BODY)
    feature = _feature("billing", ("app/billing/page.tsx",))
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx = _ctx(tmp_path, ["app/billing/page.tsx"], cache_backend=backend)

    c1 = _FakeAnthropic([_RESP])
    stage_3_flows([feature], ctx, client=c1, max_workers=2)
    assert c1.call_count == 1

    # Append a trailing comment: same three exports, different source bytes.
    _make_ts(tmp_path, "app/billing/page.tsx", _BODY + "// changed\n")
    c2 = _FakeAnthropic([_RESP])
    r2 = stage_3_flows([feature], ctx, client=c2, max_workers=2)
    assert c2.call_count == 1  # MISS → LLM re-called
    assert r2.cache_hits == 0


# ── Fault swallowed ─────────────────────────────────────────────────────────


def test_cache_fault_swallowed(tmp_path: Path) -> None:
    _make_ts(tmp_path, "app/billing/page.tsx", _BODY)
    feature = _feature("billing", ("app/billing/page.tsx",))
    ctx = _ctx(tmp_path, ["app/billing/page.tsx"], cache_backend=_BoomBackend())

    c = _FakeAnthropic([_RESP])
    r = stage_3_flows([feature], ctx, client=c, max_workers=2)
    # get() raised → treated as a miss → LLM called; set() raised → swallowed.
    assert c.call_count == 1
    assert r.llm_calls == 1
    assert r.cache_hits == 0
    assert r.features_with_flows[0].flows  # produced flows, no crash


# ── None backend = pre-cache behaviour ──────────────────────────────────────


def test_none_backend_no_short_circuit(tmp_path: Path) -> None:
    _make_ts(tmp_path, "app/billing/page.tsx", _BODY)
    feature = _feature("billing", ("app/billing/page.tsx",))
    ctx = _ctx(tmp_path, ["app/billing/page.tsx"], cache_backend=None)

    c1 = _FakeAnthropic([_RESP])
    stage_3_flows([feature], ctx, client=c1, max_workers=2)
    c2 = _FakeAnthropic([_RESP])
    r2 = stage_3_flows([feature], ctx, client=c2, max_workers=2)
    # No backend → no short-circuit → every run calls the LLM.
    assert c1.call_count == 1
    assert c2.call_count == 1
    assert r2.cache_hits == 0


# ── Deterministic flow ordering ─────────────────────────────────────────────


def test_flows_output_order_deterministic(tmp_path: Path) -> None:
    """The LLM emits flows out of line order; the final list is stably
    ordered by (entry_point_file, entry_point_line, name)."""
    body = (
        "export function Aaa() {}\n"   # line 1
        "export function Bbb() {}\n"   # line 2
        "export function Ccc() {}\n"   # line 3
    )
    _make_ts(tmp_path, "app/x/page.tsx", body)
    feature = _feature("x", ("app/x/page.tsx",))
    ctx = _ctx(tmp_path, ["app/x/page.tsx"])

    # Emit deliberately out of line order: Ccc(3), Aaa(1), Bbb(2).
    resp = json.dumps({"flows": [
        {"name": "zzz-flow", "symbols": ["Ccc"]},
        {"name": "aaa-flow", "symbols": ["Aaa"]},
        {"name": "mmm-flow", "symbols": ["Bbb"]},
    ]})
    r = stage_3_flows([feature], ctx, client=_FakeAnthropic([resp]), max_workers=2)
    names = [f.name for f in r.features_with_flows[0].flows]
    # Sorted by entry line: Aaa(1)->aaa-flow, Bbb(2)->mmm-flow, Ccc(3)->zzz-flow.
    assert names == ["aaa-flow", "mmm-flow", "zzz-flow"]


def test_sorted_flows_is_pure_reorder() -> None:
    flows = [
        FlowSpec(name="b-flow", entry_point_file="f.ts", entry_point_line=2),
        FlowSpec(name="a-flow", entry_point_file="f.ts", entry_point_line=1),
        FlowSpec(name="c-flow", entry_point_file="a.ts", entry_point_line=9),
    ]
    out = _sorted_flows(flows)
    # a.ts < f.ts, then by line within f.ts.
    assert [f.name for f in out] == ["c-flow", "a-flow", "b-flow"]
    # Same membership — a pure reorder, nothing dropped or added.
    assert {f.name for f in out} == {f.name for f in flows}
