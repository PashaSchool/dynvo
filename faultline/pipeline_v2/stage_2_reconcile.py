"""Stage 2 — anchor reconciliation.

Merges :class:`AnchorCandidate` instances from Stage 1 across all
sources, resolves cross-extractor name conflicts via a stable
priority rule, attributes every tracked file to exactly one feature,
and emits :class:`DeveloperFeature` records ready for Stage 3 flow
detection.

Algorithm (per the ``pipeline-architecture`` skill):

  1. **Merge by name overlap**: candidates whose slugs are
     token-set-identical OR have Jaccard similarity ≥ 0.7 are merged
     into one feature.
  2. **Name priority on conflict**: when merged candidates disagree on
     slug, pick by ``package > route > mvc > schema > config``.
  3. **Primary path attribution**: each file belongs to exactly one
     feature. Conflicts resolved by source priority (the feature with
     the highest-priority source wins; the loser drops the path).
  4. **Confidence**: ``high`` if ≥2 sources agreed, ``medium`` if 1.
     ``low`` is reserved for Stage 4 LLM fallback.
  5. **Optional LLM 2nd-opinion** (Haiku 4.5) for ambiguous pairs
     (Jaccard 0.3–0.6) — disabled by default. Cost capped at one
     short call per ambiguous pair.

Stage 2 is otherwise pure / deterministic. Idempotent on identical
input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Source priority ────────────────────────────────────────────────────────
#
# Higher = more authoritative. Package names are the most stable
# semantic anchor (one upgrade per quarter at most); route slugs change
# when the URL changes (a few times per year); MVC controller names are
# refactor-sensitive; schema model names are renamed as the data model
# evolves; config-as-product anchors carry less product-vocabulary
# weight than the others.

_SOURCE_PRIORITY: dict[str, int] = {
    "package": 5,
    "route":   4,
    "mvc":     3,
    "schema":  2,
    "config":  1,
}
"""Stage 2 priority used when:

   - merged candidates disagree on the canonical slug
   - two features compete for the same file path
