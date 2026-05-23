"""Express adapter — stub for Sprint 2 (functional in Sprint 3+).

Express middleware chains (``app.use(authMiddleware)``) are intra-
repo function references and DO get picked up by the universal T1
call-graph. This adapter is reserved for future Express-only
detections such as router-mount edges (``app.use('/api', router)``).
"""

from __future__ import annotations


def detect_express_specific_edges(*_args: object, **_kw: object) -> list[object]:
    """Returns empty list — adapter is intentionally inert in v1."""
    return []


__all__ = ["detect_express_specific_edges"]
