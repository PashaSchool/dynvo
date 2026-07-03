"""Next.js Pages Router + classic React SPA :class:`FrameworkProfile`
(Phase B #3 — the last Tier-1 profile).

The engine's deep, deterministic understanding of how a Next.js PAGES
Router app or a classic React single-page app (react-router library
mode / Vite / CRA) assembles files into user-facing capabilities.
Encodes the *framework convention* (valid for ANY such repo), never a
corpus repo's paths — see CLAUDE.md ``rule-no-repo-specific-paths`` /
``rule-no-magic-tuning``.

Why one profile for both shapes: they are the same product-app grammar
— a tree of page components mounted by a client router — differing only
in WHERE the route table lives (the ``pages/`` filesystem vs
``<Route>`` elements / ``createBrowserRouter`` objects). Splitting them
would duplicate every colocation/attribution rule.

Structural model encoded (framework docs, never a repo README):

  * **Next Pages Router** — routes are files under ``pages/`` or
    ``src/pages/`` (the routing root sits at a package root or under
    ``src/``; a directory merely *named* ``pages`` elsewhere is not a
    router). ``pages/api/**`` files are API endpoints. ``_app`` /
    ``_document`` / ``_error`` / ``404`` / ``500`` are the app SHELL —
    per-page scaffold the framework mounts around every route; they are
    never a capability of their own. The Next ``pageExtensions``
    convention (``*.page.tsx``) marks routed files with a ``page``
    dot-token that is NOT part of the URL.
  * **App Router precedence** — Next itself prefers ``app/`` when both
    trees exist; this profile therefore NEVER claims a repo that shows
    a real (non-example) App Router tree or an App-Router-tagged
    workspace. Mixed repos belong to the ``next-app-router`` profile.
  * **react-router (library mode)** — the SPA declares its route table
    in source: ``<Routes>`` / ``<Route path=...>`` elements or
    ``createBrowserRouter``/``useRoutes`` route objects. The component
    each route mounts (resolved through the router file's OWN imports,
    including ``lazy(() => import(...))``) is the real entry point; the
    router file itself is the SHELL. Framework mode (``@react-router/*``
    packages / ``react-router.config.*``) is the Remix successor with
    ``app/routes/**`` file routing — a DIFFERENT stack, explicitly not
    claimed here.
  * **Vite / CRA render entry** — a plain React SPA is rooted at an
    ``index.html`` host page plus a ``src/index.*``/``src/main.*``
    module calling ``ReactDOM.createRoot``/``render``. Weak evidence on
    its own (no router grammar), graded below every workspace-tag grade.
  * **Capability unit** — the first meaningful URL segment: the page
    file's top-level ``pages/`` segment (or its filename stem), or the
    route literal's first static segment for router-element SPAs.
    Colocated ``components/`` / ``hooks/`` / ``lib/`` primitives are
    genuinely cross-cutting and fan out (blast radius), never collapse
    into one blob.

Alignment contract (same as the Next-App/FastAPI/Django profiles):
``feature_of`` returns the SAME kebab slug the profile's Stage-1
extractors emit — the pages claims are literally the inverse of the
route extractor's own bucket computation (shared code path), and the
SPA claims mirror :class:`ReactRouterSpaExtractor` — because Stage 2
re-homes a path only onto a feature whose name already exists.

Activation fold (Phase B): the Pages-Router / SPA conventions in
``extractors/route.py`` are pure YAML data (``filesystem-routing.yaml``
entries for ``next-pages`` / ``vite`` / ``tanstack-router``) with NO
stack-literal gate in Python — there is no G3 allowlist row to delete
for this stack (the single ``route.py`` row, ``elif stack in
py_stacks:``, belongs to the Python marker-file frameworks). What the
profile folds is the ACTIVATION HOLE: keyless scans of mis-tagged
repos (``js-generic`` monorepo roots) never reach those YAML rows, so
:meth:`NextPagesReactProfile.stage_1_extractor_overrides` supplies a
route-extractor instance that ALWAYS runs the pages pass on every
pages root in the tree — the reused stock parsing plus the app-shell
rule — and a new router-element extractor for the SPA shape.

Deterministic — NO LLM, NO network. Universal — no corpus paths; the
structural caps are justified inline.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_json,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.extractors.route import (
    RouteFileExtractor,
    _emit_for_fs_routing,
    _load_routing_tables,
)
from faultline.pipeline_v2.profiles._splitter import split_workspaces
from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# ── framework fingerprints (framework constants, not tuned numbers) ─────────

#: The stack tag Stage 0 / the auditor emit for a Pages-Router repo.
#: NOT trusted bare (the litestar/flake8-django lesson): the tag grade
#: additionally requires a structural fingerprint before it fires.
_PAGES_STACK_TAGS = frozenset({"next-pages"})

#: Workspace tags that mark a repo as Next-era. ANY App-Router-tagged
#: workspace hands the repo to the ``next-app-router`` profile (Next's
#: own precedence when both trees coexist), and ANY Next-tagged
#: workspace disqualifies the SPA grades (an embedded ``react-router``
#: widget inside a Next product does not make the repo a React SPA).
_NEXT_APP_TAG = "next-app-router"
_NEXT_TAGS = frozenset({"next-app-router", "next-pages", "next"})

#: Root-level manifest filenames that declare a NON-JS primary shape.
#: A React-SPA / Pages-Router *product repo* is rooted at a JS manifest;
#: a server-language manifest at the repo ROOT (traefik's ``go.mod``,
#: a Django ``manage.py``) means the React tree is an embedded frontend
#: of that backend's product — its profile (or the default) owns the
#: repo, never this one. Ecosystem manifest names, not corpus paths.
_NON_JS_ROOT_MANIFESTS = frozenset({
    "go.mod", "cargo.toml", "pyproject.toml", "setup.py", "setup.cfg",
    "pipfile", "manage.py", "requirements.txt", "gemfile", "pom.xml",
    "build.gradle", "build.gradle.kts", "composer.json", "mix.exs",
})

#: App-shell filename stems (first dot-token). Next mounts these around
#: every page — framework wiring, never a capability. ``404``/``500``
#: are the convention error pages.
_SHELL_STEMS = frozenset({"_app", "_document", "_error", "404", "500"})

#: Filename stems that qualify a ``pages/`` tree as a REAL Pages Router
#: (detection only): the shell files and the root index page. A bare
#: folder of page-like components without any of these is not enough
#: evidence to claim the repo (structural confirmation, the litestar
#: lesson).
_PAGES_MARKER_STEMS = frozenset({"_app", "_document", "index"})

#: JS/TS source extensions for router files / page entries.
_JS_EXTS = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs")

#: Path segments that never host routing evidence: vendored trees,
#: docs/example scaffolding, build output, tests. Ecosystem names, not
#: corpus paths. ``example`` matches as a PREFIX (``examples/``,
#: ``example-apps/`` — the ecosystem uses both spellings).
_EXCLUDED_SEGMENTS = frozenset({
    "node_modules", "dist", "build", "out", ".next",
    "docs", "doc", "samples", "sample", "fixtures",
    "__tests__", "test", "tests", "e2e", "cypress", "playwright",
    "storybook", ".storybook",
})
_EXCLUDED_SEGMENT_PREFIXES = ("example",)

#: react-router LIBRARY-mode grammar — the route table declared in
#: source. Framework mode (``@react-router/*`` / ``react-router.config``)
#: is excluded at the fingerprint level, so a hit here is a genuine SPA.
_ROUTER_GRAMMAR_RE = re.compile(
    r"<Routes\b|<Route[\s>]|createBrowserRouter\s*\(|createHashRouter\s*\("
    r"|createMemoryRouter\s*\(|<RouterProvider\b|useRoutes\s*\(",
)

#: ReactDOM render entry (Vite / CRA SPA host module).
_RENDER_ENTRY_RE = re.compile(
    r"\bcreateRoot\s*\(|\bhydrateRoot\s*\(|ReactDOM\.render\s*\(",
)
_REACT_DOM_IMPORT_RE = re.compile(r"""from\s+["']react-dom""")

