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

# Member-expression call: ``obj.method(`` / ``this.method(`` /
# ``Class.staticMethod(`` — group 1 is the METHOD name (the identifier
# AFTER the dot). Used to resolve a call to the SPECIFIC method body of an
# imported class, never the whole class. The receiver (``obj``) is
# intentionally ignored: we match the callee file by which imported file
# exports a class CONTAINING a method of this name. STRUCTURAL — no
# per-repo names.
_MEMBER_CALL_PATTERN = re.compile(
    r"\.\s*([A-Za-z_$][\w$]*)\s*\(",
)
# Constructor call: ``new Class(`` — group 1 is the class name. Resolves
# to the class's CONSTRUCTOR body only (or nothing if no explicit
# constructor), never the whole class.
_NEW_CALL_PATTERN = re.compile(
    r"\bnew\s+([A-Za-z_$][\w$]*)\s*\(",
)


def _extract_member_method_calls(
    source: str,
    start_line: int | None,
    end_line: int | None,
) -> set[str]:
    """Method names invoked as ``obj.method(...)`` in a source slice.

    These are the callee METHODS the flow actually exercises. Resolving
    them to the specific method body (not the enclosing class) is what
    keeps a flow's LOC = lines actually run, instead of pulling a whole
    1800-line repository class in for one ``repo.findById()`` call.
    """
    if not source:
        return set()
    if start_line is not None and end_line is not None and end_line >= start_line:
        lines = source.splitlines()
        slice_ = "\n".join(lines[start_line - 1: end_line])
    else:
        slice_ = source
    return {m.group(1) for m in _MEMBER_CALL_PATTERN.finditer(slice_)}


def _extract_constructor_calls(
    source: str,
    start_line: int | None,
    end_line: int | None,
) -> set[str]:
    """Class names invoked as ``new Class(...)`` in a source slice."""
    if not source:
        return set()
    if start_line is not None and end_line is not None and end_line >= start_line:
        lines = source.splitlines()
        slice_ = "\n".join(lines[start_line - 1: end_line])
    else:
        slice_ = source
    return {m.group(1) for m in _NEW_CALL_PATTERN.finditer(slice_)}

# A call-expression with its parenthesised argument list, e.g.
# ``defaultResponderForAppDir(postHandler)`` or
# ``apiHandler({ GET: getHandler, POST: postHandler })``. Group 1 is the
# raw argument text between the outermost parens. Used to recover
# function-REFERENCE arguments (handlers passed by name) that the plain
# call-pattern above misses because they are not themselves invoked.
#
# Non-nesting-aware on purpose (regex): the argument slice may under-
# capture deeply nested parens, but the downstream resolver only emits an
# edge when an identifier in the slice resolves to a real LOCAL function
# symbol, so spurious captures are precision-filtered. This is the
# structural fix for higher-order wrapper entries common in JS/TS:
#   export const POST = defaultResponderForAppDir(postHandler)
#   export default withErrorHandler(handler)
#   export const GET = apiHandler({ GET: getHandler })
# No hardcoded wrapper names — any ``f(...refs...)`` whose ref is a local
# function is unwrapped.
_CALL_ARGS_PATTERN = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(([^;]*?)\)",
    re.DOTALL,
)
_BARE_IDENT_PATTERN = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\b")


def _extract_reference_identifiers(
    source: str,
    start_line: int | None,
    end_line: int | None,
) -> set[str]:
    """Collect identifiers passed as REFERENCE ARGUMENTS to calls.

    Recovers higher-order-wrapper handler references: in
    ``const POST = wrap(postHandler)`` the plain call-pattern finds only
    ``wrap`` (``postHandler`` is never followed by ``(``). Here we scan
    the argument lists of every call-expression in the slice and return
    the bare identifiers found inside them (positional refs AND object-
    property values like ``{ GET: getHandler }``).

    The caller intersects these against the file's LOCAL function symbols
    before emitting any edge, so non-handler tokens (literals, type
    names, the wrapper itself) never produce phantom callees. STRUCTURAL
    and stack-neutral — no wrapper-name allow-list.
    """
    if not source:
        return set()
    if start_line is not None and end_line is not None and end_line >= start_line:
        lines = source.splitlines()
        slice_ = "\n".join(lines[start_line - 1: end_line])
    else:
        slice_ = source
    refs: set[str] = set()
    for m in _CALL_ARGS_PATTERN.finditer(slice_):
        args = m.group(1)
        for im in _BARE_IDENT_PATTERN.finditer(args):
            refs.add(im.group(1))
    return refs


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
    # Telemetry for method-level member-call resolution. A member call
    # ``obj.method()`` to an imported file whose method we could NOT pin to
    # a specific symbol range is counted as a MISS — we attribute nothing
    # for it (a tight under-count beats a whole-class over-count) and record
    # it here so the miss rate is measurable.
    member_calls_resolved: int = 0
    member_calls_unresolved: int = 0


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


