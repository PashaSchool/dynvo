"""Product-Spine Wave 2a — product-surface taxonomy (spec §4.2, Stage 6.85).

Kills rootcause RC4 / evidence class C3: the engine had no product-surface
taxonomy, so any route container could become a product feature — 39
info-page PFs on the 10-scan board (26 in supabase alone: "Marketing Site
Pages" 1090 files, "Docs Site & API Reference", Blog, Brand Assets…), each
padded with exactly one journey. The one structural signal separating
marketing from product surfaces (Next route-groups) was stripped at Stage 1
(now carried — see ``extractors/route.route_groups_of``).

Two wiring points in ``phase_finalize``:

**Layer-1 tagging** (:func:`tag_layer1`) — right after Stage 6.8 lineage +
Stage 6.8b system-route classification: stamps ``surface_scope`` on every
``routes_index`` entry and every developer feature. Stage 6.7d consumes the
dev tags (tier-3 route-surface promotion skips non-product devs; the
container-page guard consumes the ``shell`` tag).

**Emission lane** (:func:`apply_emission_taxonomy`) — after Stage 6.97 LOC +
the flowless-shell resolution, before emission integrity:

  1. re-stamps dev tags (paths moved through strips/carves) and tags every
     user flow + product feature;
  2. **non-product lane** (spec §4.2 consequence a): product features whose
     evidence majority is marketing / docs / legal / dev_tooling / shell
     leave ``product_features[]`` into the additive
     ``non_product_surfaces[]`` output lane, taking their journeys with
     them — non-product surfaces never mint PFs in the product list
     (validator I20);
  3. **info-page dissolution** (consequence b): a marketing/legal-scope
     journey over single info PAGES (contact / about / imprint / faq /
     terms class) attached to a PRODUCT feature dissolves — its member
     flows become plain dev-flows of the hosting UF (same product feature,
     max entry-path overlap) — info pages are never their own UF/PF;
  4. **dev lane re-bind**: a shared-platform dev whose surface is
     non-product re-binds to the lane surface claiming its paths — info
     pages are never Shared Platform residents;
  5. **shared-resident reasons** (validator I22): every dev feature still
     bound to the shared bucket carries a machine-readable
     ``shared_reason`` (no_anchor_lineage | genuinely_shared_infra |
     facet_view | awaiting_wave2_mint | non_product_surface).

``surface_scope`` vocabulary (spec §4.2):
``product | marketing | docs | legal | system | dev_tooling | shell``.

Signals: route-groups (author's own surface declaration), workspace class
(apps/www, apps/docs), URL-segment lexicon, Stage 6.8b system triggers,
container identity. Patterns live in ``surface-scope-patterns.yaml``
(authoring copy ``eval/surface-scope-patterns.yaml``, drift-guarded) —
structural web conventions only, never a repo-specific path. ``system``
scope comes ONLY from Stage 6.8b verdicts (never lexicon-derived — /webhooks
and /cron segments appear in product settings pages; measured on the
recorded Lane-C claim table).

Ambiguous / no-signal → ``product`` (conservative: never hide a product
journey). Aggregation is strict-majority/argmax over path evidence —
scale-invariant, no tuned constants (rule-no-magic-tuning). Adapted from the
parked ``agent/scope-filter`` classifier (Lane C, 2026-07-05), extended to
route/dev/PF grain + legal/shell scopes + the wired consequences.

Deterministic, $0 LLM, no network. Kill-switches (default ON):
``FAULTLINE_SURFACE_TAXONOMY=0`` disables tagging AND every consequence;
``FAULTLINE_SURFACE_LANE=0`` keeps tags but disables the emission-lane
moves (2-4); ``FAULTLINE_SHARED_REASONS=0`` disables the I22 stamping.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from faultline.pipeline_v2.data import load_yaml

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature, Flow, UserFlow

__all__ = [
    "SURFACE_TAXONOMY_ENV",
    "SURFACE_LANE_ENV",
    "SHARED_REASONS_ENV",
    "NONPRODUCT_SCOPE_ENV",
    "DOCS_REANCHOR_ENV",
    "SURFACE_SCOPES",
    "SCOPE_PRODUCT",
    "NON_PRODUCT_PF_SCOPES",
    "taxonomy_enabled",
    "lane_enabled",
    "shared_reasons_enabled",
    "nonproduct_scope_enabled",
    "docs_reanchor_enabled",
    "SurfaceScopeClassifier",
    "load_patterns",
    "is_non_product_dev",
    "tag_layer1",
    "apply_emission_taxonomy",
]

SURFACE_TAXONOMY_ENV = "FAULTLINE_SURFACE_TAXONOMY"
SURFACE_LANE_ENV = "FAULTLINE_SURFACE_LANE"
SHARED_REASONS_ENV = "FAULTLINE_SHARED_REASONS"
#: B28 (Shape E) — dev-artifact workspace detection: P-B registry-publisher
#: manifests + P-D hub-fixture marks (Stage 6.86 telemetry) join the
#: emission classifier's instrument-dirs channel behind the R1
#: (journey-isolation strict-minority) + R2 (dominant-app) rails; the S1g
#: types-only prong in ``technology_instruments`` keys on the same flag.
#: Default ON; ``FAULTLINE_NONPRODUCT_SCOPE=0`` restores this slice
#: byte-identically.
NONPRODUCT_SCOPE_ENV = "FAULTLINE_NONPRODUCT_SCOPE"
#: B28 (Shape D) — a PRODUCT-scoped PF anchored inside a non-product app
#: (supabase ``auth`` @ ``route:apps/docs/app/guides/auth`` whose body is
#: 107/107 ``apps/studio/**``) re-anchors in place to its
#: evidence-majority product surface (majority-dir election, route-lineage
#: preferred). Journeys/uuid/paths untouched — conservation trivial.
#: Default ON; ``FAULTLINE_DOCS_REANCHOR=0`` restores this slice
#: byte-identically.
DOCS_REANCHOR_ENV = "FAULTLINE_DOCS_REANCHOR"

_PATTERNS_FILE = "surface-scope-patterns.yaml"

SCOPE_PRODUCT = "product"
SCOPE_SYSTEM = "system"
SCOPE_SHELL = "shell"

#: Fixed precedence among competing NON-product scopes (most specific /
#: least ambiguous first — documented in the pattern file header).
_NON_PRODUCT_PRECEDENCE = (
    "system", "dev_tooling", "legal", "docs", "marketing", "shell",
)

SURFACE_SCOPES = (SCOPE_PRODUCT,) + _NON_PRODUCT_PRECEDENCE

#: PF scopes that leave the product list for the non-product lane —
#: mirrors the validator's I20 bad-scope set. ``system`` deliberately
#: STAYS in the product list (background-job capabilities are product
#: infrastructure with legitimate journeys; I20 allows it).
NON_PRODUCT_PF_SCOPES = frozenset(
    ("marketing", "docs", "legal", "dev_tooling", "shell"),
)

#: Dev scopes that block the 6.7d tier mints / capability joins and that
#: re-bind from the shared bucket to the lane (shell is handled by the
#: container-page guard instead).
_NON_PRODUCT_DEV_SCOPES = frozenset(("marketing", "docs", "legal", "dev_tooling"))

_SHARED_PF_KEYS = frozenset(("shared-platform", "platform"))

# Path segments that mark the start of URL context in a route file path —
# framework routing roots (Next app/, Next+Astro+Nuxt pages/, Remix/SvelteKit
# routes/). Only segments AFTER the last such marker are matched against the
# url_segments lexicon, so arbitrary source dirs (src/features/blog-model/…)
# never match. (Parked Lane-C rule, kept verbatim.)
_URL_ROOT_MARKERS = frozenset({"app", "pages", "routes"})

# Monorepo workspace container dirs — ``<container>/<name>`` where <name> is
# checked against the workspace_dirs lexicon.
_WORKSPACE_CONTAINERS = frozenset(
    {"apps", "packages", "sites", "websites", "tools"},
)

# Route-file basenames that carry no lexical signal of their own.
_NEUTRAL_STEMS = frozenset(
    {"page", "route", "index", "layout", "_index", "default"},
)

_DYNAMIC_SEG_RE = re.compile(r"^\[.*\]$|^:.+$|^<.+>$|^\{.+\}$|^\*|^\$")

_TRIGGER_INTERACTIVE = "interactive"


def _flag(env: str) -> bool:
    return os.environ.get(env, "1").strip().lower() not in {"0", "false"}


def taxonomy_enabled() -> bool:
    """Master switch — default ON, ``FAULTLINE_SURFACE_TAXONOMY=0`` off."""
    return _flag(SURFACE_TAXONOMY_ENV)


def lane_enabled() -> bool:
    """Emission-lane consequences — default ON, ``FAULTLINE_SURFACE_LANE=0``
    keeps the tags but moves nothing."""
    return _flag(SURFACE_LANE_ENV)


def shared_reasons_enabled() -> bool:
    """I22 stamping — default ON, ``FAULTLINE_SHARED_REASONS=0`` off."""
    return _flag(SHARED_REASONS_ENV)


def nonproduct_scope_enabled() -> bool:
    """B28 Shape E — default ON, ``FAULTLINE_NONPRODUCT_SCOPE=0`` off."""
    return _flag(NONPRODUCT_SCOPE_ENV)


def docs_reanchor_enabled() -> bool:
    """B28 Shape D — default ON, ``FAULTLINE_DOCS_REANCHOR=0`` off."""
    return _flag(DOCS_REANCHOR_ENV)


def load_patterns() -> dict[str, Any]:
    """Load the runtime pattern file (``{}`` if absent → classifier no-ops)."""
    try:
        return load_yaml(_PATTERNS_FILE) or {}
    except FileNotFoundError:  # pragma: no cover — packaging bug surface
        return {}


def _invert(block: Mapping[str, Any] | None) -> dict[str, str]:
    """``{scope: [token, …]}`` → ``{token: scope}`` (first scope in
    precedence order wins a duplicate token; ``product`` first)."""
    out: dict[str, str] = {}
    if not block:
        return out
    for scope in (SCOPE_PRODUCT,) + _NON_PRODUCT_PRECEDENCE:
        for tok in block.get(scope) or []:
            out.setdefault(str(tok).lower(), scope)
    return out


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Field access that works for pydantic models AND plain dicts."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


class SurfaceScopeClassifier:
    """Deterministic scope classifier over path / route / name evidence.
    Importable standalone (offline re-scoring reads recorded scan
    artifacts directly); the engine wiring lives in :func:`tag_layer1` /
    :func:`apply_emission_taxonomy`.

    ``repo_path`` (optional) enables the ONE filesystem signal: the
    published-CLI override. A ``cli``/``mcp``-named workspace can be a
    PRODUCT shipped CLI (midday's ``packages/cli`` is a published npm
    package with a ``bin`` customers install — its 'Authenticate via CLI'
    journeys are real product journeys), so a dev_tooling workspace-dir
    vote is overridden to PRODUCT when the workspace's ``package.json``
    declares a ``bin`` AND is not ``private: true`` (operator/coordinator
    doctrine, 2026-07-06). No repo_path → no reads → the lexicon verdict
    stands unchanged."""

    def __init__(self, patterns: dict | None = None,
                 repo_path: Any = None,
                 routes_index: Iterable[Mapping[str, Any]] | None = None,
                 instrument_dirs: Iterable[str] | None = None,
                 ) -> None:
        cfg = patterns if patterns is not None else load_patterns()
        self._groups = _invert(cfg.get("route_groups"))
        self._url = _invert(cfg.get("url_segments"))
        self._workspace = _invert(cfg.get("workspace_dirs"))
        self._root_dirs = _invert(cfg.get("root_dirs"))
        self._repo_path = repo_path
        self._published_cache: dict[str, bool] = {}
        # W4.2 Fix 1 — mechanism-detected technology-instrument dirs
        # (Stage 6.86 telemetry). A path inside one is a dev_tooling
        # surface signal; product-declared signals still win precedence.
        self._instrument_dirs: tuple[str, ...] = tuple(sorted(
            _norm(str(d)).lower() for d in (instrument_dirs or []) if d
        ))
        self._shell_slugs = frozenset(
            str(s).lower() for s in (cfg.get("shell_slugs") or [])
        )
        self._info_segments = frozenset(
            str(s).lower() for s in (cfg.get("info_page_segments") or [])
        ) | frozenset(
            str(s).lower()
            for s in ((cfg.get("url_segments") or {}).get("legal") or [])
        )
        # W2b.1 fix (c1) — the STRUCTURAL workspace-class signal: a
        # workspace whose routes are ALL public marketing/info pages IS
        # a marketing surface even when its dir name sits outside the
        # lexicon (typebot apps/landing-page under TanStack: no route
        # groups, so per-file lexicon signals drown in abstentions at
        # feature grain). Built once from the routes_index; empty when
        # the caller has no routes.
        self._ws_scope_overrides: dict[str, str] = {}
        if routes_index is not None:
            self._ws_scope_overrides = self._workspace_route_overrides(
                routes_index)

    def _workspace_route_overrides(
        self, routes_index: Iterable[Mapping[str, Any]],
    ) -> dict[str, str]:
        """``{workspace prefix: scope}`` for workspaces that are WHOLLY a
        public marketing surface, decided structurally from their route
        profile (W2b.1 fix c1):

          * every route is a PAGE (any API-method route disqualifies —
            openstatus apps/web keeps its api/webhook product surface);
          * every route is interactive (no system triggers);
          * ZERO product-declared signals (an authored ``(dashboard)``
            route-group vote wins);
          * ≥ 1 marketing/legal lexicon signal, and marketing + legal +
            info-page signals form a STRICT MAJORITY of the workspace's
            routes (abstains count in the denominator — the same
            conservative ratio rule as feature-grain voting).

        Workspaces the lexicon already classifies (apps/www, apps/docs)
        are skipped — the vocabulary stays authoritative.
        """
        by_ws: dict[str, list[Mapping[str, Any]]] = {}
        for entry in routes_index or []:
            f = _norm(str(entry.get("file") or "")).lower()
            segs = [s for s in f.split("/") if s]
            if len(segs) >= 3 and segs[0] in _WORKSPACE_CONTAINERS:
                by_ws.setdefault("/".join(segs[:2]), []).append(entry)
        out: dict[str, str] = {}
        for ws, entries in sorted(by_ws.items()):
            ws_name = ws.split("/")[1]
            if self._workspace.get(ws_name):
                continue  # lexicon-classified workspace — vocab wins
            if any(str(e.get("method") or "").upper() != "PAGE"
                   for e in entries):
                continue
            if any(str(e.get("trigger") or _TRIGGER_INTERACTIVE)
                   != _TRIGGER_INTERACTIVE for e in entries):
                continue
            n = len(entries)
            mk_legal = 0
            product = 0
            for e in entries:
                pattern = str(e.get("pattern") or "")
                file = str(e.get("file") or "")
                hits: set[str] = set()
                sc_p = self.classify_route_pattern(pattern)
                if sc_p:
                    hits.add(sc_p)
                sc_f = self.classify_path(file)
                if sc_f:
                    hits.add(sc_f)
                if SCOPE_PRODUCT in hits:
                    product += 1
                    continue
                info = self.is_info_page_path(file) or any(
                    s in self._info_segments
                    for s in pattern.lower().split("/")
                )
                if "marketing" in hits or "legal" in hits or info:
                    mk_legal += 1
            if product == 0 and mk_legal >= 1 and mk_legal * 2 > n:
                out[ws] = "marketing"
        return out

    # ── path / route / name signals ─────────────────────────────────

    def classify_path(self, path: str) -> str | None:
        """Scope signal of one file path (``None`` = no signal).

        Signal sources, all collected then resolved by precedence
        (product first — the author's ``(dashboard)`` declaration beats a
        stray lexicon hit):

        1. Next route-groups — valid anywhere in the path;
        2. URL-context segments after the LAST routing-root marker;
        3. workspace dirs (``apps/docs``, ``packages/cli`` …);
        4. repo-ROOT surface dirs (``docs/…``, ``website/…`` — the
           conservative ``root_dirs`` subset).
        """
        if not path:
            return None
        segs = [s for s in _norm(path).lower().split("/") if s]
        hits: set[str] = set()
        for seg in segs:
            if seg.startswith("(") and seg.endswith(")") and len(seg) > 2:
                inner = seg[1:-1]
                if inner.startswith("."):
                    continue  # intercepting-route marker, not a group
                sc = self._groups.get(inner) or self._url.get(inner)
                if sc:
                    hits.add(sc)
        root_idx = -1
        for i, seg in enumerate(segs):
            if seg in _URL_ROOT_MARKERS:
                root_idx = i
        if root_idx >= 0:
            url_segs = [
                s for s in segs[root_idx + 1:]
                if not (s.startswith("(") and s.endswith(")"))
            ]
            if url_segs:
                # filename → stem (``blog.tsx`` → ``blog``); neutral drop
                stem = url_segs[-1].rsplit(".", 1)[0]
                url_segs = url_segs[:-1] + (
                    [] if stem in _NEUTRAL_STEMS else [stem]
                )
            for s in url_segs:
                sc = self._url.get(s)
                if sc:
                    hits.add(sc)
        for i, seg in enumerate(segs[:-1]):
            if seg in _WORKSPACE_CONTAINERS:
                sc = self._workspace.get(segs[i + 1])
                if sc == "dev_tooling" and self._is_published_cli(
                    "/".join(segs[: i + 2]),
                ):
                    sc = SCOPE_PRODUCT  # shipped product CLI, not tooling
                if sc:
                    hits.add(sc)
        if len(segs) > 1:
            sc = self._root_dirs.get(segs[0])
            if sc:
                hits.add(sc)
        if len(segs) >= 2 and self._ws_scope_overrides:
            sc = self._ws_scope_overrides.get("/".join(segs[:2]))
            if sc:
                hits.add(sc)
        if self._instrument_dirs:
            joined = "/".join(segs)
            if any(joined == d or joined.startswith(d + "/")
                   for d in self._instrument_dirs):
                hits.add("dev_tooling")
        if SCOPE_PRODUCT in hits:
            return SCOPE_PRODUCT
        for sc in _NON_PRODUCT_PRECEDENCE:
            if sc in hits:
                return sc
        return None

    def _is_published_cli(self, workspace_dir: str) -> bool:
        """Published-product override for a dev_tooling-named workspace.

        Deterministic signal (coordinator doctrine, 2026-07-06, from the
        midday review): the workspace's ``package.json`` declares a
        non-empty ``bin`` AND is not ``private: true`` ⇒ customers
        install and drive it ⇒ PRODUCT surface. Unreadable / absent
        manifest, no ``bin``, or ``private: true`` ⇒ the lexicon's
        dev_tooling verdict stands. No repo_path ⇒ never reads ⇒ False.
        """
        if self._repo_path is None:
            return False
        cached = self._published_cache.get(workspace_dir)
        if cached is not None:
            return cached
        verdict = False
        try:
            import json as _json
            from pathlib import Path as _Path

            pj = _Path(self._repo_path) / workspace_dir / "package.json"
            if pj.is_file():
                doc = _json.loads(pj.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    verdict = bool(doc.get("bin")) and doc.get("private") is not True
        except (OSError, ValueError):  # unreadable manifest → no override
            verdict = False
        self._published_cache[workspace_dir] = verdict
        return verdict

    def classify_route_pattern(self, route_pattern: str) -> str | None:
        """Scope signal of one URL route pattern (``None`` = no signal)."""
        if not route_pattern:
            return None
        segs = [
            s for s in route_pattern.lower().split("/")
            if s and not _DYNAMIC_SEG_RE.match(s)
        ]
        hits = {self._url[s] for s in segs if s in self._url}
        if SCOPE_PRODUCT in hits:
            return SCOPE_PRODUCT
        for sc in _NON_PRODUCT_PRECEDENCE:
            if sc in hits:
                return sc
        return None

    def is_shell_name(self, *names: str | None) -> bool:
        """Container-page identity: the kebab slug is a shell slug,
        optionally with a ``-page`` suffix (mirrors the 6.7d container
        guard / validator I11 vocabulary — the guard consumes this tag)."""
        for raw in names:
            if not raw:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", str(raw).lower()).strip("-")
            if slug in self._shell_slugs:
                return True
            if slug.endswith("-page") and slug[:-5] in self._shell_slugs:
                return True
        return False

    def is_info_page_path(self, path: str | None) -> bool:
        """True when the path's URL-context segments hit the info-page
        class (contact / about / imprint / faq / legal pages)."""
        if not path:
            return False
        segs = [s for s in _norm(path).lower().split("/") if s]
        root_idx = -1
        for i, seg in enumerate(segs):
            if seg in _URL_ROOT_MARKERS:
                root_idx = i
        if root_idx < 0:
            return False
        url_segs = [
            s for s in segs[root_idx + 1:]
            if not (s.startswith("(") and s.endswith(")"))
        ]
        if url_segs:
            stem = url_segs[-1].rsplit(".", 1)[0]
            url_segs = url_segs[:-1] + ([] if stem in _NEUTRAL_STEMS else [stem])
        return any(s in self._info_segments for s in url_segs)

    # ── route-entry grain ────────────────────────────────────────────

    def classify_route_entry(self, entry: Mapping[str, Any]) -> str:
        """Scope of one ``routes_index`` entry.

        Stage 6.8b's ``trigger`` verdict wins (system routes are system
        surfaces); then lexicon signals from the pattern and the file
        path; an unmatched real route is PRODUCT surface (conservative).
        The bare root pattern (``/``) with no other signal is the app
        shell.
        """
        trig = str(entry.get("trigger") or _TRIGGER_INTERACTIVE)
        if trig != _TRIGGER_INTERACTIVE:
            return SCOPE_SYSTEM
        hits: set[str] = set()
        sc_p = self.classify_route_pattern(str(entry.get("pattern") or ""))
        if sc_p:
            hits.add(sc_p)
        sc_f = self.classify_path(str(entry.get("file") or ""))
        if sc_f:
            hits.add(sc_f)
        if SCOPE_PRODUCT in hits:
            return SCOPE_PRODUCT
        for sc in _NON_PRODUCT_PRECEDENCE:
            if sc in hits:
                return sc
        pattern = _norm(str(entry.get("pattern") or ""))
        if pattern in ("", "/"):
            return SCOPE_SHELL
        return SCOPE_PRODUCT

    # ── feature grain (developer features AND product features) ─────

    def classify_feature(
        self,
        feature: Any,
        route_by_file: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> str:
        """Scope of one feature row from its own evidence.

        1. Container identity (name is a shell slug) → ``shell``.
        2. Weighted vote over owned paths: each path contributes its
           :meth:`classify_path` signal; a path that IS a route file
           contributes the route entry's scope instead (system trigger /
           pattern lexicon / product default). No-signal paths abstain
           but COUNT in the denominator.
        3. Verdict: a non-product scope wins ONLY on a strict majority of
           ALL owned paths (abstains included) that also beats the
           product weight — a feature is a non-product surface when its
           evidence dominates its whole footprint, never on a stray
           lexicon hit. Everything else → ``product`` (conservative:
           never hide a product capability). No paths → ``product``.
        """
        scope, _ambiguous, _weights = self.classify_feature_with_signals(
            feature, route_by_file,
        )
        return scope

    def classify_feature_with_signals(
        self,
        feature: Any,
        route_by_file: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[str, bool, dict[str, int]]:
        """:meth:`classify_feature` + the W3 §4.7 ambiguity channel.

        Returns ``(scope, ambiguous, signal_weights)``. The verdict is
        BYTE-IDENTICAL to :meth:`classify_feature` (same rules — this is
        the same computation, annotated). ``ambiguous`` is the NARROW
        "conflicting signals" notion the Surface Adjudicator consumes:

        * a non-product signal AND a product signal are both present
          (direct conflict), OR
        * a non-product signal is present but the verdict fell to the
          conservative ``product`` default only because abstaining
          paths denied it the strict majority.

        Shell-named containers and no-signal features are NOT ambiguous
        (nothing to adjudicate — no conflicting evidence exists).
        """
        if self.is_shell_name(_get(feature, "name"),
                              _get(feature, "display_name")):
            return SCOPE_SHELL, False, {SCOPE_SHELL: 1}
        weights: dict[str, int] = {}
        total = 0
        rbf = route_by_file or {}
        for p in _get(feature, "paths") or []:
            key = _norm(str(p))
            total += 1
            entry = rbf.get(key)
            if entry is not None:
                sc: str | None = self.classify_route_entry(entry)
            else:
                sc = self.classify_path(key)
            if sc:
                weights[sc] = weights.get(sc, 0) + 1
        if not weights or not total:
            return SCOPE_PRODUCT, False, weights
        product_w = weights.get(SCOPE_PRODUCT, 0)
        best_np = None
        for sc in _NON_PRODUCT_PRECEDENCE:
            w = weights.get(sc, 0)
            if w and (best_np is None or w > weights.get(best_np, 0)):
                best_np = sc
        ambiguous = best_np is not None and (
            product_w > 0 or weights[best_np] * 2 <= total
        )
        if (
            best_np is None
            or weights[best_np] * 2 <= total
            or product_w >= weights[best_np]
        ):
            return SCOPE_PRODUCT, ambiguous, weights
        return best_np, ambiguous, weights

    # ── user-flow grain (parked Lane-C aggregation, extended) ───────

    def member_vote(
        self,
        entry_file: str | None,
        paths: Iterable[str] = (),
        entry_is_route: bool = False,
    ) -> str | None:
        """One member flow's scope vote (``None`` = abstain)."""
        sc = self.classify_path(entry_file or "")
        if sc:
            return sc
        if entry_is_route:
            return SCOPE_PRODUCT  # real product route — blocks non-product
        path_votes = {v for v in (self.classify_path(p) for p in paths) if v}
        if len(path_votes) == 1:
            return next(iter(path_votes))
        return None

    def classify_user_flow(
        self,
        member_votes: Iterable[str | None],
        uf_routes: Iterable[str] = (),
        uf_category: str | None = None,
    ) -> str:
        """Aggregate member votes + UF route patterns into one scope.

        ``category == "system"`` (Stage 6.8b) is authoritative; any
        product vote blocks a non-product verdict (parked Lane-C rule);
        ``shell`` votes abstain at journey grain (a journey is never "the
        app shell"); otherwise majority with fixed precedence.
        """
        if uf_category == SCOPE_SYSTEM:
            return SCOPE_SYSTEM
        votes = [
            v for v in member_votes if v is not None and v != SCOPE_SHELL
        ]
        for r in uf_routes:
            # An unmatched route pattern is product surface (conservative).
            votes.append(self.classify_route_pattern(r) or SCOPE_PRODUCT)
        if not votes or SCOPE_PRODUCT in votes:
            return SCOPE_PRODUCT
        counts: dict[str, int] = {}
        for v in votes:
            counts[v] = counts.get(v, 0) + 1
        return max(
            counts,
            key=lambda s: (counts[s], -_NON_PRODUCT_PRECEDENCE.index(s)),
        )


