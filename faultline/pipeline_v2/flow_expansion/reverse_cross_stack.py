"""Reverse cross-stack — attach FRONTEND UI-layer callers to backend flows.

The forward T2 pass (:mod:`faultline.pipeline_v2.flow_expansion.cross_stack`)
links a flow's OWN call-graph nodes to the routes they ``fetch()``. That
only fires when a frontend file is already a node of the flow's graph —
i.e. for FRONTEND-seeded flows. BACKEND-seeded flows (FastAPI / Express /
Rails route handlers traced backend-only) have NO frontend node, so the
forward pass never reaches them and they end up with ZERO frontend
participants. With no frontend surface, Stage 6.7b's ``ui_tier`` can only
ever resolve to ``no-ui``.

This module closes the gap from the OTHER side. It builds a REVERSE index
once per scan:

    scan every frontend source file → find its HTTP-client call sites
    (reusing :func:`find_cross_stack_hits`) → match each URL against the
    Sprint-1 ``routes_index`` → record  route → {frontend files}.

Then, per backend-seeded flow, it looks up the flow's entry route (the
route handler file the flow starts from, plus any ``cross_stack_server``
route nodes already on the graph) and attaches every frontend caller of
that route as a :class:`FlowParticipant` with a NEW distinct role
``"ui"`` (and ``layer="ui"``).

Design constraints honoured
===========================

  * **Reuses the cross_stack matcher** — the URL-literal extraction and
    routes_index matcher come straight from
    :func:`faultline.pipeline_v2.flow_expansion.cross_stack.find_cross_stack_hits`.
    No forked regexes.
  * **Universal, not stack-specific** ([[rule-no-repo-specific-paths]]) —
    "frontend file" = a source file with a JS/TS/Vue/Svelte suffix that
    is NOT itself a route handler in ``routes_index`` and is NOT a test /
    vendor / generated file. The discriminator that makes a file a "UI
    caller" is that it ACTUALLY contains an HTTP-client call matching a
    known route — purely code-grounded, no folder-name assumptions.
  * **Additive** — the ``ui`` participants are a separate
    :class:`FlowParticipant` role. They are NEVER nodes / edges /
    flow_symbol_attributions, so they cannot enter the flow's core-LOC
    projection (see ``expander._project_loc_detail``, which reads only
    nodes/edges/flow_symbol_attributions). ``test_files`` is likewise a
    separate field. The flow-LOC fix is untouched.
  * **Deterministic, no LLM, no README** — pure regex + path matching.
  * **No magic numbers** — every threshold is structural (suffix set,
    test/vendor markers, route-membership), none tuned to a corpus repo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.flow_expansion.cross_stack import (
    CrossStackHit,
    find_cross_stack_hits,
)

if TYPE_CHECKING:
    from faultline.models.types import Flow
    from faultline.pipeline_v2.flow_reach import ReachContext

logger = logging.getLogger(__name__)


# Frontend (UI-layer) source suffixes. A file with one of these MIGHT be a
# UI caller; the actual HTTP-client-call discriminator decides. ``.vue`` /
# ``.svelte`` carry an embedded <script> block we can still regex-scan for
# fetch/$fetch/useFetch literals.
_FRONTEND_SUFFIXES: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
})

# Markers that exclude a candidate from being treated as a UI caller. We
# reuse the central test/vendor/generated detector from flow_reach for
# vendor/generated; tests are excluded via is_test_file.
_MAX_FILE_BYTES = 512_000  # skip pathologically large bundles/minified files


@dataclass
class ReverseCrossStackIndex:
    """Route → frontend-caller lookups built once per scan.

    ``by_route_file`` keys on the route handler FILE (matches a flow's
    ``entry_point_file`` / a ``cross_stack_server`` node's file).
    ``by_route_pattern`` keys on the normalised route PATTERN (matches a
    flow whose entry is described by a pattern rather than a file).
    Values are ordered, de-duplicated frontend file paths.
    """

    by_route_file: dict[str, list[str]] = field(default_factory=dict)
    by_route_pattern: dict[str, list[str]] = field(default_factory=dict)
    # Frontend files that matched at least one route (for telemetry).
    frontend_callers: set[str] = field(default_factory=set)
    routes_matched: int = 0

    def callers_for(
        self, *, route_file: str | None, route_patterns: list[str]
    ) -> list[str]:
        """Frontend files calling a route, looked up by file then patterns."""
        out: list[str] = []
        seen: set[str] = set()
        if route_file:
            for f in self.by_route_file.get(route_file, []):
                if f not in seen:
                    seen.add(f)
                    out.append(f)
        for pat in route_patterns:
            for f in self.by_route_pattern.get(pat, []):
                if f not in seen:
                    seen.add(f)
                    out.append(f)
        return out


def _is_frontend_candidate(path: str, route_files: frozenset[str]) -> bool:
    """True when ``path`` could be a frontend UI caller of a backend route.

    A candidate is a JS/TS/Vue/Svelte source file that is NOT itself a
    route handler (so we never label a backend route file as its own UI
    caller) and NOT a test / vendor / generated file.
    """
    from faultline.analyzer.validation import is_test_file
    from faultline.pipeline_v2.flow_reach import _is_test_or_vendor_or_generated

    suffix = Path(path).suffix.lower()
    if suffix not in _FRONTEND_SUFFIXES:
        return False
    if path in route_files:
        return False
    if is_test_file(path):
        return False
    if _is_test_or_vendor_or_generated(path):
        return False
    return True


def _read_source(rctx: "ReachContext", path: str) -> str | None:
    """Read a frontend file's source, preferring the pre-parsed signature.

    Falls back to a bounded on-disk read. Returns ``None`` on any IO
    failure or when the file is implausibly large (minified bundle).
    """
    sig = rctx.signatures.get(path)
    src = getattr(sig, "source", None) if sig is not None else None
    if src:
        return src
    try:
        abs_path = Path(rctx.repo_path) / path
        if abs_path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return abs_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return None


def build_reverse_index(
    rctx: "ReachContext",
    routes_index: list[dict[str, Any]],
) -> ReverseCrossStackIndex:
    """Scan every frontend source file for HTTP-client calls → route map.

    Reuses :func:`find_cross_stack_hits` so the URL-literal extraction and
    routes_index matcher are identical to the forward T2 pass.
    """
    index = ReverseCrossStackIndex()
    if not routes_index:
        return index

    route_files: frozenset[str] = frozenset(
        str(e.get("file") or "") for e in routes_index if e.get("file")
    )

    for path in sorted(rctx.file_set):
        if not _is_frontend_candidate(path, route_files):
            continue
        source = _read_source(rctx, path)
        if not source:
            continue
        hits: list[CrossStackHit] = find_cross_stack_hits(
            client_file=path,
            client_symbol=None,
            source_slice=source,
            routes_index=routes_index,
        )
        if not hits:
            continue
        index.frontend_callers.add(path)
        for hit in hits:
            if hit.route_file:
                bucket = index.by_route_file.setdefault(hit.route_file, [])
                if path not in bucket:
                    bucket.append(path)
            if hit.route_pattern:
                pbucket = index.by_route_pattern.setdefault(
                    hit.route_pattern, [],
                )
                if path not in pbucket:
                    pbucket.append(path)

    index.routes_matched = len(index.by_route_file) + len(
        index.by_route_pattern,
    )
    return index


def _flow_route_targets(flow: "Flow") -> tuple[str | None, list[str]]:
    """The route file + patterns that identify a backend flow's entry route.

    Sources (all already on the flow, no new computation):
      * ``entry_point_file`` — the route handler file the flow starts at.
      * ``cross_stack_server`` nodes — server route handlers already
        linked on the graph (carry file + a ``METHOD:pattern`` id tail).
    """
    route_file = flow.entry_point_file
    patterns: list[str] = []
    seen: set[str] = set()
    for n in flow.nodes or []:
        if getattr(n, "role", None) == "cross_stack_server":
            nid = getattr(n, "id", "") or ""
            # id shape: "{route_file}#{METHOD}:{pattern}"
            if "#" in nid and ":" in nid:
                tail = nid.split("#", 1)[1]
                pat = tail.split(":", 1)[1] if ":" in tail else ""
                if pat and pat not in seen:
                    seen.add(pat)
                    patterns.append(pat)
    return route_file, patterns


def attach_ui_participants(
    flow: "Flow",
    index: ReverseCrossStackIndex,
) -> int:
    """Attach frontend callers of a flow's route as ``ui`` participants.

    Idempotent + additive: appends a :class:`FlowParticipant` with
    ``layer="ui"`` / ``role="ui"`` for each frontend caller not already a
    participant. Never touches nodes / edges / symbol attributions, so the
    flow's core-LOC projection is unaffected. Returns the count attached.
    """
    from faultline.models.types import FlowParticipant

    route_file, patterns = _flow_route_targets(flow)
    callers = index.callers_for(route_file=route_file, route_patterns=patterns)
    if not callers:
        return 0

    existing = {p.path for p in (flow.participants or [])}
    attached = 0
    new_parts = list(flow.participants or [])
    for fe in callers:
        if fe in existing:
            # Promote an existing participant to ui-layer only if it is
            # not already a richer layer; safest is to leave it and skip.
            continue
        new_parts.append(FlowParticipant(path=fe, layer="ui", role="ui"))
        existing.add(fe)
        attached += 1
    if attached:
        flow.participants = new_parts
    return attached


def attach_reverse_cross_stack(
    flows: list["Flow"],
    rctx: "ReachContext",
    routes_index: list[dict[str, Any]],
    *,
    index: ReverseCrossStackIndex | None = None,
) -> dict[str, Any]:
    """Run the reverse cross-stack pass over a flow list (in place).

    Args:
        flows: every flow to enrich (containment + top-level bipartite).
            Pass the union; duplicates by identity are fine (idempotent).
        rctx: the shared :class:`ReachContext` (gives file_set + source).
        routes_index: the Sprint-1 ``routes_index``.
        index: a pre-built :class:`ReverseCrossStackIndex` (built once for
            the whole scan and reused across the two flow lists). When
            ``None`` it is built here.

    Returns:
        Telemetry dict (additive into ``scan_meta``).
    """
    if index is None:
        index = build_reverse_index(rctx, routes_index)

    ui_attached_total = 0
    flows_with_ui = 0
    for fl in flows:
        n = attach_ui_participants(fl, index)
        if n:
            ui_attached_total += n
            flows_with_ui += 1

    return {
        "frontend_callers_scanned": len(index.frontend_callers),
        "routes_with_ui_callers": len(index.by_route_file),
        "patterns_with_ui_callers": len(index.by_route_pattern),
        "ui_participants_attached": ui_attached_total,
        "flows_with_ui_participants": flows_with_ui,
    }


__all__ = [
    "ReverseCrossStackIndex",
    "build_reverse_index",
    "attach_ui_participants",
    "attach_reverse_cross_stack",
]
