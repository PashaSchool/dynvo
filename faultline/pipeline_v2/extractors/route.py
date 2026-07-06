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
workspace stack when monorepo). The routing-convention tables live in
``eval/stacks/filesystem-routing.yaml`` (authoring copy) with the
runtime copy packaged at ``faultline/pipeline_v2/data/stacks/`` —
per the ``stack-pattern-library`` skill, conventions live in YAML,
never hardcoded in Python.

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

from faultline.analyzer.validation import is_test_file
from faultline.pipeline_v2.data import load_stack_yaml
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
# Loaded from ``stacks/filesystem-routing.yaml`` in the packaged data
# tree (authoring copy: ``eval/stacks/filesystem-routing.yaml``):
#
#   stack → (routing_roots, page_suffixes)
#     - routing_roots : every match must start with one of these
#                       (workspace prefix prepended for monorepos).
#     - page_suffixes : file must end with one of these to count as
#                       a page/handler.
#
#   python_routing_markers — marker filenames for Python web frameworks
#   where routing lives in marker files, not file-system convention.
#
# All paths are POSIX. Tests build synthetic repos in ``tmp_path``
# without git, so no normalisation other than slash flipping needed.

_RoutingTables = tuple[
    dict[str, tuple[tuple[str, ...], tuple[str, ...]]],  # stack routing
    tuple[str, ...],                                     # python markers
]

_ROUTING_CACHE: _RoutingTables | None = None


def _load_routing_tables() -> _RoutingTables:
    """Parse filesystem-routing.yaml once into the historical tuple shapes.

    Hermetic: resolves via ``importlib.resources`` (see
    ``faultline.pipeline_v2.data``). A missing data file raises — a
    packaging bug, never a silently-tolerated condition.
    """
    global _ROUTING_CACHE
    if _ROUTING_CACHE is not None:
        return _ROUTING_CACHE

    config = load_stack_yaml("filesystem-routing")
    stack_routing: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for stack, entry in (config.get("stacks") or {}).items():
        if not isinstance(entry, dict):
            continue
        roots = tuple(str(r) for r in (entry.get("roots") or []))
        suffixes = tuple(str(s) for s in (entry.get("suffixes") or []))
        if roots and suffixes:
            stack_routing[str(stack)] = (roots, suffixes)
    markers = tuple(
        str(m) for m in (config.get("python_routing_markers") or [])
    )
    _ROUTING_CACHE = (stack_routing, markers)
    return _ROUTING_CACHE


def _strip_route_groups(segments: list[str]) -> list[str]:
    """Drop Next.js route-group segments like ``(marketing)``.

    Route groups are organisational only — they do not appear in URLs
    and must not become anchor names. Their NAMES are still carried as
    anchor metadata (see :func:`route_groups_of`) — Product-Spine Wave 2a
    stopped discarding the author's surface declaration (rootcause RC4).
    """
    return [s for s in segments if not (s.startswith("(") and s.endswith(")"))]


def route_groups_of(path: str) -> tuple[str, ...]:
    """Route-group names present in *path*, sorted + deduped.

    ``apps/web/app/(marketing)/pricing/page.tsx`` → ``("marketing",)``.
    Intercepting-route markers (``(.)folder`` / ``(..)folder``) are not
    groups and are skipped. Lowercased — group names are surface labels,
    not identifiers.
    """
    groups: set[str] = set()
    for seg in posix(path).split("/"):
        if (
            seg.startswith("(")
            and seg.endswith(")")
            and len(seg) > 2
            and not seg[1:-1].startswith(".")
        ):
            groups.add(seg[1:-1].lower())
    return tuple(sorted(groups))


def _first_meaningful_segment(segments: list[str]) -> str | None:
    """Return the first segment that is not noise / not a route group.

    Used to derive an anchor slug from a routing path. Dynamic segments
    like ``[id]`` / ``[...slug]`` (Next) or ``$id`` / ``$`` (Remix /
    React Router) are skipped — they describe params, not features.
    Remix-flat-routes folder groups carry a trailing ``+``
    (``admin+/``) which is stripped before the checks; segments with a
    leading underscore (``_authenticated``) are pathless layouts in
    Remix / React Router and private (non-routed) folders in Next App
    Router — organisational only, never a feature name.
    """
    for seg in segments:
        # remix-flat-routes folder convention: ``admin+/`` routes as
        # ``admin`` — the ``+`` only marks "this folder is a flat-route
        # group".
        seg = seg.rstrip("+")
        if not seg:
            continue
        # Dynamic segments are not features.
        if seg.startswith("[") and seg.endswith("]"):
            continue
        if seg.startswith("$"):
            continue
        if seg.startswith("(") and seg.endswith(")"):
            continue
        # Pathless layout (Remix ``_auth``) / private folder (Next
        # ``_components``) — organisational, not part of the URL.
        if seg.startswith("_"):
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
            "_index", "_layout", "_route",
        }

        # Capture original (pre-strip) segments so we can recover a
        # slug from a leaf route-group like ``(home)`` when nothing
        # else survives stripping. Next.js route groups are
        # organisational but their NAME is meaningful — ``(home)``
        # exists precisely so the maintainer can label the top-level
        # home page tree. Without this fallback the root-level route
        # group's ``page.tsx`` would never produce a feature anchor.
        original_segments = list(dir_segments)
        # Strip route groups from the directory segments.
        dir_segments = _strip_route_groups(dir_segments)

        if stem_is_marker:
            first = _first_meaningful_segment(dir_segments)
            if first is None:
                # Sprint D3 — try recovering a slug from a route-group
                # segment when stripping removed everything. The group
                # token has the form ``(name)``; we use the inner name.
                for seg in original_segments:
                    if (
                        seg.startswith("(")
                        and seg.endswith(")")
                        and len(seg) > 2
                    ):
                        candidate = slugify(seg[1:-1])
                        if candidate and not is_noise(candidate):
                            first = candidate
                            break
                if first is None:
                    continue
            slug = slugify(first)
        else:
            # Pages Router top-level file: stem is the slug source.
            # Prepend a meaningful directory if one exists (so
            # ``pages/users/[id].tsx`` → ``users`` from the dir).
            #
            # Remix / React Router flat routes encode nested URL
            # segments in the FILENAME with dots
            # (``users.$id.edit.tsx`` → ``/users/:id/edit``) — split
            # the stem and pick the first meaningful sub-segment so
            # the param/pathless parts don't pollute the slug.
            slug_source: str | None = _first_meaningful_segment(dir_segments)
            if slug_source is None:
                stem_segments = [s for s in stem.split(".") if s]
                slug_source = _first_meaningful_segment(stem_segments)
            if slug_source is None or is_noise(slug_source):
                continue
            slug = slugify(slug_source)
        if not slug:
            continue
        buckets[slug].append(p)

    return buckets


