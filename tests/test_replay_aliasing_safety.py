"""Sprint 10a aliasing-safety tests for the replay registry.

These tests pin the contract:
  - ``run_stage(..., isolate=True)`` MUST NOT mutate caller's input
  - ``run_chain(...)`` MUST NOT mutate caller's input
  - The returned FeatureMap is a NEW object — modifying it doesn't
    affect the input
  - Feature objects on the output are NOT shared with the input

The audit (Sprint 9f) flagged silent aliasing as the #1 refactor
risk. These tests catch it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, FeatureMap
from faultline.replay import StageContext, run_chain, run_stage


def _make_fm(features=None):
    """Build a tiny FeatureMap. Features default to one ``auth`` slug
    with a tRPC router path so feature-protection fires.
    """
    if features is None:
        features = [
            Feature(
                name="auth", display_name="Auth",
                paths=["packages/trpc/server/auth-router/router.ts"],
                authors=[], total_commits=5, bug_fixes=0,
                bug_fix_ratio=0.0,
                last_modified=datetime.now(tz=timezone.utc),
                health_score=95.0, flows=[],
            ),
        ]
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=features,
    )


# ── run_stage isolation ─────────────────────────────────────────────


def test_run_stage_does_not_mutate_input_when_isolated():
    """Default isolate=True: caller's FeatureMap is untouched."""
    fm_in = _make_fm()
    assert fm_in.features[0].protected is False
    out = run_stage("feature-protection", fm_in)
    # output has the mutation
    assert out.features[0].protected is True
    # input does NOT
    assert fm_in.features[0].protected is False


def test_run_stage_returns_new_object_not_input():
    fm_in = _make_fm()
    out = run_stage("feature-protection", fm_in)
    assert out is not fm_in


def test_run_stage_does_not_share_feature_objects_with_input():
    fm_in = _make_fm()
    out = run_stage("feature-protection", fm_in)
    # Feature objects on output are NEW — not the same Python objects.
    in_feat = fm_in.features[0]
    out_feat = out.features[0]
    assert in_feat is not out_feat


def test_run_stage_isolate_false_skips_boundary_deep_copy():
    """Sprint 10a — with full per-stage purity, stages internally
    deep-copy regardless of isolate=. The isolate flag now controls
    only the BOUNDARY copy at run_stage entry. So isolate=False
    means the stage receives the input by reference and the stage's
    internal pure-function copy takes over. Output is still a fresh
    object; input is still untouched (because internal stage is pure).
    """
    fm_in = _make_fm()
    out = run_stage("feature-protection", fm_in, isolate=False)
    # Input never mutated regardless of isolate, because stage is pure
    assert fm_in.features[0].protected is False
    assert out.features[0].protected is True


# ── run_chain isolation ─────────────────────────────────────────────


def test_run_chain_does_not_mutate_input():
    fm_in = _make_fm()
    out = run_chain(["feature-protection"], fm_in)
    assert fm_in.features[0].protected is False
    assert out.features[0].protected is True


def test_run_chain_multi_stage_does_not_leak_intermediate_mutation():
    """A two-stage chain with dedup should produce a clean output
    while the input remains pristine.
    """
    fm_in = _make_fm([
        Feature(
            name="auth", display_name="Auth",
            paths=["a.ts"], authors=[], total_commits=5, bug_fixes=0,
            bug_fix_ratio=0.0,
            last_modified=datetime.now(tz=timezone.utc),
            health_score=95.0, flows=[],
        ),
        Feature(
            name="auth", display_name="Auth",
            paths=["b.ts"], authors=[], total_commits=5, bug_fixes=0,
            bug_fix_ratio=0.0,
            last_modified=datetime.now(tz=timezone.utc),
            health_score=95.0, flows=[],
        ),
    ])
    in_feature_count = len(fm_in.features)
    out = run_chain(["feature-dedup"], fm_in)
    # Output has merged the two auth features
    assert len(out.features) == 1
    # Input is untouched
    assert len(fm_in.features) == in_feature_count


def test_run_chain_branching_from_same_input_produces_independent_outputs():
    """A user can branch off the same input through two different
    chains and the two outputs do NOT share state.
    """
    fm_in = _make_fm()
    out_a = run_chain(["feature-protection"], fm_in)
    out_b = run_chain(["feature-protection"], fm_in)
    # Each output is its own object
    assert out_a is not out_b
    assert out_a.features[0] is not out_b.features[0]
    # Mutating out_a doesn't bleed into out_b
    out_a.features[0].display_name = "MUTATED-A"
    assert out_b.features[0].display_name != "MUTATED-A"
    assert fm_in.features[0].display_name == "Auth"


# ── Feature object identity ─────────────────────────────────────────


def test_input_feature_list_object_is_not_aliased_to_output():
    """The Python list ``features`` on input != on output."""
    fm_in = _make_fm()
    out = run_stage("feature-protection", fm_in)
    assert fm_in.features is not out.features


def test_input_feature_paths_list_not_aliased_to_output():
    """The Python list ``paths`` inside each Feature is independent
    on input vs output — mutating one must not affect the other.
    """
    fm_in = _make_fm()
    out = run_stage("feature-protection", fm_in)
    out.features[0].paths.append("MUTATED.ts")
    assert "MUTATED.ts" not in fm_in.features[0].paths
