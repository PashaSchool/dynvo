"""Tests for Stage 8 — marketing-grounded Layer 2 clusterer (Sprint E1).

Unit-level. Network + Anthropic calls are stubbed via small fake
objects so the test suite stays hermetic.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from faultline.analyzer.marketing_fetcher import MarketingTaxonomy
from faultline.models.types import Feature
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    Stage8Result,
    _cache_key,
    _load_cached_taxonomy,
    _write_cache,
    cluster_via_haiku,
    fetch_marketing_taxonomy,
    run_stage_8,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _feat(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["alice"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="developer",
    )


def _product(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        display_name=name,
        paths=paths,
        authors=["alice"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="product",
    )


def _ctx(repo_path: Path) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="next-monorepo",
        monorepo=True,
        workspaces=None,
        tracked_files=[],
        commits=[],
    )


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


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the engine base dir to a per-test tmp dir so we don't
    poison ``~/.faultline/marketing-cache/`` from the test suite. The
    default ``FilesystemCacheBackend`` resolves its base from
    ``FAULTLINES_RUN_DIR`` → marketing cache lands under tmp_path."""
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(tmp_path))


# ── Test 1: customer YAML short-circuit ─────────────────────────────────


def test_customer_yaml_short_circuits_without_haiku(tmp_path: Path) -> None:
    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    pre_products = [_product("HTTP Uptime Monitoring", ["apps/web/lib/checker.ts"])]
    pre_map = {"checker": ("HTTP Uptime Monitoring",)}
    client = _FakeClient(response_text="should-not-be-called")

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={"rule:customer-yaml": 1},
        client=client,
    )

    assert result.telemetry["source"] == "customer-yaml"
    assert result.telemetry["haiku_called"] is False
    assert client.messages.calls == []  # Haiku NOT called


# ── Test 2: marketing fetched + Haiku called → mapping applied ──────────


def test_marketing_fetch_plus_haiku_overrides_pre_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [
        _feat("checker", ["apps/web/lib/checker.ts"]),
        _feat("regions", ["apps/web/lib/regions.ts"]),
        _feat("status-page", ["apps/status/page.tsx"]),
    ]
    pre_products = [_product("Billing", ["apps/web/lib/billing.ts"])]
    pre_map = {"checker": ("Billing",)}

    fake_taxonomy = MarketingTaxonomy(
        repo_slug="openstatus",
        source_url="https://openstatus.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=(
            "HTTP Uptime Monitoring",
            "Multi-Region Probing",
            "Status Page Builder",
        ),
        confidence=0.9,
        notes="test",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_taxonomy,
    )

    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
        {"developer": "regions", "product": "Multi-Region Probing"},
        {"developer": "status-page", "product": "Status Page Builder"},
    ]}))

    # Make tmp_path look like 'openstatus'
    repo_root = tmp_path / "openstatus"
    repo_root.mkdir()
    result = run_stage_8(
        _ctx(repo_root),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={"rule:dep-anchor": 1},
        client=client,
    )

    assert result.telemetry["source"] == "marketing+haiku"
    assert result.telemetry["haiku_called"] is True
    assert result.telemetry["taxonomy_size"] == 3
    assert len(client.messages.calls) == 1
    names = {pf.name for pf in result.product_features}
    assert "HTTP Uptime Monitoring" in names
    assert "Multi-Region Probing" in names
    assert "Status Page Builder" in names
    # Haiku mapping should override the deterministic "Billing" label
    # for "checker":
    assert result.dev_to_product_map["checker"] == ("HTTP Uptime Monitoring",)


# ── Test 3: marketing fetch failed → fallback to deterministic ──────────


def test_marketing_fetch_failed_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    pre_products = [_product("Billing", ["apps/web/lib/checker.ts"])]
    pre_map = {"checker": ("Billing",)}

    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )

    client = _FakeClient(response_text="not-called")

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={"rule:dep-anchor": 1},
        client=client,
    )

    assert result.telemetry["source"] == "deterministic-only"
    assert result.telemetry["fallback_reason"] == "fetch-failed-or-empty"
    assert result.product_features == pre_products
    assert result.dev_to_product_map == pre_map
    # Haiku was NEVER called because we never had a taxonomy.
    assert client.messages.calls == []


