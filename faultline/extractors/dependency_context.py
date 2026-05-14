"""Dependency-context window builder for LLM prompts.

Per the ``dependency-context-injector`` skill (faultlines-app repo,
``.claude/skills/dependency-context-injector/SKILL.md``) and prior-art
research finding that ArchAgent's biggest single F1 lift came from
this technique (arXiv 2601.13007).

Given a chunk of files that's about to be sent to Sonnet for feature
detection, this module produces a context block that lists:

  - Files in the chunk
  - Files this chunk IMPORTS (with their top-level exported symbols)
  - Files that IMPORT this chunk (callers) with the symbols they use

1-hop only — ArchAgent's ablation showed 2-hop adds noise without
proportional F1 gain. Token budget is capped (default 1.5k chars,
which is roughly 400-500 tokens) with relevance-based truncation:
neighbours that touch multiple files in the chunk win.

Phase 3a: this module is the FIRST Phase-2-style extractor. It does
NOT implement the full ``Extractor`` Protocol because the LLM scanner
that consumes its output isn't yet refactored into the new shape;
instead it exposes a pure ``build_dependency_context_block`` function
the legacy ``sonnet_scanner`` calls when the
``FAULTLINE_DEP_CONTEXT=1`` env var is set. Once Phase 2 lands and the
scanner becomes ``LlmFeatureExtractor``, this class will be
constructor-injected into it directly.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from faultline.analyzer.symbol_graph import ImportEdge, SymbolGraph

# Heuristic char-to-token ratio for an English+code mix. We slightly
# under-estimate so the budget is safe.
_CHARS_PER_TOKEN = 4

# Default budget per chunk: 1500 chars ≈ 375 tokens. Matches the
# upper bound suggested in the skill doc.
DEFAULT_BUDGET_CHARS = 1500

# Synthetic edge symbols emitted by the symbol graph for URL-routed
# (HTTP) reachability. Excluded from the context block because they
# don't help feature clustering at the import-level.
_SYNTHETIC_SYMBOLS = frozenset({"@http"})


@dataclass(frozen=True, slots=True, kw_only=True)
class _Neighbour:
    """One neighbour file with the symbols it interacts with via this chunk."""

    path: str
    direction: str          # "imported" (this chunk imports it) | "caller" (it imports this chunk)
    symbols: tuple[str, ...]
    touches_in_chunk: int   # how many files in the chunk this neighbour relates to
    exports: tuple[str, ...] = ()


def is_enabled() -> bool:
    """True iff the FAULTLINE_DEP_CONTEXT env var enables injection.

    Off by default during Phase 3a rollout so A/B testing is clean
    (run with and without to measure recall delta).
    """
    return os.environ.get("FAULTLINE_DEP_CONTEXT", "").lower() in {"1", "true", "yes", "on"}


def build_dependency_context_block(
    *,
    chunk_files: list[str],
    graph: SymbolGraph,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> str:
    """Return a prompt-ready context block describing the chunk's
    1-hop dependency neighbourhood.

    Empty string when:
      - chunk has <2 files (the LLM has the full context already)
      - graph has no edges touching the chunk
      - budget_chars is too small for any meaningful content
    """
    if len(chunk_files) < 2:
        return ""

    chunk_set = set(chunk_files)
    neighbours = _collect_neighbours(chunk_set, graph)
    if not neighbours:
        return ""

    ranked = _rank_neighbours(neighbours)
    return _format_block(chunk_files, ranked, graph, budget_chars=budget_chars)


def _collect_neighbours(
    chunk_set: set[str], graph: SymbolGraph,
) -> list[_Neighbour]:
    """Walk the graph and collect 1-hop neighbours of any chunk file.

    Aggregates by (neighbour_path, direction) so a neighbour file
    appears at most twice (once as imported, once as caller).
    """
    # (path, direction) → (symbols set, distinct chunk files touched)
    accum: dict[tuple[str, str], tuple[set[str], set[str]]] = {}

    for src in chunk_set:
        # Out-edges: src imports something
        for edge in graph.imports_from(src):
            if edge.target_file in chunk_set:
                continue   # internal; no new context
            if edge.target_symbol in _SYNTHETIC_SYMBOLS:
                continue
            key = (edge.target_file, "imported")
            slot = accum.setdefault(key, (set(), set()))
            slot[0].add(edge.target_symbol)
            slot[1].add(src)

        # In-edges: src is imported by something
        for edge in graph.callers_of(src):
            # NB: graph.callers_of(src) returns ImportEdge with
            # ``target_file`` = src (the file BEING imported). The
            # actual caller is the dict key whose forward list contains
            # this edge; symbol_graph keeps that backward by file in
            # ``reverse`` so we have to walk it. Simpler path: look up
            # the caller via forward graph reconstruction below.
            pass

    # Build a (caller_file → list of edges) view from the forward map
    # to identify callers cleanly. graph.callers_of() doesn't return
    # the caller file directly — we walk forward edges instead.
    for caller_file, edges in graph.forward.items():
        if caller_file in chunk_set:
            continue
        for edge in edges:
            if edge.target_file not in chunk_set:
                continue
            if edge.target_symbol in _SYNTHETIC_SYMBOLS:
                continue
            key = (caller_file, "caller")
            slot = accum.setdefault(key, (set(), set()))
            slot[0].add(edge.target_symbol)
            slot[1].add(edge.target_file)

    out: list[_Neighbour] = []
    for (path, direction), (symbols, touched) in accum.items():
        exports = _top_exports_for(path, graph, limit=6)
        out.append(_Neighbour(
            path=path,
            direction=direction,
            symbols=tuple(sorted(symbols)),
            touches_in_chunk=len(touched),
            exports=exports,
        ))
    return out


def _top_exports_for(
    file: str, graph: SymbolGraph, *, limit: int,
) -> tuple[str, ...]:
    """Return up to `limit` top-level exported symbol names from a file."""
    ranges = graph.exports.get(file, []) or []
    # Stable order: by start_line ascending, take the first `limit`.
    names = [r.name for r in sorted(ranges, key=lambda r: r.start_line)][:limit]
    return tuple(names)


def _rank_neighbours(neighbours: list[_Neighbour]) -> list[_Neighbour]:
    """Sort neighbours by (touches_in_chunk desc, then directional priority,
    then path for stability). Neighbours that touch multiple chunk files are
    the most informative for clustering — they win the budget first.
    """
    direction_rank = {"imported": 0, "caller": 1}
    return sorted(
        neighbours,
        key=lambda n: (
            -n.touches_in_chunk,
            direction_rank.get(n.direction, 9),
            n.path,
        ),
    )


def _format_block(
    chunk_files: list[str],
    ranked: list[_Neighbour],
    graph: SymbolGraph,
    *,
    budget_chars: int,
) -> str:
    """Render the context block, truncating when budget runs out."""
    head_lines: list[str] = [
        "=== DEPENDENCY CONTEXT FOR THIS CHUNK ===",
        "",
        "Files in this chunk:",
    ]
    for f in chunk_files:
        head_lines.append(f"  {f}")

    imports_block, callers_block = [], []
    for n in ranked:
        exports_label = ""
        if n.exports:
            exports_label = f"  → exports: {', '.join(n.exports)}"
        # Show up to 5 symbols actually used; if more, append +N.
        used = list(n.symbols)
        used_label = ""
        if used:
            head = used[:5]
            tail = "" if len(used) <= 5 else f", +{len(used) - 5}"
            used_label = f"  (uses: {', '.join(head)}{tail})"
        line = f"  {n.path}{exports_label}{used_label}"
        if n.direction == "imported":
            imports_block.append(line)
        else:
            callers_block.append(line)

    parts: list[str] = []
    parts.extend(head_lines)
    if imports_block:
        parts.append("")
        parts.append("This chunk IMPORTS:")
        parts.extend(imports_block)
    if callers_block:
        parts.append("")
        parts.append("This chunk is IMPORTED BY (callers outside the chunk):")
        parts.extend(callers_block)
    parts.append("")
    parts.append("(third-party imports omitted; 1-hop only)")
    parts.append("=== END DEPENDENCY CONTEXT ===")

    block = "\n".join(parts)
    if len(block) <= budget_chars:
        return block

    # Over budget — trim from the tail until we fit. Always keep the
    # "Files in this chunk:" section + at least one neighbour to avoid
    # an empty block.
    truncated_lines: list[str] = list(head_lines)
    fixed_size = sum(len(line) + 1 for line in head_lines) + 80  # padding for footer
    remaining = budget_chars - fixed_size

    def _push(section_label: str, lines: list[str]) -> None:
        nonlocal remaining
        if not lines:
            return
        truncated_lines.append("")
        truncated_lines.append(section_label)
        remaining -= len(section_label) + 2
        kept = 0
        for line in lines:
            if remaining < len(line) + 1 and kept >= 1:
                break
            truncated_lines.append(line)
            remaining -= len(line) + 1
            kept += 1
        if kept < len(lines):
            truncated_lines.append(f"  … ({len(lines) - kept} more omitted for budget)")
            remaining -= 60

    _push("This chunk IMPORTS:", imports_block)
    _push("This chunk is IMPORTED BY (callers outside the chunk):", callers_block)
    truncated_lines.append("")
    truncated_lines.append("(third-party imports omitted; 1-hop only)")
    truncated_lines.append("=== END DEPENDENCY CONTEXT ===")
    return "\n".join(truncated_lines)


# ── Protocol-conforming wrapper (forward-compat for Phase 2) ─────────


@dataclass(frozen=True, slots=True, kw_only=True)
class DependencyContextInjector:
    """Phase 2 wrapper that will conform to the ``Extractor`` Protocol
    once the LLM scanner consumes the new architecture. Today it's a
    thin holder so callers can pass an instance via dependency
    injection (matches the rest of the new-architecture style).

    Usage during Phase 3a:
        injector = DependencyContextInjector(graph=graph)
        block = injector.context_for(chunk_files)
    """

    graph: SymbolGraph
    budget_chars: int = DEFAULT_BUDGET_CHARS
    name: str = "dependency-context-injector"

    def context_for(self, chunk_files: Iterable[str]) -> str:
        return build_dependency_context_block(
            chunk_files=list(chunk_files),
            graph=self.graph,
            budget_chars=self.budget_chars,
        )


__all__ = [
    "DependencyContextInjector",
    "build_dependency_context_block",
    "is_enabled",
    "DEFAULT_BUDGET_CHARS",
]
