"""Pipeline v2 — the Layer 1/2 rebuild that lives in parallel to
``faultline.llm.pipeline``.

Public surface (built up incrementally as stages land on the
``agent/layer1-dev-features-v1`` branch):

    Stage 0 — :func:`stage_0_intake`        (intake, stack detection)
    Stage 1 — :func:`stage_1_extractors`    (parallel deterministic extractors)
    Stage 2 — :func:`stage_2_reconcile`     (anchor reconciliation)
    Stage 3 — :func:`stage_3_flows`         (flow detection, Haiku 4.5)
    Stage 4 — :func:`stage_4_residual`      (LLM fallback, residual only)
    Stage 5 — :func:`stage_5_postprocess`   (naming-discipline + slug)
    Stages 6–7 — TODO

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
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    Stage2Result,
    stage_2_reconcile,
)
from faultline.pipeline_v2.stage_3_flows import (
    FeatureWithFlows,
    FlowSpec,
    Stage3Result,
    stage_3_flows,
)
from faultline.pipeline_v2.stage_4_residual import (
    Stage4Result,
    stage_4_residual,
)

__all__ = [
    # Stage 0
    "ScanContext",
    "Workspace",
    "stage_0_intake",
    # Stage 1 contract + orchestrator
    "AnchorCandidate",
    "AnchorExtractor",
    "stage_1_extractors",
    # Stage 2 reconciliation
    "DeveloperFeature",
    "Stage2Result",
    "stage_2_reconcile",
    # Stage 3 flow detection
    "FeatureWithFlows",
    "FlowSpec",
    "Stage3Result",
    "stage_3_flows",
    # Stage 4 residual LLM fallback
    "Stage4Result",
    "stage_4_residual",
]