# ── Test 4: taxonomy too small → fallback ───────────────────────────────


def test_taxonomy_below_threshold_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    pre_products = [_product("Billing", ["apps/web/lib/checker.ts"])]
    pre_map = {"checker": ("Billing",)}

    # Only 2 entries — below _MIN_TAXONOMY_SIZE (=3).
    tiny = MarketingTaxonomy(
        repo_slug="x",
        source_url="https://x.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=("Foo Bar", "Baz Qux"),
        confidence=0.5,
        notes="",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: tiny,
    )

    client = _FakeClient(response_text="not-called")
    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=client,
    )

    assert result.telemetry["source"] == "deterministic-only"
    assert result.telemetry["fallback_reason"] == "taxonomy-too-small"
    assert client.messages.calls == []


# ── Test 5: no client → fallback ───────────────────────────────────────


def test_no_client_falls_back_to_deterministic(tmp_path: Path) -> None:
    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    pre_products = [_product("Billing", ["apps/web/lib/checker.ts"])]
    pre_map = {"checker": ("Billing",)}

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=None,
    )

    assert result.telemetry["source"] == "deterministic-only"
    assert result.telemetry["fallback_reason"] == "no-client"
    assert result.product_features == pre_products


# ── Test 6: Haiku invents taxonomy → rejected ───────────────────────────


def test_haiku_invented_labels_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [
        _feat("checker", ["apps/web/lib/checker.ts"]),
        _feat("regions", ["apps/web/lib/regions.ts"]),
        _feat("status-page", ["apps/status/page.tsx"]),
    ]
    fake_taxonomy = MarketingTaxonomy(
        repo_slug="openstatus",
        source_url="https://openstatus.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=(
            "HTTP Uptime Monitoring",
            "Multi-Region Probing",
            "Status Page Builder",
        ),
        confidence=0.9,
        notes="test",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_taxonomy,
    )

    # Haiku invents "Made Up Feature" (not in taxonomy).
    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
        {"developer": "regions", "product": "Made Up Feature"},
        {"developer": "status-page", "product": "Status Page Builder"},
    ]}))

    pre_map: dict[str, tuple[str, ...]] = {}
    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        [],
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=client,
    )

    assert result.telemetry["haiku_invented_rejected"] == 1
    # Only the two real-taxonomy entries should appear:
    names = {pf.name for pf in result.product_features}
    assert "Made Up Feature" not in names
    assert "HTTP Uptime Monitoring" in names
    assert "Status Page Builder" in names


# ── Test 7: cache hit suppresses fetch ──────────────────────────────────


def test_cache_hit_returns_cached_without_network(tmp_path: Path) -> None:
    slug = "cachedrepo"
    taxonomy = MarketingTaxonomy(
        repo_slug=slug,
        source_url="https://cachedrepo.io",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=("Foo Bar", "Baz Qux", "Some Label"),
        confidence=0.9,
        notes="seeded",
    )
    _write_cache(taxonomy)

    # Now any call to discover_marketing_site/fetch_page_text should
    # never run because the cache lookup returns first.
    # Patch them with a sentinel that explodes if called.
    with patch(
        "faultline.pipeline_v2.stage_8_marketing_clusterer.discover_marketing_site"
    ) as mock_discover, patch(
        "faultline.pipeline_v2.stage_8_marketing_clusterer.fetch_page_text"
    ) as mock_fetch:
        mock_discover.side_effect = AssertionError("should not be called")
        mock_fetch.side_effect = AssertionError("should not be called")

        got = fetch_marketing_taxonomy(tmp_path, slug)

    assert got is not None
    assert got.repo_slug == slug
    assert got.source_url == "https://cachedrepo.io"
    assert "Foo Bar" in got.product_features


