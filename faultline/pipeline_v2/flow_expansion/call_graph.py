"""T1 — intra-repo static call graph (deterministic, no LLM).

Walks from a flow's entry symbol outward to surface every reachable
function / method in the repo via two layered signals:

  1. **Import edges** — file A imports file B (handled by the existing
     :mod:`faultline.pipeline_v2.flow_reach` resolvers).
  2. **Call edges** — A's source body references an identifier that
     resolves to a symbol exported by B.

Pure regex / AST-signature work — no tree-sitter at this layer (the
project already ships a tree-sitter-free signature extractor for every
supported language; we layer on top of that to keep deployment surface
flat). Adapters in ``flow_expansion/adapters/`` can add framework-
specific edge kinds without touching this module.

Caps
====
  - ``MAX_DEPTH = 4`` — bound on SAME-FILE callee recursion (a handler
    calling a private helper that calls another private helper in the
    same module).
  - ``CROSS_FILE_MAX_DEPTH = 1`` — cross-file ``call`` / ``import``
    edges may be emitted ONLY from a node whose depth is strictly less
    than this bound. With the default of 1, that means only the ENTRY
    node (depth 0) fans out across file boundaries: a flow's attributed
    implementation is the entry symbol plus its DIRECT callees (same-
    file AND imported), with NO further cross-file recursion.

    Why: cross-file BFS at ``MAX_DEPTH`` collapses the narrative-slice
    property — each flow becomes the whole transitive closure of the
    repo's import graph (measured on a FastAPI service: avg 62.5
    nodes/flow, 235/447 flows hitting the node cap). A flow is a
    reading lens over ONE behaviour, not the program's call tree. See
    ``flow-feature-concept``: a flow's identity is its narrative slice.
  - ``MAX_NODES_PER_FLOW = 80`` — beyond this we emit a single
    ``deep_call_subtree`` aggregation node and stop expansion. With the
    depth-1 cross-file cap this should rarely bind.
  - Test / vendor / generated files filtered via the same markers
    used by :mod:`flow_reach` (single source of truth: reuse the
    helper there).
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field

from faultline.analyzer.ast_extractor import FileSignature
from faultline.pipeline_v2.flow_reach import (
    ReachContext,
    _file_edges,
    _is_test_or_vendor_or_generated,
)
from faultline.pipeline_v2.flow_expansion.confidence import (
    confidence_for_call,
    confidence_for_import,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 4
DEFAULT_CROSS_FILE_MAX_DEPTH = 1
DEFAULT_MAX_NODES_PER_FLOW = 80

# Common JS / TS / Python identifier — used to scan a function body
# for callee identifiers. Conservative: requires the identifier to
# appear word-boundary-delimited and followed by ``(`` (a real call).
# This intentionally over-matches (e.g. constructor calls, JSX
# component instantiation) — the resolver only emits an edge when the
# identifier maps to a real export in a reachable file, so the
# downstream signal is precision-filtered.
_IDENT_CALL_PATTERN = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(",
)


@dataclass(frozen=True)
class CallNode:
    """One node in the intra-repo call graph (T1 internal)."""

    id: str                    # "<file>#<symbol>" or "<file>"
    file: str
    symbol: str | None
    lines: tuple[int, int] | None
    depth: int


@dataclass(frozen=True)
class CallEdge:
    """One directed edge in the intra-repo call graph (T1 internal)."""

    from_id: str
    to_id: str
    kind: str                  # "import" | "call"
    confidence: str            # "high" | "medium" | "low"


@dataclass
class CallGraphResult:
    """Output of T1 — internal dataclass; expander converts to schema."""

    nodes: list[CallNode] = field(default_factory=list)
    edges: list[CallEdge] = field(default_factory=list)
    depth_reached: int = 0
    truncated: bool = False
    dropped_node_count: int = 0


# ── Helpers ─────────────────────────────────────────────────────────────


def _node_id(file: str, symbol: str | None) -> str:
    return f"{file}#{symbol}" if symbol else file


def _symbol_line_range(
    sig: FileSignature | None,
    symbol: str,
) -> tuple[int, int] | None:
    """Look up a symbol's ``(start_line, end_line)`` in a signature.

    Returns ``None`` when the signature has no range data (e.g. some
    legacy extractors only record names).
    """
    if sig is None:
        return None
    for rng in sig.symbol_ranges:
        if rng.name == symbol:
            return (rng.start_line, rng.end_line)
    return None


def _file_exports(sig: FileSignature | None) -> tuple[str, ...]:
    """Stable tuple of exported symbol names; empty when unknown."""
    if sig is None:
        return ()
    return tuple(sig.exports)


def _extract_called_identifiers(
    source: str,
    start_line: int | None,
    end_line: int | None,
) -> set[str]:
    """Collect identifiers used as function calls in a slice of source.

    When ``start_line`` / ``end_line`` are ``None`` the whole source is
    scanned (graceful degrade when we don't know the symbol's bounds).
    """
    if not source:
        return set()
    if start_line is not None and end_line is not None and end_line >= start_line:
        lines = source.splitlines()
        slice_ = "\n".join(lines[start_line - 1: end_line])
    else:
        slice_ = source
    return {m.group(1) for m in _IDENT_CALL_PATTERN.finditer(slice_)}


# ── Public entry point ──────────────────────────────────────────────────


def build_call_graph(
    rctx: ReachContext,
    entry_file: str,
    entry_symbol: str | None,
    entry_line: int | None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    cross_file_max_depth: int = DEFAULT_CROSS_FILE_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES_PER_FLOW,
) -> CallGraphResult:
    """BFS from ``(entry_file, entry_symbol)`` over import + call edges.

    Algorithm:

      1. Seed the queue with the entry node. If ``entry_symbol`` is
         ``None``, the seed is a file-level node and only import
         edges fan out.
      2. For each dequeued node, compute its one-hop neighbours:
         a. File-level imports via :func:`flow_reach._file_edges` —
            emits ``import`` edges to the imported file.
         b. Call-identifiers within the symbol's body — for each
            identifier, look up the file that exports it among the
            files this file imports, emit a ``call`` edge to the
            ``<callee_file>#<callee_symbol>`` node.
      3. Stop when EITHER max_depth reached OR node cap hit.

    Cross-file depth gate
    ---------------------
    A neighbour in a DIFFERENT file (cross-file ``call`` / ``import``
    edge) is only expanded when ``current.depth < cross_file_max_depth``.
    With the default of 1 only the entry node fans out across files, so
    the graph is "entry + its direct callees" rather than the whole
    transitive import closure. Same-file callees keep recursing up to
    ``max_depth`` (a handler → private helper → private helper chain
    inside ONE module is still part of that behaviour's narrative).

    Per [[rule-cold-scan]]: pure in-memory; no persistence.
    """
    if max_depth < 1:
        max_depth = 1
    if cross_file_max_depth < 0:
        cross_file_max_depth = 0
    if max_nodes < 1:
        max_nodes = 1

    entry_sig = rctx.signatures.get(entry_file)
    entry_range = (
        _symbol_line_range(entry_sig, entry_symbol)
        if entry_symbol
        else None
    )
    if entry_range is None and entry_line is not None:
        entry_range = (entry_line, entry_line)

    seed = CallNode(
        id=_node_id(entry_file, entry_symbol),
        file=entry_file,
        symbol=entry_symbol,
        lines=entry_range,
        depth=0,
    )

    nodes: list[CallNode] = [seed]
    edges: list[CallEdge] = []
    seen_ids: set[str] = {seed.id}
    depth_reached = 0
    truncated = False
    dropped = 0

    queue: deque[CallNode] = deque([seed])
    while queue:
        if len(nodes) >= max_nodes:
            # Count remaining as dropped for telemetry.
            dropped += len(queue)
            truncated = True
            break
        current = queue.popleft()
        if current.depth >= max_depth:
            continue

        # Skip neighbour expansion for non-source / aggregation files.
        if _is_test_or_vendor_or_generated(current.file):
            continue

        # Cross-file edges fan out only from nodes shallower than the
        # cross-file bound (default: entry only). Beyond that we keep
        # recursing through SAME-FILE callees but never pull in another
        # file's symbols — that is what keeps a flow a narrative slice
        # rather than the whole transitive closure.
        allow_cross_file = current.depth < cross_file_max_depth

        # (a) File-level import edges.
        try:
            imported_files = _file_edges(
                current.file,
                rctx.signatures,
                rctx.file_set,
                alias_map=rctx.alias_map,
                monorepo_packages=rctx.monorepo_packages,
                go_module_prefix=rctx.go_module_prefix,
                repo_path=rctx.repo_path,
                python_source_roots=rctx.python_source_roots,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "call_graph: import-edge extraction failed for %s: %s",
                current.file, exc,
            )
            imported_files = []

        # Filter once. Drop ALL cross-file imports when the current node
        # is past the cross-file bound — imports are by definition edges
        # to another file, so beyond depth-1 they would re-open the
        # transitive closure we are explicitly excluding.
        imported_files = [
            f for f in imported_files
            if not _is_test_or_vendor_or_generated(f)
            and (allow_cross_file or f == current.file)
        ]

        # (b) Call-identifier edges (intra-symbol → callee file/symbol).
        # ``same_file_callees`` is tracked separately so its edges carry
        # the ``same_file=True`` confidence label.
        callees_by_symbol: dict[tuple[str, str], CallNode] = {}
        same_file_keys: set[tuple[str, str]] = set()
        if current.symbol is not None:
            src_sig = rctx.signatures.get(current.file)
            src = src_sig.source if src_sig else ""
            line_start = current.lines[0] if current.lines else None
            line_end = current.lines[1] if current.lines else None
            call_idents = _extract_called_identifiers(
                src, line_start, line_end,
            )

            # (b.0) Same-file callees — a handler calling a private
            # helper / method defined in its OWN module. Previously
            # unresolved (the resolver only scanned imported files),
            # which is why dynamic-language flows with in-module helpers
            # (Python ``self._helper()``, JS local funcs) collapsed to a
            # single entry node. Resolve against the current file's own
            # symbol_ranges; skip the entry symbol itself to avoid a
            # self-loop.
            own_ranges = src_sig.symbol_ranges if src_sig else []
            for rng in own_ranges:
                sym = rng.name
                if sym == current.symbol:
                    continue
                if sym not in call_idents:
                    continue
                key = (current.file, sym)
                if key in callees_by_symbol:
                    continue
                callees_by_symbol[key] = CallNode(
                    id=_node_id(current.file, sym),
                    file=current.file,
                    symbol=sym,
                    lines=(rng.start_line, rng.end_line),
                    depth=current.depth + 1,
                )
                same_file_keys.add(key)

            # (b.1) Cross-file callees — identifier → (callee_file,
            # callee_sig) restricted to files this file imports.
            for cf in imported_files:
                csig = rctx.signatures.get(cf)
                exports = _file_exports(csig)
                if not exports:
                    continue
                for sym in exports:
                    if sym in call_idents:
                        rng = _symbol_line_range(csig, sym)
                        callees_by_symbol[(cf, sym)] = CallNode(
                            id=_node_id(cf, sym),
                            file=cf,
                            symbol=sym,
                            lines=rng,
                            depth=current.depth + 1,
                        )

        # Emit call edges first (more precise than import edges).
        for (cf, sym), node in callees_by_symbol.items():
            if len(nodes) >= max_nodes:
                dropped += 1
                truncated = True
                continue
            if node.id not in seen_ids:
                seen_ids.add(node.id)
                nodes.append(node)
                queue.append(node)
                depth_reached = max(depth_reached, node.depth)
            edges.append(CallEdge(
                from_id=current.id,
                to_id=node.id,
                kind="call",
                confidence=confidence_for_call(
                    resolved_symbol=True,
                    same_file=(cf, sym) in same_file_keys,
                ),
            ))

        # Emit import edges to any imported file we DIDN'T resolve to a
        # symbol — that gives the graph file-level reach even when we
        # can't pin down the callee. Always add the file-node so the
        # graph has structure to walk on the next iteration.
        for cf in imported_files:
            file_node_id = _node_id(cf, None)
            # Skip if any callee from this file was already covered
            # via a call edge AND a node entry — avoids redundant
            # file-node duplication.
            already_via_call = any(
                k[0] == cf for k in callees_by_symbol
            )
            if already_via_call:
                continue
            if len(nodes) >= max_nodes:
                dropped += 1
                truncated = True
                continue
            if file_node_id not in seen_ids:
                seen_ids.add(file_node_id)
                file_node = CallNode(
                    id=file_node_id,
                    file=cf,
                    symbol=None,
                    lines=None,
                    depth=current.depth + 1,
                )
                nodes.append(file_node)
                queue.append(file_node)
                depth_reached = max(depth_reached, file_node.depth)
            edges.append(CallEdge(
                from_id=current.id,
                to_id=file_node_id,
                kind="import",
                confidence=confidence_for_import(
                    resolver_used="static",
                ),
            ))

    return CallGraphResult(
        nodes=nodes,
        edges=edges,
        depth_reached=depth_reached,
        truncated=truncated,
        dropped_node_count=dropped,
    )


__all__ = [
    "CallNode",
    "CallEdge",
    "CallGraphResult",
    "build_call_graph",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES_PER_FLOW",
]