def _symbol_range_obj(sig: FileSignature | None, symbol: str):
    """Return the FIRST top-level ``SymbolRange`` named ``symbol``.

    Top-level means kind != method/constructor — the exported function /
    class / const, not an inner method (those are resolved member-aware).
    Returns ``None`` when absent.
    """
    if sig is None:
        return None
    for rng in sig.symbol_ranges:
        if rng.name == symbol and rng.kind not in ("method", "constructor"):
            return rng
    return None


def _find_method_symbols(
    sig: FileSignature | None,
    method_name: str,
) -> list[tuple[str, int, int, str | None]]:
    """All METHOD / constructor symbols named ``method_name`` in a file.

    Returns ``(name, start_line, end_line, parent_class)`` tuples. A file
    may legitimately define a method of the same name on >1 class; we
    cannot statically know the receiver's class without type inference, so
    every same-named method is a candidate. The caller attributes each
    candidate's OWN tight body range — the union of (typically 1) candidate
    bodies is still vastly smaller than the whole class, and keeps the flow
    honest (the real method IS among them). Constructors are matched by the
    synthetic name ``constructor``.
    """
    if sig is None:
        return []
    out: list[tuple[str, int, int, str | None]] = []
    for rng in sig.symbol_ranges:
        if rng.kind not in ("method", "constructor"):
            continue
        if rng.name == method_name:
            out.append((
                rng.name, rng.start_line, rng.end_line,
                getattr(rng, "parent", None),
            ))
    return out


