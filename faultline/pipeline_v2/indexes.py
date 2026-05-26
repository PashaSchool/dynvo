"""path_index + routes_index — additive scan-output surfaces.

Sprint 1 (2026-05-23). Both indexes are deterministic projections of
the existing Feature + Flow + route-extractor outputs. They live as
top-level keys on the FeatureMap JSON so MCP tools, the Sentry/PostHog
attribution worker, and the incremental scan merger can do O(1) file →
feature lookups without re-walking ``features[*].paths``.

Schema
======

::

    path_index = {
        "<repo-relative-path>": {
            "feature_uuid": "<uuid hex or empty>",
            "flow_uuids": ["<uuid hex>", ...],
        },
        ...
    }

    routes_index = [
        {
            "pattern": "/api/products",
            "method": "GET",       # or "PAGE" for filesystem routes
            "feature_uuid": "<uuid hex>",
            "file": "src/app/api/products/route.ts",
        },
        ...
    ]

Notes
-----

* A path can be claimed by at most ONE feature (the
  ``Feature.paths`` semantics — owned source code). A path can be
  attached to multiple flows.
* When two features both list the same path (rare — sibling-collapse
  generally prevents this), the first feature in the input order
  wins. We log a warning and continue.
* Empty ``feature_uuid`` is allowed for routes that don't map to a
  feature yet (extractor emitted a route but Stage 2 didn't attribute
  it). The MCP tool reads "empty → unknown ownership".
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── File-path → URL pattern derivation (Fix 2, 2026-05-26) ──────────────
#
# The Stage 1 ``route`` extractor emits group-level ``AnchorCandidate``
# objects that carry only ``paths`` (the route files) — NOT a per-route
# ``{pattern, method}``. Earlier ``build_routes_index`` read ``.pattern``
# / ``.method`` / ``.file`` off each signal, so against real candidates
# every route was skipped and ``routes_index`` came back empty (cal-com:
# routes_index_size=0 despite 94% extractor coverage). We now ALSO derive
# routes deterministically from each candidate's ``paths`` by mapping the
# on-disk file-system routing convention to a URL pattern. No LLM, no
# source read — pure path arithmetic (per ``rule-cold-scan``).

# Routing root path *components* under which a file's directory tree
# encodes the URL. Matched as a component sequence ANYWHERE in the path
# (not just at index 0) so monorepo workspace prefixes — cal.com's
# ``apps/api/v1/pages/api/...`` / ``apps/web/app/...``, any Turborepo /
# pnpm-workspace layout — are transparently stripped. The file having
# been emitted by the route extractor already means it IS a route file
# for this repo's stack; we only need to find where the URL tree begins.
# Ordered longest-first so ``app/routes`` (Remix) wins over bare ``app``.
_ROUTE_ROOT_SEQS: tuple[tuple[str, ...], ...] = (
    ("app", "routes"),   # Remix
    ("src", "routes"),   # SvelteKit / TanStack
    ("src", "app"),      # Next App Router (src dir)
    ("src", "pages"),    # Next Pages Router (src dir)
    ("app",),            # Next App Router
    ("pages",),          # Next Pages Router
    ("routes",),         # generic
)

# Page-file leaf markers — the URL comes from the directory tree, the
# filename itself carries no segment.
_PAGE_MARKERS = {"page", "layout", "index", "+page", "default"}
# API route-handler leaf markers — same (directory tree is the URL).
_API_MARKERS = {"route", "+server"}

# Per-verb leaf-file convention (cal.com / classic Pages-Router REST):
# ``.../pages/api/teams/[id]/_get.ts`` → GET on ``/api/teams/:id``. The
# leading underscore + HTTP verb names the method; the URL is the dir
# tree. Also tolerate the un-prefixed ``get.ts`` form.
_VERB_LEAF_RE = re.compile(r"^_?(?P<verb>get|post|put|patch|delete|head|options)$")

# Convert a file-system dynamic segment to a URL-pattern dynamic token.
# ``[id]`` / ``[...slug]`` / ``[[...slug]]`` → ``:id`` style so the
# display-name deriver's ``_DYNAMIC_SEG_RE`` recognises + drops it.
_FS_DYNAMIC_RE = re.compile(r"^\[+\.{0,3}(?P<name>[^\]]*?)\]+$")
_GROUP_RE = re.compile(r"^\(.*\)$")  # Next.js route group — URL-invisible


def _split_at_route_root(segs: list[str]) -> list[str] | None:
    """Return the segments AFTER the first routing-root component run.

    Searches the whole path so monorepo workspace prefixes are stripped.
    ``["apps","api","v1","pages","api","teams","[id]","_get.ts"]`` with
    the ``("pages",)`` root → ``["api","teams","[id]","_get.ts"]``.
    Returns ``None`` when no routing root is present.
    """
    for i in range(len(segs)):
        for seq in _ROUTE_ROOT_SEQS:
            if segs[i:i + len(seq)] == list(seq):
                return segs[i + len(seq):]
    return None


def _derive_route_from_path(path: str) -> tuple[str, str] | None:
    """Map a route-file path to ``(pattern, method)`` or ``None``.

    ``app/api/teams/[id]/route.ts``               → ``("/api/teams/:id", "GET")``
    ``src/app/(dash)/teams/page.tsx``             → ``("/teams", "PAGE")``
    ``pages/users/[id].tsx``                      → ``("/users/:id", "PAGE")``
    ``apps/api/v1/pages/api/teams/[id]/_get.ts``  → ``("/api/teams/:id", "GET")``

    The routing root is matched as a path-component run ANYWHERE in the
    path so monorepo workspace prefixes (``apps/api/v1/…``, ``apps/web/…``)
    are stripped transparently. Method is the HTTP verb when the leaf is
    a per-verb file (``_get.ts``), ``"PAGE"`` for filesystem pages, and
    ``"GET"`` (conservative read default) for App-Router ``route.ts``
    handlers whose verb we can't know without parsing. Returns ``None``
    when no routing root is present.
    """
    p = path.replace("\\", "/")
    all_segs = [s for s in p.split("/") if s]
    rest_segs = _split_at_route_root(all_segs)
    if rest_segs is None or not rest_segs:
        return None

    dir_segs = rest_segs[:-1]
    fname = rest_segs[-1]
    stem = re.sub(r"\.[A-Za-z0-9+]+$", "", fname)

    method = "PAGE"
    is_marker_leaf = stem in _PAGE_MARKERS or stem in _API_MARKERS
    if stem in _API_MARKERS:
        method = "GET"  # App-Router route.ts — verb unknown; read default
    verb_m = _VERB_LEAF_RE.match(stem)
    if verb_m:
        method = verb_m.group("verb").upper()
        is_marker_leaf = True  # verb file names the method, not a URL seg
    elif stem.startswith("_") and not is_marker_leaf:
        # ``_auth-middleware.ts`` / ``_app.tsx`` — convention-private,
        # not an addressable route. Skip.
        return None

    # Build URL segments from the directory tree; for Pages-Router-style
    # leaf files (``users.tsx`` / ``[id].tsx``) the stem is also a seg.
    url_segs = list(dir_segs)
    if stem and not is_marker_leaf:
        url_segs.append(stem)

    out_segs: list[str] = []
    for seg in url_segs:
        if not seg or _GROUP_RE.match(seg):
            continue  # route groups are URL-invisible
        m = _FS_DYNAMIC_RE.match(seg)
        if m:
            out_segs.append(":" + (m.group("name") or "param"))
        else:
            out_segs.append(seg)

    pattern = "/" + "/".join(out_segs)
    return pattern, method


def build_path_index(
    features: list[dict[str, Any]],
    flows: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build ``{path: {feature_uuid, flow_uuids}}`` from features + flows.

    Args:
        features: list of feature dicts; each must carry ``uuid`` (set
            by lineage assignment) and ``paths``.
        flows: optional list of flow dicts; each carries ``uuid`` and
            ``paths``.

    Returns:
        A dict keyed by path. Stable order via sorted keys is left to
        the JSON writer.
    """
    index: dict[str, dict[str, Any]] = {}

    for feat in features:
        f_uuid = str(feat.get("uuid") or "")
        if not f_uuid:
            continue
        for raw in (feat.get("paths") or []):
            path = str(raw)
            entry = index.setdefault(
                path, {"feature_uuid": "", "flow_uuids": []},
            )
            if entry["feature_uuid"] and entry["feature_uuid"] != f_uuid:
                logger.debug(
                    "path_index: %s already owned by %s; ignoring %s",
                    path, entry["feature_uuid"], f_uuid,
                )
                continue
            entry["feature_uuid"] = f_uuid

    for flow in (flows or []):
        fl_uuid = str(flow.get("uuid") or "")
        if not fl_uuid:
            continue
        for raw in (flow.get("paths") or []):
            path = str(raw)
            entry = index.setdefault(
                path, {"feature_uuid": "", "flow_uuids": []},
            )
            if fl_uuid not in entry["flow_uuids"]:
                entry["flow_uuids"].append(fl_uuid)

    return index