def _emit_for_python_routing(files: list[str]) -> dict[str, list[str]]:
    """Marker-file routing pass.

    For Python stacks the route table lives in ``urls.py`` / ``router*.py``,
    OR in any ``.py`` file under a directory literally named ``routers/``
    (the dominant FastAPI convention — see Sprint S7-A).

    Each routing module becomes one anchor:
      - Marker files (``urls.py`` / ``router(s).py`` / ``routes.py``):
        slug is the enclosing directory name (e.g. ``api/v1/urls.py`` →
        ``v1``).
      - ``routers/<resource>.py``: slug is the file STEM (the resource
        name itself, e.g. ``routers/findings.py`` → ``findings``). This
        mirrors how FastAPI projects organise: each file = one
        sub-router = one user-facing surface.
    """
    buckets: dict[str, list[str]] = defaultdict(list)

    for raw in files:
        p = posix(raw)
        # Skip non-Python files cheaply.
        if not p.endswith(".py"):
            continue
        fname = p.rsplit("/", 1)[-1]
        parent = p.rsplit("/", 1)[0] if "/" in p else ""

        if fname in _load_routing_tables()[1]:
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
            continue

        # Sprint S7-A — FastAPI ``routers/<resource>.py`` convention.
        # Universal across FastAPI tutorials, FastAPI Best Practices repo,
        # and most production FastAPI projects (verified on Soc0/backend
        # where 17 router files under ``routers/`` produced 0 anchors
        # because none were named literally ``router.py``).
        #
        # We accept BOTH ``routers/`` and ``api/routers/`` etc. (any
        # path containing a ``routers/`` directory segment). The file
        # stem becomes the slug.
        if "/routers/" in p or p.startswith("routers/"):
            if fname == "__init__.py":
                continue
            stem = fname[:-3]  # strip .py
            if not stem or is_noise(stem):
                continue
            slug = slugify(stem)
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
    return _load_routing_tables()[0].get(stack)


class RouteFileExtractor:
    """File-system routing → anchor. See module docstring."""

    name = "route"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        # Test files never declare product routes — a test file matching a
        # routing convention (``backend/tests/routers/test_admin.py`` hits
        # the FastAPI ``routers/<resource>.py`` rule; ``pages/foo.test.tsx``
        # hits a Pages-Router suffix) must not become a feature anchor.
        # Without this guard every such file minted a per-file ``test-*``
        # feature whose pytest functions Stage 3 then promoted to flows
        # (the Soc0 15%%-test-flow / test-polluted-UF bug). Same structural
        # exclusion the fastapi-route extractor (YAML ``excludes``) and the
        # django / fastapi_family profile boundary indexes already apply.
        files = [f for f in ctx.tracked_files if not is_test_file(posix(f))]
        buckets: dict[str, list[str]] = defaultdict(list)

        # Iterate stacks we should consider. Single-app: ``ctx.stack``
        # with ``prefix=""``. Monorepo: per-workspace stack with the
        # workspace path as the prefix, so the routing-root check below
        # can compare against ``apps/web/app/page.tsx`` correctly.
        considered: list[tuple[str | None, list[str], str]] = []
        if ctx.monorepo and ctx.workspaces:
            for ws in ctx.workspaces:
                ws_files = [
                    f for f in (ws.files or []) if not is_test_file(posix(f))
                ]
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
            # Product-Spine Wave 2a (RC4): carry the route-group names
            # observed on this anchor's paths as metadata. The slug/paths
            # semantics above are UNCHANGED — groups stay URL-invisible.
            groups: set[str] = set()
            for p in unique_paths:
                groups.update(route_groups_of(p))
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
                    route_groups=tuple(sorted(groups)),
                ),
            )
        return out


__all__ = ["RouteFileExtractor"]
