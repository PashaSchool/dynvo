"""Auto-split oversized features (Sprint 9b).

Backstop for the documenso-style ``Authenticated App Routes`` bucket
where the primary scan groups all routes under a single feature because
they share a common ancestor folder. After route_segment + critique
discover individual features, the original bucket can persist with
dozens of flows.

Rule:
    A feature qualifies for split when ALL of the following hold:
      - At least N flows (default 40)
      - At least M paths (default 30)
      - NOT protected (would fight feature-protection)
      - At least 3 distinct next-level segments under common ancestor
      - Each kept segment has at least K paths (default 4)

When the rule fires, the feature is replaced by N child features named
after each segment (Title Case). Health/commit/bug-ratio inherited.

All thresholds are scale-invariant integer counts — no per-corpus
tuning, no magic floats.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Env-var overrides (Sprint 11b — for scan-experimenter sweeps).
# All values are scale-invariant integer thresholds per
# ``rule-no-magic-tuning``.
import os as _os

DEFAULT_MIN_FLOWS = int(_os.environ.get("FAULTLINE_AUTOSPLIT_MIN_FLOWS", "40"))
DEFAULT_MIN_PATHS = int(_os.environ.get("FAULTLINE_AUTOSPLIT_MIN_PATHS", "30"))
DEFAULT_MIN_SEGMENT_PATHS = int(_os.environ.get("FAULTLINE_AUTOSPLIT_MIN_SEGMENT_PATHS", "4"))
DEFAULT_MIN_DISTINCT_SEGMENTS = int(_os.environ.get("FAULTLINE_AUTOSPLIT_MIN_DISTINCT_SEGMENTS", "3"))


@dataclass
class AutoSplitStats:
    features_split: int = 0
    new_features: int = 0


def _common_ancestor(paths: list[str]) -> str:
    if not paths:
        return ""
    parts_lists = [p.split("/") for p in paths]
    common: list[str] = []
    for cols in zip(*parts_lists):
        first = cols[0]
        if all(c == first for c in cols):
            common.append(first)
        else:
            break
    return "/".join(common)


def _next_segment(path: str, ancestor: str) -> str:
    if not ancestor:
        rel = path
    else:
        rel = path[len(ancestor):].lstrip("/")
    if not rel:
        return ""
    return rel.split("/", 1)[0]


_NOISE_SEGMENT_NAMES = frozenset({
    "index", "_index", "page", "layout", "template", "default",
    "loading", "error", "not-found", "_app", "_document", "head",
    "icon", "favicon", "robots", "sitemap", "manifest", "config",
})


def _humanise(seg: str) -> str:
    s = re.sub(r"^\$\$?", "", seg)
    s = re.sub(r"^\[(.*)\]$", r"\1", s)
    s = re.sub(r"^\(.*\)$", "", s)
    s = s.replace("+", "")
    s = re.sub(r"(.)([A-Z])", r"\1-\2", s).lower()
    parts = re.split(r"[-_/\s.]+", s)
    return " ".join(w.capitalize() for w in parts if w)


def split_oversized_features(
    feature_map,
    *,
    min_flows: int = DEFAULT_MIN_FLOWS,
    min_paths: int = DEFAULT_MIN_PATHS,
    min_segment_paths: int = DEFAULT_MIN_SEGMENT_PATHS,
    min_distinct_segments: int = DEFAULT_MIN_DISTINCT_SEGMENTS,
):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    AutoSplitStats)``. Input ``feature_map`` is NEVER mutated.

    Splits oversized non-protected features by next-level segment.
    """
    new_fm = feature_map.model_copy(deep=True)
    stats = AutoSplitStats()
    keep: list = []
    for feat in list(new_fm.features):
        if feat.protected:
            keep.append(feat)
            continue
        if len(feat.flows) < min_flows or len(feat.paths) < min_paths:
            keep.append(feat)
            continue

        ancestor = _common_ancestor(feat.paths)
        if not ancestor or "/" not in ancestor:
            keep.append(feat)
            continue

        by_seg: dict[str, list[str]] = defaultdict(list)
        for p in feat.paths:
            seg = _next_segment(p, ancestor)
            if not seg or seg in _NOISE_SEGMENT_NAMES:
                continue
            by_seg[seg].append(p)

        big_segs = {s: ps for s, ps in by_seg.items() if len(ps) >= min_segment_paths}
        if len(big_segs) < min_distinct_segments:
            keep.append(feat)
            continue

        logger.info(
            "auto-split: %s (%d flows, %d paths) -> %d segment groups",
            feat.name, len(feat.flows), len(feat.paths), len(big_segs),
        )
        stats.features_split += 1

        flows_by_seg: dict[str, list] = defaultdict(list)
        ungrouped_flows: list = []
        for fl in feat.flows:
            fl_paths = fl.paths or []
            if not fl_paths:
                ungrouped_flows.append(fl)
                continue
            tally: dict[str, int] = defaultdict(int)
            for p in fl_paths:
                seg = _next_segment(p, ancestor)
                if seg in big_segs:
                    tally[seg] += 1
            if tally:
                winner = max(tally, key=lambda s: tally[s])
                flows_by_seg[winner].append(fl)
            else:
                ungrouped_flows.append(fl)

        new_children = []
        for seg, seg_paths in big_segs.items():
            display = _humanise(seg)
            slug = re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")
            # Sprint 9c P1 fix — deep copy so siblings don't share
            # mutable lists (authors, etc.) with the original feature.
            child = feat.model_copy(deep=True)
            child.name = f"{feat.name}/{slug}" if "/" not in feat.name else slug
            child.display_name = display
            child.paths = seg_paths
            child.flows = flows_by_seg.get(seg, [])
            child.protected = False
            child.protection_reason = None
            new_children.append(child)
            keep.append(child)
            stats.new_features += 1

        if ungrouped_flows and new_children:
            new_children[0].flows.extend(ungrouped_flows)

    new_fm.features = keep
    return new_fm, stats


__all__ = ["AutoSplitStats", "split_oversized_features"]
