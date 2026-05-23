"""T2 — cross-stack HTTP boundary resolution (deterministic, no LLM).

Scans every node in a Stage 3.5 call graph for HTTP-client call sites
(``fetch``, ``axios.get``, ``$fetch``, ``useSWR``, ``ky.get``,
``requests.get``, ``http.Get``…) and matches the URL literal against
Sprint 1's ``routes_index`` so we can emit a single
``cross_stack_http`` edge from the client node to the server-side
route handler.

Reuses the ``routes_index`` produced by
:mod:`faultline.pipeline_v2.indexes`. Per [[bipartite-flow-feature-store]]
this is the canonical Sprint 1 contract.

What we DO match
================
  * String literals: ``fetch("/api/products")``,
    ``fetch('/api/users/' + id)`` → matches literal prefix
    ``/api/users/``.
  * Template literals: ``fetch(`${API}/api/orders`)`` → matches the
    literal-only tail ``/api/orders``.
  * Dynamic path segments: ``/api/products/[id]`` (Next.js) is
    treated as a pattern; we match by prefix when the route_index
    pattern contains ``[`` / ``{`` / ``:``.

What we DON'T match (T3 territory — deferred)
=============================================
  * Fully-config-driven URLs (``fetch(API_ROUTES.products)``)
  * Dynamic-dispatch through router objects
  * GraphQL / tRPC procedure names (handled by Stage 6.4 today)
  * WebSocket / SSE channel names
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from faultline.pipeline_v2.flow_expansion.confidence import (
    confidence_for_cross_stack,
)

logger = logging.getLogger(__name__)


# ── HTTP client call patterns (universal across stacks) ─────────────────

# Each pattern returns the URL string in group(1). We match within an
# arbitrary source slice so adapter files don't need to pre-parse
# anything — the call_graph already gave us per-symbol line ranges.

_HTTP_CLIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # JS/TS:   fetch("/path") / fetch('/path') / fetch(`/path`)
    re.compile(r"""\bfetch\s*\(\s*['"`]([^'"`]+)['"`]"""),
    # axios.get / axios.post / axios.request (and other verbs)
    re.compile(
        r"""\baxios\s*\.\s*(?:get|post|put|patch|delete|head|options|request)"""
        r"""\s*\(\s*['"`]([^'"`]+)['"`]"""
    ),
    # Nuxt/Vue:  $fetch("/path")  /  useFetch("/path")  /  useAsyncData(..., () => $fetch(...))
    re.compile(r"""\$fetch\s*\(\s*['"`]([^'"`]+)['"`]"""),
    re.compile(r"""\buseFetch\s*\(\s*['"`]([^'"`]+)['"`]"""),
    # ky.get('/path')
    re.compile(
        r"""\bky\s*\.\s*(?:get|post|put|patch|delete|head)"""
        r"""\s*\(\s*['"`]([^'"`]+)['"`]"""
    ),
    # SWR / TanStack: useSWR('/path', …) / useQuery({ queryKey: ['x'], queryFn: () => fetch('/y') })
    re.compile(r"""\buseSWR\s*\(\s*['"`]([^'"`]+)['"`]"""),
    # Python: requests.get("/path") / httpx.get / aiohttp session.get
    re.compile(
        r"""\b(?:requests|httpx|session)\s*\.\s*(?:get|post|put|patch|delete|head)"""
        r"""\s*\(\s*['"]([^'"]+)['"]"""
    ),
    # Go: http.Get("/path") / http.Post / client.Do(req for "/path")
    re.compile(
        r"""\bhttp\s*\.\s*(?:Get|Post|Put|Patch|Delete|Head)"""
        r"""\s*\(\s*['"`]([^'"`]+)['"`]"""
    ),
)


# Template literal: pull literal text outside of ${...} blocks.
_RE_TEMPLATE_INTERP = re.compile(r"\$\{[^}]*\}")


def _looks_like_template(url: str) -> bool:
    """True when the URL contains a template interpolation marker.

    We never store the actual interpolation; only its presence affects
    confidence (medium vs high).
    """
    return "${" in url


def _strip_template_interp(url: str) -> str:
    """Replace ``${...}`` with a placeholder ``*`` for prefix matching.

    This lets us still attempt a literal-tail match when the URL is
    e.g. ``${API}/api/products`` → we want ``/api/products`` to match.
    """
    return _RE_TEMPLATE_INTERP.sub("*", url)


# ── Routes-index matching ───────────────────────────────────────────────


def _normalise_pattern(pattern: str) -> str:
    """Normalise a routes_index pattern for comparison.

    Strips trailing slashes (except root) and collapses Next.js
    ``[...slug]`` / FastAPI ``{slug}`` / Rails ``:slug`` to a single
    wildcard token ``*`` so prefix matching is uniform.
    """
    p = pattern.rstrip("/") or "/"
    # Collapse dynamic segments to "*".
    p = re.sub(r"\[\.\.\.[^\]]+\]", "*", p)         # Next.js catch-all
    p = re.sub(r"\[[^\]]+\]", "*", p)               # Next.js dynamic
    p = re.sub(r"\{[^}]+\}", "*", p)                # FastAPI / Spring
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "*", p)  # Rails / Express
    return p


