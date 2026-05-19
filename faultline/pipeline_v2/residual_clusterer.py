"""Residual-path clusterer (Sprint A2 + A2b).

Stage 4 used to chunk the residual list into fixed-size 200-path
slices and stop after 5 chunks. That hard-coded budget silently lost
the majority of the residual on large repos (infisical: 7979 paths,
supabase: 8584). The replacement here groups residual paths by their
STRUCTURAL signature so Stage 4 makes one Haiku call per coherent
cluster of paths instead of one call per arbitrary slice.

Sprint A2b — coarsen key, synthesize singletons
================================================

A2 (commit ``1a79a35``) shipped a 4-component cluster key
``(top_level_dir, filename_suffix, extension, depth_bucket)``. On
TypeScript monorepos the ``filename_suffix`` axis exploded cluster
cardinality (openstatus: 1160 clusters from 2557 paths) because file
stems are diverse (``page``, ``route``, ``handler``, ``client``,
``lib``, ``view``). Every cluster yielded one new feature name, so
saturation never triggered → 1160 Haiku calls, $1.02 spend.

A2b changes:

  1. **Drop ``filename_suffix`` from the key**. ``filename_suffix`` is
     a NAMING convention; we are a STRUCTURAL extractor. Conventions
     belong to LLM interpretation, not clustering. New key is
     ``(top_level_dir, extension, depth_bucket)`` — a 3-tuple.
     Collapses ``Card.tsx + Form.tsx + Modal.tsx`` under the same
     ``app/components/``-``tsx``-``mid-depth`` cluster, where they
     belong.

  2. **Synthesize singleton features deterministically**. A cluster
     of size 1 doesn't need an LLM call — the path itself names the
     feature. ``synthesize_singleton_feature()`` is a pure function
     that derives a kebab name from the path (parent dir + stem, drop
     noise tokens) OR returns ``None`` to skip entirely for
     scaffolding files (root dot-files, known manifests handled by
     the Stage 1 package extractor).

Design constraints
==================

* Scale-invariant. The clustering key is derived from path structure
  (top-level dir, extension, depth bucket) — never from a tunable
  numeric threshold.

* Deterministic. Same input → identical cluster ordering, identical
  ``sample_paths`` selection. Required for cache-replayability of
  Stage 4 across A/B runs.

* No magic numbers. The single literal — ``SAMPLE_CAP = 15`` — is the
  number of sample paths shown to the LLM per cluster; 15 is enough
  for Haiku to infer a name without bloating prompt tokens. This is
  a UI-shape constant, not a tuning knob.

Cluster key
===========

Each path is mapped to ``(top_level_dir, extension, depth_bucket)``
where:

* ``top_level_dir``: ``path.split("/")[0]``, or empty string for a
  root-level file.

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
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

# Maximum number of representative paths surfaced to the LLM per
# cluster. This is a presentation constant — the LLM doesn't need
# 500 paths to name a cluster — not a tuning threshold.
SAMPLE_CAP = 15

# Depth bands. These are structural and apply to every stack; not a
# per-repo tune.
_DEPTH_SHALLOW_MAX = 2
_DEPTH_MID_MAX = 5


# Cluster key is a 3-tuple after A2b. ``filename_suffix`` was dropped
# because it's a naming convention, not a structural axis — see module
# docstring.
ClusterKey = tuple[str, str, str]  # (top_level_dir, extension, depth_bucket)


@dataclass(frozen=True)
class ResidualCluster:
    """A structurally-coherent group of residual paths.

    Attributes:
        key: ``(top_level_dir, extension, depth_bucket)`` — the
            structural signature shared by every member.
        paths: full membership (sorted ascending).
        sample_paths: ≤ ``SAMPLE_CAP`` evenly-spaced representatives,
            in the same order as ``paths``.
        size: ``len(paths)`` — exposed to the LLM so it knows the true
            scale of the cluster even when ``sample_paths`` is truncated.
    """

    key: ClusterKey
    paths: tuple[str, ...]
    sample_paths: tuple[str, ...]
    size: int


# ── Key derivation ────────────────────────────────────────────────────


def _top_level_dir(path: str) -> str:
    """First path segment, or empty string for a root-level file."""
    if "/" not in path:
        return ""
    return path.split("/", 1)[0]


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


def _cluster_key(path: str) -> ClusterKey:
    return (
        _top_level_dir(path),
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


# ── Singleton synthesizer (deterministic, no LLM) ────────────────────


# Path-noise tokens we strip when synthesizing a feature name from a
# single path. These are universal layout vocabulary — they NEVER
# carry product meaning, they're just where engineers store things.
# Keep this set small + universal: do NOT add stack-specific tokens
# (per memory/rule-no-magic-tuning.md and rule-no-repo-specific-paths.md).
_NOISE_TOKENS = frozenset({
    "src", "app", "lib", "libs", "pkg", "internal",
    "main", "index",
})

# Known config / manifest filenames that the Stage 1 package extractor
# already handles. If a singleton turns out to be one of these, skip
# emitting a feature for it — the Stage 1/2 layer is the source of
# truth for these surfaces.
_MANIFEST_FILENAMES = frozenset({
    "package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml",
    "yarn.lock", "package-lock.json", "lerna.json", "turbo.json",
    "nx.json", "rush.json",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "poetry.lock", "uv.lock", "pipfile", "pipfile.lock",
    "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock",
    "composer.json", "composer.lock",
    "mix.exs", "mix.lock",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "go.mod", "go.sum",
})

# Universal config-file extensions that are noise at root if the
# basename is recognisable as a dependency/build manifest. We
# combine this with the manifest filename set above.
_ROOT_CONFIG_EXTS = frozenset({
    ".json", ".yaml", ".yml", ".toml", ".ini", ".lock",
})


def _kebab_token(s: str) -> str:
    """Convert a filename-style token to kebab-friendly form.

    Lowercases, replaces ``_`` and ``.`` with ``-``, collapses runs.
    """
    out_chars: list[str] = []
    prev_dash = False
    for ch in s.lower():
        if ch.isalnum():
            out_chars.append(ch)
            prev_dash = False
        elif ch in ("_", "-", "."):
            if not prev_dash and out_chars:
                out_chars.append("-")
                prev_dash = True
        # else: skip
    # Trim trailing dash
    while out_chars and out_chars[-1] == "-":
        out_chars.pop()
    return "".join(out_chars)


def synthesize_singleton_feature(
    path: str,
    repo_root: Path | str | None = None,
) -> "DeveloperFeature | None":
    """Synthesize a deterministic feature for a size-1 cluster.

    Returns ``None`` to skip (the path is pure scaffolding and should
    not surface as a feature). Otherwise returns a fully-built
    :class:`DeveloperFeature` with ``confidence="low"``,
    ``sources=["singleton-synth"]`` and a name derived purely from
    the path structure.

    Rules (in order):

      1. Root-level dot-files (``.eslintrc``, ``.gitignore``,
         ``.prettierrc`` etc.) → ``None``. These are tooling config,
         not features.
      2. Root-level files whose basename is a known dependency
         manifest (``package.json``, ``Cargo.toml``, …) → ``None``.
         The Stage 1 package-anchor extractor already handles those.
      3. Otherwise: build the name from meaningful path components.
         Drop noise tokens (``src``, ``app``, ``lib``, ``main``,
         ``index``). Kebab-case the result.

    The function is pure: same input → same output. No I/O, no
    network, no LLM.

    Examples:
        ``apps/admin/providers/store.provider.tsx``
            → ``admin-providers-store-provider``
        ``apps/api/.coveragerc``
            → ``api-coveragerc``
        ``.env.example``
            → ``env-example``
        ``.gitignore``
            → ``None``
        ``package.json``
            → ``None``
    """
    # Local import to avoid the module-level cycle with stage_2 -> base.
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

    _ = repo_root  # signature symmetry; pure path heuristic doesn't need it

    if not isinstance(path, str) or not path:
        return None

    pp = PurePosixPath(path)
    parts = [p for p in pp.parts if p]
    if not parts:
        return None
    basename = parts[-1]

    is_root = len(parts) == 1

    # Rule 1: root-level dot-files
    if is_root and basename.startswith("."):
        # The stem after the leading dot ("eslintrc" from ".eslintrc",
        # "env.example" from ".env.example"). We treat dot-stems with
        # additional structure as still emit-able (e.g. ``.env.example``
        # → ``env-example``) — but pure tool configs like ``.eslintrc``
        # / ``.gitignore`` / ``.prettierrc`` are skipped.
        stem_after_dot = basename[1:]
        if "." not in stem_after_dot:
            # Pure dot-file with no further structure → tooling config.
            return None
        # Fall through to the naming logic below; the leading dot will
        # be stripped by the kebab pass.

    # Rule 2: root-level dependency manifest
    if is_root:
        bn_lower = basename.lower()
        if bn_lower in _MANIFEST_FILENAMES:
            return None
        # Generic root configs we don't have a manifest entry for
        # (e.g. ``vite.config.ts``, ``next.config.js``) DO synthesize —
        # those genuinely tell a developer something about the repo's
        # surface. We only suppress the well-known manifest names above.

    # Rule 3: build the name from meaningful parts. Drop noise tokens.
    #
    # Leaf-stem strategy:
    #   * For a "normal" file (``store.provider.tsx``), strip the
    #     final extension → ``store.provider`` and split on ``.`` so
    #     every chunk becomes a token (``store``, ``provider``).
    #   * For a leaf that starts with a dot (``.coveragerc``,
    #     ``.env.example``), the leading dot is part of the
    #     filename — treat it as a tooling/config marker, drop it,
    #     and keep ALL remaining dot-chunks as tokens. So
    #     ``.env.example`` → ``env``, ``example`` and
    #     ``.coveragerc`` → ``coveragerc``.
    if basename.startswith("."):
        # The leading-dot filename: drop the dot, then treat the
        # whole remainder as the source of tokens (do NOT strip the
        # last ``.``-chunk as an extension).
        leaf_tokens_source = basename.lstrip(".")
    else:
        # Normal filename: strip the final extension only (so
        # ``store.provider.tsx`` → ``store.provider``).
        leaf_tokens_source = basename
        if "." in leaf_tokens_source:
            leaf_tokens_source = leaf_tokens_source.rsplit(".", 1)[0]

    components: list[str] = []
    for part in parts[:-1]:
        token = _kebab_token(part)
        if not token or token in _NOISE_TOKENS:
            continue
        components.append(token)

    # The leaf may itself contain ``.`` separators
    # (``store.provider`` or ``env.example``) — keep them all.
    for chunk in leaf_tokens_source.split("."):
        token = _kebab_token(chunk)
        if not token or token in _NOISE_TOKENS:
            continue
        components.append(token)

    if not components:
        return None

    # Dedup adjacent duplicates (``api/api/handler`` → ``api-handler``)
    # without changing order.
    deduped: list[str] = []
    for c in components:
        if deduped and deduped[-1] == c:
            continue
        deduped.append(c)

    name = "-".join(deduped)
    if not name:
        return None
    if name in _NOISE_TOKENS:
        # The whole path collapsed to one noise token after stripping.
        return None

    return DeveloperFeature(
        name=name,
        paths=(path,),
        sources=["singleton-synth"],
        confidence="low",
        rationale="stage-4-singleton-synth",
    )


# ── Public entry point ────────────────────────────────────────────────


def cluster_residual_paths(paths: Iterable[str]) -> list[ResidualCluster]:
    """Group ``paths`` by structural signature.

    The returned list is sorted by cluster key so two runs on the same
    input produce identical output (important for cached A/B testing).
    Empty input returns an empty list.
    """
    bucketed: dict[ClusterKey, list[str]] = {}
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
    "ClusterKey",
    "ResidualCluster",
    "SAMPLE_CAP",
    "cluster_residual_paths",
    "synthesize_singleton_feature",
]
