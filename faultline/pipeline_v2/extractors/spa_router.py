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
  * Seg D — B74 Seg A route tables (SEPARATE flag
    ``FAULTLINE_SPA_ROUTE_TABLE``, default OFF): exported enum /
    flat-const route tables (twenty ``AppPath``/``SettingsPath``, novu
    ``ROUTES``) whose members Seg B honestly SKIPS (``path={Table.X}``
    is not a literal). Candidacy is shape-only; emission requires
    path-position consumption in a router-marker file. Same source slug
    (``spa-page``), so every downstream fence covers the new rows.

Every route is emitted with ``method="PAGE"`` (the GET-equivalent client
surface, the same token the Next Pages extractor stamps), so downstream PAGE
consumers — ``spine_anchors`` ``page_route_files`` → the 6.86 mint
PAGE-SURFACE rule, journey seeding, the B65 partition ``no_product_surface``
prong, ``file_lane`` S3 surface guard — see SPA pages with zero new wiring.
``build_routes_index`` folds the candidates' explicit ``.routes`` AFTER all
existing sources and stamps ``kind="spa-page"`` (Seg C): an identical
``(pattern, method, file)`` triple emitted by an existing source wins and
stays byte-identical; spa rows only ADD.

Flag ``FAULTLINE_SPA_ROUTER_ENTRIES`` — default **ON** since the B65-v4
re-flip (2026-07-18, KEY_SCHEMA 31). Explicit ``0`` -> ``extract``
returns ``[]`` AND the registry does not even register the source
(``scan_meta.extractor_hits`` serializes every registered key — the
B67 kill-switch lesson), so the kill-switch scan is byte-identical to
pre-B65-v3; unset ≡ explicit ``1``.

Grammar vocabulary lives in ``stacks/spa-router.yaml`` (authoring mirror
``eval/stacks/spa-router.yaml``) — mechanisms, not per-repo dictionaries.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import os
import posixpath
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

#: B74 Seg A — SPA route-table extraction (default OFF). Sub-arm of the
#: spa-router family: arming it adds route-table candidates to the SAME
#: ``spa-page`` source (no new ``extractor_hits`` key), so every B65-v4
#: downstream fence (6.86 spa-page mint priority/floor/mass fence) covers
#: the new rows with zero new wiring. The family kill-switch
#: (``FAULTLINE_SPA_ROUTER_ENTRIES=0``) dominates: it unregisters the
#: whole extractor, table arm included.
SPA_ROUTE_TABLE_ENV = "FAULTLINE_SPA_ROUTE_TABLE"

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
    """Default **ON** since the 2026-07-18 B65-v4 re-flip (KEY_SCHEMA 31;
    full convoy2 on f38760e + operator panel PASS — R1 healed: mint
    priority/floor gated by the authored-subtree predicate, spa-born
    mass fence, member-twin bar, barrel-hop, template-literal honest
    skip). ``FAULTLINE_SPA_ROUTER_ENTRIES=0`` (or false/no/off) keeps
    the extractor inert (``extract`` -> ``[]``) AND unregistered (see
    :mod:`faultline.pipeline_v2.stage_1_extractors`), restoring the
    pre-B65-v3 scan byte-identically — explicit off stays a valid
    kill-switch forever; unset ≡ explicit ``1``."""
    return os.environ.get(SPA_ROUTER_ENTRIES_ENV, "1").strip().lower() not in {
        "", "0", "false", "no", "off",
    }


def spa_route_table_enabled() -> bool:
    """B74 Seg A — default **OFF**. ``FAULTLINE_SPA_ROUTE_TABLE=1`` arms
    the route-table arm (:func:`_collect_route_tables`); unset / falsy
    keeps it inert so the emitted candidate set — and therefore the scan
    — is byte-identical to the flag-less engine. Registered in
    ``scan_result_cache.ENV_OUTPUT_FLAGS`` (append-only, no KEY_SCHEMA
    bump)."""
    return os.environ.get(SPA_ROUTE_TABLE_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
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
) -> tuple[list[str], list[str]] | None:
    """``(before, rest)`` around the first pages-root component run
    (searched anywhere in the path — monorepo workspace prefixes are
    transparently stripped; same law as ``indexes._split_at_route_root``).
    ``before`` carries the enclosing-package segments for the slugless-
    page fallback below."""
    for i in range(len(segs)):
        for seq in roots:
            if segs[i:i + len(seq)] == list(seq):
                return segs[:i], segs[i + len(seq):]
    return None


