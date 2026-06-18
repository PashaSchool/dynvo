"""Wrapped-handler line-range resolution (P4) — universal, deterministic.

Stage 3 attaches a flow's ``entry_point_line`` from the START line of an
exported symbol. When that export is a thin higher-order wrapper —
``export const POST = withAuth(handler)`` or a server-action assigned
from a wrapper — the start line points at the 2-LOC wrapper export, NOT
the real handler body. The symptom: Formbricks emitted 449/449 flows
with 0 LOC because every wrapped export resolved to its wrapper line.

This module recovers the REAL handler line by reusing the existing,
proven, stack-neutral reference-argument scanner
(:func:`faultline.pipeline_v2.flow_expansion.call_graph._extract_reference_identifiers`):
it finds the bare identifiers passed as arguments to calls inside the
wrapper's body slice, intersects them against the file's LOCAL function
symbols, and resolves to that inner function's definition line.

Universal by construction:
  * No wrapper-name allow-list — any ``f(...localRef...)`` body qualifies.
  * No per-repo paths, no magic numbers.
  * Pure: reads the in-memory :class:`FileSignature` (source +
    symbol_ranges) already produced by Stage 3; no extra file IO.

It is NOT profile-gated — a wrapped export is a wrapped export on every
stack — but it lives under ``profiles/`` because the Framework Knowledge
Layer's :meth:`flow_entries` seeding relies on it to give correct line
ranges, and it is exercised through the P4 wiring. When the input has no
wrapper, the original line is returned unchanged (identity), so existing
non-wrapped flows are untouched (regression guard).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultline.pipeline_v2.flow_expansion.call_graph import (
    _extract_reference_identifiers,
)

if TYPE_CHECKING:
    from faultline.analyzer.ast_extractor import FileSignature


def resolve_handler_line(
    sig: "FileSignature",
    symbol: str,
    export_line: int,
) -> int:
    """Return the inner handler's start line, or ``export_line`` unchanged.

    Args:
        sig: the file signature carrying ``source`` + ``symbol_ranges``.
        symbol: the exported symbol whose definition may be a wrapper.
        export_line: the symbol's currently-resolved (possibly wrapper)
            start line.

    Returns:
        The start line of the inner local function the wrapper invokes,
        when the export's body is a single higher-order wrapper call
        whose reference argument matches a LOCAL function symbol in the
        same file. Otherwise ``export_line`` unchanged.
    """
    if not sig or not getattr(sig, "source", ""):
        return export_line

    ranges = {r.name: r for r in sig.symbol_ranges}
    export_range = ranges.get(symbol)
    if export_range is None:
        return export_line

    # Only attempt unwrapping for SHORT bodies — a real, multi-line
    # handler defined inline is not a wrapper and must keep its own line.
    # "Short" is structural (the export spans at most a couple of lines),
    # not a tuned threshold: a genuine wrapper export
    # (``export const POST = withAuth(handler)``) is a single statement.
    span = export_range.end_line - export_range.start_line
    if span > _MAX_WRAPPER_SPAN_LINES:
        return export_line

    refs = _extract_reference_identifiers(
        sig.source, export_range.start_line, export_range.end_line,
    )
    if not refs:
        return export_line

    # Resolve to a LOCAL function symbol — never to the wrapper itself
    # (``symbol``) and never to a non-local token. Among multiple local
    # matches pick the earliest-defined (deterministic + the handler is
    # usually declared above its wrapped export).
    local_matches = [
        ranges[r].start_line
        for r in refs
        if r != symbol and r in ranges
    ]
    if not local_matches:
        return export_line
    return min(local_matches)


# A wrapper export is a single statement; allow one line of slack for
# formatting (``export const POST =\n  withAuth(handler)``). Structural,
# stack-invariant — NOT a per-corpus tuned number.
_MAX_WRAPPER_SPAN_LINES = 2


__all__ = ["resolve_handler_line"]
