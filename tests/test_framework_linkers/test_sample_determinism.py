"""Stage 6.4 linker telemetry — debug samples must not leak ThreadPool
completion order into scan output (perf-wave determinism fix, 2026-07-07).

The audit's papermark canon-vs-nocap diff caught a set-equal reordering
of ``per_linker[...].unmatched_sample`` inside scan_meta: sample lists
were appended from Stage 6.4 worker threads with an append-time cap, so
BOTH the membership (first N appends won) and the order depended on
thread scheduling. These tests fail on that behaviour: for every linker
telemetry class, two different insertion orders (with more items than
the emission cap) must emit byte-identical ``as_dict()`` payloads.
"""

from __future__ import annotations

import json

from faultline.framework_linkers.base import canonical_sample
from faultline.framework_linkers.nextjs_http_route import (
    _LinkerTelemetry as HttpTelemetry,
)
from faultline.framework_linkers.nextjs_server_actions import (
    _LinkerTelemetry as ActionsTelemetry,
)
from faultline.framework_linkers.store_mutation import (
    _LinkerTelemetry as StoreTelemetry,
)
from faultline.framework_linkers.trpc_procedure import (
    _LinkerTelemetry as TrpcTelemetry,
)


def _dict_samples(n: int) -> list[dict[str, str]]:
    # Distinct, deliberately unsorted-by-construction items.
    return [
        {"url": f"/api/z{99 - i}", "file": f"components/c{99 - i}.tsx"}
        for i in range(n)
    ]


def _canon(d: object) -> str:
    return json.dumps(d, sort_keys=True)


# ── canonical_sample helper ─────────────────────────────────────────────


def test_canonical_sample_is_order_insensitive_and_capped() -> None:
    items = _dict_samples(25)
    fwd = canonical_sample(list(items), 10)
    rev = canonical_sample(list(reversed(items)), 10)
    assert fwd == rev
    assert len(fwd) == 10
    # Deterministic membership: lexicographically-first 10 under the
    # canonical JSON key, NOT the first 10 appended.
    keys = [_canon(i) for i in fwd]
    assert keys == sorted(_canon(i) for i in items)[:10]


def test_canonical_sample_keeps_duplicates() -> None:
    items = [{"a": 1}, {"a": 1}, {"a": 0}]
    assert canonical_sample(items, 3) == [{"a": 0}, {"a": 1}, {"a": 1}]


# ── per-linker telemetry: thread-order independence ─────────────────────


def test_http_route_unmatched_sample_order_independent() -> None:
    items = _dict_samples(30)
    t1, t2 = HttpTelemetry(), HttpTelemetry()
    t1.unmatched_sample.extend(items)
    t2.unmatched_sample.extend(reversed(items))
    assert t1.as_dict() == t2.as_dict()
    assert len(t1.as_dict()["unmatched_sample"]) == 10


def test_server_actions_sample_links_order_independent() -> None:
    items = [
        {"source": f"s{i}.tsx:1", "target": f"m{i}.ts:act", "kind": "imported-action"}
        for i in range(20, 0, -1)
    ]
    t1, t2 = ActionsTelemetry(), ActionsTelemetry()
    t1.sample_links.extend(items)
    t2.sample_links.extend(reversed(items))
    assert t1.as_dict() == t2.as_dict()
    assert len(t1.as_dict()["sample_links"]) == 5


def test_trpc_samples_order_independent() -> None:
    t1, t2 = TrpcTelemetry(), TrpcTelemetry()
    paths = [f"router.proc{i}" for i in range(30, 0, -1)]
    links = [
        {"source": f"f{i}.ts:2", "target": f"r.ts:p{i}", "verb": "query"}
        for i in range(12)
    ]
    t1.unmatched_paths_sample.extend(paths)
    t1.sample_links.extend(links)
    t2.unmatched_paths_sample.extend(reversed(paths))
    t2.sample_links.extend(reversed(links))
    d1, d2 = t1.as_dict(), t2.as_dict()
    assert d1 == d2
    assert len(d1["unmatched_paths_sample"]) == 10
    assert len(d1["sample_links"]) == 5


def test_store_mutation_sample_links_order_independent() -> None:
    items = [
        {"source": f"s{i}.ts:9", "target": f"store.ts:set{i}:L1", "kind": "store-write"}
        for i in range(15)
    ]
    t1, t2 = StoreTelemetry(), StoreTelemetry()
    t1.sample_links.extend(items)
    t2.sample_links.extend(reversed(items))
    assert t1.as_dict() == t2.as_dict()
    assert len(t1.as_dict()["sample_links"]) == 10


# ── append sites must be uncapped (membership decided at emission) ──────


def test_http_route_append_site_no_longer_caps(tmp_path) -> None:
    """The linker appends EVERY unmatched url; the cap is applied only at
    ``as_dict`` time under a canonical sort. If an append-time cap comes
    back, membership becomes arrival-order-dependent again — this test
    exercises >10 unmatched urls through the real scan path and asserts
    the raw list kept all of them."""
    from unittest.mock import MagicMock

    from faultline.framework_linkers.nextjs_http_route import (
        NextjsHttpRouteLinker,
        _CompiledRoute,
        _url_pattern_to_regex,
    )

    linker = NextjsHttpRouteLinker()
    routes = [
        _CompiledRoute(
            regex=_url_pattern_to_regex("/api/known"),
            file="app/api/known/route.ts",
            raw_pattern="/api/known",
        ),
    ]
    text = "\n".join(
        f'fetch("/api/unknown{i:02d}")' for i in range(15)
    )
    ctx = MagicMock()
    ctx.repo_path = tmp_path
    log = MagicMock()
    linker._scan_file_for_links(
        "components/caller.tsx", text, routes, ctx, log,
        feature_name="f",
    )
    assert len(linker.telemetry.unmatched_sample) == 15
    assert len(linker.telemetry.as_dict()["unmatched_sample"]) == 10
