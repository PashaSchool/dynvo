"""Stage 8.6.5 — shared-scaffold filter (deterministic).

Why this stage exists
=====================

Shared workspace scaffold — ``packages/lib``, ``packages/ui``, ``i18n``,
top-level ``utils`` / ``hooks`` / shared ``components`` — leaks into *specific*
features' primary ``paths`` via Stage 6.3 import-tree expansion, which (unlike
Stage 2.6's closure) has NO fan-in guard: every feature whose code imports a
shared ``format-date.ts`` gets it added to its own ``paths``. The result is a
precision leak — a util imported by 20 features sits as a primary member of all
20, none of which it actually *belongs* to.

This stage removes a file from a specific feature's primary ``paths`` when it is
BOTH:

  1. **under a structural scaffold directory** (a universal vocabulary of
     scaffold dir names — ``lib`` / ``ui`` / ``utils`` / ``i18n`` / ``hooks`` /
     ``components`` / …, never a repo-specific path), AND
  2. **claimed by ``>= max(3, P90)`` specific features** — top-decile fan-in of
     the repo's OWN distribution, with a structural floor of 3 (the same
     scale-invariant cap as Stage 2.6).

Both conditions are required. The location restriction is load-bearing: it keeps
high-fan-in *domain* files (a shared ``models/user`` or ``services/auth`` that a
handful of sibling features legitimately share) as primary members — which is
exactly what protects recall. Validated on the membership corpus: precision rises
(inbox-zero micro +10pp) with recall flat-or-UP on documenso AND inbox-zero.

The removed file is NOT lost: this stage runs BEFORE Stage 8.7 de-sink, so the
file stays on its workspace anchor as honest residual, and Stage 8.8 surfaces it
as a ``role="shared"`` member on every feature that imports it. ``member_files``
are left untouched here (the N:M ledger legitimately keeps shared claims).

Scale-invariant (percentile + structural vocabulary, no path/count tuning).
Deterministic. No LLM. Default ON; disable via
``FAULTLINE_STAGE_8_6_5_SCAFFOLD_FILTER=0``.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.stage_2_6_membership_closure import _nearest_rank
from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    _is_workspace_anchor,
    _prune_surfaces,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature


# Universal scaffold directory vocabulary — structural container/scaffold words,
# never product domains. A file is scaffold-located when any path SEGMENT is one
# of these (matched on a `/`-bounded token). Mirrors the spirit of the existing
# ``_CONTAINER_NAMES`` / ``_PHANTOM_CLUSTER_NAMES`` vocabularies. Deliberately
# excludes domain layers (``models`` / ``services`` / ``db`` / ``ee``) so a
# high-fan-in domain file legitimately shared by a few sibling features is kept
# (this is what protects recall — see module docstring).
_SCAFFOLD_SEGMENTS: frozenset[str] = frozenset({
    "lib", "libs", "ui", "i18n", "intl", "locale", "locales",
    "util", "utils", "helper", "helpers", "style", "styles",
    "constant", "constants", "type", "types", "hook", "hooks",
    "component", "components",
})

_SCAFFOLD_RE = re.compile(
    r"(?:^|/)(" + "|".join(sorted(_SCAFFOLD_SEGMENTS)) + r")(?:/|$)",
    re.IGNORECASE,
)

# Top-decile fan-in: a shared file is one claimed by at least this percentile of
# the per-file claim-count distribution (with the structural floor below).
_FAN_IN_PCT = 0.90
_FAN_IN_FLOOR = 3


def _is_scaffold_path(path: str) -> bool:
    return bool(_SCAFFOLD_RE.search(path))


@dataclass
class ScaffoldFilterResult:
    enabled: bool = True
    fan_in_threshold: int = 0
    shared_scaffold_files: int = 0     # distinct files demoted
    paths_removed: int = 0             # total (feature, path) demotions
    features_trimmed: int = 0
    sample: list[str] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "fan_in_threshold": self.fan_in_threshold,
            "shared_scaffold_files": self.shared_scaffold_files,
            "paths_removed": self.paths_removed,
            "features_trimmed": self.features_trimmed,
            "sample": list(self.sample[:20]),
        }


def _is_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_8_6_5_SCAFFOLD_FILTER", "1") != "0"


def filter_shared_scaffold(features: list["Feature"]) -> ScaffoldFilterResult:
    """Demote high-fan-in scaffold files from specific features' primary paths.

    Mutates trimmed specific features in place (``paths`` + path-keyed
    attribution surfaces). Workspace anchors are untouched (the file stays on
    the anchor as residual). Returns a :class:`ScaffoldFilterResult`.
    """
    result = ScaffoldFilterResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    specifics = [f for f in features if not _is_workspace_anchor(f)]
    if not specifics:
        return result

    # fan-in over the primary paths of specific features only.
    fan_in: Counter[str] = Counter()
    for f in specifics:
        for p in f.paths:
            fan_in[p] += 1
    if not fan_in:
        return result

    threshold = max(_FAN_IN_FLOOR, _nearest_rank(sorted(fan_in.values()), _FAN_IN_PCT))
    result.fan_in_threshold = threshold

    shared = {
        p for p, c in fan_in.items()
        if c >= threshold and _is_scaffold_path(p)
    }
    if not shared:
        return result
    result.shared_scaffold_files = len(shared)
    result.sample = sorted(shared)[:20]

    for f in specifics:
        original = f.paths
        kept = [p for p in original if p not in shared]
        if len(kept) == len(original):
            continue
        removed = set(original) - set(kept)
        f.paths = kept
        _prune_surfaces(f, removed)
        result.paths_removed += len(removed)
        result.features_trimmed += 1

    return result


__all__ = [
    "ScaffoldFilterResult",
    "filter_shared_scaffold",
]
