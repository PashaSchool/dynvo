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


def _meaningful_segments(segments_under_app: list[str]) -> list[str]:
    """The ordered list of meaningful (URL-bearing) segments, kebab-slugged.

    Drops everything that is NOT a distinct named capability segment:
    route groups, private folders, parallel slots, intercepting markers,
    dynamic params, and framework-noise tokens (``api`` etc.). What
    survives is the sequence of *author-named* path segments — each of
    which is a candidate capability name.
    """
    out: list[str] = []
    for seg in segments_under_app:
        if _is_skipped_segment(seg):
            continue
        slug = slugify(seg)
        if slug and not is_noise(slug):
            out.append(slug)
    return out


def _leaf_feature_slug(segments_under_app: list[str]) -> str | None:
    """The file's OWN (deepest) meaningful segment → kebab slug, or ``None``.

    CONSERVATIVE attribution. A routing file sits inside a chain of named
    segments (``auth/saml``, ``integrations/slack``, ``teams/[id]/saml``).
    The *first* meaningful segment is an organisational ANCESTOR — folding
    a file up into it erases the distinct child capability (the SAML
    handler becomes part of ``auth``; the Slack installer becomes part of
    a generic ``integrations`` blob). That over-consolidation is the
    flip-side of the blob fix.

    Distinct named segments = distinct capabilities. So we return the
    *deepest* meaningful segment — the one that directly owns the routing
    file — NOT the first. By the re-home contract (a path is only re-homed
    when the returned name already exists in the Stage-2 feature set) this
    is strictly safe:

      * When the file is shallow (its own segment IS the first meaningful
        one, e.g. ``settings/page.tsx`` → ``settings``), leaf == first, so
        a genuinely-colocated capability still groups under its anchor.
      * When the file lives in a DISTINCT deeper segment (``saml`` /
        ``slack`` / ``scim``) the route extractor never surfaced that
        deep segment as an anchor, so the leaf slug does not exist → no
        re-home → the LLM's richer capability name SURVIVES (the desired
        conservative behaviour: ``None``-equivalent for re-home).

    Falls back to a leaf route-group's inner name when stripping removes
    every segment (``app/(home)/page.tsx`` → ``home``), matching the
    extractor's Sprint-D3 recovery — but only when there is no meaningful
    URL segment at all (a root-level group page), never to OVERRIDE a real
    named segment.
    """
    meaningful = _meaningful_segments(segments_under_app)
    if meaningful:
        return meaningful[-1]
    # Recovery: a leaf route group carries a meaningful name (only when no
    # real URL segment exists).
    for seg in reversed(segments_under_app):
        if seg.startswith("(") and seg.endswith(")") and len(seg) > 2:
            slug = slugify(seg[1:-1])
            if slug and not is_noise(slug):
                return slug
    return None


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
        """The route-segment feature this App Router file serves, or ``None``.

        CONSERVATIVE: returns the kebab slug of the file's OWN (deepest)
        meaningful URL segment under ``app/`` — NOT the first-meaningful
        ancestor. Distinct named segments are distinct capabilities, so a
        SAML handler under ``auth/saml`` or a Slack installer under
        ``integrations/slack`` keeps its own segment key instead of being
        folded up into the ancestor route feature (``auth`` /
        ``integrations``) — which would erase the capability and turn the
        ancestor into a blob.

        By the re-home contract (a path is re-homed only when the returned
        name already EXISTS in the Stage-2 feature set) this is strictly
        safe in both directions:
          * shallow files (own segment == first meaningful) group under
            their existing route anchor exactly as before;
          * files in a distinct deeper segment return a leaf slug the
            route extractor never surfaced, so no re-home fires and the
            LLM's richer capability name SURVIVES (None-equivalent).

        Files NOT under an app tree (shared libs, top-level components)
        return ``None`` so they fall through to the generic residual /
        fan-out path UNCHANGED — they must not be force-homed into one
        route feature (that is the blob).
        """
        segs, _fname = _split(path)
        idx = _app_index(segs)
        if idx is None:
            return None
        under_app = segs[idx + 1:]
        if not under_app:
            # A file directly at ``app/`` (root layout/page) — the root
            # capability; no segment slug to attach to. Let it fall
            # through rather than invent a name.
            return None
        return _leaf_feature_slug(under_app)

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


__all__ = ["NextAppRouterProfile"]
