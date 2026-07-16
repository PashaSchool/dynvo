"""SpaRouterExtractor — client-side SPA routes as Stage 1 entries (B65-v3).

Vue/React SPA repos declare their page routes either in the FILE TREE of a
non-framework convention (vite-plugin-pages ``pages/**/*.vue`` — invisible to
the filesystem RouteFileExtractor, whose grammars carry ts/tsx suffixes for
the ``vite`` stack and ``.vue`` only under the ``nuxt`` stack tag) or in CODE
(react-router ``<Route path=...>`` / ``createBrowserRouter``). Both shapes
never land in ``routes_index`` → their flows/journeys never mint, the B65
partition surface-detect sees no product surface, and whole boards stay empty
(B65-v2 root: hoppscotch routes_index=1 at 33 real vue pages; B58-v3 S1
total-extraction-miss class; B44 outline-empty-board class).

Segments (each a separate commit, ONE flag):
  * Seg A — vue file-based pages: ``pages/**/*.vue`` under a ``src/pages`` /
    ``pages`` root run (matched anywhere — monorepo prefixes transparent).
    Nuxt-style ``_param`` and bracket ``[param]`` segments are dynamics;
    ``index`` leaves take the directory URL. Corroboration: vue-router /
    vite-plugin-pages / unplugin-vue-router in any manifest's deps. Nuxt
    trees (a tracked ``nuxt.config.*`` / a ``nuxt``-tagged workspace) are
    self-skipped — the RouteFileExtractor ``nuxt`` grammar owns them.
  * Seg B — react-router code config: JSX ``<Route path=...>`` trees (nested
    relative paths joined through the tag stack) and ``createBrowserRouter``/
    ``createHashRouter``/``createMemoryRouter`` object arrays. The mounted
    element resolves through the file's own imports to its ENTRY file; a
    ``lazy(() => import(...))`` target IS the entry (the B37 lazy-dispatch
    bridge). Corroboration: react-router(-dom) dep. Next / Remix /
    react-router-framework repos disqualify (their pages are covered by
    filesystem extractors — SACRED no-dup law).

Every route is emitted with ``method="PAGE"`` (the GET-equivalent client
surface, the same token the Next Pages extractor stamps), so downstream PAGE
consumers — ``spine_anchors`` ``page_route_files`` → the 6.86 mint
PAGE-SURFACE rule, journey seeding, the B65 partition ``no_product_surface``
prong, ``file_lane`` S3 surface guard — see SPA pages with zero new wiring.
``build_routes_index`` folds the candidates' explicit ``.routes`` AFTER all
existing sources and stamps ``kind="spa-page"`` (Seg C): an identical
``(pattern, method, file)`` triple emitted by an existing source wins and
stays byte-identical; spa rows only ADD.

Flag ``FAULTLINE_SPA_ROUTER_ENTRIES`` — default OFF. Unset/``0`` ->
``extract`` returns ``[]`` AND the registry does not even register the
source (``scan_meta.extractor_hits`` serializes every registered key — the
B67 kill-switch lesson), so every scan is byte-identical to pre-B65-v3.

Grammar vocabulary lives in ``stacks/spa-router.yaml`` (authoring mirror
``eval/stacks/spa-router.yaml``) — mechanisms, not per-repo dictionaries.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_json,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


SPA_ROUTER_ENTRIES_ENV = "FAULTLINE_SPA_ROUTER_ENTRIES"

#: The single source slug on every emitted candidate (both segments share it,
#: so ``extractor_hits`` grows by exactly one key when the flag is ON).
SPA_PAGE_SOURCE = "spa-page"

#: ``routes_index`` entry kind stamped by ``build_routes_index`` Pass C.
SPA_PAGE_KIND = "spa-page"

#: Bounded per-file read — router/config modules are small; the cap only
#: guards pathological blobs (mirrors ``server_api_entries._MAX_BYTES``).
_MAX_BYTES = 1_500_000

#: Manifest read budget (mirrors the profile-module convention).
_MAX_MANIFEST_READS = 40


def spa_router_entries_enabled() -> bool:
    """``True`` when ``FAULTLINE_SPA_ROUTER_ENTRIES`` is set truthy (default OFF).

    Unset/``0`` keeps the extractor inert (``extract`` -> ``[]``) AND
    unregistered (see :mod:`faultline.pipeline_v2.stage_1_extractors`), so
    every scan is byte-identical to pre-B65-v3."""
    return os.environ.get(SPA_ROUTER_ENTRIES_ENV, "0").strip().lower() not in {
        "", "0", "false", "no", "off",
    }


# ── config ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cfg() -> dict:
    """Grammar vocabulary from ``stacks/spa-router.yaml`` (cached)."""
    return load_stack_yaml("spa-router")


def _confidence() -> float:
    return float(_cfg().get("confidence") or 0.8)


@lru_cache(maxsize=1)
def _skip_segments() -> frozenset[str]:
    return frozenset(
        str(s).lower() for s in (_cfg().get("skip_path_segments") or ())
    )


@lru_cache(maxsize=1)
def _skip_filename_markers() -> frozenset[str]:
    return frozenset(
        str(s).lower() for s in (_cfg().get("skip_filename_markers") or ())
    )


def _should_skip_path(path: str) -> bool:
    """``True`` for a test/mock/fixture file (shared predicate) OR an artifact
    class the predicate does not cover (storybook / examples / playground /
    demo / sample / generated). Segment match is EXACT — never a substring.
    Mirrors ``server_api_entries._should_skip_path`` (spec SACRED: stories /
    ``__tests__`` are never entries)."""
    p = posix(path).lower()
    if is_test_path(p):
        return True
    segs = p.split("/")
    if any(seg in _skip_segments() for seg in segs[:-1]):
        return True
    base = segs[-1] if segs else ""
    dotparts = base.split(".")
    if len(dotparts) >= 2 and any(
        comp in _skip_filename_markers() for comp in dotparts[1:-1]
    ):
        return True
    return False


# ── shared helpers ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Route:
    pattern: str
    method: str


@dataclass
class _Entry:
    """One detected page entry (pre-emission)."""

    slug: str
    file: str
    grammar: str
    routes: list[_Route] = field(default_factory=list)


def _manifests(ctx: "ScanContext") -> list[dict]:
    """Parsed package.json manifests: repo root + Stage-0 workspaces + any
    SHALLOW tracked ``package.json`` (depth <= 2) — covers polyglot repos
    whose frontend dir is not a detected workspace (Soc0 ``frontend/``).
    Bounded, deterministic (sorted tracked order)."""
    out: list[dict] = []
    root = read_json(Path(ctx.repo_path) / "package.json")
    if isinstance(root, dict):
        out.append(root)
    for ws in ctx.workspaces or []:
        if isinstance(ws.package_json, dict):
            out.append(ws.package_json)
    reads = 0
    for f in sorted(posix(x) for x in ctx.tracked_files):
        if reads >= _MAX_MANIFEST_READS:
            break
        if not f.endswith("/package.json") or f.count("/") > 2:
            continue
        if any(seg in _skip_segments() for seg in f.lower().split("/")[:-1]):
            continue
        doc = read_json(Path(ctx.repo_path) / f)
        reads += 1
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _dep_blocks(pkg: dict) -> list[dict]:
    out = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = pkg.get(key)
        if isinstance(block, dict):
            out.append(block)
    return out


def _has_dep(manifests: list[dict], markers: tuple[str, ...]) -> bool:
    """Exact dependency-key match in any manifest's dep block."""
    wanted = set(markers)
    for pkg in manifests:
        for block in _dep_blocks(pkg):
            if wanted & set(block):
                return True
    return False


