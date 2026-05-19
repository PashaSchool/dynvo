"""RouteFileExtractor — file-system routing convention → anchor.

Covers the convention-based router stacks where the path on disk
encodes the route. Per the ``route-file-extractor`` skill:

  - Next.js App Router    : ``app/**/page.{tsx,jsx,ts,js}``
                            + ``app/**/route.{ts,js}``
  - Next.js Pages Router  : ``pages/**/*.{tsx,jsx,ts,js}``
                            (and ``pages/api/**`` for API)
  - Remix                 : ``app/routes/**``
  - Astro                 : ``src/pages/**``
  - SvelteKit             : ``src/routes/**``
  - Nuxt                  : ``pages/**`` (Nuxt 3 uses ``pages/`` too)
  - Django                : ``**/urls.py``
  - FastAPI               : ``**/routers/*.py`` (convention; not all)
  - Rails                 : ``config/routes.rb`` (single file → one
                            anchor per top-level resource)

Stack-specific patterns are read from ``ctx.stack`` (and the per-
workspace stack when monorepo). The mapping table below is the
in-Python fallback used until ``eval/stacks/<stack>.yaml`` lands —
the ``stack-pattern-library`` skill defines the YAML schema.

The extractor returns one :class:`AnchorCandidate` per top-level
*route group* — not one per file. The first non-noise path segment
under the routing root is the slug. Files in nested route groups
collapse to the same anchor (``app/(dashboard)/settings/page.tsx`` and
``app/(dashboard)/settings/profile/page.tsx`` both belong to the
``settings`` anchor). Route groups in parentheses (``(marketing)``)
are skipped so authors can group without inventing a feature.

No LLM. No network. Pure pattern matching on ``ctx.tracked_files``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── Routing convention table ────────────────────────────────────────────────
#
# (routing_root_prefix, page_suffixes)
#   - routing_root_prefix : every match must start with one of these,
#                           after optional ``src/`` prefix removal.
#   - page_suffixes       : file must end with one of these to count as
#                           a page/handler.
#
# All paths are POSIX. Tests build synthetic repos in ``tmp_path``
# without git, so no normalisation other than slash flipping needed.

_PAGE_TS_SUFFIXES = (
    "/page.tsx", "/page.jsx", "/page.ts", "/page.js",
    "/route.ts", "/route.js",  # App Router API
)

_STACK_ROUTING: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # Next.js App Router — both ``app/`` and ``src/app/`` are valid
    "next-app-router": (("app/", "src/app/"), _PAGE_TS_SUFFIXES),
    # Pages Router — any ts/tsx file under pages/ counts; api/ included
    "next-pages":      (("pages/", "src/pages/"), (".tsx", ".jsx", ".ts", ".js")),
    # Remix — app/routes/**
    "remix":           (("app/routes/",), (".tsx", ".jsx", ".ts", ".js")),
    # Astro — src/pages/**.astro (and ts/tsx for islands API)
    "astro":           (("src/pages/", "pages/"),
                        (".astro", ".tsx", ".ts", ".js")),
    # SvelteKit — src/routes/+page.svelte and +server.ts
    "sveltekit":       (("src/routes/",), ("+page.svelte", "+server.ts",
                                            "+server.js", "+page.ts")),
    # Nuxt — pages/**/*.vue (Nuxt 3)
    "nuxt":            (("pages/", "src/pages/"), (".vue",)),
    # TanStack Router — file-based routes at src/routes/**, AND many
    # apps use src/pages/** alongside an explicit route config. Match
    # both so polyglot frontends like infisical's TanStack+Vite app
    # produce anchor candidates.
    "tanstack-router": (("src/routes/", "src/pages/"),
                        (".tsx", ".jsx", ".ts", ".js")),
    # Generic Vite SPA — most Vite apps mount a router (TanStack /
    # react-router) over src/pages/** or src/routes/**. We use the
    # same convention as TanStack since the file shape is what we
    # actually grep on.
    "vite":            (("src/pages/", "src/routes/"),
                        (".tsx", ".jsx", ".ts", ".js")),
}


# Python web frameworks — when present, routing lives in marker files
# (not file-system convention). For these stacks the extractor emits
# one anchor per *directory containing* the marker file. ``urls.py``
# / ``router*.py`` are the conventional markers.
_PYTHON_ROUTING_MARKERS = (
    "urls.py",          # Django
    "router.py",        # FastAPI convention
    "routers.py",       # variant
    "routes.py",        # Flask / FastAPI variant
)


def _strip_route_groups(segments: list[str]) -> list[str]:
    """Drop Next.js route-group segments like ``(marketing)``.

    Route groups are organisational only — they do not appear in URLs
    and must not become anchor names.
    """
    return [s for s in segments if not (s.startswith("(") and s.endswith(")"))]


def _first_meaningful_segment(segments: list[str]) -> str | None:
    """Return the first segment that is not noise / not a route group.

    Used to derive an anchor slug from a routing path. Dynamic segments
    like ``[id]`` or ``[...slug]`` are skipped — they describe params,
    not features.
    """
    for seg in segments:
        if not seg:
            continue
        # Dynamic segments are not features.
        if seg.startswith("[") and seg.endswith("]"):
            continue
        if seg.startswith("(") and seg.endswith(")"):
            continue
        if is_noise(seg):
            continue
        return seg
    return None


def _emit_for_fs_routing(
    files: list[str],
    routing_roots: tuple[str, ...],
    page_suffixes: tuple[str, ...],
) -> dict[str, list[str]]:
    """File-system routing pass.

    Returns a mapping ``slug → list[file]``. The slug is the first
    meaningful URL segment under one of the routing roots.
    """
    buckets: dict[str, list[str]] = defaultdict(list)

    for raw in files:
        p = posix(raw)
        # find which routing root applies (None if file is not a page)
        rest: str | None = None
        for root in routing_roots:
            if p.startswith(root):
                rest = p[len(root):]
                break
        if rest is None:
            continue
        if not any(p.endswith(suf) for suf in page_suffixes):
            continue

        # Split rest into URL segments (directories) + filename. App
        # Router uses page.tsx / route.ts where the URL path is the
        # *directory* tree; Pages Router uses the filename itself
        # (``pages/dashboard.tsx`` → ``/dashboard``).
        if "/" in rest:
            url_path, fname = rest.rsplit("/", 1)
            dir_segments = url_path.split("/")
        else:
            # File is at the routing root (``pages/dashboard.tsx`` or
            # ``app/page.tsx``).
            fname, dir_segments = rest, []

        # Strip the page-suffix off the filename to get a stem.
        stem = fname
        for suf in page_suffixes:
            # ``suf`` may be ``/page.tsx`` (leading slash) or ``.tsx``
            # (extension only) — normalise by stripping the leading
            # slash before comparing.
            cmp = suf.lstrip("/")
            if stem.endswith(cmp):
                stem = stem[: -len(cmp)]
                break
        # If the filename stem is one of the convention "no-slug"
        # markers (``page``, ``route``, ``+page.svelte`` etc.), the
        # slug must come from the directory tree, not the filename.
        # Otherwise (Pages Router style ``dashboard.tsx``) the stem
        # itself is the slug source.
        stem = stem.rstrip(".")
        stem_is_marker = (not stem) or stem in {
            "page", "route", "layout", "index",
            "+page", "+server", "_app", "_document",
        }

        # Strip route groups from the directory segments.
        dir_segments = _strip_route_groups(dir_segments)

        if stem_is_marker:
            first = _first_meaningful_segment(dir_segments)
            if first is None:
                continue
            slug = slugify(first)
        else:
            # Pages Router top-level file: stem is the slug source.
            # Prepend a meaningful directory if one exists (so
            # ``pages/users/[id].tsx`` → ``users`` from the dir).
            first_dir = _first_meaningful_segment(dir_segments)
            slug_source = first_dir if first_dir else stem
            if is_noise(slug_source):
                continue
            slug = slugify(slug_source)
        if not slug:
            continue
        buckets[slug].append(p)

    return buckets


def _emit_for_python_routing(files: list[str]) -> dict[str, list[str]]:
    """Marker-file routing pass.

    For Python stacks the route table lives in ``urls.py`` / ``router*.py``.
    Each directory containing one of those markers becomes one anchor;
    the slug is the directory name (or the parent if the marker sits
    directly under ``src/`` / project root).
    """
    buckets: dict[str, list[str]] = defaultdict(list)

    for raw in files:
        p = posix(raw)
        fname = p.rsplit("/", 1)[-1]
        if fname not in _PYTHON_ROUTING_MARKERS:
            continue
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        if not parent:
            continue
        # walk up until we hit a non-noise segment
        segments = parent.split("/")
        slug_source = None
        for seg in reversed(segments):
            if seg and not is_noise(seg):
                slug_source = seg
                break
        if slug_source is None:
            continue
        slug = slugify(slug_source)
        if not slug:
            continue
        buckets[slug].append(p)

    return buckets


def _select_routing_for_stack(
    stack: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return the (routing_roots, page_suffixes) for a known FS-routing
    stack. ``None`` for stacks that don't follow file-system routing
    (Python backends use ``_emit_for_python_routing`` instead).
    """
    if not stack:
        return None
    return _STACK_ROUTING.get(stack)


