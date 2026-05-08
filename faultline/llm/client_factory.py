"""LLM client factory — picks Anthropic or Gemini based on env / arg.

Used by the new pipeline call sites (sonnet_scanner, flow_detector_v2,
flow_judge) so that ``FAULTLINE_USE_GEMINI=1`` swaps the primary LLM
to Gemini end-to-end. Default behaviour unchanged.

Env vars
========

  FAULTLINE_USE_GEMINI=1   route every primary call to Gemini
  GEMINI_API_KEY           required when FAULTLINE_USE_GEMINI=1
  ANTHROPIC_API_KEY        used when FAULTLINE_USE_GEMINI is unset

Usage
=====

    from faultline.llm.client_factory import make_llm_client

    client = make_llm_client(api_key)        # Anthropic by default
    client.messages.create(...)              # same shape both ways
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _gemini_only_enabled() -> bool:
    flag = os.environ.get("FAULTLINE_USE_GEMINI", "").strip().lower()
    return flag in {"1", "true", "yes", "on", "pro", "flash"}


def _gemini_force_flash() -> bool:
    """Free-tier safety: when env var is '1' or 'flash' (default for free
    accounts), every Sonnet-class call routes to gemini-flash-latest
    instead of gemini-2.5-pro. Pro requires a paid Gemini plan
    (free-tier limit: 0). Set FAULTLINE_USE_GEMINI=pro to override
    when you have a paid plan.
    """
    flag = os.environ.get("FAULTLINE_USE_GEMINI", "").strip().lower()
    if flag == "pro":
        return False  # caller has paid access — keep Pro mapping
    return flag in {"1", "true", "yes", "on", "flash"}


def make_llm_client(api_key: str | None = None):
    """Return an LLM client whose ``.messages.create(...)`` is Anthropic-shaped.

    When ``FAULTLINE_USE_GEMINI=1`` env is set, returns ``GeminiClient``.
    Otherwise returns ``anthropic.Anthropic``.
    """
    if _gemini_only_enabled():
        from faultline.llm.gemini_fallback import GeminiClient
        logger.info("client_factory: FAULTLINE_USE_GEMINI=1 — using GeminiClient")
        return GeminiClient()
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def gemini_model_for(anthropic_model: str | None) -> str | None:
    """If Gemini-only mode is on, return the Gemini model id mapped from
    the requested Anthropic model. Returns the input unchanged otherwise.

    Free-tier override: when FAULTLINE_USE_GEMINI is '1' / 'flash'
    (default, no opt-in for paid Pro), all calls route to
    gemini-flash-latest because Pro free tier has limit=0.
    """
    if not _gemini_only_enabled():
        return anthropic_model
    if _gemini_force_flash():
        return "gemini-flash-latest"
    from faultline.llm.gemini_fallback import map_to_gemini
    return map_to_gemini(anthropic_model)
