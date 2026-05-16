"""Pluggable LLM client contract (Sprint 8h).

Every aggregator that calls an LLM should depend on this Protocol
rather than a concrete provider SDK. Use
``faultline.llm.factory.make_client(role)`` to obtain an instance —
the factory reads role→provider+model config from env vars, so swapping
Haiku → Gemini Flash for a given role is one config change, not a code
edit across N aggregators.

The contract is intentionally small: ``complete(*, system, user,
max_tokens)`` returning ``LlmResponse``. That covers every aggregator
we have today (display canonicalizer, critique, recall-critique,
flow-attribution-critique). Extending the contract should be rare and
require simultaneous updates to every provider adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from faultline.signals import LlmResponse


@runtime_checkable
class LlmClient(Protocol):
    """Provider-agnostic completion client.

    Implementations live under ``faultline.llm.providers/*`` and are
    selected at runtime by ``faultline.llm.factory``. Concrete adapters
    handle SDK-specific quirks (Anthropic's blocks-list response,
    Gemini's candidates-list response) and emit a normalised
    ``LlmResponse``.

    Implementations SHOULD record token usage to a ``CostTracker``
    when one is supplied at construction time so cross-aggregator cost
    is visible alongside the primary scan totals.
    """

    name: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        tools=None,
    ) -> LlmResponse:
        """Run a single completion.

        ``tools`` is reserved for future tool-use roles; current
        aggregators pass None.
        """
        ...
