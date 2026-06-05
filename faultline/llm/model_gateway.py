"""Env-aware AI-Gateway model-name mapping.

Pure compatibility shim. The Anthropic Python SDK already honours
``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_API_KEY`` from the environment, so
transport routing through ``https://ai-gateway.vercel.sh`` is automatic.
The ONLY incompatibility is the model slug: the gateway requires
provider-prefixed, dot-versioned IDs (``anthropic/claude-haiku-4.5``)
while the engine passes bare, dash-versioned IDs (``claude-haiku-4-5``).
Bare IDs return ``404 Model not found`` through the gateway.

This module normalises model IDs *only* when gateway mode is active.
On the direct-Anthropic path (no ``ANTHROPIC_BASE_URL``) every function
is a strict no-op and bare IDs pass through byte-identical to today.

NO prompt changes. NO model-selection logic. NO behaviour change on the
direct path. Apply :func:`resolve_model` at the point each Anthropic call
passes ``model=`` to ``.messages.create(...)``.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Host fragment that marks the Vercel AI Gateway. Matched as a substring
# of ANTHROPIC_BASE_URL so scheme/path variations still trip it.
_GATEWAY_HOST_FRAGMENT = "ai-gateway.vercel.sh"

# Generic env override: set FAULTLINES_MODEL_NAMESPACE to force gateway-style
# normalisation even when pointed at a non-Vercel gateway that uses the same
# provider-prefixed, dot-versioned slug convention.
_NAMESPACE_ENV = "FAULTLINES_MODEL_NAMESPACE"

# Provider prefix the gateway expects.
_PROVIDER_PREFIX = "anthropic/"

# Authoritative override table — every model ID the engine currently uses,
# mapped to the EXACT slug the gateway's /v1/models advertises. The generic
# rule below reproduces these, but the table is the source of truth so a
# gateway-side slug quirk can be pinned without touching the algorithm.
#
# NOTE: pinned dated snapshots (e.g. ...-20251001) map to the gateway's
# ROLLING slug (anthropic/claude-haiku-4.5). The gateway exposes no dated
# snapshots, so this is a minor, accepted snapshot-drift in gateway mode.
_KNOWN_MODELS: dict[str, str] = {
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-sonnet-4-20250514": "anthropic/claude-sonnet-4",
    "claude-opus-4-6": "anthropic/claude-opus-4.6",
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-opus-4-20250514": "anthropic/claude-opus-4",
}

# Trailing ``-YYYYMMDD`` snapshot date.
_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")

# Trailing ``-<major>-<minor>`` version pair (dash form) → dot form.
_VERSION_PAIR_RE = re.compile(r"-(\d+)-(\d+)$")


def gateway_mode_enabled() -> bool:
    """True when LLM calls are routed through an AI gateway.

    Driven entirely by env:
    - ``ANTHROPIC_BASE_URL`` contains ``ai-gateway.vercel.sh``, or
    - ``FAULTLINES_MODEL_NAMESPACE`` is set (generic gateway escape hatch).

    No host is hardcoded anywhere outside this function.
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if _GATEWAY_HOST_FRAGMENT in base_url:
        return True
    if os.environ.get(_NAMESPACE_ENV):
        return True
    return False


def to_gateway_model(model_id: str) -> str:
    """Normalise a bare engine model ID to its gateway slug.

    Deterministic and idempotent. Resolution order:

    1. Already provider-prefixed (``anthropic/...``) → returned unchanged.
    2. Exact match in the authoritative :data:`_KNOWN_MODELS` table.
    3. Generic rule for unknown IDs (logged at WARNING):
       - strip a trailing ``-YYYYMMDD`` snapshot date,
       - convert a trailing ``-<maj>-<min>`` to ``-<maj>.<min>``
         (a bare trailing ``-<maj>`` is left as-is),
       - prefix ``anthropic/``.
    """
    if not model_id:
        return model_id

    # (1) Idempotency: anything already namespaced is passed through.
    if model_id.startswith(_PROVIDER_PREFIX):
        return model_id

    # (2) Authoritative table.
    known = _KNOWN_MODELS.get(model_id)
    if known is not None:
        return known

    # (3) Generic fallback for IDs we have not pinned.
    logger.warning(
        "model_gateway: unknown model id %r not in known set; "
        "applying generic gateway normalisation",
        model_id,
    )
    normalised = _DATE_SUFFIX_RE.sub("", model_id)
    normalised = _VERSION_PAIR_RE.sub(r"-\1.\2", normalised)
    return _PROVIDER_PREFIX + normalised


def resolve_model(model_id: str) -> str:
    """Gateway-aware model resolver applied at each ``.messages.create``.

    Returns :func:`to_gateway_model` output when :func:`gateway_mode_enabled`,
    otherwise returns ``model_id`` byte-identical (direct-Anthropic no-op).
    """
    if gateway_mode_enabled():
        return to_gateway_model(model_id)
    return model_id


__all__ = ["gateway_mode_enabled", "to_gateway_model", "resolve_model"]
