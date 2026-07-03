"""Scan-unit scoped ``ScanContext`` construction (StackProfile Phase B+).

One tiny, dependency-light helper shared by per-unit profile selection
(:mod:`faultline.pipeline_v2.profiles._per_unit`), the composite
profile's per-path dispatch, and the per-unit ``repo_class`` refinement
(:mod:`faultline.pipeline_v2.stage_0_7_repo_class`). It builds a
read-only view of ``ctx`` narrowed to one scan unit's subtree:

  * ``tracked_files`` — the subset under the unit path (paths stay
    REPO-ROOT-relative; profiles read ``ctx.repo_path / rel``, so
    scoping the list is sufficient and no relativisation happens).
  * ``stack`` — the unit's own stack: the matching enumerated
    workspace's stack when one exists, else a fresh
    :func:`~faultline.pipeline_v2.stage_0_intake.detect_stack` against
    the unit root (with unit-relative file names, as that helper
    expects).
  * ``monorepo=False`` / ``workspaces=None`` — a unit is a single
    attribution scope; profile detection inside it must not recurse.
  * auditor fields cleared — the whole-repo auditor verdict does not
    transfer to a subtree.

Pure + deterministic. NO LLM, NO network, no mutation of the source
context.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    detect_stack,
)

if TYPE_CHECKING:
    pass


def scan_unit_subpaths(ctx: "ScanContext") -> list[str]:
    """The repo's per-profile scan-unit subpaths (``[]`` when none).

    Sources, in order:

      1. The Stage 0.6b partition's app/service units — EXCEPT when the
         workspaces were synthesized by the manifest-less
         split-fullstack rescue (those repos have always scanned
         whole-repo under one profile and their snapshots pin that).
      2. Single-workspace colocated-app fallback (the dispatch shape):
         the partition declines below MIN_WORKSPACES_FOR_PARTITION,
         but a workspace produced by the Phase B+ colocated-app
         discovery (``workspace_manager == "colocated"``) is still its
         own attribution scope when it classifies app/service.

    Pure + deterministic (sorted); never raises (``[]`` on any fault).
    Imported lazily to keep this module dependency-light.
    """
    try:
        from faultline.pipeline_v2.stage_0_6_project_classifier import (
            _UNIT_TYPES,
            classify_project,
            partition_monorepo,
        )

        plan = partition_monorepo(ctx)
        if plan.is_monorepo:
            if plan.synthesized_split:
                return []
            subpaths = sorted(u.subpath for u in plan.units if u.subpath)
            if subpaths:
                return subpaths
        if (
            ctx.monorepo
            and ctx.workspaces
            and ctx.workspace_manager == "colocated"
        ):
            return sorted(
                ws.path
                for ws in ctx.workspaces
                if ws.path
                and classify_project(
                    ctx.repo_path, ws,
                ).project_type in _UNIT_TYPES
            )
        return []
    except Exception:  # noqa: BLE001 — unit derivation must never fail a scan
        logging.getLogger(__name__).warning(
            "scan_unit_subpaths failed; falling back to whole-repo",
            exc_info=True,
        )
        return []


def unit_files(ctx: "ScanContext", subpath: str) -> list[str]:
    """Tracked files under ``subpath`` (repo-root-relative, ordered)."""
    prefix = subpath.rstrip("/") + "/"
    return [
        f for f in ctx.tracked_files
        if f == subpath or f.replace("\\", "/").startswith(prefix)
    ]


def residual_files(ctx: "ScanContext", subpaths: tuple[str, ...]) -> list[str]:
    """Tracked files under NONE of ``subpaths`` (the root residue)."""
    prefixes = tuple(sp.rstrip("/") + "/" for sp in subpaths)
    return [
        f for f in ctx.tracked_files
        if not any(
            f == p[:-1] or f.replace("\\", "/").startswith(p)
            for p in prefixes
        )
    ]


def _unit_stack(ctx: "ScanContext", subpath: str, files: list[str]) -> str | None:
    for ws in ctx.workspaces or []:
        if ws.path == subpath and ws.stack:
            return ws.stack
    prefix = subpath.rstrip("/") + "/"
    rel_files = [
        f[len(prefix):] if f.startswith(prefix) else f for f in files
    ]
    stack, _signals = detect_stack(ctx.repo_path / subpath, rel_files)
    return stack


def unit_scoped_ctx(ctx: "ScanContext", subpath: str) -> "ScanContext":
    """A read-only view of ``ctx`` narrowed to one scan unit."""
    files = unit_files(ctx, subpath)
    return ScanContext(
        repo_path=ctx.repo_path,
        stack=_unit_stack(ctx, subpath, files),
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=ctx.commits,
        stack_signals=[f"scan-unit scope: {subpath}"],
        workspace_manager=None,
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        audited_stack=None,
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=None,
        cache_backend=ctx.cache_backend,
        subpath=ctx.subpath,
    )


def residual_scoped_ctx(
    ctx: "ScanContext", subpaths: tuple[str, ...],
) -> "ScanContext":
    """A view of ``ctx`` minus every unit subtree (the root residue).

    Keeps the whole-repo stack fields (the residue IS the whole-repo
    story minus the units) and drops the unit workspaces from the
    enumeration so a root profile's workspace-fraction grades see only
    what it still owns.
    """
    files = residual_files(ctx, subpaths)
    kept_ws = [
        ws for ws in (ctx.workspaces or [])
        if not any(
            ws.path == sp or ws.path.startswith(sp.rstrip("/") + "/")
            for sp in subpaths
        )
    ] or None
    return ScanContext(
        repo_path=ctx.repo_path,
        stack=ctx.stack,
        monorepo=bool(kept_ws),
        workspaces=kept_ws,
        tracked_files=files,
        commits=ctx.commits,
        stack_signals=[f"residual scope (minus {len(subpaths)} units)"],
        workspace_manager=ctx.workspace_manager,
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        audited_stack=ctx.audited_stack,
        secondary_stacks=ctx.secondary_stacks,
        extractor_hints=ctx.extractor_hints,
        auditor_confidence=ctx.auditor_confidence,
        cache_backend=ctx.cache_backend,
        subpath=ctx.subpath,
    )


__all__ = [
    "residual_files",
    "residual_scoped_ctx",
    "scan_unit_subpaths",
    "unit_files",
    "unit_scoped_ctx",
]
