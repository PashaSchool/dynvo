"""AggregateResult — the merged output of all aggregators for one scan.

Per docs/ROADMAP-90-RECALL.md (faultlines-app repo). One AggregateResult
represents the final detected feature map for one repo run, as built up
piece-by-piece by individual aggregators (FeatureAggregator, FlowAggregator,
DedupAggregator, CompactionAggregator). Aggregators don't mutate it
in place — they produce a new AggregateResult and the orchestrator
``merge``s them.

This module is intentionally thin: AggregateResult is a value object,
not a service. Conversion to the existing ``models.types.FeatureMap``
shape happens in a Writer (``writers/json_writer.py`` — Phase 2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Self


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregatedFeature:
    """Aggregator-side feature record.

    Subset of ``models.types.Feature`` carrying only fields the
    composable aggregators set. The Writer fills in remaining
    legacy fields when serialising to ``feature-map.json`` so older
    consumers continue to work during the Phase 2 migration.
    """

    name: str
    description: str | None = None
    paths: tuple[str, ...] = ()
    flows: tuple["AggregatedFlow", ...] = ()
    aliases: tuple[str, ...] = ()
    discovery_method: str = "primary"  # "primary" | "critique" | "anchor" | ...
    confidence: float = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregatedFlow:
    """Aggregator-side flow record."""

    name: str
    description: str | None = None
    paths: tuple[str, ...] = ()
    discovery_method: str = "primary"


@dataclass(frozen=True, slots=True, kw_only=True)
class AggregateResult:
    """The merged output of one scan's aggregators.

    Built up via successive ``merge`` calls in the orchestrator. The
    final value goes to the Writer.

    ``warnings`` carries non-fatal observations (e.g. expected-feature
    anchor not found in detected list) that the dashboard can surface.
    """

    repo_path: str
    analyzed_at: datetime
    features: tuple[AggregatedFeature, ...] = ()
    detection_confidence: str = "high"  # "high" | "medium" | "low"
    detection_method: str = "extractors"  # "extractors" | "extractors+llm" | "llm-only"
    stack_recognised: bool = True
    warnings: tuple[str, ...] = ()
    extra: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def empty(cls, repo_root: Path) -> Self:
        return cls(
            repo_path=str(repo_root),
            analyzed_at=datetime.utcnow(),
        )

    def merge(self, other: "AggregateResult") -> "AggregateResult":
        """Combine two AggregateResults. ``other`` wins for overlapping
        feature names (later aggregator overrides earlier — matches the
        existing dedup → compaction order). Warnings are unioned.
        """
        if not other.features:
            return replace(
                self,
                detection_confidence=other.detection_confidence or self.detection_confidence,
                detection_method=other.detection_method or self.detection_method,
                stack_recognised=other.stack_recognised and self.stack_recognised,
                warnings=tuple(set(self.warnings) | set(other.warnings)),
            )
        by_name: dict[str, AggregatedFeature] = {f.name: f for f in self.features}
        for feat in other.features:
            by_name[feat.name] = feat
        return replace(
            self,
            features=tuple(by_name.values()),
            detection_confidence=other.detection_confidence or self.detection_confidence,
            detection_method=other.detection_method or self.detection_method,
            stack_recognised=other.stack_recognised and self.stack_recognised,
            warnings=tuple(set(self.warnings) | set(other.warnings)),
        )


__all__ = [
    "AggregatedFeature",
    "AggregatedFlow",
    "AggregateResult",
]
