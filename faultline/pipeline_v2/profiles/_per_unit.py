"""Per-scan-unit profile selection (StackProfile Phase B+).

:func:`select_scan_profile` is the orchestrator's ONE entry point for
profile selection. It refines the existing whole-repo winner with the
Stage 0.6b partition:

  1. Select the whole-repo winner exactly as before
     (:meth:`ProfileRegistry.select` — highest ``detects``, G1
     lexicographic tie-break).
  2. Run the deterministic partition planner
     (:func:`~faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo`)
     — pure, no artifacts. No units (single-package repo, library
     monorepo, non-monorepo) → return the whole-repo winner: the
     byte-for-byte G4 path.
  3. Select a profile PER UNIT against a unit-scoped context
     (:func:`~faultline.pipeline_v2.unit_scope.unit_scoped_ctx`) —
     same registry, same G1 determinism. Unrecognised units get the
     :class:`DefaultProfile` like any other context.
  4. Units whose selection equals the whole-repo winner add nothing
     (the winner already serves them). If EVERY unit agrees → return
     the whole-repo winner unchanged (uniform monorepos — formbricks,
     dub — stay byte-identical).
  5. Otherwise build a
     :class:`~faultline.pipeline_v2.profiles._composite.CompositeProfile`
     with a FRESH profile instance per differing unit (single-slot
     index memos inside concrete profiles must not thrash across
     scopes) and the whole-repo winner serving the residue.

Deterministic — NO LLM, NO network; unit order sorted by subpath.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from faultline.pipeline_v2.profiles._composite import CompositeProfile
from faultline.pipeline_v2.profiles._registry import ProfileRegistry
from faultline.pipeline_v2.profiles.base import FrameworkProfile
from faultline.pipeline_v2.unit_scope import (
    scan_unit_subpaths,
    unit_scoped_ctx,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# Unit derivation (partition units + colocated single-workspace
# fallback, split-fullstack exclusion) lives in
# :func:`faultline.pipeline_v2.unit_scope.scan_unit_subpaths` — shared
# with the per-unit ``repo_class`` refinement so both consumers see the
# SAME unit list.


def _fresh_instance(profile: FrameworkProfile) -> FrameworkProfile:
    """A fresh same-class instance for a unit binding (memo isolation).

    Concrete profiles are no-arg constructible; if one is not,
    fall back to the shared instance (correct, just slower — the
    single-slot memo rebuilds on scope alternation).
    """
    try:
        candidate = type(profile)()
    except Exception:  # noqa: BLE001 — defensive
        return profile
    return candidate if isinstance(candidate, FrameworkProfile) else profile


def select_scan_profile(
    ctx: "ScanContext",
    profiles: list[FrameworkProfile] | None = None,
) -> FrameworkProfile:
    """Whole-repo selection refined per scan unit (see module docstring).

    Pass ``profiles`` to select within a fixed set (tests / injection);
    omit it to discover via entry-points + built-ins.
    """
    registry = ProfileRegistry(profiles)
    root = registry.select(ctx)

    subpaths = scan_unit_subpaths(ctx)
    if not subpaths:
        return root

    differing: list[tuple[str, FrameworkProfile]] = []
    assignments: list[tuple[str, str]] = []
    for subpath in subpaths:
        try:
            scoped = unit_scoped_ctx(ctx, subpath)
            unit_profile = (
                registry.select(scoped) if scoped.tracked_files else root
            )
        except Exception as exc:  # noqa: BLE001 — selection must never fail
            logger.warning(
                "per-unit selection failed for unit %s (%s); "
                "falling back to whole-repo winner",
                subpath, exc,
            )
            unit_profile = root
        assignments.append((subpath, getattr(unit_profile, "name", "default")))
        if getattr(unit_profile, "name", "default") != getattr(
            root, "name", "default",
        ):
            differing.append((subpath, _fresh_instance(unit_profile)))

    logger.info(
        "per-unit profile selection: root=%s units=%s",
        getattr(root, "name", "default"), assignments,
    )
    if not differing:
        return root
    return CompositeProfile(root=root, units=tuple(differing))


__all__ = ["select_scan_profile"]