"""

_LOW_PRIORITY_DEFAULT = 0  # Unknown sources fall below all known ones.


def _priority(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, _LOW_PRIORITY_DEFAULT)


# ── Public output dataclass ────────────────────────────────────────────────


Confidence = Literal["high", "medium", "low"]


@dataclass
class DeveloperFeature:
    """Stage 2 output — one Layer 1 developer feature.

    Attributes:
        name: kebab-case slug, the merged canonical name.
        paths: files attributed to this feature (after dedup).
        sources: list of extractor sources that contributed
            (highest-priority source listed first).
        confidence: ``"high"`` for ≥2 sources, ``"medium"`` for 1.
            ``"low"`` is never emitted by Stage 2; Stage 4 owns it.
        display_name: optional Title Case label (set by Stage 5 if
            ``None``).
        rationale: human-readable merge explanation (debug only).
    """

    name: str
    paths: tuple[str, ...]
    sources: list[str]
    confidence: Confidence
    display_name: str | None = None
    rationale: str = ""
    # Per-source confidence_self values, kept for downstream tie-breaks.
    source_confidences: dict[str, float] = field(default_factory=dict)


@dataclass
class Stage2Result:
    """What Stage 2 returns to the caller.

    ``features`` is the primary output. ``unattributed`` lists the
    tracked files NOT claimed by any anchor — Stage 4 (LLM fallback)
    runs only over these.

    ``zero_path_drops_count`` (Sprint S4b) records the number of
    features that ended reconciliation with no path attribution and
    were dropped by the defensive zero-path filter. A non-zero count
    is a signal to inspect either Stage 1 (extractor emitted bad
    anchor) or the attribution rule (a path-sharing pattern that
    even the Fix-A zero-path-protection couldn't rescue).
    """

    features: list[DeveloperFeature]
    unattributed: list[str]
    # Free-form telemetry; Stage 7 reads this for ``scan_meta``.
    notes: list[str] = field(default_factory=list)
    zero_path_drops_count: int = 0
    zero_path_drops_sample: list[str] = field(default_factory=list)


# ── Similarity ─────────────────────────────────────────────────────────────


def _slug_tokens(slug: str) -> frozenset[str]:
    """Split a kebab-case slug into a token set, for Jaccard."""
    return frozenset(t for t in slug.split("-") if t)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _should_merge(a: str, b: str, *, threshold: float = 0.7) -> bool:
    """``True`` if two slugs should merge into one feature."""
    if a == b:
        return True
    ta, tb = _slug_tokens(a), _slug_tokens(b)
    if ta == tb:
        return True
    return _jaccard(ta, tb) >= threshold


# ── Union-find for merging candidates by name ──────────────────────────────


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _build_merge_groups(
    candidates: list[AnchorCandidate],
    *,
    jaccard_threshold: float = 0.7,
) -> list[list[int]]:
    """Return list of index lists; each inner list is one merge group."""
    n = len(candidates)
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _should_merge(
                candidates[i].name,
                candidates[j].name,
                threshold=jaccard_threshold,
            ):
                uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = uf.find(i)
        groups.setdefault(root, []).append(i)
    return list(groups.values())


# ── Optional LLM 2nd-opinion (Haiku) ───────────────────────────────────────


_HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


def _llm_pick_name(
    candidate_a: AnchorCandidate,
    candidate_b: AnchorCandidate,
) -> str | None:
    """Ask Haiku which of two ambiguous candidate names to keep.

    Returns the chosen slug, or ``None`` if the call failed / was
    inconclusive. Caller falls back to the priority rule on ``None``.

    This is the only LLM call Stage 2 ever makes, and it only fires
    when ``llm_reconcile=True`` AND the slug Jaccard sits in 0.3..0.6.
    """
    try:
        # Local import to keep ``anthropic`` from being a hard dep of
        # Stage 2 callers that don't enable the 2nd-opinion path.
        from anthropic import Anthropic  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("anthropic package not available; LLM reconcile skipped")
        return None

    prompt = (
        f"Two deterministic extractors produced candidate feature names "
        f"for overlapping code. Pick the SINGLE better kebab-case slug "
        f"to keep. Reply with ONLY the slug, no prose.\n\n"
        f"A ({candidate_a.source}): {candidate_a.name}\n"
        f"   sample paths: {list(candidate_a.paths)[:3]}\n"
        f"B ({candidate_b.source}): {candidate_b.name}\n"
        f"   sample paths: {list(candidate_b.paths)[:3]}\n"
    )

    try:
        client = Anthropic()
        msg = client.messages.create(
            model=_HAIKU_MODEL_ID,
            max_tokens=24,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (msg.content[0].text or "").strip()  # type: ignore[union-attr]
        # Sanitize: take first whitespace-separated token, lowercase,
        # validate it kebab-cases cleanly.
        first = text.splitlines()[0].strip().split()[0] if text else ""
        first = first.strip("`\"' .").lower()
        if first in {candidate_a.name, candidate_b.name}:
            return first
        return None
    except Exception as exc:  # noqa: BLE001 — LLM failure is non-fatal
        logger.warning("LLM 2nd-opinion failed: %s", exc)
        return None


# ── Group → DeveloperFeature ───────────────────────────────────────────────


def _pick_canonical_name(
    group: list[AnchorCandidate],
    *,
    llm_reconcile: bool,
    llm_call: callable = _llm_pick_name,
) -> tuple[str, str | None]:
    """Choose the canonical slug for a merged group.

    Returns ``(slug, llm_decision_note)``. ``llm_decision_note`` is
    ``None`` when no LLM call was made.
    """
    if len(group) == 1:
        return group[0].name, None

    # Are all the slugs already identical? Trivial case.
    distinct = {c.name for c in group}
    if len(distinct) == 1:
        return next(iter(distinct)), None

    # Sort by (priority desc, confidence_self desc, name asc) so the
    # default winner is deterministic.
    ranked = sorted(
        group,
        key=lambda c: (-_priority(c.source), -c.confidence_self, c.name),
    )
    leader = ranked[0]

    # LLM 2nd-opinion gating: only when llm_reconcile=True AND the
    # leader vs runner-up Jaccard is "ambiguous" (0.3..0.6).
    if llm_reconcile and len(ranked) >= 2:
        runner = ranked[1]
        j = _jaccard(_slug_tokens(leader.name), _slug_tokens(runner.name))
        if 0.3 <= j <= 0.6:
            picked = llm_call(leader, runner)
            if picked is not None:
                return picked, f"llm picked {picked!r} from "\
                               f"{{{leader.name!r}, {runner.name!r}}}"

    return leader.name, None


def _build_feature_from_group(
    group: list[AnchorCandidate],
    canonical_name: str,
    rationale_extra: str | None,
) -> DeveloperFeature:
    """Bundle a merged group into a :class:`DeveloperFeature`.

    Note: ``paths`` here is the *claim union* — the cross-feature
    attribution pass runs separately and may strip paths that lose to
    a higher-priority feature.
    """
    sources_ranked = sorted(
        {c.source for c in group},
        key=lambda s: (-_priority(s), s),
    )
    paths: set[str] = set()
    display_name: str | None = None
    source_conf: dict[str, float] = {}
    rationale_bits: list[str] = []
    for c in group:
        paths.update(c.paths)
        if display_name is None and c.display_name:
            display_name = c.display_name
        source_conf[c.source] = max(
            source_conf.get(c.source, 0.0), c.confidence_self,
        )
        if c.rationale:
            rationale_bits.append(f"[{c.source}] {c.rationale}")

    confidence: Confidence = "high" if len(sources_ranked) >= 2 else "medium"
    rationale = " | ".join(rationale_bits)
    if rationale_extra:
        rationale = (rationale + " | " + rationale_extra) if rationale else rationale_extra

    return DeveloperFeature(
        name=canonical_name,
        paths=tuple(sorted(paths)),
        sources=sources_ranked,
        confidence=confidence,
        display_name=display_name,
        rationale=rationale,
        source_confidences=source_conf,
    )


# ── Cross-feature path attribution ─────────────────────────────────────────


def _attribute_paths(
    features: list[DeveloperFeature],
) -> list[DeveloperFeature]:
    """Each file gets ONE owner — UNLESS that would orphan a feature.

    Conflicts (file appears in 2+ feature ``paths``) resolve by:

      1. highest source priority among the feature's sources
      2. higher overall confidence (``high`` > ``medium``)
      3. more total paths (the larger feature, as a tie-breaker)
      4. lexicographic name (final deterministic fallback)

    Losers drop the contested path; they keep their other paths.

    Sprint S4b — zero-path-protection: when stripping a contested path
    would leave the loser feature with ZERO remaining paths, we KEEP
    the contested path on the loser. This is the structural fix for
    the "URL ghost" bug where multiple route slugs legitimately share
    one source file (e.g. a single Fastify plugin file declaring
    routes for ``/bitbucket`` + ``/gitlab`` + ``/github``). Without
    this guard, two of the three slugs end up as zero-path "ghost"
    features that downstream stages cannot attribute coverage, blame,
    or flows to. Keeping the path on the loser preserves provenance
    and matches the real-world co-location pattern.

    The winner always retains the path, so the canonical owner is
    still single-valued for downstream consumers that care about
    primary attribution. Path-sharing here only widens the loser's
    visibility window; it never reassigns winnership.
    """
    # Build an index ``path → list of feature indices that claim it``.
    claims: dict[str, list[int]] = {}
    for idx, f in enumerate(features):
        for p in f.paths:
            claims.setdefault(p, []).append(idx)

    if not claims:
        return features

    # Pass 1 — provisional strip set, ranking-only.
    proposed_strip: dict[int, set[str]] = {}
    for path, idx_list in claims.items():
        if len(idx_list) == 1:
            continue
        # rank features
        def _rank_key(i: int) -> tuple[int, int, int, str]:
            f = features[i]
            best_src_prio = max(_priority(s) for s in f.sources) if f.sources else 0
            conf_rank = {"high": 2, "medium": 1, "low": 0}[f.confidence]
            return (-best_src_prio, -conf_rank, -len(f.paths), f.name)

        ranked = sorted(idx_list, key=_rank_key)
        for loser in ranked[1:]:
            proposed_strip.setdefault(loser, set()).add(path)

    if not proposed_strip:
        return features

    # Pass 2 — zero-path protection: cancel strips that would orphan
    # the loser. A "loser" feature that would end with len(paths)==0
    # keeps ALL its contested paths so it stays attributable. We
    # process in a deterministic order (sorted feature index) so the
    # outcome is reproducible across runs.
    strip: dict[int, set[str]] = {}
    for idx in sorted(proposed_strip):
        f = features[idx]
        contested = proposed_strip[idx]
        survivors = tuple(p for p in f.paths if p not in contested)
        if not survivors:
            # Stripping would orphan this feature; keep the contested
            # paths instead. Loser shares ownership with winner(s).
            continue
        strip[idx] = contested

    if not strip:
        return features

    rebuilt: list[DeveloperFeature] = []
    for idx, f in enumerate(features):
        if idx not in strip:
            rebuilt.append(f)
            continue
        new_paths = tuple(p for p in f.paths if p not in strip[idx])
        rebuilt.append(
            DeveloperFeature(
                name=f.name,
                paths=new_paths,
                sources=f.sources,
                confidence=f.confidence,
                display_name=f.display_name,
                rationale=f.rationale,
                source_confidences=f.source_confidences,
            ),
        )
    return rebuilt


# ── Public entry point ─────────────────────────────────────────────────────


def stage_2_reconcile(
    candidates_by_source: dict[str, list[AnchorCandidate]],
    ctx: "ScanContext",
    *,
    llm_reconcile: bool = False,
    jaccard_threshold: float = 0.7,
    _llm_call: callable = _llm_pick_name,
) -> Stage2Result:
    """Reconcile cross-extractor anchor candidates.

    Args:
        candidates_by_source: Stage 1 output. The ``_errors`` key (if
            present) is ignored here — Stage 7 surfaces it.
        ctx: Stage 0 output. Needed for the full tracked-file list so
            we can compute the ``unattributed`` residual.
        llm_reconcile: When True, ambiguous name pairs (Jaccard 0.3..0.6)
            are sent to Haiku 4.5 for a 2nd-opinion. When False (default)
            the priority rule alone resolves the tie. No LLM calls are
            made when False.
        jaccard_threshold: similarity above which two slugs merge into
            one feature (default 0.7 per the spec).
        _llm_call: injectable LLM-call function for tests. Default is
            :func:`_llm_pick_name`; tests pass a stub.

    Returns:
        :class:`Stage2Result` with the merged features and the
        unattributed file list.
    """
    # Drop the sentinel ``_errors`` key before processing.
    flat: list[AnchorCandidate] = []
    for source, cands in candidates_by_source.items():
        if source == "_errors":
            continue
        for c in cands:
            if isinstance(c, AnchorCandidate):
                flat.append(c)

    if not flat:
        return Stage2Result(
            features=[],
            unattributed=list(ctx.tracked_files),
            notes=["no Stage 1 candidates — nothing to reconcile"],
        )

    # 1) Merge candidates by name similarity.
    groups = _build_merge_groups(flat, jaccard_threshold=jaccard_threshold)

    # 2) Per group: choose canonical name + assemble DeveloperFeature.
    features: list[DeveloperFeature] = []
    notes: list[str] = []
    for group_indices in groups:
        group = [flat[i] for i in group_indices]
        canonical, llm_note = _pick_canonical_name(
            group,
            llm_reconcile=llm_reconcile,
            llm_call=_llm_call,
        )
        if llm_note:
            notes.append(llm_note)
        features.append(_build_feature_from_group(group, canonical, llm_note))

    # 3) Resolve cross-feature file conflicts.
    features = _attribute_paths(features)

    # 4) Sprint S4b — defensive zero-path drop. Any feature that
    #    ended reconciliation with NO paths cannot be attributed to
    #    files in the working tree, so downstream stages (Stage 5
    #    naming discipline, Stage 6 commit/coverage enrichment,
    #    Stage 5.3 sibling-collapse, Stage 5.5 bipartite) all have
    #    nothing to operate on. We drop them here and record the
    #    sample for telemetry. Pure structural — no thresholds.
    zero_path = [f for f in features if not f.paths]
    if zero_path:
        zero_path_names = [f.name for f in zero_path]
        features = [f for f in features if f.paths]
        notes.append(
            f"dropped {len(zero_path_names)} zero-path feature(s) "
            f"after attribution: {zero_path_names[:10]}",
        )
    zero_path_drops_count = len(zero_path)
    zero_path_drops_sample = [f.name for f in zero_path[:10]]

    # 5) Compute unattributed residual.
    attributed = {p for f in features for p in f.paths}
    unattributed = [p for p in ctx.tracked_files if p not in attributed]

    return Stage2Result(
        features=features,
        unattributed=unattributed,
        notes=notes,
        zero_path_drops_count=zero_path_drops_count,
        zero_path_drops_sample=zero_path_drops_sample,
    )


__all__ = [
    "DeveloperFeature",
    "Stage2Result",
    "stage_2_reconcile",
]
