"""Deterministic product-surface scope classification (Stage 6.85).

MISSION-92R Lane C (2026-07-05). The dominant class of high-confidence FALSE
surfaced claims (precision-p1b) is REAL grounded code OUTSIDE the product's
user journeys: marketing sites (blog / guides / pricing / legal / landings),
docs sites, webhook/cron system surfaces, MCP/CLI dev-tooling. This stage tags
every ``user_flow`` with a ``surface_scope`` so downstream surfacing can rank
product journeys separately from non-product surfaces.

``surface_scope`` vocabulary: ``product`` | ``marketing`` | ``docs`` |
``system`` | ``dev_tooling``.

ADDITIVE ONLY — nothing is removed from scan output; the explore tier keeps
everything. Ambiguous / no-signal → ``product`` (conservative: never hide a
product journey).

The patterns live in ``surface-scope-patterns.yaml`` (authoring copy
``eval/surface-scope-patterns.yaml``; runtime copy
``faultline/pipeline_v2/data/surface-scope-patterns.yaml`` — kept
byte-identical). Per stack-pattern-library, anything hardcoded in Python is a
bug; this module only *applies* the patterns. Structural vocabulary only —
universal web conventions (Next route-groups, marketing/docs URL slugs,
monorepo workspace names), never a repo-specific path.

No LLM, no network, no filesystem reads beyond the pattern file. The
``system`` scope comes ONLY from the existing Stage 6.8b aggregate verdict
(``UserFlow.category == "system"``) — it is deliberately NOT re-derived from
path segments or per-member triggers: /webhooks and /cron segments also
appear in PRODUCT settings pages, and golden journals legitimately contain
system journeys (measured on the recorded 24-draw claim table, Lane C v1).

Classification is evidence-voted per UF from its member flows:

1. a member whose entry path matches the lexicon votes that scope;
2. a member whose entry is a ROUTE file that matches nothing votes
   ``product`` (a real product route — blocks any non-product verdict);
3. otherwise the member's flow paths vote only when they unanimously
   agree on one scope; anything else abstains.

UF verdict: ``category == "system"`` → system; any ``product`` vote →
product; else majority of non-product votes (fixed precedence
``dev_tooling > docs > marketing`` breaks ties); no votes → product.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable, Mapping

from faultline.pipeline_v2.data import load_yaml

_PATTERNS_FILE = "surface-scope-patterns.yaml"

SCOPE_PRODUCT = "product"
SCOPE_SYSTEM = "system"

# Fixed precedence when several non-product scopes compete (documented in the
# pattern file header): most specific / least ambiguous first. ``system`` is
# not lexicon-derived (see module docstring) but keeps its slot for the
# vocabulary + telemetry ordering.
_NON_PRODUCT_PRECEDENCE = ("system", "dev_tooling", "docs", "marketing")

SURFACE_SCOPES = (SCOPE_PRODUCT,) + _NON_PRODUCT_PRECEDENCE

# Path segments that mark the start of URL context in a route file path —
# framework routing roots (Next app/, Next+Astro+Nuxt pages/, Remix/SvelteKit
# routes/). Only segments AFTER the last such marker are matched against the
# url_segments lexicon, so arbitrary source dirs (src/features/blog-model/…)
# never match.
_URL_ROOT_MARKERS = frozenset({"app", "pages", "routes"})

# Monorepo workspace container dirs — ``<container>/<name>`` where <name> is
# checked against the workspace_dirs lexicon.
_WORKSPACE_CONTAINERS = frozenset({"apps", "packages", "sites", "websites", "tools"})

# Route-file basenames that carry no lexical signal of their own.
_NEUTRAL_STEMS = frozenset({"page", "route", "index", "layout", "_index", "default"})

_DYNAMIC_SEG_RE = re.compile(r"^\[.*\]$|^:.+$|^<.+>$|^\{.+\}$|^\*")


def load_patterns() -> dict[str, Any]:
    """Load the runtime pattern file (``{}`` if absent → classifier no-ops)."""
    try:
        return load_yaml(_PATTERNS_FILE) or {}
    except FileNotFoundError:
        return {}


def _invert(block: Mapping[str, Any] | None) -> dict[str, str]:
    """``{scope: [token, …]}`` → ``{token: scope}`` (first scope in
    precedence order wins a duplicate token)."""
    out: dict[str, str] = {}
    if not block:
        return out
    for scope in _NON_PRODUCT_PRECEDENCE:
        for tok in block.get(scope) or []:
            out.setdefault(str(tok).lower(), scope)
    return out


class SurfaceScopeClassifier:
    """Pure, deterministic scope classifier over path/route evidence.

    Importable standalone (offline claim re-scoring uses it directly on
    recorded scan artifacts); the engine wiring lives in
    :func:`tag_user_flows`.
    """

    def __init__(self, patterns: dict | None = None) -> None:
        cfg = patterns if patterns is not None else load_patterns()
        self._groups = _invert(cfg.get("route_groups"))
        self._url = _invert(cfg.get("url_segments"))
        self._workspace = _invert(cfg.get("workspace_dirs"))

    # ── path / route classification ──────────────────────────────

    def classify_path(self, path: str) -> str | None:
        """Scope signal of one file path (``None`` = no signal)."""
        if not path:
            return None
        segs = [s for s in path.replace("\\", "/").lower().split("/") if s]
        hits: set[str] = set()
        # 1. Next route-groups — ``(marketing)`` is the author's own surface
        #    declaration, valid anywhere in the path.
        for seg in segs:
            if seg.startswith("(") and seg.endswith(")"):
                sc = self._groups.get(seg[1:-1]) or self._url.get(seg[1:-1])
                if sc:
                    hits.add(sc)
        # 2. URL-context segments after the LAST routing-root marker.
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
                # filename → stem (``blog.tsx`` → ``blog``); neutral stems drop
                stem = url_segs[-1].rsplit(".", 1)[0]
                url_segs = url_segs[:-1] + ([] if stem in _NEUTRAL_STEMS else [stem])
            for s in url_segs:
                sc = self._url.get(s)
                if sc:
                    hits.add(sc)
        # 3. Workspace dirs — ``apps/docs``, ``packages/cli`` …
        for i, seg in enumerate(segs[:-1]):
            if seg in _WORKSPACE_CONTAINERS:
                sc = self._workspace.get(segs[i + 1])
                if sc:
                    hits.add(sc)
        for sc in _NON_PRODUCT_PRECEDENCE:
            if sc in hits:
                return sc
        return None

    def classify_route(self, route_pattern: str) -> str | None:
        """Scope signal of one URL route pattern (``None`` = no signal)."""
        if not route_pattern:
            return None
        segs = [
            s for s in route_pattern.lower().split("/")
            if s and not _DYNAMIC_SEG_RE.match(s)
        ]
        hits = {self._url[s] for s in segs if s in self._url}
        for sc in _NON_PRODUCT_PRECEDENCE:
            if sc in hits:
                return sc
        return None

    # ── UF-level aggregation ─────────────────────────────────────

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
            return SCOPE_PRODUCT  # a real product route — blocks non-product
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
        """Aggregate member votes + UF route patterns into one scope."""
        if uf_category == SCOPE_SYSTEM:
            return SCOPE_SYSTEM
        votes = [v for v in member_votes if v is not None]
        for r in uf_routes:
            # An unmatched route pattern is product surface (conservative).
            votes.append(self.classify_route(r) or SCOPE_PRODUCT)
        if not votes or SCOPE_PRODUCT in votes:
            return SCOPE_PRODUCT
        counts = Counter(votes)
        best = max(
            counts,
            key=lambda s: (counts[s], -_NON_PRODUCT_PRECEDENCE.index(s)),
        )
        return best


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Field access that works for pydantic models AND plain dicts."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def tag_user_flows(
    user_flows: list[Any],
    flows: list[Any],
    routes_index: list[Mapping[str, Any]] | None = None,
    patterns: dict | None = None,
) -> dict[str, int]:
    """Stamp ``surface_scope`` onto each user flow in place.

    Accepts pydantic models or plain dicts for both ``user_flows`` and
    ``flows``. Returns the ``{scope: count}`` telemetry counter for
    ``scan_meta.surface_scope_counts``.
    """
    clf = SurfaceScopeClassifier(patterns)
    flow_by_key: dict[str, Any] = {}
    for fl in flows:
        for key in (_get(fl, "uuid"), _get(fl, "name")):
            if key:
                flow_by_key.setdefault(str(key), fl)
    route_files: set[str] = set()
    for entry in routes_index or []:
        f = entry.get("file")
        if f:
            route_files.add(str(f))
    counts: dict[str, int] = {}
    for uf in user_flows:
        votes: list[str | None] = []
        for fid in _get(uf, "member_flow_ids") or []:
            fl = flow_by_key.get(str(fid))
            if fl is None:
                continue
            entry = (
                _get(fl, "entry_point_file")
                or _get(_get(fl, "entry_point") or {}, "path")
                or ""
            )
            votes.append(clf.member_vote(
                entry,
                paths=_get(fl, "paths") or [],
                entry_is_route=str(entry) in route_files,
            ))
        scope = clf.classify_user_flow(
            votes,
            uf_routes=_get(uf, "routes") or [],
            uf_category=_get(uf, "category"),
        )
        if isinstance(uf, Mapping):
            uf["surface_scope"] = scope  # type: ignore[index]
        else:
            uf.surface_scope = scope
        counts[scope] = counts.get(scope, 0) + 1
    return counts


__all__ = [
    "SURFACE_SCOPES",
    "SurfaceScopeClassifier",
    "load_patterns",
    "tag_user_flows",
]
