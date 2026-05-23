"""FastAPI adapter — stub for Sprint 2 (functional in Sprint 3+).

Planned detections (NOT shipped in v1):

  * ``Depends(...)`` dependency-injection edges from a route handler
    to its DI factories.
  * ``BackgroundTasks.add_task(fn)`` async work edges.
"""

from __future__ import annotations


def detect_fastapi_specific_edges(*_args: object, **_kw: object) -> list[object]:
    """Returns empty list — adapter is intentionally inert in v1."""
    return []


__all__ = ["detect_fastapi_specific_edges"]