def build_routes_index(
    features: list[dict[str, Any]],
    extractor_signals: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a flat route registry from route-extractor signals.

    The route extractor (``faultline/pipeline_v2/extractors/route.py``)
    emits ``RouteSignal`` candidates with ``pattern``, ``method``, and
    a source ``file``. We map each one to the owning feature via the
    feature's ``paths`` list (the route file must appear in exactly one
    feature). Routes that don't match any feature get
    ``feature_uuid=""`` so the dashboard can surface "orphan route".

    Args:
        features: lineage-assigned features (already carry ``uuid``).
        extractor_signals: Stage 1 output dict (``{extractor_name:
            [Signal, ...]}``). When ``None`` or missing the ``route``
            key, returns an empty list.

    Returns:
        Flat list of route dicts.
    """
    if not extractor_signals:
        return []
    route_signals = extractor_signals.get("route") or []
    if not route_signals:
        return []

    # file -> feature_uuid lookup (first-write-wins, matches path_index)
    file_owner: dict[str, str] = {}
    for feat in features:
        f_uuid = str(feat.get("uuid") or "")
        if not f_uuid:
            continue
        for raw in (feat.get("paths") or []):
            file_owner.setdefault(str(raw), f_uuid)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _emit(pattern: str, method: str, file_str: str) -> None:
        key = (pattern, method, file_str)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "pattern": pattern,
            "method": method,
            "feature_uuid": file_owner.get(file_str, ""),
            "file": file_str,
        })

    for sig in route_signals:
        # Path 1 — a signal that already carries an explicit
        # ``{pattern, method, file}`` (synthetic / future RouteSignal).
        pattern = getattr(sig, "pattern", None) or (
            sig.get("pattern") if isinstance(sig, dict) else None
        )
        if pattern:
            method = getattr(sig, "method", None) or (
                sig.get("method") if isinstance(sig, dict) else "GET"
            )
            source_file = getattr(sig, "file", None) or (
                sig.get("file") if isinstance(sig, dict) else None
            )
            _emit(
                str(pattern), str(method or "GET"),
                str(source_file) if source_file else "",
            )
            continue

        # Path 2 — a real ``AnchorCandidate`` (only ``paths``). Derive a
        # ``{pattern, method}`` per route file from the on-disk routing
        # convention. This is the Fix-2 path that finally populates
        # ``routes_index`` for filesystem-routed (Next.js etc.) repos.
        paths = getattr(sig, "paths", None)
        if paths is None and isinstance(sig, dict):
            paths = sig.get("paths")
        for raw in (paths or []):
            file_str = str(raw)
            derived = _derive_route_from_path(file_str)
            if derived is None:
                continue
            pat, meth = derived
            _emit(pat, meth, file_str)
    return out


__all__ = ["build_path_index", "build_routes_index"]
