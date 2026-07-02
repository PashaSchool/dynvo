"""Tests for Stage 5.4 — cross-feature entry-point flow dedup.

Covers: owner-of-entry wins; most-owned tie-break; lexicographic final
tie-break; flows without entry untouched; single-claimant untouched;
feature paths/membership untouched; env opt-out; determinism.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_5_4_cross_flow_dedup import (
    dedup_cross_feature_flows,
)

_ENV = "FAULTLINE_STAGE_5_4_CROSS_FLOW_DEDUP"

import pytest


@pytest.fixture(autouse=True)
def _enable_stage(monkeypatch):
    """Stage is default-OFF (F1 cost, 2026-07-02) — tests exercise the
    opt-in behavior."""
    monkeypatch.setenv(_ENV, "1")


def _flow(name, entry_file=None, entry_line=None, paths=None):
    return Flow(
        name=name, uuid=name, paths=paths or ["src/a.ts"], authors=[],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=90.0, entry_point_file=entry_file,
        entry_point_line=entry_line,
    )


def _feat(name, paths, flows):
    return Feature(
        name=name, paths=paths, authors=[], total_commits=3, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=90.0, layer="developer", flows=flows,
    )


def test_owner_of_entry_wins():
    """The feature whose paths contain the entry file keeps its flow."""
    owner = _feat("array", ["utils/array.ts"],
                  [_flow("group-data-flow", "utils/array.ts", 19,
                         ["utils/array.ts"])])
    claimant = _feat("shared-state", ["store/state.ts"],
                     [_flow("sort-and-group-flow", "utils/array.ts", 19,
                            ["utils/array.ts", "store/state.ts"])])
    res = dedup_cross_feature_flows([claimant, owner])  # claimant FIRST
    assert res.flows_removed == 1
    assert [f.name for f in owner.flows] == ["group-data-flow"]
    # Audit fix: the loser's paths are UNIONED into the winner so Stage 5.5
    # recovers the cross-feature (secondary/blast-radius) signal.
    assert set(owner.flows[0].paths) == {"utils/array.ts", "store/state.ts"}
    assert claimant.flows == []
    assert claimant.paths == ["store/state.ts"]  # membership untouched


def test_most_owned_tiebreak_when_no_owner():
    """Neither owns the entry file → feature owning more of the flow's
    paths wins."""
    a = _feat("alpha", ["a/x.ts", "a/y.ts"],
              [_flow("alpha-flow", "shared/util.ts", 5,
                     ["a/x.ts", "a/y.ts", "shared/util.ts"])])
    b = _feat("beta", ["b/z.ts"],
              [_flow("beta-flow", "shared/util.ts", 5,
                     ["b/z.ts", "shared/util.ts"])])
    res = dedup_cross_feature_flows([b, a])
    assert res.flows_removed == 1
    assert [f.name for f in a.flows] == ["alpha-flow"]  # owns 2 of its paths
    assert b.flows == []


def test_lexicographic_final_tiebreak():
    a = _feat("aaa", ["a.ts"], [_flow("f1", "shared.ts", 1, ["shared.ts"])])
    b = _feat("bbb", ["b.ts"], [_flow("f2", "shared.ts", 1, ["shared.ts"])])
    res = dedup_cross_feature_flows([b, a])
    assert res.flows_removed == 1
    assert [f.name for f in a.flows] == ["f1"]  # "aaa" < "bbb"


def test_no_entry_untouched():
    a = _feat("a", ["a.ts"], [_flow("f1"), _flow("f2")])
    res = dedup_cross_feature_flows([a])
    assert res.flows_removed == 0
    assert len(a.flows) == 2


def test_single_claimant_untouched():
    a = _feat("a", ["a.ts"], [_flow("f1", "a.ts", 1)])
    b = _feat("b", ["b.ts"], [_flow("f2", "b.ts", 1)])
    res = dedup_cross_feature_flows([a, b])
    assert res.flows_removed == 0


def test_default_off(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    owner = _feat("a", ["s.ts"], [_flow("f1", "s.ts", 1)])
    twin = _feat("b", ["b.ts"], [_flow("f2", "s.ts", 1)])
    res = dedup_cross_feature_flows([owner, twin])
    assert res.enabled is False and res.flows_removed == 0


def test_env_opt_out(monkeypatch):
    monkeypatch.setenv(_ENV, "0")
    owner = _feat("a", ["s.ts"], [_flow("f1", "s.ts", 1)])
    twin = _feat("b", ["b.ts"], [_flow("f2", "s.ts", 1)])
    res = dedup_cross_feature_flows([owner, twin])
    assert res.enabled is False and res.flows_removed == 0
    assert len(twin.flows) == 1


def test_deterministic_regardless_of_input_order():
    def build(order):
        owner = _feat("array", ["utils/array.ts"],
                      [_flow("group-data-flow", "utils/array.ts", 19)])
        claimant = _feat("shared", ["store/s.ts"],
                         [_flow("sort-flow", "utils/array.ts", 19)])
        feats = [owner, claimant] if order else [claimant, owner]
        dedup_cross_feature_flows(feats)
        return sorted((f.name, [x.name for x in f.flows]) for f in feats)
    assert build(True) == build(False)
