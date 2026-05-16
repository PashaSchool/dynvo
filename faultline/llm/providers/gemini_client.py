"""Google Gemini provider adapter.

Uses the ``google-genai`` SDK if installed (``from google import genai``).
Adapter is small enough that adding a different SDK later is a one-file
swap without touching aggregators — that's the whole point of the
``LlmClient`` Protocol.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from faultline.signals import LlmResponse

if TYPE_CHECKING:
    from faultline.llm.cost import CostTracker

logger = logging.getLogger(__name__)


class GeminiLlmClient:
    """``LlmClient`` over the ``google-genai`` Python SDK.

    The SDK call shape (March 2026):
        client = genai.Client(api_key=...)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": user}]}],
            config={"system_instruction": system, "max_output_tokens": N},
        )
        text = response.text
    """

    name = "gemini"

    def __init__(
        self,
        *,
        client,
        model: str,
        tracker: "CostTracker | None" = None,
        cost_label: str = "gemini",
    ) -> None:
        self._client = client
        self._model = model
        self._tracker = tracker
        self._cost_label = cost_label

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        tools=None,
    ) -> LlmResponse:
        _ = tools
        # Sprint 9c determinism — temperature=0 → same prompt produces
        # the same output across runs.
        response = self._client.models.generate_content(
            model=self._model,
            contents=[{"role": "user", "parts": [{"text": user}]}],
            config={
                "system_instruction": system,
                "max_output_tokens": max_tokens,
                "temperature": 0,
            },
        )
        text = getattr(response, "text", "") or ""

        usage = getattr(response, "usage_metadata", None)
        in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        if self._tracker is not None and (in_tok or out_tok):
            self._tracker.record(
                provider="gemini",
                model=self._model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                label=self._cost_label,
            )

        # Gemini doesn't expose an Anthropic-style stop_reason — emit
        # "end_turn" by default for shape parity.
        return LlmResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason="end_turn",
        )