#: JSX ``<Route ...`` attribute window: react-router puts the mounted
#: element INSIDE the tag (``element={<X/>}``), so the tag text can
#: contain ``>`` before the tag ends — a fixed window after the tag
#: start is inspected instead of brittle bracket matching. A route
#: declaration (path + element/component props) fits comfortably in a
#: few hundred characters; this is an I/O window, not an accuracy knob.
_ROUTE_TAG_RE = re.compile(r"<Route[\s\n]")
_ROUTE_WINDOW = 600
_PATH_ATTR_RE = re.compile(r"""\bpath\s*=\s*\{?["'`]([^"'`]+)["'`]\}?""")
_PATH_KEY_RE = re.compile(r"""\bpath\s*:\s*["'`]([^"'`]+)["'`]""")
_ELEMENT_ATTR_RE = re.compile(
    r"\b(?:element|component|Component)\s*[=:]\s*\{?",
)
_IDENT_RE = re.compile(r"\b([A-Z][A-Za-z0-9_$]*)\b")

#: ES import grammar a router file uses to reach its page components.
_IMPORT_DEFAULT_RE = re.compile(
    r"""(?m)^\s*import\s+([A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\})?\s+from\s+["']([^"']+)["']""",
)
_IMPORT_NAMED_RE = re.compile(
    r"""(?ms)^\s*import\s+(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^}]*)\}\s*from\s+["']([^"']+)["']""",
)
#: Lazy component binding — ANY wrapper around a dynamic import counts
#: (``lazy``, ``React.lazy``, project ``lazyWithRetry`` wrappers):
#: resolution is the filter, no wrapper name-list needed.
_LAZY_IMPORT_RE = re.compile(
    r"""(?ms)\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=[^;]{0,200}?import\(\s*["']([^"']+)["']\s*\)""",
)

#: Structural role directory tables — identical vocabulary to the
#: next_app_router profile (the ecosystem-standard placeholder names).
_COMPONENT_DIRS = frozenset({"components", "ui", "primitives"})
_HOOK_DIRS = frozenset({"hooks"})
_LIB_DIRS = frozenset({"lib", "libs", "utils", "util", "helpers"})
_SERVICE_DIRS = frozenset({"services", "server", "data", "queries", "store",
                           "redux", "contexts", "context"})
_DOMAIN_DIRS = frozenset({"models", "schemas", "domain", "entities",
                          "constant", "constants"})
_TEST_MARKERS = (".test.", ".spec.", "/__tests__/", "/tests/", "/e2e/")

