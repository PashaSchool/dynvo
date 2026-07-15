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

import json
import os
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
from faultline.pipeline_v2.extractors.route import RouteFileExtractor
from faultline.pipeline_v2.profiles._dispatch import (
    dispatch_resolver_enabled,
    ts_lazy_binding_specs,
)
from faultline.pipeline_v2.profiles._pages_surface import (
    _JS_EXTS,
    _PagesIndex,
    _SHELL_STEMS,
    _first_dot_token,
    _is_excluded_path,
    _is_js_source,
    _segments,
    default_export_symbol,
    pages_flow_entries,
    url_from_rest,
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

#: App-shell / marker stems, JS extensions, excluded segments, and the
#: whole Pages-Router index now live in ``profiles/_pages_surface.py``
#: (helper tier) so the ``next-app-router`` profile can reuse them for
#: HYBRID ``pages/`` + ``app/`` trees without a cross-profile import
#: (G2 lint). Re-imported above under their original names.

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


def _is_test_path(path: str) -> bool:
    return any(m in posix(path).lower() for m in _TEST_MARKERS)


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
#
# ``_pages_roots`` + ``_PagesIndex`` moved to ``profiles/_pages_surface.py``
# (imported above) — shared with the ``next-app-router`` profile's hybrid
# ``pages/`` + ``app/`` support.


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


def _try_stem(stem: str, tracked_set: frozenset[str]) -> str | None:
    """First tracked file for ``stem`` — direct then ``/index`` — or None."""
    for ext in _JS_EXTS:
        if f"{stem}{ext}" in tracked_set:
            return f"{stem}{ext}"
    for ext in _JS_EXTS:
        if f"{stem}/index{ext}" in tracked_set:
            return f"{stem}/index{ext}"
    return None


def _resolve_spec(
    spec: str,
    from_file: str,
    tracked_set: frozenset[str],
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> str | None:
    """Resolve a module specifier to a tracked source file.

    Relative specifiers resolve against the importing file's directory;
    the ``@/`` and ``~/`` aliases resolve against the package's ``src/``
    root (the Vite/CRA/Next jsconfig convention). Bare package imports
    return ``None`` (external dependency, not a page).

    B44 — ``alias_map`` (``{prefix: (root_dir, ...)}``, from the repo's
    tsconfig ``paths``, threaded only when
    ``FAULTLINE_ROUTER_ALIAS_RESOLVE`` is set) generalises the alias
    resolution: a Vite SPA that maps ``~/`` → ``app/`` (outline) or ``@/``
    → any non-``src`` root resolves against its DECLARED root. When
    ``alias_map`` is ``None`` (flag off) the function is byte-identical to
    the pre-B44 ``src/``-only behaviour.
    """
    segs, _fname = _segments(from_file)
    if spec.startswith("."):
        base = list(segs)
        for part in spec.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if base:
                    base.pop()
            else:
                base.append(part)
        return _try_stem("/".join(base), tracked_set)

    # B44 — declared tsconfig aliases (longest-prefix wins so ``@shared/``
    # beats a bare ``@/``). Only consulted when the flag threaded a map.
    if alias_map:
        for prefix in sorted(alias_map, key=len, reverse=True):
            if not spec.startswith(prefix):
                continue
            rest = [p for p in spec[len(prefix):].split("/") if p and p != "."]
            for root in alias_map[prefix]:
                root_segs = [p for p in root.split("/") if p]
                hit = _try_stem("/".join(root_segs + rest), tracked_set)
                if hit is not None:
                    return hit
            # Known alias but nothing resolved under any declared root —
            # fall through to the legacy ``src`` heuristic below.
            break

    if spec.startswith(("@/", "~/")):
        # Legacy heuristic: walk up to the nearest ``src`` segment.
        if "src" not in segs:
            return None
        src_idx = len(segs) - 1 - segs[::-1].index("src")
        base = segs[: src_idx + 1] + [
            p for p in spec[2:].split("/") if p and p != "."
        ]
        return _try_stem("/".join(base), tracked_set)
    return None


#: B44 flag — react-router-SPA alias resolution + route emission. Default
#: OFF ⇒ ``@/``/``~/`` resolve only against ``src/`` and SPA anchors carry
#: no ``routes`` (byte-identical). Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS``.
ROUTER_ALIAS_RESOLVE_ENV = "FAULTLINE_ROUTER_ALIAS_RESOLVE"


def router_alias_resolve_enabled() -> bool:
    """Default ON (flipped B62, KEY_SCHEMA 29); unset ≡ explicit-1.
    ``FAULTLINE_ROUTER_ALIAS_RESOLVE=0`` (or false/off) disables."""
    return os.environ.get(ROUTER_ALIAS_RESOLVE_ENV, "1").strip() not in {
        "", "0", "false", "False",
    }


def _read_jsonc(path) -> object | None:  # noqa: ANN001 — Path
    """Tolerant JSON-with-comments read (tsconfig files carry ``//`` +
    ``/* */`` comments and trailing commas). Returns ``None`` on failure."""
    text = read_text(path)
    if not text:
        return None
    # Strip block + line comments, then trailing commas — enough for the
    # ``compilerOptions.paths`` subset we read (never executes JS).
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(^|[^:])//[^\n]*", r"\1", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _norm_dir(*parts: str) -> str:
    """Join path fragments into a clean POSIX dir prefix (``""`` = root).

    Drops ``.``/``./`` and empty segments; the result has NO trailing
    slash and NO leading slash so it composes with ``"/".join(...)``.
    """
    out: list[str] = []
    for frag in parts:
        for seg in frag.replace("\\", "/").split("/"):
            if seg in ("", "."):
                continue
            out.append(seg)
    return "/".join(out)


def _build_alias_map(ctx: "ScanContext") -> dict[str, tuple[str, ...]]:
    """``{alias_prefix: (root_dir, ...)}`` from tracked tsconfig ``paths``.

    ``{"~/*": ["./app/*"]}`` in ``tsconfig.json`` → ``{"~/": ("app",)}``.
    Roots are resolved relative to the tsconfig's own directory + its
    ``baseUrl`` (so a monorepo ``apps/web/tsconfig.json`` ``@/`` → ``src/``
    yields ``apps/web/src``). Only wildcard (``prefix/*`` → ``root/*``)
    aliases are read; several tsconfigs merge (multiple roots per prefix,
    tried in declared order). Bounded read; deterministic (sorted files).
    """
    out: dict[str, set[str]] = {}
    reads = 0
    for f in sorted(posix(x) for x in ctx.tracked_files):
        if f.rsplit("/", 1)[-1] != "tsconfig.json" or _is_excluded_path(f):
            continue
        if reads >= _MAX_MANIFEST_READS:
            break
        doc = _read_jsonc(ctx.repo_path / f)
        reads += 1
        if not isinstance(doc, dict):
            continue
        co = doc.get("compilerOptions")
        if not isinstance(co, dict):
            continue
        paths = co.get("paths")
        if not isinstance(paths, dict):
            continue
        raw_base = co.get("baseUrl")
        base_url = raw_base if isinstance(raw_base, str) else "."
        cfg_dir = f.rsplit("/", 1)[0] if "/" in f else ""
        for alias, targets in paths.items():
            if (
                not isinstance(alias, str)
                or not alias.endswith("*")
                or not isinstance(targets, list)
            ):
                continue
            prefix = alias[:-1]  # keep e.g. "~/", "@/", "@shared/"
            if not prefix:
                continue
            for t in targets:
                if not isinstance(t, str) or not t.endswith("*"):
                    continue
                root = _norm_dir(cfg_dir, base_url, t[:-1])
                out.setdefault(prefix, set()).add(root)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def _route_slug(path_literal: str, skip_interp: bool = False) -> str:
    """First meaningful static segment of a route path literal.

    B64 — ``skip_interp`` (armed only by the dispatch-resolver flag): a
    segment carrying template interpolation (``${…}``) is DYNAMIC, the
    same law as ``:param`` — interpolation TEXT is never a static
    segment (pre-fix it minted garbage slugs like ``debug-path`` from
    ``${debugPath()}/changesets``; wave-17 outline raw-dir-slug row)."""
    for seg in path_literal.split("/"):
        seg = seg.strip()
        if not seg or seg.startswith((":", "*")) or seg == "*":
            continue
        if skip_interp and "${" in seg:
            continue
        slug = slugify(seg)
        if slug and not is_noise(slug):
            return slug
    return ""


# ── B64 (b) route-path const-fold ─────────────────────────────────────────
#
# A react-router ``path={draftsPath()}`` / ``{ path: ROUTES.home }`` carries
# its route literal INDIRECTLY through a pure route-helper or a route-const
# instead of a quoted string, so the literal-only ``_PATH_ATTR_RE`` /
# ``_PATH_KEY_RE`` miss and the whole ``<Route>`` (path AND component) is
# dropped — the outline "~87% invisible" class. B64 folds ONE level of PURE
# literal-returning helpers/consts (in this file OR the imported definition
# file) back into the literal so the route survives. Anti-cases — free vars,
# call args, conditionals, concatenation, objects, interpolated templates,
# multi-statement bodies — are an honest SKIP: no route path is invented
# (the residual is the B63 metric). Runtime is never interpreted.
_PATH_EXPR_ATTR_RE = re.compile(r"""\bpath\s*=\s*\{\s*([^{}]+?)\s*\}""")
_PATH_EXPR_KEY_RE = re.compile(
    r"""\bpath\s*:\s*([A-Za-z_$][\w$.]*(?:\(\s*\))?)\s*(?=[,}\n])""",
)
_EXPR_CALL_RE = re.compile(r"^([A-Za-z_$][\w$]*)\(\s*\)$")
_EXPR_IDENT_RE = re.compile(r"^([A-Za-z_$][\w$]*)$")
_EXPR_MEMBER_RE = re.compile(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$")
#: A PURE string value — a whole quoted literal with NO template
#: substitution. Whole-string anchors: any surrounding operator (``+``,
#: ``?``, ``(``) breaks the match, so concatenation / ternaries never fold.
_PURE_STR_RE = re.compile(r"""^(["'])((?:(?!\1)(?!\$\{).)*)\1$""")
_PURE_TEMPLATE_RE = re.compile(r"^`([^`$]*)`$")


def _pure_string_value(expr: str) -> str | None:
    """The string VALUE of ``expr`` iff it is a pure string literal or a
    substitution-free template — else ``None`` (honest skip)."""
    expr = expr.strip()
    m = _PURE_STR_RE.match(expr)
    if m:
        return m.group(2)
    t = _PURE_TEMPLATE_RE.match(expr)
    if t:
        return t.group(1)
    return None


def _extract_pure_def(text: str, name: str) -> str | None:
    """The pure route literal a same-file ``name`` def resolves to, else
    ``None``. Handles ``function name() { return <pure>; }`` (EMPTY params,
    SINGLE-return body), ``const name = () => <pure>`` (empty-param arrow),
    and a bare ``const name = <pure-string>``. Any parameter, extra
    statement, branch, or non-literal return → ``None``."""
    n = re.escape(name)
    fn = re.search(
        r"(?:export\s+)?(?:async\s+)?function\s+" + n
        + r"\s*\(\s*\)\s*(?::[^{]+?)?\{\s*return\s+([^;{}]+?)\s*;?\s*\}",
        text, re.S,
    )
    if fn:
        return _pure_string_value(fn.group(1))
    cm = re.search(
        r"(?:export\s+)?(?:const|let|var)\s+" + n
        + r"\s*(?::[^=]+?)?=\s*([^\n;]+)",
        text,
    )
    if cm:
        rhs = cm.group(1).strip().rstrip(";").strip()
        am = re.match(r"^\(\s*\)\s*(?::[^=]+?)?=>\s*(.+)$", rhs)
        if am:
            body = am.group(1).strip()
            bm = re.match(r"^\{\s*return\s+([^;{}]+?)\s*;?\s*\}$", body)
            if bm:
                return _pure_string_value(bm.group(1))
            return _pure_string_value(body)
        return _pure_string_value(rhs)
    return None


def _extract_object_member(text: str, obj: str, member: str) -> str | None:
    """The pure literal at ``obj.member`` for a flat literal-map const
    (``const ROUTES = { home: "/home", ... }``), else ``None``."""
    n = re.escape(obj)
    m = re.search(
        r"(?:export\s+)?(?:const|let|var)\s+" + n
        + r"\s*(?::[^=]+?)?=\s*\{(.*?)\}",
        text, re.S,
    )
    if not m:
        return None
    mem = re.escape(member)
    em = re.search(
        r"""(?:^|[,{])\s*['"]?""" + mem + r"""['"]?\s*:\s*([^,}\n]+)""",
        m.group(1),
    )
    if em:
        return _pure_string_value(em.group(1).strip())
    return None


def _make_path_folder(
    text: str,
    imports: dict[str, str],
    rel: str,
    tracked_set: frozenset[str],
    alias_map: dict[str, tuple[str, ...]] | None,
    repo_path,  # noqa: ANN001 — Path
):
    """Build the one-level const-fold callback for a router file.

    ``fold(expr) -> str`` returns the folded route literal for a bare
    ident / zero-arg call / ``obj.member`` expression, or ``""`` when the
    expression is not a PURE literal-returning def reachable in ONE level
    (same file, or the file its ident is imported from). Deterministic;
    bounded (one def-file read per specifier, cached)."""
    def_cache: dict[str, str | None] = {}

    def _def_text(name: str) -> str | None:
        spec = imports.get(name)
        if not spec:
            return None
        if spec in def_cache:
            return def_cache[spec]
        target = _resolve_spec(spec, rel, tracked_set, alias_map)
        txt = read_text(repo_path / target) if target else None
        def_cache[spec] = txt
        return txt

    def fold(expr: str) -> str:
        expr = expr.strip()
        call = _EXPR_CALL_RE.match(expr)
        if call:
            name = call.group(1)
            lit = _extract_pure_def(text, name)
            if lit is None:
                dt = _def_text(name)
                lit = _extract_pure_def(dt, name) if dt else None
            return lit or ""
        member = _EXPR_MEMBER_RE.match(expr)
        if member:
            obj, mem = member.group(1), member.group(2)
            lit = _extract_object_member(text, obj, mem)
            if lit is None:
                dt = _def_text(obj)
                lit = _extract_object_member(dt, obj, mem) if dt else None
            return lit or ""
        ident = _EXPR_IDENT_RE.match(expr)
        if ident:
            name = ident.group(1)
            lit = _extract_pure_def(text, name)
            if lit is None:
                dt = _def_text(name)
                lit = _extract_pure_def(dt, name) if dt else None
            return lit or ""
        # A template literal whose EVERY ``${…}`` interpolation folds
        # (same one-level purity law, recursion depth 1 by grammar —
        # a folded value is a plain literal): ``${debugPath()}/changesets``
        # → ``/debug/changesets``. Any unfoldable interpolation ⇒ "".
        if expr.startswith("`") and expr.endswith("`") and "${" in expr:
            body = expr[1:-1]
            out_parts: list[str] = []
            pos = 0
            ok = True
            while True:
                i = body.find("${", pos)
                if i < 0:
                    out_parts.append(body[pos:])
                    break
                j = body.find("}", i)
                if j < 0:
                    ok = False
                    break
                out_parts.append(body[pos:i])
                inner = fold(body[i + 2:j])
                if not inner:
                    ok = False
                    break
                out_parts.append(inner)
                pos = j + 1
            if ok:
                return "".join(out_parts)
        return ""

    return fold


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
        # B44 — declared-alias resolution + route emission (flag-gated).
        # ``alias_map is None`` (flag off) ⇒ ``_resolve_spec`` keeps its
        # ``src/``-only behaviour and no route tuples are recorded, so the
        # index is byte-identical to pre-B44.
        #
        # B64 — the dispatch-resolver is SELF-CONTAINED: resolving a lazy
        # route sub-tree inherently needs alias resolution to reach
        # ``~/``/``@/``-aliased scenes (the Stage 6.3 import tree already
        # resolves those via tsconfig independently), so arming the resolver
        # arms the same alias map + route emission. With BOTH flags off this
        # is byte-identical to pre-B44/B64.
        self._alias_on = router_alias_resolve_enabled()
        self._resolver_on = dispatch_resolver_enabled()
        _routes_on = self._alias_on or self._resolver_on
        alias_map = _build_alias_map(ctx) if _routes_on else None

        self.entries: list[FlowEntry] = []
        self.router_files: set[str] = set()
        buckets: dict[str, set[str]] = defaultdict(set)
        owned: dict[str, str] = {}
        route_tuples: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        seen: set[tuple[str, str, str]] = set()
        scanned: set[str] = set()
        # B64 (a): a router file that ``lazy(() => import("./sub"))``-loads a
        # sub-route-tree (outline's ``routes/index.tsx`` → ``./authenticated``,
        # the ~87% bridge) names files that must be scanned EVEN when they sit
        # past the ``_MAX_SOURCE_READS`` I/O window — the lazy edge is
        # explicit author evidence, not speculative I/O. These are drained
        # after the bounded pass, cap-exempt but budgeted against pathology.
        lazy_pending: list[str] = []

        def _scan(rel: str, text: str) -> None:
            self.router_files.add(rel)
            imports = _import_map(text)
            folder = (
                _make_path_folder(
                    text, imports, rel, tracked_set, alias_map,
                    ctx.repo_path,
                )
                if self._resolver_on else None
            )

            for path_literal, ident in self._route_pairs(text, folder):
                resolved = self._resolve_ident(
                    ident, imports, rel, tracked_set, alias_map,
                ) if ident else None
                slug = _route_slug(
                    path_literal, skip_interp=self._resolver_on,
                )
                if not slug and resolved is not None:
                    _segs, fname = _segments(resolved)
                    slug = slugify(_first_dot_token(fname))
                # B64 — tertiary fallback: the RESOLVED component's own
                # ident names the branch when both the path (all-dynamic
                # segments) and the file stem (``index.ts``) are mute
                # (outline ``${searchPath()}/:query?`` + ``Search/index.ts``
                # → ``search``). Only the ident whose resolution produced
                # ``resolved`` may name — a prop/wrapper name never does.
                if (
                    self._resolver_on
                    and (not slug or is_noise(slug))
                    and resolved is not None
                    and ident
                ):
                    for cand in reversed(ident.split()):
                        spec = imports.get(cand)
                        if not spec or _resolve_spec(
                            spec, rel, tracked_set, alias_map,
                        ) != resolved:
                            continue
                        s = slugify(cand)
                        if s and not is_noise(s):
                            slug = s
                        break
                if not slug or is_noise(slug):
                    continue
                # B64 — error-route shell rule: the SAME law the Pages
                # profile enforces via ``_SHELL_STEMS`` (404/500/_error are
                # framework wiring, never a capability) applied to SPA
                # route intake (wave-17 outline ``'404'`` raw-slug +
                # flowless rows).
                if self._resolver_on:
                    stem = ""
                    if resolved is not None:
                        _rsegs, rfname = _segments(resolved)
                        stem = _first_dot_token(rfname)
                    if slug in ("404", "500") or stem in _SHELL_STEMS:
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
                    if _routes_on:
                        route_tuples[slug].add((route, "PAGE", resolved))

            # B64 (a): enqueue lazily-imported sub-route-trees of THIS router
            # file (deterministic sorted order). Non-router targets read once
            # in the drain and skipped; only router-grammar files recurse.
            if self._resolver_on:
                for _local, spec in sorted(ts_lazy_binding_specs(text).items()):
                    target = _resolve_spec(spec, rel, tracked_set, alias_map)
                    if (
                        target
                        and target not in scanned
                        and _is_js_source(target)
                        and not _is_excluded_path(target)
                        and not _is_test_path(target)
                    ):
                        lazy_pending.append(target)

        reads = 0
        for rel in _candidate_order(candidates):
            if reads >= _MAX_SOURCE_READS:
                break
            if rel in scanned:
                continue
            text = read_text(ctx.repo_path / rel)
            reads += 1
            scanned.add(rel)
            if not text or not _ROUTER_GRAMMAR_RE.search(text):
                continue
            _scan(rel, text)

        # B64 (a): drain lazy-followed sub-routers (cap-exempt, budgeted).
        lazy_reads = 0
        while lazy_pending and lazy_reads < _MAX_SOURCE_READS:
            rel = lazy_pending.pop(0)
            if rel in scanned:
                continue
            scanned.add(rel)
            lazy_reads += 1
            text = read_text(ctx.repo_path / rel)
            if not text or not _ROUTER_GRAMMAR_RE.search(text):
                continue  # a leaf scene component — resolved, not a sub-router
            _scan(rel, text)

        self.buckets: dict[str, tuple[str, ...]] = {
            slug: tuple(sorted(paths))
            for slug, paths in sorted(buckets.items())
        }
        self.owned: dict[str, str] = owned
        # B44 — slug → sorted route tuples for ``routes_index`` (empty when
        # the flag is off, so ``ReactRouterSpaExtractor`` emits route-less
        # anchors byte-identically).
        self.route_tuples_by_slug: dict[str, tuple[tuple[str, str, str], ...]] = {
            slug: tuple(sorted(tuples))
            for slug, tuples in sorted(route_tuples.items())
        }

    @staticmethod
    def _route_pairs(text, folder=None):  # noqa: ANN001,ANN205 — see below
        """(path literal, component ident) pairs declared in ``text``.

        Two grammars: JSX ``<Route path=... element={...}>`` (a bounded
        window after each tag start — the element prop nests JSX, so
        bracket matching is brittle) and route OBJECTS
        (``{ path: "x", element: <X/> }``) for the
        ``createBrowserRouter`` / ``useRoutes`` style.

        B64 (b): ``folder`` (``None`` unless the dispatch-resolver flag is
        armed) folds a NON-literal path (``path={draftsPath()}`` /
        ``{ path: ROUTES.home }``) into its route literal one level deep;
        ``folder is None`` ⇒ every non-literal path is dropped exactly as
        pre-B64 (byte-identical).
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
                # B64 (b): a non-literal path (helper call / const / member)
                # folds one level; unfoldable ⇒ dropped, as pre-B64.
                if folder is not None:
                    em = _PATH_EXPR_ATTR_RE.search(window)
                    if em:
                        folded = folder(em.group(1))
                        if folded:
                            pairs.append((folded, _component_in(window)))
                continue  # pathless layout route — shell, not an entry
            path = pm.group(1)
            # B64 (b): an interpolated template path — fold every ``${…}``
            # when possible (``${debugPath()}/changesets`` →
            # ``/debug/changesets``); unfoldable ⇒ keep the raw text
            # (the slug law treats interpolation segments as dynamic).
            if folder is not None and "${" in path:
                folded = folder("`" + path + "`")
                if folded:
                    path = folded
            pairs.append((path, _component_in(window)))

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

            # B64 (b): route OBJECTS whose ``path`` is a helper/const/member
            # (``{ path: ROUTES.home, element: <Home/> }``) — invisible to
            # the literal-only ``_PATH_KEY_RE``. Fold one level; unfoldable
            # entries are skipped. Only runs when the flag armed ``folder``.
            if folder is not None:
                expr_matches = list(_PATH_EXPR_KEY_RE.finditer(text))
                for i, pm in enumerate(expr_matches):
                    folded = folder(pm.group(1))
                    if not folded:
                        continue
                    end = pm.end() + _ROUTE_WINDOW
                    if i + 1 < len(expr_matches):
                        end = min(end, expr_matches[i + 1].start())
                    pairs.append(
                        (folded, _component_in(text[pm.end():end])),
                    )

        return pairs

    @staticmethod
    def _resolve_ident(
        idents: str,
        imports: dict[str, str],
        rel: str,
        tracked_set: frozenset[str],
        alias_map: dict[str, tuple[str, ...]] | None = None,
    ) -> str | None:
        """Resolve the mounted component to a tracked file.

        Tries the window's capitalized idents LAST-first: by JSX grammar
        the wrapping element name precedes its props, so the innermost
        (real) page reference comes last (``<Lazy Page={AddAdmin}/>`` →
        try ``AddAdmin`` before ``Lazy``). Resolution through the
        file's own imports is the filter — an ident that is not
        imported (a prop name like ``Page``) resolves to nothing and is
        skipped. ``alias_map`` (B44) threads the tsconfig alias roots when
        the flag is on; ``None`` keeps the pre-B44 ``src/``-only path.
        """
        for ident in reversed(idents.split()):
            spec = imports.get(ident)
            if not spec:
                continue
            resolved = _resolve_spec(spec, rel, tracked_set, alias_map)
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
            # B44 — carry the branch's route tuples so ``build_routes_index``
            # populates ``routes_index`` for react-router SPAs (empty when
            # the alias flag is off ⇒ route-less anchor, byte-identical).
            routes = index.route_tuples_by_slug.get(slug, ())
            out.append(AnchorCandidate(
                name=slug,
                paths=paths,
                source=self.name,
                confidence_self=min(0.6 + 0.05 * len(paths), 0.95),
                rationale=(
                    f"react-router branch '/{slug}' mounts "
                    f"{len(paths)} component file(s)"
                ),
                routes=routes,
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
        entries = pages_flow_entries(ctx, self._pages(ctx))
        entries.extend(self._router(ctx).entries)
        return entries

    # URL derivation + default-export symbol live in ``_pages_surface``
    # (shared with the next-app-router hybrid support); kept as thin
    # delegates for any existing caller of the staticmethod spelling.
    _url_from_rest = staticmethod(url_from_rest)
    _default_export_symbol = staticmethod(default_export_symbol)

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
