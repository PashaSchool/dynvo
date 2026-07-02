"""Stage 3 CHUNKED flow detection for OVERSIZED features.

A giant feature (supabase "studio": 2000+ files) previously got ONE Haiku
call capped at MAX_FLOWS_PER_FEATURE=12 flows for dozens of real journeys.
Stage 3 now partitions an OVERSIZED feature (Stage-8.9 contract, constants
IMPORTED — rule-no-magic-tuning) by its widest directory fan-out and runs
the existing per-feature LLM machinery once per chunk.

Covers:

  * chunk partition determinism + file conservation (order-independent);
  * the oversized gate reuses the Stage-8.9 constants (3 scales per
    rule-no-magic-tuning);
  * chunked feature issues one call per chunk and may exceed the old
    12-flow single-call ceiling; per-chunk cap + SHARED S7-B entry dedup;
  * non-oversized path byte-unchanged (cache-key payload stability);
  * per-chunk cache hit/replay (2nd run = zero Haiku calls);
  * chunk telemetry counts;
  * pathological wide fan-out bounded by the structural cap (smallest
    chunks merge into the residual);
  * FAULTLINE_STAGE3_CHUNKED=0 kill switch restores the single-call path.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from faultline.cache.backend import CacheKind, FilesystemCacheBackend
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_3_flows import (
    MAX_FLOWS_PER_FEATURE,
    STAGE3_CACHE_VERSION,
    _flow_cache_key,
    _oversized_cut,
    _plan_chunks,
    _validate_and_attach_lines,
    _widest_fanout,
    stage_3_flows,
)
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _OVERSIZED_MEDIAN_MULT,
    _OVERSIZED_SHARE,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


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


_DOMAINS = ("auth", "billing", "storage")


def _giant_repo(tmp_path: Path) -> tuple[DeveloperFeature, list[DeveloperFeature], list[str]]:
    """One OVERSIZED feature (12 files, 3-domain fan-out under
    ``giant/app``) + three 1-file features that pin the repo median low.

    Sizes [12, 1, 1, 1] → median=max(2,1)=2, total=15,
    cut=max(2*2, ceil(0.15*15))=4 → the giant (12>4) is oversized;
    max_chunks = 12 // 2 = 6 ≥ 3 domains → chunks = auth/billing/storage.
    """
    giant_paths: list[str] = []
    for dom in _DOMAINS:
        for i in range(4):
            rel = f"giant/app/{dom}/f{i}.ts"
            _make_ts(
                tmp_path, rel,
                f"export function {dom.title()}Fn{i}() {{}}\n",
            )
            giant_paths.append(rel)
    small: list[DeveloperFeature] = []
    for j in range(3):
        rel = f"small{j}/index.ts"
        _make_ts(tmp_path, rel, "const x = 1\n")  # 0 exports → no LLM call
        small.append(_feature(f"small{j}", (rel,), sources=("package",)))
    giant = _feature("giant", tuple(giant_paths))
    files = giant_paths + [f.paths[0] for f in small]
    return giant, small, files


def _resp(names: list[str]) -> str:
    return json.dumps({"flows": [{"name": n, "description": ""} for n in names]})


def _chunk_resps(per_chunk: int = 5) -> list[str]:
    """One canned response per domain chunk, 5 uniquely-named flows each."""
    return [
        _resp([f"manage-{dom}-thing{i}-flow" for i in range(per_chunk)])
        for dom in _DOMAINS
    ]


# ── Chunk partition: determinism + conservation ─────────────────────────────


def test_chunk_partition_deterministic_and_conserving() -> None:
    paths = [
        f"apps/studio/components/interfaces/{d}/f{i}.tsx"
        for d in ("Auth", "Database", "Storage", "Billing")
        for i in range(5)
    ] + ["apps/studio/lib/util.ts", "apps/studio/pages/index.tsx"]

    plan = _plan_chunks(sorted(paths), 6)
    assert plan is not None

    # Order-independence: any input permutation yields the identical plan
    # (the house set-iteration trap class — assert, don't assume).
    rng = random.Random(1234)
    for _ in range(5):
        shuffled = list(paths)
        rng.shuffle(shuffled)
        assert _plan_chunks(shuffled, 6) == plan

    # File conservation: every input path in exactly one chunk.
    all_files = [p for _, files in plan for p in files]
    assert sorted(all_files) == sorted(paths)
    assert len(all_files) == len(set(all_files))

    # Widest fan-out level found (4 children beats every 1-child ancestor).
    container, children = _widest_fanout(paths)  # type: ignore[misc]
    assert container == "apps/studio/components/interfaces"
    assert sorted(children) == ["Auth", "Billing", "Database", "Storage"]


def test_no_fanout_returns_none() -> None:
    # Single flat dir → 1 child at every level → no split (< _MIN_DOMAINS).
    paths = [f"src/only/f{i}.ts" for i in range(10)]
    assert _plan_chunks(sorted(paths), 5) is None
    # Degenerate cap → no chunking regardless of structure.
    assert _plan_chunks(["a/x/1.ts", "b/y/2.ts"], 1) is None


def test_pathological_wide_fanout_bounded_by_cap() -> None:
    # 40 sibling dirs, 1 file each. A naive chunker would fire 40 calls;
    # the cap keeps the largest (cap-1) chunks and merges the smallest
    # into the residual.
    paths = [f"src/mod{i:02d}/f.ts" for i in range(40)]
    plan = _plan_chunks(sorted(paths), 5)
    assert plan is not None
    assert len(plan) <= 5
    labels = [name for name, _ in plan]
    assert labels[-1] == "__residual__"
    # Conservation still holds after the merge.
    all_files = [p for _, files in plan for p in files]
    assert sorted(all_files) == sorted(paths)
    # Residual absorbed the overflow (smallest chunks merged up).
    assert len(plan[-1][1]) == 40 - (len(plan) - 1)


# ── Oversized gate: Stage-8.9 contract reuse (3 scales) ─────────────────────


def _sized_features(sizes: list[int]) -> list[DeveloperFeature]:
    feats = []
    for i, n in enumerate(sizes):
        feats.append(
            _feature(f"f{i}", tuple(f"pkg{i}/m{j}.ts" for j in range(n))),
        )
    return feats


@pytest.mark.parametrize(
    "sizes",
    [
        [1, 1, 3],                     # tiny repo
        [5, 8, 10, 12, 60],            # medium SaaS
        [30] * 40 + [900],             # mega-monorepo
    ],
)
def test_oversized_cut_matches_stage_8_9_formula(sizes: list[int]) -> None:
    import math
    import statistics

    feats = _sized_features(sizes)
    got = _oversized_cut(feats)
    assert got is not None
    cut, median = got
    exp_median = max(2, int(statistics.median(sizes)))
    exp_cut = max(
        _OVERSIZED_MEDIAN_MULT * exp_median,
        math.ceil(_OVERSIZED_SHARE * sum(sizes)),  # paths are all distinct
    )
    assert median == exp_median
    assert cut == exp_cut


def test_single_feature_repo_never_oversized() -> None:
    feats = _sized_features([100])
    cut, median = _oversized_cut(feats)  # type: ignore[misc]
    assert median == 100
    assert not (100 > cut)  # its own 2x-median cut can't be exceeded


def test_oversized_cut_empty_input() -> None:
    assert _oversized_cut([]) is None
    assert _oversized_cut([_feature("empty", ())]) is None


# ── Chunked detection end-to-end (fake client) ──────────────────────────────


def test_chunked_feature_exceeds_single_call_ceiling(tmp_path: Path) -> None:
    giant, small, files = _giant_repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    client = _FakeAnthropic(_chunk_resps(5))

    result = stage_3_flows([giant] + small, ctx, client=client, max_workers=1)

    # One call per domain chunk; small features have no LLM surface.
    assert client.call_count == 3
    assert result.llm_calls == 3

    giant_fwf = result.features_with_flows[0]
    assert giant_fwf.feature.name == "giant"
    # 3 chunks x 5 flows = 15 > the old single-call ceiling of 12.
    assert len(giant_fwf.flows) == 15 > MAX_FLOWS_PER_FEATURE
    assert "chunked oversized feature into 3 chunk(s)" in giant_fwf.rationale

    # Each chunk prompt carried ONLY that chunk's paths.
    for call in client.calls:
        user = call["messages"][0]["content"]
        doms_in_prompt = [d for d in _DOMAINS if f"giant/app/{d}/" in user]
        assert len(doms_in_prompt) == 1

    # Telemetry counts.
    assert result.chunk_telemetry == {
        "features_chunked": 1,
        "chunks_total": 3,
        "chunk_llm_calls": 3,
        "chunk_cache_hits": 0,
        "flows_from_chunks": 15,
    }


def test_chunk_call_reuses_per_chunk_flow_cap(tmp_path: Path) -> None:
    # A chunk answering 20 flows is capped at MAX_FLOWS_PER_FEATURE per
    # CHUNK (the point: the cap is per call, no longer per feature).
    giant, small, files = _giant_repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    over = _resp([f"manage-x{i}-flow" for i in range(20)])
    client = _FakeAnthropic([over, over, over])

    result = stage_3_flows([giant] + small, ctx, client=client, max_workers=1)
    giant_fwf = result.features_with_flows[0]
    assert len(giant_fwf.flows) == 3 * MAX_FLOWS_PER_FEATURE


def test_kill_switch_restores_single_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE3_CHUNKED", "0")
    giant, small, files = _giant_repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    client = _FakeAnthropic([_resp(["manage-giant-flow"])])

    result = stage_3_flows([giant] + small, ctx, client=client, max_workers=1)
    assert client.call_count == 1  # whole-feature single call
    giant_fwf = result.features_with_flows[0]
    assert "chunked" not in giant_fwf.rationale
    assert result.chunk_telemetry == {
        "features_chunked": 0,
        "chunks_total": 0,
        "chunk_llm_calls": 0,
        "chunk_cache_hits": 0,
        "flows_from_chunks": 0,
    }


# ── S7-B entry dedup SHARED across chunks ───────────────────────────────────


def test_seen_entries_shared_across_calls_dedups() -> None:
    sym_loc = {"A": ("f.ts", 3), "B": ("g.ts", 7)}
    shared: set[tuple[str, int]] = set()

    v1, _ = _validate_and_attach_lines(
        [{"name": "create-a-flow", "symbols": ["A"]}],
        sym_loc, seen_entries=shared,
    )
    assert [f.name for f in v1] == ["create-a-flow"]

    # Second "chunk": a flow at A's entry is a same-entry twin → dropped;
    # B's entry is new → kept.
    v2, notes = _validate_and_attach_lines(
        [
            {"name": "manage-a-flow", "symbols": ["A"]},
            {"name": "manage-b-flow", "symbols": ["B"]},
        ],
        sym_loc, seen_entries=shared,
    )
    assert [f.name for f in v2] == ["manage-b-flow"]
    assert any("deduped 1 flow" in n for n in notes)


def test_seen_entries_default_is_fresh_per_call() -> None:
    # Byte-identical legacy behaviour when the caller passes nothing.
    sym_loc = {"A": ("f.ts", 3)}
    raw = [{"name": "create-a-flow", "symbols": ["A"]}]
    v1, _ = _validate_and_attach_lines(raw, sym_loc)
    v2, _ = _validate_and_attach_lines(raw, sym_loc)
    assert len(v1) == len(v2) == 1


# ── Non-oversized path byte-unchanged (cache-key stability) ─────────────────


def test_whole_feature_cache_key_payload_unchanged() -> None:
    """The default (paths=None) key derivation is byte-identical to the
    pre-chunking payload — existing warm-cache entries still hit."""
    feature = _feature("billing", ("app/b.ts", "app/a.ts"))
    kwargs = dict(
        model="claude-haiku-4-5",
        system="SYS",
        exports=["A", "B", "C"],
        routes=["GET /x"],
        content_sig={"app/a.ts": "aaa", "app/b.ts": "bbb"},
    )
    # Inline re-implementation of the ORIGINAL (pre-chunking) derivation.
    legacy_payload = json.dumps(
        {
            "version": STAGE3_CACHE_VERSION,
            "model": kwargs["model"],
            "system": kwargs["system"],
            "name": feature.name,
            "paths": sorted(feature.paths),
            "exports": list(kwargs["exports"]),
            "routes": list(kwargs["routes"]),
            "content_sig": kwargs["content_sig"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    legacy_key = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()[:32]

    assert _flow_cache_key(feature, **kwargs) == legacy_key
    # Passing the full path set explicitly is the same key…
    assert _flow_cache_key(
        feature, **kwargs, paths=list(feature.paths),
    ) == legacy_key
    # …a chunk (proper subset) is a DIFFERENT key — no collision with a
    # stale whole-feature entry, hence no STAGE3_CACHE_VERSION bump.
    assert _flow_cache_key(feature, **kwargs, paths=["app/a.ts"]) != legacy_key


def test_non_oversized_feature_single_call_unchanged(tmp_path: Path) -> None:
    body = (
        "export function BillingPage() {}\n"
        "export function CheckoutForm() {}\n"
        "export function SubscriptionTable() {}\n"
    )
    _make_ts(tmp_path, "app/billing/page.tsx", body)
    feature = _feature("billing", ("app/billing/page.tsx",))
    ctx = _ctx(tmp_path, ["app/billing/page.tsx"])
    client = _FakeAnthropic([_resp(["view-billing-flow"])])

    result = stage_3_flows([feature], ctx, client=client, max_workers=1)
    assert client.call_count == 1
    fwf = result.features_with_flows[0]
    assert fwf.rationale.startswith("detected 1 flows")
    assert result.chunk_telemetry["features_chunked"] == 0


# ── Per-chunk cache hit/replay ───────────────────────────────────────────────


def test_chunk_cache_replay_zero_llm_calls(tmp_path: Path) -> None:
    giant, small, files = _giant_repo(tmp_path)
    backend = FilesystemCacheBackend(base_dir=tmp_path / "cache")
    ctx = _ctx(tmp_path, files, cache_backend=backend)

    c1 = _FakeAnthropic(_chunk_resps(4))
    r1 = stage_3_flows([giant] + small, ctx, client=c1, max_workers=1)
    assert c1.call_count == 3
    assert r1.chunk_telemetry["chunk_llm_calls"] == 3
    assert r1.chunk_telemetry["chunk_cache_hits"] == 0

    # One cache entry PER CHUNK, all under the versioned LLM_FLOWS kind.
    ns = backend.load_namespace(CacheKind.LLM_FLOWS.value)
    assert len(ns) == 3
    assert all(v["version"] == STAGE3_CACHE_VERSION for v in ns.values())

    # Warm run: zero Haiku calls, per-chunk hits, identical flows.
    c2 = _FakeAnthropic(_chunk_resps(4))
    r2 = stage_3_flows([giant] + small, ctx, client=c2, max_workers=1)
    assert c2.call_count == 0
    assert r2.llm_calls == 0
    assert r2.cache_hits == 3
    assert r2.chunk_telemetry["chunk_cache_hits"] == 3
    assert r2.chunk_telemetry["chunk_llm_calls"] == 0

    def _tuples(fwf: Any) -> list[tuple[str, Any, Any]]:
        return [
            (f.name, f.entry_point_file, f.entry_point_line) for f in fwf.flows
        ]

    assert _tuples(r1.features_with_flows[0]) == _tuples(
        r2.features_with_flows[0],
    )


def test_sparse_chunk_of_nonroute_giant_still_prompted(monkeypatch, tmp_path):
    """Audit nuance: a non-route-anchored oversized feature's sparse chunk
    (<3 exports, no routes) must still be prompted — the legacy whole-feature
    call gated on COMBINED exports, so per-chunk re-gating would silently
    lose flows. Only truly empty chunks are skipped."""
    from faultline.pipeline_v2.stage_3_flows import _passes_flow_gate

    # sanity of the legacy gate itself: 1 export + no route + no route-anchor
    # source fails the legacy gate — which is why the chunk path must NOT
    # re-apply it per chunk.
    class _F:
        sources = ("package",)
    assert _passes_flow_gate(_F(), ["one_export"], []) is False
    # the chunked path's rule: prompted iff chunk has ANY export or route —
    # covered end-to-end by existing chunked tests; here we pin the contract
    # that the helper is NOT consulted per chunk anymore (see stage source).
    import inspect
    from faultline.pipeline_v2 import stage_3_flows as m
    src = inspect.getsource(m)
    chunk_body = src.split("def _process_chunked", 1)[1].split("def ", 1)[0]
    assert "_passes_flow_gate" not in chunk_body
    assert "if not exports and not routes:" in chunk_body
