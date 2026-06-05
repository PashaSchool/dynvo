"""File-to-canonical-feature assignment cache.

After every successful scan we save ``{file_path: canonical_name}``
to ``~/.faultline/assignments-{repo_slug}.json``. The next scan
loads it before running ``deep_scan`` and uses it to renormalize
fresh feature names whose file set overwhelmingly belongs to a
previous canonical — e.g. Sonnet returns ``prisma-database`` with
175 files of which 170 were previously assigned to ``prisma``,
so we rename it to ``prisma`` before sub_decompose / critique
even see it.

This is the last piece needed for cross-run stability convergence.
The Sonnet prompt hint (Step 1) tells the LLM to prefer canonical
names; this catches the cases where Sonnet ignores the hint.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from faultline.cache.backend import CacheBackend
    from faultline.llm.sonnet_scanner import DeepScanResult


logger = logging.getLogger(__name__)


_RENAME_THRESHOLD = 0.6  # 60% of files must map to a single previous canonical


def _slug_for_root(repo_root: Path) -> str:
    """Stable, filesystem-safe identifier for a repo path.

    This is the cache KEY for the ``assignment`` kind — unchanged from
    the legacy ``assignments-<slug>.json`` filename so dev fs caches
    stay valid.
    """
    return repo_root.resolve().name.lower().replace(" ", "-")


def _resolve_backend(cache_backend: "CacheBackend | None") -> "CacheBackend":
    if cache_backend is not None:
        return cache_backend
    from faultline.cache import get_cache_backend

    return get_cache_backend()


def load_assignments(
    repo_root: Path, *, cache_backend: "CacheBackend | None" = None,
) -> dict[str, str]:
    """Return ``{file_path: canonical_name}`` from previous scan, or {}."""
    backend = _resolve_backend(cache_backend)
    data = backend.get("assignment", _slug_for_root(repo_root))
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_assignments(
    result: "DeepScanResult",
    repo_root: Path,
    *,
    cache_backend: "CacheBackend | None" = None,
) -> int:
    """Write the current scan's file → feature mapping. Returns count saved.

    Sprint 1 (2026-05-23): when the ``FAULTLINES_PRODUCTION=1`` env
    var is set, this becomes a no-op so SaaS workers on shared hosts
    don't accumulate per-repo state that would break cold-scan
    semantics across tenants. See
    ``faultline.pipeline_v2.production_mode``.
    """
    from faultline.pipeline_v2.production_mode import production_mode_enabled
    if production_mode_enabled():
        logger.info(
            "assignments: FAULTLINES_PRODUCTION=1 — skipping save (cold-scan mode)",
        )
        return 0
    backend = _resolve_backend(cache_backend)
    inverted: dict[str, str] = {}
    for feature_name, files in result.features.items():
        for f in files:
            inverted[f] = feature_name
    # Stable ordering preserves byte-identical cache bodies across runs.
    inverted = {k: inverted[k] for k in sorted(inverted)}
    backend.set("assignment", _slug_for_root(repo_root), inverted)
    logger.info(
        "assignments: saved %d file→feature mappings (slug=%s)",
        len(inverted), _slug_for_root(repo_root),
    )
    return len(inverted)


def renormalize_features(
    result: "DeepScanResult",
    prev_assignments: dict[str, str],
    *,
    threshold: float = _RENAME_THRESHOLD,
    locked_canonicals: frozenset[str] | None = None,
) -> int:
    """Rename detected features to their previous canonical when possible.

    For each detected feature F with files [f1, f2, ...]:
      - Count how many ``f_i`` were assigned to each canonical in the
        previous scan.
      - If the top previous-canonical X covers ≥ ``threshold`` of F's
        files AND X != F.name, rename F → X. Merge into X if X already
        exists in the result.

    ``locked_canonicals`` (optional): when provided, X is only used
    as a rename target if X is in this set. Prevents drift from
    one ephemeral name to another. Pass
    ``RepoConfig.all_canonical_names()`` here.

    Returns the number of features renamed.
    """
    if not prev_assignments:
        return 0
    locked = locked_canonicals or frozenset()
    renamed = 0
    # Iterate over a snapshot — we mutate result.features below.
    for name in list(result.features.keys()):
        files = result.features.get(name) or []
        if not files:
            continue
        prev_names = [
            prev_assignments[f] for f in files if f in prev_assignments
        ]
        if not prev_names:
            continue
        top, top_count = Counter(prev_names).most_common(1)[0]
        coverage = top_count / len(files)
        if coverage < threshold:
            continue
        if top == name:
            continue
        # Only rename to a canonical the user / engine has locked.
        # Without this guard a transient T_n name could become the
        # forever-canonical via the cache.
        if locked and top not in locked:
            continue
        # Merge
        merged: dict[str, None] = {
            f: None for f in result.features.get(top, [])
        }
        for f in result.features.pop(name):
            merged[f] = None
        result.features[top] = sorted(merged)
        if name in result.descriptions:
            desc = result.descriptions.pop(name)
            result.descriptions.setdefault(top, desc)
        if name in result.flows:
            target = result.flows.setdefault(top, [])
            seen = set(target)
            for fn in result.flows.pop(name):
                if fn not in seen:
                    target.append(fn)
                    seen.add(fn)
        if name in result.flow_descriptions:
            target_d = result.flow_descriptions.setdefault(top, {})
            for fn, desc in result.flow_descriptions.pop(name).items():
                target_d.setdefault(fn, desc)
        logger.info(
            "assignments: renormalized %r → %r (%.0f%% coverage, %d files)",
            name, top, coverage * 100, len(files),
        )
        renamed += 1
    return renamed
