"""Unit tests for faultline.pipeline_v2.incremental_gate.

These prove the incremental LLM-gating contract WITHOUT calling an LLM:

  * ``partition_features`` sends only changed-touching features onward
    (so Stage 3's per-feature Haiku call runs on the changed subset).
  * ``filter_unattributed`` sends only changed residual paths to Stage 4.
  * ``rehydrate_untouched_features`` rebuilds full Feature objects (with
    flows + metrics) from the base scan for untouched features, and
    reports any base-miss so the caller can re-scan instead of dropping.

The end-to-end "Stage 3 receives only the changed subset" assertion
lives in ``test_incremental_gate_run.py`` (mocks Stage 3 and asserts the
feature list it was handed).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from faultline.pipeline_v2.incremental_gate import (
    base_features_by_name,
    compute_changed_set,
    filter_unattributed,
    partition_features,
    rehydrate_untouched_features,
)


# ── tiny stand-in for a Stage-2 DeveloperFeature ───────────────────
class _Feat:
    """Duck-typed Stage-2 feature: only ``name`` + ``paths`` are read."""

    def __init__(self, name: str, paths: list[str]) -> None:
        self.name = name
        self.paths = tuple(paths)


# ── partition_features ─────────────────────────────────────────────


def test_partition_marks_feature_touched_when_any_path_changed():
    feats = [
        _Feat("auth", ["src/auth/login.ts", "src/auth/session.ts"]),
        _Feat("billing", ["src/billing/charge.ts"]),
        _Feat("ui", ["src/ui/button.tsx"]),
    ]
    changed = {"src/auth/login.ts"}
    part = partition_features(feats, changed)
    assert [f.name for f in part.touched] == ["auth"]
    assert {f.name for f in part.untouched} == {"billing", "ui"}
    assert part.touched_names == {"auth"}


def test_partition_all_untouched_when_changed_set_disjoint():
    feats = [_Feat("auth", ["src/auth/a.ts"]), _Feat("ui", ["src/ui/b.tsx"])]
    part = partition_features(feats, {"docs/readme-unrelated.ts"})
    assert part.touched == []
    assert {f.name for f in part.untouched} == {"auth", "ui"}


def test_partition_all_touched_when_changed_set_covers_all():
    feats = [_Feat("auth", ["src/auth/a.ts"]), _Feat("ui", ["src/ui/b.tsx"])]
    part = partition_features(feats, {"src/auth/a.ts", "src/ui/b.tsx"})
    assert {f.name for f in part.touched} == {"auth", "ui"}
    assert part.untouched == []


def test_partition_does_not_mutate_input():
    feats = [_Feat("auth", ["src/auth/a.ts"])]
    partition_features(feats, {"src/auth/a.ts"})
    # original list object untouched
    assert len(feats) == 1 and feats[0].name == "auth"


def test_partition_empty_changed_set_means_nothing_touched():
    """The key cost-saving case: a no-op diff touches ZERO features, so
    Stage 3 (the per-feature Haiku call) runs on an empty list."""
    feats = [_Feat("a", ["x.ts"]), _Feat("b", ["y.ts"]), _Feat("c", ["z.ts"])]
    part = partition_features(feats, set())
    assert part.touched == []
    assert len(part.untouched) == 3


# ── filter_unattributed ────────────────────────────────────────────


def test_filter_unattributed_keeps_only_changed_paths():
    resid = ["src/x.ts", "src/y.ts", "src/z.ts"]
    out = filter_unattributed(resid, {"src/y.ts"})
    assert out == ["src/y.ts"]


def test_filter_unattributed_preserves_order():
    resid = ["a.ts", "b.ts", "c.ts", "d.ts"]
    out = filter_unattributed(resid, {"d.ts", "a.ts"})
    assert out == ["a.ts", "d.ts"]


def test_filter_unattributed_empty_when_nothing_changed():
    """No changed residual paths → Stage 4 makes ZERO LLM calls."""
    assert filter_unattributed(["a.ts", "b.ts"], set()) == []


# ── base_features_by_name ──────────────────────────────────────────


def test_base_features_by_name_indexes_developer_features():
    base = {"developer_features": [
        {"name": "auth", "paths": ["a.ts"]},
        {"name": "billing", "paths": ["b.ts"]},
    ]}
    idx = base_features_by_name(base)
    assert set(idx) == {"auth", "billing"}


def test_base_features_by_name_falls_back_to_features_alias():
    base = {"features": [{"name": "auth", "paths": ["a.ts"]}]}
    idx = base_features_by_name(base)
    assert "auth" in idx


def test_base_features_by_name_skips_product_layer():
    base = {"developer_features": [
        {"name": "auth", "paths": ["a.ts"], "layer": "developer"},
        {"name": "Billing Suite", "paths": [], "layer": "product"},
    ]}
    idx = base_features_by_name(base)
    assert "auth" in idx
    assert "Billing Suite" not in idx


def test_base_features_by_name_first_wins_on_duplicate():
    base = {"developer_features": [
        {"name": "auth", "paths": ["first.ts"]},
        {"name": "auth", "paths": ["second.ts"]},
    ]}
    idx = base_features_by_name(base)
    assert idx["auth"]["paths"] == ["first.ts"]


# ── rehydrate_untouched_features ───────────────────────────────────


_METRIC_FIELDS = {
    "authors": ["alice"],
    "total_commits": 12,
    "bug_fixes": 1,
    "bug_fix_ratio": 0.08,
    "last_modified": "2026-05-01T00:00:00+00:00",
    "health_score": 82.5,
}


def _full_base_feature(name: str, paths: list[str], *, flows=None) -> dict:
    """A base-scan feature dict with the required pydantic Feature fields."""
    return {
        "name": name,
        "paths": paths,
        **_METRIC_FIELDS,
        "coverage_pct": 71.0,
        "flows": flows or [],
        "uuid": "a" * 32,
        "layer": "developer",
    }


def test_rehydrate_rebuilds_feature_with_flows_and_metrics():
    base = {"developer_features": [
        _full_base_feature(
            "auth", ["src/auth/a.ts"],
            flows=[{
                "name": "sign-in-flow",
                "paths": ["src/auth/a.ts"],
                **_METRIC_FIELDS,
            }],
        ),
    ]}
    untouched = [_Feat("auth", ["src/auth/a.ts"])]
    res = rehydrate_untouched_features(untouched, base)
    assert res.rehydrated_names == ["auth"]
    assert res.missing_names == []
    assert len(res.features) == 1
    feat = res.features[0]
    # Carried-forward metrics survive (the whole point — no re-LLM).
    assert feat.health_score == 82.5
    assert feat.coverage_pct == 71.0
    assert feat.total_commits == 12
    # Carried-forward flows survive.
    assert [fl.name for fl in feat.flows] == ["sign-in-flow"]


def test_rehydrate_reports_missing_when_no_base_twin():
    """Untouched feature with NO base match must NOT be silently dropped
    — it is reported so the caller re-scans it through Stage 3."""
    base = {"developer_features": [_full_base_feature("auth", ["a.ts"])]}
    untouched = [_Feat("auth", ["a.ts"]), _Feat("brand-new", ["c.ts"])]
    res = rehydrate_untouched_features(untouched, base)
    assert res.rehydrated_names == ["auth"]
    assert res.missing_names == ["brand-new"]
    assert {f.name for f in res.features} == {"auth"}


def test_rehydrate_treats_unparseable_base_feature_as_missing():
    """A malformed base feature (missing required fields) is reported as
    missing rather than crashing the scan."""
    base = {"developer_features": [{"name": "auth"}]}  # no paths/metrics
    untouched = [_Feat("auth", ["a.ts"])]
    res = rehydrate_untouched_features(untouched, base)
    assert res.features == []
    assert res.missing_names == ["auth"]


def test_rehydrate_empty_untouched_returns_empty():
    res = rehydrate_untouched_features([], {"developer_features": []})
    assert res.features == []
    assert res.rehydrated_names == []
    assert res.missing_names == []


# ── compute_changed_set (git-backed) ───────────────────────────────


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "a.ts").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
    ).stdout.strip()
    (path / "b.ts").write_text("world")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "add b"], cwd=path, check=True)
    return sha1


def test_compute_changed_set_returns_set_of_changed_files(tmp_path):
    sha1 = _init_repo(tmp_path)
    changed = compute_changed_set(tmp_path, sha1, base_scan={})
    assert changed == {"b.ts"}
    assert isinstance(changed, set)


def test_compute_changed_set_empty_on_bad_sha(tmp_path):
    _init_repo(tmp_path)
    changed = compute_changed_set(tmp_path, "deadbeef" * 5, base_scan={})
    assert changed == set()
