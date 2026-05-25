"""Feature deduplication — hybrid (Sprint 9e).

Critique + flow-critique aggregators add features without checking
whether a semantic equivalent already exists. Primary scan can also
emit multiple slugs for the same domain (documenso ships three
"Auth" features from different packages). The result is a feature
map with high recall but low precision because the same product
capability appears under 2-4 different names.

Two-phase dedup:

  1. Deterministic — token-Jaccard similarity. Pairs with Jaccard
     ≥ ``HIGH_THRESHOLD`` are merged with no LLM cost.

  2. LLM verification — pairs in the ambiguous band
     ``[AMBIGUOUS_LOW, HIGH_THRESHOLD)`` are sent to the LLM in a
     single batched call. ``yes/no`` per pair decides the merge.

Merge target ("canonical") is chosen by a deterministic priority:

  protected > primary > critique > flow-critique
  tie-break: more paths > more flows > longer display name

When merging, all paths and flows from the loser are unioned into the
canonical; if any input was ``protected``, the canonical inherits
``protected=True`` (with ``protection_reason="multi-anchor"`` when
reasons differ).

No persistence, no per-repo tuning, no ground-truth seeding —
complies with ``rule-cold-scan``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Thresholds (scale-invariant, not corpus-tuned) ──────────────────
#
# Env-var override (Sprint 11b — for scan-experimenter sweeps):
#   FAULTLINE_DEDUP_HIGH_THRESHOLD  (default 0.6)
#   FAULTLINE_DEDUP_AMBIGUOUS_LOW   (default 0.4)
import os as _os

HIGH_THRESHOLD = float(_os.environ.get("FAULTLINE_DEDUP_HIGH_THRESHOLD", "0.6"))
AMBIGUOUS_LOW = float(_os.environ.get("FAULTLINE_DEDUP_AMBIGUOUS_LOW", "0.4"))


# Stop-words removed before computing Jaccard. Generic vocab, no
# per-repo names per ``rule-no-repo-specific-paths``.
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "for", "with", "in", "on",
    "to", "is", "are", "be", "by", "at", "as", "it", "this", "that",
    "flow", "manage", "handle", "do", "use",
    "feature", "features", "service", "system", "module", "modules",
    "api", "ui", "ux",   # too generic — appear in 50%+ features
})


# Discovery method priority (higher = preferred as canonical).
_DISCOVERY_PRIORITY = {
    "primary": 100,
    "critique": 50,
    "flow-critique": 30,
}


@dataclass
class DedupStats:
    pairs_high: int = 0          # deterministic merges
    pairs_ambiguous: int = 0     # sent to LLM
    pairs_llm_merged: int = 0    # LLM said "yes"
    features_before: int = 0
    features_after: int = 0
    clusters_merged: int = 0


# ── Tokenisation ────────────────────────────────────────────────────


def _tokens(name: str) -> set[str]:
    """Lowercase tokens of a feature name with stop-words removed.
    Splits on ``-_/`` and whitespace; tokens shorter than 3 chars are
    dropped (catches 1-2 char noise like "ee", "v1") unless they're
    a recognised abbreviation.
    """
    parts = re.split(r"[-_/\s.]+", name.lower())
    out: set[str] = set()
    for p in parts:
        if not p or p in _STOP_WORDS:
            continue
        if len(p) < 3 and p not in {"jwt", "sso", "mfa", "otp", "rpc"}:
            continue
        # Simple stem — drop trailing 's' for plural / singular merge
        if len(p) > 4 and p.endswith("s") and not p.endswith("ss"):
            p = p[:-1]
        out.add(p)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    return len(inter) / len(a | b)


# ── Canonical pick ──────────────────────────────────────────────────


def _pick_canonical(features):
    """Pick the feature that should "absorb" the others in a cluster.

    Priority: protected > primary > critique > flow-critique.
    Tie-break: more paths > more flows > longer human display name.
    """
    def key(f):
        prot_pri = 200 if f.protected else 0
        disco_pri = _DISCOVERY_PRIORITY.get(f.discovery_method, 0)
        path_count = len(f.paths)
        flow_count = len(f.flows)
        display_len = len(f.display_name or f.name)
        return (prot_pri + disco_pri, path_count, flow_count, display_len)
    return max(features, key=key)


def _merge_into(canonical, others) -> None:
    """Union paths/flows from each non-canonical feature into the
    canonical. Marks canonical protected when any input was.
    """
    canonical_paths = set(canonical.paths)
    canonical_flow_names = {fl.name for fl in canonical.flows}
    any_other_protected = False
    other_reasons: set[str] = set()
    if canonical.protected and canonical.protection_reason:
        other_reasons.add(canonical.protection_reason)
    for o in others:
        if o.protected:
            any_other_protected = True
            if o.protection_reason:
                other_reasons.add(o.protection_reason)
        # union paths (preserve order on canonical side)
        for p in o.paths:
            if p not in canonical_paths:
                canonical.paths.append(p)
                canonical_paths.add(p)
        # union flows (by name)
        for fl in o.flows:
            if fl.name not in canonical_flow_names:
                canonical.flows.append(fl)
                canonical_flow_names.add(fl.name)
        canonical.total_commits += o.total_commits
        canonical.bug_fixes += o.bug_fixes
    canonical.bug_fix_ratio = canonical.bug_fixes / max(canonical.total_commits, 1)
    if any_other_protected or canonical.protected:
        canonical.protected = True
        if len(other_reasons) >= 2:
            canonical.protection_reason = "multi-anchor"
        elif other_reasons:
            canonical.protection_reason = next(iter(other_reasons))


# ── Clustering ──────────────────────────────────────────────────────


def _cluster_by_jaccard(features, threshold: float) -> list[list]:
    """Build connected-component clusters of features whose
    pairwise Jaccard ≥ threshold. O(N^2) — fine for N up to ~200.
    """
    n = len(features)
    if n <= 1:
        return [[f] for f in features]
    token_sets = [_tokens(f.display_name or f.name) for f in features]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if _jaccard(token_sets[i], token_sets[j]) >= threshold:
                union(i, j)

    clusters: dict[int, list] = {}
    for i, f in enumerate(features):
        clusters.setdefault(find(i), []).append(f)
    return list(clusters.values())


def _ambiguous_pairs(features) -> list[tuple]:
    """Return list of (feature_i, feature_j, jaccard) pairs where
    jaccard is in [AMBIGUOUS_LOW, HIGH_THRESHOLD). Caller may send
    these to an LLM for verification.
    """
    n = len(features)
    token_sets = [_tokens(f.display_name or f.name) for f in features]
    out = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = _jaccard(token_sets[i], token_sets[j])
            if AMBIGUOUS_LOW <= sim < HIGH_THRESHOLD:
                out.append((features[i], features[j], sim))
    return out


# ── LLM verification ────────────────────────────────────────────────


_LLM_SYSTEM_PROMPT = """\
You are deciding which feature names represent the SAME product
capability versus DIFFERENT capabilities. You receive a list of
pairs. For each pair, respond ``yes`` (same capability — merge) or
``no`` (different capabilities — keep separate).

