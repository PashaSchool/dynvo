"""React adapter — stub for Sprint 2 (functional in Sprint 3+).

Planned detections (NOT shipped in v1):

  * ``useEffect(() => fetch(...))`` lifecycle-triggered HTTP
  * React Query / SWR hooks already covered by
    :mod:`faultline.pipeline_v2.flow_expansion.cross_stack`.
  * Suspense + ``use()`` data fetching.
"""

from __future__ import annotations


def detect_react_specific_edges(*_args: object, **_kw: object) -> list[object]:
    """Returns empty list — adapter is intentionally inert in v1."""
    return []


__all__ = ["detect_react_specific_edges"]
