"""Stage registry + dispatcher for replay (Sprint 9f).

Each stage has a small adapter that:
  - Takes a ``FeatureMap`` (plus a ``StageContext`` carrying optional
    deps like an LLM client, repo_root path).
  - Returns a new (possibly mutated) ``FeatureMap``.
  - Has no side effects on disk by default.

The adapter is intentionally a thin wrapper around the existing
aggregator code — production cli.py still drives the full pipeline.
This module exists so a single stage can be re-executed on a cached
artifact for fast iteration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from faultline.models.types import FeatureMap

logger = logging.getLogger(__name__)


@dataclass
class StageContext:
    """Dependencies a stage may need beyond the FeatureMap input.

    All optional — stages that don't need a dep ignore it. Stages
    that DO need a missing dep must error loudly.
    """

    repo_root: Path | None = None
    llm: object | None = None           # LlmClient or None
    api_key: str | None = None
    tracker: object | None = None       # CostTracker or None
    repo_structure: object | None = None


# ── Artifact I/O ─────────────────────────────────────────────────────


def _unwrap_feature_map(blob: dict) -> dict:
    """Locate the raw FeatureMap dict inside a possibly-wrapped
    artifact. Supports:
      - raw FeatureMap (has ``features`` key at top level)
      - 99-feature-map-final wrapper (``feature_map`` nested key)
      - stage-output wrapper with ``data.feature_map`` (rare)
    """
    if "features" in blob and "repo_path" in blob:
        return blob
    if "feature_map" in blob and isinstance(blob["feature_map"], dict):
        return blob["feature_map"]
    data = blob.get("data") or {}
    if isinstance(data, dict) and "feature_map" in data:
        return data["feature_map"]
    raise ValueError(
        "artifact does not contain a FeatureMap "
        "(expected top-level 'features' or wrapped 'feature_map')",
    )


def load_artifact(path: str | Path) -> FeatureMap:
    """Load a FeatureMap from an artifact file. Accepts raw or
    wrapped JSON.
    """
    blob = json.loads(Path(path).read_text())
    fm_dict = _unwrap_feature_map(blob)
    return FeatureMap.model_validate(fm_dict)


def save_artifact(fm: FeatureMap, path: str | Path) -> None:
    """Write a FeatureMap to disk as raw JSON (no wrapper). Use
    ``model_dump(mode="json")`` for stable datetime serialisation.
    """
    Path(path).write_text(
        json.dumps(fm.model_dump(mode="json"), indent=2, ensure_ascii=False),
    )


# ── Stage adapters ───────────────────────────────────────────────────


def _stage_feature_protection(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.aggregators.feature_protection import mark_protected
    new_fm, _ = mark_protected(fm)
    return new_fm


def _stage_critique(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.llm.recall_critique_runner import apply_critique_to_feature_map
    if ctx.repo_root is None:
        raise ValueError("stage 'critique' needs ctx.repo_root")
    new_fm, _ = apply_critique_to_feature_map(
        feature_map=fm,
        repo_root=ctx.repo_root,
        api_key=ctx.api_key,
        tracker=ctx.tracker,
        repo_structure=ctx.repo_structure,
    )
    return new_fm


def _stage_feature_dedup(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.aggregators.feature_dedup import dedup_features
    new_fm, _ = dedup_features(fm, llm=ctx.llm)
    return new_fm


def _stage_flow_attribution_critique(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.aggregators.flow_attribution_critique import critique_flow_attribution
    if ctx.llm is None:
        raise ValueError("stage 'flow-attribution-critique' needs ctx.llm")
    new_fm, _ = critique_flow_attribution(fm, llm=ctx.llm)
    return new_fm


def _stage_auto_split(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.aggregators.auto_split import split_oversized_features
    new_fm, _ = split_oversized_features(fm)
    return new_fm


def _stage_flow_reattribution(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.aggregators.flow_reattribution import FlowReattribution
    new_fm, _ = FlowReattribution().reattribute(fm)
    return new_fm


def _stage_zero_flow_recovery(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.aggregators.zero_flow_recovery import ZeroFlowRecovery
    if ctx.repo_root is None:
        raise ValueError("stage 'zero-flow-recovery' needs ctx.repo_root")
    new_fm, _ = ZeroFlowRecovery().recover(fm, repo_root=ctx.repo_root)
    return new_fm


def _stage_post_process(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    from faultline.analyzer.post_process import run as run_post_process
    return run_post_process(
        fm, repo_path=str(ctx.repo_root) if ctx.repo_root else "",
    )


def _stage_display_canonicalizer_strip(fm: FeatureMap, ctx: StageContext) -> FeatureMap:
    """Deterministic strip_page_suffix + scrub_structural_displays."""
    from faultline.aggregators.display_name_canonicalizer import (
        scrub_structural_displays,
        strip_page_suffix,
    )
    fm, _ = strip_page_suffix(fm)
    fm, _ = scrub_structural_displays(fm)
    return fm


_REGISTRY: dict[str, Callable[[FeatureMap, StageContext], FeatureMap]] = {
    "feature-protection":          _stage_feature_protection,
    "critique":                    _stage_critique,
    "feature-dedup":               _stage_feature_dedup,
    "flow-attribution-critique":   _stage_flow_attribution_critique,
    "auto-split":                  _stage_auto_split,
    "flow-reattribution":          _stage_flow_reattribution,
    "zero-flow-recovery":          _stage_zero_flow_recovery,
    "post-process":                _stage_post_process,
    "display-canonicalizer-strip": _stage_display_canonicalizer_strip,
}


# ── Public API ───────────────────────────────────────────────────────


def list_stages() -> list[str]:
    """Return the list of stage names supported by replay."""
    return list(_REGISTRY.keys())


def _deep_copy_fm(fm: FeatureMap) -> FeatureMap:
    """Deep-copy a FeatureMap so a stage that mutates won't leak
    mutations into the caller's input. Critical for replay safety —
    the input artifact is treated as immutable observation.
    """
    return fm.model_copy(deep=True)


def run_stage(
    name: str, fm: FeatureMap, *, ctx: StageContext | None = None,
    isolate: bool = True,
) -> FeatureMap:
    """Re-execute a single stage on ``fm``.

    ``isolate`` (Sprint 10a): when True (default), the input is deep-
    copied before the stage runs. Guarantees the caller's FeatureMap
    is never mutated by a stage that still uses the in-place
    contract internally. Set False ONLY when chaining stages that
    explicitly hand off ownership.
    """
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown stage {name!r}; available: {sorted(_REGISTRY)}",
        )
    target = _deep_copy_fm(fm) if isolate else fm
    return _REGISTRY[name](target, ctx or StageContext())


def run_chain(
    names: list[str], fm: FeatureMap, *, ctx: StageContext | None = None,
) -> FeatureMap:
    """Run stages in sequence. Input ``fm`` is deep-copied once; each
    stage hands its output to the next (no per-stage copy — that
    would be O(N^2) memory on long chains).
    """
    ctx = ctx or StageContext()
    target = _deep_copy_fm(fm)
    for i, name in enumerate(names):
        # Only the first stage receives an already-isolated copy;
        # subsequent stages re-use the chain's working FeatureMap.
        target = run_stage(name, target, ctx=ctx, isolate=False)
    return target
