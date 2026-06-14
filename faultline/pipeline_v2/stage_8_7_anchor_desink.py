"""Stage 8.7 — workspace-anchor de-sink (deterministic).

Why this stage exists
=====================

``PackageAnchorExtractor`` emits one *workspace anchor* per declared
monorepo workspace (``backend``, ``frontend-v2``, ``dify-web``,
``packages/lib`` …) whose ``paths`` are the ENTIRE workspace file tree —
a blunt fallback container so a generically-named package never silently
disappears. Stage 2 then hands that anchor file ownership over the
more-specific route / mvc / schema features that live inside it
(``package`` is the highest source priority), and Stage 6.3 re-expands
those specific features back into the workspace by import-following. So
by Stage 8 the workspace anchor and the specific features *double-claim*
every import-reachable file in the workspace, and the anchor shows up as
a structural blob — one feature owning 40-80 % of the repo (see
``eval/structural_audit``; universal across Soc0 AND infisical).

This stage releases the double-claim: a workspace anchor must not keep a
path that a NON-anchor feature also claims. What remains on the anchor is
the genuine *residual* — files no specific feature reached (e.g. the
dependency-injected service / model long-tail that static
import-following structurally cannot follow). The released files are NOT
lost: they stay attributed to the specific feature that claimed them.
Pure precision — per-feature file-share concentration drops
(``eval/structural_audit``) with no membership recall cost
(``eval/membership``).

Two load-bearing safety rules
=============================

1. **Zero-path protection.** A workspace anchor whose every path is also
   claimed elsewhere is NOT emptied — it keeps its full path-set. An
   empty feature is a downstream ghost (no blame / coverage / flow
   target), and the residual it represents still needs a home until
   DI-aware attribution (a separate lever) can re-home those files.
   Mirrors Stage 2's ``_attribute_paths`` zero-path protection.

2. **Workspace anchors only.** Detection is the ``"workspace anchor"``
   rationale marker (``PackageAnchorExtractor`` workspace mode), never
   the dep-category ``"package anchor"`` marker. A dep-category anchor
   (Billing ← stripe, Auth ← next-auth) is itself a specific capability
   whose import-reachable consumers it legitimately owns; de-sinking it
   would strip real members.

The marker rides ``Feature.description`` (set once at Stage 5 from the
extractor rationale via ``_public_description`` and never overwritten for
developer features — the same signal ``stage_6_3._is_package_anchor``
already depends on in production).

Scale-invariant: no path names, file counts, or tuned ratios
(``rule-no-magic-tuning``, ``rule-no-repo-specific-paths``). Deterministic.
No LLM. No network.

Insertion point is AFTER Stage 8.6 (non-source / phantom drop) so the
feature set is final, and it resyncs the affected product features'
``paths`` itself — the Stage 8.6 reconcile is conditional
(``if nonsource_dropped``), so a naive dev-path prune would otherwise
desync dev vs product paths.

Default ON; disable via ``FAULTLINE_STAGE_8_7_DESINK=0``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import Feature


# ── Detection ────────────────────────────────────────────────────────────────

_WORKSPACE_ANCHOR_MARKER = "workspace anchor"


def _is_workspace_anchor(feature: "Feature") -> bool:
    """``True`` when *feature* is a monorepo workspace anchor.

    The marker is the ``PackageAnchorExtractor`` workspace-mode rationale
    (``"workspace anchor 'backend' from monorepo package 'backend/'"``),
    carried verbatim on ``Feature.description``. Deliberately distinct
    from the dep-category ``"package anchor"`` marker — see module
    docstring rule 2.
    """
    return _WORKSPACE_ANCHOR_MARKER in (feature.description or "").lower()


# Path-keyed attribution surfaces kept consistent with ``paths`` when a
# path is released. ``(attribute_name, file_field)``. The intentionally
# N:M overlay surfaces — ``member_files`` and ``shared_participants`` —
# are NOT pruned: a single file legitimately appears on many features
# there (see the ``Feature`` model docstring). ``getattr`` guards mean a
# surface absent on a given scan is a safe no-op.
_FILE_KEYED_SURFACES: tuple[tuple[str, str], ...] = (
    ("shared_attributions", "file_path"),
    ("symbol_attributions", "file"),
    ("hotspot_files", "path"),
    ("hotspot_files_detail", "path"),
    ("participants", "path"),
)


# ── Result / telemetry ───────────────────────────────────────────────────────


@dataclass
class DesinkResult:
    """Per-scan de-sink outcome, captured for the stage artifact."""

    enabled: bool = True
    anchors_total: int = 0          # workspace-anchor features seen
    anchors_desunk: int = 0         # anchors that released ≥1 path
    anchors_protected: int = 0      # anchors fully-claimed → kept whole
    paths_removed: int = 0          # total paths released across anchors
    product_features_resynced: int = 0
    affected_pf_ids: set[str] = field(default_factory=set)
    desunk_sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "anchors_total": self.anchors_total,
            "anchors_desunk": self.anchors_desunk,
            "anchors_protected": self.anchors_protected,
            "paths_removed": self.paths_removed,
            "product_features_resynced": self.product_features_resynced,
            "desunk_sample": list(self.desunk_sample[:20]),
        }


def _is_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_STAGE_8_7_DESINK=0``."""
    return os.environ.get("FAULTLINE_STAGE_8_7_DESINK", "1") != "0"


