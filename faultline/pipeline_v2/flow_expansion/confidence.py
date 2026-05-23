"""Confidence scoring for Stage 3.5 flow-expansion edges + nodes.

Per [[rule-no-magic-tuning]]: confidence labels are categorical, not
threshold-based magic numbers. Three buckets:

  - ``high``    — deterministic resolution succeeded (symbol exported
                  from the imported file, route literal exact-matched
                  routes_index entry, etc.).
  - ``medium``  — partial resolution (file-level only, glob-style
                  route literal, ${var} interpolation captured the
                  literal portion only).
  - ``low``     — defensive fallback (parse failure, unknown stack,
                  identifier match without import grounding).
"""

from __future__ import annotations

from typing import Literal

Confidence = Literal["high", "medium", "low"]


def confidence_for_import(resolver_used: str) -> Confidence:
    """File-level import edge confidence.

    ``resolver_used`` ∈ ``{"static", "alias", "monorepo", "regex"}``.
    Today's resolvers (static / alias / monorepo) all return ``high``;
    pure-regex resolvers (Python/Go/Rust when AST is unavailable) drop
    to ``medium``.
    """
    if resolver_used in {"static", "alias", "monorepo"}:
        return "high"
    if resolver_used == "regex":
        return "medium"
    return "low"


def confidence_for_call(
    *,
    resolved_symbol: bool,
    same_file: bool,
) -> Confidence:
    """Intra-symbol call-edge confidence.

    Requires BOTH (a) an identifier-as-call-site match in the caller's
    body AND (b) an exported-symbol resolution in an imported file.
    When both hold we emit ``high``. Same-file resolution (rare today
    because we restrict to imported files) lands at ``medium``.
    """
    if resolved_symbol and not same_file:
        return "high"
    if resolved_symbol and same_file:
        return "medium"
    return "low"


def confidence_for_cross_stack(
    *,
    literal_match: bool,
    template_interpolation: bool,
) -> Confidence:
    """T2 cross-stack HTTP edge confidence.

    A ``fetch("/api/products")`` literal that exact-matches a
    routes_index entry → ``high``. An interpolated literal like
    ``fetch(`${BASE}/api/products`)`` where we only matched the
    literal tail → ``medium``. Anything else (config-driven URL,
    runtime concat) → ``low``.
    """
    if literal_match and not template_interpolation:
        return "high"
    if literal_match and template_interpolation:
        return "medium"
    return "low"


__all__ = [
    "Confidence",
    "confidence_for_import",
    "confidence_for_call",
    "confidence_for_cross_stack",
]
