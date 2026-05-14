"""Signal — the unit of evidence emitted by an Extractor.

Per docs/ROADMAP-90-RECALL.md (faultlines-app repo). One Signal =
one fact extracted from a repo (e.g. "this file is a Next.js page
route", "this dependency implies the Billing feature exists",
"this Sonnet pass produced this raw feature cluster").

Aggregators read signal kinds + payloads and turn them into
features/flows. Extractors don't decide what features exist; they
decide what evidence the aggregator gets.

Signals are frozen + slotted dataclasses for hashability and cheap
construction. Payload is an immutable Mapping so signals can be
deduplicated by value across extractors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _freeze(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Wrap a dict in a read-only proxy. Caller is expected to pass
    already-immutable values (str / int / tuple / None / nested
    proxies); we don't deep-freeze.
    """
    return MappingProxyType(dict(payload))


@dataclass(frozen=True, slots=True, kw_only=True)
class Signal:
    """One unit of evidence from an extractor.

    ``kind`` is a short stable string (e.g. ``"route-page"``,
    ``"controller-action"``, ``"expected-feature"``,
    ``"declared-surface"``, ``"llm-feature-cluster"``,
    ``"import-edge"``) — aggregators dispatch on it.

    ``source`` is the emitting extractor's name. Used for provenance
    and debugging; never load-bearing for aggregation logic.

    ``payload`` is the data — kind-specific. Convention: include a
    ``file`` key when the signal points at a specific file; include
    ``confidence`` (0.0–1.0) when the extractor isn't certain.
    """

    kind: str
    source: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Replace mutable payload with a read-only view to honour the
        # frozen contract for hash/equality.
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", _freeze(self.payload))


# ── LLM transport types (used by LlmClient Protocol) ──────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmResponse:
    """Normalised LLM completion result.

    Single shape across providers (Sonnet, Gemini, Haiku) so callers
    don't switch on response variants. Extractors needing the raw
    provider response should re-do the call with the provider's SDK
    directly (rare; almost never the right move).
    """

    text: str                          # the assistant's textual content
    tool_calls: tuple["LlmToolCall", ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"      # or "max_tokens", "tool_use", etc.


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmToolCall:
    """One tool invocation from a tool-use turn."""

    name: str
    arguments: Mapping[str, object]    # the parsed JSON arguments

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, MappingProxyType):
            object.__setattr__(self, "arguments", _freeze(self.arguments))


__all__ = ["Signal", "LlmResponse", "LlmToolCall"]