def is_non_product_dev(dev: Any) -> bool:
    """True when *dev* carries a non-product (non-shell) surface tag —
    consumed by the 6.7d residual ladder (tier mints / capability joins
    must not promote marketing/docs/legal/dev_tooling surfaces). Reads the
    stamped field only; absent tag (taxonomy off / pre-W2a scans) → False,
    so every consumer no-ops under the kill-switch."""
    return str(_get(dev, "surface_scope") or "") in _NON_PRODUCT_DEV_SCOPES


# ── wiring helpers ──────────────────────────────────────────────────────


def _route_by_file(
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for entry in routes_index or []:
        f = _norm(str(entry.get("file") or ""))
        if f:
            out.setdefault(f, entry)
    return out


def _stamp(obj: Any, field: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[field] = value
    else:
        setattr(obj, field, value)


def tag_layer1(
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
    patterns: dict | None = None,
    repo_path: Any = None,
) -> dict[str, Any]:
    """Stage 6.85 Layer-1 tagging — stamp ``surface_scope`` on every
    ``routes_index`` entry and every developer feature, in place.

    Runs right after Stage 6.8b (so route ``trigger`` verdicts exist).
    ``repo_path`` enables the published-CLI product override.
    Returns telemetry ``{route_scopes: {...}, dev_scopes: {...}}``.
    """
    tele: dict[str, Any] = {"enabled": taxonomy_enabled()}
    if not taxonomy_enabled():
        return tele
    clf = SurfaceScopeClassifier(patterns, repo_path=repo_path,
                                 routes_index=routes_index)
    route_counts: dict[str, int] = {}
    for entry in routes_index or []:
        sc = clf.classify_route_entry(entry)
        entry["surface_scope"] = sc
        route_counts[sc] = route_counts.get(sc, 0) + 1
    rbf = _route_by_file(routes_index)
    dev_counts: dict[str, int] = {}
    for dev in developer_features:
        if getattr(dev, "layer", "developer") != "developer":
            continue
        sc = clf.classify_feature(dev, rbf)
        dev.surface_scope = sc
        dev_counts[sc] = dev_counts.get(sc, 0) + 1
    tele["route_scopes"] = route_counts
    tele["dev_scopes"] = dev_counts
    return tele


# ── emission lane ───────────────────────────────────────────────────────


def _pf_key(pf: Any) -> str:
    return str(_get(pf, "id", None) or _get(pf, "name", "") or "")


def _is_shared_bucket(pf: Any) -> bool:
    return _pf_key(pf).strip().lower() in _SHARED_PF_KEYS


def _flow_lookup(flows: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for fl in flows:
        for key in (_get(fl, "uuid"), _get(fl, "name")):
            if key and str(key) not in out:
                out[str(key)] = fl
    return out


def _entry_of(flow: Any) -> str | None:
    entry = _get(flow, "entry_point_file")
    if entry:
        return str(entry)
    ep = _get(flow, "entry_point")
    if ep is not None:
        path = ep.get("path") if isinstance(ep, dict) else getattr(ep, "path", None)
        if path:
            return str(path)
    return None


def _uf_entry_files(uf: Any, flow_by_id: Mapping[str, Any]) -> list[str]:
    files: list[str] = []
    for mid in _get(uf, "member_flow_ids") or []:
        fl = flow_by_id.get(str(mid))
        if fl is None:
            continue
        e = _entry_of(fl)
        if e:
            files.append(_norm(e))
        else:
            files.extend(_norm(str(p)) for p in (_get(fl, "paths") or []))
    return files


def _tag_user_flows(
    user_flows: list["UserFlow"],
    flow_by_id: Mapping[str, Any],
    route_files: frozenset[str],
    clf: SurfaceScopeClassifier,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for uf in user_flows:
        # B52 — a transport-lane journey (lane_ref set) already carries
        # its authoritative scope ('platform_infrastructure', stamped by
        # Stage 6.985); the member-vote classifier must not overwrite
        # it. lane_ref exists only under FAULTLINE_FLOWFUL_TRANSPORT_LANE
        # → OFF scans are byte-identical (counts included).
        if _get(uf, "lane_ref"):
            sc0 = str(_get(uf, "surface_scope") or "platform_infrastructure")
            counts[sc0] = counts.get(sc0, 0) + 1
            continue
        votes: list[str | None] = []
        for mid in _get(uf, "member_flow_ids") or []:
            fl = flow_by_id.get(str(mid))
            if fl is None:
                continue
            entry = _entry_of(fl) or ""
            votes.append(clf.member_vote(
                entry,
                paths=_get(fl, "paths") or [],
                entry_is_route=_norm(entry) in route_files,
            ))
        sc = clf.classify_user_flow(
            votes,
            uf_routes=_get(uf, "routes") or [],
            uf_category=_get(uf, "category"),
        )
        _stamp(uf, "surface_scope", sc)
        counts[sc] = counts.get(sc, 0) + 1
    return counts


def _lane_entry(
    pf: Any,
    scope: str,
    member_devs: list[Any],
    moved_ufs: list[Any],
) -> dict[str, Any]:
    """One ``non_product_surfaces[]`` row — a compact, self-contained view
    of the surface (NOT a full Feature dump; the dev rows stay in
    ``features[]`` with their ``product_feature_id`` pointing here)."""
    entry: dict[str, Any] = {
        "name": _get(pf, "name"),
        "display_name": _get(pf, "display_name"),
        "surface_scope": scope,
        "description": _get(pf, "description"),
        "uuid": _get(pf, "uuid") or "",
        "paths": list(_get(pf, "paths") or []),
        "loc": _get(pf, "loc"),
        "loc_shared": _get(pf, "loc_shared"),
        "member_devs": sorted(
            str(_get(d, "name") or "") for d in member_devs
        ),
        "reason": "non_product_surface_scope",
    }
    entry["user_flows"] = [
        uf.model_dump() if hasattr(uf, "model_dump") else dict(uf)
        for uf in moved_ufs
    ]
    return entry


def _dissolve_info_ufs(
    user_flows: list["UserFlow"],
    flow_by_id: Mapping[str, Any],
    product_pf_keys: frozenset[str],
    clf: SurfaceScopeClassifier,
    tele: dict[str, Any],
) -> None:
    """Consequence (b): info-page journeys dissolve into the hosting UF.

    A UF qualifies when its scope is marketing/legal, EVERY member flow's
    entry (or paths, when no entry) lands on an info-page URL, and its
    product feature is a real product-lane PF that hosts at least one
    OTHER journey (the host). The member flows join the host as plain
    dev-flows; the info UF row is removed (never a PF, never its own
    journey). No host → left in place, tagged only (honest residual)."""
    by_pf: dict[str, list[Any]] = {}
    for uf in user_flows:
        ref = _get(uf, "product_feature_id")
        if ref:
            by_pf.setdefault(str(ref), []).append(uf)

    dissolved: list[Any] = []
    for uf in list(user_flows):
        if str(_get(uf, "surface_scope") or "") not in ("marketing", "legal"):
            continue
        pfid = str(_get(uf, "product_feature_id") or "")
        if pfid not in product_pf_keys:
            continue  # lane PFs took their journeys already; orphans skip
        entries = _uf_entry_files(uf, flow_by_id)
        if not entries or not all(clf.is_info_page_path(e) for e in entries):
            continue
        hosts = [
            u for u in by_pf.get(pfid, [])
            if u is not uf and u not in dissolved
        ]
        if not hosts:
            continue  # "where one exists" — never orphan a PF's only UF
        uf_files = set(entries)

        def _overlap(host: Any) -> int:
            hfiles = set(_uf_entry_files(host, flow_by_id))
            shared_dirs = 0
            for a in uf_files:
                a_dir = a.rsplit("/", 1)[0]
                for b in hfiles:
                    b_dir = b.rsplit("/", 1)[0]
                    common = 0
                    for sa, sb in zip(a_dir.split("/"), b_dir.split("/")):
                        if sa != sb:
                            break
                        common += 1
                    shared_dirs = max(shared_dirs, common)
            return shared_dirs

        host = sorted(
            hosts,
            key=lambda h: (
                -_overlap(h),
                -len(_get(h, "member_flow_ids") or []),
                str(_get(h, "id") or ""),
            ),
        )[0]
        host_mids = list(_get(host, "member_flow_ids") or [])
        for mid in _get(uf, "member_flow_ids") or []:
            if mid not in host_mids:
                host_mids.append(mid)
        _stamp(host, "member_flow_ids", host_mids)
        _stamp(host, "member_count", len(host_mids))
        host_routes = list(_get(host, "routes") or [])
        for r in _get(uf, "routes") or []:
            if r not in host_routes:
                host_routes.append(r)
        _stamp(host, "routes", host_routes)
        dissolved.append(uf)
        if len(tele.setdefault("info_ufs_dissolved_sample", [])) < 10:
            tele["info_ufs_dissolved_sample"].append({
                "uf": _get(uf, "name"), "host": _get(host, "name"),
                "pf": pfid,
            })

    if dissolved:
        gone = {id(u) for u in dissolved}
        user_flows[:] = [u for u in user_flows if id(u) not in gone]
    tele["info_ufs_dissolved"] = len(dissolved)


def _shared_reason_for(dev: Any, clf: SurfaceScopeClassifier) -> str:
    """Deterministic reason ladder for one shared-bucket resident."""
    from faultline.pipeline_v2.spine_hygiene import is_concern_name
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        _is_workspace_anchor,
    )
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _STRUCTURE_LEAK_SLUGS,
    )

    scope = str(_get(dev, "surface_scope") or "")
    if scope in _NON_PRODUCT_DEV_SCOPES:
        return "non_product_surface"
    name = str(_get(dev, "name") or "")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if (
        scope == SCOPE_SHELL
        or slug in _STRUCTURE_LEAK_SLUGS
        or _is_workspace_anchor(dev)
    ):
        return "genuinely_shared_infra"
    if is_concern_name(name):
        return "facet_view"
    has_anchor_claim = any(
        (m.get("role") if isinstance(m, dict) else getattr(m, "role", None))
        == "anchor"
        for m in (_get(dev, "member_files") or [])
    )
    if has_anchor_claim or (_get(dev, "flows") or []):
        return "awaiting_wave2_mint"
    return "no_anchor_lineage"


# ── B28 — dev-artifact workspaces (Shape E) + Shape-D re-anchor ────────


def _workspace_unit(path: str | None) -> str | None:
    """Workspace grain of a path: ``<seg0>/<seg1>`` when two segments
    exist (``apps/docs``, ``blocks/vue``, ``packages/app-store``), else
    the root segment. No container vocabulary — B28 candidacy is decided
    by manifests/marks, never by the container's name."""
    if not path:
        return None
    segs = [s for s in _norm(str(path)).split("/") if s]
    if not segs:
        return None
    return "/".join(segs[:2]) if len(segs) >= 2 else segs[0]


def _is_registry_publisher(repo_path: Any, ws_dir: str) -> bool:
    """B28 P-B — the workspace's own manifests declare a component-registry
    PUBLISHER (the shadcn registry convention): ``components.json`` at the
    workspace root **plus** registry evidence (a ``registry/`` source tree,
    a ``registry.json``, or a ``shadcn build`` script). Mere shadcn
    CONSUMERS carry ``components.json`` alone (measured: papermark,
    openstatus ×3, supabase ``packages/ui``) and never fire."""
    try:
        from pathlib import Path as _Path

        root = _Path(repo_path) / ws_dir
        if not (root / "components.json").is_file():
            return False
        if (root / "registry").is_dir() or (root / "registry.json").is_file():
            return True
        pj = root / "package.json"
        if pj.is_file():
            import json as _json

            doc = _json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                scripts = " ".join(
                    str(v) for v in (doc.get("scripts") or {}).values()
                )
                return "shadcn build" in scripts
    except (OSError, ValueError):  # unreadable manifest → no override
        return False
    return False


def _dev_artifact_dirs(
    developer_features: Iterable[Any],
    product_features: Iterable[Any],
    user_flows: Iterable[Any],
    flows: Iterable[Any],
    repo_path: Any,
    marked_units: Iterable[str],
    tele: dict[str, Any],
) -> set[str]:
    """Collect B28 Shape-E candidate workspaces (P-B manifests + the P-D
    marks from Stage 6.86) and gate them behind the two hard rails:

    * **R1 journey-isolation (strict minority)** — entry contributions of
      journeys homed OUTSIDE the workspace must be a strict minority of
      the workspace's entry contributions (zero-tolerance is too brittle:
      one ui-library registry template mirrors a studio route);
    * **R2 dominant-app guard** — never the workspace holding the strict
      majority of the board's member-flow entries (the repo whose
      registry IS the product — shadcn-class).

    Survivors join the classifier's instrument-dirs channel, so the
    EXISTING strict-majority feature vote + emission lane execute the
    demotion — journeys ride along (operator ruling 2026-07-10)."""
    def _under(path: str | None, prefix: str) -> bool:
        return bool(path) and (
            path == prefix or str(path).startswith(prefix + "/"))

    flow_by_id = _flow_lookup(flows)
    pf_anchor_path: dict[str, str] = {}
    for pf in product_features:
        aid = str(_get(pf, "anchor_id") or "")
        if ":" in aid:
            apath = _norm(aid.split(":", 1)[1])
            if "/" in apath:  # short keyed-artifact anchors have no unit
                pf_anchor_path[_pf_key(pf)] = apath

    # Candidates are PF-ANCHORED dirs only — B28's mandate is "PFs
    # anchored inside non-product apps"; a dev-artifact workspace nobody
    # anchors a PF in changes nothing on the product board, so touching
    # it (dev-grain scope shifts) would be pure blast radius. P-D marks
    # keep their own hub-child grain; P-B checks the 2-seg workspace of
    # each anchor.
    cands: dict[str, str] = {}
    for u in marked_units or ():
        nu = _norm(str(u))
        if nu and any(_under(ap, nu) for ap in pf_anchor_path.values()):
            cands[nu] = "P-D:hub-fixture"
    if repo_path is not None:
        checked: set[str] = set()
        for ap in sorted(pf_anchor_path.values()):
            ws = _workspace_unit(ap)
            if not ws or "/" not in ws or ws in cands or ws in checked:
                continue
            checked.add(ws)
            if _is_registry_publisher(repo_path, ws):
                cands[ws] = "P-B:registry-publisher"
    if not cands:
        return set()
    # R1/R2 rails at each candidate's OWN grain (2-seg workspace or
    # hub-child dir): internal = entries under the candidate from
    # journeys whose home PF is anchored under it too; external = entries
    # under it from journeys homed elsewhere.
    entries: list[tuple[str, str | None]] = []  # (entry, home anchor)
    for uf in user_flows:
        home = pf_anchor_path.get(
            str(_get(uf, "product_feature_id") or ""))
        for mid in _get(uf, "member_flow_ids") or []:
            fl = flow_by_id.get(str(mid))
            entry = _entry_of(fl) if fl is not None else None
            if entry:
                entries.append((_norm(entry), home))
    total = len(entries)

    applied: set[str] = set()
    blocked: dict[str, str] = {}
    for cand in sorted(cands):
        i = sum(1 for e, home in entries
                if _under(e, cand) and _under(home, cand))
        x = sum(1 for e, home in entries
                if _under(e, cand) and not _under(home, cand))
        if x and x >= i:
            blocked[cand] = f"R1:external {x} >= internal {i}"
            continue
        if total and (i + x) * 2 > total:
            blocked[cand] = "R2:dominant-app"
            continue
        applied.add(cand)
    tele["candidates"] = dict(sorted(cands.items()))
    tele["applied"] = sorted(applied)
    if blocked:
        tele["blocked"] = blocked
    return applied


_CODE_FILE_EXT_RE = re.compile(
    r"\.(?:tsx?|jsx?|mjs|cjs|mts|cts|vue|svelte|py)$"
)


def _majority_dir(paths: list[str]) -> str | None:
    """Deepest directory reached by descending while ONE child holds a
    strict majority of the ORIGINAL path mass (scale-invariant — the
    B20/B22 strict-majority family at directory grain). Anchoring the
    bar to the original mass stops the walk from compounding relative
    majorities into an over-deep election (midday ``insights``: the
    remaining-mass rule descended to ``…/src/content/prompts``; the
    original-mass rule stops at ``packages/insights/src``)."""
    if not paths:
        return None
    total = len(paths)
    cur = ""
    while True:
        kids: dict[str, int] = {}
        for p in paths:
            if cur and not p.startswith(cur + "/"):
                continue
            rel = p[len(cur):].lstrip("/") if cur else p
            segs = rel.split("/")
            if len(segs) > 1:
                kids[segs[0]] = kids.get(segs[0], 0) + 1
        if not kids:
            return cur or None
        best = max(sorted(kids), key=lambda k: kids[k])
        if kids[best] * 2 <= total:
            return cur or None
        cur = (cur + "/" if cur else "") + best


def _reanchor_mislocated_pfs(
    product_features: list[Any],
    clf: SurfaceScopeClassifier,
    route_files: frozenset[str],
    tele: dict[str, Any],
) -> None:
    """B28 Shape D — a PRODUCT-scoped PF whose anchor sits inside a
    non-product workspace but whose OWN evidence majority lives outside it
    (supabase docs-guides class: ``auth`` anchored at
    ``route:apps/docs/app/guides/auth`` with 107/107 studio paths) keeps
    its journeys and its uuid and re-anchors in place:

    1. ``outside`` = owned paths not under the anchor workspace; a strict
       majority is required (an inside-majority PF is the LANE's case);
    2. elect the majority-dir walk result when it lands strictly deeper
       than the workspace root; else fall back to the common prefix of
       the outside ROUTE files (anchor-lineage law) — extension-stripped
       when it is a single file; else refuse (telemetry, no mutation);
    3. anchor kind: ``route:`` when the elected prefix carries
       routes_index lineage, else ``fdir:``.
    """
    moves: list[dict[str, str]] = []
    refused: dict[str, str] = {}
    stripped_routes = {
        _CODE_FILE_EXT_RE.sub("", rf): rf for rf in route_files
    }
    for pf in product_features:
        if _is_shared_bucket(pf):
            continue
        if str(_get(pf, "surface_scope") or "") != SCOPE_PRODUCT:
            continue
        aid = str(_get(pf, "anchor_id") or "")
        if ":" not in aid:
            continue
        apath = _norm(aid.split(":", 1)[1])
        if "/" not in apath:
            continue  # short keyed-artifact anchor — no workspace unit
        unit = _workspace_unit(apath)
        if not unit:
            continue
        unit_scope = clf.classify_path(unit)
        if unit_scope in (None, SCOPE_PRODUCT):
            continue
        name = str(_get(pf, "display_name") or _get(pf, "name") or "?")
        paths = [_norm(str(p)) for p in (_get(pf, "paths") or [])]
        outside = [p for p in paths if _workspace_unit(p) != unit]
        if not paths or len(outside) * 2 <= len(paths):
            # body majority INSIDE the non-product workspace — the
            # emission lane owns that shape; nothing to re-anchor.
            continue
        unit_depth = len(unit.split("/"))
        elected = _majority_dir(outside)
        if not elected or len(elected.split("/")) <= unit_depth:
            rfiles = [p for p in outside if p in route_files]
            common: str | None = None
            if rfiles:
                segs_list = [p.split("/") for p in rfiles]
                out: list[str] = []
                for segs in zip(*segs_list):
                    if len(set(segs)) == 1:
                        out.append(segs[0])
                    else:
                        break
                common = "/".join(out) or None
                if common and _CODE_FILE_EXT_RE.search(common):
                    common = _CODE_FILE_EXT_RE.sub("", common)
            if common and len(common.split("/")) > unit_depth:
                elected = common
            else:
                refused[name] = "no-deep-majority-dir"
                continue
        has_route = elected in stripped_routes or any(
            rf == elected or rf.startswith(elected + "/")
            for rf in route_files
        )
        new_aid = ("route:" if has_route else "fdir:") + elected
        if new_aid == aid:
            continue
        moves.append({"pf": name, "from": aid, "to": new_aid})
        _stamp(pf, "anchor_id", new_aid)
    if moves:
        tele["reanchored"] = moves
    if refused:
        tele["refused"] = refused


def apply_emission_taxonomy(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    routes_index: list[dict[str, Any]] | None,
    patterns: dict | None = None,
    repo_path: Any = None,
    adjudicator: Any = None,
    instrument_dirs: Iterable[str] | None = None,
    dev_artifact_units: Iterable[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]], list["Feature"]]:
    """Emission-time taxonomy: tag UFs/PFs, split the non-product lane,
    dissolve info-page journeys, re-bind non-product shared devs, stamp
    shared-resident reasons.

    Mutates ``developer_features`` / ``user_flows`` in place; returns
    ``(telemetry, non_product_surfaces, product_features)`` — the caller
    rebinds its ``product_features`` list (lane rows are REMOVED from it).
    ``repo_path`` enables the published-CLI product override.

    ``adjudicator`` (W3 §4.7, keyed scans): callable resolving the
    NARROW ambiguous-PF minority (conflicting deterministic signals —
    see :meth:`SurfaceScopeClassifier.classify_feature_with_signals`).
    Its verdict must be a scope the item's own signals support (the
    personas module enforces that contract; re-checked here) — invalid
    or absent verdicts keep the deterministic scope, so the adjudicator
    can never invent a surface. ``None`` (keyless / kill-switch) is the
    deterministic path, byte-identical to pre-W3.
    """
    tele: dict[str, Any] = {"enabled": taxonomy_enabled()}
    if not taxonomy_enabled():
        return tele, [], product_features

    # B28 Shape E — P-B/P-D dev-artifact workspaces join the instrument
    # channel behind the R1/R2 rails BEFORE the classifier is built, so
    # the existing strict-majority vote + emission lane execute the
    # demotion with journeys riding along. Flag-OFF (or no survivors)
    # leaves ``instrument_dirs`` byte-identical.
    if nonproduct_scope_enabled():
        dev_tele: dict[str, Any] = {}
        extra = _dev_artifact_dirs(
            developer_features, product_features, user_flows, flows,
            repo_path, dev_artifact_units, dev_tele,
        )
        if extra:
            instrument_dirs = tuple(sorted(
                {_norm(str(d)) for d in (instrument_dirs or []) if d}
                | extra
            ))
        if dev_tele.get("candidates"):
            tele["dev_artifact"] = dev_tele

    clf = SurfaceScopeClassifier(patterns, repo_path=repo_path,
                                 routes_index=routes_index,
                                 instrument_dirs=instrument_dirs)
    # W4.2 Fix 1 rider — PF-grain classification must not second-guess
    # the mint with the instrument prong: a PRODUCT capability whose
    # paths straddle instrument dirs (midday `banking` after the Fix-3
    # provider fold + shared members) would tip dev_tooling and leave
    # the board. The instrument prong applies at PF grain ONLY to PFs
    # whose OWN anchor sits inside an instrument dir; every other PF
    # classifies with the plain (pre-W4.2) signal set. Dev/UF grain
    # keeps the instrument-aware classifier.
    _instr = tuple(sorted(_norm(str(d)).lower()
                          for d in (instrument_dirs or []) if d))
    clf_plain = (SurfaceScopeClassifier(patterns, repo_path=repo_path,
                                        routes_index=routes_index)
                 if _instr else clf)

    def _anchor_in_instruments(pf: Any) -> bool:
        aid = str(_get(pf, "anchor_id", None) or "")
        if ":" not in aid:
            return False
        path = _norm(aid.split(":", 1)[1]).lower()
        return bool(path) and any(
            path == d or path.startswith(d + "/") for d in _instr
        )

    def _pf_clf(pf: Any) -> SurfaceScopeClassifier:
        return clf if _anchor_in_instruments(pf) else clf_plain

    rbf = _route_by_file(routes_index)
    route_files = frozenset(rbf.keys())
    flow_by_id = _flow_lookup(flows)

    # 1a. Re-stamp dev tags — paths moved through 6.9/6.9b strips and the
    # 8.9.x carves since the Layer-1 pass; classification is cheap and
    # idempotent, so recompute from the FINAL ownership.
    dev_counts: dict[str, int] = {}
    for dev in developer_features:
        if getattr(dev, "layer", "developer") != "developer":
            continue
        sc = clf.classify_feature(dev, rbf)
        dev.surface_scope = sc
        dev_counts[sc] = dev_counts.get(sc, 0) + 1
    tele["dev_scopes"] = dev_counts

    # 1b. Tag journeys.
    tele["uf_scopes"] = _tag_user_flows(user_flows, flow_by_id, route_files, clf)

    # 1c. Tag product features. The shared/platform bucket row is a
    # cross-cutting aggregate, not a surface — it stays in the product
    # list by definition (I10/I22 govern it) and carries the neutral
    # ``product`` tag so I20 activation covers every PF row.
    pf_counts: dict[str, int] = {}
    ambiguous_pfs: list[tuple[Any, str, dict[str, int]]] = []
    for pf in product_features:
        if _is_shared_bucket(pf):
            sc = SCOPE_PRODUCT
        else:
            sc, ambiguous, sig = _pf_clf(pf).classify_feature_with_signals(
                pf, rbf)
            if ambiguous:
                ambiguous_pfs.append((pf, sc, sig))
        pf.surface_scope = sc
        pf_counts[sc] = pf_counts.get(sc, 0) + 1
    tele["pf_scopes"] = pf_counts

    # 1d. Surface Adjudicator (W3 §4.7) — ONLY the conflicting-signal
    # minority; the verdict set per item is bounded to the scopes its
    # own deterministic signals support (∪ product). Runs BEFORE the
    # lane split so a flipped marketing/docs PF leaves the product list
    # like any deterministically-classified one. Deterministic verdicts
    # stand on any failure/reject (never blocks).
    #
    # JOURNEY GUARD (W3 mini-A/B finding, openstatus `notifications`):
    # a PF referenced by >=2 user journeys carries product-declared
    # evidence the path lexicon can't see — the flowful-never-in-lane
    # law (I9) at PF grain, and the same denominator doctrine as the
    # marketing workspace-class override ("zero product-declared
    # signals"). Such PFs are never sent for adjudication; the flip
    # class is journey-thin info/tool surfaces only.
    if adjudicator is not None and ambiguous_pfs:
        uf_refs: dict[str, int] = {}
        for uf in user_flows:
            ref = str(_get(uf, "product_feature_id") or "")
            if ref:
                uf_refs[ref] = uf_refs.get(ref, 0) + 1
        assignable = frozenset(NON_PRODUCT_PF_SCOPES) | {SCOPE_PRODUCT}

        # W5.1 ROUTE GUARD (midday `transactions` regression): a PF that
        # owns a real PRODUCT route file carries product-declared evidence
        # the path lexicon undercounts — the same "real product route blocks
        # non-product" law member_vote applies at journey grain (:602). The
        # marketing website's `.../transactions` page (2 files) merged into
        # the 70-file dashboard `transactions` capability made it ambiguous;
        # a lone LLM flip then hid a core product surface into the lane. A
        # PF anchored on a product route is never sent for a non-product
        # flip. Structural, scale-invariant (no threshold), deterministic.
        def _owns_product_route(pf: Any) -> bool:
            clf_pf = _pf_clf(pf)
            for p in (_get(pf, "paths") or []):
                entry = rbf.get(_norm(str(p)))
                if entry is not None and \
                        clf_pf.classify_route_entry(entry) == SCOPE_PRODUCT:
                    return True
            return False

        items = []
        journey_guarded = 0
        route_guarded = 0
        for pf, sc, sig in ambiguous_pfs:
            allowed = sorted(
                ({SCOPE_PRODUCT} | set(sig)) & assignable
            )
            if len(allowed) < 2:
                continue  # nothing to adjudicate
            if uf_refs.get(_pf_key(pf), 0) >= 2:
                journey_guarded += 1
                continue  # journey-rich ⇒ product-evidenced, never flip
            if _owns_product_route(pf):
                route_guarded += 1
                continue  # owns a real product route ⇒ never flip to lane
            paths = [str(p) for p in (_get(pf, "paths") or [])][:5]
            items.append({
                "id": _pf_key(pf),
                "name": str(
                    _get(pf, "display_name") or _get(pf, "name") or ""),
                "allowed": allowed,
                "signals": {k: v for k, v in sorted(sig.items())},
                "paths": paths,
            })
        verdicts: dict[str, str] = {}
        if items:
            try:
                verdicts = adjudicator(items) or {}
            except Exception:  # noqa: BLE001 — persona must never break a scan
                verdicts = {}
        flips = 0
        by_key = {_pf_key(pf): pf for pf, _sc, _sig in ambiguous_pfs}
        allowed_by_id = {i["id"]: set(i["allowed"]) for i in items}
        for iid, scope in verdicts.items():
            pf_obj = by_key.get(str(iid))
            if pf_obj is None or scope not in (
                allowed_by_id.get(str(iid)) or set()
            ):
                continue
            old = str(_get(pf_obj, "surface_scope") or "")
            if scope != old:
                pf_counts[old] = pf_counts.get(old, 1) - 1
                pf_counts[scope] = pf_counts.get(scope, 0) + 1
                pf_obj.surface_scope = scope
                flips += 1
        tele["adjudicator"] = {
            "ambiguous": len(ambiguous_pfs),
            "journey_guarded": journey_guarded,
            "route_guarded": route_guarded,
            "sent": len(items),
            "verdicts": len(verdicts),
            "flips": flips,
        }
        tele["pf_scopes"] = pf_counts

    # B28 Shape D — PRODUCT-scoped PFs anchored inside non-product
    # workspaces re-anchor to their evidence-majority product surface.
    # Runs on FINAL scopes (post-adjudicator) and BEFORE the lane split
    # (a laned PF is Shape E and never re-anchors). No journey, uuid or
    # path mutation — conservation trivial; validator I23 reads the
    # healed anchor.
    if docs_reanchor_enabled():
        reanchor_tele: dict[str, Any] = {}
        _reanchor_mislocated_pfs(
            product_features, clf, route_files, reanchor_tele,
        )
        if reanchor_tele:
            tele["docs_reanchor"] = reanchor_tele

    lane: list[dict[str, Any]] = []
    if lane_enabled():
        # 2. Non-product lane split (consequence a / validator I20).
        keep: list[Any] = []
        moved: list[Any] = []
        for pf in product_features:
            if (
                not _is_shared_bucket(pf)
                and str(_get(pf, "surface_scope") or "") in NON_PRODUCT_PF_SCOPES
            ):
                moved.append(pf)
            else:
                keep.append(pf)
        if moved:
            moved_keys = {_pf_key(pf) for pf in moved}
            ufs_by_pf: dict[str, list[Any]] = {}
            kept_ufs: list[Any] = []
            for uf in user_flows:
                ref = str(_get(uf, "product_feature_id") or "")
                if ref in moved_keys:
                    ufs_by_pf.setdefault(ref, []).append(uf)
                else:
                    kept_ufs.append(uf)
            devs_by_pf: dict[str, list[Any]] = {}
            for dev in developer_features:
                ref = str(_get(dev, "product_feature_id") or "")
                if ref in moved_keys:
                    devs_by_pf.setdefault(ref, []).append(dev)
            for pf in moved:
                key = _pf_key(pf)
                lane.append(_lane_entry(
                    pf,
                    str(_get(pf, "surface_scope")),
                    devs_by_pf.get(key, []),
                    ufs_by_pf.get(key, []),
                ))
            user_flows[:] = kept_ufs
            product_features = keep
        tele["pfs_moved_to_lane"] = len(moved)
        tele["ufs_moved_to_lane"] = sum(
            len(e["user_flows"]) for e in lane
        )
        tele["lane_names"] = [e["name"] for e in lane][:20]

        # 3. Info-page journey dissolution (consequence b).
        product_pf_keys = frozenset(
            _pf_key(pf) for pf in product_features if not _is_shared_bucket(pf)
        )
        _dissolve_info_ufs(user_flows, flow_by_id, product_pf_keys, clf, tele)

        # 4. Non-product shared devs re-bind to the lane surface claiming
        # their paths (info pages are never Shared Platform residents).
        rebound = 0
        if lane:
            lane_paths = [
                (e["name"], frozenset(_norm(str(p)) for p in e["paths"]))
                for e in lane
            ]
            for dev in developer_features:
                pfid = str(_get(dev, "product_feature_id") or "")
                if pfid.strip().lower() not in _SHARED_PF_KEYS:
                    continue
                if str(_get(dev, "surface_scope") or "") not in _NON_PRODUCT_DEV_SCOPES:
                    continue
                own = frozenset(
                    _norm(str(p)) for p in (_get(dev, "paths") or [])
                )
                best_name, best_hit = None, 0
                for name, lpaths in lane_paths:
                    hit = len(own & lpaths)
                    if hit > best_hit:
                        best_name, best_hit = name, hit
                if best_name:
                    dev.product_feature_id = best_name
                    rebound += 1
        tele["devs_rebound_to_lane"] = rebound

    # 5. Shared-resident reasons (validator I22) — every dev still bound
    # to the shared bucket carries a machine-readable reason.
    if shared_reasons_enabled():
        reason_counts: dict[str, int] = {}
        for dev in developer_features:
            pfid = str(_get(dev, "product_feature_id") or "")
            if pfid.strip().lower() not in _SHARED_PF_KEYS:
                continue
            reason = _shared_reason_for(dev, clf)
            dev.shared_reason = reason
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        tele["shared_reasons"] = reason_counts

    return tele, lane, product_features