#: Few-owners-is-signal fan-out cap for genuinely shared files — the
#: same scale-invariant policy (and constant) as the other profiles.
_SHARED_FANOUT_CAP = 3

#: Bounded-scan caps — I/O safety valves for huge repos, not accuracy
#: knobs (candidate lists are priority-ordered so evidence is found in
#: the first few files). Same values as the FastAPI/Django profiles.
_MAX_MANIFEST_READS = 100
_MAX_SOURCE_READS = 400
#: A capitalized token inside a route-element window that resolves via
#: the file's imports is the mounted component; at most this many
#: tokens are tried per window (the component reference sits at the
#: front of the prop value by grammar). CPU valve, not an accuracy knob.
_MAX_TOKEN_TRIES = 8


# ── small helpers ────────────────────────────────────────────────────────────


def _segments(path: str) -> tuple[list[str], str]:
    p = posix(path)
    if "/" in p:
        head, fname = p.rsplit("/", 1)
        return head.split("/"), fname
    return [], p


def _is_excluded_path(path: str) -> bool:
    segs, _fname = _segments(posix(path).lower())
    for seg in segs:
        if seg in _EXCLUDED_SEGMENTS:
            return True
        if seg.startswith(_EXCLUDED_SEGMENT_PREFIXES):
            return True
    return False


def _is_test_path(path: str) -> bool:
    return any(m in posix(path).lower() for m in _TEST_MARKERS)


def _first_dot_token(fname: str) -> str:
    return fname.split(".", 1)[0]


def _is_js_source(path: str) -> bool:
    return posix(path).lower().endswith(_JS_EXTS)


def _pages_suffixes() -> tuple[str, ...]:
    """The Pages-Router page suffixes from the packaged stack YAML —
    single source of truth shared with the stock route extractor."""
    return _load_routing_tables()[0]["next-pages"][1]


def _candidate_order(files: list[str]) -> list[str]:
    """Router-shaped files first so bounded scans find evidence fast."""
    def _key(f: str) -> tuple[int, str]:
        _segs, fname = _segments(posix(f))
        stem = _first_dot_token(fname).lower()
        if stem in ("app", "router", "routes", "routing"):
            rank = 0
        elif stem in ("main", "index"):
            rank = 1
        else:
            rank = 2
        return (rank, f)

    return sorted(files, key=_key)


# ── pages-root discovery + bucket index ──────────────────────────────────────


def _pages_roots(tracked: list[str]) -> list[str]:
    """Every accepted Pages-Router routing root in the tree (sorted).

    A root is a ``pages`` directory segment whose parent is a package
    root — structurally: the prefix before ``pages/`` is empty, ends in
    ``src``, or hosts a tracked ``package.json`` (Next only routes
    ``<root>/pages`` / ``<root>/src/pages``; a directory merely NAMED
    ``pages`` deeper in the source tree — ``lib/pages/`` — is not a
    router). Excluded/vendored segments never host roots.
    """
    tracked_set = frozenset(tracked)
    roots: set[str] = set()
    for f in tracked:
        p = posix(f)
        if not _is_js_source(p) or _is_excluded_path(p):
            continue
        segs, _fname = _segments(p)
        for i, seg in enumerate(segs):
            if seg != "pages":
                continue
            prefix = "/".join(segs[:i])
            ok = (
                not prefix
                or segs[i - 1] == "src"
                or f"{prefix}/package.json" in tracked_set
            )
            if ok:
                roots.add((prefix + "/" if prefix else "") + "pages/")
            break  # only the first ``pages`` segment can be the router
    return sorted(roots)


class _PagesIndex:
    """Deterministic index of the Pages-Router surface.

    ``roots`` — accepted routing roots; ``buckets`` — slug → sorted
    routing files (the EXACT computation the profile's route extractor
    emits, so ``feature_of`` aligns byte-for-byte); ``owned`` — routing
    file → slug; shell files are indexed separately (never owned, never
    a capability).
    """

    def __init__(self, ctx: "ScanContext") -> None:
        tracked = [posix(f) for f in ctx.tracked_files]
        self.roots: tuple[str, ...] = tuple(_pages_roots(tracked))
        suffixes = _pages_suffixes()

        self.shell_files: set[str] = set()
        routable: list[str] = []
        for f in tracked:
            root = self._root_of(f)
            if root is None:
                continue
            if not f.endswith(suffixes):
                continue
            _segs, fname = _segments(f)
            if _first_dot_token(fname) in _SHELL_STEMS:
                self.shell_files.add(f)
                continue
            routable.append(f)

        raw = _emit_for_fs_routing(routable, self.roots, suffixes)
        self.buckets: dict[str, tuple[str, ...]] = {
            slug: tuple(sorted(set(paths)))
            for slug, paths in sorted(raw.items())
        }
        self.owned: dict[str, str] = {
            p: slug for slug, paths in self.buckets.items() for p in paths
        }
        self.routable: tuple[str, ...] = tuple(sorted(routable))

    def _root_of(self, path: str) -> str | None:
        for root in self.roots:
            if path.startswith(root):
                return root
        return None

    def rest_of(self, path: str) -> str | None:
        """Path relative to its routing root, or ``None``."""
        root = self._root_of(path)
        return path[len(root):] if root else None

    def marker_roots(self) -> list[str]:
        """Roots showing a Pages-Router MARKER file (detection grade).

        Excluded/vendored roots never qualify; a root whose package
        also hosts a real App Router tree is disqualified (Next's own
        app-over-pages precedence — those repos belong to the
        ``next-app-router`` profile)."""
        qualified: list[str] = []
        for root in self.roots:
            if _is_excluded_path(root + "x"):
                continue
            for f in list(self.shell_files) + list(self.routable):
                if not f.startswith(root):
                    continue
                _segs, fname = _segments(f)
                if _first_dot_token(fname) in _PAGES_MARKER_STEMS:
                    qualified.append(root)
                    break
        return qualified


