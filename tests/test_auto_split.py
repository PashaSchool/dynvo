"""Tests for ``faultline.aggregators.auto_split.split_oversized_features``
(Sprint 9b).

Splits an oversized non-protected feature whose paths share a common
ancestor and break out into 3+ next-level segment groups.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.aggregators.auto_split import (
    AutoSplitStats,
    split_oversized_features,
)
from faultline.models.types import Feature, FeatureMap, Flow


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _flow(name: str, paths: list[str]) -> Flow:
    return Flow(
        name=name, paths=paths, authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_now(),
        health_score=99.0,
    )


def _feat(
    name: str,
    paths: list[str],
    flows: list[Flow] | None = None,
    *,
    protected: bool = False,
) -> Feature:
    return Feature(
        name=name, paths=paths, flows=flows or [], authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_now(), health_score=99.0, protected=protected,
    )


def _fm(features: list[Feature]) -> FeatureMap:
    return FeatureMap(
        repo_path="/tmp/x", analyzed_at=_now(),
        total_commits=0, date_range_days=365, features=features,
    )


# ── Skipping conditions ──────────────────────────────────────────────


def test_skipped_when_below_min_flows(tmp_path):
    feat = _feat(
        "auth-routes",
        paths=[f"app/(dashboard)/x{i}/page.tsx" for i in range(40)],
        flows=[_flow(f"f{i}", [f"app/(dashboard)/x{i}/page.tsx"])
               for i in range(2)],
    )
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    assert stats == AutoSplitStats()
    assert len(fm.features) == 1


def test_skipped_when_below_min_paths():
    feat = _feat(
        "auth-routes",
        paths=[f"app/(dashboard)/x/page{i}.tsx" for i in range(5)],
        flows=[_flow(f"f{i}", [f"app/(dashboard)/x/page{i}.tsx"])
               for i in range(40)],
    )
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    assert stats == AutoSplitStats()


def test_skipped_when_only_single_segment_under_ancestor():
    """All paths share the SAME next segment → only one group → skip."""
    paths = [f"app/(dashboard)/billing/page{i}.tsx" for i in range(40)]
    flows = [_flow(f"f{i}", [paths[i]]) for i in range(40)]
    feat = _feat("billing", paths=paths, flows=flows)
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    assert stats.features_split == 0


def test_skipped_when_protected():
    paths = [f"app/(dashboard)/seg{i // 10}/page{i}.tsx" for i in range(40)]
    flows = [_flow(f"f{i}", [paths[i]]) for i in range(40)]
    feat = _feat("authenticated", paths=paths, flows=flows, protected=True)
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    assert stats == AutoSplitStats()
    assert fm.features[0].name == "authenticated"


# ── Splitting fires ──────────────────────────────────────────────────


def test_splits_when_three_or_more_segments_above_threshold():
    paths = []
    for seg in ("inbox", "documents", "templates", "settings"):
        paths.extend(f"app/(dashboard)/{seg}/page{i}.tsx" for i in range(10))
    flows = [
        _flow(f"f-{i}", [paths[i]]) for i in range(len(paths))
    ]
    feat = _feat("authenticated-app", paths=paths, flows=flows)
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    assert stats.features_split == 1
    assert stats.new_features == 4
    names = {f.name for f in fm.features}
    # Each child name namespaced under the parent
    assert all(n.startswith("authenticated-app/") for n in names)
    # Display names are humanised Title Case
    displays = {f.display_name for f in fm.features}
    assert {"Inbox", "Documents", "Templates", "Settings"}.issubset(displays)


def test_split_skipped_when_segment_groups_too_small():
    """Each segment must have ``>= min_segment_paths`` (default 4) and
    there must be at least ``min_distinct_segments`` (default 3) such
    groups. Too-small groups shrink the count below the threshold.
    """
    paths = []
    # 30 paths spread across 30 distinct segments — each group has 1
    for i in range(40):
        paths.append(f"app/(dashboard)/seg{i:03d}/page.tsx")
    flows = [_flow(f"f{i}", [paths[i]]) for i in range(40)]
    feat = _feat("authenticated", paths=paths, flows=flows)
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    assert stats.features_split == 0


def test_returns_stats_dataclass_default():
    fm, stats = split_oversized_features(_fm([]))
    assert isinstance(stats, AutoSplitStats)
    assert stats.features_split == 0
    assert stats.new_features == 0
