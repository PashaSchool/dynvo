"""
Mock PostHog + Sentry server for local testing.

Generates realistic fake analytics data based on routes you provide.
No Docker, no signup — just run and test.

Usage:
    python scripts/mock_analytics_server.py

    # Then use in faultline:
    faultline analyze . --llm --flows \
        --posthog-key test --posthog-project 1 --posthog-host http://localhost:9876 \
        --sentry-token test --sentry-org test --sentry-project test --sentry-host http://localhost:9876

Endpoints:
    GET  /                                          → 200 (health check)
    GET  /api/projects/:id/                         → PostHog project info
    POST /api/projects/:id/query/                   → PostHog HogQL query (pageviews, errors)
    GET  /api/0/projects/:org/:project/             → Sentry project info
    GET  /api/0/projects/:org/:project/issues/      → Sentry issues list
"""

import json
import random
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = 9876

# Realistic routes — these will appear in analytics data.
# The mock server generates random traffic/error data for each.
ROUTES = [
    "/",
    "/login",
    "/signup",
    "/dashboard",
    "/dashboard/settings",
    "/dashboard/analytics",
    "/profile",
    "/profile/edit",
    "/checkout",
    "/checkout/payment",
    "/checkout/confirmation",
    "/products",
    "/products/search",
    "/admin",
    "/admin/users",
    "/admin/settings",
    "/api/auth/login",
    "/api/auth/register",
    "/api/users",
    "/api/products",
    "/api/orders",
    "/api/webhooks/stripe",
    "/notifications",
    "/settings",
    "/settings/billing",
    "/settings/team",
]

# Error messages pool
ERRORS = [
    "TypeError: Cannot read properties of undefined (reading 'id')",
    "ReferenceError: user is not defined",
    "NetworkError: Failed to fetch",
    "TimeoutError: Request timed out after 30000ms",
    "SyntaxError: Unexpected token in JSON at position 0",
    "Error: NEXT_NOT_FOUND",
    "Error: 429 Too Many Requests",
    "TypeError: Cannot destructure property 'data' of undefined",
    "Error: Payment intent creation failed",
    "Error: Database connection pool exhausted",
    "Error: JWT token expired",
    "Error: CORS policy blocked request",
    "ChunkLoadError: Loading chunk 42 failed",
    "Error: Hydration mismatch",
]


def _seed_for_route(route: str) -> int:
    """Deterministic seed so data is stable across requests."""
    return int(hashlib.md5(route.encode()).hexdigest()[:8], 16)


def _generate_posthog_pageviews() -> list:
    """Generate PostHog HogQL query results for $pageview events."""
    results = []
    for route in ROUTES:
        if route.startswith("/api/"):
            continue  # APIs don't have pageviews
        rng = random.Random(_seed_for_route(route))
        views = rng.randint(50, 50000)
        visitors = int(views * rng.uniform(0.3, 0.8))
        avg_dur = round(rng.uniform(5, 180), 1)
        results.append([
            f"https://app.example.com{route}",
            views,
            visitors,
            avg_dur,
        ])
    results.sort(key=lambda r: r[1], reverse=True)
    return results


def _generate_posthog_errors() -> list:
    """Generate PostHog HogQL query results for $exception events."""
    results = []
    for route in ROUTES:
        rng = random.Random(_seed_for_route(route) + 1)
        if rng.random() < 0.3:
            continue  # 30% of routes have no errors
        errors = rng.randint(1, 500)
        unique = min(errors, rng.randint(1, 8))
        msgs = rng.sample(ERRORS, min(unique, len(ERRORS)))
        results.append([
            f"https://app.example.com{route}",
            errors,
            unique,
            msgs,
        ])
    results.sort(key=lambda r: r[1], reverse=True)
    return results


def _generate_sentry_issues() -> list:
    """Generate Sentry-style issues list."""
    issues = []
    for i, route in enumerate(ROUTES):
        rng = random.Random(_seed_for_route(route) + 2)
        if rng.random() < 0.25:
            continue
        error_count = rng.randint(1, 1000)
        error_msg = rng.choice(ERRORS)
        issues.append({
            "id": str(1000 + i),
            "title": error_msg,
            "culprit": f"GET {route}" if not route.startswith("/api/") else route,
            "count": str(error_count),
            "permalink": f"https://sentry.io/issues/{1000 + i}/",
            "metadata": {"type": "Error", "value": error_msg},
        })
    issues.sort(key=lambda x: int(x["count"]), reverse=True)
    return issues


class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Health check / PostHog project info
        if path == "/" or path.startswith("/api/projects/") and path.endswith("/") and "issues" not in path and "query" not in path:
            self._json_response({"id": 1, "name": "Mock Project", "status": "ok"})
            return

        # Sentry project info
        if "/api/0/projects/" in path and path.endswith("/") and "issues" not in path:
            self._json_response({"id": "1", "name": "mock-project", "slug": "mock-project"})
            return

        # Sentry issues
        if "/api/0/projects/" in path and "issues" in path:
            issues = _generate_sentry_issues()
            self._json_response(issues)
            return

        self._json_response({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # PostHog query endpoint
        if "/query" in path:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode() if content_len else "{}"

            # Determine if this is a pageview or error query
            if "$exception" in body:
                results = _generate_posthog_errors()
            else:
                results = _generate_posthog_pageviews()

            self._json_response({
                "results": results,
                "columns": ["url", "count", "unique", "extra"],
            })
            return

        self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Cleaner log output."""
        print(f"  {args[0]}")


def main():
    server = HTTPServer(("localhost", PORT), MockHandler)
    print(f"""
╔══════════════════════════════════════════════════════╗
║  Mock Analytics Server                               ║
║  PostHog + Sentry compatible                         ║
║                                                      ║
║  Running on http://localhost:{PORT}                    ║
║                                                      ║
║  PostHog endpoints:                                  ║
║    GET  /api/projects/1/          (project info)     ║
║    POST /api/projects/1/query/    (HogQL queries)    ║
║                                                      ║
║  Sentry endpoints:                                   ║
║    GET  /api/0/projects/org/proj/ (project info)     ║
║    GET  /api/0/projects/org/proj/issues/ (issues)    ║
║                                                      ║
║  {len(ROUTES)} routes with fake traffic + error data          ║
║  Press Ctrl+C to stop                                ║
╚══════════════════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
