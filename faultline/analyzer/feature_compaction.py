"""Sprint 19.5 — deterministic noise-feature filter + reattribution.

Some scan outputs include features whose names are conventional commit
verbs (``improvement``, ``fix``, ``refactor``) or generic placeholders
(``ui``, ``apps``, ``lib``, ``utils``). These come from two upstream
sources:

  1. ``commit_prefix_enrichment_pass`` — when a project uses
     "improvement: do X" commit messages, the prefix becomes a feature.
  2. Heuristic detector picking up directory names that aren't true
     domain features.

Neither source is wrong per se — but the resulting features have low
domain content and pollute precision in eval / dashboard.

This module provides a **deterministic, no-LLM** filter that removes
features matching well-known noise patterns. The filter is intentionally
conservative (small whitelist of names) so it never drops a legitimate
domain feature.

Public surface
==============

    compact(features, *, return_stats=False) -> list[Feature]
        Returns a new feature list with noise removed. Files attached
        to removed features are NOT re-attributed (the eval doesn't
        need them; the pipeline has its own shared-infra fold-in).

Caller responsibility: the pipeline (or eval_run) decides whether to
apply this. It's not on by default — it's a filter that an evaluator
or an exporter can opt into.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Pure conventional commit verbs / placeholders — never legit features.
# Anything matching here is dropped regardless of size.
NEVER_FEATURES: frozenset[str] = frozenset({
    "improvement", "improvements", "fix", "fixes", "feat", "feats",
    "feature", "chore", "chores", "refactor", "perf",
    "hotfix", "bugfix", "patch", "patches", "cleanup", "tweak",
    "tweaks", "misc", "common", "base", "general", "shared",
    "util", "utils", "helper", "helpers", "core", "main", "lib",
    "libs",
})

# Test bucket names — never a feature.
TEST_NAMES: frozenset[str] = frozenset({
    "tests", "test", "api-tests", "e2e", "specs", "spec",
    "integration-tests", "unit-tests",
})

# Layer / placeholder names. Dropped only when small (< 200 paths) so a
# legitimately-named layer feature in a small repo isn't accidentally
# removed.
LAYER_NAMES: frozenset[str] = frozenset({
    "frontend", "backend", "ui", "public", "app", "apps",
    "client", "server-app",
})
LAYER_DROP_THRESHOLD = 200


def _is_noise(feature: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (is_noise, reason). Reason is None when not noise."""
    name = (feature.get("name") or "").strip().lower()
    if not name:
        return (True, "empty-name")
    if name in NEVER_FEATURES:
        return (True, "commit-verb")
    if name in TEST_NAMES:
        return (True, "tests")
    if name in LAYER_NAMES:
        n_paths = len(feature.get("paths") or [])
        if n_paths < LAYER_DROP_THRESHOLD:
            return (True, "layer-noise")
    # Heuristic: name is exactly the form `<word>` and the description
    # explicitly says "Detected from N commits with '<word>:' prefix"
    # AND the name is in NEVER_FEATURES / LAYER_NAMES — already covered
    # above. Don't add more — too easy to drop legit features.
    return (False, None)


