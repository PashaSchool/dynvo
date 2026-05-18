"""Stage 1 extractor Protocol + shared dataclass.

The single source of truth for what an extractor produces and how the
orchestrator interacts with it. Keep this file dependency-light —
extractors import from here and the orchestrator imports from here, so
any heavy import added here becomes a transitive cost on every scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from faultline.pipeline_v2.stage_0_intake import ScanContext


@dataclass(frozen=True)
class AnchorCandidate:
    """One deterministic-anchor signal emitted by a Stage 1 extractor.

    Attributes:
        name: kebab-case slug, e.g. ``"auth"``, ``"server-slack"``,
            ``"checkout-api"``. Used as the merge key in Stage 2.
        paths: files (repo-relative POSIX strings) claimed by this
            anchor. Same file may appear in candidates from multiple
            extractors — Stage 2 reconciles ownership using a stable
            source-priority rule.
        source: which extractor produced this candidate. One of
            ``"route"``, ``"mvc"``, ``"schema"``, ``"package"``,
            ``"config"``. Custom extractors registered via entry-points
            should use their own slug; Stage 2 treats unknown sources
            with the lowest priority by default.
        confidence_self: 0..1 — how confident *this* extractor is in
            this candidate on its own. Stage 2 may use it as a tie-break.
        display_name: optional Title Case label for UIs. When ``None``
            Stage 5 derives it from ``name``.
        rationale: short human-readable string explaining why this
            anchor exists. Surfaced in stage artifacts / debug output;
            never shown to end-users without sanitisation.
    """

    name: str
    paths: tuple[str, ...]
    source: str
    confidence_self: float
    display_name: str | None = None
    rationale: str = ""


@runtime_checkable
class AnchorExtractor(Protocol):
    """Stage 1 contract.

    The Protocol is intentionally narrow: an extractor takes a
    :class:`ScanContext` (read-only) and returns a list of
    :class:`AnchorCandidate`. No state is shared between extractors;
    they run in parallel and must be pure with respect to the context.

    ``name`` is the source slug emitted on every candidate. It must
    match the ``source`` field of every :class:`AnchorCandidate` the
    extractor produces. Use lowercase kebab-case.
    """

    name: str

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:
        """Return anchor candidates for ``ctx``.

        Returning an empty list is the correct response when the
        extractor's stack/convention does not apply to the repo —
        do NOT raise. Raising is reserved for genuine programming
        errors; the orchestrator catches them and records the failure
        as a warning, but the canonical "doesn't apply" answer is
        ``[]``.
        """
        ...


__all__ = ["AnchorCandidate", "AnchorExtractor"]
