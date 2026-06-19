"""Process-wide "current pipeline stage" marker.

So LLM call sites can tag each request with the stage that issued it — read by
the local dev proxy (faultlines-app scripts/llm-subscription-proxy) to show the
exact stage per LLM call instead of guessing from the prompt.

Why a module-global stack (not a ContextVar): LLM-heavy stages (3, 4, 8…) fan
out calls across a ThreadPoolExecutor, and ContextVars do NOT propagate into
worker threads. The pipeline runs stages **sequentially**, so a lock-guarded
global stack is shared across all those worker threads and stays correct;
nesting (a stage inside a stage) is handled by the stack.

Leaf module — imports only the stdlib, so anything (run_logger, cost) can import
it without a cycle. Fully optional: if nothing pushed, current_stage() is None.
"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_STACK: list[dict[str, Any]] = []


def push_stage(num: int, name: str) -> int:
    """Mark a stage as current. Returns a token to pass to pop_stage()."""
    with _LOCK:
        _STACK.append({"num": num, "name": name})
        return len(_STACK)


def pop_stage(token: int | None = None) -> None:
    """Clear the current stage (best-effort; never raises)."""
    with _LOCK:
        if _STACK:
            _STACK.pop()


def current_stage() -> dict[str, Any] | None:
    """The innermost active stage, or None outside any stage."""
    with _LOCK:
        return dict(_STACK[-1]) if _STACK else None


__all__ = ["push_stage", "pop_stage", "current_stage"]