def _find_constructor_symbol(
    sig: FileSignature | None,
    class_name: str,
) -> tuple[int, int] | None:
    """The ``(start, end)`` of ``class_name``'s constructor body, if any.

    Returns ``None`` when the class has no EXPLICIT constructor — then a
    ``new Class()`` call attributes nothing (an implicit constructor has no
    user-authored lines to count), which is the correct under-count.
    """
    if sig is None:
        return None
    for rng in sig.symbol_ranges:
        if (
            rng.kind == "constructor"
            and getattr(rng, "parent", None) == class_name
        ):
            return (rng.start_line, rng.end_line)
    return None


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
    member_resolved = 0
    member_unresolved = 0

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
                workspace_package_map=rctx.workspace_package_map,
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
            # Member-expression method calls (``obj.method()``) and
            # constructor calls (``new Class()``) recovered WITH their
            # receiver/keyword context, so we can resolve them to the
            # SPECIFIC method / constructor body of an imported class
            # rather than the whole class. ``call_idents`` already holds
            # the bare method name too (it drops the ``.``), which is why
            # the OLD resolver wrongly matched the class export; the
            # member/constructor sets below let us route those names to
            # method-level ranges and EXCLUDE the whole-class fallback.
            member_method_calls = _extract_member_method_calls(
                src, line_start, line_end,
            )
            constructor_calls = _extract_constructor_calls(
                src, line_start, line_end,
            )

            # Higher-order WRAPPER unwrap: when the entry/current symbol's
            # body is ``X = wrap(handler)`` the real logic lives in the
            # local ``handler`` reference, which is NOT a call-expression
            # and so is absent from ``call_idents``. Recover identifiers
            # passed as reference arguments and treat any that resolve to
            # a LOCAL function symbol as a same-file callee (the true
            # handler body). Intersection with own_ranges below keeps this
            # precise; no wrapper-name allow-list (structural, stack-
            # neutral). See _extract_reference_identifiers docstring.
            ref_idents = _extract_reference_identifiers(
                src, line_start, line_end,
            )
            same_file_targets = call_idents | ref_idents

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
                # Same-file callees match EITHER a real call (``foo()``)
                # OR a function REFERENCE argument (``wrap(foo)``). The
                # reference path only fires for symbols that are callable
                # (local function / arrow / exported function), so a const
                # data object passed as an arg never becomes a phantom
                # callee.
                is_ref_only = sym not in call_idents
                if is_ref_only:
                    if sym not in same_file_targets:
                        continue
                    if rng.kind not in ("local", "function"):
                        continue
                key = (current.file, sym)
                if key in callees_by_symbol:
                    continue
                # Wrapper-unwrap depth handling: when the entry symbol's
                # body is a pure higher-order wrapper (``X = wrap(handler)``)
                # the referenced LOCAL handler is the TRUE entry body. It
                # must inherit the entry's cross-file budget so its own
                # imported callees (repositories / services) still fan out
                # at depth-1 — otherwise unwrapping merely shifts the real
                # logic one level deeper than the cross-file cap and the
                # flow stays single-file. A reference-only callee resolved
                # AT the entry node (depth 0) therefore keeps depth 0; all
                # other callees recurse at depth+1 as before.
                callee_depth = (
                    current.depth
                    if (is_ref_only and current.depth == 0)
                    else current.depth + 1
                )
                callees_by_symbol[key] = CallNode(
                    id=_node_id(current.file, sym),
                    file=current.file,
                    symbol=sym,
                    lines=(rng.start_line, rng.end_line),
                    depth=callee_depth,
                )
                same_file_keys.add(key)

            # (b.1) Cross-file callees — identifier → (callee_file,
            # callee_sig) restricted to files this file imports.
            #
            # METHOD-LEVEL resolution (the core of this fix):
            #   * A direct call to a FUNCTION / const export (``helper()``)
            #     resolves to that symbol's full range — a function's body
            #     IS its full range, so this is correct and UNCHANGED.
            #   * A call to a CLASS export name is NOT pulled in whole. The
            #     name only appears because of ``new Class()`` (constructor)
            #     or a static call; we attribute the CONSTRUCTOR body only
            #     (or nothing if implicit). The whole-class body is never
            #     attributed for a mere construction.
            #   * A member call ``obj.method()`` resolves to the specific
            #     METHOD symbol(s) of that name in the imported file — the
            #     method's OWN tight line range, never the enclosing class.
            #   * Unresolvable member calls attribute NOTHING (tight under-
            #     count) and bump the miss counter.
            for cf in imported_files:
                csig = rctx.signatures.get(cf)
                if csig is None:
                    continue
                exports = _file_exports(csig)

                # Direct function/const-export calls: ``name()`` where the
                # export is NOT a class. Classes are handled via the
                # constructor / member paths below so we never pull a whole
                # class in just because its name was referenced.
                for sym in exports:
                    if sym not in call_idents:
                        continue
                    rng_obj = _symbol_range_obj(csig, sym)
                    if rng_obj is not None and rng_obj.kind == "class":
                        continue  # never whole-class; see constructor path
                    rng_lines = (
                        (rng_obj.start_line, rng_obj.end_line)
                        if rng_obj is not None
                        else None
                    )
                    callees_by_symbol[(cf, sym)] = CallNode(
                        id=_node_id(cf, sym),
                        file=cf,
                        symbol=sym,
                        lines=rng_lines,
                        depth=current.depth + 1,
                    )

                # Constructor calls: ``new Class()`` → constructor body
                # only. The class must actually be exported by this file
                # (avoids cross-file name collisions). Nothing attributed
                # when the class has no explicit constructor.
                for cls in constructor_calls:
                    if cls not in exports:
                        continue
                    ctor = _find_constructor_symbol(csig, cls)
                    if ctor is None:
                        continue
                    ctor_key = (cf, f"{cls}.constructor")
                    callees_by_symbol[ctor_key] = CallNode(
                        id=_node_id(cf, f"{cls}.constructor"),
                        file=cf,
                        symbol=f"{cls}.constructor",
                        lines=ctor,
                        depth=current.depth + 1,
                    )

                # Member method calls: ``obj.method()`` → the specific
                # method body(ies) of that name in this imported file. We
                # only resolve a member call to a file that the symbol could
                # plausibly come from (it exports a class AND defines a
                # method of this name) — pure structural matching.
                file_has_class = any(
                    r.kind == "class" for r in csig.symbol_ranges
                )
                for meth in member_method_calls:
                    if not file_has_class:
                        continue
                    cands = _find_method_symbols(csig, meth)
                    if not cands:
                        continue
                    for (mname, ms, me, parent) in cands:
                        label = f"{parent}.{mname}" if parent else mname
                        callees_by_symbol[(cf, label)] = CallNode(
                            id=_node_id(cf, label),
                            file=cf,
                            symbol=label,
                            lines=(ms, me),
                            depth=current.depth + 1,
                        )

            # Member-call resolution telemetry: a member call is RESOLVED
            # when at least one imported file defines a class with a method
            # of that name; otherwise it is a MISS (attributed nothing).
            for meth in member_method_calls:
                resolved = any(
                    _find_method_symbols(rctx.signatures.get(cf), meth)
                    and any(
                        r.kind == "class"
                        for r in (rctx.signatures.get(cf).symbol_ranges)  # type: ignore[union-attr]
                    )
                    for cf in imported_files
                    if rctx.signatures.get(cf) is not None
                )
                if resolved:
                    member_resolved += 1
                else:
                    member_unresolved += 1

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
        member_calls_resolved=member_resolved,
        member_calls_unresolved=member_unresolved,
    )


__all__ = [
    "CallNode",
    "CallEdge",
    "CallGraphResult",
    "build_call_graph",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_NODES_PER_FLOW",
]
