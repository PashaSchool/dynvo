"""Sprint 10a — per-stage purity contract tests.

Each pipeline stage is being refactored from in-place mutation
to pure-function semantics: input is NEVER mutated, a NEW
FeatureMap is returned, and Feature objects on the output are
NOT shared with the input.

Tests progress sequentially through the refactor. As each stage
is refactored, the corresponding pair of tests (in-place +
purity) goes from xfail → pass.

Naming convention:
  test_<stage>_input_not_mutated      ← key purity contract
  test_<stage>_returns_new_feature_map
  test_<stage>_features_not_aliased
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, FeatureMap


def _feat(name="auth", paths=None, protected=False, flows=None):
    return Feature(
        name=name, display_name=name.capitalize(),
        paths=paths or ["a.ts"],
        authors=[], total_commits=5, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=95.0,
        flows=flows or [], protected=protected,
    )


def _fm(features=None):
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=features or [],
    )


# ── Stage 1: mark_protected ─────────────────────────────────────────


def test_mark_protected_input_not_mutated():
    """After refactor: input fm must not be mutated."""
    from faultline.aggregators.feature_protection import mark_protected
    fm_in = _fm([_feat("auth", paths=["packages/auth/server/index.ts"])])
    new_fm, reasons = mark_protected(fm_in)
    # Input untouched
    assert fm_in.features[0].protected is False
    assert fm_in.features[0].protection_reason is None
    # Output has the mutation
    assert new_fm.features[0].protected is True
    assert reasons == {"auth": "workspace-package"}


def test_mark_protected_returns_new_feature_map():
    from faultline.aggregators.feature_protection import mark_protected
    fm_in = _fm([_feat("auth", paths=["packages/auth/server/index.ts"])])
    new_fm, _ = mark_protected(fm_in)
    assert new_fm is not fm_in


def test_mark_protected_features_not_aliased():
    from faultline.aggregators.feature_protection import mark_protected
    fm_in = _fm([_feat("auth", paths=["packages/auth/server/index.ts"])])
    new_fm, _ = mark_protected(fm_in)
    new_fm.features[0].paths.append("INJECTED.ts")
    assert "INJECTED.ts" not in fm_in.features[0].paths


# ── Stage 2: strip_page_suffix ──────────────────────────────────────


def test_strip_page_suffix_input_not_mutated():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    f = _feat("dashboard")
    f.display_name = "Dashboard Page"
    fm_in = _fm([f])
    new_fm, changed = strip_page_suffix(fm_in)
    assert fm_in.features[0].display_name == "Dashboard Page"
    assert new_fm.features[0].display_name == "Dashboard"
    assert changed == 1


def test_strip_page_suffix_returns_new_feature_map():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    fm_in = _fm([_feat("a")])
    new_fm, _ = strip_page_suffix(fm_in)
    assert new_fm is not fm_in


# ── Stage 3: scrub_structural_displays ──────────────────────────────


def test_scrub_structural_displays_input_not_mutated():
    from faultline.aggregators.display_name_canonicalizer import scrub_structural_displays
    f = _feat("auth")
    f.display_name = "Trpc"  # structural — should be scrubbed
    fm_in = _fm([f])
    new_fm, scrubbed = scrub_structural_displays(fm_in)
    assert fm_in.features[0].display_name == "Trpc"
    assert new_fm.features[0].display_name is None
    assert scrubbed == 1


# ── Stage 4: split_oversized_features ───────────────────────────────


def test_split_oversized_input_not_mutated():
    from faultline.aggregators.auto_split import split_oversized_features
    from faultline.models.types import Flow

    flows = [
        Flow(
            name=f"f{i}",
            paths=[f"apps/x/routes/billing/page{i}.tsx"],
            authors=[], total_commits=0, bug_fixes=0,
            bug_fix_ratio=0.0,
            last_modified=datetime.now(tz=timezone.utc),
            health_score=95.0,
        ) for i in range(50)
    ]
    paths = (
        [f"apps/x/routes/billing/page{i}.tsx" for i in range(10)]
        + [f"apps/x/routes/dashboard/page{i}.tsx" for i in range(10)]
        + [f"apps/x/routes/settings/page{i}.tsx" for i in range(10)]
    )
    feat = _feat("authenticated", paths=paths, flows=flows)
    fm_in = _fm([feat])
    new_fm, stats = split_oversized_features(fm_in)
    # Input: still 1 feature
    assert len(fm_in.features) == 1
    # Output: 3 features
    assert len(new_fm.features) == 3
    assert stats.features_split == 1


# ── Stage 5: dedup_features ─────────────────────────────────────────


def test_dedup_features_input_not_mutated():
    from faultline.aggregators.feature_dedup import dedup_features
    fm_in = _fm([_feat("auth", paths=["a.ts"]), _feat("auth", paths=["b.ts"])])
    new_fm, stats = dedup_features(fm_in)
    assert len(fm_in.features) == 2
    assert len(new_fm.features) == 1
    assert stats.clusters_merged == 1


# ── Stage 6: FlowReattribution.reattribute ──────────────────────────


def test_flow_reattribution_input_not_mutated():
    from faultline.aggregators.flow_reattribution import FlowReattribution
    from faultline.models.types import Flow
    fl = Flow(
        name="enterprise-billing-flow", paths=[],
        authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=95.0,
    )
    fm_in = _fm([
        _feat("organisation-management", flows=[fl]),
        _feat("ee/enterprise-billing", flows=[]),
    ])
    new_fm, n_moved = FlowReattribution().reattribute(fm_in)
    # Input unchanged
    assert [fl.name for fl in fm_in.features[0].flows] == ["enterprise-billing-flow"]
    assert fm_in.features[1].flows == []
    # Output: flow moved
    assert new_fm.features[0].flows == []
    assert [fl.name for fl in new_fm.features[1].flows] == ["enterprise-billing-flow"]
    assert n_moved == 1
