"""Pipeline v2 — the Layer 1/2 rebuild that lives in parallel to
``faultline.llm.pipeline``.

As of 2026-05-18 this package contains only Stage 0 (repo intake).
Subsequent stages — extractors orchestrator, developer-feature
clustering, optional Layer 2 product-feature roll-up — will land in
sibling modules under this package as the rebuild proceeds. The
legacy pipeline at ``faultline.llm.pipeline`` is untouched and stays
the default until v2 reaches parity.

Public surface:

    >>> from faultline.pipeline_v2 import stage_0_intake, ScanContext, Workspace
"""

from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    Workspace,
    stage_0_intake,
)

__all__ = ["ScanContext", "Workspace", "stage_0_intake"]