class RouteFileExtractor:
    """File-system routing → anchor. See module docstring."""

    name = "route"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        files = list(ctx.tracked_files)
        buckets: dict[str, list[str]] = defaultdict(list)

        # Iterate stacks we should consider. Single-app: ``ctx.stack``
        # with ``prefix=""``. Monorepo: per-workspace stack with the
        # workspace path as the prefix, so the routing-root check below
        # can compare against ``apps/web/app/page.tsx`` correctly.
        considered: list[tuple[str | None, list[str], str]] = []
        if ctx.monorepo and ctx.workspaces:
            for ws in ctx.workspaces:
                ws_files = list(ws.files) if ws.files else []
                if ws_files:
                    ws_prefix = posix(ws.path).rstrip("/") + "/" if ws.path else ""
                    considered.append((ws.stack, ws_files, ws_prefix))
            # Also consider root-level files (single-package repos that
            # happen to live alongside a monorepo manifest).
            if ctx.stack:
                considered.append((ctx.stack, files, ""))
        else:
            considered.append((ctx.stack, files, ""))

        py_stacks = {"django", "fastapi", "flask", "starlette"}

        for stack, scoped_files, prefix in considered:
            fs_pattern = _select_routing_for_stack(stack)
            if fs_pattern is not None:
                roots, suffixes = fs_pattern
                # Prepend the workspace prefix to each routing root so
                # ``app/`` becomes ``apps/web/app/`` when scoped to a
                # monorepo workspace.
                rooted = tuple(prefix + r for r in roots) if prefix else roots
                for slug, paths in _emit_for_fs_routing(
                    scoped_files, rooted, suffixes,
                ).items():
                    buckets[slug].extend(paths)
            elif stack in py_stacks:
                for slug, paths in _emit_for_python_routing(scoped_files).items():
                    buckets[slug].extend(paths)

        out: list[AnchorCandidate] = []
        for slug, paths in buckets.items():
            unique_paths = tuple(sorted(set(paths)))
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=unique_paths,
                    source=self.name,
                    # Confidence scales weakly with evidence: more files
                    # under the route group → stronger anchor. Capped at
                    # 0.95 to leave headroom for multi-source agreement.
                    confidence_self=min(0.6 + 0.05 * len(unique_paths), 0.95),
                    rationale=f"route convention slug '{slug}' "
                              f"derived from {len(unique_paths)} routing file(s)",
                ),
            )
        return out


__all__ = ["RouteFileExtractor"]