def _has_dep_prefix(manifests: list[dict], prefixes: tuple[str, ...]) -> bool:
    for pkg in manifests:
        for block in _dep_blocks(pkg):
            for name in block:
                if any(str(name).startswith(p) for p in prefixes):
                    return True
    return False


def _first_static_segment(pattern: str) -> str:
    """Slug of the first static (non-param, non-noise) URL segment; ``""``
    when every segment is dynamic/noise (the honest route-extractor law)."""
    for raw in pattern.split("/"):
        seg = raw.strip()
        if not seg or seg.startswith((":", "*")) or seg == "*":
            continue
        slug = slugify(seg)
        if slug and not is_noise(slug):
            return slug
    return ""


def _join_paths(parent: str, child: str) -> str:
    """react-router path join: an absolute child replaces; a relative child
    appends. Collapses duplicate slashes; result always starts with ``/``."""
    child = child.strip()
    if child.startswith("/"):
        joined = child
    elif not child:
        joined = parent or "/"
    else:
        joined = (parent.rstrip("/") or "") + "/" + child
    segs = [s for s in joined.split("/") if s]
    return "/" + "/".join(segs)


# ── Seg A — vue file-based pages ─────────────────────────────────────────────


@dataclass(frozen=True)
class _VueGrammar:
    dep_markers: tuple[str, ...]
    roots: tuple[tuple[str, ...], ...]
    suffix: str
    index_stems: frozenset[str]


@lru_cache(maxsize=1)
def _vue_grammar() -> _VueGrammar | None:
    block = _cfg().get("vue")
    if not isinstance(block, dict):
        return None
    return _VueGrammar(
        dep_markers=tuple(str(m) for m in (block.get("dep_markers") or ())),
        roots=tuple(
            tuple(str(s) for s in seq) for seq in (block.get("roots") or ())
        ),
        suffix=str(block.get("suffix") or ".vue"),
        index_stems=frozenset(
            str(s) for s in (block.get("index_stems") or ())
        ),
    )


#: ``[id]`` / ``[...slug]`` / ``[[...slug]]`` bracket dynamics.
_BRACKET_DYNAMIC_RE = re.compile(r"^\[+\.{0,3}(?P<name>[^\]]*?)\]+$")