def _url_matches_pattern(url: str, pattern: str) -> bool:
    """Does ``url`` (client literal) match ``pattern`` (route entry)?

    Algorithm:
      1. Exact match (after normalisation) → True.
      2. Url starts with pattern's static prefix (everything before
         the first ``*``) → True. This handles
         ``fetch("/api/users/" + id)`` whose literal prefix is
         ``/api/users/``.
    """
    norm_url = url.rstrip("/") or "/"
    norm_pat = _normalise_pattern(pattern)
    if norm_url == norm_pat:
        return True
    # Replace "*" with a regex-safe placeholder for prefix split.
    if "*" in norm_pat:
        static_prefix = norm_pat.split("*", 1)[0]
        if static_prefix and norm_url.startswith(static_prefix.rstrip("/")):
            return True
    # Wildcarded literal (from template interp).
    if "*" in norm_url:
        parts = [p for p in norm_url.split("*") if p]
        # Each literal chunk between wildcards must appear in pattern
        # in order. Strict prefix not required (wildcard could be at
        # head — `${BASE}/api/x` → "*/api/x" → just needs "/api/x"
        # somewhere in the pattern).
        if not parts:
            return False
        cursor = 0
        for chunk in parts:
            needle = chunk.rstrip("/")
            if not needle:
                continue
            idx = norm_pat.find(needle, cursor)
            if idx < 0:
                return False
            cursor = idx + len(needle)
        return True
    return False


# ── Public dataclasses ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CrossStackHit:
    """One matched cross-stack edge.

    ``client_file`` + ``client_symbol`` come from the call_graph node
    we scanned. ``url`` is the literal we matched. ``route`` is the
    routes_index entry (carries ``feature_uuid`` + handler ``file``).
    """

    client_file: str
    client_symbol: str | None
    url: str
    route_pattern: str
    route_method: str
    route_file: str
    route_feature_uuid: str
    is_template: bool


def find_cross_stack_hits(
    *,
    client_file: str,
    client_symbol: str | None,
    source_slice: str,
    routes_index: list[dict[str, Any]],
) -> list[CrossStackHit]:
    """Scan ``source_slice`` for HTTP client calls and match them.

    Returns one :class:`CrossStackHit` per (url, route) pair. A single
    fetch literal may match multiple routes when the verb is ambiguous
    (no method filter in the literal) — we emit one hit per match so
    the caller can decide whether to dedup.
    """
    if not source_slice or not routes_index:
        return []

    out: list[CrossStackHit] = []
    seen: set[tuple[str, str, str]] = set()

    for pat in _HTTP_CLIENT_PATTERNS:
        for m in pat.finditer(source_slice):
            raw_url = m.group(1)
            if not raw_url:
                continue
            is_template = _looks_like_template(raw_url)
            search_url = (
                _strip_template_interp(raw_url) if is_template else raw_url
            )
            # Only match paths — skip anything that looks fully absolute
            # to an external host (http://, https://) unless the routes
            # index contains that host (rare).
            if search_url.startswith(("http://", "https://")):
                # Drop scheme + host portion if it points back to a
                # routes_index entry — best-effort.
                tail = re.sub(r"^https?://[^/]+", "", search_url) or "/"
                search_url = tail
            for entry in routes_index:
                pattern = str(entry.get("pattern") or "")
                if not pattern:
                    continue
                if not _url_matches_pattern(search_url, pattern):
                    continue
                key = (client_file, search_url, pattern)
                if key in seen:
                    continue
                seen.add(key)
                out.append(CrossStackHit(
                    client_file=client_file,
                    client_symbol=client_symbol,
                    url=search_url,
                    route_pattern=pattern,
                    route_method=str(entry.get("method") or "GET"),
                    route_file=str(entry.get("file") or ""),
                    route_feature_uuid=str(entry.get("feature_uuid") or ""),
                    is_template=is_template,
                ))

    return out


def confidence_for_hit(hit: CrossStackHit) -> str:
    """Pass-through to the central confidence module."""
    return confidence_for_cross_stack(
        literal_match=True,
        template_interpolation=hit.is_template,
    )


__all__ = [
    "CrossStackHit",
    "find_cross_stack_hits",
    "confidence_for_hit",
]