def _enclosing_package_slug(before: list[str]) -> str:
    """Nearest non-noise path segment BEFORE the pages root — the
    enclosing package dir (``packages/hoppscotch-common/src/pages/…`` →
    ``hoppscotch-common``). The ``server_api_entries`` parent-dir
    precedent; ``""`` when nothing qualifies (single-app repo root)."""
    for seg in reversed(before):
        s = slugify(seg)
        if s and not is_noise(s):
            return s
    return ""


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
        split = _split_at_pages_root(p.split("/"), gr.roots)
        if split is None or not split[1]:
            continue
        before, rest = split
        stem = rest[-1][: -len(gr.suffix)]
        url_parts = list(rest[:-1])
        if stem not in gr.index_stems:
            url_parts.append(stem)
        pattern = "/" + "/".join(_vue_url_segment(s) for s in url_parts)
        slug = _first_static_segment(pattern)
        if not slug:
            # Grammar iter-3 (panel 2026-07-16): a ROOT index page
            # (``pages/index.vue`` → ``/`` — hoppscotch's flagship REST
            # page) and a noise-only static chain
            # (``view/_id/_version.vue`` → ``/view/:id/:version``) are
            # REAL product surfaces; their slug falls back to the
            # enclosing package segment (the server_api_entries
            # parent-dir precedent). Pure catch-alls (``_.vue`` →
            # ``/:catchAll``) still skip: not the root URL and no
            # static segment at all.
            has_static = any(
                s for s in pattern.split("/") if s and not s.startswith(":")
            )
            if pattern == "/" or has_static:
                slug = _enclosing_package_slug(before)
        if not slug:
            continue
        entries.append(_Entry(slug, p, "vue-pages", [_Route(pattern, "PAGE")]))
    return entries


# ── Seg B — react-router code config ─────────────────────────────────────────


@dataclass(frozen=True)
class _ReactRouterGrammar:
    dep_markers: tuple[str, ...]
    disqualify_deps_exact: tuple[str, ...]
    disqualify_dep_prefixes: tuple[str, ...]
    disqualify_config_files: tuple[str, ...]
    file_markers: tuple[str, ...]
    extensions: tuple[str, ...]
    local_alias_prefixes: tuple[str, ...]
    redirect_components: frozenset[str]
    component_suffix_strip: tuple[str, ...]


@lru_cache(maxsize=1)
def _rr_grammar() -> _ReactRouterGrammar | None:
    block = _cfg().get("react_router")
    if not isinstance(block, dict):
        return None

    def _tup(key: str) -> tuple[str, ...]:
        return tuple(str(s) for s in (block.get(key) or ()))

    return _ReactRouterGrammar(
        dep_markers=_tup("dep_markers"),
        disqualify_deps_exact=_tup("disqualify_deps_exact"),
        disqualify_dep_prefixes=_tup("disqualify_dep_prefixes"),
        disqualify_config_files=_tup("disqualify_config_files"),
        file_markers=_tup("file_markers"),
        extensions=_tup("extensions"),
        local_alias_prefixes=_tup("local_alias_prefixes"),
        redirect_components=frozenset(_tup("redirect_components")),
        component_suffix_strip=_tup("component_suffix_strip"),
    )


def _rr_active(ctx: "ScanContext", gr: _ReactRouterGrammar) -> bool:
    """Dep-corroborated activation with the framework disqualifiers."""
    manifests = _manifests(ctx)
    if not _has_dep(manifests, gr.dep_markers):
        return False
    if gr.disqualify_deps_exact and _has_dep(
        manifests, gr.disqualify_deps_exact,
    ):
        return False
    if gr.disqualify_dep_prefixes and _has_dep_prefix(
        manifests, gr.disqualify_dep_prefixes,
    ):
        return False
    for f in ctx.tracked_files:
        base = posix(f).rsplit("/", 1)[-1]
        if any(base.startswith(cf) for cf in gr.disqualify_config_files):
            return False
    return True


# ── Seg B imports / resolution ──


_IMPORT_DEFAULT_RE = re.compile(
    r"import\s+([A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\})?\s+from\s*"
    r"[\"'`]([^\"'`]+)[\"'`]",
)
_IMPORT_NAMED_RE = re.compile(
    r"import\s+(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^}]*)\}\s*from\s*"
    r"[\"'`]([^\"'`]+)[\"'`]",
)
_LAZY_BINDING_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:React\s*\.\s*)?lazy\s*"
    r"\(\s*\(\s*\)\s*=>\s*import\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*\)",
)
_LAZY_ROUTE_RE = re.compile(
    r"^\s*\(\s*\)\s*=>\s*import\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*\)",
)
_COMPONENT_IDENT_RE = re.compile(r"<\s*([A-Z][\w.]*)|Component\s*:\s*([A-Z][\w$]*)")
_TAG_TOKEN_RE = re.compile(r"</?\s*[A-Z][\w.]*|/>")

_ENTRY_EXTS = ("", ".tsx", ".ts", ".jsx", ".js",
               "/index.tsx", "/index.ts", "/index.jsx", "/index.js")


def _import_map(text: str) -> dict[str, str]:
    """Local component name → import specifier (default + named + lazy).

    Lazy bindings map the LOCAL name to the imported spec directly — the
    lazy target is the entry by the B37 bridge law."""
    out: dict[str, str] = {}
    for m in _IMPORT_DEFAULT_RE.finditer(text):
        out.setdefault(m.group(1), m.group(2))
    for m in _IMPORT_NAMED_RE.finditer(text):
        for piece in m.group(1).split(","):
            piece = piece.strip()
            if not piece or piece.startswith("type "):
                continue
            if " as " in piece:
                _orig, alias = (s.strip() for s in piece.split(" as ", 1))
            else:
                alias = piece
            if alias.isidentifier():
                out.setdefault(alias, m.group(2))
    for m in _LAZY_BINDING_RE.finditer(text):
        out[m.group(1)] = m.group(2)
    return out


