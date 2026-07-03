"""Stage 2 — anchor reconciliation.

Merges :class:`AnchorCandidate` instances from Stage 1 across all
sources, resolves cross-extractor name conflicts via a stable
priority rule, attributes every tracked file to exactly one feature,
and emits :class:`DeveloperFeature` records ready for Stage 3 flow
detection.

Algorithm (per the ``pipeline-architecture`` skill):

  1. **Merge by name overlap**: candidates whose slugs are
     token-set-identical OR have Jaccard similarity ≥ 0.7 are merged
     into one feature. Before comparison, slugs are normalized by
     stripping a short fixed list of URL-structure stop tokens
     (``api``/``internal``/``v1``..``v3``), and a strict token-subset
     (containment) of ≥2 tokens also merges — see
     :func:`_normalized_tokens` / :func:`_token_containment`.
  1b. **Guarded file-overlap merge** (2026-06): cross-extractor
     fragments of the same feature that miss the name bar but claim
     the same anchor files are merged under FOUR simultaneous guards —
     see :func:`_file_overlap_should_merge`. This deliberately
     implements the narrow surviving conditions of the REFUTED naive
     "identical path-set ⇒ merge" experiment (memory
     finding-pathset-merge-refuted-2026-06-01): naive file-set merging
     cratered precision on JS/Prisma stacks, so schema-sourced
     candidates never merge by file overlap and a name signal is still
     required.
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
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.llm_health import LlmHealth

if TYPE_CHECKING:
    from faultline.models.types import MemberFile
    from faultline.pipeline_v2.profiles.base import FrameworkProfile
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)
from faultline.llm.model_gateway import resolve_model as gateway_model


# ── Source priority ────────────────────────────────────────────────────────
#
# Higher = more authoritative. Package names are the most stable
# semantic anchor (one upgrade per quarter at most); route slugs change
# when the URL changes (a few times per year); MVC controller names are
# refactor-sensitive; schema model names are renamed as the data model
# evolves; config-as-product anchors carry less product-vocabulary
# weight than the others.

_SOURCE_PRIORITY: dict[str, int] = {
    "package":         5,
    # Cargo workspace member names come straight from the Cargo.toml
    # manifests (workspace members + per-crate ``name``) — the same
    # stability class as ``package``. Added 2026-06 (metric-honesty
    # review): missing sources defaulted to 0 and lost every
    # file-ownership conflict.
    "rust-workspace":  5,
    "route":           4,
    # Rails routes are the same shape as ``route`` (file-system /
    # declared HTTP entry points) — match its priority.
    "rails-routes":    4,
    # Declared HTTP entry points parsed from source (decorators /
    # router-registration calls) — same semantics as ``route`` /
    # ``rails-routes``, so the same tier. All five added 2026-06
    # (metric-honesty review): they were absent → priority 0 → they
    # lost file ownership to every listed source, including ``config``.
    "fastapi-route":   4,   # FastAPI @app/@router decorators
    # Profile-supplied domain-package anchors (FastAPI-family, Phase B).
    # Code-layout-derived like the other module/sub-package extractors,
    # so the same ``mvc`` tier: below declared HTTP entry points (the
    # router file itself belongs to the route anchor), above schema.
    "fastapi-domain":  3,
    "route-fastify":   4,   # Fastify .get/.post route registrations
    # Profile-supplied react-router route-element anchors (Phase B #3).
    # Declared route table parsed from source — same semantics as
    # ``route`` / the other declared-entry-point sources, same tier.
    "react-router-spa": 4,
    "route-express":   4,   # Express .get/.post route registrations
    "go-router":       4,   # Go HTTP mux/handler registrations
    "django-route":    4,   # Django/DRF urls.py urlpatterns + views
    # Profile-supplied Django app-directory anchors (Phase B #2). Same
    # code-layout tier as fastapi-domain: below declared URLConf routes
    # (the urls/views files belong to the route anchor), above schema.
    "django-app":      3,
    "mvc":             3,
    # Rails models name the domain resource and are higher-precision
    # than the schema (which only has table names without methods).
    "rails-models":    3,
    # Module/sub-package structure extractors. Their anchors are
    # code-layout-derived (first-level dirs / src modules / exported
    # submodules) — refactor-sensitive like controller names, so they
    # sit at the ``mvc`` tier: above schema/config supporting evidence
    # but below manifests and declared HTTP entry points. Added
    # 2026-06 (metric-honesty review, same priority-0 hole as above).
    "go-package":      3,   # Go cmd/internal/pkg first-level dirs
    "rust-module":     3,   # Rust src/<m>.rs / src/<m>/ modules
    "python-library":  3,   # Python top-level submodules
    "js-library":      3,   # package.json#exports + lib/ submodules
    "schema":          2,
    # Views / jobs / Stimulus are supporting evidence for a resource
    # noun already named by routes / models — schema-tier priority so
    # they merge into route/model anchors without overriding them.
    "rails-views":     2,
    "rails-jobs":      2,
    "rails-stimulus":  2,
    "config":          1,
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
    # Stage 2.6 (2026-06) — per-file membership provenance. Populated
    # by the import-closure membership pass with pydantic
    # ``faultline.models.types.MemberFile`` records (anchor / closure /
    # co-commit / shared). Stage 5 forwards it onto the public
    # ``Feature.member_files``. Empty until Stage 2.6 runs.
    member_files: list["MemberFile"] = field(default_factory=list)

    # In-scan merge lineage: the LOSING candidate slugs absorbed into
    # this feature (canonical name excluded), sorted. NOTE: this is
    # deliberately NOT stamped onto the public ``Feature.merged_from``
    # — that field carries CROSS-SCAN lineage UUIDs owned by Stage 6.8
    # (see ``faultline/pipeline_v2/lineage.py``). The in-scan dedup
    # convention (mirroring Stage 5's ``dedup_merged_from:`` drop-log
    # tag) is a ``merged_from:`` rationale entry plus this field.
    merged_from: list[str] = field(default_factory=list)


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
    # Schema-only phantom suppression (2026-06). Number + sample of the
    # bare-DB-entity features dropped because they had no owning code
    # (every source was a schema-declaration source). Stage 7 surfaces
    # this in ``scan_meta`` so a non-zero count is visible telemetry.
    schema_only_suppressed_count: int = 0
    schema_only_suppressed_sample: list[str] = field(default_factory=list)


# ── Similarity ─────────────────────────────────────────────────────────────


def _slug_tokens(slug: str) -> frozenset[str]:
    """Split a kebab-case slug into a token set, for Jaccard."""
    return frozenset(t for t in slug.split("-") if t)


# Universal URL-structure stop tokens stripped before slug comparison.
#
# Each entry is a STRUCTURAL marker of how an HTTP surface is mounted,
# never product vocabulary — that's the justification for hardcoding
# them (house rule: structural constants OK, tuned magic numbers NOT):
#
#   - ``api``       — the universal transport mount prefix (``/api/...``,
#                     ``api-`` route groups). Names the wire, not the
#                     feature: ``api-org-knowledge`` IS ``org-knowledge``.
#   - ``v1``/``v2``/``v3`` — URL version segments (``/api/v1/...``).
#                     Pure routing plumbing in every framework.
#   - ``internal``  — visibility-scoping mount segment
#                     (``/internal/...``, ``internal-`` admin surfaces).
#
# Deliberately NOT included: domain-ish words (``admin``, ``app``,
# ``web``) — those routinely ARE the feature — and NO stemming or
# alias folding (``org`` ↔ ``organization`` was considered and
# REJECTED: it is a vocabulary equivalence, not a structural one, and
# a partial alias table would be exactly the per-repo tuning the house
# rule forbids).
_STOP_PREFIX_TOKENS: frozenset[str] = frozenset(
    {"api", "internal", "v1", "v2", "v3"},
)


def _normalized_tokens(slug: str) -> frozenset[str]:
    """Token set with URL-structure stop tokens stripped.

    Falls back to the RAW token set when stripping would leave nothing
    (a slug made entirely of structural tokens, e.g. ``api`` or
    ``api-v1``, must not normalize to the universally-matching empty
    set).
    """
    raw = _slug_tokens(slug)
    stripped = raw - _STOP_PREFIX_TOKENS
    return stripped or raw


def _token_containment(
    a: frozenset[str],
    b: frozenset[str],
    *,
    min_subset_tokens: int = 2,
) -> bool:
    """``True`` when one token set is a STRICT subset of the other.

    ``min_subset_tokens`` guards the weak end: a single-token subset
    (``auth`` ⊂ ``auth-tokens`` ⊂ ``auth-sessions`` …) carries no
    compound-noun specificity and would transitively collapse whole
    families via union-find, so name-only containment requires the
    subset to keep ≥2 tokens. The file-overlap rule relaxes this to 1
    because there the shared-anchor-file evidence carries the weight.
    """
    if a == b:
        return False
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return len(smaller) >= min_subset_tokens and smaller < larger


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _should_merge(a: str, b: str, *, threshold: float = 0.7) -> bool:
    """``True`` if two slugs should merge into one feature.

    Order of checks (cheapest first):

      1. literal / token-set equality on RAW tokens;
      2. raw-token Jaccard ≥ ``threshold`` (the original rule);
      3. equality on NORMALIZED tokens (stop-prefix-stripped) —
         ``api-org-knowledge`` ≡ ``org-knowledge``;
      4. strict token containment on normalized tokens (≥2-token
         subset) — ``org-knowledge`` ⊂ ``org-knowledge-base``;
      5. normalized-token Jaccard ≥ ``threshold``.
    """
    if a == b:
        return True
    ta, tb = _slug_tokens(a), _slug_tokens(b)
    if ta == tb:
        return True
    if _jaccard(ta, tb) >= threshold:
        return True
    na, nb = _normalized_tokens(a), _normalized_tokens(b)
    if na == nb:
        return True
    if _token_containment(na, nb):
        return True
    return _jaccard(na, nb) >= threshold


# ── Guarded file-overlap merge (2026-06) ───────────────────────────────────
#
# Sources whose candidates may NOT participate in a file-overlap merge.
# This is the load-bearing guard from the refuted path-set-merge
# experiment (finding-pathset-merge-refuted-2026-06-01): schema-derived
# features (Prisma models etc.) legitimately share files with route
# features — one ``schema.prisma`` holds every model — without being
# the same feature, and merging them cratered precision −13/−20pp on
# JS/Prisma stacks. ``rails-models`` is the same signal class (model
# declarations naming a data resource), so it is excluded too.

_FILE_OVERLAP_EXCLUDED_SOURCES: frozenset[str] = frozenset(
    {"schema", "rails-models"},
)


# ── Schema-only phantom suppression (2026-06) ──────────────────────────────
#
# A DB-schema model (Prisma model/enum, Rails table) is EVIDENCE of a
# feature, not a feature in itself — see the ``schema-domain-extractor``
# skill ("A model's existence is evidence of a feature; the FEATURE
# itself comes from controllers/routes/UI"). When a model ALSO has its
# own owning code (a ``booking`` model + a ``booking/`` route, or a
# ``packages/features/membership/`` module), the route/mvc/package
# extractor emits a same-named candidate that NAME-MERGES with the schema
# candidate in Stage 2 — so the resulting feature carries that code
# source and survives.
#
# But a bare data entity with NO owning code of its own (``host-group``,
# ``credential``, ``organization-settings`` … in cal.com's
# ``schema.prisma``) never merges with anything: it spawns a feature
# whose ONLY anchor is the single shared schema file. Stage 2.6
# import-closure then piles the SAME closure set onto every such
# feature, producing dozens of identical phantom duplicates (cal.com:
# 92 features sharing one 133-file set). These are data entities, not
# product features.
#
# SUPPRESSION RULE (structural, scale-invariant, no magic numbers, no
# repo paths): drop a merged feature iff its contributing sources are a
# NON-EMPTY SUBSET of the schema-declaration sources below — i.e. the
# feature has NO distinct owning module / route / dir in code. This is
# the same "schema-declaration source" class already used by the
# file-overlap guard, reused here for consistency. A schema model with
# its own code keeps a non-schema source and is never suppressed, so no
# real feature is lost (verified: cal.com booking/availability/team/
# membership/user all carry a route or module source).
#
# Enum/barrel re-export features (a Prisma enum re-exported through a
# ``js-library`` barrel ``index.ts``) carry a ``js-library`` source and
# are deliberately OUT of scope here — that fan-out is the
# complementary Stage 8.8 barrel/hub guard's job, not this rule's.

_SCHEMA_DECLARATION_SOURCES: frozenset[str] = _FILE_OVERLAP_EXCLUDED_SOURCES


def _is_schema_only_phantom(feature: DeveloperFeature) -> bool:
    """``True`` when a feature is a bare schema model with no owning code.

    Structural test: every contributing source is a schema-declaration
    source (``schema`` / ``rails-models``). Such a feature's anchor set
    is only the shared schema file(s) — it has no route, module, or
    directory of its own and must not be promoted to a standalone
    developer feature. A model that owns code name-merges with the
    code's candidate in Stage 2 and gains a non-schema source, so this
    returns ``False`` for it.
    """
    sources = set(feature.sources)
    if not sources:
        return False
    return sources <= _SCHEMA_DECLARATION_SOURCES


def _suppress_schema_only_phantoms(
    features: list[DeveloperFeature],
) -> tuple[list[DeveloperFeature], list[str]]:
    """Filter out schema-only phantom features (see module-level note).

    Returns ``(kept_features, suppressed_names)``. Pure / deterministic;
    no thresholds, no repo-specific paths.
    """
    kept: list[DeveloperFeature] = []
    suppressed: list[str] = []
    for f in features:
        if _is_schema_only_phantom(f):
            suppressed.append(f.name)
        else:
            kept.append(f)
    return kept, sorted(suppressed)


def _file_overlap_should_merge(
    a: AnchorCandidate,
    b: AnchorCandidate,
    *,
    threshold: float = 0.7,
) -> bool:
    """Second merge predicate: cross-extractor fragments sharing files.

    Merges ONLY when ALL of the following hold (each guard answers the
    refuted naive "share files ⇒ merge" finding):

      1. **cross-source** — same-source siblings sharing a file are
         usually genuinely distinct routes declared in one module;
      2. **neither schema-sourced** — see
         :data:`_FILE_OVERLAP_EXCLUDED_SOURCES`;
      3. **anchor-file containment** — the path-set overlap covers at
         least HALF of the SMALLER candidate's path set
         (scale-invariant ratio, no absolute file counts);
      4. **a name signal still agrees** — normalized-token containment
         (1-token subsets allowed here: the file evidence carries the
         weight) OR normalized-token Jaccard ≥ ``threshold``.
    """
    if a.source == b.source:
        return False
    if (
        a.source in _FILE_OVERLAP_EXCLUDED_SOURCES
        or b.source in _FILE_OVERLAP_EXCLUDED_SOURCES
    ):
        return False
    pa, pb = frozenset(a.paths), frozenset(b.paths)
    if not pa or not pb:
        return False
    overlap = len(pa & pb)
    if overlap * 2 < min(len(pa), len(pb)):
        return False
    na, nb = _normalized_tokens(a.name), _normalized_tokens(b.name)
    if na == nb:
        return True
    if _token_containment(na, nb, min_subset_tokens=1):
        return True
    return _jaccard(na, nb) >= threshold


# ── Rails cross-extractor merger (H2) ──────────────────────────────────────
#
# When the Stage 0.5 auditor declared ``rails-app``, we add a second
# union pass that collapses anchors whose Rails canonical noun
# (singular form with ``-controller``/``-job`` suffixes stripped) is
# identical. Without this, "address" (rails-models) and "addresses"
# (rails-views + rails-routes) live as 3 separate features because
# their token sets share zero elements.


def _rails_should_merge(a: str, b: str) -> bool:
    """``True`` when two slugs map to the same Rails canonical noun.

    Lazy import keeps the dependency local — Stage 2 must still import
    cleanly for non-Rails repos where ``_rails`` may not exist.
    """
    try:
        from faultline.pipeline_v2.extractors._rails import (
            rails_canonical_noun,
        )
    except ImportError:  # pragma: no cover — defensive
        return False
    if not a or not b or a == b:
        return a == b
    ca = rails_canonical_noun(a)
    cb = rails_canonical_noun(b)
    if not ca or not cb:
        return False
    return ca == cb


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
    rails_merge: bool = False,
) -> list[list[int]]:
    """Return list of index lists; each inner list is one merge group.

    Args:
        rails_merge: when True, add a second union pass that collapses
            anchors whose Rails canonical noun is identical (handles
            singular ↔ plural mismatches between models / views / routes
            / controllers). Off by default; the orchestrator turns it
            on when ``ctx.audited_stack == "rails-app"``.
    """
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
            elif rails_merge and _rails_should_merge(
                candidates[i].name, candidates[j].name,
            ):
                uf.union(i, j)
            elif _file_overlap_should_merge(
                candidates[i], candidates[j], threshold=jaccard_threshold,
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
    llm_health: LlmHealth | None = None,
) -> str | None:
    """Ask Haiku which of two ambiguous candidate names to keep.

    Returns the chosen slug, or ``None`` if the call failed / was
    inconclusive. Caller falls back to the priority rule on ``None``.

    This is the only LLM call Stage 2 ever makes, and it only fires
    when ``llm_reconcile=True`` AND the slug Jaccard sits in 0.3..0.6.
    Consults the shared :class:`LlmHealth`: after the first auth-class
    failure anywhere in the scan the call is skipped (dead key).
    """
    if llm_health is not None and not llm_health.should_call():
        return None
    try:
        # Local import to keep ``anthropic`` from being a hard dep of
        # Stage 2 callers that don't enable the 2nd-opinion path.
        from anthropic import Anthropic
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
            model=gateway_model(_HAIKU_MODEL_ID),
            max_tokens=24,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (msg.content[0].text or "").strip()  # type: ignore[union-attr]
        # Sanitize: take first whitespace-separated token, lowercase,
        # validate it kebab-cases cleanly.
        first = text.splitlines()[0].strip().split()[0] if text else ""
        first = first.strip("`\"' .").lower()
        if llm_health is not None:
            llm_health.record_success()
        if first in {candidate_a.name, candidate_b.name}:
            return first
        return None
    except Exception as exc:  # noqa: BLE001 — LLM failure is non-fatal
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_2_reconcile",
        ):
            logger.error(
                "stage_2_reconcile: LLM authentication failed — skipping "
                "all remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("LLM 2nd-opinion failed: %s", exc)
        return None


# ── Group → DeveloperFeature ───────────────────────────────────────────────


def _pick_canonical_name(
    group: list[AnchorCandidate],
    *,
    llm_reconcile: bool,
    llm_call: Callable[[AnchorCandidate, AnchorCandidate], str | None] = _llm_pick_name,
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

    # Rails canonical-noun preference. When the group mixes Rails
    # sources (rails-models / rails-routes / rails-views / etc.), we
    # prefer the singular form of the canonical noun as the slug so
    # the resulting feature name is "address" (the model) rather than
    # "addresses" (the views/routes directory).
    sources_in_group = {c.source for c in group}
    if any(s.startswith("rails-") for s in sources_in_group):
        try:
            from faultline.pipeline_v2.extractors._rails import (
                rails_canonical_noun,
            )
            canon = rails_canonical_noun(leader.name)
            if canon:
                # Pick whichever candidate's slug ALREADY equals the
                # canonical (singular) form, else fall back to canon.
                for c in ranked:
                    if c.name == canon:
                        return canon, None
                return canon, None
        except ImportError:  # pragma: no cover — defensive
            pass

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

    # In-scan merge lineage: losing slugs absorbed into the canonical
    # name (Stage 5 ``dedup_merged_from:`` convention).
    merged_from = sorted({c.name for c in group} - {canonical_name})
    if merged_from:
        merge_note = f"merged_from:{','.join(merged_from)}"
        rationale = (rationale + " | " + merge_note) if rationale else merge_note

    return DeveloperFeature(
        name=canonical_name,
        paths=tuple(sorted(paths)),
        sources=sources_ranked,
        confidence=confidence,
        display_name=display_name,
        rationale=rationale,
        source_confidences=source_conf,
        merged_from=merged_from,
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
                merged_from=f.merged_from,
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
    llm_health: LlmHealth | None = None,
    profile: "FrameworkProfile | None" = None,
    _llm_call: Callable[[AnchorCandidate, AnchorCandidate], str | None] = _llm_pick_name,
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
    # Bind the shared LLM-health state into the call helper (the
    # injectable ``_llm_call`` keeps its 2-arg test contract). Once any
    # stage has hit an auth-class failure the 2nd-opinion call is
    # skipped — the priority rule resolves the tie deterministically.
    if llm_health is not None:
        _base_llm_call = _llm_call
        _health = llm_health

        def _guarded_llm_call(
            a: AnchorCandidate, b: AnchorCandidate,
        ) -> str | None:
            if not _health.should_call():
                return None
            if _base_llm_call is _llm_pick_name:
                return _llm_pick_name(a, b, llm_health=_health)
            return _base_llm_call(a, b)

        _llm_call = _guarded_llm_call

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
    #    Rails: also unify singular ↔ plural via canonical-noun pass.
    rails_merge = (ctx.audited_stack or "").lower() == "rails-app" or (
        "rails-app" in tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    )
    groups = _build_merge_groups(
        flat,
        jaccard_threshold=jaccard_threshold,
        rails_merge=rails_merge,
    )

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

    # 2.8) Schema-only phantom suppression. A merged feature whose ONLY
    #      contributing sources are schema-declaration sources is a bare
    #      DB entity with no owning code (route/module/dir) — it must not
    #      become a standalone developer feature, and if it did, Stage 2.6
    #      import-closure would clone the same closure set onto every such
    #      feature (the cal.com 92-phantom-dup bug). Runs BEFORE profile
    #      and path attribution so the per-feature ``paths`` are still the
    #      pure schema anchor set (no closure inflation yet). See
    #      :func:`_is_schema_only_phantom`.
    features, suppressed_phantoms = _suppress_schema_only_phantoms(features)
    if suppressed_phantoms:
        notes.append(
            f"suppressed {len(suppressed_phantoms)} schema-only phantom "
            f"feature(s) (no owning code): {suppressed_phantoms[:10]}",
        )

    # 2.9) Profile-driven attribution (P4 framework-awareness).
    #      The active FrameworkProfile gets the FIRST say over which
    #      feature a file belongs to — route-group / feature-folder
    #      semantics override generic path-proximity, which is what kills
    #      the physical-container blob. Files the profile does not claim
    #      fall through to the conflict-resolution step below UNCHANGED.
    #      No-op for the DefaultProfile / None (regression guard): the
    #      input list is returned by identity, so the legacy path is
    #      byte-for-byte preserved when no concrete profile wins.
    if profile is not None:
        from faultline.pipeline_v2.profiles._attribution import (
            apply_profile_attribution,
            is_active,
        )

        if is_active(profile):
            def _rehome(f: DeveloperFeature, new_paths: tuple[str, ...]) -> DeveloperFeature:
                return DeveloperFeature(
                    name=f.name,
                    paths=new_paths,
                    sources=f.sources,
                    confidence=f.confidence,
                    display_name=f.display_name,
                    rationale=f.rationale,
                    source_confidences=f.source_confidences,
                    merged_from=f.merged_from,
                )

            def _make_feature(name: str, paths: tuple[str, ...]) -> DeveloperFeature:
                # Profile-synthesised capability boundary (route group /
                # module folder). Sourced as ``route`` so Stage 2.6's
                # import-closure (which seeds from ANCHOR_SOURCES features)
                # pulls the boundary's colocated component/service/lib
                # files into it — without that the boundary would only own
                # its directly re-homed routing files. It is a declared
                # entry-point boundary, so ``route`` is the accurate source.
                return DeveloperFeature(
                    name=name,
                    paths=paths,
                    sources=["route"],
                    confidence="medium",
                    rationale="profile-synthesised capability boundary "
                              f"(profile={profile.name})",
                )

            features = apply_profile_attribution(
                features, profile, ctx, rebuild=_rehome,
                make_feature=_make_feature,
            )
            notes.append(f"profile-attribution applied (profile={profile.name})")

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
        schema_only_suppressed_count=len(suppressed_phantoms),
        schema_only_suppressed_sample=suppressed_phantoms[:10],
    )


__all__ = [
    "DeveloperFeature",
    "Stage2Result",
    "stage_2_reconcile",
]
