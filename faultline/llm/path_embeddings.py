"""Sprint 21 — Path-only embedding clustering.

Architectural lever: insert a deterministic clustering step BEFORE
``deep_scan``. The scanner gives Sonnet pre-grouped path clusters
instead of a single 1000-path soup. Sonnet just NAMES each cluster.

Why this works
==============

  - Bounded payloads: each cluster is 50-200 paths; Sonnet doesn't
    have to hold a full monorepo in attention.
  - Deterministic structure: k-means produces stable groupings;
    no Sonnet calibration drift.
  - No content leak: we embed FILE PATHS only (e.g.
    ``apps/web/auth/login.ts``). Same metadata Sonnet already sees.
    No README, no source code, nothing new exposed.
  - Targets weak cohort: flat-Vue (uptime-kuma, hoppscotch) and
    over-decomposed monorepos (cal.com, infisical) — both fail
    because Sonnet can't decompose well from raw path soup.

Public surface
==============

    EmbeddingProvider (Protocol)
        embed(texts) -> list[list[float]]

    VoyageEmbedder    — uses voyage-3-lite ($0.02/M tokens, code-tuned)
    LocalEmbedder     — sentence-transformers all-MiniLM-L6-v2 (offline)

    cluster_paths(paths, *, k=None, embedder=None) -> list[list[str]]
        K-means on path embeddings → list of clusters (each a path list).
        ``k`` defaults to adaptive: max(8, min(25, n_files // 50)).

The clustering is a DETERMINISTIC transformation given the same
input + provider + seed. Re-running on identical input yields
identical clusters.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_VOYAGE_MODEL = "voyage-3-lite"
DEFAULT_LOCAL_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_BATCH_SIZE = 128

# Adaptive K bounds — established by the 26-repo S17 corpus where GT
# averages ~13 features/repo. Floor 8 keeps small repos from
# collapsing; ceiling 25 keeps big monorepos from over-decomposing.
_K_FLOOR = 8
_K_CEILING = 25
_FILES_PER_CLUSTER = 50


def adaptive_k(n_files: int) -> int:
    """Return a sensible cluster count for a repo of ``n_files``."""
    if n_files <= 0:
        return _K_FLOOR
    derived = n_files // _FILES_PER_CLUSTER
    return max(_K_FLOOR, min(_K_CEILING, derived))


# ── Provider protocol ─────────────────────────────────────────────────


class EmbeddingProvider(Protocol):
    """Anything that turns a list of strings into vectors."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def name(self) -> str: ...


@dataclass
class VoyageEmbedder:
    """Cloud embeddings via Voyage AI. ~$0.02/M tokens, code-tuned."""

    api_key: str | None = None
    model: str = DEFAULT_VOYAGE_MODEL
    input_type: str = "document"  # "document" or "query" — paths are docs
    batch_size: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("VOYAGE_AI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "VoyageEmbedder requires api_key (VOYAGE_AI_API_KEY env var)"
            )
        try:
            import voyageai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "voyageai package missing. pip install voyageai"
            ) from exc

    @property
    def name(self) -> str:
        return f"voyage:{self.model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import voyageai
        client = voyageai.Client(api_key=self.api_key)
        out: list[list[float]] = []
        # Voyage caps batch at 128 / 1000-token limit per text. Paths
        # are short (avg ~10 tokens) so batch size is the constraint.
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i : i + self.batch_size]
            try:
                resp = client.embed(
                    chunk, model=self.model, input_type=self.input_type,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "VoyageEmbedder: batch %d failed (%s) — using zeros",
                    i // self.batch_size, exc,
                )
                # Provide zero-vectors so caller doesn't crash; results
                # for this batch effectively become a single cluster.
                # Use the dim of the previous successful batch when we
                # have one, else fallback to 512 (voyage-3-lite default).
                fallback_dim = len(out[0]) if out else 512
                out.extend([[0.0] * fallback_dim] * len(chunk))
                continue
            out.extend(resp.embeddings)
        return out


@dataclass
class LocalEmbedder:
    """Offline embeddings via sentence-transformers. Slower but private."""

    model: str = DEFAULT_LOCAL_MODEL

    def __post_init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers package missing. "
                "pip install sentence-transformers"
            ) from exc

    @property
    def name(self) -> str:
        return f"local:{self.model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(self.model)
        vecs = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]


