"""React-Router-framework / Remix :class:`FrameworkProfile` (B44).

The engine's deterministic understanding of a **React Router v7 framework
mode** app (the Remix successor) and classic **Remix**: a package that
declares file-based routing under ``app/routes/**`` and mounts it through
``react-router.config.*`` / ``@react-router/*`` (or the legacy
``@remix-run/*``) packages.

Why a dedicated profile — the Stage-3 keyless blind spot
--------------------------------------------------------

The stock ``route`` extractor ALREADY reads the ``app/routes/**``
convention (``filesystem-routing.yaml`` ``remix`` / ``react-router``
rows) and populates ``routes_index`` for these repos. But route
extraction seeds ANCHORS (features), not flows: in a keyless scan the
ONLY deterministic flow source is ``profile.flow_entries`` (Stage 3's
``_profile_flows_by_feature``), and a react-router-framework workspace
won the :class:`DefaultProfile`, whose ``flow_entries`` is ``[]``. So a
documenso ``apps/remix`` with 121 live routes shipped 0 flows — every
one of its route files sat feature-owned but flow-less, and the B47
arm-B e2e-orphan bridge (route → handler → flow → UF) had no flow to
graduate on.

This profile closes the hole the same way the two Next profiles do: it
seeds ONE :class:`FlowEntry` per ``app/routes/**`` route file, anchored
on the file's default-export component symbol (the B41/B43 anchor
completeness chain, reused) so the flow gets a real ``(file, line)``
span instead of shipping hollow.

Structural model (framework docs, never a repo README):

  * **Routing root** — ``app/routes/**`` (React Router framework mode /
    Remix / ``remix-flat-routes``). Flat-route filenames carry dot and
    ``+`` folder segments (``_authenticated+/dashboard.tsx``,
    ``t.$teamUrl+/...``); the URL derivation is the SAME on-disk mapping
    the stock ``routes_index`` builder uses (``indexes._derive_route_
    from_path`` — the ``("app","routes")`` root), so a profile-seeded
    flow's route matches its ``routes_index`` entry.
  * **API routes** — files under an ``api`` / ``api+`` route segment are
    HTTP surfaces (``kind="http"``); everything else is a navigable
    ``page``.
  * **Shell** — ``root.tsx`` / ``entry.client`` / ``entry.server`` /
    ``routes.ts`` (the flat-routes config) are framework wiring, never a
    capability; they seed nothing.

Detection is framework-mode-ONLY (a ``react-router.config.*`` file OR an
``@react-router/*`` / ``@remix-run/*`` package): the classic
``react-router-dom`` LIBRARY SPA (outline: in-source ``<Route>``
elements, no ``app/routes/`` file convention) is DELIBERATELY not claimed
here — it belongs to :class:`NextPagesReactProfile`, which owns the
in-source router grammar. The two never overlap.

Deterministic — NO LLM, NO network. Universal — no corpus paths.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import posix, read_json
from faultline.pipeline_v2.profiles._pages_surface import (
    _is_excluded_path,
    _is_js_source,
    _segments,
    default_export_symbol,
)
from faultline.pipeline_v2.profiles._splitter import split_workspaces
from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


#: B44 flag — this profile is registered ONLY when set. Default OFF ⇒ a
#: react-router-framework unit keeps falling to the DefaultProfile and the
#: board is byte-identical (regression guard). Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS``.
REACT_ROUTER_FW_PROFILE_ENV = "FAULTLINE_REACT_ROUTER_FW_PROFILE"


def react_router_fw_profile_enabled() -> bool:
    """``True`` when ``FAULTLINE_REACT_ROUTER_FW_PROFILE`` is set truthy."""
    return os.environ.get(REACT_ROUTER_FW_PROFILE_ENV, "0").strip() not in {
        "", "0", "false", "False",
    }


#: The routing-root component run. Matched ANYWHERE in a path so a
#: monorepo workspace prefix (``apps/remix/app/routes/...``) or a
#: unit-scoped path (``app/routes/...``) both resolve.
_ROUTES_ROOT = ("app", "routes")

#: Framework-wiring stems under ``app/`` that are never a capability: the
#: app shell (``root``, ``entry.*``), the flat-routes config (``routes``),
#: and pathless LAYOUT wrappers (``_layout`` / ``__layout`` — they wrap
#: children, they are not a navigable surface). The index route ``_index``
#: is deliberately NOT here (it IS the navigable index page).
_SHELL_STEMS = frozenset({
    "root", "routes", "entry.client", "entry.server", "entry",
    "_layout", "__layout",
})

#: Dependency-name markers of framework mode (runtime OR dev). ANY of
#: these + an ``app/routes/`` tree, or a ``react-router.config.*`` file,
#: is unambiguous framework mode.
_FW_DEP_PREFIXES = ("@react-router/", "@remix-run/")

#: Workspace stack tags Stage 0 / the auditor emit for this stack.
_FW_STACK_TAGS = frozenset({"react-router", "remix"})

#: Structural role directory tables — ecosystem-standard placeholder
#: names, identical vocabulary to the Next profiles.
_COMPONENT_DIRS = frozenset({"components", "ui", "primitives"})
_HOOK_DIRS = frozenset({"hooks"})
_LIB_DIRS = frozenset({"lib", "libs", "utils", "util", "helpers"})
_SERVICE_DIRS = frozenset({"services", "server", "data", "queries", "store"})
_DOMAIN_DIRS = frozenset({"models", "schemas", "domain", "entities"})
_TEST_MARKERS = (".test.", ".spec.", "/__tests__/", "/tests/", "/e2e/")

#: I/O safety valve for huge manifests (candidate lists priority-ordered).
_MAX_MANIFEST_READS = 100


def _is_test_path(path: str) -> bool:
    return any(m in posix(path).lower() for m in _TEST_MARKERS)


def _routes_rest(path: str) -> list[str] | None:
    """Segments AFTER the first ``app/routes`` component run, or ``None``.

    ``apps/remix/app/routes/_authenticated+/dashboard.tsx`` →
    ``["_authenticated+", "dashboard.tsx"]``. Searches the whole path so a
    monorepo prefix or a unit-scoped path both resolve.
    """
    p = posix(path)
    segs = [s for s in p.split("/") if s]
    for i in range(len(segs)):
        if segs[i:i + len(_ROUTES_ROOT)] == list(_ROUTES_ROOT):
            return segs[i + len(_ROUTES_ROOT):]
    return None


def _route_file_stem(fname: str) -> str:
    """The first dot-token of a route filename (``dashboard.tsx`` →
    ``dashboard``; ``t.$teamUrl.tsx`` → ``t``)."""
    return fname.split(".", 1)[0]


#: Route-file extensions to strip when deriving the URL.
_ROUTE_EXTS = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs", ".mdx", ".md")


def _route_pattern(rest: list[str]) -> str:
    """A best-effort URL pattern for a ``app/routes/**`` file (flat-routes).

    ``["_authenticated+", "admin+", "documents.$id.tsx"]`` → ``/admin/
    documents/:id``. Remix / ``remix-flat-routes`` conventions handled:
    ``+`` folder-group suffixes and ``_``-prefixed pathless layout
    segments drop, dot tokens in the filename are path separators,
    ``$param`` → ``:param``, ``_index`` is the parent's index. Used ONLY
    to name the seeded flow (``feature_of`` is opinion-free), so an
    imperfect pattern only affects a flow's display name, never
    attribution.
    """
    segs: list[str] = []

    def _emit(tok: str) -> None:
        tok = tok.strip()
        if not tok or tok in ("_index", "index", "route", "_layout"):
            return
        if tok.startswith(("_", "(")):
            return  # pathless layout / route group — URL-invisible
        if tok.startswith("$"):
            segs.append(":" + tok[1:])
        else:
            segs.append(tok)

    for d in rest[:-1]:
        _emit(d.rstrip("+"))
    if rest:
        fname = rest[-1]
        stem = fname
        for ext in _ROUTE_EXTS:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        for tok in stem.split("."):
            _emit(tok.rstrip("+"))
    return "/" + "/".join(segs) if segs else "/"


class ReactRouterFrameworkProfile:
    """Framework Knowledge Layer for React Router framework mode / Remix."""

    name = "react-router-fw"

    # ── detection ───────────────────────────────────────────────────────────

    def detects(self, ctx: "ScanContext") -> float:
        """Confidence this is a React-Router-framework / Remix repo.

        Framework-mode fingerprints only, graded by SCOPE so a frontend
        sub-app never steals a monorepo's root from a flow-generating
        backend:

          * **Whole-repo monorepo scope** (``ctx.workspaces`` present) —
            ``0.6 + 0.35·fraction`` of workspaces that ARE
            react-router-framework, capped 0.95 (mirrors
            NextAppRouterProfile). This is the B44-v2 guard: a repo whose
            ROOT is a backend (plane: django serving ``apps/api`` via the
            residue) with a few react-router sub-apps yields a MODERATE
            score — react-router-fw wins those sub-apps as UNITS but the
            backend keeps the root, so its residue flows survive. A flat
            0.9 here (the v1 bug) flipped plane's root django →
            react-router-fw and vaporised 384 apps/api flows.
          * **Unit / single-package scope** (``ctx.workspaces`` empty —
            the per-unit selector passes a workspace-less scoped ctx) —
            0.9 on a ``react-router.config.*`` file OR ``@react-router/*``
            / ``@remix-run/*`` dep together with an ``app/routes/`` tree.
            The scope IS the app, so strong local evidence wins it.
          * 0.0 — otherwise (never wins; classic ``react-router-dom``
            LIBRARY SPAs are owned by NextPagesReactProfile).
        """
        wss = ctx.workspaces or []
        tracked = [posix(f) for f in ctx.tracked_files]

        if wss:
            rr = sum(1 for ws in wss if self._ws_is_react_router_fw(ctx, ws, tracked))
            if rr:
                return min(0.6 + 0.35 * (rr / len(wss)), 0.95)
            return 0.0

        # Unit scope (workspaces=None) or single-package repo.
        if not any(_routes_rest(f) is not None for f in tracked):
            return 0.0
        has_config = any(
            posix(f).rsplit("/", 1)[-1].startswith("react-router.config.")
            for f in tracked
        )
        if has_config or self._has_fw_dep(ctx, tracked):
            return 0.9
        return 0.0

    def _ws_is_react_router_fw(
        self, ctx: "ScanContext", ws: "Workspace", tracked: list[str],
    ) -> bool:
        """A workspace is react-router-framework iff its subtree has an
        ``app/routes/`` tree AND (a ``react-router.config.*`` file OR an
        ``@react-router/*`` / ``@remix-run/*`` dep), or the auditor tagged
        it ``react-router`` / ``remix``."""
        if (ws.stack or "").lower() in _FW_STACK_TAGS:
            return True
        if not ws.path:
            return False
        prefix = ws.path.rstrip("/") + "/"
        ws_files = [f for f in tracked if f.startswith(prefix)]
        if not any(_routes_rest(f) is not None for f in ws_files):
            return False
        if any(
            f.rsplit("/", 1)[-1].startswith("react-router.config.")
            for f in ws_files
        ):
            return True
        return self._has_fw_dep(ctx, ws_files)

    def _has_fw_dep(self, ctx: "ScanContext", tracked: list[str]) -> bool:
        reads = 0
        for f in sorted(tracked):
            if f.rsplit("/", 1)[-1] != "package.json" or _is_excluded_path(f):
                continue
            if reads >= _MAX_MANIFEST_READS:
                break
            doc = read_json(ctx.repo_path / f)
            reads += 1
            if not isinstance(doc, dict):
                continue
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                block = doc.get(key)
                if isinstance(block, dict) and any(
                    d.startswith(_FW_DEP_PREFIXES) for d in block
                ):
                    return True
        return False

    # ── workspaces ───────────────────────────────────────────────────────────

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """Pure delegation to the shared package-manager splitter."""
        return split_workspaces(ctx)

    # ── file classification ───────────────────────────────────────────────────

    def classify_file(self, path: str) -> FileRole:
        p = posix(path)
        segs, fname = _segments(p)
        if _is_test_path(p):
            return FileRole.TEST
        low = fname.lower()
        if low.startswith(("react-router.config.", "vite.config.")) or (
            ".config." in low
        ):
            return FileRole.CONFIG

        rest = _routes_rest(p)
        if rest is not None and _is_js_source(p):
            if _route_file_stem(fname) in _SHELL_STEMS:
                return FileRole.CONFIG
            # An ``api`` / ``api+`` route segment is an HTTP surface.
            if any(seg.rstrip("+") == "api" for seg in rest[:-1]) or (
                _route_file_stem(fname).rstrip("+") == "api"
            ):
                return FileRole.API
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

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:  # noqa: ARG002
        """Opinion-free: the stock ``route`` extractor already buckets
        ``app/routes/**`` files into features, so this profile does NOT
        re-home — it purely SEEDS flows onto those extractor-owned
        features (via ``flow_entries`` + Stage-3 path-ownership). Returning
        ``None`` for every path keeps feature composition byte-identical to
        the DefaultProfile: B44 adds flows, it does not reshuffle features.
        """
        return None

    # ── flow entries ───────────────────────────────────────────────────────────

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        """One :class:`FlowEntry` per ``app/routes/**`` route file.

        The keystone of B44: without this a react-router-framework unit
        ships flow-less in keyless mode. Files under an ``api`` route
        segment are ``kind="http"``; the rest are ``kind="page"``. Each
        entry's ``symbol`` is the file's default-export component (the
        B41/B43 anchor-completeness chain via ``default_export_symbol``) so
        the Stage-3 seed resolves a real ``(file, line)`` span instead of
        hollow. Shell/config files seed nothing.
        """
        entries: list[FlowEntry] = []
        seen: set[tuple[str, str]] = set()
        for f in sorted(posix(x) for x in ctx.tracked_files):
            rest = _routes_rest(f)
            if rest is None or not _is_js_source(f):
                continue
            if _is_excluded_path(f) or _is_test_path(f):
                continue
            _segs, fname = _segments(f)
            stem = _route_file_stem(fname)
            if stem in _SHELL_STEMS:
                continue
            route = _route_pattern(rest)
            is_api = any(seg.rstrip("+") == "api" for seg in rest[:-1]) or (
                stem.rstrip("+") == "api"
            )
            key = (f, route)
            if key in seen:
                continue
            seen.add(key)
            entries.append(FlowEntry(
                path=f,
                symbol=default_export_symbol(ctx.repo_path, f),
                kind="http" if is_api else "page",
                route=route,
            ))
        return entries

    # ── attribution policy ─────────────────────────────────────────────────────

    def attribution_rules(self) -> AttributionSpec:
        """Empty policy — the pure-flow-seeder contract (see
        :meth:`feature_of`). An empty :class:`AttributionSpec` means no
        shared-role fan-out and no colocation re-home, so feature
        membership for a react-router-framework unit is byte-identical to
        the DefaultProfile. B44 adds flows only; attribution is unchanged."""
        return AttributionSpec()


__all__ = [
    "REACT_ROUTER_FW_PROFILE_ENV",
    "ReactRouterFrameworkProfile",
    "react_router_fw_profile_enabled",
]
