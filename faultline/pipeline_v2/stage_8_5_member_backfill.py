"""Stage 8.5 — deterministic path-overlap member backfill (additive).

PROBLEM
-------
The Stage 8 analyst payload is capped (``_MAX_DEV_FEATURES_IN_PAYLOAD``)
and sorted by path-count DESC. On large monorepos (e.g. cal.com: 573 dev
features) only the biggest features reach the analyst, so the vast
majority of developer features are *unmapped by construction* — they were
never offered to the analyst for membership at all. The deterministic
turborepo rollup already computes rich path→PF attributions, but those are
not projected back onto ``developer_features[].product_feature_id`` for the
features the analyst never saw.

WHAT THIS STAGE DOES
--------------------
For every developer feature that the analyst left UNMAPPED, compute a
scale-invariant structural overlap against each product feature's claimed
paths and, if the single best overlap clears a majority threshold, assign
that product feature as its parent. Features that don't clear the threshold
stay unmapped — internal / infra features (build config, CI, shared utils,
telemetry, migrations, types, tooling) legitimately belong to no product
feature, and we NEVER force-assign or invent a catch-all bucket.

ADDITIVE GUARANTEE
------------------
This stage runs AFTER the Stage 8 analyst + rollup. It only ever WRITES
``product_feature_id`` (and the matching ``dev_to_product_map`` entry) on
features whose value was previously ``None``. It never:
  - touches the analyst prompt / payload,
  - adds / removes / renames any product feature,
  - re-maps a feature the analyst already mapped.
Because the ``product_features[]`` ARRAY is untouched, Layer-2 product
detection precision/recall (which score the PF name set) are mathematically
invariant — this stage only enriches MEMBERSHIP.

SCALE INVARIANCE
----------------
The signal is a RATIO of the dev feature's own paths:

    overlap_ratio(dev, pf) = |dev.paths ∩ pf.paths| / |dev.paths|

This is a fraction in [0, 1] that behaves identically on a 5-path feature
and a 600-path feature — no absolute counts, no per-repo magic numbers
(see ``rule-no-magic-tuning``). The threshold is a structural MAJORITY
boundary (> half of the feature's files belong to the same product
feature), not a value tuned to one corpus repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol

# Scale-invariant majority threshold. A developer feature is back-filled
# into a product feature only when a strict majority of its OWN files are
# claimed by that single product feature. 0.5 is a structural boundary
# ("most of this feature lives there"), not a tuned constant: the cal.com
# replay sweep (0.3 → 0.8) shows a stable attach plateau around it while
# genuinely-internal features (tsconfig, types, prisma, background-jobs,
# api-proxy, config) remain below it and stay correctly unmapped.
_DEFAULT_OVERLAP_THRESHOLD = 0.5

_ENV_FLAG = "FAULTLINE_STAGE_8_5_BACKFILL"
_ENV_THRESHOLD = "FAULTLINE_STAGE_8_5_THRESHOLD"


class _FeatureLike(Protocol):
    """Structural view of the fields this stage reads/writes.

    Kept as a Protocol so the stage is testable with lightweight fakes and
    does not import the heavy pydantic ``Feature`` model (composition over
    inheritance; structural typing per ``oop-architect``).
    """

    name: str
    paths: list[str]
    product_feature_id: str | None


@dataclass(frozen=True)
class BackfillResult:
    """Outcome of the Stage 8.5 backfill pass."""

    attached: int
    still_unmapped: int
    threshold: float
    attached_pct_before: float
    attached_pct_after: float
    # feature name -> product feature name it was back-filled into
    assignments: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def as_telemetry(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "attached": self.attached,
            "still_unmapped": self.still_unmapped,
            "threshold": self.threshold,
            "attached_pct_before": round(self.attached_pct_before, 4),
            "attached_pct_after": round(self.attached_pct_after, 4),
        }


def _is_enabled() -> bool:
    """Default ON; disabled via ``FAULTLINE_STAGE_8_5_BACKFILL=0``."""
    raw = os.environ.get(_ENV_FLAG)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _resolve_threshold(override: float | None) -> float:
    if override is not None:
        return override
    raw = os.environ.get(_ENV_THRESHOLD)
    if raw:
        try:
            val = float(raw)
            if 0.0 < val <= 1.0:
                return val
        except ValueError:
            pass
    return _DEFAULT_OVERLAP_THRESHOLD


def overlap_ratio(dev_paths: Iterable[str], pf_paths: set[str]) -> float:
    """Fraction of the dev feature's OWN paths claimed by ``pf_paths``.

    Scale-invariant: a ratio in [0, 1] independent of repo size. Returns
    0.0 for a feature with no paths (nothing to attribute).
    """
    dp = {p for p in dev_paths if p}
    if not dp:
        return 0.0
    return len(dp & pf_paths) / len(dp)


def _best_product_feature(
    dev_paths: list[str],
    pf_paths_by_name: Mapping[str, set[str]],
    threshold: float,
) -> str | None:
    """Return the single best-overlapping PF name above ``threshold``.

    Ties (equal ratios) resolve deterministically by PF name to keep the
    stage reproducible across runs. Returns ``None`` when nothing clears
    the threshold — the feature stays UNMAPPED.
    """
    best_name: str | None = None
    best_ratio = 0.0
    for pf_name in sorted(pf_paths_by_name):  # deterministic tie-break
        ratio = overlap_ratio(dev_paths, pf_paths_by_name[pf_name])
        if ratio > best_ratio:
            best_ratio = ratio
            best_name = pf_name
    if best_name is not None and best_ratio >= threshold:
        return best_name
    return None


def run_stage_8_5_backfill(
    features: list[_FeatureLike],
    product_features: list[_FeatureLike],
    dev_to_product_map: dict[str, list[str]] | None = None,
    *,
    threshold: float | None = None,
    enabled: bool | None = None,
) -> BackfillResult:
    """Back-fill ``product_feature_id`` on analyst-unmapped dev features.

    Mutates ``features`` in place (sets ``product_feature_id``) and, when
    provided, updates ``dev_to_product_map`` so downstream consumers that
    read the map stay consistent. Returns telemetry.
    """
    is_on = _is_enabled() if enabled is None else enabled
    total = len(features)
    mapped_before = sum(1 for f in features if f.product_feature_id)
    pct_before = (mapped_before / total) if total else 0.0
    thr = _resolve_threshold(threshold)

    if not is_on or total == 0 or not product_features:
        return BackfillResult(
            attached=0,
            still_unmapped=total - mapped_before,
            threshold=thr,
            attached_pct_before=pct_before,
            attached_pct_after=pct_before,
            enabled=is_on,
        )

    # Reuse the rollup's path attributions: each product feature's ``paths``
    # IS the rollup-attributed file set. No recomputation from scratch.
    pf_paths_by_name: dict[str, set[str]] = {
        pf.name: {p for p in (pf.paths or []) if p} for pf in product_features
    }
    # Drop product features with no paths — they can never overlap.
    pf_paths_by_name = {k: v for k, v in pf_paths_by_name.items() if v}

    assignments: dict[str, str] = {}
    for f in features:
        if f.product_feature_id:
            continue  # never re-map what the analyst already mapped
        best = _best_product_feature(f.paths or [], pf_paths_by_name, thr)
        if best is None:
            continue  # below threshold → legitimately stays unmapped
        f.product_feature_id = best
        assignments[f.name] = best
        if dev_to_product_map is not None:
            dev_to_product_map[f.name] = [best]

    mapped_after = sum(1 for f in features if f.product_feature_id)
    return BackfillResult(
        attached=len(assignments),
        still_unmapped=total - mapped_after,
        threshold=thr,
        attached_pct_before=pct_before,
        attached_pct_after=(mapped_after / total) if total else 0.0,
        assignments=assignments,
        enabled=is_on,
    )
