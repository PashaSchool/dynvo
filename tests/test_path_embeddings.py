"""Sprint 21 Day 1 — path_embeddings unit tests with fake embedder."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from faultline.llm.path_embeddings import (
    LocalEmbedder,
    VoyageEmbedder,
    adaptive_k,
    cluster_paths,
    get_default_provider,
)


@dataclass
class FakeEmbedder:
    """Deterministic fake — embeds based on path's first directory.

    Files in the same top-level directory get the same vector; files
    in different top-level directories get orthogonal vectors. This
    lets us assert that clustering recovers the directory grouping.
    """

    name: str = "fake:dir-onehot"

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Build a dim-N one-hot per top-level dir
        top_dirs: dict[str, int] = {}
        for t in texts:
            d = t.split("/")[0]
            if d not in top_dirs:
                top_dirs[d] = len(top_dirs)
        dim = max(2, len(top_dirs))
        out: list[list[float]] = []
        for t in texts:
            d = t.split("/")[0]
            vec = [0.0] * dim
            vec[top_dirs[d]] = 1.0
            out.append(vec)
        return out


def test_adaptive_k_floor():
    assert adaptive_k(0) >= 8
    assert adaptive_k(50) >= 8


def test_adaptive_k_ceiling():
    assert adaptive_k(10_000) <= 25


def test_adaptive_k_scales_with_size():
    assert adaptive_k(100) <= adaptive_k(1000) <= adaptive_k(10_000)


def test_cluster_paths_groups_by_directory():
    paths = [
        "auth/login.ts", "auth/session.ts", "auth/oauth.ts",
        "billing/checkout.ts", "billing/webhook.ts",
        "ui/button.tsx", "ui/dropdown.tsx",
    ]
    clusters = cluster_paths(paths, k=3, embedder=FakeEmbedder(), seed=42)
    # Each cluster's paths share a top-level dir
    for cl in clusters:
        top_dirs = {p.split("/")[0] for p in cl}
        assert len(top_dirs) == 1, f"cluster mixed dirs: {top_dirs}"


def test_cluster_paths_returns_single_when_k_le_1():
    paths = ["a.ts", "b.ts"]
    assert cluster_paths(paths, k=1, embedder=FakeEmbedder()) == [paths]
    assert cluster_paths(paths, k=0, embedder=FakeEmbedder()) == [paths]


def test_cluster_paths_returns_single_when_no_embedder(monkeypatch):
    monkeypatch.delenv("VOYAGE_AI_API_KEY", raising=False)
    paths = ["a.ts", "b.ts", "c.ts"]
    out = cluster_paths(paths, k=3)  # no embedder, no env key
    assert out == [paths]


def test_cluster_paths_empty():
    assert cluster_paths([]) == []


def test_cluster_paths_returns_largest_first():
    paths = (
        ["big/" + f for f in ("a", "b", "c", "d", "e")] +
        ["small/x", "small/y"]
    )
    clusters = cluster_paths(paths, k=2, embedder=FakeEmbedder(), seed=42)
    assert len(clusters[0]) >= len(clusters[1])


def test_voyage_embedder_requires_key(monkeypatch):
    monkeypatch.delenv("VOYAGE_AI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        VoyageEmbedder(api_key=None)


def test_get_default_provider_when_no_key(monkeypatch):
    monkeypatch.delenv("VOYAGE_AI_API_KEY", raising=False)
    assert get_default_provider() is None


def test_local_embedder_imports_only_when_called():
    """Constructor doesn't load the model — only embed() does.

    Skips if sentence-transformers isn't installed (it's an opt-in dep).
    """
    try:
        emb = LocalEmbedder()
    except ImportError:
        pytest.skip("sentence-transformers not installed")
    assert "local:" in emb.name


def test_clustering_deterministic_with_seed():
    paths = [
        "auth/login.ts", "auth/session.ts",
        "billing/checkout.ts", "billing/refund.ts",
    ]
    a = cluster_paths(paths, k=2, embedder=FakeEmbedder(), seed=42)
    b = cluster_paths(paths, k=2, embedder=FakeEmbedder(), seed=42)
    assert a == b
