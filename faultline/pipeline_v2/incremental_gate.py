"""Incremental LLM gating — restrict the expensive LLM stages to the
files a ``--since`` diff actually touched.

Problem
-------
Before this module, the ``scan-v2 --since`` path ran the WHOLE pipeline
(including the per-feature Haiku call in Stage 3 and the per-cluster
Haiku call in Stage 4) over every file in the repo, then carried forward
ONLY Stage 6 metrics for untouched features (see
``incremental.carry_forward_metrics``). Net effect: a PR-sized
incremental scan cost the same ~$0.24 as a full cold scan — the LLM bill
was identical because the call count was identical. See
``finding-incremental-no-llm-savings`` (2026-05-25).

Fix (Option A from the finding)
-------------------------------
This module supplies the pure functions that let ``run.py`` GATE the
LLM stages by the changed-file set, on the ``--since`` path ONLY:

  1. ``compute_changed_set`` — the changed-file set for this diff,
     computed EARLY (right after deterministic Stage 2), before any
     LLM stage runs.
  2. ``partition_features`` — split the Stage-2 deterministic features
     into ``touched`` (any path in the changed set) and ``untouched``.
     Stage 3 (flows) then runs over ``touched`` only.
  3. ``filter_unattributed`` — Stage 4 (residual LLM fallback) runs over
     the intersection of the unattributed paths and the changed set.
  4. ``rehydrate_untouched_features`` — rebuild fully-formed
     :class:`~faultline.models.types.Feature` objects (with their flows
     and metrics already attached) from the BASE scan for every
     untouched Stage-2 feature, so the final output is complete. These
     spliced features skipped Stages 3/4 entirely — that is the cost
     saving.

Why this is cold-scan safe
--------------------------
NONE of these functions run on a full / cold scan. ``run.py`` guards the
whole gating branch behind ``not is_full_scan and base_scan_dict is not
None``. A full scan (no ``--since``) takes the existing whole-repo code
path unchanged, so the cold-scan principle (``rule-cold-scan``: no priors
leak into a fresh X-ray) is preserved. Only the explicit ``--since`` +
``--base-scan-path`` branch may reuse base-scan features, which is
exactly what an incremental refresh is *for*.

Why the matching is deterministic (no LLM, no magic numbers)
------------------------------------------------------------
Stages 0/1/2 are fully deterministic. For code that did NOT change, the
extractors emit the SAME feature ``name`` and the SAME ``paths`` on the
fresh run as in the base scan. So we match an untouched Stage-2 feature
to its base counterpart by ``name`` — a stable, structural key. No
similarity threshold, no per-repo tuning (``rule-no-magic-tuning``), no
hardcoded paths (``rule-no-repo-specific-paths``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.incremental import changed_files_since

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

logger = logging.getLogger(__name__)


def compute_changed_set(
    repo_path: Path | str, since: str, base_scan: dict[str, Any],
) -> set[str]:
    """Return the set of repo-relative files changed since ``since``.

    Thin wrapper over :func:`incremental.changed_files_since` that
    returns a ``set`` (membership tests dominate the gating loops). The
    ``base_scan`` argument is accepted for symmetry / future use (e.g.
    augmenting the diff with files renamed between scans) but the
    changed set itself comes purely from git — never from the base scan,
    so a stale base can never *hide* a changed file.
    """
    return set(changed_files_since(Path(repo_path), since))


@dataclass
class FeaturePartition:
    """Result of splitting Stage-2 features by the changed-file set.

    Attributes:
        touched: Stage-2 features with at least one path in the changed
            set. These flow into Stage 3 (and onward) normally.
        untouched: Stage-2 features with NO changed path. These skip
            Stage 3/4 (the LLM stages) and are re-hydrated from the base
            scan instead.
        touched_names: convenience set of ``touched`` feature names.
    """

    touched: list[DeveloperFeature]
    untouched: list[DeveloperFeature]
    touched_names: set[str] = field(default_factory=set)


def partition_features(
    features: list[DeveloperFeature], changed_files: set[str],
) -> FeaturePartition:
    """Split Stage-2 deterministic features into touched / untouched.

    A feature is ``touched`` iff ANY of its ``paths`` is in
    ``changed_files``. Untouched features carry no changed code, so
    re-running the per-feature Stage 3 Haiku call on them would spend
    money to (deterministically) reproduce the base scan's flows.

    Pure function — does not mutate ``features``.
    """
    touched: list[DeveloperFeature] = []
    untouched: list[DeveloperFeature] = []
    for f in features:
        if any(p in changed_files for p in f.paths):
            touched.append(f)
        else:
            untouched.append(f)
    return FeaturePartition(
        touched=touched,
        untouched=untouched,
        touched_names={f.name for f in touched},
    )


def filter_unattributed(
    unattributed: list[str], changed_files: set[str],
) -> list[str]:
    """Restrict the Stage-4 residual input to changed files only.

    Stage 4 makes one Haiku call per residual cluster. Unattributed
    files that did not change can keep whatever the base scan said about
    them (they are re-hydrated via the untouched-feature path), so they
    must NOT seed fresh residual clusters here. Order-preserving.
    """
    cs = changed_files
    return [p for p in unattributed if p in cs]


def base_features_by_name(base_scan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the base scan's developer features by ``name``.

    Reads ``developer_features`` (preferred) or the legacy ``features``
    alias. Features without a ``name`` are skipped. On a duplicate name
    the FIRST occurrence wins (Stage 5 slugify already de-collides names
    with ``-2`` suffixes, so duplicates are not expected).
    """
    base_feats = (
        base_scan.get("developer_features")
        or base_scan.get("features")
        or []
    )
    out: dict[str, dict[str, Any]] = {}
    for bf in base_feats:
        name = bf.get("name")
        if not name or name in out:
            continue
        # Only Layer-1 developer features are re-hydratable here; Layer-2
        # product features are rebuilt downstream by Stage 8.
        if bf.get("layer", "developer") != "developer":
            continue
        out[str(name)] = bf
    return out


