"""Core protocols for the composable extractor architecture.

Per docs/ROADMAP-90-RECALL.md (in faultlines-app repo) and the
[[oop-architect]] / [[python-expert]] skills. Defines the four small
runtime-checkable Protocols that the orchestrator depends on:

  - ``Extractor``: emits Signal objects from a repo
  - ``Aggregator``: combines signals into AggregateResult components
  - ``Writer``: serialises AggregateResult to a destination
  - ``LlmClient``: thin transport for LLM calls (so extractors that
    need the LLM accept a client, not a concrete library import)

Phase 0a (this file): introduce the Protocols. No existing module
imports them yet. Phase 2 migrates the engine to consume them.

Why Protocols (and not ABCs):
  - Structural typing — any class with the right shape qualifies; no
    base-class inheritance forced on extractors.
  - ``runtime_checkable`` enables ``isinstance(obj, Extractor)`` checks
    at the orchestrator boundary if needed (rare; type checking does
    most of the work).
  - Easier test fakes: a small dataclass + two methods is enough.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from faultline.results import AggregateResult
from faultline.signals import LlmResponse, Signal


@runtime_checkable
class Extractor(Protocol):
    """Emits Signal objects describing what it found in a repo.

    Implementations live under ``faultline/extractors/<name>.py`` and
    do not import each other — they communicate via Signal through the
    orchestrator. Each extractor is responsible for exactly one signal
    type (one source of change → one module).
    """

    name: str

    def applicable(self, repo_root: Path) -> bool:
        """Cheap pre-check: does this extractor have anything to say
        about this repo? Called once before ``extract``. Should not
        raise; returning ``False`` short-circuits cleanly.
        """
        ...

    def extract(self, repo_root: Path, files: Iterable[Path]) -> list[Signal]:
        """Run extraction. Returns a list of Signal objects, never
        raises on empty input — returns an empty list instead. Errors
        that prevent extraction should raise a narrow custom exception
        (subclass of ``ExtractorError``) which the orchestrator logs
        and skips.
        """
        ...


@runtime_checkable
class Aggregator(Protocol):
    """Combines a stream of Signal objects into one component of an
    ``AggregateResult`` (e.g. the feature list, the flow list).

    Aggregators do NOT know which extractor produced a signal — they
    operate on signal kind + payload. This is what makes the architecture
    open for extension (new extractor → no change to aggregators).
    """

    name: str

    def aggregate(self, signals: Iterable[Signal]) -> AggregateResult:
        """Reduce signals to an AggregateResult. Should be deterministic
        for a given input — same signals in any order produce the same
        output (signal ordering is NOT load-bearing).
        """
        ...


@runtime_checkable
class Writer(Protocol):
    """Serialises an AggregateResult to a destination (JSON file,
    cloud sync, terminal report, etc.). A Pipeline run uses one Writer;
    multi-destination writes compose multiple Writers via a wrapper.
    """

    name: str

    def write(self, result: AggregateResult, dest: Path) -> None:
        """Write the result. Idempotent on repeated calls with the
        same arguments (overwrites cleanly). Should raise on IO
        failure rather than silently dropping the write.
        """
        ...


@runtime_checkable
class LlmClient(Protocol):
    """Thin transport for LLM calls.

    Extractors and aggregators that need the LLM accept an LlmClient in
    their constructor — they never import ``anthropic`` or ``google``
    SDKs directly. This makes them testable with a fake client and
    lets us swap providers (Sonnet ↔ Gemini) without touching the
    extractor logic.
    """

    name: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        tools: list[dict] | None = None,
    ) -> LlmResponse:
        """Run a single completion. ``system`` and ``user`` are the
        prompt segments; ``max_tokens`` caps output. ``tools`` is the
        Anthropic tool-use shape (or its Gemini equivalent — the client
        translates). Returns a normalised LlmResponse so callers don't
        depend on a specific provider's response shape.
        """
        ...


# ── Custom exceptions (narrow; per python-expert skill) ───────────────


class ExtractorError(Exception):
    """Base for any failure inside an extractor."""


class StackDetectionError(ExtractorError):
    """We couldn't identify which stack rules apply to this repo."""


class AggregatorError(Exception):
    """Base for aggregation failures."""


class WriterError(Exception):
    """Base for writer failures."""


class LlmTransportError(Exception):
    """Base for LLM transport failures (timeouts, rate limits, API
    errors). Retry/backoff happens INSIDE the client; this exception
    means the retries were exhausted.
    """


__all__ = [
    "Extractor",
    "Aggregator",
    "Writer",
    "LlmClient",
    "ExtractorError",
    "StackDetectionError",
    "AggregatorError",
    "WriterError",
    "LlmTransportError",
]