def _app_router_marker_dirs(tracked: list[str]) -> set[str]:
    """Package prefixes showing a REAL (non-excluded) App Router tree."""
    out: set[str] = set()
    for f in tracked:
        p = posix(f)
        if _is_excluded_path(p):
            continue
        segs, fname = _segments(p)
        stem = _first_dot_token(fname)
        ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""
        if stem not in ("page", "route") or ext not in (".tsx", ".ts",
                                                        ".jsx", ".js"):
            continue
        for i, seg in enumerate(segs):
            if seg == "app":
                prefix = "/".join(segs[:i])
                if prefix.endswith("/src") or prefix == "src":
                    prefix = prefix[:-4].rstrip("/")
                out.add(prefix)
                break
    return out


# ── react-router SPA index ───────────────────────────────────────────────────


def _import_map(text: str) -> dict[str, str]:
    """Component ident → module specifier from a router file's imports."""
    out: dict[str, str] = {}
    for m in _IMPORT_DEFAULT_RE.finditer(text):
        out[m.group(1)] = m.group(2)
    for m in _IMPORT_NAMED_RE.finditer(text):
        spec = m.group(2)
        for raw in m.group(1).split(","):
            raw = raw.strip()
            if not raw:
                continue
            if " as " in raw:
                _orig, alias = (s.strip() for s in raw.split(" as ", 1))
            else:
                alias = raw
            if alias.isidentifier():
                out[alias] = spec
    for m in _LAZY_IMPORT_RE.finditer(text):
        out[m.group(1)] = m.group(2)
    return out


def _resolve_spec(
    spec: str, from_file: str, tracked_set: frozenset[str],
) -> str | None:
    """Resolve a module specifier to a tracked source file.

    Relative specifiers resolve against the importing file's directory;
    the ``@/`` and ``~/`` aliases resolve against the package's ``src/``
    root (the Vite/CRA/Next jsconfig convention). Bare package imports
    return ``None`` (external dependency, not a page).
    """
    segs, _fname = _segments(from_file)
    base: list[str]
    if spec.startswith("."):
        base = list(segs)
        parts = spec.split("/")
        for part in parts:
            if part in ("", "."):
                continue
            if part == "..":
                if base:
                    base.pop()
            else:
                base.append(part)
    elif spec.startswith(("@/", "~/")):
        # Walk up to the nearest ``src`` segment of the importing file.
        if "src" not in segs:
            return None
        src_idx = len(segs) - 1 - segs[::-1].index("src")
        base = segs[: src_idx + 1] + [
            p for p in spec[2:].split("/") if p and p != "."
        ]
    else:
        return None
    stem = "/".join(base)
    for ext in _JS_EXTS:
        if f"{stem}{ext}" in tracked_set:
            return f"{stem}{ext}"
    for ext in _JS_EXTS:
        if f"{stem}/index{ext}" in tracked_set:
            return f"{stem}/index{ext}"
    return None


def _route_slug(path_literal: str) -> str:
    """First meaningful static segment of a route path literal."""
    for seg in path_literal.split("/"):
        seg = seg.strip()
        if not seg or seg.startswith((":", "*")) or seg == "*":
            continue
        slug = slugify(seg)
        if slug and not is_noise(slug):
            return slug
    return ""