@dataclass
class RehydrateResult:
    """Outcome of re-hydrating untouched features from the base scan.

    Attributes:
        features: fully-formed :class:`Feature` objects spliced back in.
        rehydrated_names: names successfully matched to the base scan.
        missing_names: untouched Stage-2 names with NO base match. These
            are re-scanned the normal way (they fall back through to
            Stage 3) so nothing is silently dropped.
    """

    features: list[Feature]
    rehydrated_names: list[str]
    missing_names: list[str]


def rehydrate_untouched_features(
    untouched: list[DeveloperFeature],
    base_scan: dict[str, Any],
) -> RehydrateResult:
    """Rebuild :class:`Feature` objects for untouched features from base.

    For each untouched Stage-2 feature, look up its base-scan twin by
    ``name`` and re-validate the base dict into a :class:`Feature`
    (carrying its flows, metrics, participants, attributions, uuid —
    everything Stages 3-6 would have produced). These features skipped
    the LLM stages entirely; this is where the saving is realised.

    A Stage-2 feature with no base match is reported in
    ``missing_names`` so the caller can route it back through the normal
    (LLM) path rather than dropping it. This is the silent-drop guard.
    """
    from faultline.models.types import Feature  # local import: avoid cycle

    by_name = base_features_by_name(base_scan)
    rehydrated: list[Feature] = []
    rehydrated_names: list[str] = []
    missing_names: list[str] = []
    for f in untouched:
        bf = by_name.get(f.name)
        if bf is None:
            missing_names.append(f.name)
            continue
        try:
            feat = Feature.model_validate(bf)
        except Exception as exc:  # noqa: BLE001 — base scan is external input
            logger.warning(
                "incremental_gate: could not rehydrate base feature %r "
                "(%s) — re-scanning it the normal way",
                f.name, exc,
            )
            missing_names.append(f.name)
            continue
        rehydrated.append(feat)
        rehydrated_names.append(f.name)
    return RehydrateResult(
        features=rehydrated,
        rehydrated_names=rehydrated_names,
        missing_names=missing_names,
    )


__all__ = [
    "FeaturePartition",
    "RehydrateResult",
    "base_features_by_name",
    "compute_changed_set",
    "filter_unattributed",
    "partition_features",
    "rehydrate_untouched_features",
]
