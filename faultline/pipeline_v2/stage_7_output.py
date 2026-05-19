"""Stage 7 — output assembly.

Builds the final :class:`FeatureMap` from the Stage 6 enriched
:class:`Feature` list, attaches ``scan_meta`` telemetry, and persists
it via the existing :func:`faultline.output.writer.write_feature_map`.

The pipeline-v2 output is intentionally compatible with the legacy
on-disk schema:

  - ``features`` keeps every developer feature (back-compat with the
    landing app, replay registry, cloud sync, incremental loader).
  - ``developer_features`` / ``product_features`` are re-derived on
    ``model_dump`` so v2 consumers can read the layered shape directly.
  - ``product_features`` is always empty for Layer 1 (Layer 2 is
    deferred per the rebuild plan).
  - ``scan_meta`` carries stage timings, stack/monorepo detection,
    LLM fallback share, model id, and any warnings the pipeline
    collected.

Per-stage artifact logging
==========================

Each stage can call :func:`write_stage_artifact` to drop a single JSON
snapshot of its output into ``~/.faultline/logs/<slug>/NN-stage-<name>.json``.
This is debug-only — the orchestrator wires it from the outside so
individual stages stay pure.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.models.types import Feature, FeatureMap
from faultline.output.writer import write_feature_map

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Per-stage artifact logging ──────────────────────────────────────────


def _repo_slug(repo_path: Path | str) -> str:
    """Same slug rule the writer uses — kebab-cased dirname."""
    name = Path(repo_path).name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "repo"


def stage_artifact_dir(repo_path: Path | str) -> Path:
    """Return the per-slug artifact directory, creating it if needed.

    This is the *parent* of all per-run directories — i.e.
    ``~/.faultline/logs/<slug>/``. Callers writing stage artifacts
    for a specific run should pass ``ctx.run_dir`` (or call
    ``write_stage_artifact(..., run_dir=ctx.run_dir)``) so the
    snapshot lands under the run-id subdir.
    """
    slug = _repo_slug(repo_path)
    target = Path.home() / ".faultline" / "logs" / slug
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_stage_artifact(
    repo_path: Path | str,
    stage_index: int,
    stage_name: str,
    payload: dict[str, Any],
    *,
    run_dir: Path | None = None,
) -> Path:
    """Write a single stage's output snapshot to disk for replay/debug.

    Args:
        repo_path: scan target — used for the slug directory when
            ``run_dir`` is not provided.
        stage_index: 0..7, matches the pipeline stage number.
        stage_name: short kebab slug ("intake", "extractors", ...).
        payload: a JSON-serialisable dict. Datetimes are stringified
            via the default callable.
        run_dir: when provided, the artifact lives under this dir
            (typically ``ctx.run_dir`` — i.e.
            ``~/.faultline/logs/<slug>/<run_id>/``). When ``None``
            we fall back to the legacy flat layout for back-compat.

    Returns:
        The full path the artifact was written to. Errors are caught
        and logged (debug-only logging should never break a scan).
    """
    target_dir = Path(run_dir) if run_dir is not None else stage_artifact_dir(repo_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{stage_index:02d}-stage-{stage_name}.json"
    path = target_dir / fname
    try:
        path.write_text(json.dumps(payload, indent=2, default=str))
    except Exception as exc:  # noqa: BLE001 — debug artifact, never fatal
        logger.warning("stage_7_output: failed to write %s: %s", path, exc)
    return path


# ── FeatureMap construction ─────────────────────────────────────────────


def _resolve_remote_url(repo_path: Path) -> str:
    """Best-effort remote URL discovery; never raises."""
    try:
        from faultline.analyzer.git import get_remote_url, load_repo
        repo = load_repo(str(repo_path))
        return get_remote_url(repo) or ""
    except Exception as exc:  # noqa: BLE001 — non-git fixtures, missing remote
        logger.debug("stage_7_output: no remote_url (%s)", exc)
        return ""


def build_feature_map(
    features: list[Feature],
    ctx: "ScanContext",
    scan_meta: dict[str, Any],
    *,
    days: int = 365,
) -> FeatureMap:
    """Assemble the final :class:`FeatureMap`.

    The Layer 2 product feature list is intentionally empty — pipeline
    v2 stops at Layer 1.

    ``features`` is fed as ``developer_features`` so the model
    validator stamps ``layer="developer"`` consistently and ``features``
    (the legacy back-compat field) gets the same list as a side effect.
    """
    return FeatureMap(
        repo_path=str(ctx.repo_path),
        remote_url=_resolve_remote_url(ctx.repo_path),
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=len(ctx.commits),
        date_range_days=days,
        developer_features=list(features),
        product_features=[],
        scan_meta=dict(scan_meta),
    )


# ── Public entry point ──────────────────────────────────────────────────


def stage_7_output(
    features: list[Feature],
    ctx: "ScanContext",
    scan_meta: dict[str, Any],
    out_path: Path | None = None,
    *,
    days: int = 365,
) -> Path:
    """Build the :class:`FeatureMap`, persist it, and return the path.

    Args:
        features: Stage 6 enriched feature list.
        ctx: Stage 0 context.
        scan_meta: accumulated telemetry from the orchestrator.
        out_path: explicit output path. When ``None``, the writer picks
            a timestamped path under ``~/.faultline/``.
        days: history window — defaults to 365, matching Stage 0's
            default.

    Returns:
        The :class:`Path` the feature map was written to.
    """
    fm = build_feature_map(features, ctx, scan_meta, days=days)

    # Snapshot Stage 7's input for replay before we hand off to the writer.
    write_stage_artifact(
        ctx.repo_path,
        stage_index=7,
        stage_name="output",
        payload={
            "feature_count": len(features),
            "feature_names": [f.name for f in features],
            "scan_meta": scan_meta,
        },
        run_dir=ctx.run_dir,
    )

    written = write_feature_map(fm, str(out_path) if out_path else None)
    return Path(written)


__all__ = [
    "build_feature_map",
    "stage_7_output",
    "stage_artifact_dir",
    "write_stage_artifact",
]
