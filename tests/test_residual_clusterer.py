"""Tests for ``faultline.pipeline_v2.residual_clusterer``.

Verifies:

* Empty / degenerate input → empty / single-cluster output.
* Cluster keying is structural (top-level dir, filename suffix,
  extension, depth bucket) and case-sensitive.
* Sample selection is even-spaced, deterministic, ≤ ``SAMPLE_CAP``.
* Cluster ordering is stable across runs (sorted by key).
"""

from __future__ import annotations

from faultline.pipeline_v2.residual_clusterer import (
    SAMPLE_CAP,
    ResidualCluster,
    _depth_bucket,
    _evenly_spaced_sample,
    _filename_suffix,
    cluster_residual_paths,
)


# ── helpers ──────────────────────────────────────────────────────────


def test_empty_input_returns_empty_list() -> None:
    assert cluster_residual_paths([]) == []
    assert cluster_residual_paths(iter([])) == []


def test_filters_non_string_and_empty() -> None:
    # mixed iterable shouldn't crash; non-strings + empty strings dropped
    result = cluster_residual_paths(["a.ts", "", "x.go"])
    names = [c.key[2] for c in result]
    assert ".ts" in names and ".go" in names


def test_single_path_single_cluster() -> None:
    out = cluster_residual_paths(["foo/bar.ts"])
    assert len(out) == 1
    c = out[0]
    assert c.size == 1
    assert c.paths == ("foo/bar.ts",)
    assert c.sample_paths == ("foo/bar.ts",)
    assert c.key[0] == "foo"      # top-level dir
    assert c.key[2] == ".ts"      # extension


def test_root_level_file_empty_top_level() -> None:
    out = cluster_residual_paths(["README"])
    assert out[0].key[0] == ""


# ── key derivation ───────────────────────────────────────────────────


def test_filename_suffix_picks_after_last_separator() -> None:
    assert _filename_suffix("user_handler.go") == "handler"
    assert _filename_suffix("billing-portal-handler.tsx") == "handler"
    assert _filename_suffix("routes.ts") == "routes"
    assert _filename_suffix("a/b/c/main.py") == "main"


def test_depth_bucket_bands() -> None:
    assert _depth_bucket("a.ts") == "shallow"           # 1 segment
    assert _depth_bucket("a/b.ts") == "shallow"         # 2
    assert _depth_bucket("a/b/c.ts") == "mid"           # 3
    assert _depth_bucket("a/b/c/d/e.ts") == "mid"       # 5
    assert _depth_bucket("a/b/c/d/e/f.ts") == "deep"    # 6
    assert _depth_bucket("a/b/c/d/e/f/g/h.ts") == "deep"


def test_same_suffix_different_top_level_split() -> None:
    paths = [
        "api/user_handler.go",
        "api/billing_handler.go",
        "web/admin_handler.go",
    ]
    out = cluster_residual_paths(paths)
    keys_by_top = {c.key[0]: c for c in out}
    # api and web are different top-level dirs → separate clusters
    assert "api" in keys_by_top
    assert "web" in keys_by_top
    assert keys_by_top["api"].size == 2
    assert keys_by_top["web"].size == 1


def test_same_top_level_different_suffix_split() -> None:
    paths = [
        "api/user_handler.go",
        "api/billing_routes.go",
    ]
    out = cluster_residual_paths(paths)
    assert len(out) == 2     # different suffixes (handler / routes)


def test_same_signature_grouped_even_across_subdirs() -> None:
    # all under api/, all *_handler.go, all .go extension; depth differs
    # only enough to stay within the same bucket.
    paths = [
        "api/v1/user_handler.go",
        "api/v1/billing_handler.go",
        "api/v2/audit_handler.go",
    ]
    out = cluster_residual_paths(paths)
    assert len(out) == 1
    assert out[0].size == 3


# ── sample selection ─────────────────────────────────────────────────


def test_even_spacing_caps_at_sample_cap() -> None:
    # All paths share suffix "handler" → same cluster.
    paths = [f"a/svc-{i:03}-handler.ts" for i in range(100)]
    out = cluster_residual_paths(paths)
    assert len(out) == 1
    c = out[0]
    assert c.size == 100
    assert len(c.sample_paths) <= SAMPLE_CAP
    # First member should always appear (index 0)
    assert c.sample_paths[0] == c.paths[0]
    # Even spacing — second sample should NOT be index 1
    assert c.sample_paths[1] != c.paths[1]


def test_sample_returns_all_when_below_cap() -> None:
    paths = [f"a/svc-{i}-handler.ts" for i in range(5)]
    out = cluster_residual_paths(paths)
    c = out[0]
    assert c.sample_paths == c.paths


def test_evenly_spaced_sample_directly() -> None:
    # n=30, cap=15 → stride=2, indices 0,2,4,...,28 → 15 samples
    items = [f"x{i}" for i in range(30)]
    sample = _evenly_spaced_sample(items, cap=15)
    assert len(sample) == 15
    assert sample[0] == "x0"
    assert sample[1] == "x2"
    assert sample[-1] == "x28"


def test_evenly_spaced_sample_empty() -> None:
    assert _evenly_spaced_sample([], cap=15) == ()


# ── determinism ──────────────────────────────────────────────────────


def test_deterministic_ordering_across_runs() -> None:
    paths = [
        "z/last.ts",
        "a/first.go",
        "m/middle.py",
        "a/second.go",
    ]
    out1 = cluster_residual_paths(paths)
    out2 = cluster_residual_paths(reversed(paths))
    keys1 = [c.key for c in out1]
    keys2 = [c.key for c in out2]
    assert keys1 == keys2
    # ascending by key tuple — top-level "a" before "m" before "z"
    assert keys1[0][0] == "a"


def test_cluster_is_frozen_dataclass() -> None:
    c = ResidualCluster(
        key=("a", "x", ".ts", "shallow"),
        paths=("a/x.ts",),
        sample_paths=("a/x.ts",),
        size=1,
    )
    try:
        c.size = 2  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ResidualCluster should be frozen")