class _RouterIndex:
    """Deterministic index of react-router route declarations.

    ``entries`` — one :class:`FlowEntry` per (component file, symbol,
    route); ``owned`` — resolved component file → branch slug;
    ``buckets`` — slug → sorted component files (what
    :class:`ReactRouterSpaExtractor` emits). Router files themselves
    are the app SHELL (root router) — indexed but never owned.
    """

    def __init__(self, ctx: "ScanContext") -> None:
        tracked = [posix(f) for f in ctx.tracked_files]
        tracked_set = frozenset(tracked)
        candidates = [
            f for f in tracked
            if _is_js_source(f)
            and not _is_excluded_path(f)
            and not _is_test_path(f)
        ]

        self.entries: list[FlowEntry] = []
        self.router_files: set[str] = set()
        buckets: dict[str, set[str]] = defaultdict(set)
        owned: dict[str, str] = {}
        seen: set[tuple[str, str, str]] = set()

        reads = 0
        for rel in _candidate_order(candidates):
            if reads >= _MAX_SOURCE_READS:
                break
            text = read_text(ctx.repo_path / rel)
            reads += 1
            if not text or not _ROUTER_GRAMMAR_RE.search(text):
                continue
            self.router_files.add(rel)
            imports = _import_map(text)

            for path_literal, ident in self._route_pairs(text):
                resolved = self._resolve_ident(
                    ident, imports, rel, tracked_set,
                ) if ident else None
                slug = _route_slug(path_literal)
                if not slug and resolved is not None:
                    _segs, fname = _segments(resolved)
                    slug = slugify(_first_dot_token(fname))
                if not slug or is_noise(slug):
                    continue
                entry_file = resolved if resolved is not None else rel
                route = path_literal if path_literal.startswith("/") else (
                    "/" + path_literal
                )
                key = (entry_file, ident, route)
                if key in seen:
                    continue
                seen.add(key)
                self.entries.append(FlowEntry(
                    path=entry_file, symbol=ident, kind="page", route=route,
                ))
                if resolved is not None:
                    buckets[slug].add(resolved)
                    owned.setdefault(resolved, slug)

        self.buckets: dict[str, tuple[str, ...]] = {
            slug: tuple(sorted(paths))
            for slug, paths in sorted(buckets.items())
        }
        self.owned: dict[str, str] = owned

    @staticmethod
    def _route_pairs(text: str) -> list[tuple[str, str]]:
        """(path literal, component ident) pairs declared in ``text``.

        Two grammars: JSX ``<Route path=... element={...}>`` (a bounded
        window after each tag start — the element prop nests JSX, so
        bracket matching is brittle) and route OBJECTS
        (``{ path: "x", element: <X/> }``) for the
        ``createBrowserRouter`` / ``useRoutes`` style.
        """
        pairs: list[tuple[str, str]] = []

        def _component_in(window: str) -> str:
            m = _ELEMENT_ATTR_RE.search(window)
            if not m:
                return ""
            blob = window[m.end(): m.end() + 200]
            # The element value ends at the JSX self-close — anything
            # beyond belongs to a sibling attribute / the next tag.
            close = blob.find("/>")
            if close != -1:
                blob = blob[:close]
            # The LAST resolvable-looking ident wins downstream; here we
            # keep every capitalized ident (wrapper elements come first
            # by JSX grammar: ``<Wrapper Page={RealPage} />``) joined so
            # the resolver can try them in reverse order.
            idents = _IDENT_RE.findall(blob)
            return " ".join(idents[:_MAX_TOKEN_TRIES])

        # One window per tag, bounded by the NEXT tag start so a
        # pathless layout route can never steal its first child's path
        # and a tag's element can never bleed into a sibling's.
        starts = [m.start() for m in _ROUTE_TAG_RE.finditer(text)]
        for i, s in enumerate(starts):
            end = s + _ROUTE_WINDOW
            if i + 1 < len(starts):
                end = min(end, starts[i + 1])
            window = text[s:end]
            pm = _PATH_ATTR_RE.search(window)
            if not pm:
                continue  # pathless layout route — shell, not an entry
            pairs.append((pm.group(1), _component_in(window)))

        if re.search(
            r"createBrowserRouter\s*\(|createHashRouter\s*\("
            r"|createMemoryRouter\s*\(|useRoutes\s*\(",
            text,
        ):
            key_matches = list(_PATH_KEY_RE.finditer(text))
            for i, pm in enumerate(key_matches):
                end = pm.end() + _ROUTE_WINDOW
                if i + 1 < len(key_matches):
                    end = min(end, key_matches[i + 1].start())
                pairs.append((pm.group(1), _component_in(text[pm.end():end])))

        return pairs

    @staticmethod
    def _resolve_ident(
        idents: str,
        imports: dict[str, str],
        rel: str,
        tracked_set: frozenset[str],
    ) -> str | None:
        """Resolve the mounted component to a tracked file.

        Tries the window's capitalized idents LAST-first: by JSX grammar
        the wrapping element name precedes its props, so the innermost
        (real) page reference comes last (``<Lazy Page={AddAdmin}/>`` →
        try ``AddAdmin`` before ``Lazy``). Resolution through the
        file's own imports is the filter — an ident that is not
        imported (a prop name like ``Page``) resolves to nothing and is
        skipped.
        """
        for ident in reversed(idents.split()):
            spec = imports.get(ident)
            if not spec:
                continue
            resolved = _resolve_spec(spec, rel, tracked_set)
            if resolved is not None:
                return resolved
        return None


# ── profile-supplied Stage-1 extractors ──────────────────────────────────────


class _ProfileActivatedPagesRouteExtractor(RouteFileExtractor):
    """The reused route extractor + an unconditional pages pass.

    The profile has already established this IS a Pages-Router / React
    SPA repo, so the stock stack-tag activation (which never fires on a
    ``js-generic``-mis-tagged keyless scan) is supplemented: every
    accepted pages root in the tree gets the stock
    ``_emit_for_fs_routing`` pass with the ``next-pages`` conventions —
    the PARSING is reused byte-for-byte. The app-shell rule is applied
    on top: ``_app``/``_document``/``_error``/``404``/``500`` files are
    stripped from every bucket (they are framework wiring, and the
    stock pass would otherwise mint a bogus ``404`` capability).
    """

    def __init__(self, pages_index_of) -> None:  # noqa: ANN001 — profile memo hook
        super().__init__()
        self._pages_index_of = pages_index_of

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        index: _PagesIndex = self._pages_index_of(ctx)
        buckets: dict[str, set[str]] = defaultdict(set)
        for a in super().extract(ctx):
            buckets[a.name].update(a.paths)
        for slug, paths in index.buckets.items():
            buckets[slug].update(paths)

        shell = index.shell_files
        out: list[AnchorCandidate] = []
        for slug in sorted(buckets):
            paths = tuple(sorted(p for p in buckets[slug] if p not in shell))
            if not paths:
                continue
            out.append(AnchorCandidate(
                name=slug,
                paths=paths,
                source=self.name,
                confidence_self=min(0.6 + 0.05 * len(paths), 0.95),
                rationale=(
                    f"route convention slug '{slug}' derived from "
                    f"{len(paths)} routing file(s)"
                ),
            ))
        return out


