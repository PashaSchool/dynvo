"""Production-mode gate for the engine.

Reads ``FAULTLINES_PRODUCTION=1`` from the environment. When set:

  * the engine MUST NOT write per-repo persistence caches
    (``~/.faultline/assignments-*.json``) — these break cold-scan
    semantics for SaaS workers running on shared hosts.
  * the LLM cache (``~/.faultline/llm-cache/*.json``) is still
    allowed — it's content-hashed (deterministic short-circuiting,
    not "remembering this repo") per memory/rule-cold-scan.md.

Dev default (envvar absent or "0"): unchanged behavior. The
assignments cache write paths in ``faultline.analyzer.assignments``
keep working for legacy ``pipeline.py`` flows.

The check is a single function so production hosts can mock it in
tests.
"""

from __future__ import annotations

import os


def production_mode_enabled() -> bool:
    """Return ``True`` when ``FAULTLINES_PRODUCTION`` is set to a truthy value.

    Truthy = ``"1"``, ``"true"``, ``"True"``, ``"yes"``. Anything
    else (absent, ``"0"``, ``""``) is dev mode (default).
    """
    raw = os.environ.get("FAULTLINES_PRODUCTION", "").strip().lower()
    return raw in {"1", "true", "yes"}


__all__ = ["production_mode_enabled"]
