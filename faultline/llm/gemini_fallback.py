"""Sprint 24 — Gemini fallback for Anthropic primary.

Lightweight wrapper that retries failed Anthropic calls on Gemini.
Triggered ONLY on hard failures (HTTP 429 / 500 / 503 / timeout /
connection error) — never on quality grounds. The fallback is a
production-hygiene measure: Anthropic outage doesn't kill the scan.

Mapping:
  claude-sonnet-4-6  ↔  gemini-2.5-pro      (deep_scan, flow_judge)
  claude-haiku-4-5   ↔  gemini-flash-latest (judge calls, dedup)

Public surface
==============

    is_retriable(exc) -> bool
        True for HTTP 429 / 5xx / timeout / connection error from
        anthropic SDK exceptions.

    map_to_gemini(model) -> str
        "claude-sonnet-4-6"  → "gemini-2.5-pro"
        "claude-haiku-4-5"   → "gemini-flash-latest"

    GeminiClient (kwargs-compatible facade for messages.create)
        wraps google.genai.Client with an Anthropic-shaped response

    with_anthropic_fallback(stage_name, primary_call) -> response
        helper decorator. Tries primary_call(); on retriable failure,
        retries via Gemini if GOOGLE_API_KEY is set.

Configuration
=============

  ANTHROPIC_API_KEY — primary (required)
  GOOGLE_API_KEY    — fallback (optional; when missing the fallback
                      is silent no-op and the original error
                      propagates)

Cost
====

Gemini Pro/Flash run only when Anthropic fails. Expected hit-rate
< 1% in production, so cost impact is negligible.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_PRO = "gemini-2.5-pro"
DEFAULT_GEMINI_FLASH = "gemini-flash-latest"


# ── Retriable detection ──────────────────────────────────────────────


_RETRIABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def is_retriable(exc: BaseException) -> bool:
    """True iff ``exc`` is a transient Anthropic SDK failure.

    Covers:
      - anthropic.APIStatusError with retriable HTTP codes
      - anthropic.APIConnectionError / APITimeoutError
      - anthropic.APIError subclasses surfaced by transient outages

    Filters out:
      - AuthenticationError (key issue, fallback won't help)
      - BadRequestError / InvalidRequestError (logic bug)
      - Pydantic ValidationError (response shape)
    """
    name = type(exc).__name__
    if "Authentication" in name or "BadRequest" in name or "InvalidRequest" in name:
        return False
    if name in {"APIConnectionError", "APITimeoutError", "ConnectError"}:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _RETRIABLE_STATUS_CODES:
        return True
    if "Overloaded" in str(exc) or "rate_limit" in str(exc).lower():
        return True
    return False


def map_to_gemini(anthropic_model: str | None) -> str:
    """Translate the Anthropic model id to its Gemini fallback."""
    if not anthropic_model:
        return DEFAULT_GEMINI_PRO
    m = anthropic_model.lower()
    if "haiku" in m:
        return DEFAULT_GEMINI_FLASH
    if "opus" in m or "sonnet" in m:
        return DEFAULT_GEMINI_PRO
    # Conservative default: Pro for unknown models
    return DEFAULT_GEMINI_PRO


# ── Anthropic-shaped response from Gemini ────────────────────────────


@dataclass
class _GeminiBlock:
    """Anthropic-shaped content block."""
    type: str = "text"
    text: str = ""


@dataclass
class _GeminiResponse:
    """Anthropic-shaped messages.create response from Gemini."""
    content: list[_GeminiBlock]
    model: str
    stop_reason: str = "end_turn"

    # Anthropic SDK exposes `.usage.input_tokens` etc. — provide a
    # minimal stand-in so cost-tracker code paths don't crash.
    @property
    def usage(self):  # noqa: D401 — Anthropic API parity
        return _GeminiUsage()


@dataclass
class _GeminiUsage:
    input_tokens: int = 0
    output_tokens: int = 0


# ── Client facade ────────────────────────────────────────────────────


class GeminiClient:
    """Anthropic-shaped client backed by google-genai.

    Implements the ``messages.create(...)`` surface used by Faultlines
    (system, messages, max_tokens, temperature, model). Returns a
    ``_GeminiResponse`` whose ``.content[0].text`` mirrors the Anthropic
    text block convention.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "GeminiClient requires GOOGLE_API_KEY env var or api_key=...",
            )
        try:
            from google import genai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "google-genai package missing. pip install google-genai",
            ) from exc
        self.messages = _MessagesAPI(self)


class _MessagesAPI:
    """Mirror of anthropic.Anthropic().messages.create signature."""

    def __init__(self, client: GeminiClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0,
        **_: Any,
    ) -> _GeminiResponse:
        from google import genai
        sdk = genai.Client(api_key=self._client.api_key)

        # Compose prompt — google-genai uses role-based contents.
        contents = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                # Anthropic-style multi-block; flatten text only.
                content = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            contents.append({
                "role": "user" if role == "user" else "model",
                "parts": [{"text": str(content)}],
            })

        config: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system:
            config["system_instruction"] = system

        try:
            resp = sdk.models.generate_content(
                model=model, contents=contents, config=config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GeminiClient.messages.create failed: %s", exc)
            raise

        text = getattr(resp, "text", None) or ""
        return _GeminiResponse(
            content=[_GeminiBlock(type="text", text=text)],
            model=model,
        )


# ── Fallback runner ──────────────────────────────────────────────────


def with_anthropic_fallback(
    stage_name: str,
    primary_call: Callable[[], Any],
    *,
    model: str | None = None,
    gemini_overrides: dict | None = None,
) -> Any:
    """Run ``primary_call`` (Anthropic). On retriable failure, retry via Gemini.

    The caller's ``primary_call`` lambda must close over the request it
    wants to make. On retriable error this helper:

      1. Logs the failure (stage name + exception class).
      2. If GOOGLE_API_KEY is set: builds a GeminiClient, invokes the
         same call shape via ``gemini_overrides`` (the lambda is
         closed-over; the override dict carries the retry kwargs).
      3. If not, re-raises the original error.

    Caller is responsible for plumbing the override into their re-call.
    For typical sites, the wrapper looks like::

        def call_with_fallback(client_factory, **kwargs):
            try:
                return client_factory().messages.create(**kwargs)
            except Exception as exc:
                if not is_retriable(exc):
                    raise
                gem = _get_gemini_client()
                if gem is None:
                    raise
                kwargs['model'] = map_to_gemini(kwargs.get('model'))
                return gem.messages.create(**kwargs)

    Returns whatever ``primary_call`` returns or its Gemini retry.
    """
    try:
        return primary_call()
    except Exception as exc:  # noqa: BLE001
        if not is_retriable(exc):
            raise
        if not os.environ.get("GOOGLE_API_KEY"):
            logger.info(
                "fallback (%s): retriable error %s but GOOGLE_API_KEY unset — "
                "propagating original error",
                stage_name, type(exc).__name__,
            )
            raise
        logger.warning(
            "fallback (%s): Anthropic %s — retrying via Gemini",
            stage_name, type(exc).__name__,
        )
        if gemini_overrides is None:
            raise  # caller didn't provide retry kwargs
        try:
            client = GeminiClient()
            kwargs = dict(gemini_overrides)
            kwargs["model"] = map_to_gemini(model)
            return client.messages.create(**kwargs)
        except Exception as gem_exc:  # noqa: BLE001
            logger.warning(
                "fallback (%s): Gemini also failed (%s) — raising original",
                stage_name, type(gem_exc).__name__,
            )
            raise exc from gem_exc
