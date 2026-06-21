"""Phase 5 — auto-partition assembly: stitch ISOLATED per-project scans.

This module is the PAYOFF of the brain-partitioner
(:mod:`faultline.pipeline_v2.stage_0_6_project_classifier`). Where Stage
6.6 (:mod:`faultline.pipeline_v2.stage_6_6_monorepo_assembly`) produces the
*approximate* monorepo view — it re-projects a SINGLE whole-repo scan's
flat ``developer_features`` into projects by path-overlap — this module
produces the *precise* view: each project's featuremap is its OWN
ISOLATED scan, so its features are WITHIN-project by construction (a
whole-repo scan can never span them).

Pipeline position
=================

The CLI's ``--auto-partition`` flag runs the repo-level intake + partition
ONCE (:func:`stage_0_6_project_classifier.partition_monorepo`), feeds
``plan.subpaths()`` to
:func:`faultline.pipeline_v2.multi.run_pipeline_multi` (which scans each
sub-project in isolation, sharing one git pass), and hands the resulting
``list[MultiScanResult]`` here to be assembled into one monorepo output
structure.

Design tenets (mirror the rest of pipeline_v2)
=============================================

  - Pure + deterministic. :func:`build_partition_assembly` is a pure
    function of (repo_root, plan, results): same inputs -> same output.
    No LLM, no network — $0. The only I/O is reading each project's
    already-written FeatureMap JSON off disk (degrades to ``None`` on a
    missing/unreadable file; never raises).
  - REUSE, don't re-implement. The cross-project dependency graph is the
    SAME extractor Stage 6.6 uses
    (:func:`stage_6_6_monorepo_assembly.build_cross_project_graph` over
    ``plan.classifications``) — this module does not duplicate the dep
    graph.
  - DB model B (memory: project-monorepo-subprojects-2026-06-09): each
    scan UNIT is a sub-project, 1:1 with a subpath. The assembled output
    carries one ``project`` per ``MultiScanResult``, in plan order.
  - Back-compat sacred. Single-project / non-monorepo repos NEVER reach
    this module — the CLI keeps emitting the normal flat FeatureMap. When
    called on a non-monorepo plan defensively, it returns the trivial
    ``{"is_monorepo": False}`` (same shape Stage 6.6 returns), so a caller
    can treat it uniformly.
  - No magic numbers, no repo-specific paths (memory: rule-no-magic-tuning
    / rule-no-repo-specific-paths). Only structural facts (plan units,
    feature counts, file-set sizes) feed the stats.

Output contract
===============

A NEW top-level shape (NOT a flat FeatureMap — the result is intrinsically
multi-project, with one full featuremap nested per project)::

    {
      "is_monorepo": True,
      "partition": "isolated-per-project",   # vs Stage 6.6 "single-scan-grouping"
      "projects": [
        {
          "name": "twenty-front",
          "type": "app",
          "subpath": "packages/twenty-front",
          "scan_status": "ok",                # ok | failed
          "error": None,                      # str on failure
          "out_path": "/abs/path/to/feature-map-….json",
          "featuremap": {                     # the ISOLATED scan, None on failure
            "developer_features": [...],
            "product_features": [...],
            "user_flows": [...],
            "flows": [...],
            "feature_flow_edges": [...],
            "routes_index": [...],
            "path_index": {...},
            "scan_meta": {...},
          },
          "summary": {                        # cheap per-project rollup
            "developer_feature_count": 42,
            "user_flow_count": 12,
            "flow_count": 30,
            "max_feature_share": 0.18,        # the per-project BLOB metric
            "file_count": 412,
          },
        },
        ...
      ],
      "cross_project_graph": {"nodes": [...], "edges": [...]},  # REUSED Stage 6.6
      "partition_plan": {
        "units": [{subpath, project_type, name}, ...],
        "excluded": [{path, type, reason}, ...],
        "rationale": "...",
      },
      "stats": {
        "project_count": 4,
        "scanned": 4,
        "failed": 0,
        "edge_count": 7,
        "developer_feature_total": 130,       # sum across projects
        "max_project_blob_share": 0.31,       # worst per-project blob
      },
    }

The nested ``featuremap`` is the project's FeatureMap JSON minus the
heavyweight repo-level scalars that are meaningless once nested
(``repo_path`` / ``remote_url`` / ``engine_version`` live on the
top-level scan record). It carries every key a consumer needs to render
that project's Signal Room independently.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from faultline.pipeline_v2.stage_6_6_monorepo_assembly import (
    DepEdgeExtractor,
    build_cross_project_graph,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.multi import MultiScanResult
    from faultline.pipeline_v2.stage_0_6_project_classifier import PartitionPlan

logger = logging.getLogger(__name__)


# Keys carried into the nested per-project ``featuremap``. Repo-level
# scalars (repo_path / remote_url / engine_version / analyzed_at /
# total_commits / date_range_days) are dropped — they belong on the
# top-level scan record, not on each nested project. ``monorepo`` is
# dropped too: a sub-project scan is single-project, so its own assembly
# view is always the trivial ``{"is_monorepo": False}`` — noise here.
_PROJECT_FEATUREMAP_KEYS: tuple[str, ...] = (
    "schema_version",
    "developer_features",
    "product_features",
    "user_flows",
    "flows",
    "feature_flow_edges",
    "routes_index",
    "path_index",
    "scan_meta",
    "is_full_scan",
)


def _read_featuremap(out_path: Path | None) -> dict[str, Any] | None:
    """Read a project's written FeatureMap JSON, projected to project keys.

    Returns ``None`` (logged) when the path is missing or the JSON is
    unreadable — never raises, so one corrupt artifact can't break the
    whole assembly.
    """
    if out_path is None:
        return None
    try:
        raw = json.loads(Path(out_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "auto_partition: could not read featuremap %s (%s)", out_path, exc
        )
        return None
    if not isinstance(raw, dict):
        return None
    return {k: raw[k] for k in _PROJECT_FEATUREMAP_KEYS if k in raw}


def _feature_file_set(feature: dict[str, Any]) -> set[str]:
    """Distinct file paths a developer feature touches.

    Reads ``member_files`` FIRST — the engine's canonical membership field,
    a list of ``{path, role, primary, ...}`` dicts — exactly as
    ``cold_eval._file_set`` does, then falls back to ``paths``/``files``.
    Reading ``paths`` only diverged from the canonical judge and produced a
    DIFFERENT blob metric than the one cold_eval gates on (the same
    ``_file_set`` schema mismatch that mis-measured the blob corpus-wide —
    see ``finding-coldeval-blob-broken-2026-06-19``). Both degrade to empty.
    Used for the per-project blob metric WITHOUT re-reading any source.
    """
    files: set[str] = set()
    mf = feature.get("member_files")
    if isinstance(mf, list):
        for m in mf:
            if isinstance(m, dict):
                pp = m.get("path")
                if isinstance(pp, str):
                    files.add(pp)
            elif isinstance(m, str):
                files.add(m)
    if files:
        return files
    for key in ("paths", "files"):
        val = feature.get(key)
        if isinstance(val, list):
            for p in val:
                if isinstance(p, str):
                    files.add(p)
                elif isinstance(p, dict):
                    # Tolerate path-objects ({path: ...}) defensively.
                    pp = p.get("path")
                    if isinstance(pp, str):
                        files.add(pp)
    return files


def _project_summary(featuremap: dict[str, Any] | None) -> dict[str, Any]:
    """Cheap per-project rollup, incl. the per-project BLOB metric.

    ``max_feature_share`` = the largest fraction of the project's distinct
    owned files held by a single developer feature. This is the SAME blob
    signal ``cold_eval`` computes (owned_max / files) but scoped to the
    ISOLATED project scan — the metric this whole phase is trying to lower
    vs the whole-repo scan. Scale-invariant (a ratio), so it compares
    fairly across a 5-feature project and a 200-feature one.

    Returns zeros for a missing featuremap (failed scan).
    """
    if not featuremap:
        return {
            "developer_feature_count": 0,
            "user_flow_count": 0,
            "flow_count": 0,
            "max_feature_share": 0.0,
            "file_count": 0,
        }
    feats = featuremap.get("developer_features") or []
    user_flows = featuremap.get("user_flows") or []
    flows = featuremap.get("flows") or []

    per_feature_files: list[set[str]] = []
    all_files: set[str] = set()
    for f in feats:
        if not isinstance(f, dict):
            continue
        fs = _feature_file_set(f)
        per_feature_files.append(fs)
        all_files |= fs

    total_files = len(all_files)
    if total_files > 0 and per_feature_files:
        max_owned = max((len(fs) for fs in per_feature_files), default=0)
        max_share = round(max_owned / total_files, 4)
    else:
        max_share = 0.0

    return {
        "developer_feature_count": len(feats),
        "user_flow_count": len(user_flows),
        "flow_count": len(flows),
        "max_feature_share": max_share,
        "file_count": total_files,
    }


def build_partition_assembly(
    repo_path: Path | str,
    plan: "PartitionPlan",
    results: Sequence["MultiScanResult"],
    *,
    edge_extractors: Sequence[DepEdgeExtractor] | None = None,
) -> dict[str, Any]:
    """Assemble ISOLATED per-project scans into one monorepo output.

    Pure + deterministic + $0. Reuses
    :func:`stage_6_6_monorepo_assembly.build_cross_project_graph` for the
    cross-project dependency graph (over ``plan.classifications``) so the
    graph is identical to the single-scan assembly view — the ONLY
    difference is that each project's features come from its own isolated
    scan, not from path-overlap grouping of a whole-repo scan.

    Args:
        repo_path: absolute repo root (the dep graph reads manifests under
            it).
        plan: the deterministic :class:`PartitionPlan` that produced the
            subpaths. ``plan.subpaths()`` must equal the ``results``
            subpaths (in order) — i.e. the caller fed exactly this plan to
            ``run_pipeline_multi``.
        results: the per-subpath outcomes from ``run_pipeline_multi``.
            Each carries an ``out_path`` (the written FeatureMap JSON) on
            success, or an ``error`` on failure (recorded, not raised).
        edge_extractors: optional override of the dep-edge extractor set
            (tests). ``None`` -> the default per-ecosystem extractors.

    Returns:
        The assembled monorepo dict (see the module docstring for the full
        contract). Returns ``{"is_monorepo": False}`` defensively when the
        plan is not a monorepo (a caller should not invoke it then, but the
        guard keeps the shape uniform).
    """
    repo_root = Path(repo_path).resolve()

    if not plan.is_monorepo:
        return {"is_monorepo": False}

    # ── Cross-project dependency graph — REUSE Stage 6.6 extractor ──────
    # Over EVERY classified project (apps/services that became units AND
    # the ride-along libs) so a lib's fan_in is visible — same as the
    # single-scan view.
    nodes, edges = build_cross_project_graph(
        repo_root, plan.classifications, extractors=edge_extractors
    )

    # ── Per-unit metadata from the plan (subpath -> ScanUnit) ──────────
    unit_by_subpath = {u.subpath: u for u in plan.units if u.subpath is not None}

    # ── One project entry per scan result (DB model B: unit = project) ──
    projects: list[dict[str, Any]] = []
    developer_feature_total = 0
    project_blob_shares: list[float] = []
    scanned = 0
    failed = 0
    for r in results:
        unit = unit_by_subpath.get(r.subpath)
        featuremap = _read_featuremap(r.out_path)
        ok = r.error is None and featuremap is not None
        if ok:
            scanned += 1
        else:
            failed += 1
        summary = _project_summary(featuremap)
        developer_feature_total += summary["developer_feature_count"]
        if ok and summary["file_count"] > 0:
            project_blob_shares.append(summary["max_feature_share"])
        projects.append(
            {
                "name": unit.name if unit is not None else r.subpath,
                "type": unit.project_type if unit is not None else "app",
                "subpath": r.subpath,
                "scan_status": "ok" if ok else "failed",
                "error": r.error,
                "out_path": str(r.out_path) if r.out_path is not None else None,
                "featuremap": featuremap,
                "summary": summary,
            }
        )

    max_project_blob_share = (
        round(max(project_blob_shares), 4) if project_blob_shares else 0.0
    )

    return {
        "is_monorepo": True,
        "partition": "isolated-per-project",
        "projects": projects,
        "cross_project_graph": {
            "nodes": [
                {"name": n.name, "type": n.type, "subpath": n.subpath, "fan_in": n.fan_in}
                for n in nodes
            ],
            "edges": [
                {
                    "from": e.from_project,
                    "to": e.to_project,
                    "ecosystem": e.ecosystem,
                    "via": e.via,
                }
                for e in edges
            ],
        },
        "partition_plan": {
            "units": [
                {"subpath": u.subpath, "project_type": u.project_type, "name": u.name}
                for u in plan.units
            ],
            "excluded": [
                {"path": e.path, "type": e.type, "reason": e.reason}
                for e in plan.excluded
            ],
            "rationale": plan.rationale,
        },
        "stats": {
            "project_count": len(projects),
            "scanned": scanned,
            "failed": failed,
            "edge_count": len(edges),
            "developer_feature_total": developer_feature_total,
            "max_project_blob_share": max_project_blob_share,
        },
    }


__all__ = ["build_partition_assembly"]