def _resolve_spec(
    spec: str,
    config_file: str,
    tracked: tuple[str, ...],
    gr: _ReactRouterGrammar,
) -> str | None:
    """Resolve an import specifier to a tracked repo file, or ``None``.

    Relative specs resolve against the config file's directory (exact
    match + extension/index probing). Alias-prefixed specs (``@/x`` →
    tail ``x``) resolve by UNIQUE suffix-match against tracked files —
    ambiguity is an honest skip (no guessing across monorepo apps).
    Bare-package specs are external → ``None``."""
    tracked_set = frozenset(tracked)
    if spec.startswith("."):
        base_dir = config_file.rsplit("/", 1)[0] if "/" in config_file else ""
        base = posixpath.normpath(
            (base_dir + "/" if base_dir else "") + spec
        )
        if base.startswith(".."):
            return None
        for ext in _ENTRY_EXTS:
            cand = base + ext
            if cand in tracked_set:
                return cand
        return None

    tail: str | None = None
    for prefix in gr.local_alias_prefixes:
        if spec.startswith(prefix):
            tail = spec[len(prefix):]
            break
    if tail is None or not tail:
        return None  # bare package import — external, honest skip
    for ext in _ENTRY_EXTS:
        suffix = "/" + tail + ext
        hits = [f for f in tracked if f.endswith(suffix)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None  # ambiguous across workspaces — honest skip
    return None


#: One pure re-export statement — the only line class allowed in a
#: barrel (``export { X } from '...'`` / ``export { default } from`` /
#: ``export * from`` / ``export * as ns from``). Line-based on purpose:
#: a multi-line export or any local logic fails the predicate and the
#: file honestly stays the entry.
_REEXPORT_LINE_RE = re.compile(
    r"^\s*export\s+(?:\{[^}]*\}|\*(?:\s+as\s+[\w$]+)?)\s*from\s*"
    r"[\"'`]([^\"'`]+)[\"'`]\s*;?\s*$",
)

#: Barrel-chain bound (a barrel re-exporting another barrel) — purely a
#: cycle/pathology guard, not a tuning knob.
_MAX_BARREL_HOPS = 3


def _hop_reexport_barrel(
    entry: str,
    repo_root: Path,
    tracked: tuple[str, ...],
    gr: _ReactRouterGrammar,
) -> str:
    """Resolve THROUGH pure re-export barrels to the real page file.

    B65-v4 iter-2 (the Soc0 Ticketing 0-LOC husk root): the router
    element imports ``~/features/ticketing`` which resolves to the
    1-line barrel ``index.ts`` (``export { TicketingPage } from
    './TicketingPage'``). The barrel is module PLUMBING, never the page
    surface — stamping it as the entry mints a member-ful 0-LOC dev row
    (the LOC-doctrine violation) while the real page file carries the
    mass. STRUCTURAL predicate, no dictionaries: a file is a barrel iff
    EVERY non-empty, non-``//``-comment line is a re-export statement;
    it hops iff its re-exports resolve to exactly ONE distinct tracked
    file (a multi-target feature index is not a page echo — honest
    no-hop). Bounded chain, cycle-safe, deterministic."""
    seen = {entry}
    for _ in range(_MAX_BARREL_HOPS):
        text = read_text(repo_root / entry)
        if not text or len(text) > _MAX_BYTES:
            break
        specs: list[str] = []
        pure = True
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            m = _REEXPORT_LINE_RE.match(s)
            if m is None:
                pure = False
                break
            specs.append(m.group(1))
        if not pure or not specs:
            break
        targets = {
            r for spec in specs
            if (r := _resolve_spec(spec, entry, tracked, gr)) is not None
        }
        if len(targets) != 1:
            break  # unresolvable or multi-target barrel — honest no-hop
        nxt = next(iter(targets))
        if nxt in seen:
            break  # cycle guard
        seen.add(nxt)
        entry = nxt
    return entry


def _component_candidates(body: str) -> list[str]:
    """Capitalized component idents in an element body, innermost-last.

    JSX grammar puts wrappers first (``<TrialGuard><CasesPage/></...``), so
    candidates are returned in appearance order — callers walk them LAST-
    first to prefer the innermost (the real page; the same law as the
    profile-module ``_resolve_ident``)."""
    out: list[str] = []
    for m in _COMPONENT_IDENT_RE.finditer(body):
        name = m.group(1) or m.group(2)
        if name:
            out.append(name.split(".")[0])
    return out


@dataclass
class _RouteDecl:
    """One parsed route declaration (pre-resolution)."""

    pattern: str
    element_body: str      # raw element/Component window ("" when lazy-only)
    lazy_spec: str         # import spec of a route-level lazy, or ""


# ── Seg B grammar 1 — JSX <Route> trees ──


def _scan_string(text: str, i: int, quote: str) -> int:
    """Index just past the closing ``quote`` (no escape handling — route
    literals never embed escaped quotes; best-effort determinism)."""
    j = text.find(quote, i)
    return len(text) if j < 0 else j + 1


def _find_tag_end(text: str, start: int) -> tuple[int, bool]:
    """From just past ``<Route``, return ``(index past '>', self_closing)``.

    Tracks quote state and ``{}`` depth so a ``>`` inside a JSX-expression
    attribute (``element={<Home />}``) never terminates the tag."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            i = _scan_string(text, i + 1, ch)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == ">" and depth == 0:
            self_closing = text[i - 1] == "/"
            return i + 1, self_closing
        i += 1
    return n, True


_ATTR_PATH_RE = re.compile(r"\bpath\s*=\s*(?:[\"']([^\"']*)[\"']|\{)")
_ATTR_INDEX_RE = re.compile(r"\bindex\b(?!\s*=\s*\{?\s*false)")
_ATTR_ELEMENT_RE = re.compile(r"\b(?:element|Component)\s*=\s*\{")
_ATTR_LAZY_RE = re.compile(r"\blazy\s*=\s*\{")


def _balanced_brace_body(text: str, open_idx: int) -> str:
    """Body of the ``{...}`` opening at ``open_idx`` (index OF the brace)."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            i = _scan_string(text, i + 1, ch)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return text[open_idx + 1:]


def _parse_route_tag(tag_text: str) -> _RouteDecl | None:
    """Parse one ``<Route ...`` opening-tag body into a :class:`_RouteDecl`.

    Returns ``None`` for a pathless, non-index route (a layout shell)."""
    pm = _ATTR_PATH_RE.search(tag_text)
    pattern = ""
    if pm:
        if pm.group(1) is not None:
            if "${" in pm.group(1):
                return None  # interpolated — never a literal (B64 law)
            pattern = pm.group(1)
        else:
            # ``path={...}`` expression — a pure string literal inside the
            # braces folds; anything else is an honest skip (B64 law: a
            # non-literal path is never invented). iter-3 (the twenty
            # SettingsRoutes wave exhibit): an INTERPOLATED template
            # literal (``path={`${SettingsPath.GraphQLPlayground}`}``)
            # is NOT a literal — the raw ``/${…}`` garbage minted
            # parent-root settings anchors — so ``${`` rejects.
            body = _balanced_brace_body(tag_text, pm.end() - 1).strip()
            lm = re.match(r"^[\"'`]([^\"'`]*)[\"'`]$", body)
            if lm and "${" not in lm.group(1):
                pattern = lm.group(1)
            else:
                return None
    is_index = bool(_ATTR_INDEX_RE.search(tag_text)) if not pattern else False
    if not pattern and not is_index:
        return None

    element_body = ""
    em = _ATTR_ELEMENT_RE.search(tag_text)
    if em:
        element_body = _balanced_brace_body(tag_text, em.end() - 1)

    lazy_spec = ""
    lm2 = _ATTR_LAZY_RE.search(tag_text)
    if lm2:
        lazy_body = _balanced_brace_body(tag_text, lm2.end() - 1)
        am = _LAZY_ROUTE_RE.match(lazy_body.strip())
        if am:
            lazy_spec = am.group(1)

    if not element_body and not lazy_spec:
        return None
    return _RouteDecl(pattern, element_body, lazy_spec)


def _scan_jsx_routes(text: str) -> list[tuple[str, _RouteDecl]]:
    """(full URL pattern, decl) pairs from the ``<Route>`` tag tree.

    A tag stack joins nested relative paths; pathless layout wrappers
    contribute nothing to the URL but keep the stack balanced."""
    out: list[tuple[str, _RouteDecl]] = []
    stack: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            break
        if text.startswith("</Route", lt):
            if stack:
                stack.pop()
            i = lt + 7
            continue
        if not text.startswith("<Route", lt) or (
            lt + 6 < n and (text[lt + 6].isalnum() or text[lt + 6] in "_$.")
        ):
            i = lt + 1
            continue
        tag_end, self_closing = _find_tag_end(text, lt + 6)
        tag_text = text[lt + 6:tag_end]
        decl = _parse_route_tag(tag_text)
        parent = stack[-1] if stack else ""
        own_path = ""
        if decl is not None:
            full = _join_paths(parent, decl.pattern)
            out.append((full, decl))
            own_path = full
        else:
            # Layout shell — still consumes a stack slot; a literal path
            # on a shell (element-less path route) extends the chain.
            pm = _ATTR_PATH_RE.search(tag_text)
            if pm and pm.group(1) and "${" not in pm.group(1):
                own_path = _join_paths(parent, pm.group(1))
            else:
                own_path = parent
        if not self_closing:
            stack.append(own_path)
        i = tag_end
    return out


# ── Seg B grammar 2 — createXRouter object arrays ──


_CREATE_ROUTER_RE = re.compile(
    r"\b(createBrowserRouter|createHashRouter|createMemoryRouter)\s*\(",
)
_OBJ_PATH_RE = re.compile(r"^path\s*:\s*[\"'`]([^\"'`]*)[\"'`]")
_OBJ_INDEX_RE = re.compile(r"^index\s*:\s*true\b")
_OBJ_ELEMENT_RE = re.compile(r"^(?:element|Component)\s*:")
_OBJ_LAZY_RE = re.compile(r"^lazy\s*:")


def _balanced_paren_region(text: str, open_idx: int) -> str:
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            i = _scan_string(text, i + 1, ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return text[open_idx + 1:]


@dataclass
class _ObjFrame:
    path: str = ""
    index: bool = False
    element_window: str = ""
    lazy_spec: str = ""


def _scan_router_objects(text: str) -> list[tuple[str, _RouteDecl]]:
    """(full URL pattern, decl) pairs from ``createXRouter([...])`` arrays.

    A brace-frame stack mirrors the object nesting; ``children`` arrays
    inherit the ancestor frames' joined path chain."""
    out: list[tuple[str, _RouteDecl]] = []
    for cm in _CREATE_ROUTER_RE.finditer(text):
        region = _balanced_paren_region(text, cm.end() - 1)
        frames: list[_ObjFrame] = []
        i = 0
        n = len(region)
        while i < n:
            ch = region[i]
            if ch in "\"'`":
                # Peek object keys at frame level BEFORE consuming strings:
                # keys are unquoted in this grammar, so strings are opaque.
                i = _scan_string(region, i + 1, ch)
                continue
            if ch == "{":
                frames.append(_ObjFrame())
                i += 1
                continue
            if ch == "}":
                if frames:
                    fr = frames.pop()
                    if (fr.path or fr.index) and (
                        fr.element_window or fr.lazy_spec
                    ):
                        parent = ""
                        for anc in frames:
                            if anc.path:
                                parent = _join_paths(parent, anc.path)
                        full = _join_paths(parent, fr.path)
                        out.append((
                            full,
                            _RouteDecl(full, fr.element_window, fr.lazy_spec),
                        ))
                i += 1
                continue
            if frames and (i == 0 or not (
                region[i - 1].isalnum() or region[i - 1] in "_$."
            )):
                rest = region[i:]
                pm = _OBJ_PATH_RE.match(rest)
                if pm:
                    if "${" not in pm.group(1):
                        frames[-1].path = pm.group(1)
                    i += pm.end()
                    continue
                if _OBJ_INDEX_RE.match(rest):
                    frames[-1].index = True
                elif _OBJ_ELEMENT_RE.match(rest):
                    frames[-1].element_window = rest[:400]
                elif _OBJ_LAZY_RE.match(rest):
                    lm = _LAZY_ROUTE_RE.match(
                        rest.split(":", 1)[1] if ":" in rest else "",
                    )
                    if lm:
                        frames[-1].lazy_spec = lm.group(1)
            i += 1
    return out


# ── Seg B assembly ──


def _innermost_component(
    body: str, redirect: frozenset[str],
) -> tuple[str | None, bool]:
    """(innermost capitalized ident, is_redirect). Innermost = the LAST
    ident by JSX grammar (wrappers precede their children)."""
    cands = _component_candidates(body)
    if not cands:
        return None, False
    inner = cands[-1]
    return inner, inner in redirect


def _rr_component_slug(name: str, gr: _ReactRouterGrammar) -> str:
    """Component-name slug fallback (``HomePage`` → ``home``)."""
    stem = name
    changed = True
    while changed:
        changed = False
        for suf in gr.component_suffix_strip:
            if stem.endswith(suf) and len(stem) > len(suf):
                stem = stem[: -len(suf)]
                changed = True
                break
    slug = slugify(stem)
    return "" if not slug or is_noise(slug) else slug


def _collect_react_router(ctx: "ScanContext") -> list[_Entry]:
    gr = _rr_grammar()
    if gr is None or not gr.extensions:
        return []
    if not _rr_active(ctx, gr):
        return []

    tracked = tuple(sorted(posix(f) for f in ctx.tracked_files))
    exts = tuple(gr.extensions)
    entries: list[_Entry] = []

    for path in tracked:
        if not path.endswith(exts):
            continue
        if _should_skip_path(path):
            continue
        text = read_text(Path(ctx.repo_path) / path)
        if not text or len(text) > _MAX_BYTES:
            continue
        if not any(mk in text for mk in gr.file_markers):
            continue
        imports = _import_map(text)
        pairs = _scan_jsx_routes(text) + _scan_router_objects(text)
        for full, decl in pairs:
            entry_file = path  # honest fallback: the config file itself
            comp_name: str | None = None
            if decl.lazy_spec:
                resolved = _resolve_spec(decl.lazy_spec, path, tracked, gr)
                if resolved is not None:
                    entry_file = _hop_reexport_barrel(
                        resolved, Path(ctx.repo_path), tracked, gr)
            elif decl.element_body:
                comp_name, is_redirect = _innermost_component(
                    decl.element_body, gr.redirect_components,
                )
                if is_redirect:
                    continue  # a redirect is not a page surface
                for cand in reversed(
                    _component_candidates(decl.element_body)
                ):
                    spec = imports.get(cand)
                    if not spec:
                        continue
                    resolved = _resolve_spec(spec, path, tracked, gr)
                    if resolved is not None:
                        entry_file = _hop_reexport_barrel(
                            resolved, Path(ctx.repo_path), tracked, gr)
                        comp_name = cand
                        break
            slug = _first_static_segment(full)
            if not slug and comp_name:
                slug = _rr_component_slug(comp_name, gr)
            if not slug:
                continue  # all-dynamic path + mute component — honest skip
            entries.append(
                _Entry(slug, entry_file, "react-router", [_Route(full, "PAGE")]),
            )
    return entries


# ── Seg D (B74 Seg A) — SPA route-table extraction ───────────────────────────
#
# Exported enum / flat const-object route tables (twenty ``AppPath`` /
# ``SettingsPath``, novu dashboard ``ROUTES``) whose members are consumed
# by a router in path-position. Probe canon 2026-07-20 (SHIP/HIGH, ledger
# §ПРОБА B74 SEG A): candidacy = SHAPE (>=3 non-empty string values,
# >=0.8 route-like ratio; a leading ``/`` is a PRIOR, never a gate —
# twenty SettingsPath slash_ratio 0.0 is the consumption-primary case);
# the EMISSION gate is load-bearing: >=1 member consumed as ``path=`` /
# ``path:`` inside a router-marker file (corpus probe: 1,895 candidate
# tables -> exactly 3 emit, 0 false; DOCUMENTATION_PATHS / BACKGROUND
# asset packs / AppBasePath / redirect maps all die here). Values with a
# resolvable Route element take the page component's file as their entry
# (lazy / lazyWithPreload bridge through the consuming file's imports,
# wrapper + ``fallback={...}`` filtered); values without a direct Route
# (twenty tasks/opportunities nav-aliases) emit as pattern anchors on
# the table file itself — WITHOUT an owner (legal by spec).

#: Candidacy floor + route-like shape ratio (probe canon; scale-invariant
#: ratios, no per-repo tuning).
_RT_MIN_ENTRIES = 3
_RT_ROUTELIKE_RATIO = 0.8
_RT_MAX_VALUE_LEN = 120
#: Owner-walk window bounds (probe canon).
_RT_USE_WINDOW = 2500
_RT_ELEMENT_WINDOW = 400

_RT_ENUM_RE = re.compile(
    r"export\s+(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)\s*\{([^}]*)\}",
    re.DOTALL,
)
_RT_ENUM_MEMBER_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\s*=\s*(['\"`])((?:(?!\2).)*)\2",
)
_RT_CONST_OBJ_RE = re.compile(
    r"export\s+const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]{0,120}?)?=\s*"
    r"(?:Object\.freeze\s*\(\s*)?\{",
)
_RT_OBJ_MEMBER_RE = re.compile(
    r"(?:^|[,{]\s*)['\"]?([A-Za-z_$][\w$]*)['\"]?\s*:\s*"
    r"(['\"`])((?:(?!\2).)*)\2",
    re.DOTALL,
)
#: Route-like value: URL path characters only — no spaces, no scheme.
_RT_ROUTE_VAL_RE = re.compile(r"^[/A-Za-z0-9_\-:.*()\[\]#/]*$")
#: ``fallback={...}`` props are loaders, never owners (probe patch).
_RT_FALLBACK_PROP_RE = re.compile(r"fallback\s*=\s*\{[^}]*\}")
_RT_ELEMENT_RE = re.compile(r"element\s*[:=]\s*\{?")
_RT_COMPONENT_RE = re.compile(
    r"[Cc]omponent\s*[:=]\s*\{?\s*(?:<\s*)?([A-Za-z_$][\w$]*)",
)
_RT_JSX_IDENT_RE = re.compile(r"<\s*([A-Z][\w$]*)")


@dataclass(frozen=True)
class _RouteTableGrammar:
    extensions: tuple[str, ...]
    extra_skip_segments: frozenset[str]
    extra_skip_filename_markers: frozenset[str]
    router_import_re: "re.Pattern[str] | None"
    router_markers: tuple[str, ...]
    wrapper_components: frozenset[str]
    lazy_binding_re: "re.Pattern[str] | None"
    #: Thin :class:`_ReactRouterGrammar` carrying ONLY the fields the
    #: shared resolution helpers (:func:`_resolve_spec`,
    #: :func:`_hop_reexport_barrel`, :func:`_rr_component_slug`) read —
    #: alias prefixes + suffix strip. Reuse over reimplementation.
    resolve_gr: _ReactRouterGrammar


@lru_cache(maxsize=1)
def _rt_grammar() -> _RouteTableGrammar | None:
    block = _cfg().get("route_table")
    if not isinstance(block, dict):
        return None

    def _tup(key: str) -> tuple[str, ...]:
        return tuple(str(s) for s in (block.get(key) or ()))

    pkgs = _tup("router_import_packages")
    import_re = (
        re.compile(
            r"from\s+['\"]("
            + "|".join(re.escape(p) for p in pkgs)
            + r")['\"]",
        )
        if pkgs else None
    )
    callees = _tup("lazy_callees")
    lazy_re = (
        re.compile(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"(?:React\s*\.\s*)?(?:"
            + "|".join(re.escape(c) for c in callees)
            + r")\s*\(\s*\(\s*\)\s*=>\s*import\s*\(\s*"
            r"['\"`]([^'\"`]+)['\"`]",
        )
        if callees else None
    )
    return _RouteTableGrammar(
        extensions=_tup("extensions"),
        extra_skip_segments=frozenset(
            s.lower() for s in _tup("extra_skip_segments")
        ),
        extra_skip_filename_markers=frozenset(
            s.lower() for s in _tup("extra_skip_filename_markers")
        ),
        router_import_re=import_re,
        router_markers=_tup("router_markers"),
        wrapper_components=frozenset(_tup("wrapper_components")),
        lazy_binding_re=lazy_re,
        resolve_gr=_ReactRouterGrammar(
            dep_markers=(),
            disqualify_deps_exact=(),
            disqualify_dep_prefixes=(),
            disqualify_config_files=(),
            file_markers=(),
            extensions=(),
            local_alias_prefixes=_tup("local_alias_prefixes"),
            redirect_components=frozenset(),
            component_suffix_strip=_tup("component_suffix_strip"),
        ),
    )


@dataclass
class _RouteTable:
    """One candidate table: exported name + ``{member key: raw value}``."""

    name: str
    file: str
    entries: dict[str, str]


def _rt_route_like(value: str) -> bool:
    return bool(value) and len(value) <= _RT_MAX_VALUE_LEN and bool(
        _RT_ROUTE_VAL_RE.match(value)
    )


def _rt_should_skip(path: str, gr: _RouteTableGrammar) -> bool:
    """Shared artifact-class predicate + the route-table-ONLY extras
    (``__generated__`` dirs, ``.d.ts`` declaration files). The extras
    live in their own YAML keys so Seg A/B behaviour never shifts."""
    if _should_skip_path(path):
        return True
    p = posix(path).lower()
    segs = p.split("/")
    if any(seg in gr.extra_skip_segments for seg in segs[:-1]):
        return True
    dotparts = (segs[-1] if segs else "").split(".")
    return len(dotparts) >= 2 and any(
        comp in gr.extra_skip_filename_markers for comp in dotparts[1:-1]
    )


def _rt_find_tables(path: str, text: str) -> list[_RouteTable]:
    """Candidate tables in one file (shape gate only — no consumption)."""
    raw: list[tuple[str, dict[str, str]]] = []
    for em in _RT_ENUM_RE.finditer(text):
        raw.append((
            em.group(1),
            {
                mm.group(1): mm.group(3)
                for mm in _RT_ENUM_MEMBER_RE.finditer(em.group(2))
            },
        ))
    for cm in _RT_CONST_OBJ_RE.finditer(text):
        body = _balanced_brace_body(text, cm.end() - 1)
        if "{" in body:
            continue  # nested object — flat tables only (probe canon)
        raw.append((
            cm.group(1),
            {
                mm.group(1): mm.group(3)
                for mm in _RT_OBJ_MEMBER_RE.finditer(body)
            },
        ))
    out: list[_RouteTable] = []
    for name, entries in raw:
        vals = [v for v in entries.values() if v]
        if len(vals) < _RT_MIN_ENTRIES:
            continue
        routelike = [v for v in vals if _rt_route_like(v)]
        if len(routelike) / len(vals) < _RT_ROUTELIKE_RATIO:
            continue
        out.append(_RouteTable(name, path, entries))
    return out


def _rt_is_router_file(text: str, gr: _RouteTableGrammar) -> bool:
    """The consumption gate's file predicate: a router-package import OR
    any router-marker substring."""
    if gr.router_import_re is not None and gr.router_import_re.search(text):
        return True
    return any(mk in text for mk in gr.router_markers)


def _rt_use_re(name: str) -> "re.Pattern[str]":
    """``path= / path:`` member consumption — one regex serves both the
    emission gate (any match) and the owner walk (member capture)."""
    return re.compile(
        r"path\s*[:=]\s*\{?\s*" + re.escape(name) + r"\.([A-Za-z_$][\w$]*)",
    )


def _rt_scoped_tracked(
    consumer: str, tracked: tuple[str, ...],
) -> tuple[str, ...]:
    """Tracked files under the consumer's OWN package subtree.

    Alias prefixes (``@/`` / ``~/``) are per-app tsconfig conventions —
    resolving them against the WHOLE monorepo can suffix-match a
    foreign app's file (novu census exhibit: ``@/pages`` from
    ``apps/dashboard`` hit ``playground/nextjs/src/pages/index.tsx``).
    The package boundary = the longest ancestor dir of the consumer
    carrying a tracked ``package.json`` (pure mechanism — no path
    dictionary). Falls back to the full tracked set when the consumer
    sits outside any package dir (single-app repo root)."""
    tracked_set = frozenset(tracked)
    segs = consumer.split("/")[:-1]
    for i in range(len(segs), 0, -1):
        prefix = "/".join(segs[:i])
        if prefix + "/package.json" in tracked_set:
            return tuple(f for f in tracked if f.startswith(prefix + "/"))
    return tracked


def _rt_owner_map(
    text: str,
    consumer: str,
    table: _RouteTable,
    gr: _RouteTableGrammar,
    repo_root: Path,
    tracked: tuple[str, ...],
) -> dict[str, tuple[str, str]]:
    """``value -> (component, entry file)`` for ONE consuming router file.

    Probe-canon window walk: each ``path={Table.Member}`` match owns the
    window up to the next match (bounded); the owner is the first
    non-wrapper capitalized element after ``element=`` (``fallback={...}``
    loader props stripped first), else the ``component:`` ident. The
    entry file resolves through the consuming file's own imports (lazy /
    lazyWithPreload bridge first, then the barrel hop); an unresolvable
    spec or a same-file component honestly falls back to the consumer."""
    binding: dict[str, str] = dict(_import_map(text))
    if gr.lazy_binding_re is not None:
        for lm in gr.lazy_binding_re.finditer(text):
            binding[lm.group(1)] = lm.group(2)
    scoped = _rt_scoped_tracked(consumer, tracked)
    owners: dict[str, tuple[str, str]] = {}
    matches = list(_rt_use_re(table.name).finditer(text))
    for i, m in enumerate(matches):
        value = table.entries.get(m.group(1))
        if not value or value in owners:
            continue
        end = (
            matches[i + 1].start() if i + 1 < len(matches)
            else min(len(text), m.end() + _RT_USE_WINDOW)
        )
        window = text[m.end():end]
        comp: str | None = None
        em = _RT_ELEMENT_RE.search(window)
        if em:
            sub = _RT_FALLBACK_PROP_RE.sub(
                "", window[em.end():em.end() + _RT_ELEMENT_WINDOW],
            )
            for cand in _RT_JSX_IDENT_RE.findall(sub):
                if cand not in gr.wrapper_components:
                    comp = cand
                    break
        if comp is None:
            cm = _RT_COMPONENT_RE.search(window)
            if cm and cm.group(1) not in gr.wrapper_components:
                comp = cm.group(1)
        if comp is None:
            continue
        entry_file = consumer
        spec = binding.get(comp)
        if spec:
            # Package-scoped first (alias = per-app convention), full
            # tracked set as the fallback (Seg-B parity for consumers
            # outside any package dir).
            resolved = _resolve_spec(spec, consumer, scoped, gr.resolve_gr)
            if resolved is None and scoped is not tracked:
                resolved = _resolve_spec(
                    spec, consumer, tracked, gr.resolve_gr,
                )
            if resolved is not None:
                entry_file = _hop_reexport_barrel(
                    resolved, repo_root, scoped, gr.resolve_gr,
                )
        owners[value] = (comp, entry_file)
    return owners


def _collect_route_tables(ctx: "ScanContext") -> list[_Entry]:
    gr = _rt_grammar()
    if gr is None or not gr.extensions:
        return []
    repo_root = Path(ctx.repo_path)
    tracked = tuple(sorted(posix(f) for f in ctx.tracked_files))
    tables: list[_RouteTable] = []
    router_texts: dict[str, str] = {}
    for path in tracked:
        if not path.endswith(gr.extensions):
            continue
        if _rt_should_skip(path, gr):
            continue
        text = read_text(repo_root / path)
        if not text or len(text) > _MAX_BYTES:
            continue
        tables.extend(_rt_find_tables(path, text))
        if _rt_is_router_file(text, gr):
            router_texts[path] = text
    if not tables or not router_texts:
        return []

    entries: list[_Entry] = []
    for table in tables:
        consumed = False
        owners: dict[str, tuple[str, str]] = {}
        use_re = _rt_use_re(table.name)
        for consumer in sorted(router_texts):
            text = router_texts[consumer]
            if table.name not in text or not use_re.search(text):
                continue
            consumed = True
            for value, owner in _rt_owner_map(
                text, consumer, table, gr, repo_root, tracked,
            ).items():
                owners.setdefault(value, owner)
        if not consumed:
            continue  # the load-bearing emission gate (probe canon)
        for value in sorted(
            {v for v in table.entries.values() if _rt_route_like(v)}
        ):
            own = owners.get(value)
            comp = own[0] if own else None
            entry_file = own[1] if own else table.file
            slug = _first_static_segment(value)
            if not slug and comp:
                slug = _rr_component_slug(comp, gr.resolve_gr)
            if not slug:
                # Ownerless root ("/" nav-alias, twenty AppPath.Index):
                # the table NAME is the author's own namespace
                # declaration — its slug carries the pattern anchor
                # (noise-checked; the Seg-A enclosing-slug precedent).
                tslug = slugify(table.name)
                if tslug and not is_noise(tslug):
                    slug = tslug
            if not slug:
                continue  # all-dynamic pattern + no component — honest skip
            entries.append(
                _Entry(
                    slug, entry_file, "route-table", [_Route(value, "PAGE")],
                ),
            )
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
        entries.extend(_collect_react_router(ctx))
        if spa_route_table_enabled():
            # B74 Seg A — additive route-table arm on the SAME source
            # slug: unset/falsy keeps the emitted set byte-identical.
            entries.extend(_collect_route_tables(ctx))
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
    "SPA_ROUTE_TABLE_ENV",
    "SpaRouterExtractor",
    "spa_route_table_enabled",
    "spa_router_entries_enabled",
]