# ── Test 8: cache miss when TTL expired ────────────────────────────────


def test_cache_miss_on_expired_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "staletest"
    cache_root = tmp_path / "marketing-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    # Hand-craft a cache entry with an old epoch.
    (cache_root / f"{slug}.json").write_text(json.dumps({
        "repo_slug": slug,
        "source_url": "https://x.dev",
        "fetched_at": "2020-01-01T00:00:00+00:00",
        "fetched_at_epoch": time.time() - 10 * 24 * 3600,  # 10 days old
        "product_features": ["Old", "Stuff"],
        "confidence": 0.9,
        "notes": "",
    }), encoding="utf-8")

    cached = _load_cached_taxonomy(slug)
    assert cached is None  # TTL expired


# ── Test 9: empty Haiku response → fallback ────────────────────────────


def test_empty_haiku_response_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    pre_products = [_product("Billing", ["apps/web/lib/checker.ts"])]
    pre_map = {"checker": ("Billing",)}

    fake_taxonomy = MarketingTaxonomy(
        repo_slug="x",
        source_url="https://x.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=("Foo Bar", "Baz Qux", "Quux Norf"),
        confidence=0.9,
        notes="",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_taxonomy,
    )

    # Haiku returns empty mapping.
    client = _FakeClient(response_text=json.dumps({"mappings": []}))

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=client,
    )

    # No usable mapping → fallback path.
    assert result.telemetry["source"] == "deterministic-only"
    assert result.product_features == pre_products


# ── Test 10: cluster_via_haiku records cost in tracker ─────────────────


def test_cluster_via_haiku_records_cost() -> None:
    from faultline.llm.cost import CostTracker

    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    taxonomy = MarketingTaxonomy(
        repo_slug="x",
        source_url="https://x.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=("HTTP Uptime Monitoring",),
        confidence=0.9,
        notes="",
    )
    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
    ]}))
    tracker = CostTracker()
    mapping, telemetry = cluster_via_haiku(
        feats, taxonomy,
        client=client, model="claude-haiku-4-5-20251001",
        cost_tracker=tracker,
    )
    assert mapping == {"checker": "HTTP Uptime Monitoring"}
    assert telemetry["called"] is True
    assert telemetry["tokens_in"] == 1000
    assert telemetry["tokens_out"] == 200
    assert tracker.total_cost_usd > 0
    assert tracker.call_count == 1


# ── Test 11: cache hit telemetry surfaces in run_stage_8 ───────────────


def test_run_stage_8_marks_cache_hit_in_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = tmp_path.name  # ctx.repo_path.name
    # Seed cache directly so the cached_before lookup hits.
    taxonomy = MarketingTaxonomy(
        repo_slug=slug,
        source_url="https://cached.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=("HTTP Uptime Monitoring", "Multi-Region Probing", "Status Pages"),
        confidence=0.9,
        notes="",
    )
    _write_cache(taxonomy)

    feats = [_feat("checker", ["apps/web/lib/checker.ts"])]
    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
    ]}))

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        [],
        dev_to_product_map_pre={},
        source_breakdown_pre={},
        client=client,
    )
    assert result.telemetry["cache_hit"] is True
    assert result.telemetry["source"] == "marketing+haiku"


# ── M2 — dedupe Stage-6.5 PFs whose devs are all Haiku-mapped ──────────