def compact(
    features: list[dict[str, Any]],
    *,
    return_stats: bool = False,
    top_n: int | None = None,
    min_paths: int = 0,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter out noise features. Pure function — does not mutate input.

    Args:
        features: list of feature dicts (from feature-map JSON).
        return_stats: when True, return ``(kept, stats)``.
        top_n: optional cap on output count. After noise filtering,
            keep only the top-N features by path count. Long-tail
            fragments below the cut are dropped (they are usually
            sub-features that GT bundles into a parent).
        min_paths: drop features with fewer than this many paths.
            Use sparingly — small-but-real features (e.g. ``badges``
            in uptime-kuma with 1 file) are valid.

    Returns:
        ``list[Feature]`` (or tuple if ``return_stats``).
    """
    kept: list[dict[str, Any]] = []
    dropped: list[tuple[str, str, int]] = []
    for f in features:
        n_paths = len(f.get("paths") or [])
        is_noise, reason = _is_noise(f)
        if is_noise:
            dropped.append((f.get("name", "?"), reason or "?", n_paths))
            continue
        if min_paths and n_paths < min_paths:
            dropped.append((f.get("name", "?"), "below-min-paths", n_paths))
            continue
        kept.append(f)

    # Top-N truncation. Sort by path count desc; keep first top_n.
    if top_n is not None and len(kept) > top_n:
        kept_sorted = sorted(kept, key=lambda f: -len(f.get("paths") or []))
        truncated = kept_sorted[top_n:]
        kept = kept_sorted[:top_n]
        for t in truncated:
            dropped.append((
                t.get("name", "?"),
                "top-n-truncate",
                len(t.get("paths") or []),
            ))

    if return_stats:
        stats = {
            "n_kept": len(kept),
            "n_dropped": len(dropped),
            "dropped": dropped,
            "paths_dropped": sum(d[2] for d in dropped),
        }
        return (kept, stats)

    if dropped:
        logger.info(
            "feature_compaction: dropped %d noise features (%d paths): %s",
            len(dropped),
            sum(d[2] for d in dropped),
            ", ".join(f"{n}({r})" for n, r, _ in dropped[:5]),
        )

    return kept


# ── Reattribution (Variant C) ─────────────────────────────────────────


_STOPWORDS: frozenset[str] = frozenset({
    "api", "app", "apps", "web", "lib", "libs", "src", "dist", "build",
    "v1", "v2", "v3", "v4", "main", "core", "common", "shared", "utils",
    "util", "helper", "helpers", "service", "services", "manager",
    "managers", "module", "modules", "the", "and", "of", "for", "to",
})


def _tokens(name: str) -> set[str]:
    """Split a kebab-case name into significant tokens (>2 chars, non-stop)."""
    parts = name.lower().replace("_", "-").replace("/", "-").split("-")
    return {p for p in parts if len(p) > 2 and p not in _STOPWORDS}


def _path_prefix(paths: list[str], depth: int = 2) -> str:
    """Most common N-segment path prefix among the feature's files."""
    if not paths:
        return ""
    prefixes: dict[str, int] = {}
    for p in paths:
        segs = p.split("/")[:depth]
        if not segs:
            continue
        pre = "/".join(segs)
        prefixes[pre] = prefixes.get(pre, 0) + 1
    if not prefixes:
        return ""
    return max(prefixes.items(), key=lambda kv: kv[1])[0]


def _similarity(dropped: dict, kept: dict) -> float:
    """Score 0.0 - 1.0 of how well a dropped feature merges into kept.

    Combines:
      - token overlap on names (60%)
      - path-prefix overlap (40%)
    """
    d_tokens = _tokens(dropped.get("name", ""))
    k_tokens = _tokens(kept.get("name", ""))
    if not d_tokens or not k_tokens:
        token_score = 0.0
    else:
        common = d_tokens & k_tokens
        token_score = len(common) / max(len(d_tokens), len(k_tokens))

    d_prefix = _path_prefix(dropped.get("paths") or [])
    k_prefix = _path_prefix(kept.get("paths") or [])
    if d_prefix and k_prefix:
        prefix_score = 1.0 if d_prefix == k_prefix else (
            0.5 if d_prefix.split("/")[0] == k_prefix.split("/")[0] else 0.0
        )
    else:
        prefix_score = 0.0

    return 0.6 * token_score + 0.4 * prefix_score


def _merge_into(target: dict, source: dict) -> None:
    """In-place merge ``source`` into ``target``. Preserves invariants:

    - paths: union (dedupe)
    - total_commits / bug_fixes: sum
    - bug_fix_ratio: recompute
    - flows: extend with non-duplicate names
    - aliases: append source's name (S20 — for eval coverage recovery)
    - other numeric fields untouched (target's value wins)
    """
    # S20 — preserve dropped feature's name as alias so eval / dashboard
    # can still match against maintainer's GT vocabulary even after
    # reattribution. Recovers coverage lost in S19.5 compaction.
    aliases = list(target.get("aliases") or [])
    src_name = (source.get("name") or "").strip()
    if src_name and src_name != target.get("name") and src_name not in aliases:
        aliases.append(src_name)
    for a in source.get("aliases") or []:
        if a and a != target.get("name") and a not in aliases:
            aliases.append(a)
    target["aliases"] = aliases

    target_paths = list(target.get("paths") or [])
    source_paths = list(source.get("paths") or [])
    seen = set(target_paths)
    for p in source_paths:
        if p not in seen:
            target_paths.append(p)
            seen.add(p)
    target["paths"] = target_paths

    t_commits = int(target.get("total_commits") or 0)
    s_commits = int(source.get("total_commits") or 0)
    t_fixes = int(target.get("bug_fixes") or 0)
    s_fixes = int(source.get("bug_fixes") or 0)
    target["total_commits"] = t_commits + s_commits
    target["bug_fixes"] = t_fixes + s_fixes
    target["bug_fix_ratio"] = (
        target["bug_fixes"] / target["total_commits"]
        if target["total_commits"] else 0.0
    )

    t_flow_names = {(fl.get("name") or "") for fl in (target.get("flows") or [])}
    new_flows = list(target.get("flows") or [])
    for fl in source.get("flows") or []:
        if fl.get("name") and fl["name"] not in t_flow_names:
            new_flows.append(fl)
            t_flow_names.add(fl["name"])
    target["flows"] = new_flows


def reattribute(
    features: list[dict],
    *,
    top_n: int,
    min_similarity: float = 0.15,
) -> tuple[list[dict], dict]:
    """Variant C — drop noise, cap at top-N, merge cut features into nearest kept.

    For each feature dropped by ``compact(top_n)``:
      1. Find best-similarity match among kept features.
      2. If similarity ≥ ``min_similarity``, merge into that kept feature
         (paths, bug_fixes, total_commits, flows all sum/extend).
      3. Else drop (no good merge target — pure noise).

    Returns:
        (compact_features, stats)
        ``stats`` shows how many were merged vs hard-dropped.
    """
    # First pass: noise filter only (keep size for full set).
    after_noise, _ = compact(features, return_stats=True)
    # Sort by path count descending; split top-N from tail.
    after_noise_sorted = sorted(
        after_noise, key=lambda f: -len(f.get("paths") or []),
    )
    if len(after_noise_sorted) <= top_n:
        return (
            [dict(f) for f in after_noise_sorted],
            {"merged": 0, "hard_dropped": 0, "kept": len(after_noise_sorted)},
        )

    kept_originals = after_noise_sorted[:top_n]
    tail = after_noise_sorted[top_n:]

    # Deep-copy kept so we don't mutate original feature dicts.
    kept = [dict(f) for f in kept_originals]
    # Re-deep-copy nested mutable fields we'll merge into.
    for f in kept:
        f["paths"] = list(f.get("paths") or [])
        f["flows"] = list(f.get("flows") or [])

    merged_count = 0
    hard_dropped = 0
    for d in tail:
        best_idx = -1
        best_score = 0.0
        for idx, k in enumerate(kept):
            score = _similarity(d, k)
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx >= 0 and best_score >= min_similarity:
            _merge_into(kept[best_idx], d)
            merged_count += 1
        else:
            hard_dropped += 1

    return (kept, {
        "merged": merged_count,
        "hard_dropped": hard_dropped,
        "kept": len(kept),
    })
