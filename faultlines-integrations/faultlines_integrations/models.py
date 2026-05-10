"""Data models + impact-score math for analytics enrichment.

Moved verbatim from `faultline/integrations/base.py` so this package
has no runtime dependency on the faultline CLI codebase.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class PageMetrics(BaseModel):
    """Aggregated traffic metrics for a single page/route."""

    route: str
    pageviews: int = 0
    unique_visitors: int = 0
    avg_session_duration_sec: float = 0.0
    bounce_rate: float | None = None


class ErrorEntry(BaseModel):
    """Single error type with count and optional link."""

    title: str
    count: int
    url: str = ""


class ErrorMetrics(BaseModel):
    """Aggregated error metrics for a single route or component."""

    route: str
    error_count: int = 0
    unique_errors: int = 0
    top_errors: list[ErrorEntry] = []


class ImpactScore(BaseModel):
    """Computed impact combining health score with analytics data."""

    flow_name: str
    health_score: float
    pageviews: int
    error_count: int
    impact_level: str  # critical | high | medium | low | healthy
    score: float  # 0-100, lower = more urgent


@runtime_checkable
class AnalyticsProvider(Protocol):
    """Protocol that all analytics providers must implement."""

    @property
    def name(self) -> str: ...

    async def validate_connection(self) -> bool: ...

    async def get_page_traffic(self, days: int = 30) -> list[PageMetrics]: ...

    async def get_error_counts(self, days: int = 30) -> list[ErrorMetrics]: ...


# ── Scoring ──────────────────────────────────────────────────────────


def compute_impact_scores(
    flows: list[dict],
    traffic: list[PageMetrics],
    errors: list[ErrorMetrics],
) -> list[ImpactScore]:
    """Combine health scores with analytics to produce impact scores.

    Args:
        flows: list of dicts with 'name', 'health_score', 'paths' keys
        traffic: page traffic from analytics provider
        errors: error counts from analytics provider

    Returns sorted by urgency (most critical first).
    """
    traffic_by_route = {pm.route: pm for pm in traffic}
    errors_by_route = {em.route: em for em in errors}

    scores: list[ImpactScore] = []
    for flow in flows:
        flow_name = flow["name"]
        health = flow["health_score"]
        flow_paths = flow.get("paths", [])

        total_views = 0
        total_errors = 0
        for path in flow_paths:
            route = path_to_route(path)
            if route in traffic_by_route:
                total_views += traffic_by_route[route].pageviews
            if route in errors_by_route:
                total_errors += errors_by_route[route].error_count

        score = _calculate_score(health, total_views, total_errors)
        scores.append(
            ImpactScore(
                flow_name=flow_name,
                health_score=health,
                pageviews=total_views,
                error_count=total_errors,
                impact_level=_score_to_level(score),
                score=score,
            )
        )

    return sorted(scores, key=lambda s: s.score)


def path_to_route(file_path: str) -> str:
    """Convert a file path to a URL route for matching analytics rows."""
    route = file_path
    for prefix in ("src/app/", "src/pages/", "app/", "pages/", "src/routes/"):
        if route.startswith(prefix):
            route = route[len(prefix):]
            break
    for suffix in (
        "/page.tsx", "/page.ts", "/page.jsx", "/page.js",
        "/+page.svelte", "/+page.ts",
        "/index.tsx", "/index.ts", "/index.jsx", "/index.js",
        ".tsx", ".ts", ".jsx", ".js", ".svelte",
    ):
        if route.endswith(suffix):
            route = route[: -len(suffix)]
            break
    if not route.startswith("/"):
        route = "/" + route
    if route != "/" and route.endswith("/"):
        route = route.rstrip("/")
    return route


def _calculate_score(health: float, pageviews: int, errors: int) -> float:
    """Lower score = more urgent. health 40% / traffic 35% / errors 25%."""
    traffic_norm = min(math.log10(max(pageviews, 1)) / 5.0, 1.0) * 100
    error_norm = min(math.log10(max(errors, 1)) / 4.0, 1.0) * 100
    return health * 0.40 + (100 - traffic_norm) * 0.35 + (100 - error_norm) * 0.25


def _score_to_level(score: float) -> str:
    if score < 25:
        return "critical"
    if score < 45:
        return "high"
    if score < 65:
        return "medium"
    if score < 80:
        return "low"
    return "healthy"
