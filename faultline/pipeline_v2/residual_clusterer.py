"""Residual-path clusterer (Sprint A2).

Stage 4 used to chunk the residual list into fixed-size 200-path
slices and stop after 5 chunks. That hard-coded budget silently lost
the majority of the residual on large repos (infisical: 7979 paths,
supabase: 8584). The replacement here groups residual paths by their
STRUCTURAL signature so Stage 4 makes one Haiku call per coherent
cluster of paths instead of one call per arbitrary slice.

Design constraints
==================

* Scale-invariant. The clustering key is derived from path structure
  (top-level dir, filename suffix, extension, depth bucket) — never
  from a tunable numeric threshold. A small repo with 5 distinct
  structures → 5 clusters. A huge repo with 200 distinct structures
  → 200 clusters. Cost scales with structural diversity, not raw
  path count.

* Deterministic. Same input → identical cluster ordering, identical
  ``sample_paths`` selection. Required for cache-replayability of
  Stage 4 across A/B runs.

* No magic numbers. The single literal — ``SAMPLE_CAP = 15`` — is the
  number of sample paths shown to the LLM per cluster; 15 is enough
  for Haiku to infer a name without bloating prompt tokens. This is
  a UI-shape constant, not a tuning knob.

Cluster key
===========

Each path is mapped to ``(top_level_dir, filename_suffix, extension,
depth_bucket)`` where:

* ``top_level_dir``: ``path.split("/")[0]``, or empty string for a
  root-level file.

* ``filename_suffix``: portion of the stem after the last ``_`` or
  ``-`` separator. If the stem has no such separator, the full stem
  is used. ``user_handler.go`` → ``handler``; ``routes.ts`` →
  ``routes``; ``billing-portal.tsx`` → ``portal``.

* ``extension``: ``Path(p).suffix`` (e.g. ``.go``, ``.tsx``).

* ``depth_bucket``: structural band derived from segment count —
  ``"shallow"`` for ≤2, ``"mid"`` for 3-5, ``"deep"`` for ≥6.
  These bands intentionally mirror how engineers think about path
  depth ("top-level", "module-deep", "deeply nested") rather than
  any numeric threshold tuned against one repo.

Sample selection
================

``sample_paths`` is an evenly-spaced subset of ≤15 representatives
from the sorted cluster. We never just take the first 15 because
that would bias toward alphabetical neighbours; even spacing gives
the LLM a more representative cross-section of the cluster's range
without bloating the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

# Maximum number of representative paths surfaced to the LLM per
# cluster. This is a presentation constant — the LLM doesn't need
# 500 paths to name a cluster — not a tuning threshold.
SAMPLE_CAP = 15

# Depth bands. These are structural and apply to every stack; not a
# per-repo tune.
_DEPTH_SHALLOW_MAX = 2
_DEPTH_MID_MAX = 5


@dataclass(frozen=True)
class ResidualCluster:
    """A structurally-coherent group of residual paths.

    Attributes:
        key: ``(top_level_dir, filename_suffix, extension, depth_bucket)``
            — the structural signature shared by every member.
        paths: full membership (sorted ascending).
        sample_paths: ≤ ``SAMPLE_CAP`` evenly-spaced representatives,
            in the same order as ``paths``.
        size: ``len(paths)`` — exposed to the LLM so it knows the true
            scale of the cluster even when ``sample_paths`` is truncated.
    """

    key: tuple[str, str, str, str]
    paths: tuple[str, ...]
    sample_paths: tuple[str, ...]
    size: int


# ── Key derivation ────────────────────────────────────────────────────


def _top_level_dir(path: str) -> str:
    """First path segment, or empty string for a root-level file."""
    if "/" not in path:
        return ""
    return path.split("/", 1)[0]


def _filename_suffix(path: str) -> str:
    """Stem text after the last ``_`` or ``-`` separator.

    Falls back to the full stem when neither separator is present.
    """
    stem = PurePosixPath(path).stem
    # Pick the LATEST separator position — handles compound names like
    # ``billing-portal-handler`` → ``handler``.
    last_underscore = stem.rfind("_")
    last_hyphen = stem.rfind("-")
    cut = max(last_underscore, last_hyphen)
    if cut == -1:
        return stem
    return stem[cut + 1 :] or stem


def _extension(path: str) -> str:
    return PurePosixPath(path).suffix


def _depth_bucket(path: str) -> str:
    # Segment count on a POSIX path — root-level file has 1 segment,
    # ``a/b/c.ts`` has 3. We don't normalise leading slashes because
    # repo-relative paths never start with one.
    segments = len([s for s in path.split("/") if s])
    if segments <= _DEPTH_SHALLOW_MAX:
        return "shallow"
    if segments <= _DEPTH_MID_MAX:
        return "mid"
    return "deep"


def _cluster_key(path: str) -> tuple[str, str, str, str]:
    return (
        _top_level_dir(path),
        _filename_suffix(path),
        _extension(path),
        _depth_bucket(path),
    )


# ── Sample selection ──────────────────────────────────────────────────


def _evenly_spaced_sample(sorted_paths: list[str], cap: int = SAMPLE_CAP) -> tuple[str, ...]:
    """Pick ≤``cap`` representatives from ``sorted_paths`` at even strides.

    Strategy: stride = ``max(1, n // cap)``, take indices ``[0, stride,
    2*stride, ...]`` while ``< n``, trim to ``cap``. This guarantees
    determinism, covers the cluster's full range, and never returns
    duplicates because indices are strictly increasing.
    """
    n = len(sorted_paths)
    if n == 0:
        return ()
    if n <= cap:
        return tuple(sorted_paths)
    stride = max(1, n // cap)
    picked: list[str] = []
    i = 0
    while i < n and len(picked) < cap:
        picked.append(sorted_paths[i])
        i += stride
    return tuple(picked)


# ── Public entry point ────────────────────────────────────────────────


def cluster_residual_paths(paths: Iterable[str]) -> list[ResidualCluster]:
    """Group ``paths`` by structural signature.

    The returned list is sorted by cluster key so two runs on the same
    input produce identical output (important for cached A/B testing).
    Empty input returns an empty list.
    """
    bucketed: dict[tuple[str, str, str, str], list[str]] = {}
    for p in paths:
        if not isinstance(p, str) or not p:
            continue
        key = _cluster_key(p)
        bucketed.setdefault(key, []).append(p)

    clusters: list[ResidualCluster] = []
    for key in sorted(bucketed.keys()):
        members = sorted(bucketed[key])
        clusters.append(
            ResidualCluster(
                key=key,
                paths=tuple(members),
                sample_paths=_evenly_spaced_sample(members),
                size=len(members),
            ),
        )
    return clusters


__all__ = [
    "ResidualCluster",
    "SAMPLE_CAP",
    "cluster_residual_paths",
]
