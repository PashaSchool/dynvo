"""Sanity checks on provider construction + URL helpers."""

from __future__ import annotations

import pytest

from faultlines_integrations.posthog import PostHogProvider, _extract_route, _dedupe_routes
from faultlines_integrations.sentry import (
    SentryProvider, _extract_route_from_issue, _normalize_route,
)
from faultlines_integrations.models import PageMetrics


@pytest.mark.asyncio
async def test_posthog_provider_constructs():
    p = PostHogProvider(api_key="k", project_id="123", host="https://app.posthog.com")
    assert p.name == "posthog"
    await p.close()


@pytest.mark.asyncio
async def test_sentry_provider_get_page_traffic_returns_empty():
    p = SentryProvider(auth_token="t", organization="o", project="p")
    assert p.name == "sentry"
    assert await p.get_page_traffic() == []
    await p.close()


def test_posthog_extract_route_strips_query_and_trailing_slash():
    assert _extract_route("https://app.example.com/login?ref=x") == "/login"
    assert _extract_route("https://app.example.com/dashboard/") == "/dashboard"
    assert _extract_route("") == ""


def test_posthog_dedupe_merges_routes():
    metrics = [
        PageMetrics(route="/login", pageviews=10),
        PageMetrics(route="/login", pageviews=15),
        PageMetrics(route="/billing", pageviews=5),
    ]
    merged = _dedupe_routes(metrics)
    by_route = {m.route: m for m in merged}
    assert by_route["/login"].pageviews == 25
    assert by_route["/billing"].pageviews == 5


def test_sentry_extract_route_from_culprit():
    issue = {"culprit": "GET /api/checkout"}
    assert _extract_route_from_issue(issue) == "/api/checkout"
    issue = {"culprit": "/dashboard/settings"}
    assert _extract_route_from_issue(issue) == "/dashboard/settings"


def test_sentry_normalize_route_strips_query():
    assert _normalize_route("/x?y=1") == "/x"
    assert _normalize_route("/x/") == "/x"
    assert _normalize_route("/") == "/"