# ── Core ─────────────────────────────────────────────────────────────────────


def _prune_surfaces(feature: "Feature", removed: set[str]) -> None:
    """Drop every path-keyed attribution entry whose file was released."""
    for attr, file_field in _FILE_KEYED_SURFACES:
        items = getattr(feature, attr, None)
        if not items:
            continue
        kept = [it for it in items if getattr(it, file_field, None) not in removed]
        if len(kept) != len(items):
            setattr(feature, attr, kept)


def desink_workspace_anchors(
    features: list["Feature"],
    product_features: list["Feature"] | None = None,
) -> DesinkResult:
    """Release double-claimed paths from workspace-anchor features.

    Mutates the affected developer features in place (``paths`` plus the
    path-keyed attribution surfaces) and, when ``product_features`` is
    given, resyncs the ``paths`` of every product feature that owns a
    de-sunk anchor. Returns a :class:`DesinkResult` for telemetry.
    """
    result = DesinkResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    anchors = [f for f in features if _is_workspace_anchor(f)]
    result.anchors_total = len(anchors)
    if not anchors:
        return result

    # Union of paths claimed by every NON-anchor (specific) feature.
    specific_paths: set[str] = set()
    for f in features:
        if _is_workspace_anchor(f):
            continue
        specific_paths.update(f.paths)
    if not specific_paths:
        return result

    for anchor in anchors:
        original = list(anchor.paths)
        if not original:
            continue
        kept = [p for p in original if p not in specific_paths]
        if len(kept) == len(original):
            continue  # nothing this anchor holds is claimed elsewhere
        if not kept:
            # Zero-path protection — keep the anchor whole rather than
            # orphan it (rule 1).
            result.anchors_protected += 1
            continue
        removed = set(original) - set(kept)
        anchor.paths = kept
        _prune_surfaces(anchor, removed)
        result.anchors_desunk += 1
        result.paths_removed += len(removed)
        pid = getattr(anchor, "product_feature_id", None)
        if pid:
            result.affected_pf_ids.add(pid)
        if len(result.desunk_sample) < 20:
            result.desunk_sample.append({
                "feature": anchor.name,
                "removed": len(removed),
                "kept": len(kept),
            })

    if product_features and result.affected_pf_ids:
        result.product_features_resynced = _resync_product_feature_paths(
            features, product_features, result.affected_pf_ids,
        )

    return result


def _resync_product_feature_paths(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    affected_pf_ids: set[str],
) -> int:
    """Recompute ``paths`` for product features that own a de-sunk anchor.

    A product feature's ``paths`` are the union of its member developer
    features' ``paths``; after de-sink that union shrank. We recompute
    ONLY the affected product features (those naming a de-sunk anchor as
    a member) — surgical so unaffected product features are byte-stable.
    Never drops a product feature: de-sink removes paths, never members,
    so no product feature can lose all its members here.

    Returns the number of product features whose ``paths`` changed.
    """
    members_by_pf: dict[str, list["Feature"]] = defaultdict(list)
    for f in developer_features:
        pid = getattr(f, "product_feature_id", None)
        if pid:
            members_by_pf[pid].append(f)

    resynced = 0
    for pf in product_features:
        if pf.name not in affected_pf_ids:
            continue
        merged: list[str] = []
        seen: set[str] = set()
        for m in members_by_pf.get(pf.name, ()):
            for p in m.paths:
                if p not in seen:
                    merged.append(p)
                    seen.add(p)
        if list(pf.paths) != merged:
            pf.paths = merged
            resynced += 1
    return resynced


__all__ = [
    "DesinkResult",
    "desink_workspace_anchors",
]
