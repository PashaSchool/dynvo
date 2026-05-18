"""Pipeline v2 — the Layer 1/2 rebuild that lives in parallel to
``faultline.llm.pipeline``.

Public surface (built up incrementally as stages land on the
``agent/layer1-dev-features-v1`` branch):

    Stage 0 — :func:`stage_0_intake`        (intake, stack detection)
    Stage 1 — :func:`stage_1_extractors`    (parallel deterministic extractors)
    Stages 2–7 — TODO

The legacy pipeline at ``faultline.llm.pipeline`` is untouched and
stays the default until v2 reaches parity.
"""

from faultline.pipeline_v2.extractors import (
    AnchorCandidate,
    AnchorExtractor,
)
from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    Workspace,
    stage_0_intake,
)
from faultline.pipeline_v2.stage_1_extractors import stage_1_extractors

__all__ = [
    # Stage 0
    "ScanContext",
    "Workspace",
    "stage_0_intake",
    # Stage 1 contract + orchestrator
    "AnchorCandidate",
    "AnchorExtractor",
    "stage_1_extractors",
]
