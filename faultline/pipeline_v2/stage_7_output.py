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

from faultline.models.types import (
    SCHEMA_VERSION,
    Feature,
    FeatureFlowEdge,
    FeatureMap,
    Flow,
    UfCapability,
    UserFlow,
)
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
    from faultline.cache.paths import faultline_base_dir

    slug = _repo_slug(repo_path)
    target = faultline_base_dir() / "logs" / slug
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
    flows: list[Flow] | None = None,
    feature_flow_edges: list[FeatureFlowEdge] | None = None,
    product_features: list[Feature] | None = None,
    user_flows: list[UserFlow] | None = None,
    path_index: dict[str, dict[str, Any]] | None = None,
    routes_index: list[dict[str, Any]] | None = None,
    is_full_scan: bool = True,
    base_scan_commit: str = "",
    scan_commit: str = "",
    engine_version: str = "",
    monorepo: dict[str, Any] | None = None,
    uf_capabilities: list[UfCapability] | None = None,
) -> FeatureMap:
    """Assemble the final :class:`FeatureMap`.

    ``features`` is fed as ``developer_features`` so the model
    validator stamps ``layer="developer"`` consistently and ``features``
    (the legacy back-compat field) gets the same list as a side effect.

    ``product_features`` (Sprint B3) is the Layer 2 cluster output —
    typically the result of Stage 6.5's deterministic product
    clusterer. Defaults to ``[]`` for back-compat with Layer-1-only
    callers and tests.

    ``flows`` / ``feature_flow_edges`` (Sprint B1) are the top-level
    bipartite store. The per-feature ``Feature.flows[]`` list stays
    populated as the containment projection so the landing app keeps
    working without modification.

    ``monorepo`` (Stage 6.6) is the deterministic ADDITIVE assembly view
    (per-project grouping + cross-project dependency graph) built by
    :func:`faultline.pipeline_v2.stage_6_6_monorepo_assembly.build_monorepo_assembly`.
    Defaults to ``{}`` (no field churn) for non-monorepo repos and for
    callers that don't compute it. NEVER mutates ``features``.

    ``repo_class`` (Stage 0.7, Phase C) is projected from
    ``scan_meta['repo_class']['class']`` — one source of truth, no new
    parameter for callers (the replay registry's Stage 7 call picks it
    up for free). ``""`` when the classifier didn't run (legacy
    callers / hand-built scan_meta).
    """
    repo_class = ""
    rc_block = scan_meta.get("repo_class")
    if isinstance(rc_block, dict):
        repo_class = str(rc_block.get("class") or "")
    return FeatureMap(
        schema_version=SCHEMA_VERSION,
        repo_path=str(ctx.repo_path),
        remote_url=_resolve_remote_url(ctx.repo_path),
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=len(ctx.commits),
        date_range_days=days,
        developer_features=list(features),
        product_features=list(product_features or []),
        scan_meta=dict(scan_meta),
        flows=list(flows or []),
        feature_flow_edges=list(feature_flow_edges or []),
        user_flows=list(user_flows or []),
        uf_capabilities=list(uf_capabilities or []),
        path_index=dict(path_index or {}),
        routes_index=list(routes_index or []),
        is_full_scan=is_full_scan,
        base_scan_commit=base_scan_commit,
        scan_commit=scan_commit,
        engine_version=engine_version,
        monorepo=dict(monorepo or {}),
        repo_class=repo_class,
    )


# ── Public entry point ──────────────────────────────────────────────────


def stage_7_output(
    features: list[Feature],
    ctx: "ScanContext",
    scan_meta: dict[str, Any],
    out_path: Path | None = None,
    *,
    days: int = 365,
    flows: list[Flow] | None = None,
    feature_flow_edges: list[FeatureFlowEdge] | None = None,
    product_features: list[Feature] | None = None,
    user_flows: list[UserFlow] | None = None,
    path_index: dict[str, dict[str, Any]] | None = None,
    routes_index: list[dict[str, Any]] | None = None,
    is_full_scan: bool = True,
    base_scan_commit: str = "",
    scan_commit: str = "",
    engine_version: str = "",
    monorepo: dict[str, Any] | None = None,
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
    # ── Stage 6.7e — UF grain lattice (deterministic, $0, additive) ──
    # Built here (not as a separate pipeline stage) so EVERY producer of a
    # feature map — live scan, replay chains from any recorded run, tests —
    # gets the capability rollup for free from the final ``user_flows``
    # leaves. Default ON (``FAULTLINE_UF_LATTICE=0`` disables); failure
    # degrades to an empty lattice, never a failed scan.
    from faultline.pipeline_v2.stage_6_7e_uf_lattice import (
        build_uf_lattice,
        lattice_enabled,
    )
    uf_capabilities: list[UfCapability] = []
    if user_flows and lattice_enabled():
        try:
            uf_capabilities, lattice_tele = build_uf_lattice(user_flows)
            scan_meta["uf_lattice"] = lattice_tele
        except Exception as exc:  # noqa: BLE001 — additive view, never fatal
            logger.warning("stage_6_7e: lattice build failed: %s", exc)
            uf_capabilities = []
            scan_meta["uf_lattice"] = {"enabled": True, "error": str(exc)}

    fm = build_feature_map(
        features, ctx, scan_meta,
        days=days, flows=flows, feature_flow_edges=feature_flow_edges,
        product_features=product_features,
        user_flows=user_flows,
        uf_capabilities=uf_capabilities,
        path_index=path_index,
        routes_index=routes_index,
        is_full_scan=is_full_scan,
        base_scan_commit=base_scan_commit,
        scan_commit=scan_commit,
        engine_version=engine_version,
        monorepo=monorepo,
    )

    # Snapshot Stage 7's input for replay before we hand off to the writer.
    write_stage_artifact(
        ctx.repo_path,
        stage_index=7,
        stage_name="output",
        payload={
            "feature_count": len(features),
            "feature_names": [f.name for f in features],
            "flows_total": len(flows or []),
            "feature_flow_edges_total": len(feature_flow_edges or []),
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
