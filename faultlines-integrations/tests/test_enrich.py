"""Smoke tests for the standalone enrichment pipeline.

We use the synchronous `enrich_feature_map_from_metrics` helper so
no asyncio + httpx mocking is needed — the provider classes have
their own thin tests.
"""

from __future__ import annotations

import pytest

from faultlines_integrations.enrich import enrich_feature_map_from_metrics
from faultlines_integrations.models import (
    ErrorMetrics, ImpactScore, PageMetrics,
    compute_impact_scores, path_to_route,
)


@pytest.fixture
def sample_feature_map() -> dict:
    return {
        "schema_version": 1,
        "features": [
            {
                "name": "auth",
                "health_score": 70.0,
                "paths": ["src/app/login/page.tsx"],
                "flows": [
                    {
                        "name": "login-flow",
                        "health_score": 65.0,
                        "paths": ["src/app/login/page.tsx"],
                    },
                    {
                        "name": "signup-flow",
                        "health_score": 90.0,
                        "paths": ["src/app/signup/page.tsx"],
                    },
                ],
            },
            {
                "name": "billing",
                "health_score": 40.0,
                "paths": ["src/app/billing/page.tsx"],
                # no flows — falls back to feature-as-flow
            },
        ],
    }


def test_path_to_route_strips_framework_conventions():
    assert path_to_route("src/app/login/page.tsx") == "/login"
    assert path_to_route("pages/api/webhooks/github.ts") == "/api/webhooks/github"
    assert path_to_route("src/routes/checkout/+page.svelte") == "/checkout"


def test_compute_impact_orders_most_urgent_first():
    flows = [
        {"name": "low-traffic-healthy", "health_score": 90.0, "paths": ["src/app/about/page.tsx"]},
        {"name": "high-traffic-broken", "health_score": 30.0, "paths": ["src/app/checkout/page.tsx"]},
    ]
    traffic = [PageMetrics(route="/checkout", pageviews=50_000)]
    errors = [ErrorMetrics(route="/checkout", error_count=500)]

    scores = compute_impact_scores(flows, traffic, errors)
    assert scores[0].flow_name == "high-traffic-broken"
    assert scores[0].impact_level in {"critical", "high"}
    assert scores[-1].flow_name == "low-traffic-healthy"


def test_enrich_feature_map_attaches_scores_and_meta(sample_feature_map):
    traffic = [
        PageMetrics(route="/login", pageviews=10_000),
        PageMetrics(route="/billing", pageviews=2_000),
    ]
    errors = [ErrorMetrics(route="/login", error_count=200)]

    enriched = enrich_feature_map_from_metrics(
        sample_feature_map, traffic=traffic, errors=errors,
    )

    assert "impact_scores" in enriched
    assert "analytics_meta" in enriched
    assert enriched["analytics_meta"]["scored_flows"] == 3  # 2 flows + 1 flowless feature

    flow_names = {s["flow_name"] for s in enriched["impact_scores"]}
    assert flow_names == {"login-flow", "signup-flow", "billing"}

    # Original feature map stays intact.
    assert enriched["features"] == sample_feature_map["features"]


def test_enrich_with_no_metrics_still_emits_scores(sample_feature_map):
    enriched = enrich_feature_map_from_metrics(sample_feature_map)
    assert len(enriched["impact_scores"]) == 3
    # No traffic + no errors → score driven by health only.
    for s in enriched["impact_scores"]:
        assert s["pageviews"] == 0
        assert s["error_count"] == 0


def test_impact_score_model_validates():
    s = ImpactScore(
        flow_name="x", health_score=50.0, pageviews=100,
        error_count=10, impact_level="medium", score=55.0,
    )
    assert s.flow_name == "x"
