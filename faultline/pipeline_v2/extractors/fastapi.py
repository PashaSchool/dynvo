"""FastApiRouteExtractor — FastAPI decorator routes → anchors + routes.

Parses ``.py`` files for FastAPI route declarations:

  * ``@app.get("/x")`` / ``@router.post("/y", status_code=201)`` —
    decorator on an app or router object names the HTTP method + the
    leaf URL path.
  * ``router = APIRouter(prefix="/api/admin")`` — the router's own
    prefix, composed in front of every decorator path that targets that
    router variable in the same file.
  * ``app.include_router(admin.router, prefix="/extra")`` — an optional
    extra prefix composed in front of every route the included module
    contributed.

The URL pattern lives INSIDE the source (decorator arg + prefixes), not
in the file-system path — so unlike the filesystem ``route`` extractor
this one emits explicit ``routes`` tuples on each :class:`AnchorCandidate`
that ``build_routes_index`` reads directly (Pass A).

We use REGEX deliberately — not the Python AST — to match the style of
the other extractors and stay robust to partial/invalid files. Patterns
live in ``eval/stacks/fastapi.yaml`` (per ``stack-pattern-library``);
this module only compiles + applies them.

Activation gate: the extractor fires when Stage 0 / the auditor
classified the repo as ``fastapi`` (primary OR secondary stack), or
when a Python repo otherwise exposes FastAPI source markers. Self-skips
to ``[]`` on non-FastAPI repos.

No LLM. No network. Pure file-system scan + regex.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _load_config() -> dict:
    """Load fastapi.yaml from the packaged data tree (hermetic)."""
    return load_stack_yaml("fastapi")


# Compiled-regex cache keyed by id(config) so test reloads don't reuse
# stale patterns.
_COMPILED_CACHE: dict[int, "_Compiled"] = {}


class _Compiled:
    """Compiled regexes + scalar config for one fastapi.yaml dict."""

    __slots__ = (
        "decorator_re",
        "router_ctor_re",
        "prefix_re",
        "include_router_re",
        "file_suffix",
        "excludes",
        "conf_with_ctor",
        "conf_decorator_only",
    )

    def __init__(self, config: dict) -> None:
        rx = config.get("route_extraction") or {}
        # Fall back to built-in defaults so a missing/garbled YAML still
        # yields a working extractor (the YAML is the source of truth but
        # must not be a single point of failure).
        decorator = rx.get("decorator_pattern") or (
            r"@(\w+)\.(get|post|put|patch|delete|head|options)\("
            r"\s*['\"]([^'\"]*)['\"]"
        )
        router_ctor = rx.get("router_ctor_pattern") or (
            r"(\w+)\s*(?::\s*APIRouter\s*)?=\s*APIRouter\(([^)]*)\)"
        )
        prefix = rx.get("prefix_pattern") or r"prefix\s*=\s*['\"]([^'\"]*)['\"]"
        include = rx.get("include_router_pattern") or (
            r"\.include_router\(\s*([\w.]+)\s*(?:,\s*([^)]*))?\)"
        )
        self.decorator_re = re.compile(decorator)
        self.router_ctor_re = re.compile(router_ctor)
        self.prefix_re = re.compile(prefix)
        self.include_router_re = re.compile(include)
        self.file_suffix = str(rx.get("file_suffix") or ".py")
        self.excludes = tuple(
            str(p) for p in (config.get("excludes") or []) if isinstance(p, str)
        )
        conf = config.get("confidence") or {}
        self.conf_with_ctor = float(conf.get("with_router_ctor", 0.9))
        self.conf_decorator_only = float(conf.get("decorator_only", 0.75))


def _compile(config: dict) -> _Compiled:
    key = id(config)
    cached = _COMPILED_CACHE.get(key)
    if cached is None:
        cached = _Compiled(config)
        _COMPILED_CACHE[key] = cached
    return cached


# ── Activation gate ────────────────────────────────────────────────────────


def _has_fastapi_source(ctx: "ScanContext") -> bool:
    """Cheap structural check — a tracked .py file uses FastAPI/APIRouter.

    Only consulted when the stack tags are inconclusive. Scans at most a
    bounded number of candidate router files (``routers/`` / ``main.py``
    shaped) to keep the gate cheap on large repos.
    """
    checked = 0
    for rel in ctx.tracked_files:
        if not rel.endswith(".py"):
            continue
        norm = rel.replace("\\", "/")
        if "/site-packages/" in f"/{norm}" or "/.venv/" in f"/{norm}":
            continue
        text = read_text(ctx.repo_path / rel)
        if text and ("APIRouter(" in text or "FastAPI(" in text):
            return True
        checked += 1
        if checked >= 200:
            break
    return False


def _is_fastapi_repo(ctx: "ScanContext") -> bool:
    audited = (ctx.audited_stack or "").lower()
    if audited == "fastapi" or audited.startswith("fastapi"):
        return True
    if (ctx.stack or "").lower() == "fastapi":
        return True
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    if "fastapi" in secondaries:
        return True
    # Python repo with inconclusive stack tag → confirm via source marker.
    stack = (ctx.stack or "").lower()
    if stack in ("python", "python-lib", "python-library", "") or not stack:
        return _has_fastapi_source(ctx)
    return False


# ── Helpers ────────────────────────────────────────────────────────────────


def _join_path(*parts: str) -> str:
    """Join URL parts, collapsing duplicate slashes; keep ``{param}``."""
    joined = "/".join(p.strip("/") for p in parts if p)
    joined = re.sub(r"/{2,}", "/", joined)
    return "/" + joined if not joined.startswith("/") else joined


def _route_to_slug(route: str) -> str:
    if not route or route == "/":
        return "root"
    stripped = route.replace("{", " ").replace("}", " ").replace(":", " ")
    return slugify(stripped) or "root"


def _module_key(rel_path: str) -> str:
    """Module stem used to correlate ``include_router(<module>.router)``.

    ``backend/routers/admin.py`` → ``admin``.
    """
    return Path(rel_path).stem


def _is_excluded(rel_path: str, excludes: tuple[str, ...]) -> bool:
    p = f"/{posix(rel_path)}"
    return any(ex and ex in p for ex in excludes)


# ── Extractor ──────────────────────────────────────────────────────────────


class FastApiRouteExtractor:
    """FastAPI decorator-route parser. Emits anchors + explicit routes.

    Implements the :class:`AnchorExtractor` Protocol.
    """

    name = "fastapi-route"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else _load_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_fastapi_repo(ctx):
            return []
        c = _compile(self._config)

        # First pass — collect per-module routes + remember which files
        # declared a router constructor (for confidence) and gather
        # include_router prefixes so a router included with an extra
        # prefix gets it composed onto its routes.
        # module_key -> list[(pattern_without_include_prefix, method, file)]
        module_routes: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        module_has_ctor: dict[str, bool] = defaultdict(bool)
        # module_key -> extra prefix supplied at include_router(...) site
        include_prefix: dict[str, str] = {}

        for rel_path in ctx.tracked_files:
            if not rel_path.endswith(c.file_suffix):
                continue
            if _is_excluded(rel_path, c.excludes):
                continue
            text = read_text(ctx.repo_path / rel_path)
            if not text:
                continue
            if "@" not in text and "include_router" not in text:
                continue

            mod = _module_key(rel_path)
            rel_posix = posix(rel_path)

            # Router var → its own prefix declared in this file.
            router_prefix: dict[str, str] = {}
            for m in c.router_ctor_re.finditer(text):
                var = m.group(1)
                ctor_args = m.group(2) or ""
                pm = c.prefix_re.search(ctor_args)
                router_prefix[var] = pm.group(1) if pm else ""
                module_has_ctor[mod] = True

            # Decorator routes — compose router var prefix + leaf path.
            for m in c.decorator_re.finditer(text):
                obj = m.group(1)
                method = m.group(2).upper()
                leaf = m.group(3)
                # ``app`` (the FastAPI instance) carries no prefix.
                prefix = router_prefix.get(obj, "")
                pattern = _join_path(prefix, leaf)
                module_routes[mod].append((pattern, method, rel_posix))

            # include_router(<x>.router, prefix="...") — record the extra
            # prefix against the included module so its routes inherit it.
            for m in c.include_router_re.finditer(text):
                ref = m.group(1)  # e.g. "admin.router" / "admin"
                args = m.group(2) or ""
                pm = c.prefix_re.search(args)
                if not pm:
                    continue
                inc_mod = ref.split(".")[0]
                include_prefix[inc_mod] = pm.group(1)

        # Second pass — build one anchor per module, composing any
        # include_router prefix onto each route.
        out: list[AnchorCandidate] = []
        for mod, routes in module_routes.items():
            extra = include_prefix.get(mod, "")
            composed: list[tuple[str, str, str]] = []
            files: set[str] = set()
            for pattern, method, file_str in routes:
                final = _join_path(extra, pattern) if extra else pattern
                composed.append((final, method, file_str))
                files.add(file_str)
            if not composed:
                continue

            # Name the anchor by the longest shared URL prefix (≈ the
            # router's resource), falling back to the module stem.
            slug = _route_to_slug(_common_prefix([p for p, _, _ in composed]))
            if slug == "root":
                slug = slugify(mod) or "root"

            conf = (
                c.conf_with_ctor if module_has_ctor.get(mod)
                else c.conf_decorator_only
            )
            sample = ", ".join(
                f"{meth} {pat}" for pat, meth, _ in composed[:5]
            )
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=tuple(sorted(files)),
                    source=self.name,
                    confidence_self=conf,
                    rationale=f"fastapi routes: {sample}",
                    routes=tuple(composed),
                ),
            )
        return out


def _common_prefix(patterns: list[str]) -> str:
    """Longest common URL-path prefix across patterns (segment-wise)."""
    if not patterns:
        return ""
    seg_lists = [p.strip("/").split("/") for p in patterns]
    common: list[str] = []
    for segs in zip(*seg_lists):
        first = segs[0]
        if all(s == first for s in segs) and not first.startswith("{"):
            common.append(first)
        else:
            break
    return "/" + "/".join(common) if common else ""


__all__ = ["FastApiRouteExtractor"]