def test_m2_drops_singleton_pre_pfs_fully_covered_by_haiku(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stage-6.5 PF whose only dev member is now mapped by Haiku to
    a different marketing label must be dropped (it is a duplicate).

    Before M2 this PF was preserved (creating singleton workspace+domain
    duplicates of every Haiku-mapped feature — 84% of corpus
    over-emission)."""
    feats = [
        _feat("checker", ["apps/web/lib/checker.ts"]),
        _feat("regions", ["apps/web/lib/regions.ts"]),
    ]
    # Stage 6.5 placed each dev feature under a workspace+domain
    # singleton PF.
    pre_products = [
        _product("Checker", ["apps/web/lib/checker.ts"]),
        _product("Regions", ["apps/web/lib/regions.ts"]),
    ]
    pre_map: dict[str, tuple[str, ...]] = {
        "checker": ("Checker",),
        "regions": ("Regions",),
    }
    fake_taxonomy = MarketingTaxonomy(
        repo_slug="openstatus",
        source_url="https://openstatus.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=(
            "HTTP Uptime Monitoring",
            "Multi-Region Probing",
            "Status Page Builder",
        ),
        confidence=0.9,
        notes="",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_taxonomy,
    )
    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
        {"developer": "regions", "product": "Multi-Region Probing"},
    ]}))

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=client,
    )

    names = {pf.name for pf in result.product_features}
    # Haiku PFs present
    assert "HTTP Uptime Monitoring" in names
    assert "Multi-Region Probing" in names
    # Stage 6.5 duplicates dropped (every dev member is Haiku-mapped)
    assert "Checker" not in names
    assert "Regions" not in names


def test_m2_keeps_pre_pfs_when_haiku_leaves_dev_unmapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Haiku doesn't map a dev feature, its Stage-6.5 PF must be
    preserved to avoid recall regression."""
    feats = [
        _feat("checker", ["apps/web/lib/checker.ts"]),
        _feat("misc_util", ["apps/web/lib/misc.ts"]),
    ]
    pre_products = [
        _product("Checker", ["apps/web/lib/checker.ts"]),
        _product("Misc Util", ["apps/web/lib/misc.ts"]),
    ]
    pre_map: dict[str, tuple[str, ...]] = {
        "checker": ("Checker",),
        "misc_util": ("Misc Util",),
    }
    fake_taxonomy = MarketingTaxonomy(
        repo_slug="openstatus",
        source_url="https://openstatus.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=(
            "HTTP Uptime Monitoring",
            "Multi-Region Probing",
            "Status Page Builder",
        ),
        confidence=0.9,
        notes="",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_taxonomy,
    )
    # Haiku maps only `checker`, returns null for `misc_util`.
    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
        {"developer": "misc_util", "product": None},
    ]}))

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=client,
    )

    names = {pf.name for pf in result.product_features}
    assert "HTTP Uptime Monitoring" in names
    # Checker PF dropped (dev is Haiku-mapped)
    assert "Checker" not in names
    # Misc Util PF preserved (its dev is unmapped — recall protection)
    assert "Misc Util" in names


def test_m2_drops_pre_pf_with_partial_haiku_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a Stage-6.5 PF carries 2 dev members and both are
    Haiku-mapped (even to DIFFERENT marketing labels), the PF is
    redundant — drop it."""
    feats = [
        _feat("checker", ["apps/web/lib/checker.ts"]),
        _feat("regions", ["apps/web/lib/regions.ts"]),
    ]
    pre_products = [
        _product("Infra", ["apps/web/lib/checker.ts", "apps/web/lib/regions.ts"]),
    ]
    pre_map: dict[str, tuple[str, ...]] = {
        "checker": ("Infra",),
        "regions": ("Infra",),
    }
    fake_taxonomy = MarketingTaxonomy(
        repo_slug="openstatus",
        source_url="https://openstatus.dev",
        fetched_at="2026-05-20T00:00:00+00:00",
        product_features=(
            "HTTP Uptime Monitoring",
            "Multi-Region Probing",
            "Status Page Builder",
        ),
        confidence=0.9,
        notes="",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_taxonomy,
    )
    client = _FakeClient(response_text=json.dumps({"mappings": [
        {"developer": "checker", "product": "HTTP Uptime Monitoring"},
        {"developer": "regions", "product": "Multi-Region Probing"},
    ]}))

    result = run_stage_8(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={},
        client=client,
    )

    names = {pf.name for pf in result.product_features}
    assert "Infra" not in names  # both members rehoused → drop
    assert "HTTP Uptime Monitoring" in names
    assert "Multi-Region Probing" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
