"""Django :class:`FrameworkProfile` — Django + DRF (Phase B #2).

The engine's deep, deterministic understanding of how a Django
repository assembles files into user-facing capabilities. Encodes the
*framework convention* (valid for ANY Django repo), never a corpus
repo's paths — see CLAUDE.md ``rule-no-repo-specific-paths`` /
``rule-no-magic-tuning``.

Why this profile exists (WS2 finding, 2026-07-03): keyless scans have
no Stage 0.5 auditor (an LLM stage), and Stage 0's heuristic tags a
hybrid/packaged repo by its ROOT manifest — a Django monolith with a
root ``package.json`` collapses to ``js-generic`` (saleor attribution
5.85 %) and the Django extractor never activates. Worse, Stage 0's
substring dependency probe mis-tags NON-Django repos as ``django``
(litestar via a ``"DJ",  # flake8-django`` ruff comment), so a bare
``django`` stack tag is NOT trusted here: :meth:`detects` requires
structural confirmation before the tag-grade score.

Structural model encoded (framework docs, never a repo README):

  * **Dependency manifests** — ``django`` / ``djangorestframework``
    declared in any tracked ``pyproject.toml`` / ``requirements*.txt``
    / ``Pipfile`` / ``setup.py`` at a TOKEN boundary (the substring
    trap: ``flake8-django`` / ``django-filter`` / ``pytest-django``
    must not count).
  * **Project grammar** — ``manage.py`` (``DJANGO_SETTINGS_MODULE`` /
    ``execute_from_command_line``), a settings module declaring
    ``INSTALLED_APPS``, ``urls.py`` with ``urlpatterns`` +
    ``path()/re_path()/url()``, ``get_wsgi_application`` /
    ``get_asgi_application``.
  * **Apps are the capability unit** — Django's explicitness is the
    whole point: ``INSTALLED_APPS`` literally enumerates the project's
    capability modules. An app boundary is (a) an ``INSTALLED_APPS``
    entry that resolves to a tracked directory, or (b) structurally, a
    directory declaring ``apps.py`` (Django's own app marker) or a
    ``models.py`` / ``models/`` package (weak evidence — noise-named
    dirs are rejected for this grade only).
  * **The project settings package is a SHELL, not a capability** —
    the directory hosting the settings module(s) (or a ``settings/``
    package) plus root ``urls.py`` / ``wsgi.py`` / ``asgi.py`` /
    ``manage.py`` assembles apps; it must not become a boundary that
    swallows every residual file (the Next profile's ownerless
    ``app/`` root, the FastAPI profile's composition-dominant shell).
  * **Colocation** — templates, static assets, ``tests`` and
    ``migrations`` live INSIDE the app package by convention; they
    belong to the app's slice (migrations/admin are *support* roles,
    tests are stripped from the output tree by Stage 6.9).
  * **Cross-layer name mirror** — large Django projects mirror app
    names across layers (``graphql/<app>/``, ``api/v1/<app>/``,
    ``tests/<app>/``): a sub-directory of one app named EXACTLY like a
    sibling app serves that sibling's capability. Vocabulary comes
    only from the project's own app list — no free-text matching.
  * **URLConf → view entries** — ``urlpatterns`` route the URL space
    to view callables (``views.foo`` / ``Cls.as_view()`` / DRF
    ``router.register``). The view symbol — not the URLConf line — is
    the real entry point, so flow entries resolve the dotted reference
    through the URLConf's own imports to the file DECLARING the view.

Alignment contract (same as the Next/FastAPI profiles): ``feature_of``
returns the SAME kebab slug the profile's own Stage-1 app extractor
emits (``slugify`` of the app directory name) because Stage-2 re-homes
a path only onto a feature whose name already exists.

Activation fold (Phase B): the pre-profile activation gates in
``extractors/django.py`` (auditor-hint substring + inconclusive-stack
source probe, 2 G3 allowlist rows) are deleted; structural activation
now flows through :meth:`DjangoProfile.stage_1_extractor_overrides` —
the profile, having already detected the framework structurally,
supplies an always-active route extractor instance plus the app
extractor. The trunk consumes overrides duck-typed and never names a
concrete profile.

Deterministic — NO LLM, NO network. Universal — no corpus paths; the
structural floor and the fan-out cap are justified inline.
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
from faultline.pipeline_v2.extractors.django import (
    DjangoExtractor,
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

#: The stack tags Stage 0 / the auditor emit for Django. NOT trusted on
#: their own — Stage 0's substring probe mis-tags foreign repos (the
#: litestar/flake8-django incident), so the tag grade additionally
#: requires a structural fingerprint (dep or source) to fire.
_STACK_TAGS = frozenset({"django", "django-app"})

#: Dependency manifest filenames (looked up ANYWHERE in the tree —
#: hybrid repos keep the backend manifest in a sub-package).
_MANIFEST_NAMES = ("pyproject.toml", "Pipfile", "setup.py", "setup.cfg")
_REQUIREMENTS_PREFIX = "requirements"

#: A Django dependency declaration at a token boundary. Word-boundary +
#: right-context anchoring avoids the substring trap (``flake8-django``,
#: ``django-filter``, ``pytest-django`` must NOT count; ``django[bcrypt]``
#: and ``Django>=4.2`` must).
_DEP_RE = re.compile(
    r"""(?mix)
    (?:^|["'\s=\[])             # token start: line start, quote, ws, =, [
    (django|djangorestframework)
    (?:\[[^\]]*\])?             # optional extras: django[bcrypt]
    \s*(?:[<>=!~^,;"']|$)       # version op / quote / list sep / EOL
    """,
)

#: Source grammar — the unambiguous "this tree runs Django" markers.
_DJANGO_IMPORT_RE = re.compile(r"(?m)^\s*(?:from|import)\s+django\b")
_INSTALLED_APPS_RE = re.compile(r"(?m)^\s*INSTALLED_APPS\s*[:=+]")
_URLPATTERNS_RE = re.compile(r"(?m)^\s*urlpatterns\s*[+:]?=")
_WSGI_ASGI_RE = re.compile(r"\bget_(?:wsgi|asgi)_application\s*\(")
_MANAGE_MARKER_RE = re.compile(
    r"DJANGO_SETTINGS_MODULE|execute_from_command_line",
)

#: URLConf grammar. Route calls are frequently MULTI-LINE (one kwarg per
#: line), so the head is matched alone and the argument window after it
#: is inspected separately (``_ARG_WINDOW``).
_ROUTE_HEAD_RE = re.compile(
    r"\b(path|re_path|url)\(\s*[rR]?['\"]([^'\"]*)['\"]\s*,",
)
#: DRF router registration: ``router.register("units", UnitViewSet)``.
_REGISTER_RE = re.compile(
    r"\.register\(\s*[rR]?['\"]([^'\"]*)['\"]\s*,\s*([A-Za-z_][\w.]*)",
)
_STRING_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_DOTTED_TOKEN_RE = re.compile(r"[A-Za-z_][\w.]*")
_NAMED_GROUP_RE = re.compile(r"\(\?P<(\w+)>[^)]*\)")

#: Import statements inside a URLConf — the resolver follows the file's
#: OWN imports only (single-file, deterministic).
_FROM_IMPORT_PAREN_RE = re.compile(
    r"^\s*from\s+([.\w]+)\s+import\s+\(([^)]*)\)", re.M | re.S,
)
_FROM_IMPORT_RE = re.compile(r"(?m)^\s*from\s+([.\w]+)\s+import\s+([^(\n]+)")
_IMPORT_RE = re.compile(r"(?m)^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?")

#: Argument tokens that are never a view reference (kwarg names / the
#: structural ``include`` mount). ``include`` routes mount sub-URLConfs
#: and are skipped exactly like the reused extractor skips them.
_VIEW_ARG_STOPWORDS = frozenset({
    "include", "name", "namespace", "kwargs", "r", "lambda",
})

#: How many characters of a route call's argument list are inspected
#: after the URL literal, and how many candidate tokens are tried for
#: resolution. I/O / CPU safety valves (a route call's view reference
#: sits at the front of its argument list by grammar), not accuracy
#: knobs. Structural, identical on every repo.
_ARG_WINDOW = 400
_MAX_TOKEN_TRIES = 8

#: Path segments that never host app boundaries nor detection evidence:
#: vendored trees, virtualenvs, docs/example scaffolding. Ecosystem
#: names, not corpus paths. (Tests + migrations are deliberately NOT
#: here for OWNERSHIP — Django colocates them inside the app — but
#: boundary/evidence candidacy still rejects test paths separately.)
_EXCLUDED_SEGMENTS = frozenset({
    "node_modules", "site-packages", ".venv", "venv", "__pycache__",
    "docs", "doc", "examples", "example", "samples", "dist", "build",
})

#: Structural role tables (classification only).
_CONFIG_FILES = frozenset({
    "settings.py", "apps.py", "admin.py", "conf.py", "config.py",
    "manage.py", "wsgi.py", "asgi.py",
})
_API_FILES = frozenset({
    "urls.py", "urlconf.py", "views.py", "viewsets.py",
    "serializers.py", "api.py", "forms.py", "routers.py",
})
_API_DIRS = frozenset({"views", "viewsets", "serializers", "api", "rest",
                       "endpoints"})
_DOMAIN_FILES = frozenset({"models.py", "managers.py", "querysets.py"})
_DOMAIN_DIRS = frozenset({"models"})
_SERVICE_FILES = frozenset({"tasks.py", "services.py", "signals.py",
                            "celery.py", "jobs.py"})
_SERVICE_DIRS = frozenset({"tasks", "services", "management", "jobs",
                           "bgtasks"})
_COMPONENT_DIRS = frozenset({"templates", "templatetags", "static"})
_LIB_DIRS = frozenset({"lib", "libs", "utils", "util", "helpers",
                       "common", "core", "shared"})

#: A capability app must own at least this many distinct non-test
#: source files (same structural floor as the Next / FastAPI profiles):
#: one lone module is not a multi-file capability slice; two colocated
#: modules is the smallest non-trivial one. NOT corpus-tuned.
_MIN_BOUNDARY_FILES = 2

#: Few-owners-is-signal fan-out cap for genuinely shared files — the
#: same scale-invariant policy (and constant) as the Next / FastAPI
#: profiles.
_SHARED_FANOUT_CAP = 3

#: Bounded-scan caps — I/O safety valves for huge repos, not accuracy
#: knobs (candidate lists are priority-ordered, so evidence is found in
#: the first few files). Same values as the FastAPI profile.
_MAX_MANIFEST_READS = 100
_MAX_SOURCE_READS = 400
_MAX_SETTINGS_READS = 50


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


def _is_vendored_path(path: str) -> bool:
    """Hard exclusion — never owned, never evidence."""
    segs, _fname = _segments(posix(path).lower())
    return bool(_EXCLUDED_SEGMENTS.intersection(segs))


def _is_excluded_path(path: str) -> bool:
    """Evidence/boundary-candidacy exclusion (vendored OR test)."""
    return _is_vendored_path(path) or _is_test_path(path)


def _iter_py(ctx: "ScanContext") -> list[str]:
    return [f for f in ctx.tracked_files if posix(f).endswith(".py")]


def _is_settings_module(path: str) -> bool:
    """True for ``settings.py`` / ``settings_*.py`` / ``*_settings.py``
    and any module inside a ``settings/`` package."""
    segs, fname = _segments(posix(path))
    if not fname.endswith(".py"):
        return False
    stem = fname[:-3].lower()
    if stem == "settings" or stem.startswith("settings_") or stem.endswith(
        "_settings",
    ):
        return True
    return bool(segs) and segs[-1].lower() == "settings"


def _is_urlconf_file(path: str) -> bool:
    """``urls.py`` / ``*_urls.py`` / ``urlconf.py`` — plus any module of
    a ``urls/`` PACKAGE (large DRF projects split the URLConf into one
    module per resource: ``app/urls/project.py``)."""
    segs, fname = _segments(posix(path))
    low = fname.lower()
    if low == "urlconf.py" or low.endswith("urls.py"):
        return True
    return bool(segs) and segs[-1].lower() == "urls" and low.endswith(".py")


def _candidate_order(py_files: list[str]) -> list[str]:
    """Entry-shaped files first so bounded scans find evidence fast."""
    def _key(f: str) -> tuple[int, str]:
        _segs, fname = _segments(posix(f))
        low = fname.lower()
        if low == "manage.py":
            rank = 0
        elif _is_settings_module(f):
            rank = 1
        elif _is_urlconf_file(f) or low in ("wsgi.py", "asgi.py", "apps.py"):
            rank = 2
        else:
            rank = 3
        return (rank, f)

    return sorted(py_files, key=_key)


def _clean_pattern(literal: str) -> str:
    """Human-readable route from a ``path()``/``re_path()`` literal."""
    s = _NAMED_GROUP_RE.sub(r"<\1>", literal)
    s = re.sub(r"[\^$\\]", "", s)
    s = re.sub(r"[()?+*|]", "", s)
    return s


# ── INSTALLED_APPS parsing ───────────────────────────────────────────────────


def _parse_installed_apps(text: str) -> list[str]:
    """Quoted entries of every ``INSTALLED_APPS`` list / append in ``text``."""
    entries: list[str] = []
    for m in re.finditer(r"INSTALLED_APPS\b", text):
        window = text[m.end(): m.end() + 4000]
        bracket = window.find("[")
        paren = window.find("(")
        openers = [i for i in (bracket, paren) if i != -1]
        if not openers:
            continue
        start = min(openers)
        closer = "]" if start == bracket else ")"
        end = window.find(closer, start)
        if end == -1:
            continue
        entries.extend(
            re.findall(r"['\"]([\w.]+)['\"]", window[start:end]),
        )
    return entries


def _ancestors(dir_path: str) -> list[str]:
    """``a/b/c`` → ``["a/b/c", "a/b", "a", ""]`` (deterministic bases)."""
    out: list[str] = []
    cur = dir_path
    while cur:
        out.append(cur)
        cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
    out.append("")
    return out


def _resolve_app_dir(
    entry: str, settings_dir: str, dir_set: frozenset[str],
) -> str | None:
    """Map an ``INSTALLED_APPS`` dotted entry to a tracked directory.

    The base is walked up from the settings module's own directory (a
    ``plane.app`` entry read from ``apps/api/plane/settings/common.py``
    resolves at base ``apps/api``). ``AppConfig``-path entries
    (``myapp.apps.MyConfig``) fall back by dropping trailing segments.
    """
    parts = [p for p in entry.split(".") if p]
    while parts:
        rel = "/".join(parts)
        for base in _ancestors(settings_dir):
            cand = f"{base}/{rel}" if base else rel
            if cand in dir_set:
                return cand
        # AppConfig-style entry — drop the trailing class/module segment.
        parts = parts[:-1]
    return None


# ── app-boundary index ───────────────────────────────────────────────────────


class _AppIndex:
    """Deterministic index of Django app boundaries + file ownership.

    ``boundaries`` maps an app directory prefix (POSIX, no trailing
    slash) to its kebab slug; ``owned`` maps every owned tracked file
    to its slug (deepest boundary wins, then the cross-layer name
    mirror re-homes exact app-name sub-segments). ``population`` maps
    each slug to its sorted owned files.
    """

    def __init__(self, ctx: "ScanContext") -> None:
        tracked = [posix(f) for f in ctx.tracked_files]
        tracked_set = frozenset(tracked)
        dir_set: set[str] = set()
        for f in tracked:
            segs, _fname = _segments(f)
            for i in range(1, len(segs) + 1):
                dir_set.add("/".join(segs[:i]))
        frozen_dirs = frozenset(dir_set)

        # ── INSTALLED_APPS-declared apps (the framework's own list) ──
        installed: set[str] = set()
        shells: set[str] = set()
        reads = 0
        for rel in sorted(tracked):
            if reads >= _MAX_SETTINGS_READS:
                break
            if not _is_settings_module(rel) or _is_excluded_path(rel):
                continue
            text = read_text(ctx.repo_path / rel)
            reads += 1
            if not text or "INSTALLED_APPS" not in text:
                continue
            segs, _fname = _segments(rel)
            settings_dir = "/".join(segs)
            # The project SHELL: the package hosting the settings module
            # (for a ``settings/`` package, the package's parent).
            shell = settings_dir
            if segs and segs[-1].lower() == "settings":
                shell = "/".join(segs[:-1])
            if shell:
                shells.add(shell)
            for entry in _parse_installed_apps(text):
                d = _resolve_app_dir(entry, settings_dir, frozen_dirs)
                if d:
                    installed.add(d)

        # ── structural apps (apps.py strong; models.py/models/ weak) ──
        structural: set[str] = set()
        for f in tracked:
            if _is_excluded_path(f):
                continue
            segs, fname = _segments(f)
            if not segs:
                # wsgi/asgi/manage at the repo root → root is shell-ish,
                # but the root is never a boundary anyway.
                continue
            low = fname.lower()
            if low in ("wsgi.py", "asgi.py", "manage.py"):
                shells.add("/".join(segs))
                continue
            if low == "apps.py":
                structural.add("/".join(segs))
            elif low == "models.py" and not is_noise(segs[-1]):
                structural.add("/".join(segs))
            elif (
                low == "__init__.py"
                and len(segs) >= 2
                and segs[-1].lower() in _DOMAIN_DIRS
                and not is_noise(segs[-2])
            ):
                structural.add("/".join(segs[:-1]))

        candidates = {
            c for c in (installed | structural)
            if c and c not in shells and slugify(c.rsplit("/", 1)[-1])
        }

        # ── deepest-wins physical ownership + structural floor ──
        ordered = sorted(candidates, key=lambda p: (-p.count("/"), p))
        phys_owner: dict[str, str] = {}
        phys_py: dict[str, set[str]] = {c: set() for c in candidates}
        for f in tracked:
            if _is_vendored_path(f):
                continue
            for cand in ordered:
                if f.startswith(cand + "/"):
                    phys_owner[f] = cand
                    if f.endswith(".py") and not _is_test_path(f):
                        phys_py[cand].add(f)
                    break

        kept = sorted(
            c for c, files in phys_py.items()
            if len(files) >= _MIN_BOUNDARY_FILES
        )
        self.boundaries: dict[str, str] = {
            c: slugify(c.rsplit("/", 1)[-1]) for c in kept
        }

        # ── cross-layer name mirror (app-name vocabulary only) ──
        # Basename → boundary, unambiguous names only.
        by_base: dict[str, list[str]] = {}
        for c in kept:
            by_base.setdefault(c.rsplit("/", 1)[-1], []).append(c)
        unique_base = {b: cs[0] for b, cs in by_base.items() if len(cs) == 1}

        kept_set = set(kept)
        self.owned: dict[str, str] = {}
        for f, owner in phys_owner.items():
            if owner not in kept_set:
                continue
            slug = self.boundaries[owner]
            own_base = owner.rsplit("/", 1)[-1]
            rel_dirs = f[len(owner) + 1:].split("/")[:-1]
            for seg in rel_dirs:
                target = unique_base.get(seg)
                if target is not None and target != owner and seg != own_base:
                    slug = self.boundaries[target]
                    break
            self.owned[f] = slug

        pop: dict[str, list[str]] = {}
        for f, slug in self.owned.items():
            pop.setdefault(slug, []).append(f)
        self.population: dict[str, tuple[str, ...]] = {
            slug: tuple(sorted(files)) for slug, files in sorted(pop.items())
        }

    def slug_of(self, path: str) -> str | None:
        return self.owned.get(posix(path))


# ── URLConf index (routes → view files/symbols) ─────────────────────────────


def _import_map(text: str, pkg_dotted: str) -> dict[str, str]:
    """Symbol → dotted module map from a URLConf's OWN import lines.

    Relative imports are rebased onto ``pkg_dotted`` (the URLConf's
    package as a dotted path from the repo root) so resolution can use
    the same tracked-file lookup as absolute imports.
    """
    out: dict[str, str] = {}

    def _rebase(mod: str) -> str:
        if not mod.startswith("."):
            return mod
        dots = len(mod) - len(mod.lstrip("."))
        rest = mod.lstrip(".")
        base_parts = pkg_dotted.split(".") if pkg_dotted else []
        if dots > 1:
            base_parts = base_parts[: len(base_parts) - (dots - 1)]
        base = ".".join(p for p in base_parts if p)
        return f"{base}.{rest}".strip(".") if rest else base

    def _add_names(mod: str, names: str) -> None:
        mod = _rebase(mod)
        for raw in names.split(","):
            raw = raw.strip()
            if not raw:
                continue
            if " as " in raw:
                orig, alias = (s.strip() for s in raw.split(" as ", 1))
            else:
                orig = alias = raw
            if orig.isidentifier() and alias.isidentifier():
                out[alias] = f"{mod}.{orig}" if mod else orig

    for m in _FROM_IMPORT_PAREN_RE.finditer(text):
        _add_names(m.group(1), m.group(2))
    for m in _FROM_IMPORT_RE.finditer(text):
        _add_names(m.group(1), m.group(2))
    for m in _IMPORT_RE.finditer(text):
        mod, alias = m.group(1), m.group(2)
        if alias:
            out[alias] = mod
        else:
            out.setdefault(mod.split(".", 1)[0], mod.split(".", 1)[0])
    return out


def _strip_comments(text: str) -> str:
    """Drop ``#``-to-EOL comments (URL literals never carry ``#``)."""
    return re.sub(r"(?m)#.*$", "", text)


#: Recursion cap for following package ``__init__`` re-exports — one
#: hop covers the dominant ``views/`` package convention; two covers a
#: nested re-export. Structural, not tuned.
_MAX_REEXPORT_HOPS = 2


def _resolve_dotted(
    parts: list[str],
    imports: dict[str, str],
    pkg_dir: str,
    tracked_set: frozenset[str],
    repo_path: "object | None" = None,
    hops: int = _MAX_REEXPORT_HOPS,
) -> tuple[str, str] | None:
    """Resolve a dotted view reference to ``(tracked file, symbol)``.

    Longest module prefix that maps to a tracked ``<mod>.py`` (or
    ``<mod>/__init__.py``) wins; bases walk up from the URLConf's own
    directory to the repo root (monorepo sub-projects resolve at their
    own base). A symbol landing on a package ``__init__.py`` follows
    the package's OWN re-export imports (``views/`` packages), at most
    :data:`_MAX_REEXPORT_HOPS` hops.
    """
    if not parts:
        return None
    head = parts[0]
    if head in imports and imports[head] != head:
        parts = imports[head].split(".") + parts[1:]
    # LONGEST module prefix wins across every base (k-major order): a
    # short local prefix (``apps`` → the app's own ``apps.py``) must
    # never shadow the full dotted path resolving at an outer base.
    for k in range(len(parts), 0, -1):
        for base in _ancestors(pkg_dir):
            rel = "/".join(parts[:k])
            cand = f"{base}/{rel}" if base else rel
            rest = parts[k:]
            if f"{cand}.py" in tracked_set:
                return f"{cand}.py", (rest[0] if rest else "")
            init = f"{cand}/__init__.py"
            if init in tracked_set:
                if not rest:
                    return init, ""
                # Package re-export: follow the __init__'s own imports.
                if repo_path is not None and hops > 0:
                    from pathlib import Path as _P

                    text = read_text(_P(str(repo_path)) / init)
                    if text:
                        inner = _import_map(
                            _strip_comments(text), cand.replace("/", "."),
                        )
                        if rest[0] in inner:
                            deeper = _resolve_dotted(
                                rest,
                                inner,
                                cand,
                                tracked_set,
                                repo_path,
                                hops - 1,
                            )
                            if deeper:
                                return deeper
                return init, rest[0]
    return None


class _UrlIndex:
    """Deterministic index of URLConf routes resolved to view files.

    ``entries`` is the flow-entry list (one per resolved route /
    registered viewset); ``view_file_by_route`` maps
    ``(urlconf file, joined pattern)`` to the resolved view file so the
    profile-supplied route extractor can re-home its route tuples onto
    the file that actually serves the route.
    """

    def __init__(self, ctx: "ScanContext") -> None:
        tracked = [posix(f) for f in ctx.tracked_files]
        tracked_set = frozenset(tracked)
        self.entries: list[FlowEntry] = []
        self.view_file_by_route: dict[tuple[str, str], str] = {}
        seen: set[tuple[str, str, str]] = set()

        for rel in sorted(tracked):
            if not _is_urlconf_file(rel) or _is_excluded_path(rel):
                continue
            raw = read_text(ctx.repo_path / rel)
            if not raw:
                continue
            text = _strip_comments(raw)
            has_patterns = "urlpatterns" in text
            has_register = ".register(" in text
            if not has_patterns and not has_register:
                continue
            segs, _fname = _segments(rel)
            pkg_dir = "/".join(segs)
            imports = _import_map(text, pkg_dir.replace("/", "."))

            if has_patterns:
                for m in _ROUTE_HEAD_RE.finditer(text):
                    literal = m.group(2)
                    blob = text[m.end(): m.end() + _ARG_WINDOW]
                    if re.match(r"\s*include\b", blob):
                        continue  # structural mount, not a leaf entry
                    resolved = self._resolve_view(
                        blob, imports, pkg_dir, tracked_set, ctx.repo_path,
                    )
                    file, symbol = resolved if resolved else (rel, "")
                    joined = _join_path(_clean_pattern(literal))
                    route = "" if joined == "/" else joined
                    key = (file, symbol, route)
                    if key in seen:
                        continue
                    seen.add(key)
                    self.entries.append(FlowEntry(
                        path=file, symbol=symbol, kind="http", route=route,
                    ))
                    if resolved:
                        self.view_file_by_route.setdefault(
                            (rel, _join_path(literal)), file,
                        )

            for m in _REGISTER_RE.finditer(text):
                prefix, ref = m.group(1), m.group(2)
                parts = [p for p in ref.split(".") if p != "as_view"]
                resolved = _resolve_dotted(
                    parts, imports, pkg_dir, tracked_set, ctx.repo_path,
                )
                file, symbol = resolved if resolved else (rel, parts[-1])
                joined = _join_path(_clean_pattern(prefix))
                route = "" if joined == "/" else joined
                key = (file, symbol, route)
                if key in seen:
                    continue
                seen.add(key)
                self.entries.append(FlowEntry(
                    path=file, symbol=symbol, kind="http", route=route,
                ))
                if resolved:
                    self.view_file_by_route.setdefault(
                        (rel, _join_path(prefix)), file,
                    )

    @staticmethod
    def _resolve_view(
        blob: str,
        imports: dict[str, str],
        pkg_dir: str,
        tracked_set: frozenset[str],
        repo_path: object,
    ) -> tuple[str, str] | None:
        """First argument token that resolves to a tracked view file.

        Decorator wrappers (``cache_page(3600)(view)``) resolve to
        nothing tracked and fall through to the wrapped reference —
        resolution is the filter, no wrapper name-list needed.
        """
        code = _STRING_RE.sub(" ", blob)
        tries = 0
        for tok in _DOTTED_TOKEN_RE.finditer(code):
            t = tok.group(0)
            parts = [p for p in t.split(".") if p]
            if not parts or parts[0] in _VIEW_ARG_STOPWORDS:
                continue
            if "as_view" in parts:
                parts = parts[: parts.index("as_view")]
                if not parts:
                    continue
            tries += 1
            resolved = _resolve_dotted(
                parts, imports, pkg_dir, tracked_set, repo_path,
            )
            if resolved:
                return resolved
            if tries >= _MAX_TOKEN_TRIES:
                break
        return None


# ── profile-supplied Stage-1 extractors ──────────────────────────────────────


class _ProfileActivatedDjangoRouteExtractor(DjangoExtractor):
    """The reused route extractor, activation folded under the profile.

    The profile has already established (via :meth:`DjangoProfile.detects`)
    that this IS a Django repo, so the tag gate is bypassed — the
    extractor's PARSING is reused untouched. Additionally, each route
    tuple's ``file`` is re-homed from the URLConf onto the file that
    DECLARES the routed view (when the profile's resolver finds it), so
    ``routes_index`` agrees with the profile's flow entries — the
    URLConf is wiring; the view serves the route.
    """

    def __init__(self) -> None:
        super().__init__()
        self._url_key: tuple[int, int] | None = None
        self._url_index: _UrlIndex | None = None

    def is_active(self, ctx: "ScanContext") -> bool:  # noqa: ARG002
        return True

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        anchors = super().extract(ctx)
        if not anchors:
            return anchors
        remap = self._urls(ctx).view_file_by_route
        if not remap:
            return anchors
        out: list[AnchorCandidate] = []
        for a in anchors:
            if not a.routes:
                out.append(a)
                continue
            new_routes = tuple(
                (pat, method, remap.get((file, pat), file))
                for pat, method, file in a.routes
            )
            extra = {f for _pat, _m, f in new_routes} - set(a.paths)
            out.append(AnchorCandidate(
                name=a.name,
                paths=tuple(sorted(set(a.paths) | extra)),
                source=a.source,
                confidence_self=a.confidence_self,
                display_name=a.display_name,
                rationale=a.rationale,
                routes=new_routes,
            ))
        return out

    def _urls(self, ctx: "ScanContext") -> _UrlIndex:
        key = (id(ctx), len(ctx.tracked_files))
        if self._url_index is None or self._url_key != key:
            self._url_index = _UrlIndex(ctx)
            self._url_key = key
        return self._url_index


class DjangoAppExtractor:
    """One anchor per Django app (capability directory).

    Implements the Stage-1 :class:`AnchorExtractor` Protocol. Emits, for
    every app boundary (INSTALLED_APPS entry / apps.py / models
    package), an :class:`AnchorCandidate` whose ``paths`` are the app's
    owned files — this is what pulls a Django monolith into
    deterministic attribution instead of leaving every non-URLConf file
    unattributed.

    Lives in the profile module (stack knowledge stays in the profile);
    reaches Stage 1 exclusively via the profile's extractor overrides,
    so it can never fire on a repo the profile did not win.
    """

    name = "django-app"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        index = _AppIndex(ctx)
        prefix_of: dict[str, str] = {}
        for prefix, slug in index.boundaries.items():
            prefix_of.setdefault(slug, prefix)
        out: list[AnchorCandidate] = []
        for slug, paths in index.population.items():
            if not slug or not paths:
                continue
            out.append(AnchorCandidate(
                name=slug,
                paths=paths,
                source=self.name,
                confidence_self=0.75,
                rationale=(
                    f"django app {prefix_of.get(slug, slug)}/ "
                    f"({len(paths)} files)"
                ),
            ))
        return out


# ── the profile ──────────────────────────────────────────────────────────────


class DjangoProfile:
    """Framework Knowledge Layer for Django / Django REST Framework."""

    name = "django"

    def __init__(self) -> None:
        # Single-slot memos: the indexes are pure w.r.t. ctx and the
        # profile instance lives for one scan; keyed by identity + tree
        # size so a different ctx can never alias.
        self._app_key: tuple[int, int] | None = None
        self._app_index: _AppIndex | None = None
        self._url_key: tuple[int, int] | None = None
        self._url_index: _UrlIndex | None = None

    # ── detection ───────────────────────────────────────────────────────────

    def detects(self, ctx: "ScanContext") -> float:
        """Confidence that this is a Django repo — LLM-free.

        Graded by signal strength (never a repo-tuned constant):

          * 0.95 — Stage 0 / the auditor tagged Django AND a structural
                   fingerprint confirms it. A bare tag is NOT trusted:
                   Stage 0's substring probe mis-tags foreign repos
                   (litestar via ``flake8-django``), and trusting it
                   would steal their selection.
          * 0.6 + 0.35·fraction — monorepo whose workspaces carry the tag.
          * 0.9  — a Django dependency declared in a tracked manifest AND
                   project grammar in source.
          * 0.75 — manifest dependency only.
          * 0.7  — source grammar only (vendored / unusual manifests).
          * 0.0  — otherwise (never wins; G4 inertness holds).
        """
        has_dep = self._manifest_dependency(ctx)
        has_source = self._source_evidence(ctx)

        if (ctx.audited_stack or ctx.stack or "").lower() in _STACK_TAGS and (
            has_dep or has_source
        ):
            return 0.95

        wss = ctx.workspaces or []
        if wss:
            tagged_ws = sum(
                1 for ws in wss if (ws.stack or "").lower() in _STACK_TAGS
            )
            if tagged_ws:
                return min(0.6 + 0.35 * (tagged_ws / len(wss)), 0.95)

        if has_dep and has_source:
            return 0.9
        if has_dep:
            return 0.75
        if has_source:
            return 0.7
        return 0.0

    @staticmethod
    def _manifest_dependency(ctx: "ScanContext") -> bool:
        """A Django dep declared in ANY tracked dependency manifest."""
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
            if text and _DEP_RE.search(text):
                return True
            if reads >= _MAX_MANIFEST_READS:
                break
        return False

    @staticmethod
    def _source_evidence(ctx: "ScanContext") -> bool:
        """Project grammar in tracked .py source (bounded, ordered)."""
        reads = 0
        for rel in _candidate_order(_iter_py(ctx)):
            p = posix(rel)
            if _is_excluded_path(p):
                continue
            text = read_text(ctx.repo_path / rel)
            reads += 1
            if text:
                fname = p.rsplit("/", 1)[-1].lower()
                if fname == "manage.py" and _MANAGE_MARKER_RE.search(text):
                    return True
                if _INSTALLED_APPS_RE.search(text) and (
                    "django.contrib" in text
                    or _DJANGO_IMPORT_RE.search(text)
                ):
                    return True
                if _DJANGO_IMPORT_RE.search(text) and (
                    _URLPATTERNS_RE.search(text)
                    or _WSGI_ASGI_RE.search(text)
                ):
                    return True
            if reads >= _MAX_SOURCE_READS:
                break
        return False

    # ── workspaces ───────────────────────────────────────────────────────────

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """Pure delegation to the shared package-manager splitter.

        Django has no framework-specific monorepo format; the
        DefaultProfile behaviour is exactly right.
        """
        return split_workspaces(ctx)

    # ── file classification ───────────────────────────────────────────────────

    def classify_file(self, path: str) -> FileRole:
        """Map a repo-relative path to its Django structural role.

        ``admin.py`` / ``apps.py`` / ``migrations/`` are SUPPORT
        artifacts of their app (registered here under ``CONFIG`` — the
        closest role in the frozen taxonomy), never capabilities of
        their own; colocated templates/static are the app's UI slice.
        """
        segs, fname = _segments(posix(path))
        low_fname = fname.lower()
        seg_set = {s.lower() for s in segs}

        if _is_test_path(path):
            return FileRole.TEST
        if (
            low_fname in _CONFIG_FILES
            or _is_settings_module(path)
            or "migrations" in seg_set
        ):
            return FileRole.CONFIG
        if _is_urlconf_file(path) or low_fname in _API_FILES or (
            seg_set & _API_DIRS
        ):
            return FileRole.API
        if low_fname in _DOMAIN_FILES or seg_set & _DOMAIN_DIRS:
            return FileRole.DOMAIN
        if low_fname in _SERVICE_FILES or seg_set & _SERVICE_DIRS:
            return FileRole.SERVICE
        if seg_set & _COMPONENT_DIRS:
            return FileRole.COMPONENT
        if seg_set & _LIB_DIRS:
            return FileRole.LIB
        return FileRole.UNKNOWN

    # ── feature attribution ────────────────────────────────────────────────────

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:
        """The app capability this file serves, or ``None``.

        Returns the kebab slug of the file's owning app — byte-identical
        to the name the profile's own :class:`DjangoAppExtractor` anchor
        carries, so the Stage-2 re-home always has an existing feature
        to land on. Files outside every app (the project shell, vendored
        trees, repo-root scripts) return ``None`` and fall through to
        the generic path unchanged.
        """
        return self._apps(ctx).slug_of(path)

    # ── flow entries ───────────────────────────────────────────────────────────

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        """Structural HTTP entry points: URLConf-routed view callables.

        One entry per (view file, symbol, route) — ``path()`` /
        ``re_path()`` / legacy ``url()`` targets plus DRF
        ``router.register`` viewsets. The entry lives at the file
        DECLARING the view (resolved through the URLConf's own imports)
        so Stage 3's line-range resolver maps to the real handler body;
        unresolvable references fall back to the URLConf file itself.
        """
        return list(self._urls(ctx).entries)

    # ── attribution policy ─────────────────────────────────────────────────────

    def attribution_rules(self) -> AttributionSpec:
        """Declarative fan-out policy for shared Django files.

        Only genuinely cross-cutting library code (LIB) fans out —
        Django's models/views/templates belong to exactly the app that
        declares them (explicit ownership is the framework's design),
        so DOMAIN/API are NOT shared roles here, unlike FastAPI's
        repo-level schema modules.
        """
        return AttributionSpec(
            colocate_roots=("api",),
            shared_roles=(FileRole.LIB,),
            max_fanout=_SHARED_FANOUT_CAP,
        )

    # ── Stage-1 activation fold (optional override contract) ────────────────────

    def stage_1_extractor_overrides(
        self, ctx: "ScanContext",  # noqa: ARG002 — contract signature
    ) -> list[object]:
        """Extractor instances Stage 1 must run for this profile's repos.

        Consumed duck-typed by ``stage_1_extractors`` (trunk never names
        this profile): same-``name`` instances REPLACE the discovered
        ones (the always-active route extractor supersedes the tag-gated
        one), new names are appended. Only reachable when this profile
        won selection — a non-winning registration stays inert (G4).
        """
        return [
            _ProfileActivatedDjangoRouteExtractor(),
            DjangoAppExtractor(),
        ]

    # ── internals ────────────────────────────────────────────────────────────

    def _apps(self, ctx: "ScanContext") -> _AppIndex:
        key = (id(ctx), len(ctx.tracked_files))
        if self._app_index is None or self._app_key != key:
            self._app_index = _AppIndex(ctx)
            self._app_key = key
        return self._app_index

    def _urls(self, ctx: "ScanContext") -> _UrlIndex:
        key = (id(ctx), len(ctx.tracked_files))
        if self._url_index is None or self._url_key != key:
            self._url_index = _UrlIndex(ctx)
            self._url_key = key
        return self._url_index


__all__ = ["DjangoAppExtractor", "DjangoProfile"]