Reason about meaning, not literal token overlap.
- "Background Tasks" and "Background Jobs" → YES (same)
- "Response Compress" and "Content Encoding" → YES (same; encoding
  is the HTTP term for compression in this context)
- "Auth" and "OAuth" → NO (OAuth is a specific auth protocol)
- "Document Signing" and "Sign Document" → YES (same)
- "Templates" and "Document Templates" → YES (same)
- "User Management" and "Admin Panel" → NO (different scopes)

Return JSON only — no prose, no markdown fences. Schema:
{
  "decisions": [
    {"pair_id": 0, "verdict": "yes"|"no", "reason": "<one short>"},
    ...
  ]
}
"""


def _ask_llm_for_pairs(llm, pairs) -> dict[int, bool]:
    """Send ambiguous pairs to LLM. Returns
    ``{pair_id: True_if_merge_else_False}``. Empty dict on failure.
    """
    if not pairs:
        return {}
    items = [
        {
            "pair_id": i,
            "a": (p[0].display_name or p[0].name),
            "b": (p[1].display_name or p[1].name),
        }
        for i, p in enumerate(pairs)
    ]
    user = "Pairs to verify:\n\n" + json.dumps(
        {"pairs": items}, indent=2, ensure_ascii=False,
    )
    try:
        response = llm.complete(
            system=_LLM_SYSTEM_PROMPT, user=user, max_tokens=2048,
        )
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("feature_dedup: LLM call failed (%s)", exc)
        return {}
    text = response.text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("feature_dedup: invalid JSON from LLM")
        return {}
    out: dict[int, bool] = {}
    for entry in data.get("decisions", []) or []:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pair_id")
        verdict = (entry.get("verdict") or "").lower()
        if isinstance(pid, int) and verdict in {"yes", "no"}:
            out[pid] = verdict == "yes"
    return out


# ── Top-level orchestrator ──────────────────────────────────────────


def dedup_features(
    feature_map,
    *,
    llm=None,
    high_threshold: float = HIGH_THRESHOLD,
    ambiguous_low: float = AMBIGUOUS_LOW,
):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    DedupStats)``. Input ``feature_map`` is NEVER mutated.

    ``llm``: optional ``LlmClient``. When None, only the deterministic
    pass runs (no ambiguous-pair verification).
    """
    new_fm = feature_map.model_copy(deep=True)
    feature_map = new_fm  # operate on the copy below
    stats = DedupStats(features_before=len(feature_map.features))

    # Phase 1 — deterministic high-Jaccard merge.
    clusters = _cluster_by_jaccard(feature_map.features, high_threshold)
    survivors: list = []
    for cluster in clusters:
        if len(cluster) == 1:
            survivors.append(cluster[0])
            continue
        canonical = _pick_canonical(cluster)
        others = [f for f in cluster if f is not canonical]
        _merge_into(canonical, others)
        survivors.append(canonical)
        stats.clusters_merged += 1
        stats.pairs_high += len(others)
    feature_map.features = survivors

    # Phase 2 — LLM verification for ambiguous pairs.
    if llm is not None:
        pairs = _ambiguous_pairs(feature_map.features)
        if pairs:
            stats.pairs_ambiguous = len(pairs)
            verdicts = _ask_llm_for_pairs(llm, pairs)
            merge_pairs = [
                pairs[pid] for pid, yes in verdicts.items() if yes
            ]
            stats.pairs_llm_merged = len(merge_pairs)
            # Apply LLM-approved merges via union-find on the pairs.
            if merge_pairs:
                id_to_feat = {id(f): f for f in feature_map.features}
                parent = {id(f): id(f) for f in feature_map.features}

                def find(x):
                    while parent[x] != x:
                        parent[x] = parent[parent[x]]
                        x = parent[x]
                    return x

                def union(a, b):
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[ra] = rb

                for a, b, _ in merge_pairs:
                    union(id(a), id(b))
                clusters_llm: dict[int, list] = {}
                for f in feature_map.features:
                    clusters_llm.setdefault(find(id(f)), []).append(f)
                survivors2: list = []
                for cl in clusters_llm.values():
                    if len(cl) == 1:
                        survivors2.append(cl[0])
                        continue
                    canonical = _pick_canonical(cl)
                    others = [f for f in cl if f is not canonical]
                    _merge_into(canonical, others)
                    survivors2.append(canonical)
                    stats.clusters_merged += 1
                feature_map.features = survivors2

    stats.features_after = len(feature_map.features)
    return new_fm, stats


__all__ = [
    "AMBIGUOUS_LOW",
    "DedupStats",
    "HIGH_THRESHOLD",
    "_jaccard",
    "_tokens",
    "_pick_canonical",
    "dedup_features",
]
