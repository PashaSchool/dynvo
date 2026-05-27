"""Stage 3.5 — Flow Expansion orchestrator.

Iterates every Flow on every Feature and enriches it with:

  * ``entry``    — canonical starting point ({file, symbol, lines}).
  * ``nodes[]``  — T1 intra-repo call graph nodes + T2 cross-stack
                   client/server nodes.
  * ``edges[]``  — typed ``import`` / ``call`` / ``cross_stack_http``
                   edges with confidence labels.
  * ``summary``  — roll-up counters (totals, depth, cross_stack_hops,
                   truncated, unsupported_stack).

Backward compatibility per Sprint 1 + the bipartite store contract:
``Flow.paths``, ``Flow.participants``, ``Flow.entry_point_file``,
``Flow.flow_symbol_attributions``, ``Flow.uuid``, and every Stage 5.5
field (``id``, ``primary_feature``, ``secondary_features``,
``shared_with_*_count``, ``cross_cutting``) are preserved unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.models.types import (
    Feature,
    Flow,
    FlowEdge,
    FlowEntryPoint,
    FlowLineRange,
    FlowLocEdge,
    FlowLocNode,
    FlowLocSymbolAttribution,
    FlowNode,
    FlowSummary,
)
# NOTE: flow_display_name.derive_display_name is intentionally NOT imported —
# display_name is reverted to kebab (flow.name). The module stays in-tree for
# a future opt-in. See the display_name block in _attach_loc_detail below.
from faultline.pipeline_v2.flow_expansion.call_graph import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES_PER_FLOW,
    CallGraphResult,
    CallNode,
    build_call_graph,
)
from faultline.pipeline_v2.flow_expansion.cross_stack import (
    CrossStackHit,
    confidence_for_hit,
    find_cross_stack_hits,
)
from faultline.pipeline_v2.flow_reach import (
    ReachContext,
    _is_test_or_vendor_or_generated,
    build_reach_context,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# Supported stacks for full T1 + T2 expansion. Anything outside this
# set gets the graceful-degrade path: entry-only node + summary flagged
# ``unsupported_stack=True``. The set intentionally enumerates by
# language file-suffix (we read from FileSignature, which is language-
# agnostic) so we don't have to dispatch on framework labels here.
_FULLY_SUPPORTED_SUFFIXES: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py",
    ".go",
})

# T1 supported (call graph) — superset of fully-supported; Rust gets
# call-graph but T2 doesn't have idiomatic HTTP client patterns we'd
# match generically.
_T1_SUPPORTED_SUFFIXES: frozenset[str] = _FULLY_SUPPORTED_SUFFIXES | {".rs"}


@dataclass
class FlowExpansionResult:
    """Output of Stage 3.5."""

    features: list[Feature]
    telemetry: dict[str, Any] = field(default_factory=dict)


# ── Helpers ─────────────────────────────────────────────────────────────


def _flow_entry_symbol(flow: Flow) -> str | None:
    """Best-effort entry symbol from existing Stage 3 attributions.

    Sprint C2's ``flow_symbol_attributions`` carries role=``entry`` for
    the resolved entry symbol when detection succeeded. Falls back to
    ``None`` when the legacy Haiku flow detector didn't surface one.
    """
    for fsa in flow.flow_symbol_attributions or []:
        if fsa.role == "entry":
            return fsa.symbol
    return None


def _flow_entry_line(flow: Flow) -> int | None:
    """Best-effort 1-indexed entry line."""
    if flow.entry_point_line is not None:
        return flow.entry_point_line
    for fsa in flow.flow_symbol_attributions or []:
        if fsa.role == "entry":
            return fsa.line_start
    return None


def _to_flow_node(
    cn: CallNode,
    *,
    role: str,
    kind: str,
    confidence: str = "high",
) -> FlowNode:
    return FlowNode(
        id=cn.id,
        kind=kind,  # type: ignore[arg-type]
        file=cn.file,
        symbol=cn.symbol,
        lines=cn.lines,
        role=role,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
    )


def _build_summary(
    *,
    nodes: list[FlowNode],
    edges: list[FlowEdge],
    cross_stack_hops: int,
    max_depth: int,
    truncated: bool,
    unsupported_stack: bool,
) -> FlowSummary:
    total_files = len({n.file for n in nodes})
    total_lines = 0
    for n in nodes:
        if n.lines is not None:
            total_lines += max(0, n.lines[1] - n.lines[0] + 1)
    return FlowSummary(
        total_nodes=len(nodes),
        total_files=total_files,
        total_lines_touched=total_lines,
        cross_stack_hops=cross_stack_hops,
        max_depth=max_depth,
        unsupported_stack=unsupported_stack,
        truncated=truncated,
    )


# Mapping FlowNode.role → the role label surfaced on the LOC parity
# view. We expose entry|step|sink semantics the task asks for: the
# single ``entry`` node stays ``entry``; T2 server endpoints and
# aggregation markers are terminal ``sink``s; everything else is a
# ``step``. The original FlowNode.role is preserved on ``Flow.nodes``.
def _loc_role(node: FlowNode, *, is_sink: bool) -> str:
    if node.role == "entry":
        return "entry"
    if is_sink or node.kind in ("route_handler", "deep_call_subtree"):
        return "sink"
    return "step"


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge a list of (start, end) into non-overlapping sorted spans."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _project_loc_detail(
    flow: Flow,
    routes: list[dict[str, Any]] | None = None,
) -> None:
    """Derive the Phase-5 LOC-parity fields from the already-computed
    Stage 3.5 graph. PURE projection — reads ``flow.entry`` /
    ``flow.nodes`` / ``flow.edges`` / ``flow.flow_symbol_attributions``
    and writes the additive ``entry_point`` / ``line_ranges`` /
    ``loc_symbol_attributions`` / ``loc_nodes`` / ``loc_edges`` /
    ``display_name``. Never mutates an existing pre-Phase-5 field.
    Idempotent.
    """
    # ── entry_point (richer object alongside legacy scalar fields) ──
    if flow.entry:
        ep_lines = flow.entry.get("lines")
        flow.entry_point = FlowEntryPoint(
            path=flow.entry.get("file") or (flow.entry_point_file or ""),
            symbol=flow.entry.get("symbol"),
            line=(ep_lines[0] if ep_lines else flow.entry_point_line),
        )
    elif flow.entry_point_file:
        flow.entry_point = FlowEntryPoint(
            path=flow.entry_point_file,
            symbol=None,
            line=flow.entry_point_line,
        )

    # Identify sink nodes: nodes that never appear as the FROM of any
    # intra-repo edge (no outgoing call/import). Cross-stack servers are
    # always sinks.
    from_ids = {e.from_ for e in flow.edges}
    node_by_id = {n.id: n for n in flow.nodes}

    # ── loc_nodes (landing shape) + per-file span collection ────────
    loc_nodes: list[FlowLocNode] = []
    spans_by_path: dict[str, list[tuple[int, int]]] = {}
    for n in flow.nodes:
        is_sink = (
            n.id not in from_ids and n.role != "entry"
        ) or n.role == "cross_stack_server"
        start = n.lines[0] if n.lines else None
        end = n.lines[1] if n.lines else None
        loc_nodes.append(FlowLocNode(
            path=n.file,
            symbol=n.symbol,
            start_line=start,
            end_line=end,
            role=_loc_role(n, is_sink=is_sink),
        ))
        if start is not None and end is not None:
            spans_by_path.setdefault(n.file, []).append((start, end))
    flow.loc_nodes = loc_nodes

    # ── line_ranges (flow's own merged span, per file) ──────────────
    line_ranges: list[FlowLineRange] = []
    for path in sorted(spans_by_path):
        for start, end in _merge_spans(spans_by_path[path]):
            line_ranges.append(FlowLineRange(
                path=path, start_line=start, end_line=end,
            ))
    flow.line_ranges = line_ranges

    # ── loc_edges (resolved endpoints + call-site) ──────────────────
    loc_edges: list[FlowLocEdge] = []
    for e in flow.edges:
        src = node_by_id.get(e.from_)
        dst = node_by_id.get(e.to)
        if src is None or dst is None:
            continue
        # Call-site: the caller's file + its function start line — the
        # most precise deterministic anchor available without a second
        # AST pass over the caller body.
        call_line = src.lines[0] if src.lines else None
        loc_edges.append(FlowLocEdge(
            from_path=src.file,
            from_symbol=src.symbol,
            to_path=dst.file,
            to_symbol=dst.symbol,
            kind=e.kind,
            call_site={"path": src.file, "line": call_line},
        ))
    flow.loc_edges = loc_edges

    # ── loc_symbol_attributions (full per-participant, parity shape) ─
    # Prefer the precise Stage 3 flow_symbol_attributions when present
    # (they carry roles + symbol-accurate line ranges); always also
    # cover every graph node so a flow whose Stage 3 detection was thin
    # still emits one record per participant. Dedup on
    # (path, symbol, start, end).
    loc_attrs: list[FlowLocSymbolAttribution] = []
    seen: set[tuple[str, str | None, int | None, int | None]] = set()

    def _add(path, symbol, kind, start, end, role):  # noqa: ANN001
        key = (path, symbol, start, end)
        if key in seen:
            return
        seen.add(key)
        loc_attrs.append(FlowLocSymbolAttribution(
            path=path, symbol=symbol, kind=kind,
            start_line=start, end_line=end, role=role,
        ))

    for fsa in flow.flow_symbol_attributions or []:
        _add(
            fsa.file, fsa.symbol, "function",
            fsa.line_start, fsa.line_end, fsa.role,
        )
    for n in flow.nodes:
        start = n.lines[0] if n.lines else None
        end = n.lines[1] if n.lines else None
        _add(n.file, n.symbol, n.kind, start, end, n.role)
    flow.loc_symbol_attributions = loc_attrs

    # ── display_name (REVERTED to kebab per user 2026-05-26) ──────────
    # The human-readable deriver (route > symbol > fb) is intentionally
    # NOT used: the user wants flow labels to stay kebab ("як перед тим
    # було, кебабом"). We mirror the stable kebab ``flow.name`` so any
    # consumer reading ``display_name`` shows kebab too. ``derive_display_name``
    # / ``flow_display_name.py`` are kept in-tree (dormant) for future opt-in.
    # ADDITIVE: only fill when empty so an upstream-assigned label survives.
    if not flow.display_name:
        flow.display_name = flow.name
    # short_label: kebab name without the trailing "-flow"/"-flows" suffix, for
    # compact display ("create-case-flow" -> "create-case"). Additive.
    if not flow.short_label:
        flow.short_label = re.sub(r"-flows?$", "", flow.name)


def _expand_one_flow(
    flow: Flow,
    rctx: ReachContext,
    routes_index: list[dict[str, Any]],
    *,
    max_depth: int,
    max_nodes: int,
) -> tuple[Flow, dict[str, int]]:
    """Return the flow with ``entry`` / ``nodes`` / ``edges`` /
    ``summary`` populated, plus a small per-flow telemetry dict.

    Idempotent: when ``flow.nodes`` is already populated we keep the
    existing graph (callers can re-run Stage 3.5 in replay scenarios
    without churning the output).
    """
    if flow.nodes:
        return flow, {
            "skipped_already_expanded": 1,
            "nodes": len(flow.nodes),
            "edges": len(flow.edges),
            "cross_stack_hops": (flow.summary.cross_stack_hops if flow.summary else 0),
        }

    entry_file = flow.entry_point_file or (
        flow.paths[0] if flow.paths else None
    )
    if not entry_file:
        # No entry point at all — emit empty graph + summary.
        flow.entry = None
        flow.nodes = []
        flow.edges = []
        flow.summary = _build_summary(
            nodes=[], edges=[], cross_stack_hops=0,
            max_depth=0, truncated=False, unsupported_stack=True,
        )
        return flow, {"no_entry": 1}

    suffix = Path(entry_file).suffix.lower()
    fully_supported = suffix in _FULLY_SUPPORTED_SUFFIXES
    t1_supported = suffix in _T1_SUPPORTED_SUFFIXES

    entry_symbol = _flow_entry_symbol(flow)
    entry_line = _flow_entry_line(flow)

    # Graceful degrade for unsupported stacks (Ruby/Java/PHP/etc.):
    # emit a single entry-only node + flag the summary.
    if not t1_supported:
        entry_node = FlowNode(
            id=f"{entry_file}#{entry_symbol}" if entry_symbol else entry_file,
            kind="entry",
            file=entry_file,
            symbol=entry_symbol,
            lines=(
                (entry_line, entry_line)
                if entry_line is not None else None
            ),
            role="entry",
            confidence="low",
        )
        flow.entry = {
            "file": entry_file,
            "symbol": entry_symbol,
            "lines": list(entry_node.lines) if entry_node.lines else None,
        }
        flow.nodes = [entry_node]
        flow.edges = []
        flow.summary = _build_summary(
            nodes=[entry_node], edges=[],
            cross_stack_hops=0, max_depth=0,
            truncated=False, unsupported_stack=True,
        )
        return flow, {
            "unsupported_stack": 1,
            "nodes": 1, "edges": 0, "cross_stack_hops": 0,
        }

    # T1 — intra-repo call graph.
    cg: CallGraphResult = build_call_graph(
        rctx,
        entry_file=entry_file,
        entry_symbol=entry_symbol,
        entry_line=entry_line,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )

    # Translate internal CallNode/CallEdge → schema FlowNode/FlowEdge.
    node_map: dict[str, FlowNode] = {}
    for i, cn in enumerate(cg.nodes):
        if i == 0:
            kind = "entry"
            role = "entry"
        elif cn.symbol is not None:
            kind = "function"
            role = "called"
        else:
            kind = "file"
            role = "support"
        node_map[cn.id] = _to_flow_node(
            cn, role=role, kind=kind,
            confidence="high" if cn.symbol else "medium",
        )
    flow_nodes: list[FlowNode] = list(node_map.values())
    flow_edges: list[FlowEdge] = [
        FlowEdge(
            from_=e.from_id, to=e.to_id, kind=e.kind,  # type: ignore[arg-type]
            confidence=e.confidence,                    # type: ignore[arg-type]
        )
        for e in cg.edges
    ]

    # Emit deep_call_subtree aggregation node when truncated.
    if cg.truncated and cg.dropped_node_count > 0:
        agg_id = f"<deep:{entry_file}#{entry_symbol or '<file>'}>"
        flow_nodes.append(FlowNode(
            id=agg_id,
            kind="deep_call_subtree",
            file=entry_file,
            symbol=None,
            lines=None,
            role="support",
            confidence="low",
            count=cg.dropped_node_count,
        ))

    # T2 — cross-stack HTTP boundary (only on fully-supported suffixes;
    # Rust call-graph survives but Rust doesn't have a generic HTTP
    # client pattern we'd match reliably).
    cross_stack_hops = 0
    if fully_supported and routes_index:
        client_files_seen: set[tuple[str, str | None]] = set()
        for cn in cg.nodes:
            csig = rctx.signatures.get(cn.file)
            if csig is None or not csig.source:
                continue
            client_key = (cn.file, cn.symbol)
            if client_key in client_files_seen:
                continue
            # Slice source by symbol lines when we have them; otherwise
            # scan the whole file.
            if cn.lines is not None:
                src_lines = csig.source.splitlines()
                slice_ = "\n".join(
                    src_lines[cn.lines[0] - 1: cn.lines[1]],
                )
            else:
                slice_ = csig.source
            hits: list[CrossStackHit] = find_cross_stack_hits(
                client_file=cn.file,
                client_symbol=cn.symbol,
                source_slice=slice_,
                routes_index=routes_index,
            )
            if not hits:
                continue
            client_files_seen.add(client_key)
            for hit in hits:
                # Don't cross-link to the same file (avoid self-loops
                # when a route handler ALSO fetches itself).
                if hit.route_file == cn.file:
                    continue
                if _is_test_or_vendor_or_generated(hit.route_file):
                    continue
                # Mark the source node as cross_stack_client.
                src_node = node_map.get(cn.id)
                if src_node is not None and src_node.role != "entry":
                    # Add a parallel "fetch_call" node so the edge has
                    # a precise origin (preserves entry as separate).
                    fetch_id = f"{cn.file}#fetch:{hit.url}"
                else:
                    fetch_id = cn.id
                if fetch_id != cn.id and fetch_id not in node_map:
                    fetch_node = FlowNode(
                        id=fetch_id,
                        kind="fetch_call",
                        file=cn.file,
                        symbol=cn.symbol,
                        lines=cn.lines,
                        role="cross_stack_client",
                        confidence=confidence_for_hit(hit),  # type: ignore[arg-type]
                    )
                    node_map[fetch_id] = fetch_node
                    flow_nodes.append(fetch_node)
                # Server-side route handler node.
                server_id = f"{hit.route_file}#{hit.route_method}:{hit.route_pattern}"
                if server_id not in node_map:
                    server_node = FlowNode(
                        id=server_id,
                        kind="route_handler",
                        file=hit.route_file,
                        symbol=None,
                        lines=None,
                        role="cross_stack_server",
                        confidence=confidence_for_hit(hit),  # type: ignore[arg-type]
                    )
                    node_map[server_id] = server_node
                    flow_nodes.append(server_node)
                flow_edges.append(FlowEdge(
                    from_=fetch_id,
                    to=server_id,
                    kind="cross_stack_http",
                    confidence=confidence_for_hit(hit),  # type: ignore[arg-type]
                ))
                cross_stack_hops += 1

    flow.entry = {
        "file": entry_file,
        "symbol": entry_symbol,
        "lines": (
            list(flow_nodes[0].lines) if flow_nodes and flow_nodes[0].lines
            else None
        ),
    }
    flow.nodes = flow_nodes
    flow.edges = flow_edges
    flow.summary = _build_summary(
        nodes=flow_nodes,
        edges=flow_edges,
        cross_stack_hops=cross_stack_hops,
        max_depth=cg.depth_reached,
        truncated=cg.truncated,
        unsupported_stack=False,
    )
    return flow, {
        "nodes": len(flow_nodes),
        "edges": len(flow_edges),
        "cross_stack_hops": cross_stack_hops,
        "depth_reached": cg.depth_reached,
        "truncated": 1 if cg.truncated else 0,
    }


# ── Public entry point ──────────────────────────────────────────────────


def expand_flows(
    features: list[Feature],
    ctx: "ScanContext",
    routes_index: list[dict[str, Any]] | None = None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES_PER_FLOW,
    log: "StageLogger | None" = None,
    top_level_flows: list[Flow] | None = None,
) -> FlowExpansionResult:
    """Run Stage 3.5 over every flow on every feature.

    Args:
        features: Stage 5.5-emitted feature list (carries Flow objects
            with the bipartite fields populated).
        ctx: Stage 0 :class:`ScanContext`.
        routes_index: Sprint 1 ``routes_index`` projection. Required
            for T2; pass ``None`` or ``[]`` to disable cross-stack
            resolution (T1 still runs).
        max_depth: per-flow BFS depth cap (default 4).
        max_nodes: per-flow node cap (default 80).
        log: optional :class:`StageLogger` from the orchestrator.
        top_level_flows: Sprint B1 top-level flows array. When
            provided, the same expansion is mirrored onto those Flow
            objects (so consumers reading the bipartite store see the
            same graph as the containment view).
    """
    rctx = build_reach_context(ctx)
    routes = routes_index or []

    flows_expanded = 0
    flows_skipped = 0
    flows_unsupported = 0
    flows_truncated = 0
    flows_no_entry = 0
    nodes_total = 0
    edges_total = 0
    cross_stack_total = 0
    deepest_depth = 0
    per_flow_telemetry: list[dict[str, Any]] = []

    # Mutate flows in place under their owning features.
    flow_by_uuid: dict[str, Flow] = {}
    for feat in features:
        for fl in feat.flows or []:
            new_fl, tel = _expand_one_flow(
                fl, rctx, routes,
                max_depth=max_depth, max_nodes=max_nodes,
            )
            # Phase 5 — additive LOC-detail projection over the graph
            # just built (or the pre-existing one in the skip path).
            _project_loc_detail(new_fl, routes)
            if tel.get("skipped_already_expanded"):
                flows_skipped += 1
            else:
                flows_expanded += 1
            if tel.get("unsupported_stack"):
                flows_unsupported += 1
            if tel.get("no_entry"):
                flows_no_entry += 1
            if tel.get("truncated"):
                flows_truncated += 1
            nodes_total += tel.get("nodes", 0)
            edges_total += tel.get("edges", 0)
            cross_stack_total += tel.get("cross_stack_hops", 0)
            deepest_depth = max(deepest_depth, tel.get("depth_reached", 0))
            if new_fl.uuid:
                flow_by_uuid[new_fl.uuid] = new_fl
            if log is not None and tel.get("cross_stack_hops"):
                log.info(
                    f"flow={fl.name} cross_stack_hops={tel['cross_stack_hops']}",
                )
            per_flow_telemetry.append({
                "name": fl.name,
                "uuid": fl.uuid,
                **tel,
            })

    # Mirror onto the top-level bipartite flow list (same Flow object
    # identity? not guaranteed — match by uuid).
    if top_level_flows:
        for tlf in top_level_flows:
            src = flow_by_uuid.get(tlf.uuid)
            if src is None:
                continue
            tlf.entry = src.entry
            tlf.nodes = list(src.nodes)
            tlf.edges = list(src.edges)
            tlf.summary = src.summary
            # Phase 5 — mirror the additive LOC-detail so the bipartite
            # top-level flows[] view stays consistent with containment.
            tlf.entry_point = src.entry_point
            # Phase 5 — mirror the deterministic display label so the
            # bipartite top-level flows[] view matches containment. Only
            # fill when empty (preserve any upstream-assigned label).
            if not tlf.display_name and src.display_name:
                tlf.display_name = src.display_name
            tlf.line_ranges = list(src.line_ranges)
            tlf.loc_symbol_attributions = list(src.loc_symbol_attributions)
            tlf.loc_nodes = list(src.loc_nodes)
            tlf.loc_edges = list(src.loc_edges)

    telemetry: dict[str, Any] = {
        "flows_expanded": flows_expanded,
        "flows_skipped_already_expanded": flows_skipped,
        "flows_unsupported_stack": flows_unsupported,
        "flows_no_entry_point": flows_no_entry,
        "flows_truncated": flows_truncated,
        "nodes_total": nodes_total,
        "edges_total": edges_total,
        "cross_stack_hops_total": cross_stack_total,
        "deepest_depth_reached": deepest_depth,
        "max_depth_configured": max_depth,
        "max_nodes_per_flow_configured": max_nodes,
        "routes_index_size": len(routes),
    }
    return FlowExpansionResult(features=features, telemetry=telemetry)


__all__ = [
    "FlowExpansionResult",
    "expand_flows",
]
