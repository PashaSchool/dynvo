"""Sprint 2 — Flow Expansion (Stage 3.5).

Transforms each Flow's single ``entry_file`` into a rich
``{entry, nodes[], edges[], summary}`` structure via:

  * **T1** — deterministic intra-repo call graph (import + call edges)
    using existing :mod:`faultline.pipeline_v2.flow_reach` resolvers +
    AST signatures, layered with symbol-level identifier matching.
  * **T2** — cross-stack HTTP boundary detection: ``fetch("/api/x")``
    strings are matched against the Sprint 1 ``routes_index`` and
    emitted as ``cross_stack_http`` edges.

Cold-scan compliant per [[rule-cold-scan]]: pure in-memory pass over
Stage 3 output; no persistence; no LLM.

Backward compatible per [[bipartite-flow-feature-store]] Sprint 1:
every legacy field on :class:`~faultline.models.types.Flow`
(``paths``, ``participants``, ``entry_point_file``, ``coverage_pct``,
``flow_symbol_attributions``, ``uuid``) is preserved unchanged. The
new fields are additive — landing keeps reading ``paths`` while MCP
tools / agent context fetchers can read the richer graph.
"""

from faultline.pipeline_v2.flow_expansion.expander import (
    FlowExpansionResult,
    expand_flows,
)

__all__ = ["expand_flows", "FlowExpansionResult"]
