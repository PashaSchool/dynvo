"""Concrete construction of the Pipeline + collaborators.

Per docs/ROADMAP-90-RECALL.md (faultlines-app repo). The orchestrator
in ``faultline.pipeline`` depends on Protocols (``Extractor``,
``Aggregator``, ``Writer``, ``LlmClient``) — it has no knowledge of
any concrete class. ``factory.py`` is where the abstract gets bound to
concrete: it constructs the actual Sonnet client, JSON writer, and so
on, and assembles them into a Pipeline instance.

Phase 0a (this file): skeleton only. ``default_pipeline()`` exists but
is intentionally a no-op stub — it raises ``NotImplementedError`` to
make it obvious the wiring hasn't been done yet. Phase 2 fills it in
by progressively replacing the stub with real Extractor/Aggregator/
Writer instances as each extractor module migrates.

The contract: anywhere that needs "the default Faultlines pipeline"
should call ``default_pipeline()``. Tests and alternative entrypoints
construct their own Pipelines directly with the Protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from faultline.protocols import Aggregator, Extractor, LlmClient, Writer


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineConfig:
    """User-facing knobs for default_pipeline().

    Kept tiny on purpose — anything more elaborate belongs in a
    dedicated factory function for that use case.
    """

    use_llm: bool = True
    detect_flows: bool = True
    detect_symbols: bool = False
    coverage_path: str | None = None
    force_stack: str | None = None


def default_extractors(config: PipelineConfig) -> Sequence[Extractor]:
    """Return the default Extractor sequence for a Phase-2 pipeline.

    Phase 0a stub: returns an empty tuple. Phase 2 incrementally adds
    Extractor instances as each module migrates from the legacy code
    paths.
    """
    _ = config
    return ()


def default_aggregators(config: PipelineConfig) -> Sequence[Aggregator]:
    """Return the default Aggregator sequence (feature, flow, dedup,
    compaction in that order). Phase 0a stub."""
    _ = config
    return ()


def default_writer(config: PipelineConfig) -> Writer | None:
    """Return the default Writer (JSON to ~/.faultline). Phase 0a stub."""
    _ = config
    return None


def default_llm_client(config: PipelineConfig) -> LlmClient | None:
    """Return the default LlmClient (Sonnet via Anthropic, with Gemini
    fallback when env vars are set). Phase 0a stub.
    """
    _ = config
    return None


def default_pipeline(config: PipelineConfig | None = None):
    """Assemble and return the default Pipeline.

    Phase 0a: raises NotImplementedError until Phase 2 wiring lands.
    """
    raise NotImplementedError(
        "default_pipeline() is a Phase 2 deliverable. Until then the "
        "engine continues to use the legacy `faultline.cli` entry point. "
        "See docs/ROADMAP-90-RECALL.md in the faultlines-app repo for the "
        "migration plan."
    )


__all__ = [
    "PipelineConfig",
    "default_extractors",
    "default_aggregators",
    "default_writer",
    "default_llm_client",
    "default_pipeline",
]
