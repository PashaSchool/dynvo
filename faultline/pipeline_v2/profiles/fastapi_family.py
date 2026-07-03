"""FastAPI-family :class:`FrameworkProfile` — FastAPI + Litestar (Phase B #1).

The engine's deep, deterministic understanding of how a FastAPI-family
repository (FastAPI, Litestar — same decorator/router grammar) assembles
files into user-facing capabilities. Encodes the *framework convention*
(valid for ANY FastAPI/Litestar repo), never a corpus repo's paths — see
CLAUDE.md ``rule-no-repo-specific-paths`` / ``rule-no-magic-tuning``.

Why this profile exists (WS2 finding, 2026-07-03): keyless scans have no
Stage 0.5 auditor (it is an LLM stage), and Stage 0's heuristic tags a
hybrid repo by its ROOT manifest — a FastAPI backend living next to a JS
frontend (root ``package.json``) collapses to ``js-generic`` and the
FastAPI extractor never activates (polar attribution 0.04 %, dispatch
0.57 %). ``detects()`` below reads the framework's own structural
fingerprints from the WHOLE tree, so Tier-1 detection is independent of
the LLM auditor.

Structural model encoded (framework docs, never a repo README):

  * **Dependency manifests** — ``fastapi`` / ``litestar`` declared in any
    tracked ``pyproject.toml`` / ``requirements*.txt`` / ``Pipfile`` /
    ``setup.py`` (manifests live per-package in hybrid repos, not at the
    repo root).
  * **App construction** — ``FastAPI(...)`` / ``Litestar(...)`` and the
    router objects ``APIRouter(...)`` (FastAPI) / ``Router(...)``
    (Litestar).
  * **Decorator grammar** — ``@app.get("/x")`` / ``@router.post("/y")``
    (FastAPI: method on an app/router object) and bare ``@get("/x")`` /
    ``@post(...)`` handlers or ``Controller`` subclasses (Litestar).
  * **Server entry** — ``uvicorn`` (the canonical ASGI dev server for
    both frameworks) as a dependency or ``uvicorn.run(`` in source.
  * **Domain packages** — the dominant large-app layout groups one
    capability per Python package: a directory holding a *router module*
    (``endpoints.py`` / ``views.py`` / ``api.py`` / any module declaring
    routes) plus its colocated ``service`` / ``models`` / ``schemas``
    siblings (polar ``server/polar/checkout/``, dispatch
    ``src/dispatch/incident/``). The directory NAME is the capability.
    Router modules inside a *generic container* (``routers/`` /
    ``api/routes/``) instead name a capability per MODULE — that case is
    already covered by the reused
    :class:`~faultline.pipeline_v2.extractors.fastapi.FastApiRouteExtractor`
    anchors, so this profile does not duplicate it.

Alignment contract (same as the Next profile): ``feature_of`` returns
the SAME kebab slug the profile's own Stage-1 domain extractor emits
(``slugify`` of the domain directory name) because Stage-2 re-homes a
path only onto a feature whose name already exists.

Activation fold (Phase B): the pre-profile activation gate in
``extractors/fastapi.py`` (Stage-0 stack-tag branch, G3 allowlist row)
is deleted; activation now flows through
:meth:`FastApiFamilyProfile.stage_1_extractor_overrides` — the profile,
having already detected the framework structurally, supplies an
always-active route extractor instance plus the domain-package
extractor. The trunk consumes overrides duck-typed and never names a
concrete profile.

Deterministic — NO LLM, NO network. Universal — no corpus paths; the
one structural floor and the fan-out cap are justified inline.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.extractors.fastapi import (
    FastApiRouteExtractor,
    _join_path,
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

#: The family's stack tags as Stage 0 / the auditor emit them.
_FAMILY_STACK_TAGS = frozenset({"fastapi", "litestar"})

#: Dependency manifest filenames (looked up ANYWHERE in the tree — hybrid
#: repos keep the backend manifest in a sub-package, not at the root).
_MANIFEST_NAMES = ("pyproject.toml", "Pipfile", "setup.py")
_REQUIREMENTS_PREFIX = "requirements"

#: A dependency declaration for the family, as it appears in
#: pyproject/requirements/Pipfile lines: the package name at a token
#: boundary followed by an extra/version/quote/end. Word-boundary +
#: right-context anchoring avoids the substring trap that mis-tagged
#: litestar as django (``"DJ",  # flake8-django``).
_DEP_RE = re.compile(
    r"""(?mix)
    (?:^|["'\s=\[])            # token start: line start, quote, ws, =, [
    (fastapi|litestar)          # the framework package
    (?:\[[^\]]*\])?             # optional extras: fastapi[standard]
    \s*(?:[<>=!~^,;"']|$)       # version op / quote / list sep / EOL
    """,
)

#: The framework's own repository declares itself as the project name —
#: structurally a family repo (the ``litestar`` clone in the corpus).
_SELF_NAME_RE = re.compile(
    r"""(?mx)^\s*name\s*=\s*["'](fastapi|litestar)["']""",
)

#: ASGI dev-server entry fingerprint (supporting evidence only).
_UVICORN_RE = re.compile(r"\buvicorn\b")

#: App construction — the unambiguous "this file boots the framework"
#: markers. ``Router(`` alone is too generic to count without a litestar
#: import in the same file (guarded in ``_source_evidence``).
_FASTAPI_IMPORT_RE = re.compile(r"(?m)^\s*(?:from|import)\s+fastapi\b")
_LITESTAR_IMPORT_RE = re.compile(r"(?m)^\s*(?:from|import)\s+litestar\b")
_FASTAPI_APP_RE = re.compile(r"\b(?:FastAPI|APIRouter)\s*\(")
_LITESTAR_APP_RE = re.compile(r"\b(?:Litestar|Router|Controller)\s*\(")

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

#: FastAPI decorator grammar: ``@<obj>.<method>("/path")``.
_FASTAPI_DECORATOR_RE = re.compile(
    r"@(\w+)\.(get|post|put|patch|delete|head|options)\(\s*['\"]([^'\"]*)['\"]?",
)
#: Litestar decorator grammar: bare ``@get("/path")`` / ``@post()`` at
#: decorator position (start of line), plus ``class X(Controller)``.
_LITESTAR_DECORATOR_RE = re.compile(
    r"(?m)^\s*@(get|post|put|patch|delete|route)\(\s*(?:['\"]([^'\"]*)['\"])?",
)
_LITESTAR_CONTROLLER_RE = re.compile(r"(?m)^\s*class\s+\w+\([^)]*\bController\b")

#: The handler def that follows a route decorator.
_DEF_RE = re.compile(r"(?:async\s+)?def\s+(\w+)\s*\(")

#: App CONSTRUCTION at MODULE scope (``app = FastAPI(...)`` on an
#: unindented line — instantiation, never a class definition, an import,
#: or a test helper building a dummy app inside a function). Composition
#: evidence for the shell rule below.
_APP_CONSTRUCTION_RE = re.compile(
    r"(?m)^\w[\w.]*(?:\s*:\s*[^=\n]+)?\s*=\s*(?:FastAPI|Litestar)\s*\(",
)
#: Router-tree composition: mounting sub-routers.
_INCLUDE_ROUTER_RE = re.compile(r"\.include_router\(")

#: Generic router-container directory names. A router module directly
#: inside one of these names a capability per MODULE (the reused route
#: extractor's job), never per DIRECTORY — the container itself is
#: scaffolding, not a capability.
_GENERIC_ROUTER_DIRS = frozenset({
    "routers", "routes", "api", "apis", "endpoints", "views",
    "handlers", "controllers", "resources", "rest",
})

#: Path segments that never host capability boundaries: tests, database
#: migrations, documentation/example trees, ops scripts, vendored code.
#: Ecosystem-standard names (alembic's ``versions/``, pytest's ``test*``
#: convention), not corpus paths.
_EXCLUDED_SEGMENTS = frozenset({
    "migrations", "versions", "alembic", "docs", "doc", "examples",
    "example", "samples", "scripts", "node_modules", "site-packages",
    ".venv", "venv", "__pycache__",
})

#: Colocation directory roles (classification only).
_DOMAIN_DIRS = frozenset({"models", "schemas", "entities", "domain", "tables"})
_SERVICE_DIRS = frozenset({"services", "crud", "repositories", "tasks", "workers"})
_LIB_DIRS = frozenset({"lib", "libs", "utils", "util", "helpers", "common",
                       "core", "kit", "shared"})
_DOMAIN_FILES = frozenset({"models.py", "schemas.py", "tables.py", "entities.py"})
_SERVICE_FILES = frozenset({"service.py", "services.py", "crud.py", "repository.py",
                            "tasks.py"})
_CONFIG_FILES = frozenset({"config.py", "settings.py", "conf.py"})
_ROUTER_FILES = frozenset({"endpoints.py", "routes.py", "router.py", "routers.py",
                           "api.py", "views.py", "urls.py"})

#: Bounded-scan caps — I/O safety valves for huge repos, not accuracy
#: knobs: evidence is almost always in the first few candidate files
#: because the candidate list is priority-ordered (entry-shaped names
#: first). Structural, identical on every repo.
_MAX_MANIFEST_READS = 100
_MAX_SOURCE_READS = 400

#: A capability directory must own at least this many distinct source
#: files (same structural floor as the Next profile's
#: ``_MIN_BOUNDARY_FILES``): one lone file is not a multi-file
#: capability slice; two colocated modules (router + service) is the
#: smallest non-trivial one. NOT corpus-tuned.
_MIN_BOUNDARY_FILES = 2

#: A shared file fanning out to more than a handful of features is
#: provenance noise, not signal — same few-owners-is-signal policy (and
#: the same constant) as the Next profile. Scale-invariant.
_SHARED_FANOUT_CAP = 3


# ── small helpers ────────────────────────────────────────────────────────────


def _segments(path: str) -> tuple[list[str], str]:
    p = posix(path)
    if "/" in p:
        head, fname = p.rsplit("/", 1)
        return head.split("/"), fname
    return [], p


def _is_test_path(path: str) -> bool:
    segs, fname = _segments(path.lower())
    if any(seg.startswith("test") or seg == "conftest" for seg in segs):
        return True
    return (
        fname.startswith("test_")
        or fname.endswith("_test.py")
        or fname == "conftest.py"
    )


def _is_excluded_path(path: str) -> bool:
    segs, _fname = _segments(posix(path).lower())
    return bool(_EXCLUDED_SEGMENTS.intersection(segs)) or _is_test_path(path)


def _routes_in_source(text: str) -> bool:
    """True when ``text`` declares family routes (decorators / router ctor)."""
    if _FASTAPI_DECORATOR_RE.search(text):
        return True
    if "APIRouter(" in text:
        return True
    if _LITESTAR_IMPORT_RE.search(text) and (
        _LITESTAR_DECORATOR_RE.search(text)
        or _LITESTAR_CONTROLLER_RE.search(text)
    ):
        return True
    return False


def _iter_py(ctx: "ScanContext") -> list[str]:
    return [f for f in ctx.tracked_files if posix(f).endswith(".py")]


def _candidate_order(py_files: list[str]) -> list[str]:
    """Entry-shaped files first so bounded scans find evidence fast.

    ``main.py`` / ``app.py`` / ``api.py`` / ``server.py`` / ``asgi.py``
    and the conventional router filenames are where app construction and
    decorators live; scanning them first keeps the bounded source scan
    reliable on multi-thousand-file repos.
    """
    entry_names = frozenset({
        "main.py", "app.py", "application.py", "server.py", "asgi.py",
    }) | _ROUTER_FILES

    def _key(f: str) -> tuple[int, str]:
        fname = posix(f).rsplit("/", 1)[-1].lower()
        return (0 if fname in entry_names else 1, f)

    return sorted(py_files, key=_key)


# ── domain-package boundary index ───────────────────────────────────────────


class _DomainIndex:
    """Deterministic index of capability boundaries + router modules.

    ``boundaries`` maps a directory prefix (POSIX, no trailing slash) to
    its kebab slug; ``owned`` maps every owned tracked file to its
    boundary prefix (deepest boundary wins). ``router_modules`` lists
    every non-excluded module that declares family routes (feeds
    ``flow_entries``).
    """

    def __init__(self, ctx: "ScanContext") -> None:
        py_files = [
            f for f in _iter_py(ctx) if not _is_excluded_path(f)
        ]

        router_modules: list[str] = []
        # Per-directory composition-vs-own-routes tally (shell rule).
        own_routes: dict[str, int] = {}
        composition: dict[str, int] = {}
        for rel in py_files:
            text = read_text(ctx.repo_path / rel)
            if not text:
                continue
            p = posix(rel)
            segs, _fname = _segments(p)
            d = "/".join(segs)
            if _routes_in_source(text):
                router_modules.append(p)
                n_routes = len(_FASTAPI_DECORATOR_RE.findall(text))
                if _LITESTAR_IMPORT_RE.search(text):
                    n_routes += len(_LITESTAR_DECORATOR_RE.findall(text))
                own_routes[d] = own_routes.get(d, 0) + n_routes
            comp = len(_INCLUDE_ROUTER_RE.findall(text)) + len(
                _APP_CONSTRUCTION_RE.findall(text),
            )
            if comp:
                composition[d] = composition.get(d, 0) + comp
        self.router_modules: tuple[str, ...] = tuple(sorted(router_modules))

        # The APPLICATION SHELL rule — the FastAPI-family analog of the
        # Next profile's ownerless ``app/`` root: a directory whose
        # direct modules are COMPOSITION-DOMINANT (module-scope app
        # construction + ``include_router`` mounts outnumber the routes
        # declared there) is the shell that assembles capabilities, not
        # a capability itself. Without it the package root (``main.py``
        # + the aggregator router) becomes one boundary owning every
        # residual file — the physical-container blob, including e.g. a
        # colocated SPA tree that Stage 4 should decompose instead. A
        # dir with real routes of its own (a sub-app capability such as
        # a hosted checkout page, or an aggregating parent that still
        # declares endpoints) stays a capability. Structural ratio —
        # scale-invariant, no tuned constant.
        shell_dirs = {
            d for d, comp in composition.items()
            if comp > own_routes.get(d, 0)
        }

        # Candidate boundary dirs: the parent dir of each router module,
        # unless the parent is a generic router container, scaffolding
        # noise, the repo root, or the application shell.
        candidates: set[str] = set()
        for rel in self.router_modules:
            segs, _fname = _segments(rel)
            if not segs:
                continue
            parent = segs[-1]
            if parent in _GENERIC_ROUTER_DIRS or is_noise(parent):
                continue
            if not slugify(parent):
                continue
            prefix = "/".join(segs)
            if prefix in shell_dirs:
                continue
            candidates.add(prefix)

        # Deepest-wins ownership: every tracked (non-test/excluded) file
        # under a candidate belongs to the DEEPEST candidate prefix above
        # it, so nested domain packages stay distinct capabilities.
        ordered = sorted(candidates, key=lambda p: (-p.count("/"), p))
        owned: dict[str, str] = {}
        population: dict[str, set[str]] = {c: set() for c in candidates}
        for rel in ctx.tracked_files:
            p = posix(rel)
            if _is_excluded_path(p):
                continue
            for cand in ordered:
                if p.startswith(cand + "/"):
                    owned[p] = cand
                    if p.endswith(".py"):
                        population[cand].add(p)
                    break

        # Structural floor: a boundary must own >= _MIN_BOUNDARY_FILES
        # source files to be a capability.
        kept = {
            c for c, files in population.items()
            if len(files) >= _MIN_BOUNDARY_FILES
        }
        self.boundaries: dict[str, str] = {
            c: slugify(c.rsplit("/", 1)[-1]) for c in sorted(kept)
        }
        self.owned: dict[str, str] = {
            p: c for p, c in owned.items() if c in kept
        }
        self.population: dict[str, tuple[str, ...]] = {
            c: tuple(sorted(
                p for p, cc in self.owned.items() if cc == c
            ))
            for c in sorted(kept)
        }

    def slug_of(self, path: str) -> str | None:
        prefix = self.owned.get(posix(path))
        return self.boundaries.get(prefix) if prefix else None


# ── profile-supplied Stage-1 extractors ──────────────────────────────────────


class _ProfileActivatedFastApiRouteExtractor(FastApiRouteExtractor):
    """The reused route extractor, activation folded under the profile.

    The profile has already established (via :meth:`detects`) that this
    IS a family repo, so the pre-profile stack-tag gate is bypassed —
    the extractor's PARSING is reused untouched. Supplied through
    ``stage_1_extractor_overrides``; replaces the discovered instance by
    ``name`` so it never runs twice.
    """

    def is_active(self, ctx: "ScanContext") -> bool:  # noqa: ARG002
        return True


class FastApiDomainExtractor:
    """One anchor per FastAPI-family domain package (capability dir).

    Implements the Stage-1 :class:`AnchorExtractor` Protocol. Emits, for
    every domain-package boundary (a directory owning a router module +
    colocated source), an :class:`AnchorCandidate` whose ``paths`` are
    the boundary's owned files — this is what pulls a domain-layout
    backend (polar / dispatch shape) into deterministic attribution
    instead of leaving every non-router file unattributed.

    Lives in the profile module (stack knowledge stays in the profile);
    reaches Stage 1 exclusively via the profile's extractor overrides,
    so it can never fire on a repo the profile did not win.
    """

    name = "fastapi-domain"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        index = _DomainIndex(ctx)
        out: list[AnchorCandidate] = []
        for prefix, slug in index.boundaries.items():
            paths = index.population[prefix]
            if not slug or not paths:
                continue
            routers = [
                p for p in paths if p in set(index.router_modules)
            ]
            out.append(AnchorCandidate(
                name=slug,
                paths=paths,
                source=self.name,
                confidence_self=0.75,
                rationale=(
                    f"fastapi-family domain package {prefix}/ "
                    f"({len(paths)} files, router={routers[0] if routers else '?'})"
                ),
            ))
        return out


# ── the profile ──────────────────────────────────────────────────────────────


class FastApiFamilyProfile:
    """Framework Knowledge Layer for FastAPI + Litestar."""

    name = "fastapi-family"

    def __init__(self) -> None:
        # Single-slot memo: the index is pure w.r.t. ctx and the profile
        # instance lives for one scan; keyed by identity + tree size so
        # a different ctx can never alias.
        self._index_key: tuple[int, int] | None = None
        self._index: _DomainIndex | None = None

    # ── detection ───────────────────────────────────────────────────────────

    def detects(self, ctx: "ScanContext") -> float:
        """Confidence that this is a FastAPI/Litestar repo — LLM-free.

        Graded by signal strength (never a repo-tuned constant):

          * 0.95 — Stage 0 / the auditor already tagged the family.
          * 0.6 + 0.35·fraction — monorepo whose workspaces carry the tag.
          * 0.9  — a family dependency declared in a tracked manifest AND
                   app-construction / decorator evidence in source.
          * 0.75 — manifest dependency only (routers too deep for the
                   bounded scan) — still beats a structural Next marker.
          * 0.7  — source evidence only (vendored / unusual manifests).
          * 0.0  — otherwise (never wins; G4 inertness holds).
        """
        if (ctx.audited_stack or ctx.stack or "").lower() in _FAMILY_STACK_TAGS:
            return 0.95

        wss = ctx.workspaces or []
        if wss:
            family_ws = sum(
                1 for ws in wss
                if (ws.stack or "").lower() in _FAMILY_STACK_TAGS
            )
            if family_ws:
                return min(0.6 + 0.35 * (family_ws / len(wss)), 0.95)

        has_dep = self._manifest_dependency(ctx)
        has_source = self._source_evidence(ctx)
        if has_dep and has_source:
            return 0.9
        if has_dep:
            return 0.75
        if has_source:
            return 0.7
        return 0.0

    @staticmethod
    def _manifest_dependency(ctx: "ScanContext") -> bool:
        """A family dep declared in ANY tracked dependency manifest."""
        reads = 0
        for rel in ctx.tracked_files:
            p = posix(rel)
            if _is_excluded_path(p):
                continue
            fname = p.rsplit("/", 1)[-1].lower()
            is_manifest = fname in _MANIFEST_NAMES or (
                fname.startswith(_REQUIREMENTS_PREFIX)
                and fname.endswith(".txt")
            )
            if not is_manifest:
                continue
            text = read_text(ctx.repo_path / rel)
            reads += 1
            if text and (_DEP_RE.search(text) or _SELF_NAME_RE.search(text)):
                return True
            if reads >= _MAX_MANIFEST_READS:
                break
        return False

    @staticmethod
    def _source_evidence(ctx: "ScanContext") -> bool:
        """App construction or decorator grammar in tracked .py source."""
        reads = 0
        for rel in _candidate_order(_iter_py(ctx)):
            p = posix(rel)
            if _is_excluded_path(p):
                continue
            text = read_text(ctx.repo_path / rel)
            reads += 1
            if text:
                if _FASTAPI_IMPORT_RE.search(text) and (
                    _FASTAPI_APP_RE.search(text)
                    or _FASTAPI_DECORATOR_RE.search(text)
                ):
                    return True
                if _LITESTAR_IMPORT_RE.search(text) and (
                    _LITESTAR_APP_RE.search(text)
                    or _LITESTAR_DECORATOR_RE.search(text)
                    or _LITESTAR_CONTROLLER_RE.search(text)
                ):
                    return True
                if _UVICORN_RE.search(text) and _FASTAPI_APP_RE.search(text):
                    return True
            if reads >= _MAX_SOURCE_READS:
                break
        return False

    # ── workspaces ───────────────────────────────────────────────────────────

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """Pure delegation to the shared package-manager splitter.

        FastAPI-family repos have no framework-specific monorepo format;
        the DefaultProfile behaviour is exactly right.
        """
        return split_workspaces(ctx)

    # ── file classification ───────────────────────────────────────────────────

    def classify_file(self, path: str) -> FileRole:
        """Map a repo-relative path to its FastAPI-family structural role."""
        segs, fname = _segments(posix(path))
        low_fname = fname.lower()
        seg_set = {s.lower() for s in segs}

        if _is_test_path(path):
            return FileRole.TEST
        if low_fname in _CONFIG_FILES:
            return FileRole.CONFIG
        if low_fname in _ROUTER_FILES or seg_set & _GENERIC_ROUTER_DIRS:
            return FileRole.API
        if low_fname in _DOMAIN_FILES or seg_set & _DOMAIN_DIRS:
            return FileRole.DOMAIN
        if low_fname in _SERVICE_FILES or seg_set & _SERVICE_DIRS:
            return FileRole.SERVICE
        if seg_set & _LIB_DIRS:
            return FileRole.LIB
        return FileRole.UNKNOWN

    # ── feature attribution ────────────────────────────────────────────────────

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:
        """The domain-package capability this file serves, or ``None``.

        Returns the kebab slug of the file's deepest owning domain
        package — byte-identical to the name the profile's own
        :class:`FastApiDomainExtractor` anchor carries, so the Stage-2
        re-home always has an existing feature to land on. Files outside
        every boundary (shared ``core/`` / ``kit/``, generic router
        containers, tests, migrations) return ``None`` and fall through
        to the generic path unchanged.
        """
        return self._domain_index(ctx).slug_of(path)

    # ── flow entries ───────────────────────────────────────────────────────────

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        """Structural HTTP entry points: decorated handler functions.

        One entry per (file, handler symbol, method+path) — FastAPI
        ``@obj.method("/p")`` and Litestar bare ``@method("/p")`` /
        Controller handlers. The symbol is the ``def`` that follows the
        decorator so Stage 3's line-range resolver can map to the real
        handler body. FastAPI routes compose the router's own declared
        ``prefix`` (parsed with the REUSED route-extractor patterns) so
        the derived flow name carries the resource, not a bare ``/``.
        """
        # Sanctioned extractor reuse: the same YAML-driven patterns the
        # FastApiRouteExtractor compiles (router ctor + prefix).
        _ex = FastApiRouteExtractor()
        _compiled = _ex.compile_patterns(_ex.load_config())

        entries: list[FlowEntry] = []
        seen: set[tuple[str, str, str]] = set()
        for rel in self._domain_index(ctx).router_modules:
            text = read_text(ctx.repo_path / rel)
            if not text:
                continue
            router_prefix: dict[str, str] = {}
            for rm in _compiled.router_ctor_re.finditer(text):
                pm = _compiled.prefix_re.search(rm.group(2) or "")
                router_prefix[rm.group(1)] = pm.group(1) if pm else ""
            for m in _FASTAPI_DECORATOR_RE.finditer(text):
                method = m.group(2).upper()
                leaf = _join_path(
                    router_prefix.get(m.group(1), ""), m.group(3) or "",
                )
                symbol = self._next_def(text, m.end())
                key = (rel, symbol, f"{method} {leaf}")
                if key in seen:
                    continue
                seen.add(key)
                entries.append(FlowEntry(
                    path=rel, symbol=symbol, kind="http",
                    route=f"{method} {leaf}",
                ))
            if _LITESTAR_IMPORT_RE.search(text):
                for m in _LITESTAR_DECORATOR_RE.finditer(text):
                    method = m.group(1).upper()
                    leaf = m.group(2) or "/"
                    symbol = self._next_def(text, m.end())
                    key = (rel, symbol, f"{method} {leaf}")
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append(FlowEntry(
                        path=rel, symbol=symbol, kind="http",
                        route=f"{method} {leaf}",
                    ))
        return entries

    @staticmethod
    def _next_def(text: str, pos: int) -> str:
        m = _DEF_RE.search(text, pos)
        return m.group(1) if m else ""

    # ── attribution policy ─────────────────────────────────────────────────────

    def attribution_rules(self) -> AttributionSpec:
        """Declarative fan-out policy for shared FastAPI-family files.

        Shared kit/core/utils (LIB) and repo-level model modules (DOMAIN
        outside a domain package) are genuinely cross-cutting: they must
        blast-radius across consuming features, capped by the same
        few-owners-is-signal policy the Next profile uses.
        """
        return AttributionSpec(
            colocate_roots=("api", "router"),
            shared_roles=(FileRole.LIB, FileRole.DOMAIN),
            max_fanout=_SHARED_FANOUT_CAP,
        )

    # ── Stage-1 activation fold (optional override contract) ────────────────────

    def stage_1_extractor_overrides(
        self, ctx: "ScanContext",  # noqa: ARG002 — contract signature
    ) -> list[object]:
        """Extractor instances Stage 1 must run for this profile's repos.

        Consumed duck-typed by ``stage_1_extractors`` (trunk never names
        this profile): same-``name`` instances REPLACE the discovered
        ones (the always-active route extractor supersedes the stack-tag
        gated one), new names are appended. Only reachable when this
        profile won selection — a non-winning registration stays inert
        (G4).
        """
        return [
            _ProfileActivatedFastApiRouteExtractor(),
            FastApiDomainExtractor(),
        ]

    # ── internals ────────────────────────────────────────────────────────────

    def _domain_index(self, ctx: "ScanContext") -> _DomainIndex:
        key = (id(ctx), len(ctx.tracked_files))
        if self._index is None or self._index_key != key:
            self._index = _DomainIndex(ctx)
            self._index_key = key
        return self._index


__all__ = ["FastApiDomainExtractor", "FastApiFamilyProfile"]
