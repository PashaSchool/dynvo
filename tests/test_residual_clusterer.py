"""Tests for ``faultline.pipeline_v2.residual_clusterer``.

Verifies:

* Empty / degenerate input → empty / single-cluster output.
* Cluster keying is structural (top-level dir, extension, depth
  bucket) — the A2b 3-tuple. ``filename_suffix`` is intentionally
  NOT in the key.
* Sample selection is even-spaced, deterministic, ≤ ``SAMPLE_CAP``.
* Cluster ordering is stable across runs (sorted by key).
* Singleton synthesizer is pure, kebab-cased, and skips scaffolding.
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2.residual_clusterer import (
    SAMPLE_CAP,
    ResidualCluster,
    _depth_bucket,
    _evenly_spaced_sample,
    cluster_residual_paths,
    synthesize_singleton_feature,
)


# ── helpers ──────────────────────────────────────────────────────────


def test_empty_input_returns_empty_list() -> None:
    assert cluster_residual_paths([]) == []
    assert cluster_residual_paths(iter([])) == []


def test_filters_non_string_and_empty() -> None:
    # mixed iterable shouldn't crash; non-strings + empty strings dropped
    result = cluster_residual_paths(["a.ts", "", "x.go"])
    exts = [c.key[1] for c in result]
    assert ".ts" in exts and ".go" in exts


def test_single_path_single_cluster() -> None:
    out = cluster_residual_paths(["foo/bar.ts"])
    assert len(out) == 1
    c = out[0]
    assert c.size == 1
    assert c.paths == ("foo/bar.ts",)
    assert c.sample_paths == ("foo/bar.ts",)
    assert c.key[0] == "foo"      # top-level dir
    assert c.key[1] == ".ts"      # extension
    assert c.key[2] == "shallow"  # depth bucket


def test_root_level_file_empty_top_level() -> None:
    out = cluster_residual_paths(["README"])
    assert out[0].key[0] == ""


# ── key derivation ───────────────────────────────────────────────────


def test_cluster_key_is_three_components() -> None:
    """A2b: ``filename_suffix`` is no longer in the key."""
    out = cluster_residual_paths(["foo/bar.ts"])
    assert len(out[0].key) == 3


def test_depth_bucket_bands() -> None:
    assert _depth_bucket("a.ts") == "shallow"           # 1 segment
    assert _depth_bucket("a/b.ts") == "shallow"         # 2
    assert _depth_bucket("a/b/c.ts") == "mid"           # 3
    assert _depth_bucket("a/b/c/d/e.ts") == "mid"       # 5
    assert _depth_bucket("a/b/c/d/e/f.ts") == "deep"    # 6
    assert _depth_bucket("a/b/c/d/e/f/g/h.ts") == "deep"


def test_same_extension_different_top_level_split() -> None:
    paths = [
        "api/user_handler.go",
        "api/billing_handler.go",
        "web/admin_view.go",
    ]
    out = cluster_residual_paths(paths)
    keys_by_top = {c.key[0]: c for c in out}
    # api and web are different top-level dirs → separate clusters
    assert "api" in keys_by_top
    assert "web" in keys_by_top
    assert keys_by_top["api"].size == 2
    assert keys_by_top["web"].size == 1


def test_same_top_level_different_filename_stems_now_collapse() -> None:
    """A2b key change: stems no longer split the cluster.

    Pre-A2b this would have produced 2 clusters (suffix ``handler`` vs
    ``routes``). Now both share key ``("api", ".go", "shallow")``.
    """
    paths = [
        "api/user_handler.go",
        "api/billing_routes.go",
    ]
    out = cluster_residual_paths(paths)
    assert len(out) == 1
    assert out[0].size == 2


def test_components_collapse_into_single_cluster() -> None:
    """The A2b motivating example: Card.tsx, Form.tsx, Modal.tsx
    under the same ``app/components/`` mid-depth folder all share
    one cluster now."""
    paths = [
        "app/components/Card.tsx",
        "app/components/Form.tsx",
        "app/components/Modal.tsx",
    ]
    out = cluster_residual_paths(paths)
    assert len(out) == 1
    assert out[0].key == ("app", ".tsx", "mid")
    assert out[0].size == 3


def test_same_signature_grouped_even_across_subdirs() -> None:
    # all under api/, all .go extension; depth differs only enough to
    # stay within the same bucket.
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
        key=("a", ".ts", "shallow"),
        paths=("a/x.ts",),
        sample_paths=("a/x.ts",),
        size=1,
    )
    try:
        c.size = 2  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ResidualCluster should be frozen")


# ── A2b: singleton synthesizer ───────────────────────────────────────


def test_synthesize_singleton_skips_root_dotfiles() -> None:
    assert synthesize_singleton_feature(".eslintrc") is None
    assert synthesize_singleton_feature(".gitignore") is None
    assert synthesize_singleton_feature(".prettierrc") is None


def test_synthesize_singleton_emits_for_dotfile_with_structure() -> None:
    # .env.example IS feature-like — engineers reference it as
    # "env-example". The pure-dot-file skip should NOT catch it.
    feat = synthesize_singleton_feature(".env.example")
    assert feat is not None
    assert feat.name == "env-example"
    assert feat.paths == (".env.example",)
    assert feat.confidence == "low"
    assert feat.sources == ["singleton-synth"]


def test_synthesize_singleton_skips_known_manifests() -> None:
    assert synthesize_singleton_feature("package.json") is None
    assert synthesize_singleton_feature("Cargo.toml") is None
    assert synthesize_singleton_feature("pyproject.toml") is None
    assert synthesize_singleton_feature("go.mod") is None
    assert synthesize_singleton_feature("Gemfile") is None


def test_synthesize_singleton_path_based_naming() -> None:
    feat = synthesize_singleton_feature(
        "apps/admin/providers/store.provider.tsx",
    )
    assert feat is not None
    # ``apps`` is not in noise set (only ``app`` is), so it survives.
    # ``store.provider`` splits on the dot — both tokens contribute.
    assert feat.name == "apps-admin-providers-store-provider"
    assert feat.paths == ("apps/admin/providers/store.provider.tsx",)


def test_synthesize_singleton_strips_noise_tokens() -> None:
    # ``src`` + ``app`` are noise; ``api`` and ``handler`` are kept.
    feat = synthesize_singleton_feature("src/api/handler.ts")
    assert feat is not None
    assert feat.name == "api-handler"


def test_synthesize_singleton_root_config_with_unknown_basename() -> None:
    # Not in the manifest set → still emits.
    feat = synthesize_singleton_feature("vite.config.ts")
    assert feat is not None
    # leaf stem ``vite.config`` splits on dot → vite-config
    assert feat.name == "vite-config"


def test_synthesize_singleton_api_coveragerc() -> None:
    feat = synthesize_singleton_feature("apps/api/.coveragerc")
    assert feat is not None
    # The leading dot is stripped by the kebab pass.
    assert feat.name == "apps-api-coveragerc"


def test_synthesize_singleton_returns_none_for_empty() -> None:
    assert synthesize_singleton_feature("") is None
    assert synthesize_singleton_feature("   ") is None


def test_synthesize_singleton_collapses_into_devfeature(tmp_path: Path) -> None:
    """Smoke: the synthesizer returns a real DeveloperFeature instance."""
    feat = synthesize_singleton_feature("apps/web/page.tsx", repo_root=tmp_path)
    assert feat is not None
    # confidence + sources + rationale per A2b contract
    assert feat.confidence == "low"
    assert feat.sources == ["singleton-synth"]
    assert "stage-4-singleton-synth" in feat.rationale
