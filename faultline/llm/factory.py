"""Pluggable LLM client factory (Sprint 8h).

Aggregators ask for a client by ROLE (e.g. ``"critique"``,
``"canonicalizer"``, ``"flow_critique"``) — the factory resolves the
role to a provider+model from env vars (or sensible defaults), then
constructs and returns the matching adapter.

Per-role env-var convention:

    FAULTLINE_LLM_<ROLE>_PROVIDER  (e.g. ``anthropic``, ``gemini``)
    FAULTLINE_LLM_<ROLE>_MODEL     (provider-specific model name)

Falls back to ``FAULTLINE_LLM_DEFAULT_PROVIDER`` /
``FAULTLINE_LLM_DEFAULT_MODEL`` when role-specific vars are absent,
then to ``("anthropic", "claude-haiku-4-5")``.

API keys come from the per-provider env var the SDK would normally
read (``ANTHROPIC_API_KEY``, ``GOOGLE_API_KEY``).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from faultline.llm.client import LlmClient

if TYPE_CHECKING:
    from faultline.llm.cost import CostTracker

logger = logging.getLogger(__name__)


_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL_BY_PROVIDER = {
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-2.5-flash",
}


def _env_role(role: str, suffix: str) -> str | None:
    """Read FAULTLINE_LLM_<ROLE>_<SUFFIX> with fallback to
    FAULTLINE_LLM_DEFAULT_<SUFFIX>. Returns None when neither set.
    """
    role_upper = role.upper().replace("-", "_")
    val = os.environ.get(f"FAULTLINE_LLM_{role_upper}_{suffix}")
    if val:
        return val
    val = os.environ.get(f"FAULTLINE_LLM_DEFAULT_{suffix}")
    return val or None


def resolve_role(role: str) -> tuple[str, str]:
    """Return the (provider, model) selected for ``role``.

    Priority: role-specific env > default env > built-in default.
    """
    provider = _env_role(role, "PROVIDER") or _DEFAULT_PROVIDER
    model = (
        _env_role(role, "MODEL")
        or _DEFAULT_MODEL_BY_PROVIDER.get(provider)
        or _DEFAULT_MODEL_BY_PROVIDER[_DEFAULT_PROVIDER]
    )
    return provider, model


def make_client(
    role: str,
    *,
    tracker: "CostTracker | None" = None,
    api_key: str | None = None,
) -> LlmClient:
    """Construct an ``LlmClient`` for ``role``.

    ``api_key``: when supplied, used in place of the env var. Useful
    for testing or for the CLI's --api-key flag.
    """
    provider, model = resolve_role(role)
    cost_label = f"{provider}-{role}"

    if provider == "anthropic":
        from anthropic import Anthropic
        from faultline.llm.providers.anthropic_client import AnthropicLlmClient
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; cannot create Anthropic client",
            )
        sdk = Anthropic(api_key=key)
        return AnthropicLlmClient(
            client=sdk, model=model, tracker=tracker, cost_label=cost_label,
        )

    if provider == "gemini":
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed; pip install google-genai",
            ) from exc
        from faultline.llm.providers.gemini_client import GeminiLlmClient
        key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set; cannot create Gemini client",
            )
        sdk = genai.Client(api_key=key)
        return GeminiLlmClient(
            client=sdk, model=model, tracker=tracker, cost_label=cost_label,
        )

    raise RuntimeError(
        f"Unknown LLM provider {provider!r} for role {role!r}; "
        f"set FAULTLINE_LLM_{role.upper()}_PROVIDER to "
        f"'anthropic' or 'gemini'",
    )


__all__ = ["make_client", "resolve_role"]
