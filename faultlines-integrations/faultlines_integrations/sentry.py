"""Sentry error monitoring provider."""

from __future__ import annotations

import httpx

from .models import ErrorEntry, ErrorMetrics, PageMetrics


class SentryProvider:
    """Fetches error metrics from the Sentry API.

    Sentry has no pageviews — `get_page_traffic` returns []. Pair with
    PostHog (or another traffic source) for full impact-score input.
    """

    name = "sentry"

    def __init__(
        self,
        auth_token: str,
        organization: str,
        project: str,
        host: str = "https://sentry.io",
    ) -> None:
        self._auth_token = auth_token
        self._organization = organization
        self._project = project
        self._host = host.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self._host}/api/0",
            headers={"Authorization": f"Bearer {self._auth_token}"},
            timeout=30.0,
        )

    async def validate_connection(self) -> bool:
        try:
            url = f"/projects/{self._organization}/{self._project}/"
            resp = await self._client.get(url)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_page_traffic(self, days: int = 30) -> list[PageMetrics]:
        return []

    async def get_error_counts(self, days: int = 30) -> list[ErrorMetrics]:
        try:
            resp = await self._client.get(
                f"/projects/{self._organization}/{self._project}/issues/",
                params={
                    "statsPeriod": f"{days}d",
                    "sort": "freq",
                    "limit": 200,
                    "query": "is:unresolved",
                },
            )
            resp.raise_for_status()
            issues = resp.json()
        except httpx.HTTPError:
            return []

        route_errors: dict[str, _RouteAccumulator] = {}
        for issue in issues:
            route = _extract_route_from_issue(issue)
            if not route:
                continue
            event_count = int(issue.get("count", "0"))
            title = issue.get("title", "Unknown error")
            issue_url = issue.get("permalink", "")
            acc = route_errors.setdefault(route, _RouteAccumulator())
            acc.total_errors += event_count
            acc.unique_errors += 1
            acc.entries.append(
                ErrorEntry(title=title, count=event_count, url=issue_url)
            )

        results: list[ErrorMetrics] = []
        for route, acc in route_errors.items():
            top = sorted(acc.entries, key=lambda e: e.count, reverse=True)[:10]
            results.append(
                ErrorMetrics(
                    route=route,
                    error_count=acc.total_errors,
                    unique_errors=acc.unique_errors,
                    top_errors=top,
                )
            )
        return sorted(results, key=lambda m: m.error_count, reverse=True)

    async def close(self) -> None:
        await self._client.aclose()


class _RouteAccumulator:
    def __init__(self) -> None:
        self.total_errors = 0
        self.unique_errors = 0
        self.entries: list[ErrorEntry] = []


def _extract_route_from_issue(issue: dict) -> str:
    culprit = issue.get("culprit", "")
    if culprit and "/" in culprit:
        parts = culprit.split(" ", 1)
        route = parts[-1] if len(parts) > 1 and parts[0].isupper() else culprit
        if route.startswith("/"):
            return _normalize_route(route)
    title = issue.get("title", "")
    if " /" in title:
        for part in title.split():
            if part.startswith("/") and len(part) > 1:
                return _normalize_route(part)
    return ""


def _normalize_route(route: str) -> str:
    if "?" in route:
        route = route.split("?")[0]
    if route != "/" and route.endswith("/"):
        route = route.rstrip("/")
    return route
