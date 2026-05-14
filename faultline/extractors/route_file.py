"""Route-file extractor — Next.js App Router (Phase 3b proof-of-concept).

Per the route-file-extractor skill (faultlines-app repo,
``.claude/skills/route-file-extractor/SKILL.md``). The first concrete
extractor that produces Signal objects from a stack-specific routing
convention. Phase 3b ships only Next.js App Router; Pages Router /
Remix / Astro / SvelteKit / Rails / Django / FastAPI / Express follow
the same pattern (one parser strategy per stack).

What it emits:
    Signal(kind="route", source="route-file-extractor", payload={
      "framework": "nextjs-app-router",
      "method": "GET" | "POST" | ... | None,   # None for non-API pages
      "path": "/dashboard/billing",
      "handler_file": "app/(dashboard)/billing/page.tsx",
      "parent_hint": "(dashboard)" | None,
      "kind": "page" | "api" | "layout" | "loading" | "error",
    })

Conventions handled:
- ``app/<path>/page.{tsx,jsx,ts,js,mdx}`` → page route (URL = path)
- ``app/<path>/route.{ts,js}`` → API endpoint (method inferred from
  exported function names: GET / POST / PUT / PATCH / DELETE / OPTIONS / HEAD)
- ``app/(group)/<path>/...`` → ``(group)`` is the maintainer's explicit
  feature bucket; URL strips it; we keep it as ``parent_hint``
- ``app/[slug]/...`` → dynamic route; path uses ``:slug`` placeholder
- ``app/_private/...`` → Next's private-folder convention; SKIPPED
- ``app/@slot/...`` → parallel route slot; SKIPPED for now (rare)
- ``app/<path>/{layout,loading,error,not-found,template,default}.tsx`` →
  framework files; emitted with kind=<layout|...> for completeness but
  the aggregator should usually ignore them as flow candidates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal

# Next App Router special files (per https://nextjs.org/docs/app)
_PAGE_FILE_RE = re.compile(r"^page\.(tsx|jsx|ts|js|mdx)$")
_API_ROUTE_FILE_RE = re.compile(r"^route\.(ts|js)$")
_FRAMEWORK_FILES = {
    "layout", "loading", "error", "not-found", "template",
    "default", "global-error", "metadata",
}
_FRAMEWORK_FILE_RE = re.compile(
    r"^(" + "|".join(_FRAMEWORK_FILES) + r")\.(tsx|jsx|ts|js)$"
)

# Exported HTTP-method names recognised in route.ts
_METHOD_EXPORT_RE = re.compile(
    r"\bexport\s+(?:async\s+)?(?:function|const|let|var)\s+"
    r"(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b"
)

# Skip these directory names anywhere in the path.
_SKIP_DIRS = {"node_modules", ".next", ".turbo", "dist", "build", ".vercel"}


@dataclass(frozen=True, slots=True, kw_only=True)
class NextRoute:
    """Parsed Next App Router route record."""

    handler_file: str          # repo-relative
    url_path: str              # "/dashboard/billing", "/users/:id"
    methods: tuple[str, ...]   # ("GET",) for pages; ("GET", "POST", ...) for routes
    kind: str                  # "page" | "api" | "layout" | "loading" | ...
    parent_hint: str | None    # nearest "(group)" if any; else None


def is_nextjs_app_router(repo_root: Path) -> bool:
    """Quick stack detection: True iff repo has an `app/` directory
    AND `next` in package.json dependencies (loose check — the
    full check belongs in stack-pattern-library YAML in Phase 2).
    """
    if not (repo_root / "app").is_dir():
        # Could also live under apps/web/app for monorepos — handle later
        if not _has_app_dir_in_workspace(repo_root):
            return False
    return _has_next_dep(repo_root)


def is_nextjs_pages_router(repo_root: Path) -> bool:
    """True iff the repo has a `pages/` directory + `next` in
    package.json. Pages Router is the legacy convention; many real
    repos still use it (and quite a few hybrid app+pages exist).
    """
    if not (repo_root / "pages").is_dir():
        if not _has_pages_dir_in_workspace(repo_root):
            return False
    return _has_next_dep(repo_root)


def _has_next_dep(repo_root: Path) -> bool:
    """True if `next` appears in the root package.json OR any
    apps/*/package.json or packages/*/package.json (monorepo)."""
    candidates = [repo_root / "package.json"]
    for parent_name in ("apps", "packages"):
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            pkg = child / "package.json"
            if pkg.exists():
                candidates.append(pkg)
    for pkg in candidates:
        if not pkg.exists():
            continue
        try:
            text = pkg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if '"next"' in text or "'next'" in text:
            return True
    return False


def _has_pages_dir_in_workspace(repo_root: Path) -> bool:
    for parent_name in ("apps", "packages"):
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (child / "pages").is_dir():
                return True
    return False


def _has_app_dir_in_workspace(repo_root: Path) -> bool:
    """Look one level deep into apps/* and packages/* for an app/ dir."""
    for parent_name in ("apps", "packages"):
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (child / "app").is_dir():
                return True
    return False


def _all_app_dirs(repo_root: Path) -> list[Path]:
    """Locate every ``app/`` directory worth scanning (root + workspaces)."""
    out: list[Path] = []
    if (repo_root / "app").is_dir():
        out.append(repo_root / "app")
    for parent_name in ("apps", "packages"):
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (child / "app").is_dir():
                out.append(child / "app")
    return out


def collect_routes(repo_root: Path) -> list[NextRoute]:
    """Walk every detected ``app/`` tree and yield route records.

    Always returns a list — empty when the repo has no Next App Router.
    Errors during file read fall through silently (best-effort).
    """
    if not is_nextjs_app_router(repo_root):
        return []
    out: list[NextRoute] = []
    for app_dir in _all_app_dirs(repo_root):
        for path in app_dir.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(repo_root).parts
            if any(p in _SKIP_DIRS for p in rel_parts):
                continue
            # Skip private folders (Next convention: prefix _)
            if any(p.startswith("_") for p in rel_parts[:-1]):
                continue
            # Skip parallel-route slots (rare; @-prefixed)
            if any(p.startswith("@") for p in rel_parts[:-1]):
                continue

            name = path.name
            page_m = _PAGE_FILE_RE.match(name)
            api_m = _API_ROUTE_FILE_RE.match(name)
            fw_m = _FRAMEWORK_FILE_RE.match(name)
            if not (page_m or api_m or fw_m):
                continue

            url_path, parent_hint = _build_url_path(rel_parts, app_dir, repo_root)
            handler_file = str(path.relative_to(repo_root)).replace("\\", "/")

            if page_m:
                kind = "page"
                methods = ("GET",)
            elif api_m:
                kind = "api"
                methods = _detect_api_methods(path)
            else:
                kind = name.split(".", 1)[0]
                methods = ()

            out.append(NextRoute(
                handler_file=handler_file,
                url_path=url_path,
                methods=methods,
                kind=kind,
                parent_hint=parent_hint,
            ))
    return out


def _build_url_path(
    rel_parts: tuple[str, ...],
    app_dir: Path,
    repo_root: Path,
) -> tuple[str, str | None]:
    """Convert app-tree path segments into (url_path, parent_hint).

    Drops route groups ``(name)`` from the URL but reports the LAST
    such group as ``parent_hint`` (used by aggregator for grouping).
    Converts ``[slug]`` → ``:slug``, ``[...catchall]`` → ``*catchall``.
    """
    # Locate the index of the app/ folder name itself in rel_parts;
    # everything AFTER it is the route segment + filename.
    app_rel = app_dir.relative_to(repo_root).parts
    segments = list(rel_parts[len(app_rel):-1])  # excl. the file itself
    parent_hint: str | None = None
    url_segments: list[str] = []
    for seg in segments:
        if seg.startswith("(") and seg.endswith(")"):
            parent_hint = seg
            continue
        if seg.startswith("[...") and seg.endswith("]"):
            url_segments.append("*" + seg[4:-1])
            continue
        if seg.startswith("[") and seg.endswith("]"):
            url_segments.append(":" + seg[1:-1])
            continue
        url_segments.append(seg)
    url_path = "/" + "/".join(url_segments) if url_segments else "/"
    return url_path, parent_hint


def _detect_api_methods(route_file: Path) -> tuple[str, ...]:
    """Read route.ts and return the set of HTTP-method exports."""
    try:
        text = route_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    methods = sorted({m.group(1) for m in _METHOD_EXPORT_RE.finditer(text)})
    return tuple(methods)


# ── Pages Router (legacy) ────────────────────────────────────────────


_PAGES_FRAMEWORK_FILES = {"_app", "_document", "_error", "404", "500"}


def _all_pages_dirs(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    if (repo_root / "pages").is_dir():
        out.append(repo_root / "pages")
    for parent_name in ("apps", "packages"):
        parent = repo_root / parent_name
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (child / "pages").is_dir():
                out.append(child / "pages")
    return out


def collect_pages_routes(repo_root: Path) -> list[NextRoute]:
    """Walk every `pages/` tree (legacy Next.js Pages Router).

    File conventions:
      pages/index.{tsx,jsx,ts,js,mdx}  → URL = '/<parent_path>'
      pages/<name>.{...}               → URL = '/<parent_path>/<name>'
      pages/api/<...>.{ts,js}          → API endpoint (single handler;
                                         method = 'ANY')
      pages/[slug].tsx                 → /:slug
      pages/[...catchall].tsx          → /*catchall
      pages/_{app,document,error,...}  → SKIPPED (framework)

    Pages Router has no equivalent of (group) — there's no parent_hint
    concept. Every detected route is parent_hint=None.
    """
    if not is_nextjs_pages_router(repo_root):
        return []
    out: list[NextRoute] = []
    valid_exts = (".tsx", ".jsx", ".ts", ".js", ".mdx")
    for pages_dir in _all_pages_dirs(repo_root):
        for path in pages_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in valid_exts:
                continue
            rel_parts = path.relative_to(repo_root).parts
            if any(p in _SKIP_DIRS for p in rel_parts):
                continue
            stem = path.stem
            if stem in _PAGES_FRAMEWORK_FILES:
                continue
            # Determine if it's an API route (lives under pages/api/)
            pages_rel = pages_dir.relative_to(repo_root).parts
            after_pages = rel_parts[len(pages_rel):]
            is_api = len(after_pages) >= 1 and after_pages[0] == "api"

            # Build URL: drop the file's own segment if it's "index";
            # convert dynamic segments
            segments = list(after_pages[:-1])     # excl. file itself
            if stem != "index":
                segments.append(stem)
            url_parts: list[str] = []
            for seg in segments:
                if seg.startswith("[...") and seg.endswith("]"):
                    url_parts.append("*" + seg[4:-1])
                elif seg.startswith("[") and seg.endswith("]"):
                    url_parts.append(":" + seg[1:-1])
                else:
                    url_parts.append(seg)
            url_path = "/" + "/".join(url_parts) if url_parts else "/"

            kind = "api" if is_api else "page"
            handler_file = str(path.relative_to(repo_root)).replace("\\", "/")

            out.append(NextRoute(
                handler_file=handler_file,
                url_path=url_path,
                methods=("ANY",) if is_api else ("GET",),
                kind=kind,
                parent_hint=None,
            ))
    return out


# ── Extractor wrapper (Protocol-conforming, Phase 3b PoC) ────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class NextPagesRouteFileExtractor:
    """Phase 3b extractor for Next.js Pages Router (legacy)."""

    name: str = "route-file-extractor:nextjs-pages-router"

    def applicable(self, repo_root: Path) -> bool:
        return is_nextjs_pages_router(repo_root)

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        routes = collect_pages_routes(repo_root)
        return [
            Signal(
                kind="route",
                source=self.name,
                payload={
                    "framework": "nextjs-pages-router",
                    "method": (r.methods[0] if r.methods else None),
                    "methods": r.methods,
                    "path": r.url_path,
                    "handler_file": r.handler_file,
                    "kind": r.kind,
                    "parent_hint": r.parent_hint,
                },
            )
            for r in routes
        ]


@dataclass(frozen=True, slots=True, kw_only=True)
class NextRouteFileExtractor:
    """Phase 3b extractor for Next.js App Router routes.

    Conforms to the ``Extractor`` Protocol from ``faultline.protocols``.
    A Phase-2 orchestrator instantiates one of these and feeds its
    ``Signal`` outputs to ``FlowAggregator`` / ``FeatureAggregator``.
    """

    name: str = "route-file-extractor:nextjs-app-router"

    def applicable(self, repo_root: Path) -> bool:
        return is_nextjs_app_router(repo_root)

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files   # we walk the tree directly via Path.rglob
        routes = collect_routes(repo_root)
        return [
            Signal(
                kind="route",
                source=self.name,
                payload={
                    "framework": "nextjs-app-router",
                    "method": (r.methods[0] if r.methods else None),
                    "methods": r.methods,
                    "path": r.url_path,
                    "handler_file": r.handler_file,
                    "kind": r.kind,
                    "parent_hint": r.parent_hint,
                },
            )
            for r in routes
        ]


__all__ = [
    "NextRoute",
    "NextRouteFileExtractor",
    "NextPagesRouteFileExtractor",
    "build_route_hints_block",
    "collect_routes",
    "collect_pages_routes",
    "is_nextjs_app_router",
    "is_nextjs_pages_router",
    "is_route_hints_enabled",
]


# ── Prompt-hint formatting (Phase 3b LLM integration) ────────────────


import os as _os
from collections import Counter as _Counter, defaultdict as _defaultdict


def is_route_hints_enabled() -> bool:
    """True iff FAULTLINE_ROUTE_HINTS env var is set.

    Off by default during Phase 3b rollout so A/B testing is clean
    (run with and without to measure recall delta on the golden corpus).
    """
    return _os.environ.get("FAULTLINE_ROUTE_HINTS", "").lower() in {
        "1", "true", "yes", "on",
    }


# Default budget for the route-hints block. Routes are short strings
# (~60-100 chars each); 2000 chars ≈ 500 tokens fits ~25 routes
# with grouping headers — enough signal without overflowing the prompt.
DEFAULT_ROUTE_HINTS_BUDGET_CHARS = 2000


def build_route_hints_block(
    repo_root: Path,
    *,
    budget_chars: int = DEFAULT_ROUTE_HINTS_BUDGET_CHARS,
) -> str:
    """Render a prompt-ready hints block listing routes grouped by
    parent_hint. Empty when no routes detected.

    Format example:
      === ROUTING-HINT (Next.js App Router) ===
      Routes maintainer-grouped under (dashboard):
        page  /billing
        page  /settings/:tab
        api   POST /api/billing/webhook
      Routes maintainer-grouped under (auth):
        page  /login
        page  /signup
      Top-level routes:
        page  /
      === END ROUTING-HINT ===

    Each route group is a strong feature-clustering hint — the
    maintainer literally put these files inside (group)/ to declare
    them as one feature. The LLM should respect that grouping.
    """
    # Try App Router first; fall back to Pages Router.
    routes = collect_routes(repo_root)
    framework_label = "Next.js App Router"
    if not routes:
        routes = collect_pages_routes(repo_root)
        framework_label = "Next.js Pages Router"
    if not routes:
        return ""

    # Sort: pages then api, by path; group by parent_hint
    grouped: dict[str | None, list[NextRoute]] = _defaultdict(list)
    for r in routes:
        if r.kind in ("layout", "loading", "error", "not-found", "template",
                      "default", "global-error", "metadata"):
            continue   # framework files don't help feature clustering
        grouped[r.parent_hint].append(r)

    if not grouped:
        return ""

    parts: list[str] = [f"=== ROUTING-HINT ({framework_label}) ==="]
    parts.append(
        f"Routes the maintainer organised below. Use these maintainer-"
        f"declared groupings as feature boundaries; the (group) names "
        f"in particular are explicit feature buckets."
    )

    # Order groups: explicit (groups) first by route count desc, then None last
    explicit_groups = sorted(
        (g for g in grouped if g is not None),
        key=lambda g: -len(grouped[g]),
    )
    for g in explicit_groups:
        parts.append(f"")
        parts.append(f"Routes maintainer-grouped under {g}:")
        for r in sorted(grouped[g], key=lambda r: (r.kind != "page", r.url_path)):
            parts.append(_format_route_line(r))

    if grouped.get(None):
        parts.append("")
        parts.append("Top-level routes (no group):")
        for r in sorted(grouped[None], key=lambda r: (r.kind != "page", r.url_path)):
            parts.append(_format_route_line(r))

    parts.append("=== END ROUTING-HINT ===")

    block = "\n".join(parts)
    if len(block) <= budget_chars:
        return block

    # Trim from the tail with an "N more omitted" marker
    truncated: list[str] = []
    for line in parts:
        if sum(len(x) + 1 for x in truncated) + len(line) > budget_chars - 80:
            truncated.append("  … (more routes omitted for budget)")
            truncated.append("=== END ROUTING-HINT ===")
            return "\n".join(truncated)
        truncated.append(line)
    return "\n".join(truncated)


def _format_route_line(r: NextRoute) -> str:
    if r.kind == "api":
        methods = ",".join(r.methods) if r.methods else "?"
        return f"  api    {methods:<12} {r.url_path}"
    return f"  page   {r.url_path}"
