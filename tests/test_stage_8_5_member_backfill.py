"""Unit tests for Stage 8.5 deterministic member backfill."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from faultline.pipeline_v2.stage_8_5_member_backfill import (
    BackfillResult,
    overlap_ratio,
    run_stage_8_5_backfill,
)


@dataclass
class FakeFeature:
    """Minimal structural stand-in for the pydantic Feature model."""

    name: str
    paths: list[str] = field(default_factory=list)
    product_feature_id: str | None = None


# ── overlap_ratio math ─────────────────────────────────────────────

def test_overlap_ratio_full():
    assert overlap_ratio(["a", "b"], {"a", "b", "c"}) == 1.0


def test_overlap_ratio_half():
    assert overlap_ratio(["a", "b"], {"a", "z"}) == 0.5


def test_overlap_ratio_none():
    assert overlap_ratio(["a", "b"], {"x", "y"}) == 0.0


def test_overlap_ratio_empty_dev_paths_is_zero():
    assert overlap_ratio([], {"a"}) == 0.0


def test_overlap_ratio_is_scale_invariant():
    # Same proportion (half) at very different sizes → identical ratio.
    small = overlap_ratio(["a", "b"], {"a"})
    big_dev = [f"f{i}" for i in range(600)]
    big_pf = {f"f{i}" for i in range(300)}
    big = overlap_ratio(big_dev, big_pf)
    assert small == big == 0.5


# ── attach above threshold ─────────────────────────────────────────

def test_attaches_when_majority_overlap():
    feats = [FakeFeature("auth", ["pkg/auth/a.ts", "pkg/auth/b.ts"])]
    pfs = [FakeFeature("Login", ["pkg/auth/a.ts", "pkg/auth/b.ts", "x.ts"])]
    res = run_stage_8_5_backfill(feats, pfs, enabled=True)
    assert feats[0].product_feature_id == "Login"
    assert res.attached == 1
    assert res.still_unmapped == 0


def test_picks_single_best_overlapping_pf():
    feats = [FakeFeature("billing", ["b/1.ts", "b/2.ts", "b/3.ts", "b/4.ts"])]
    pfs = [
        FakeFeature("Weak", ["b/1.ts"]),                       # ratio 0.25
        FakeFeature("Strong", ["b/1.ts", "b/2.ts", "b/3.ts"]),  # ratio 0.75
    ]
    run_stage_8_5_backfill(feats, pfs, enabled=True)
    assert feats[0].product_feature_id == "Strong"


# ── leave-unmapped below threshold ─────────────────────────────────

def test_leaves_internal_feature_unmapped_below_threshold():
    # tsconfig-style infra feature: no file overlap with any cluster.
    feats = [FakeFeature("tsconfig", ["tooling/tsconfig.json"])]
    pfs = [FakeFeature("Booking", ["apps/web/booking/page.tsx"])]
    res = run_stage_8_5_backfill(feats, pfs, enabled=True)
    assert feats[0].product_feature_id is None
    assert res.attached == 0
    assert res.still_unmapped == 1


def test_threshold_boundary_inclusive():
    # exactly at threshold (0.5) → attaches (>= comparison).
    feats = [FakeFeature("x", ["a", "b"])]
    pfs = [FakeFeature("PF", ["a", "z"])]  # ratio == 0.5
    run_stage_8_5_backfill(feats, pfs, threshold=0.5, enabled=True)
    assert feats[0].product_feature_id == "PF"


def test_just_below_threshold_stays_unmapped():
    feats = [FakeFeature("x", ["a", "b", "c"])]  # one match → 0.333
    pfs = [FakeFeature("PF", ["a", "z"])]
    run_stage_8_5_backfill(feats, pfs, threshold=0.5, enabled=True)
    assert feats[0].product_feature_id is None


# ── never re-maps / never alters product features ──────────────────

def test_never_remaps_already_mapped_feature():
    feats = [FakeFeature("a", ["x.ts"], product_feature_id="AnalystPF")]
    pfs = [FakeFeature("OtherPF", ["x.ts"])]  # would overlap fully
    res = run_stage_8_5_backfill(feats, pfs, enabled=True)
    assert feats[0].product_feature_id == "AnalystPF"  # untouched
    assert res.attached == 0


def test_updates_dev_to_product_map_when_provided():
    feats = [FakeFeature("auth", ["auth/a.ts"])]
    pfs = [FakeFeature("Login", ["auth/a.ts"])]
    d2p: dict[str, list[str]] = {}
    run_stage_8_5_backfill(feats, pfs, d2p, enabled=True)
    assert d2p["auth"] == ["Login"]


# ── disabled / degenerate ──────────────────────────────────────────

def test_disabled_is_noop():
    feats = [FakeFeature("auth", ["auth/a.ts"])]
    pfs = [FakeFeature("Login", ["auth/a.ts"])]
    res = run_stage_8_5_backfill(feats, pfs, enabled=False)
    assert feats[0].product_feature_id is None
    assert res.attached == 0
    assert res.enabled is False


def test_env_flag_disables(monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_8_5_BACKFILL", "0")
    feats = [FakeFeature("auth", ["auth/a.ts"])]
    pfs = [FakeFeature("Login", ["auth/a.ts"])]
    res = run_stage_8_5_backfill(feats, pfs)  # enabled resolved from env
    assert feats[0].product_feature_id is None
    assert res.attached == 0


def test_no_product_features_is_noop():
    feats = [FakeFeature("auth", ["auth/a.ts"])]
    res = run_stage_8_5_backfill(feats, [], enabled=True)
    assert res.attached == 0
    assert res.still_unmapped == 1


def test_feature_with_no_paths_stays_unmapped():
    feats = [FakeFeature("phantom", [])]
    pfs = [FakeFeature("PF", ["a.ts"])]
    res = run_stage_8_5_backfill(feats, pfs, enabled=True)
    assert feats[0].product_feature_id is None
    assert res.attached == 0


# ── telemetry ──────────────────────────────────────────────────────

def test_telemetry_reports_before_after_pct():
    feats = [
        FakeFeature("a", ["a.ts"], product_feature_id="PF"),  # pre-mapped
        FakeFeature("b", ["b.ts"]),                            # backfilled
        FakeFeature("internal", ["z.ts"]),                     # stays unmapped
    ]
    pfs = [FakeFeature("PF", ["a.ts", "b.ts"])]
    res = run_stage_8_5_backfill(feats, pfs, enabled=True)
    tele = res.as_telemetry()
    assert tele["attached"] == 1
    assert tele["still_unmapped"] == 1
    assert tele["attached_pct_before"] == pytest.approx(1 / 3, abs=1e-3)
    assert tele["attached_pct_after"] == pytest.approx(2 / 3, abs=1e-3)
    assert tele["threshold"] == 0.5
    assert tele["enabled"] is True


def test_env_threshold_override(monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_8_5_THRESHOLD", "0.9")
    feats = [FakeFeature("x", ["a", "b"])]  # 0.5 overlap
    pfs = [FakeFeature("PF", ["a", "z"])]
    res = run_stage_8_5_backfill(feats, pfs)
    assert feats[0].product_feature_id is None  # 0.5 < 0.9
    assert res.threshold == 0.9


def test_tie_break_is_deterministic():
    # Two PFs with identical overlap ratio → lexicographically-first wins,
    # stably across runs.
    feats = [FakeFeature("x", ["a", "b"])]
    pfs = [FakeFeature("Zeta", ["a"]), FakeFeature("Alpha", ["a"])]
    run_stage_8_5_backfill(feats, pfs, threshold=0.5, enabled=True)
    assert feats[0].product_feature_id == "Alpha"