def _vue_url_segment(seg: str) -> str:
    """Map one on-disk segment to its URL form (dynamics → ``:name``)."""
    m = _BRACKET_DYNAMIC_RE.match(seg)
    if m:
        return ":" + ((m.group("name") or "").lstrip(".") or "param")
    if seg == "_":
        return ":catchAll"  # nuxt-style bare-underscore catch-all
    if seg.startswith("_") and len(seg) > 1:
        return ":" + seg[1:]  # nuxt-style dynamic (_id → :id)
    return seg


def _split_at_pages_root(
    segs: list[str], roots: tuple[tuple[str, ...], ...],
) -> list[str] | None:
    """Segments AFTER the first pages-root component run (searched anywhere
    in the path — monorepo workspace prefixes are transparently stripped;
    same law as ``indexes._split_at_route_root``)."""
    for i in range(len(segs)):
        for seq in roots:
            if segs[i:i + len(seq)] == list(seq):
                return segs[i + len(seq):]
    return None


def _nuxt_prefixes(ctx: "ScanContext") -> tuple[str, ...]:
    """Path prefixes owned by Nuxt (a tracked ``nuxt.config.*`` dir, a
    ``nuxt``-tagged workspace, or the whole repo when Stage 0 / the auditor
    tagged it ``nuxt``). Pages under these are ALREADY covered by the
    RouteFileExtractor ``nuxt`` grammar — emitting them here would double
    (SACRED no-dup law)."""
    roots: set[str] = set()
    for f in ctx.tracked_files:
        p = posix(f)
        base = p.rsplit("/", 1)[-1]
        if base.startswith("nuxt.config."):
            d = p.rsplit("/", 1)[0] if "/" in p else ""
            roots.add(d + "/" if d else "")
    for ws in ctx.workspaces or []:
        if (ws.stack or "").lower() == "nuxt":
            roots.add(posix(ws.path).rstrip("/") + "/")
    for tag in (ctx.stack, ctx.audited_stack):
        if (tag or "").lower() == "nuxt":
            roots.add("")
    return tuple(sorted(roots))


def _collect_vue_pages(ctx: "ScanContext") -> list[_Entry]:
    gr = _vue_grammar()
    if gr is None or not gr.roots:
        return []
    if not _has_dep(_manifests(ctx), gr.dep_markers):
        return []  # mechanism-activation corroboration, not a name dictionary
    nuxt_prefixes = _nuxt_prefixes(ctx)

    entries: list[_Entry] = []
    for raw in ctx.tracked_files:
        p = posix(raw)
        if not p.endswith(gr.suffix):
            continue
        if any(p.startswith(pre) for pre in nuxt_prefixes):
            continue
        if _should_skip_path(p):
            continue
        rest = _split_at_pages_root(p.split("/"), gr.roots)
        if not rest:
            continue
        stem = rest[-1][: -len(gr.suffix)]
        url_parts = list(rest[:-1])
        if stem not in gr.index_stems:
            url_parts.append(stem)
        pattern = "/" + "/".join(_vue_url_segment(s) for s in url_parts)
        slug = _first_static_segment(pattern)
        if not slug:
            # Root index / pure-dynamic page (``pages/index.vue``,
            # ``pages/_.vue``) — no static segment to anchor on. Honest
            # skip, the same law the stock route extractor applies.
            continue
        entries.append(_Entry(slug, p, "vue-pages", [_Route(pattern, "PAGE")]))
    return entries


# ── extractor ────────────────────────────────────────────────────────────────


class SpaRouterExtractor:
    """Client-side SPA page routes -> routes_index entries (B65-v3)."""

    name = SPA_PAGE_SOURCE

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not spa_router_entries_enabled():
            return []
        entries: list[_Entry] = []
        entries.extend(_collect_vue_pages(ctx))
        return _emit(entries)


def _emit(entries: list[_Entry]) -> list[AnchorCandidate]:
    """Group by (file, slug), union+dedup routes, emit one candidate per
    group — the B66 emission law (sorted, deterministic)."""
    conf = _confidence()
    grouped: dict[tuple[str, str], _Entry] = {}
    for e in entries:
        if not e.slug or not e.routes:
            continue
        key = (e.file, e.slug)
        if key in grouped:
            grouped[key].routes.extend(e.routes)
        else:
            grouped[key] = _Entry(e.slug, e.file, e.grammar, list(e.routes))

    out: list[AnchorCandidate] = []
    for key in sorted(grouped):
        e = grouped[key]
        seen: dict[tuple[str, str], _Route] = {}
        for r in e.routes:
            seen.setdefault((r.pattern, r.method), r)
        routes = tuple(
            (r.pattern, r.method, e.file) for _, r in sorted(seen.items())
        )
        out.append(
            AnchorCandidate(
                name=e.slug,
                paths=(e.file,),
                source=SPA_PAGE_SOURCE,
                confidence_self=conf,
                routes=routes,
                rationale=(
                    f"{e.grammar} spa page {e.slug!r} "
                    f"({len(routes)} route(s)) in {e.file}"
                ),
            ),
        )
    return out


__all__ = [
    "SPA_PAGE_KIND",
    "SPA_PAGE_SOURCE",
    "SPA_ROUTER_ENTRIES_ENV",
    "SpaRouterExtractor",
    "spa_router_entries_enabled",
]