class ReactRouterSpaExtractor:
    """One anchor per react-router route-tree branch.

    Implements the Stage-1 ``AnchorExtractor`` Protocol. Emits, for
    every top-level route branch (the route literal's first static
    segment), an :class:`AnchorCandidate` whose ``paths`` are the
    resolved mounted-component files — the SPA analogue of a
    filesystem-routing anchor. The router file itself (the root shell)
    is never a path.

    Lives in the profile module (stack knowledge stays in the profile);
    reaches Stage 1 exclusively via the profile's extractor overrides,
    so it can never fire on a repo the profile did not win.
    """

    name = "react-router-spa"

    def __init__(self, router_index_of) -> None:  # noqa: ANN001 — profile memo hook
        self._router_index_of = router_index_of

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        index: _RouterIndex = self._router_index_of(ctx)
        out: list[AnchorCandidate] = []
        for slug, paths in index.buckets.items():
            if not slug or not paths:
                continue
            out.append(AnchorCandidate(
                name=slug,
                paths=paths,
                source=self.name,
                confidence_self=min(0.6 + 0.05 * len(paths), 0.95),
                rationale=(
                    f"react-router branch '/{slug}' mounts "
                    f"{len(paths)} component file(s)"
                ),
            ))
        return out


# ── the profile ──────────────────────────────────────────────────────────────


