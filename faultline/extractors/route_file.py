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
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return False
    try:
        text = pkg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return '"next"' in text or "'next'" in text


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


# ── Extractor wrapper (Protocol-conforming, Phase 3b PoC) ────────────


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
    "collect_routes",
    "is_nextjs_app_router",
]