def get_default_provider() -> EmbeddingProvider | None:
    """Return Voyage if VOYAGE_AI_API_KEY is set, else None.

    Local provider not auto-selected because it pulls a 200MB model on
    first use; callers must opt in explicitly via ``LocalEmbedder()``.
    """
    if os.environ.get("VOYAGE_AI_API_KEY"):
        try:
            return VoyageEmbedder()
        except (ValueError, ImportError) as exc:
            logger.warning("path_embeddings: Voyage init failed (%s)", exc)
    return None


# ── K-means clustering ────────────────────────────────────────────────


def _kmeans(
    vectors: list[list[float]],
    k: int,
    *,
    max_iter: int = 50,
    seed: int = 42,
) -> list[int]:
    """Lightweight k-means; returns cluster index per input vector.

    Uses scikit-learn when available (fast, well-tested), falls back to
    a numpy-only implementation if sklearn isn't installed.
    """
    if not vectors or k <= 0:
        return []
    n = len(vectors)
    if k >= n:
        return list(range(n))

    try:
        import numpy as np
        from sklearn.cluster import KMeans
        arr = np.asarray(vectors, dtype="float32")
        km = KMeans(n_clusters=k, random_state=seed, n_init=10, max_iter=max_iter)
        labels = km.fit_predict(arr)
        return labels.tolist()
    except ImportError:
        logger.debug("path_embeddings: sklearn unavailable, using numpy fallback")
        return _kmeans_numpy(vectors, k, max_iter=max_iter, seed=seed)


def _kmeans_numpy(
    vectors: list[list[float]], k: int,
    *, max_iter: int, seed: int,
) -> list[int]:
    """Pure numpy k-means as a fallback."""
    import numpy as np
    rng = np.random.default_rng(seed)
    arr = np.asarray(vectors, dtype="float32")
    n, d = arr.shape
    indices = rng.choice(n, size=k, replace=False)
    centers = arr[indices].copy()
    labels = np.zeros(n, dtype=np.int32)
    for _ in range(max_iter):
        # Assign
        dists = ((arr[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = dists.argmin(axis=1)
        if (new_labels == labels).all():
            break
        labels = new_labels
        # Update
        for c in range(k):
            mask = labels == c
            if mask.any():
                centers[c] = arr[mask].mean(axis=0)
    return labels.tolist()


# ── Public clustering entry point ─────────────────────────────────────


def cluster_paths(
    paths: list[str],
    *,
    k: int | None = None,
    embedder: EmbeddingProvider | None = None,
    seed: int = 42,
) -> list[list[str]]:
    """Group ``paths`` into ``k`` clusters by semantic similarity.

    Args:
      paths: file paths to cluster.
      k: number of clusters; defaults to ``adaptive_k(len(paths))``.
      embedder: provider; defaults to ``get_default_provider()`` (Voyage
        when ``VOYAGE_AI_API_KEY`` is set).
      seed: random seed for k-means determinism.

    Returns:
      List of clusters, each a list of paths. Order is by cluster index;
      empty clusters dropped. Returns ``[paths]`` (single cluster) if
      no embedder is available — callers should treat that as a no-op.
    """
    if not paths:
        return []
    if k is None:
        k = adaptive_k(len(paths))
    if k <= 1 or len(paths) <= k:
        return [list(paths)]

    embedder = embedder or get_default_provider()
    if embedder is None:
        logger.info("path_embeddings: no embedder available — returning single cluster")
        return [list(paths)]

    logger.info(
        "path_embeddings: embedding %d paths via %s (k=%d)",
        len(paths), embedder.name, k,
    )
    vectors = embedder.embed(paths)
    if not vectors or len(vectors) != len(paths):
        logger.warning(
            "path_embeddings: vector count mismatch (got %d, expected %d) — abort",
            len(vectors), len(paths),
        )
        return [list(paths)]

    labels = _kmeans(vectors, k, seed=seed)
    clusters: dict[int, list[str]] = {}
    for path, label in zip(paths, labels):
        clusters.setdefault(label, []).append(path)
    # Sort clusters by size desc so callers see big features first
    return sorted(clusters.values(), key=lambda c: -len(c))