class NextPagesReactProfile:
    """Framework Knowledge Layer for Next Pages Router + classic React SPA."""

    name = "next-pages-react"

    def __init__(self) -> None:
        # Single-slot memos (pure w.r.t. ctx; instance lives one scan).
        self._pages_key: tuple[int, int] | None = None
        self._pages_index: _PagesIndex | None = None
        self._router_key: tuple[int, int] | None = None
        self._router_index: _RouterIndex | None = None

    # ── detection ───────────────────────────────────────────────────────────

    def detects(self, ctx: "ScanContext") -> float:
        """Confidence that this is a Pages-Router / React-SPA repo.

        Graded by signal strength (never a repo-tuned constant):

          * 0.95 — Stage 0 / the auditor tagged ``next-pages`` AND a
                   structural fingerprint confirms it (bare tags are
                   not trusted — the litestar mis-tag lesson).
          * 0.6 + 0.35·fraction — monorepo whose workspaces carry the
                   ``next-pages`` tag, and NO workspace is App Router
                   (Next's own app-over-pages precedence: mixed repos
                   belong to ``next-app-router``, which must win on its
                   own score).
          * 0.9  — structural Pages Router: a ``next`` dependency in a
                   tracked ``package.json`` AND a marker-qualified
                   ``pages/`` root, AND no real App Router tree
                   anywhere (outside excluded/example scaffolding).
          * 0.85 — react-router LIBRARY-mode SPA: the dep + route-table
                   grammar in source (structural confirmation for the
                   bare dep). Framework mode (``@react-router/*`` /
                   ``react-router.config.*``) is a different stack and
                   never fires this.
          * 0.55 — Vite/CRA render entry (``index.html`` host + a
                   ``src/index|main`` module calling ReactDOM render +
                   a react dep). DELIBERATELY below the 0.6 workspace
                   floor of every framework-tag grade: this weakest
                   fingerprint exists only to claim plain SPAs nothing
                   else recognises, never to outscore a real framework
                   signal.
          * 0.0  — otherwise (never wins; G4 inertness holds).

        Both SPA grades additionally require (a) a JS root manifest —
        a ``go.mod``/``pyproject.toml``-rooted repo with an embedded
        React frontend belongs to its backend's profile (or default) —
        and (b) zero Next-tagged workspaces / a non-Next repo tag.
        """
        tag = (ctx.audited_stack or ctx.stack or "").lower()
        pages = self._pages(ctx)
        marker_roots = pages.marker_roots()
        has_next_dep = self._has_next_dep(ctx)

        if tag in _PAGES_STACK_TAGS and (marker_roots or has_next_dep):
            return 0.95

        # The remaining grades are independent fingerprints — the
        # STRONGEST applicable one wins (a monorepo whose workspaces
        # carry the tag AND whose tree shows the full structural
        # fingerprint scores the structural grade, not the weaker
        # workspace-fraction floor).
        best = 0.0

        wss = ctx.workspaces or []
        ws_tags = [(ws.stack or "").lower() for ws in wss]
        if wss and not any(t == _NEXT_APP_TAG for t in ws_tags):
            tagged = sum(1 for t in ws_tags if t in _PAGES_STACK_TAGS)
            if tagged:
                best = min(0.6 + 0.35 * (tagged / len(wss)), 0.95)

        tracked = [posix(f) for f in ctx.tracked_files]
        if not self._js_root(tracked):
            return best

        if (
            marker_roots
            and has_next_dep
            and not _app_router_marker_dirs(tracked)
        ):
            return max(best, 0.9)

        # ── SPA grades ──
        if tag.startswith("next") or any(t in _NEXT_TAGS for t in ws_tags):
            return best

        if self._react_router_library_dep(ctx) and self._router_grammar(ctx):
            return max(best, 0.85)

        if self._render_entry(ctx, tracked):
            return max(best, 0.55)

        return best

    # ── detection fingerprints ────────────────────────────────────────────────

    @staticmethod
    def _js_root(tracked: list[str]) -> bool:
        """No server-language manifest at the repo ROOT."""
        root_files = {f.lower() for f in tracked if "/" not in f}
        return not (root_files & _NON_JS_ROOT_MANIFESTS)

    @staticmethod
    def _iter_manifests(ctx: "ScanContext") -> list[str]:
        out = [
            posix(f) for f in ctx.tracked_files
            if posix(f).rsplit("/", 1)[-1] == "package.json"
            and not _is_excluded_path(posix(f))
        ]
        return sorted(out)

    @classmethod
    def _dep_in_manifests(cls, ctx: "ScanContext", *names: str) -> bool:
        """Exact dependency-key match in any tracked package.json."""
        wanted = set(names)
        reads = 0
        for rel in cls._iter_manifests(ctx):
            if reads >= _MAX_MANIFEST_READS:
                break
            doc = read_json(ctx.repo_path / rel)
            reads += 1
            if not isinstance(doc, dict):
                continue
            for key in ("dependencies", "peerDependencies"):
                block = doc.get(key)
                if isinstance(block, dict) and wanted & set(block):
                    return True
        return False

    @classmethod
    def _has_next_dep(cls, ctx: "ScanContext") -> bool:
        return cls._dep_in_manifests(ctx, "next")

    @classmethod
    def _react_router_library_dep(cls, ctx: "ScanContext") -> bool:
        """Library-mode react-router dep; framework mode disqualifies.

        Framework mode = ``@react-router/*`` packages (runtime OR dev)
        or a ``react-router.config.*`` file — the Remix-successor
        file-routing stack, not a classic SPA.
        """
        has_lib = False
        reads = 0
        for rel in cls._iter_manifests(ctx):
            if reads >= _MAX_MANIFEST_READS:
                break
            doc = read_json(ctx.repo_path / rel)
            reads += 1
            if not isinstance(doc, dict):
                continue
            for key in ("dependencies", "peerDependencies",
                        "devDependencies"):
                block = doc.get(key)
                if not isinstance(block, dict):
                    continue
                if any(d.startswith("@react-router/") for d in block):
                    return False
                if key != "devDependencies" and (
                    "react-router-dom" in block or "react-router" in block
                ):
                    has_lib = True
        if not has_lib:
            return False
        for f in ctx.tracked_files:
            fname = posix(f).rsplit("/", 1)[-1]
            if fname.startswith("react-router.config."):
                return False
        return True

    def _router_grammar(self, ctx: "ScanContext") -> bool:
        """Route-table grammar in tracked source (bounded, ordered)."""
        return bool(self._router(ctx).router_files)

    @staticmethod
    def _render_entry(ctx: "ScanContext", tracked: list[str]) -> bool:
        """Vite/CRA React host: index.html + ReactDOM render module."""
        has_bundler = any(
            posix(f).rsplit("/", 1)[-1].startswith("vite.config.")
            for f in tracked
        ) or NextPagesReactProfile._dep_in_manifests(ctx, "react-scripts")
        if not has_bundler:
            return False
        if not any(
            posix(f).rsplit("/", 1)[-1] == "index.html"
            and not _is_excluded_path(posix(f))
            for f in tracked
        ):
            return False
        reads = 0
        for f in sorted(tracked):
            p = posix(f)
            segs, fname = _segments(p)
            if not segs or segs[-1] != "src":
                continue
            if _first_dot_token(fname) not in ("index", "main"):
                continue
            if not _is_js_source(p) or _is_excluded_path(p):
                continue
            text = read_text(ctx.repo_path / p)
            reads += 1
            if text and _RENDER_ENTRY_RE.search(text) and (
                _REACT_DOM_IMPORT_RE.search(text)
            ):
                return True
            if reads >= _MAX_SOURCE_READS:
                break
        return False

    # ── workspaces ───────────────────────────────────────────────────────────

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """Pure delegation to the shared package-manager splitter."""
        return split_workspaces(ctx)

    # ── file classification ───────────────────────────────────────────────────

    def classify_file(self, path: str) -> FileRole:
        """Map a repo-relative path to its structural role."""
        p = posix(path)
        segs, fname = _segments(p)
        if _is_test_path(p):
            return FileRole.TEST
        low = fname.lower()
        if low.startswith(("next.config.", "vite.config.")) or (
            ".config." in low
        ):
            return FileRole.CONFIG

        # Pages-Router tree: shell → CONFIG; api → API; else PAGE.
        if "pages" in segs:
            idx = segs.index("pages")
            if _first_dot_token(fname) in _SHELL_STEMS:
                return FileRole.CONFIG
            if "api" in segs[idx + 1:]:
                return FileRole.API
            if _is_js_source(p):
                return FileRole.PAGE

        seg_set = {s.lower() for s in segs}
        if seg_set & _COMPONENT_DIRS:
            return FileRole.COMPONENT
        if seg_set & _HOOK_DIRS:
            return FileRole.HOOK
        if seg_set & _DOMAIN_DIRS:
            return FileRole.DOMAIN
        if seg_set & _SERVICE_DIRS:
            return FileRole.SERVICE
        if seg_set & _LIB_DIRS:
            return FileRole.LIB
        return FileRole.UNKNOWN

    # ── feature attribution ────────────────────────────────────────────────────

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:
        """The capability this file serves, or ``None`` (shared/shell).

        Pages-Router files return the slug of their routing bucket —
        byte-identical to the profile's route-extractor anchors (shared
        computation). Shell files (``_app``/``_document``/error pages)
        and root router files return ``None`` (the app-shell rule).
        SPA component files return their route-branch slug (mirrors
        :class:`ReactRouterSpaExtractor`). Everything else falls
        through unchanged.
        """
        p = posix(path)
        pages = self._pages(ctx)
        if p in pages.shell_files:
            return None
        slug = pages.owned.get(p)
        if slug is not None:
            return slug
        router = self._router(ctx)
        if p in router.router_files:
            return None
        return router.owned.get(p)

    # ── flow entries ───────────────────────────────────────────────────────────

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        """Structural entry points: page files, api pages, SPA routes.

        One entry per routed Pages-Router file (kind ``page``; files
        under the root's ``api/`` segment are kind ``http``) with the
        derived URL pattern and the default-export component symbol,
        plus one entry per resolved react-router route. Shell files
        seed nothing (the app-shell rule).
        """
        entries: list[FlowEntry] = []
        pages = self._pages(ctx)
        for f in pages.routable:
            rest = pages.rest_of(f)
            if rest is None:
                continue
            url, is_api = self._url_from_rest(rest)
            entries.append(FlowEntry(
                path=f,
                symbol=self._default_export_symbol(ctx.repo_path, f),
                kind="http" if is_api else "page",
                route=url,
            ))
        entries.extend(self._router(ctx).entries)
        return entries

    @staticmethod
    def _url_from_rest(rest: str) -> tuple[str, bool]:
        """URL pattern for a path under a pages root (+ api flag).

        ``index`` maps to the parent segment; dynamic ``[x]`` /
        ``[...x]`` / ``[[...x]]`` become ``:x``; a trailing ``page``
        dot-token (the Next ``pageExtensions`` colocation convention)
        is not part of the URL.
        """
        parts = rest.split("/")
        fname = parts.pop()
        stem = fname
        for ext in _JS_EXTS:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        stem_tokens = [t for t in stem.split(".") if t]
        if stem_tokens and stem_tokens[-1] == "page":
            stem_tokens.pop()
        stem = ".".join(stem_tokens)
        segs = [s for s in parts if s]
        if stem and stem != "index":
            segs.append(stem)
        out: list[str] = []
        for seg in segs:
            if seg.startswith("[") and seg.endswith("]"):
                out.append(":" + seg.strip("[]").lstrip("."))
            else:
                out.append(seg)
        is_api = bool(segs) and segs[0] == "api"
        return "/" + "/".join(out), is_api

    @staticmethod
    def _default_export_symbol(repo_root, rel_path: str) -> str:  # noqa: ANN001
        """Best-effort default-export component name (else ``""``)."""
        text = read_text(repo_root / rel_path)
        if not text:
            return ""
        m = re.search(
            r"export\s+default\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
            text,
        )
        if m:
            return m.group(1)
        m = re.search(r"export\s+default\s+([A-Za-z_$][\w$]*)\s*;?", text)
        if m and m.group(1) not in ("function", "async", "class"):
            return m.group(1)
        return ""

    # ── attribution policy ─────────────────────────────────────────────────────

    def attribution_rules(self) -> AttributionSpec:
        """Declarative fan-out policy — same as the Next App profile:
        shared UI/hooks/utils blast-radius across consumers instead of
        collapsing a page feature into a physical-container blob."""
        return AttributionSpec(
            colocate_roots=("page",),
            shared_roles=(FileRole.COMPONENT, FileRole.HOOK, FileRole.LIB),
            max_fanout=_SHARED_FANOUT_CAP,
        )

    # ── Stage-1 activation fold (optional override contract) ────────────────────

    def stage_1_extractor_overrides(
        self, ctx: "ScanContext",  # noqa: ARG002 — contract signature
    ) -> list[object]:
        """Extractor instances Stage 1 must run for this profile's repos.

        Consumed duck-typed by ``stage_1_extractors`` (trunk never names
        this profile): the always-active pages route extractor REPLACES
        the discovered ``route`` instance; the SPA extractor is
        appended. Only reachable when this profile won selection — a
        non-winning registration stays inert (G4).
        """
        return [
            _ProfileActivatedPagesRouteExtractor(self._pages),
            ReactRouterSpaExtractor(self._router),
        ]

    # ── internals ────────────────────────────────────────────────────────────

    def _pages(self, ctx: "ScanContext") -> _PagesIndex:
        key = (id(ctx), len(ctx.tracked_files))
        if self._pages_index is None or self._pages_key != key:
            self._pages_index = _PagesIndex(ctx)
            self._pages_key = key
        return self._pages_index

    def _router(self, ctx: "ScanContext") -> _RouterIndex:
        key = (id(ctx), len(ctx.tracked_files))
        if self._router_index is None or self._router_key != key:
            self._router_index = _RouterIndex(ctx)
            self._router_key = key
        return self._router_index


__all__ = ["NextPagesReactProfile", "ReactRouterSpaExtractor"]
