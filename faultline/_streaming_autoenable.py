"""Auto-streaming monkey-patch for the Anthropic SDK.

Anthropic requires ``stream=True`` for any ``messages.create`` whose
predicted runtime > 10 minutes (triggered by ``max_tokens`` × per-token
generation latency crossing the threshold). On Haiku 4.5 every stage
with ``max_tokens >= 8_192`` trips it; on Sonnet 4.6 the threshold is
borderline at ``max_tokens=32_768`` (which aggregator_detector.py uses)
— intermittent failures depending on prompt + load.

Many pipeline stages call ``client.messages.create(...)`` directly
without streaming. They each wrap the call in a broad ``try/except``
and silently degrade ("flow_judge: stage failed — skipping batch of 17",
"critique: API call failed — keeping pre-Sprint-9 result"). The
degradation is invisible in the final summary except as a stricter
``discovery_method='primary'`` everywhere and zero ``participants`` on
flows.

This module replaces ``anthropic.resources.messages.Messages.create``
with a wrapper that auto-uses ``messages.stream()`` (the context
manager) when ``max_tokens >= 8_192`` and ``stream`` isn't already
set. ``stream.get_final_message()`` returns the same ``Message`` shape
as the non-streamed call — every downstream consumer sees the
unchanged response.

Loaded from ``faultline/__init__.py`` so every CLI invocation is
patched before any stage constructs an ``Anthropic`` client.

Discovered 2026-05-17 in the Haiku-only worktree experiment when the
chi cold-scan produced a single LLM call total — all other stages had
silently failed with the "Streaming is required" error. Memory:
``pending-streaming-patch-port.md``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Below this threshold the SDK won't trip the 10-minute server-side
# guard regardless of model. 8_192 is the conservative floor — bigger
# max_tokens always streams, smaller never does.
_STREAM_FLOOR = 8_192


def _apply() -> None:
    try:
        from anthropic.resources.messages import Messages
    except Exception:  # pragma: no cover — SDK not installed
        logger.warning("Anthropic SDK not importable — streaming patch not applied")
        return

    if getattr(Messages.create, "_streaming_autoenable_patched", False):
        return

    _original_create = Messages.create

    def _patched_create(self: Messages, **kwargs: Any):  # type: ignore[override]
        if (
            kwargs.get("max_tokens", 0) >= _STREAM_FLOOR
            and not kwargs.get("stream")
        ):
            with self.stream(**kwargs) as stream:
                return stream.get_final_message()
        return _original_create(self, **kwargs)

    _patched_create._streaming_autoenable_patched = True  # type: ignore[attr-defined]
    Messages.create = _patched_create  # type: ignore[assignment]


_apply()
