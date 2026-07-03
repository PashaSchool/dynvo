"""Framework Knowledge Layer (Phase 1).

Sits between Stage 0 (intake) and Stage 1 (extraction). Provides:

  * :class:`FrameworkProfile` — the per-framework structural-knowledge
    contract the pipeline depends on (DIP/OCP/LSP).
  * :class:`DefaultProfile` — null-object profile so unknown stacks
    keep working unchanged.
  * :class:`ProfileRegistry` / :func:`discover_profiles` /
    :func:`select_profile` — entry-point-based discovery + selection.
  * :func:`split_workspaces` — the monorepo workspace-splitter (the
    single biggest blob lever); returns ``[root]`` for single-package
    repos, preserving the non-monorepo path.

All deterministic — NO LLM, NO network.
"""

from __future__ import annotations

from faultline.pipeline_v2.profiles._composite import CompositeProfile
from faultline.pipeline_v2.profiles._per_unit import select_scan_profile
from faultline.pipeline_v2.profiles._registry import (
    ProfileRegistry,
    discover_profiles,
    select_profile,
)
from faultline.pipeline_v2.profiles._splitter import (
    ROOT_WORKSPACE_NAME,
    ROOT_WORKSPACE_PATH,
    is_monorepo,
    split_workspaces,
)
from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
    FrameworkProfile,
)
from faultline.pipeline_v2.profiles.default import DefaultProfile

__all__ = [
    "ROOT_WORKSPACE_NAME",
    "ROOT_WORKSPACE_PATH",
    "AttributionSpec",
    "CompositeProfile",
    "DefaultProfile",
    "FileRole",
    "FlowEntry",
    "FrameworkProfile",
    "ProfileRegistry",
    "discover_profiles",
    "is_monorepo",
    "select_profile",
    "select_scan_profile",
    "split_workspaces",
]
