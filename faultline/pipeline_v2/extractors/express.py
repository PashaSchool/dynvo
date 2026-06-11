"""ExpressRouteExtractor — code-based extractor for Express HTTP routes.

Express (the most popular Node backend framework) does not encode
routes in the filesystem — routes live in code as method calls:

    app.get("/users", handler)
    router.post("/users/:id/secrets", handler)
    app.route("/books").get(handler).post(handler)
    app.use("/api/v1/secrets", secretsRouter)        // mount prefix
    const v1 = express.Router(); v1.get("/users")    // aliased router

We grep over JS/TS source files (regex, not AST — we can't assume a
TypeScript toolchain on the user's machine, mirroring the same choice
made for ``fastify.py`` / ``go_router.py``). Each matched URL produces
a route signal; the first non-noise, non-dynamic path segment becomes
the anchor slug.

Unlike the fastify extractor's fixed receiver list, this one ALSO
detects per-file ``express.Router()`` / ``Router()`` assignments, so
aliased routers (``const v1 = express.Router()``) are caught while
unrelated ``foo.get(...)`` calls on arbitrary objects still are not.
Route paths must start with ``/`` — that filters out Express settings
getters like ``app.get("port")`` / ``app.get("view engine")``.

Patterns live in ``eval/stacks/express.yaml`` (mirrored byte-identical
into ``faultline/pipeline_v2/data/stacks/`` per ``stack-pattern-library``);
this module only compiles + applies them, with built-in fallbacks so a
garbled YAML can't zero out the extractor.

Activation gate
===============

This extractor self-skips unless ONE of these holds:

  - the Stage 0.5 auditor declared ``express`` (primary OR secondary)
  - Stage 0's heuristic tag (root or any workspace) is ``express``
  - ``express`` is a RUNTIME dependency (``dependencies`` block only)
    of the root or any workspace ``package.json``

The dep-check is deliberately narrower than fastify's: ``express`` in
``devDependencies`` usually means "test server for a library" and in
``peerDependencies`` usually means "Express middleware library" —
neither is an Express APP, so neither activates the extractor.

NestJS exclusion: Nest wraps Express (``@nestjs/platform-express``
pulls ``express`` into ``dependencies`` on many Nest apps) but declares
its routes via its own ``@Controller`` / ``@Get`` decorators. A
``package.json`` whose dependencies include any ``@nestjs/``-scoped
package therefore does NOT count as an express signal — activating
would double-extract against NestJS conventions. (The fastify extractor
intentionally DOES activate on nestjs because Nest can sit on the
Fastify adapter; for Express the Nest decorators are the source of
truth, not the underlying engine.)

No LLM. No network. Pure regex over text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._pattern_base import PatternExtractor
from faultline.pipeline_v2.extractors._util import (
    is_audited_stack,
    is_noise,
    posix,
    read_json,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── Compiled config ─────────────────────────────────────────────────────────

# Built-in fallbacks — the YAML is the source of truth but must not be
# a single point of failure (mirrors ``fastapi.py``).
_FALLBACK_SOURCE_SUFFIXES: tuple[str, ...] = (
    ".ts", ".js", ".mts", ".mjs", ".cts", ".cjs",
)
_FALLBACK_RECEIVERS: tuple[str, ...] = ("app", "router", "server", "api")
_FALLBACK_ROUTER_CTOR = (
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[\w.$<>\[\] ]+?)?\s*"
    r"=\s*(?:express\s*\.\s*)?Router\s*\("
)
_FALLBACK_METHOD_CALL = (
    r"\b([A-Za-z_$][\w$]*)\."
    r"(get|post|put|patch|delete|head|options|all)\s*\(\s*"
    r"""(["'`])([^"'`]+)\3"""
)
_FALLBACK_ROUTE_CHAIN = (
    r"\b([A-Za-z_$][\w$]*)\.route\s*\(\s*"
    r"""(["'`])([^"'`]+)\2"""
)
_FALLBACK_MOUNT = (
    r"\b([A-Za-z_$][\w$]*)\.use\s*\(\s*"
    r"""(["'`])([^"'`]+)\2"""
)
_FALLBACK_EXCLUDES: tuple[str, ...] = (
    "/node_modules/", "/dist/", "/build/", "/out/", "/.next/",
    "/__tests__/", "/test/", "/tests/", "/__mocks__/", "/coverage/",
    "/vendor/",
)
_FALLBACK_EXCLUDE_SUFFIXES: tuple[str, ...] = (
    ".test.ts", ".test.js", ".test.mts", ".test.mjs",
    ".spec.ts", ".spec.js", ".spec.mts", ".spec.mjs",
    ".d.ts",
)


class _Compiled:
    """Compiled regexes + scalar config for one express.yaml dict."""

    __slots__ = (
        "source_suffixes",
        "default_receivers",
        "router_ctor_re",
        "method_call_re",
        "route_chain_re",
        "mount_re",
        "excludes",
        "exclude_suffixes",
        "conf_base",
        "conf_per_match",
        "conf_cap",
    )

    def __init__(self, config: dict) -> None:
        rx = config.get("route_extraction") or {}
        self.source_suffixes = _str_tuple(
            rx.get("source_suffixes"), _FALLBACK_SOURCE_SUFFIXES,
        )
        self.default_receivers = frozenset(
            _str_tuple(rx.get("default_receivers"), _FALLBACK_RECEIVERS),
        )
        self.router_ctor_re = re.compile(
            rx.get("router_ctor_pattern") or _FALLBACK_ROUTER_CTOR,
        )
        self.method_call_re = re.compile(
            rx.get("method_call_pattern") or _FALLBACK_METHOD_CALL,
        )
        self.route_chain_re = re.compile(
            rx.get("route_chain_pattern") or _FALLBACK_ROUTE_CHAIN,
        )
        self.mount_re = re.compile(
            rx.get("mount_pattern") or _FALLBACK_MOUNT,
        )
        self.excludes = _str_tuple(config.get("excludes"), _FALLBACK_EXCLUDES)
        self.exclude_suffixes = _str_tuple(
            config.get("exclude_filename_suffixes"),
            _FALLBACK_EXCLUDE_SUFFIXES,
        )
        conf = config.get("confidence") or {}
        self.conf_base = float(conf.get("base", 0.6))
        self.conf_per_match = float(conf.get("per_match", 0.05))
        self.conf_cap = float(conf.get("cap", 0.9))


def _str_tuple(value: object, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Coerce a YAML list into a tuple of strings; fall back when empty."""
    if isinstance(value, (list, tuple)):
        out = tuple(str(v) for v in value if isinstance(v, str) and v)
        if out:
            return out
    return fallback


# ── Activation ──────────────────────────────────────────────────────────────


def _is_nest_package(pkg: object) -> bool:
    """True if ``pkg`` runtime-depends on any ``@nestjs/``-scoped package."""
    if not isinstance(pkg, dict):
        return False
    deps = pkg.get("dependencies")
    if not isinstance(deps, dict):
        return False
    return any(str(name).startswith("@nestjs/") for name in deps)


def _has_express_dep(pkg: object) -> bool:
    """True if ``pkg`` lists ``express`` as a RUNTIME dependency and is
    not a NestJS package (see module docstring)."""
    if not isinstance(pkg, dict):
        return False
    deps = pkg.get("dependencies")
    if not isinstance(deps, dict) or "express" not in deps:
        return False
    return not _is_nest_package(pkg)


def _is_active(ctx: "ScanContext") -> bool:
    """Self-skip gate — see module docstring."""
    if is_audited_stack(ctx, "express"):
        return True
    # NestJS anywhere in the stack tags → Nest conventions own the
    # routes; never double-extract from the underlying Express engine.
    if is_audited_stack(ctx, "nestjs"):
        return False
    if (ctx.stack or "").lower() == "express":
        return True
    for ws in (ctx.workspaces or []):
        if (ws.stack or "").lower() == "express":
            return True
        if _has_express_dep(ws.package_json):
            return True
    # Root package.json (single-app repos where Stage 0 tagged a
    # different dominant framework, e.g. a Next app with an embedded
    # Express server).
    return _has_express_dep(read_json(Path(ctx.repo_path) / "package.json"))


# ── Slug derivation ─────────────────────────────────────────────────────────


def _first_meaningful_segment(url: str) -> str | None:
    """Pick the first URL segment that is not noise / not a dynamic param.

    Express uses ``/users/:id`` (colon prefix) and ``*`` catch-alls.
    Both are skipped — they describe params, not features. Non-rooted
    strings (``"port"``, ``"view engine"``) are Express settings keys,
    not routes, and yield ``None``.
    """
    if not url or not url.startswith("/"):
        return None
    cleaned = url.lstrip("/").split("?", 1)[0].split("#", 1)[0]
    for raw in cleaned.split("/"):
        seg = raw.strip()
        if not seg:
            continue
        if seg.startswith(":") or seg == "*":
            continue
        if (seg.startswith("{") and seg.endswith("}")) or \
           (seg.startswith("<") and seg.endswith(">")):
            continue
        if is_noise(seg):
            continue
        return seg
    return None


def _candidate_files(
    ctx: "ScanContext", compiled: "_Compiled",
) -> list[str]:
    """Yield tracked source files that survive the exclusion filters."""
    out: list[str] = []
    for raw in ctx.tracked_files:
        p = posix(raw)
        low = f"/{p.lower()}"
        if not any(low.endswith(suf) for suf in compiled.source_suffixes):
            continue
        if any(sub in low for sub in compiled.excludes):
            continue
        if any(low.endswith(suf) for suf in compiled.exclude_suffixes):
            continue
        out.append(p)
    return out


# ── Extractor ───────────────────────────────────────────────────────────────


class ExpressRouteExtractor(PatternExtractor):
    """Code-based Express route extractor. Activation: see module doc."""

    name = "route-express"

    # Defensive cap so a minified bundle that slipped past the dist/
    # filter can't blow up memory (mirrors fastify.py).
    _MAX_FILE_BYTES = 192 * 1024

    def load_config(self) -> dict:
        return load_stack_yaml("express")

    def is_active(self, ctx: "ScanContext") -> bool:
        return _is_active(ctx)

    def compile_patterns(self, config: dict) -> "_Compiled":
        return _Compiled(config)

    def collect(
        self, ctx: "ScanContext", compiled: "_Compiled",
    ) -> dict[str, dict]:
        c = compiled
        repo = Path(ctx.repo_path)
        buckets: dict[str, dict] = {}

        for rel in _candidate_files(ctx, c):
            try:
                text = (repo / rel).read_text(
                    encoding="utf-8", errors="ignore",
                )[: self._MAX_FILE_BYTES]
            except OSError:
                continue
            # Quick reject — file mentions neither a Router constructor
            # nor any known receiver. Cheaper than running 4 regexes.
            if "Router(" not in text and not any(
                f"{r}." in text for r in c.default_receivers
            ):
                continue

            # Per-file receiver set: the fixed defaults PLUS any
            # variable assigned from express.Router() / Router() in
            # this file (aliased routers: ``const v1 = Router()``).
            receivers = set(c.default_receivers)
            for m in c.router_ctor_re.finditer(text):
                receivers.add(m.group(1))

            urls: list[str] = []
            for m in c.method_call_re.finditer(text):
                if m.group(1) in receivers:
                    urls.append(m.group(4))
            for pattern in (c.route_chain_re, c.mount_re):
                for m in pattern.finditer(text):
                    if m.group(1) in receivers:
                        urls.append(m.group(3))

            for url in urls:
                seg = _first_meaningful_segment(url)
                if seg is None:
                    continue
                slug = slugify(seg)
                if not slug:
                    continue
                bucket = buckets.setdefault(slug, {"paths": [], "count": 0})
                bucket["paths"].append(rel)
                bucket["count"] += 1

        return buckets

    def emit(
        self,
        ctx: "ScanContext",
        key: str,
        bucket: dict,
        compiled: "_Compiled",
    ) -> AnchorCandidate:
        unique = tuple(sorted(set(bucket["paths"])))
        count = bucket["count"]
        # Confidence: more matched URLs → stronger signal, capped.
        # Mirrors the confidence model in fastify.py / go_router.py.
        confidence = min(
            compiled.conf_base + compiled.conf_per_match * count,
            compiled.conf_cap,
        )
        return AnchorCandidate(
            name=key,
            paths=unique,
            source=self.name,
            confidence_self=confidence,
            rationale=(
                f"express route slug '{key}' from "
                f"{count} URL match(es) across "
                f"{len(unique)} file(s)"
            ),
        )


__all__ = ["ExpressRouteExtractor"]
