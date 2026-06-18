"""Monorepo workspace-splitter — normalised facade over workspace detection.

The splitter answers ONE question for the Framework Knowledge Layer:
*what are the independent feature-attribution scopes in this repo?*

  * Monorepo (Turborepo / pnpm / npm-yarn / nx / lerna / cargo / go.work)
    → one :class:`Workspace` per declared package, with that package's
    files scoped to it.
  * Single-package repo → exactly ``[root]`` — one workspace == the repo
    root, carrying every tracked file. This is the byte-for-byte
    preservation of the non-monorepo path: downstream stages see the
    same single scope they always saw.

This module is a THIN normaliser: the actual format detection +
glob expansion already lives in
:func:`faultline.analyzer.workspace.detect_workspace` (it covers all
seven formats, with node_modules / target / dist descent guards and a
manifest-required rule so bare source dirs never become phantom
workspaces). We do NOT re-implement that — we reuse it and adapt its
:class:`WorkspaceInfo` into the pipeline's canonical
:class:`~faultline.pipeline_v2.stage_0_intake.Workspace` shape.

Universal by construction: no corpus-specific paths, no magic numbers.
Workspace membership is whatever the package manager manifest declares
(``apps/*``, ``packages/*``, ``members = [...]``, ``use ./mod``), never
a hardcoded directory name. Single-root fallback is structural, not a
threshold.

Deterministic — NO LLM, NO network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.analyzer.workspace import (
    WorkspaceInfo,
    detect_workspace,
)
from faultline.pipeline_v2.stage_0_intake import Workspace

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# The slug used for the implicit single-workspace == repo-root scope.
ROOT_WORKSPACE_NAME = "."
# Relative path marker for the repo root workspace.
ROOT_WORKSPACE_PATH = ""


def split_workspaces(ctx: "ScanContext") -> list[Workspace]:
    """Return the attribution scopes for ``ctx`` — never empty, never raises.

    Order of resolution:
      1. If Stage 0 already enumerated ``ctx.workspaces`` (it runs
         :func:`detect_workspace` during intake), normalise + return
         those — we do not re-walk the filesystem.
      2. Otherwise run :func:`detect_workspace` against the tracked
         files. If it detects a monorepo, adapt each package to a
         :class:`Workspace`.
      3. If nothing is detected, return the single root workspace
         carrying every tracked file (the single-package path).

    The returned list is the unit of Stage 1 extraction + Stage 2
    attribution: each workspace is its own feature-attribution scope.
    """
    # (1) Stage 0 already did the work — trust it, just guarantee the
    #     single-root invariant for non-monorepos.
    if ctx.monorepo and ctx.workspaces:
        normalised = [_normalise_existing(ws) for ws in ctx.workspaces]
        # Defensive: never hand back an empty split for a monorepo.
        if normalised:
            return normalised

    # (2) Stage 0 didn't populate workspaces (ad-hoc caller / older
    #     context) — detect from tracked files directly.
    info = detect_workspace(str(ctx.repo_path), list(ctx.tracked_files))
    if info.detected and info.packages:
        return _workspaces_from_info(info, ctx)

    # (3) Single-package repo — one workspace == repo root.
    return [_root_workspace(ctx)]


def _root_workspace(ctx: "ScanContext") -> Workspace:
    """The whole repo as a single attribution scope (non-monorepo path)."""
    return Workspace(
        name=ROOT_WORKSPACE_NAME,
        path=ROOT_WORKSPACE_PATH,
        package_json=_read_package_json(ctx.repo_path),
        stack=ctx.stack,
        files=list(ctx.tracked_files),
    )


def _workspaces_from_info(
    info: WorkspaceInfo,
    ctx: "ScanContext",
) -> list[Workspace]:
    """Adapt a :class:`WorkspaceInfo` into pipeline :class:`Workspace` objects.

    The Adapter seam: ``analyzer.workspace`` speaks ``WorkspacePackage``
    (name / path / files); the pipeline speaks ``Workspace`` (adds
    package_json + per-workspace stack). We bridge the two without
    leaking the analyzer type past this module.
    """
    out: list[Workspace] = []
    for pkg in info.packages:
        ws_root = ctx.repo_path / pkg.path
        out.append(
            Workspace(
                name=pkg.name,
                path=pkg.path,
                package_json=_read_package_json(ws_root),
                stack=None,  # per-workspace stack is enriched downstream
                files=list(pkg.files),
            )
        )
    return out


def _normalise_existing(ws: Workspace) -> Workspace:
    """Pass through a Stage-0 :class:`Workspace`, copying mutable fields.

    Keeps the splitter pure — callers can mutate the returned list
    without disturbing the source context.
    """
    return Workspace(
        name=ws.name,
        path=ws.path,
        package_json=dict(ws.package_json) if ws.package_json else None,
        stack=ws.stack,
        files=list(ws.files),
    )


def _read_package_json(root: Path) -> dict[str, object] | None:
    """Best-effort parse of ``<root>/package.json``; ``None`` if absent/bad."""
    manifest = root / "package.json"
    try:
        raw = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def is_monorepo(workspaces: list[Workspace]) -> bool:
    """True when the split produced more than the single root scope.

    A repo is treated as a monorepo for attribution purposes iff the
    splitter returned more than one workspace, OR the one workspace it
    returned is not the implicit root scope.
    """
    if len(workspaces) > 1:
        return True
    if len(workspaces) == 1:
        return workspaces[0].name != ROOT_WORKSPACE_NAME
    return False


__all__ = [
    "ROOT_WORKSPACE_NAME",
    "ROOT_WORKSPACE_PATH",
    "is_monorepo",
    "split_workspaces",
]
