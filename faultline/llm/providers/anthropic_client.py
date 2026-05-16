"""Anthropic Claude provider adapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from faultline.signals import LlmResponse

if TYPE_CHECKING:
    from faultline.llm.cost import CostTracker

logger = logging.getLogger(__name__)


class AnthropicLlmClient:
    """``LlmClient`` over the official ``anthropic`` Python SDK."""

    name = "anthropic"

    def __init__(
        self,
        *,
        client,
        model: str,
        tracker: "CostTracker | None" = None,
        cost_label: str = "anthropic",
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
        # Sprint 9c determinism — temperature=0 makes the same prompt
        # produce the same output across runs (modulo provider-side
        # infra variance). Eliminates ~70% of cross-scan stochasticity.
        # Sprint 9d streaming — Anthropic requires stream=True for
        # operations whose output may take >10 min (large max_tokens
        # like 32K can trigger this server-side). We accumulate the
        # stream and return the same shape as a non-streamed response.
        if max_tokens >= 8_192:
            return self._stream_complete(system, user, max_tokens)
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=0,
        )
        text = ""
        for block in getattr(response, "content", []) or []:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                text += block_text

        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        if self._tracker is not None and (in_tok or out_tok):
            self._tracker.record(
                provider="anthropic",
                model=self._model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                label=self._cost_label,
            )

        return LlmResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason=str(getattr(response, "stop_reason", "end_turn")),
        )

    def _stream_complete(self, system: str, user: str, max_tokens: int):
        """Streaming completion — required by Anthropic when
        max_tokens is large enough that the response could exceed
        the 10-minute non-streaming cap.
        """
        text_chunks: list[str] = []
        in_tok = 0
        out_tok = 0
        stop_reason = "end_turn"
        with self._client.messages.stream(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=0,
        ) as stream:
            for chunk in stream.text_stream:
                text_chunks.append(chunk)
            final = stream.get_final_message()
        text = "".join(text_chunks)
        usage = getattr(final, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        stop_reason = str(getattr(final, "stop_reason", "end_turn"))
        if self._tracker is not None and (in_tok or out_tok):
            self._tracker.record(
                provider="anthropic", model=self._model,
                input_tokens=in_tok, output_tokens=out_tok,
                label=self._cost_label,
            )
        return LlmResponse(
            text=text, input_tokens=in_tok, output_tokens=out_tok,
            stop_reason=stop_reason,
        )
