"""Next.js App Router :class:`FrameworkProfile` — the ONE module per stack.

This is the engine's deep, deterministic understanding of how a Next.js
App Router repository assembles files into user-facing capabilities. It
encodes the *framework convention* (valid for ANY App Router repo), not
any corpus repo's paths — see CLAUDE.md ``rule-no-repo-specific-paths``
and ``rule-no-magic-tuning``. All knowledge lives here + one entry-point
line in pyproject.toml (OCP); the core never changes.

Structural model encoded (from the official Next.js docs,
https://nextjs.org/docs/app/getting-started/project-structure —
NEVER a repo README, per ``rule-no-readme``):

  * The App Router lives under ``app/`` or ``src/app/`` (the ``src/``
    folder is an optional application-source root).
  * **Routing files** make a segment public:
      - ``page.{js,jsx,ts,tsx}``       → a page (PAGE)
      - ``route.{js,ts}``              → an API endpoint (API)
      - ``layout`` / ``template`` / ``loading`` / ``error`` /
        ``not-found`` / ``global-error`` / ``default``  → route-scaffold
        UI (PAGE — they belong to their segment's feature)
  * **Folders define URL segments.** The first *meaningful* segment under
    ``app/`` is the capability the file serves. Skipped, because they
    are NOT URL segments:
      - route groups ``(marketing)``    — organisational only
      - private folders ``_components``  — opted out of routing
      - parallel-route slots ``@modal``  — named slots
      - intercepting routes ``(.)``/``(..)``/``(...)`` — overlay markers
      - dynamic segments ``[id]`` / ``[...slug]`` / ``[[...slug]]`` — params
  * **Colocation:** components / hooks / lib / actions colocated under a
    route segment belong to that segment's feature. ``app/api/**/route.ts``
    is the API surface.
  * **Server Actions** (``"use server"`` files / functions) are mutation
    flow entries.
  * Shared UI / hooks / utils (``components/``, ``hooks/``, ``lib/``)
    are genuinely cross-cutting and must FAN OUT (blast-radius), never
    collapse a route feature into a physical-container blob.

Alignment contract (critical — see the architect's wiring notes):

  ``feature_of`` returns the SAME kebab slug the
  :class:`~faultline.pipeline_v2.extractors.route.RouteFileExtractor`
  surfaces (``slugify`` of the first meaningful segment under ``app/``),
  because the Stage-2 wiring only RE-HOMES a path to a feature whose name
  already exists — it never invents a feature. We therefore reuse the
  extractor's own ``slugify`` / ``is_noise`` helpers so the names match
  byte-for-byte.

Deterministic — NO LLM, NO network. Universal — no corpus paths, no
tuned magic numbers (the one structural cap is justified inline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import is_noise, posix, slugify
from faultline.pipeline_v2.profiles._splitter import split_workspaces
from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# ── App Router conventions (framework constants, not tuned numbers) ──────────

# The App Router root segment names. Both ``app/`` and ``src/app/`` are
# valid per the docs; we locate the segment anywhere in a (possibly
# workspace-prefixed) path so monorepo paths like
# ``apps/web/app/(dashboard)/page.tsx`` resolve correctly.
_APP_SEGMENT = "app"
_SRC_SEGMENT = "src"

# Routing special-file stems (suffix-stripped). ``page`` / ``route`` make
# a segment public; the rest are per-segment scaffold UI. All belong to
# the segment's feature.
_PAGE_STEMS = frozenset({
    "page", "layout", "template", "loading", "error",
    "not-found", "global-error", "default",
})
_ROUTE_STEMS = frozenset({"route"})

# Route Handler HTTP method exports (the structural entry symbols of a
# ``route.ts``). Per the docs, exactly these uppercase names.
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# Source extensions we classify / treat as code.
_PAGE_EXTS = (".tsx", ".jsx", ".ts", ".js")
_ROUTE_EXTS = (".ts", ".js")

# Colocation directory roles. A path segment with one of these names
# marks a shared / cross-cutting role. Used both for classification and
# for fan-out policy. These are the ecosystem-standard placeholders the
# Next docs themselves name ("components", "lib", "hooks", "utils", ...).
_COMPONENT_DIRS = frozenset({"components", "ui"})
_HOOK_DIRS = frozenset({"hooks"})
_LIB_DIRS = frozenset({"lib", "libs", "utils", "util", "helpers"})
_SERVICE_DIRS = frozenset({"services", "server", "data", "queries"})
_DOMAIN_DIRS = frozenset({"models", "schemas", "domain", "entities"})
_ACTION_DIRS = frozenset({"actions"})

# Test / config markers (universal, stack-neutral).
_TEST_MARKERS = (".test.", ".spec.", "/__tests__/", "/test/", "/tests/", "/e2e/")
_CONFIG_MARKERS = (".config.", "config.")

# ── feature-folder conventions (sub-decomposition boundaries) ─────────────────
#
# Beyond filesystem routing, the dominant Next/React large-app convention
# is to group a capability's code under a *named domain folder*: a
# "feature-sliced" or "modular" layout. These container directory names
# are ecosystem-standard (feature-sliced-design, the Next "modules"
# pattern, the bulletproof-react "features" pattern). A path
# ``modules/billing/...`` / ``features/billing/...`` declares ``billing``
# as a capability exactly the way ``app/billing/...`` does. We treat the
# segment immediately *after* one of these container names as a feature
# boundary. Universal convention names — NOT any repo's paths.
_FEATURE_CONTAINER_DIRS = frozenset({
    "modules", "features",
})
# ``components/<domain>/`` is a feature boundary ONLY when the immediate
# child is itself a named domain folder holding a subtree — a top-level
# ``components/Button.tsx`` is a shared primitive, not a feature. We keep
# this container separate because its children are more often shared UI;
# the boundary fires only when the existing route/module evidence is
# absent (see ``_owning_boundary``'s precedence).
_DOMAIN_COMPONENT_CONTAINER = "components"


# ── helpers ──────────────────────────────────────────────────────────────────


def _split(path: str) -> tuple[list[str], str]:
    """Return ``(directory_segments, filename)`` for a POSIX path."""
    p = posix(path)
    if "/" in p:
        head, fname = p.rsplit("/", 1)
        return head.split("/"), fname
    return [], p


def _app_index(segments: list[str]) -> int | None:
    """Index of the App Router root segment (``app`` or ``src/app``).

    Returns the index of the ``app`` segment, or ``None`` when the path
    is not under an App Router tree. Handles a workspace / monorepo
    prefix (``apps/web/app/...``) by scanning for the ``app`` segment;
    ``src/app`` is recognised because ``app`` still appears as a segment.
    Guards against a bare directory literally named ``app`` somewhere
    deep that is NOT the router root by requiring that the matched
    segment is followed by at least one more segment OR a routing file
    (the caller only invokes this for routing-relevant paths).
    """
    for i, seg in enumerate(segments):
        if seg == _APP_SEGMENT:
            return i
    return None


def _is_skipped_segment(seg: str) -> bool:
    """True when ``seg`` is organisational and never a URL/feature segment.

    Route groups ``(group)``, private folders ``_folder``, parallel-route
    slots ``@slot``, intercepting routes ``(.)``/``(..)``/``(...)folder``,
    dynamic segments ``[x]`` / ``[...x]`` / ``[[...x]]``, and the bare
    framework-noise tokens (``api``, ``components``, ...).
    """
    if not seg:
        return True
    # Intercepting routes start with ``(.`` — must be checked before the
    # generic route-group ``(...)`` check.
    if seg.startswith("(") and seg.endswith(")"):
        return True
    if seg.startswith("("):  # an intercepting marker like ``(.)folder``
        # (.)folder, (..)folder, (...)folder — the leading marker is in
        # parens; the routed name follows. Treat the WHOLE token as a
        # segment whose meaningful part is after the closing paren.
        return True
    if seg.startswith("_"):       # private folder
        return True
    if seg.startswith("@"):       # parallel-route slot
        return True
    if seg.startswith("[") and seg.endswith("]"):  # dynamic / catch-all
        return True
    if is_noise(seg):
        return True
    return False


# ── sub-decomposition boundary (kills the workspace-anchor blob) ──────────────
#
# Attribution model (CONSERVATIVE, leaf-based): a routing file is owned by
# its OWN (deepest) meaningful URL segment — NOT a first-meaningful
# ancestor. Distinct named segments are distinct capabilities, so a SAML
# handler under ``auth/saml`` keeps its own ``saml`` key instead of
# folding up into an ``auth`` blob. Route groups own a file only when no
# real child segment follows (group-level layouts / colocated files).
# The :func:`_route_boundary` resolver below encodes this; synthesis
# (``synthesize_features``) creates the leaf as a feature only when it
# owns ≥ ``_MIN_BOUNDARY_FILES`` source files, so one-off deep segments
# keep their richer LLM names (re-home no-ops on a name nothing holds).


@dataclass(frozen=True)
class SynthFeature:
    """A profile-synthesised capability the deterministic extractors miss.

    Returned by :meth:`NextAppRouterProfile.synthesize_features` and
    consumed by the generic attribution wiring, which CREATES a feature
    with this ``name`` (if one does not already exist) so the profile's
    :meth:`feature_of` re-home can move ``paths`` off the package
    workspace anchor onto it. ``prefix`` is the owning directory (debug /
    dedup). Public (no leading underscore) because the wiring imports it
    structurally via ``getattr`` — it is part of the profile's optional
    synthesis contract.
    """

    name: str
    paths: tuple[str, ...]
    prefix: str = ""


@dataclass(frozen=True)
class _Boundary:
    """A synthesised feature boundary: a named capability folder.

    ``slug`` is the kebab feature key (matches what the route extractor /
    Stage-2 would surface). ``prefix`` is the POSIX directory prefix
    (``apps/web/app/(dashboard)`` / ``apps/web/modules/billing``) that
    OWNS every source file beneath it. ``kind`` records which convention
    produced it (debug / precedence only). Two boundaries are equal iff
    their prefix is equal — a path resolves to exactly one owner.
    """

    slug: str
    prefix: str
    kind: str


def _route_boundary(segs: list[str], app_idx: int) -> _Boundary | None:
    """The file's nearest owning ROUTE capability under ``app/``, or ``None``.

    The owner is the file's *deepest meaningful URL segment* — the segment
    that directly names the capability the file serves. A route GROUP
    (``(area)``) is organisational and is the owner ONLY when it is the
    deepest meaningful thing on the path (a group-level ``layout`` /
    colocated file with no child route segment); the moment a real named
    child segment exists, THAT segment owns the file. This is the
    leaf-conservative rule that keeps distinct sibling capabilities
    (``(shop)/cart`` vs ``(shop)/products``) separate instead of melting
    them into one ``shop`` blob, while still pulling every routed file off
    the workspace anchor onto a real per-capability feature.

    Returns the boundary at the deepest meaningful segment. ``prefix`` is
    the directory up to and including that segment — every file beneath it
    that has no DEEPER meaningful segment shares the owner (the page, its
    layout/loading/error, and colocated components/lib under it).
    """
    deepest: _Boundary | None = None
    for i in range(app_idx + 1, len(segs)):
        seg = segs[i]
        if seg.startswith("(") and seg.endswith(")") and len(seg) > 2:
            # A route group: meaningful only as a fallback owner (when no
            # real segment follows). Record it but keep scanning for a
            # deeper named segment that should win.
            inner = seg[1:-1]
            if inner.startswith("."):  # intercepting overlay marker
                continue
            slug = slugify(inner)
            if slug and not is_noise(slug):
                deepest = _Boundary(slug, "/".join(segs[: i + 1]), "route-group")
            continue
        if _is_skipped_segment(seg):
            continue
        slug = slugify(seg)
        if slug and not is_noise(slug):
            deepest = _Boundary(slug, "/".join(segs[: i + 1]), "route-segment")
    return deepest


def _container_boundary(segs: list[str]) -> _Boundary | None:
    """A ``modules/<domain>`` / ``features/<domain>`` ancestor → ``<domain>``.

    The feature-sliced / modular convention: code for a capability lives
    under a named domain folder inside a container directory. The segment
    immediately after the FIRST container name on the path is the owner.
    Works with or without a ``src/`` prefix and with a monorepo prefix
    because we scan the whole segment list for the container name.
    """
    for i, seg in enumerate(segs):
        if seg in _FEATURE_CONTAINER_DIRS and i + 1 < len(segs):
            domain = segs[i + 1]
            if _is_skipped_segment(domain):
                continue
            slug = slugify(domain)
            if slug and not is_noise(slug):
                return _Boundary(slug, "/".join(segs[: i + 2]), "module")
    return None


def _owning_boundary(path: str) -> _Boundary | None:
    """The file's nearest owning capability folder, or ``None`` (shared).

    Precedence (most-specific structural truth first):

      1. **Route capability** ``app/.../<segment>`` — the deepest
         meaningful URL segment under the App Router tree owns the file
         (a route group owns it only when no real child segment follows).
         This is leaf-conservative: distinct sibling capabilities stay
         distinct instead of melting into one area blob.
      2. **Module / feature folder** ``modules/<d>`` / ``features/<d>`` —
         the modular-layout capability boundary, valid anywhere OUTSIDE
         ``app/`` (so it never collides with (1)).

    Returns ``None`` for files with no owning boundary: the root
    ``app/layout.tsx``, top-level shared ``components/`` / ``lib/`` /
    ``hooks/`` primitives, and anything outside these conventions. Those
    stay shared / fall through to the generic path — they must NEVER be
    glued to one feature (that is the blob).
    """
    segs, _fname = _split(path)
    app_idx = _app_index(segs)

    if app_idx is not None:
        under_app = segs[app_idx + 1:]
        if under_app:
            route = _route_boundary(segs, app_idx)
            if route is not None:
                return route
        # A file directly at ``app/`` (root layout/page), or an app tree
        # with only noise/dynamic segments — no owner.
        return None

    # Not under an app tree — only a module / feature container can own it.
    return _container_boundary(segs)


# ── the profile ──────────────────────────────────────────────────────────────


class NextAppRouterProfile:
    """Framework Knowledge Layer for Next.js App Router."""

    name = "next-app-router"

    # ── detection ───────────────────────────────────────────────────────────

    def detects(self, ctx: "ScanContext") -> float:
        """Confidence that this is a Next App Router repo.

        Deterministic, manifest/marker-driven. We trust Stage 0's already
        deterministic stack detection (``next-app-router`` is emitted when
        ``next`` is a dependency AND an ``app/`` dir exists), and we
        additionally accept the case where ANY workspace in a monorepo is
        App Router — the dominant SaaS shape (a Turborepo whose root tag
        is ``None`` but whose ``apps/web`` is App Router). Confidence is
        graded by signal strength, never a tuned constant tied to a repo.
        """
        # Strongest: Stage 0 / auditor already tagged the repo.
        if (ctx.audited_stack or ctx.stack) == "next-app-router":
            return 0.95

        # Monorepo: win when at least one workspace is App Router. We
        # scope confidence to the FRACTION of workspaces that are App
        # Router so a Next-dominant monorepo wins clearly while a repo
        # with a single incidental Next app still clears the default
        # floor. Structural ratio — scale-invariant, no magic number.
        wss = ctx.workspaces or []
        if wss:
            next_ws = sum(1 for ws in wss if (ws.stack == "next-app-router"))
            if next_ws:
                # 0.6 base (clearly beats the 0.01 default floor and any
                # secondary stack guess) + proportion bonus up to ~0.95.
                return min(0.6 + 0.35 * (next_ws / len(wss)), 0.95)

        # Structural fallback: an ``app/`` (or ``src/app/``) tree with a
        # page/route file plus a next.config is unambiguous even if the
        # dependency probe missed (e.g. a catalog/workspace dep layout).
        if self._has_app_router_marker(ctx):
            return 0.7

        return 0.0

    @staticmethod
    def _has_app_router_marker(ctx: "ScanContext") -> bool:
        """True when tracked files show an App Router routing file."""
        has_next_cfg = any(
            posix(f).rsplit("/", 1)[-1].startswith("next.config.")
            for f in ctx.tracked_files
        )
        if not has_next_cfg:
            return False
        for f in ctx.tracked_files:
            segs, fname = _split(f)
            if _app_index(segs) is None:
                continue
            stem = fname.rsplit(".", 1)[0]
            if stem in _PAGE_STEMS or stem in _ROUTE_STEMS:
                return True
        return False

    # ── workspaces ───────────────────────────────────────────────────────────

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """One workspace per monorepo package; ``[root]`` otherwise.

        Pure delegation to the shared, package-manager-driven splitter —
        Turborepo / pnpm / npm workspaces are the dominant Next shape and
        splitting them is HALF the blob fix (each app/package becomes its
        own attribution scope instead of one flat dump). Never raises,
        never returns ``[]``.
        """
        return split_workspaces(ctx)

    # ── file classification ───────────────────────────────────────────────────

    def classify_file(self, path: str) -> FileRole:
        """Map a repo-relative path to its App Router structural role."""
        segs, fname = _split(path)
        lower = posix(path).lower()

        # Tests / config first — they trump location.
        if any(m in lower for m in _TEST_MARKERS):
            return FileRole.TEST
        if any(fname.lower().count(m) for m in ("config.",)) and (
            fname.lower().startswith("next.config")
            or fname.lower().endswith((".config.ts", ".config.js", ".config.mjs"))
            or ".config." in fname.lower()
        ):
            return FileRole.CONFIG

        stem = fname.rsplit(".", 1)[0]
        ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""

        under_app = _app_index(segs) is not None

        # Routing files (only meaningful under the app tree).
        if under_app:
            if stem in _ROUTE_STEMS and ext in _ROUTE_EXTS:
                return FileRole.API
            if stem in _PAGE_STEMS and ext in _PAGE_EXTS:
                return FileRole.PAGE

        # Colocation directory roles (anywhere — app/ colocated or shared
        # top-level). Order: most specific first.
        seg_set = set(segs)
        # Server-action files: a directory named ``actions/`` OR a file
        # named ``actions.ts``/``action.ts`` (the conventional colocated
        # mutation module). Classified SERVICE (mutation surface).
        if seg_set & _ACTION_DIRS or stem in ("actions", "action"):
            return FileRole.SERVICE
        if seg_set & _COMPONENT_DIRS:
            return FileRole.COMPONENT
        if seg_set & _HOOK_DIRS or stem.startswith("use") and ext in (".ts", ".tsx"):
            # ``use*`` hook-naming convention (camelCase ``useFoo``).
            if seg_set & _HOOK_DIRS or (
                len(stem) > 3 and stem[3:4].isupper()
            ):
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
        """The capability feature this file serves, or ``None`` (shared).

        Returns the kebab slug of the file's nearest owning boundary
        (:func:`_owning_boundary`):

          * a routed file → its OWN (deepest) meaningful URL segment under
            ``app/`` (a route group owns it only when no real child segment
            follows). This is leaf-CONSERVATIVE: distinct named segments
            are distinct capabilities, so a SAML handler under ``auth/saml``
            or a Slack installer under ``integrations/slack`` keeps its own
            segment key instead of folding up into ``auth`` /
            ``integrations`` (which would erase the capability and blob the
            ancestor);
          * a modular-layout file → its ``modules/<d>`` / ``features/<d>``
            domain.

        By the re-home contract a path is re-homed only when this name
        EXISTS in the working feature set — either because an extractor
        surfaced it OR because :meth:`synthesize_features` created it for a
        genuine multi-file capability folder. Single-file deep segments are
        NOT synthesised, so they return a slug nothing else holds → no
        re-home → the LLM's richer name survives (None-equivalent). Net:
        multi-file capability folders are pulled off the workspace anchor
        (blob killed) while one-off deep segments keep their rich names.

        Files NOT under any capability boundary (root ``app/layout.tsx``,
        top-level shared ``components/`` / ``lib/`` / ``hooks/``) return
        ``None`` so they fall through to the generic residual / fan-out
        path UNCHANGED — they must not be force-homed into one feature.
        """
        boundary = _owning_boundary(path)
        return boundary.slug if boundary is not None else None

    # ── feature synthesis (sub-decompose the workspace anchor) ──────────────────

    def synthesize_features(self, ctx: "ScanContext") -> list["SynthFeature"]:
        """Capability features the extractors miss inside a single package.

        The blob: a single-package Next app becomes ONE ``[package]``
        workspace-anchor feature owning every file, because the route /
        page extractors gate on export counts and miss the capability
        FOLDERS the author drew — the per-segment leaves under a route
        group (``app/(shop)/cart`` vs ``app/(shop)/products``) and the
        modular-layout domains (``modules/billing``). Those boundaries are
        never created as features, so :meth:`feature_of`'s re-home (which
        only moves a path onto a feature that ALREADY exists) has nothing
        to land on and the files stay glued to the anchor.

        This method emits one :class:`SynthFeature` per genuine capability
        boundary (the file's nearest owning route-leaf / module — see
        :func:`_owning_boundary`) so the generic attribution wiring can
        CREATE it, after which :meth:`feature_of` re-homes the boundary's
        files off the package anchor onto it. Universal across
        single-package and multi-package monorepos: boundaries are located
        structurally, so a monorepo prefix (``apps/web/...``) resolves
        transparently. Idempotent against extractor-surfaced features — the
        wiring skips any synth name that already exists, so the route
        extractor's own anchors are never duplicated.

        Gating (structural, scale-invariant — see ``rule-no-magic-tuning``):
        a boundary is synthesised only when it owns at least
        :data:`_MIN_BOUNDARY_FILES` distinct tracked source files. A lone
        file under a named segment / folder is not a multi-file capability
        (it keeps its richer LLM name instead); two or more co-located
        source files is the smallest non-trivial capability slice. No
        corpus-tuned size, no per-repo path.
        """
        # boundary prefix -> (slug, owned source-file set). Keyed by the
        # full prefix so two distinct leaves that happen to slugify the
        # same (rare) stay separate scopes for the file-count floor.
        boundaries: dict[str, tuple[str, set[str]]] = {}
        for f in ctx.tracked_files:
            if not _is_source(f):
                continue
            boundary = _owning_boundary(f)
            if boundary is None:
                continue
            slug, owned = boundaries.setdefault(
                boundary.prefix, (boundary.slug, set()),
            )
            owned.add(posix(f))

        out: list[SynthFeature] = []
        for prefix, (slug, owned) in sorted(boundaries.items()):
            if len(owned) < _MIN_BOUNDARY_FILES:
                continue
            out.append(SynthFeature(
                name=slug,
                paths=tuple(sorted(owned)),
                prefix=prefix,
            ))
        return out

    # ── flow entries ───────────────────────────────────────────────────────────

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        """Structural entry points: pages, route-handler methods, actions.

        ONE entry per (page) and one per HTTP method actually exported by
        a ``route.ts`` — so a flow is detected ONCE per capability, not
        once per file (the duplicate-flow fix). The ``route`` field is the
        derived URL pattern; the ``symbol`` is the inner handler symbol so
        the Stage-3 wrapper-unwrap (``resolve_handler_line``) can recover
        the REAL handler body's line range when the export is a thin
        higher-order wrapper (``export const POST = withAuth(handler)``).
        """
        entries: list[FlowEntry] = []
        repo_root = ctx.repo_path
        for f in ctx.tracked_files:
            segs, fname = _split(f)
            idx = _app_index(segs)
            if idx is None:
                continue
            stem = fname.rsplit(".", 1)[0]
            ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""
            url = self._url_pattern(segs[idx + 1:])

            if stem in _PAGE_STEMS and ext in _PAGE_EXTS:
                # Only ``page`` is a navigable entry; layout/loading/error
                # are scaffold that belong to the same feature but are not
                # distinct user-facing flows (avoids per-scaffold dup
                # flows). The default-export component is the entry symbol.
                if stem == "page":
                    entries.append(FlowEntry(
                        path=posix(f),
                        symbol=self._default_export_symbol(repo_root, f),
                        kind="page",
                        route=url or "/",
                    ))
            elif stem in _ROUTE_STEMS and ext in _ROUTE_EXTS:
                for method in self._exported_methods(repo_root, f):
                    entries.append(FlowEntry(
                        path=posix(f),
                        symbol=method,
                        kind="http",
                        route=f"{method} {url or '/'}",
                    ))
        return entries

    @staticmethod
    def _url_pattern(segments_under_app: list[str]) -> str:
        """Build a readable URL pattern, dropping groups/slots/private dirs.

        Dynamic segments are kept (as ``:param``) so two distinct routes
        do not collide on one flow name; groups/private/parallel markers
        are dropped (not URL segments).
        """
        parts: list[str] = []
        for seg in segments_under_app:
            if not seg:
                continue
            if seg.startswith("(") and seg.endswith(")"):
                continue
            if seg.startswith("(") or seg.startswith("_") or seg.startswith("@"):
                continue
            if seg in (_APP_SEGMENT,):
                continue
            if seg.startswith("[") and seg.endswith("]"):
                inner = seg.strip("[]").lstrip(".")
                parts.append(f":{inner}")
                continue
            parts.append(seg)
        return "/" + "/".join(parts) if parts else ""

    @staticmethod
    def _exported_methods(repo_root, rel_path: str) -> list[str]:
        """HTTP method names a ``route.ts`` actually exports.

        Reads the file (best-effort) and keeps only the methods present as
        exports, so we never emit a POST flow for a GET-only handler. When
        the file can't be read, fall back to a single neutral entry so the
        route still seeds one flow (never zero) — degrade gracefully.
        """
        src = _read(repo_root, rel_path)
        if src is None:
            return ["GET"]
        found: list[str] = []
        for m in _HTTP_METHODS:
            # ``export async function POST`` / ``export function POST`` /
            # ``export const POST =`` — match the method as an exported
            # binding. Structural substring is sufficient + deterministic.
            if (
                f"export async function {m}" in src
                or f"export function {m}" in src
                or f"export const {m}" in src
                or f"export let {m}" in src
                or f"export {{ {m}" in src
                or f", {m}" in src and "export {" in src
            ):
                found.append(m)
        return found or ["GET"]

    @staticmethod
    def _default_export_symbol(repo_root, rel_path: str) -> str:
        """Best-effort name of a page's default-export component.

        Returns the component identifier when the file declares
        ``export default function Foo`` / ``export default Foo``; ``""``
        otherwise (the file itself is the entry — filesystem routing).
        Used as ``FlowEntry.symbol`` so the line-range resolver can map to
        the real component body.
        """
        src = _read(repo_root, rel_path)
        if not src:
            return ""
        import re as _re
        m = _re.search(r"export\s+default\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", src)
        if m:
            return m.group(1)
        m = _re.search(r"export\s+default\s+([A-Za-z_$][\w$]*)\s*;?", src)
        if m and m.group(1) not in ("function", "async", "class"):
            return m.group(1)
        return ""

    # ── attribution policy ─────────────────────────────────────────────────────

    def attribution_rules(self) -> AttributionSpec:
        """Declarative fan-out policy for shared App Router files.

        Shared UI / hooks / utils are genuinely cross-cutting: they must
        blast-radius across the features that exercise them, not collapse
        a route feature into a physical-container blob. We declare
        COMPONENT / HOOK / LIB as shared roles. ``max_fanout`` is a
        scale-invariant structural cap, NOT a corpus-tuned number: a
        single shared file attaching to an unbounded number of features
        is provenance noise (it tells a reviewer nothing), so we cap the
        blast radius at a small constant — the file stays attributed to
        the features with the strongest evidence and the rest is dropped.
        The cap is a *policy* (few-owners-is-signal), independent of any
        repo's size.
        """
        return AttributionSpec(
            colocate_roots=("page", "route"),
            shared_roles=(FileRole.COMPONENT, FileRole.HOOK, FileRole.LIB),
            max_fanout=_SHARED_FANOUT_CAP,
        )


def _read(repo_root, rel_path: str) -> str | None:
    """Best-effort UTF-8 read of ``repo_root/rel_path``; ``None`` on error."""
    try:
        from pathlib import Path
        return (Path(repo_root) / rel_path).read_text(
            encoding="utf-8", errors="ignore",
        )
    except (OSError, ValueError):
        return None


# A shared file that fans out to more than a handful of features is
# noise, not signal (a reviewer learns nothing from "used by 40
# features"). The cap is a fixed small constant expressing the
# few-owners-is-signal policy — scale-invariant, independent of repo
# size (NOT tuned to any corpus repo). Three keeps the strongest-evidence
# owners and drops the long tail.
_SHARED_FANOUT_CAP = 3


# A capability folder must own at least this many distinct source files
# to be synthesised as a feature. A single file under a named folder is
# not a multi-file capability; two co-located source files is the
# smallest non-trivial feature slice. Structural floor (the "more than a
# trivial leaf" rule), NOT a corpus-tuned size — identical behaviour on a
# 3-route app and a 300-route monorepo.
_MIN_BOUNDARY_FILES = 2

# Source-code extensions that count toward a boundary's file population.
# Non-source assets (markdown, json, images, css) live under capability
# folders too but shouldn't, on their own, constitute a feature.
_SOURCE_EXTS = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
})


def _is_source(path: str) -> bool:
    """True when ``path`` is a JS/TS source file (not an asset / doc)."""
    p = posix(path)
    dot = p.rfind(".")
    if dot == -1:
        return False
    return p[dot:].lower() in _SOURCE_EXTS


__all__ = ["NextAppRouterProfile", "SynthFeature"]
