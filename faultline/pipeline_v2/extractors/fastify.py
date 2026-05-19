"""FastifyRouteExtractor — code-based extractor for Fastify HTTP routes.

Fastify (and NestJS-when-using-Fastify-adapter) does not encode routes
in the filesystem — routes live in code as method calls:

    fastify.get("/users", handler)
    fastify.post("/users/:id/secrets", handler)
    fastify.route({ method: "GET", url: "/projects/:id" })
    fastify.register(secretsRoutes, { prefix: "/api/v1/secrets" })
    app.register(plugin, { prefix: "/auth" })

We grep over JS/TS source files (regex, not AST — we can't assume a
TypeScript toolchain on the user's machine, mirroring the same choice
made for ``go_router.py``). Each matched URL produces a route signal;
the first non-noise, non-dynamic path segment becomes the anchor slug.

Activation gate
===============

This extractor self-skips unless ONE of these holds:

  - ``ctx.audited_stack == "fastify"``
  - ``"fastify" in ctx.secondary_stacks``
  - ``ctx.audited_stack == "nestjs"`` (Nest can sit on Fastify)
  - any workspace's ``package.json`` has ``fastify`` as a dependency

The dep-check covers polyglot monorepos where Stage 0 / the auditor
tag the repo as something else (e.g. ``monorepo-polyglot``) but a
backend workspace still uses Fastify.

No LLM. No network. Pure regex over text.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import is_noise, posix, slugify
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── Patterns ────────────────────────────────────────────────────────────────

# Source-file globs we will scan. Mirrors the auditor / extractor skip
# list so we don't grep through dist or test scaffolding.
_SOURCE_SUFFIXES: tuple[str, ...] = (
    ".ts", ".js", ".mts", ".mjs", ".cts", ".cjs",
)

# Excluded path prefixes/substrings — defensive against running on
# compiled / vendored / test scaffolding output that would inflate hits.
_EXCLUDED_SUBSTRINGS: tuple[str, ...] = (
    "/node_modules/",
    "/dist/",
    "/build/",
    "/out/",
    "/.next/",
    "/__tests__/",
    "/test/",
    "/tests/",
    "/__mocks__/",
    "/coverage/",
    "/vendor/",
)

_EXCLUDED_FILENAME_SUFFIXES: tuple[str, ...] = (
    ".test.ts", ".test.js", ".test.mts", ".test.mjs",
    ".spec.ts", ".spec.js", ".spec.mts", ".spec.mjs",
    ".d.ts",
)

# fastify.get("/path", ...), app.post("/x", ...), server.delete("/y").
# The receiver name is constrained to common Fastify/Nest server idents
# so we don't grab arbitrary `foo.get(...)` calls on unrelated objects.
_METHOD_CALL_RE = re.compile(
    r"\b(?:fastify|app|server|router|instance)\."
    r"(?:get|post|put|patch|delete|head|options|all)\s*\(\s*"
    r"""(["'`])([^"'`]+)\1""",
)

# fastify.route({ method: "GET", url: "/foo" }) — properties may be in
# any order and may use single, double, or backtick quotes. Two flavours
# because regex can't capture the URL regardless of property order: we
# alternately look for ``url: "..."`` after a ``method: "..."`` and the
# reverse.
_ROUTE_OBJ_URL_RE = re.compile(
    r"\b(?:fastify|app|server|router|instance)\.route\s*\(\s*\{[^}]*?"
    r"""\burl\s*:\s*(["'`])([^"'`]+)\1""",
    re.DOTALL,
)

# fastify.register(plugin, { prefix: "/api/v1/users" }) — the plugin
# function/import name is the anchor candidate too, but the prefix is
# the most semantically meaningful slug source.
_REGISTER_PREFIX_RE = re.compile(
    r"\b(?:fastify|app|server|router|instance)\.register\s*\(\s*"
    r"[A-Za-z_$][\w.$]*\s*,\s*\{[^}]*?"
    r"""\bprefix\s*:\s*(["'`])([^"'`]+)\1""",
    re.DOTALL,
)


# ── Activation ──────────────────────────────────────────────────────────────


def _has_fastify_dep(pkg: dict | None) -> bool:
    """True if any of the dep-blocks of ``pkg`` list ``fastify``."""
    if not isinstance(pkg, dict):
        return False
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = pkg.get(key)
        if isinstance(block, dict) and "fastify" in block:
            return True
    return False


def _is_active(ctx: "ScanContext") -> bool:
    """Self-skip gate — see module docstring."""
    audited = (ctx.audited_stack or "").lower()
    if audited in {"fastify", "nestjs"}:
        return True
    secondary = {s.lower() for s in (ctx.secondary_stacks or ())}
    if "fastify" in secondary or "nestjs" in secondary:
        return True
    # Stage 0 may have tagged the root or a workspace ``"fastify"``.
    if (ctx.stack or "").lower() == "fastify":
        return True
    for ws in (ctx.workspaces or []):
        if (ws.stack or "").lower() == "fastify":
            return True
        if _has_fastify_dep(ws.package_json):
            return True
    return False


# ── Slug derivation ─────────────────────────────────────────────────────────


def _first_meaningful_segment(url: str) -> str | None:
    """Pick the first URL segment that is not noise / not a dynamic param.

    Fastify uses ``/users/:id`` (colon prefix) AND ``/users/*`` for
    catch-alls. Both are skipped — they describe params, not features.
    """
    if not url:
        return None
    # Drop leading slash, drop query string / fragments defensively.
    cleaned = url.lstrip("/").split("?", 1)[0].split("#", 1)[0]
    for raw in cleaned.split("/"):
        seg = raw.strip()
        if not seg:
            continue
        if seg.startswith(":") or seg == "*":
            continue
        # Express-style ``:foo`` was already covered; also tolerate
        # angle-bracket placeholders and curly-brace placeholders.
        if (seg.startswith("<") and seg.endswith(">")) or \
           (seg.startswith("{") and seg.endswith("}")):
            continue
        if is_noise(seg):
            continue
        return seg
    return None


def _candidate_files(ctx: "ScanContext") -> list[str]:
    """Yield tracked source files that survive the exclusion filters."""
    out: list[str] = []
    for raw in ctx.tracked_files:
        p = posix(raw)
        low = p.lower()
        if not any(low.endswith(suf) for suf in _SOURCE_SUFFIXES):
            continue
        if any(sub in low for sub in _EXCLUDED_SUBSTRINGS):
            continue
        if any(low.endswith(suf) for suf in _EXCLUDED_FILENAME_SUFFIXES):
            continue
        out.append(p)
    return out


# ── Extractor ───────────────────────────────────────────────────────────────


class FastifyRouteExtractor:
    """Code-based Fastify route extractor. Activation: see module doc."""

    name = "route-fastify"

    # 192 KB is plenty for any real route file. Defensive cap so a
    # massive minified bundle that slipped past the dist/ filter can't
    # blow up memory.
    _MAX_FILE_BYTES = 192 * 1024

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_active(ctx):
            return []

        repo = Path(ctx.repo_path)
        buckets: dict[str, list[str]] = defaultdict(list)
        url_count: dict[str, int] = defaultdict(int)

        for rel in _candidate_files(ctx):
            try:
                text = (repo / rel).read_text(
                    encoding="utf-8", errors="ignore",
                )[: self._MAX_FILE_BYTES]
            except OSError:
                continue
            if "fastify" not in text.lower() and ".register(" not in text \
                    and ".route(" not in text:
                # Quick reject — file mentions neither fastify nor any
                # of our route shapes. Cheaper than running 3 regexes.
                continue

            urls: list[str] = []
            for m in _METHOD_CALL_RE.finditer(text):
                urls.append(m.group(2))
            for m in _ROUTE_OBJ_URL_RE.finditer(text):
                urls.append(m.group(2))
            for m in _REGISTER_PREFIX_RE.finditer(text):
                urls.append(m.group(2))

            for url in urls:
                seg = _first_meaningful_segment(url)
                if seg is None:
                    continue
                slug = slugify(seg)
                if not slug:
                    continue
                buckets[slug].append(rel)
                url_count[slug] += 1

        out: list[AnchorCandidate] = []
        for slug, paths in buckets.items():
            unique = tuple(sorted(set(paths)))
            # Confidence: more matched URLs → stronger signal, capped
            # at 0.9 (router-instance present in the file). Mirrors the
            # confidence model in ``go_router.py``.
            confidence = min(0.6 + 0.05 * url_count[slug], 0.9)
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=unique,
                    source=self.name,
                    confidence_self=confidence,
                    rationale=(
                        f"fastify route slug '{slug}' from "
                        f"{url_count[slug]} URL match(es) across "
                        f"{len(unique)} file(s)"
                    ),
                ),
            )
        return out


__all__ = ["FastifyRouteExtractor"]
