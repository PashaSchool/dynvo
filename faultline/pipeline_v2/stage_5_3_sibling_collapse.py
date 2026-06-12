"""Stage 5.3 — sibling-router collapse (deterministic, no LLM).

Position
========

Runs AFTER :func:`stage_5_postprocess` (Fix A/B/C/D + Sprint S1 dedup)
and BEFORE :func:`stage_5_5_bipartite`. The collapsed feature set
becomes the input to the bipartite store, so flow attribution and
blast-radius are computed over the AGGREGATED features rather than
the over-split sibling singletons.

Why this stage exists
=====================

Backend monoliths ship dozens (sometimes hundreds) of sibling
route files under shared parent directories. Concrete example from
infisical S3.1:

    backend/src/server/routes/v1/    → 46 sibling route files
    backend/src/ee/routes/v1/        → 41 sibling route files
    .../app-connection-routers/      → 22 sibling route files

Each Fastify ``*-router.ts`` file becomes one Stage 4 LLM-fallback
feature named after the first URL segment it handles (``email``,
``analytics``, ``status``, ``cacerts``, ...). That's 408 single-path
features in one repo — engineering-grain noise that swamps the real
product surface.

Many real-world Fastify / Express / Hono / FastAPI / Django apps
nest dozens of resources under shared route prefixes. We need a
universal aggregation pass that collapses these into one feature per
shared route folder.

Algorithm (deterministic, no magic numbers per ``rule-no-magic-tuning``)
=======================================================================

1. **Group features by parent directory of their primary path.**
   For each :class:`Feature`, compute
   ``parent_dir = '/'.join(min_path.split('/')[:-1])`` where
   ``min_path`` is the lexicographically smallest path. Skip
   features with zero paths.

2. **Filter parent_dir keys by structural depth.**
   ``len(parent_dir.split('/')) >= 2`` — single-segment top dirs
   (``src``, ``app``, ``backend``) are too coarse; the repo root
   (``""``) is excluded by construction.

3. **For each ``parent_dir`` with ``len(features) >= MIN_SIBLINGS``:**

   - Every member must be **route-shaped**: at least one path
     matches one of the universal router file conventions
     (see ``_ROUTE_FILE_RE``). Members that are NOT route-shaped
     are excluded from the collapse and left as-is.
   - **Anchor-preservation guard**: features whose name doesn't
     match the synthesized parent label are PRESERVED if they have
     ``confidence in {"high", "medium"}`` (Stage 2 deterministic
     anchors). Only ``confidence == "low"`` (Stage 4 LLM-fallback)
     features collapse freely. This protects rare cases where Stage
     2 anchored a real domain (e.g. ``auth``) that happens to live
     under the same prefix as N route singletons.
   - **Synthesize parent label** from the last two segments of
     ``parent_dir`` (e.g. ``backend/src/server/routes/v1/`` →
     ``v1-routes``; ``apps/web/api/admin/`` → ``api-admin-routes``).
   - **Merge** the collapsible subset into one
     :class:`Feature`: union of paths, synthesized name, source
     ``"sibling-collapse"`` recorded in telemetry. Per-feature
     ``display_name`` derived from the parent label.

4. **Anchor-overlap exception.** If the synthesized parent label
   already exists as a Stage 2 anchor in the collapsible subset,
   fold the rest into the anchor (preserve anchor name + description).

Universal across stacks
=======================

The route-file regex covers:
  - JS/TS conventions: ``route.{ts,js,mts,mjs}``, ``*-router.{ts,js}``,
    ``*-route.{ts,js}``, ``router.{ts,js}``
  - Go: ``handler.go``, ``*_handler.go``, ``routes.go``
  - Python: ``urls.py``, ``views.py``, ``router.py``, ``routers.py``
  - Ruby: ``routes.rb``, ``*_controller.rb``
  - PHP: ``routes.php``, ``web.php``, ``api.php``
  - C#: ``*Controller.cs``
  - Elixir: ``*_controller.ex``, ``router.ex``

No repo-specific paths appear anywhere in this module. The
``MIN_SIBLINGS`` threshold (3) is structural — repos with 1-2
route files per dir never fire.

Telemetry
=========

Emits the following keys for ``scan_meta``:

    stage_5_3_collapse_groups_count
    stage_5_3_features_collapsed     (total members absorbed, ≥ 0)
    stage_5_3_features_post          (feature count after collapse)
    stage_5_3_collapse_sample        (up to 5 groups; each has
                                      parent, label, member_count,
                                      members_sample[≤5])

Complies with ``rule-cold-scan``, ``rule-no-magic-tuning``,
``rule-no-repo-specific-paths``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from faultline.models.types import Feature

if TYPE_CHECKING:
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)


# ── Universal thresholds (structural, not corpus-tuned) ───────────────────

# Minimum sibling count for collapse. Below 3, the engineering-grain
# granularity is preferred per ``rule-engineering-granularity-is-correct``.
# Repos with 1-2 sibling routes (small libs) never fire.
MIN_SIBLINGS = 3

# Minimum directory depth for parent_dir to qualify. ``foo/`` (depth 1)
# is too coarse — that's a whole top-level package. ``foo/bar/`` (depth
# 2) is the floor below which we don't risk collapsing unrelated trees.
MIN_PARENT_DEPTH = 2


# Universal route-file regex. Anchored on the file basename so it
# matches regardless of where in the tree the file lives.
#
# Patterns (case-insensitive on extension):
#   - route(s).{ts,js,mts,mjs,go,py,rb,php,ex}
#   - router(s).{ts,js,mts,mjs,py,ex}
#   - *-router.{ts,js,mts,mjs}     (Fastify, NestJS, etc.)
#   - *-route.{ts,js,mts,mjs}
#   - *_router.{py,rb,ex}
#   - handler.go, *_handler.go
#   - urls.py, views.py
#   - web.php, api.php
#   - *Controller.cs, *_controller.{rb,ex}
_ROUTE_FILE_PATTERNS = (
    r"^routes?\.(ts|js|mts|mjs|go|py|rb|php|ex)$",
    r"^routers?\.(ts|js|mts|mjs|py|ex)$",
    r"^.+-router\.(ts|js|mts|mjs)$",
    r"^.+-route\.(ts|js|mts|mjs)$",
    r"^.+_router\.(py|rb|ex)$",
    r"^.+_route\.(py|rb|ex)$",
    r"^handler\.go$",
    r"^.+_handler\.go$",
    r"^urls\.py$",
    r"^views\.py$",
    r"^router\.py$",
    r"^web\.php$",
    r"^api\.php$",
    r"^.+Controller\.cs$",
    r"^.+_controller\.(rb|ex)$",
    r"^router\.ex$",
)
_ROUTE_FILE_RE = re.compile(
    "(?:" + "|".join(_ROUTE_FILE_PATTERNS) + ")",
    re.IGNORECASE,
)


# ── Telemetry shapes ──────────────────────────────────────────────────────


@dataclass
class CollapseGroup:
    """One sibling-router collapse event."""

    parent_path: str            # e.g. "backend/src/server/routes/v1"
    parent_label: str           # e.g. "v1-routes"
    members: tuple[str, ...]    # feature names absorbed
    member_count: int           # == len(members)

    def as_dict(self, members_sample_size: int = 5) -> dict[str, object]:
        return {
            "parent": self.parent_path,
            "label": self.parent_label,
            "member_count": self.member_count,
            "members_sample": list(self.members[:members_sample_size]),
        }


@dataclass
class Stage53Result:
    """Output of :func:`collapse_sibling_routes`."""

    features: list[Feature]
    collapse_groups: list[CollapseGroup] = field(default_factory=list)

    @property
    def features_collapsed(self) -> int:
        """Total members absorbed (= sum of group sizes)."""
        return sum(g.member_count for g in self.collapse_groups)


# ── Helpers ───────────────────────────────────────────────────────────────


def _primary_parent_dir(feature: Feature) -> str | None:
    """Return the parent directory of the feature's lexicographically
    smallest path. ``None`` for features with no paths.
    """
    if not feature.paths:
        return None
    min_path = min(feature.paths)
    if "/" not in min_path:
        # Top-level file — parent_dir would be "" which fails the
        # depth filter anyway, but bail explicitly so the key is
        # never the empty string.
        return None
    return "/".join(min_path.split("/")[:-1])


def _is_route_shaped(feature: Feature) -> bool:
    """``True`` if ANY of the feature's paths matches a router-file
    convention. Conservative: one match is enough — feature can have
    other non-route paths (helpers, validators) without disqualifying.
    """
    for p in feature.paths:
        basename = p.rsplit("/", 1)[-1]
        if _ROUTE_FILE_RE.match(basename):
            return True
    return False


_VERSION_RE = re.compile(r"^v\d+$", re.IGNORECASE)
_CONTAINER_SUFFIXES = frozenset({
    "routes", "router", "routers",
    "handlers", "controllers", "views",
})


def _synthesize_parent_label(parent_dir: str) -> str:
    """Build a kebab-case label from the parent directory.

    Algorithm:
      1. Walk segments right-to-left, finding the FIRST container
         suffix (``routes``, ``router``, ``handlers``, etc.). Let
         ``container_idx`` be that position.
      2. If a container was found:
           - Collect any non-container segments to the RIGHT of it
             (e.g. ``v1`` in ``routes/v1``, ``admin`` in
             ``api/admin``).
           - Emit ``<right-segments>-<container>`` (e.g.
             ``v1-routes``). If no right-segments exist, emit just
             ``<container>`` prefixed by the immediately-preceding
             segment if any (``app/controllers`` →
             ``app-controllers``).
      3. If NO container was found, fall back to last two segments
         joined + ``-routes`` (``apps/web/api/admin`` →
         ``api-admin-routes`` when ``api`` is treated as a container;
         a true no-container case like ``frontend/dashboard`` →
         ``frontend-dashboard-routes``).

    Examples:
      ``backend/src/server/routes/v1`` → ``v1-routes``
      ``backend/src/ee/routes/v1``     → ``v1-routes``
      ``apps/web/api/admin``           → ``api-admin-routes``
      ``services/auth/handlers``       → ``auth-handlers``
      ``app/controllers``              → ``app-controllers``
      ``backend/api/v2``               → ``v2-api``
    """
    segments = [s.lower().replace("_", "-")
                for s in parent_dir.split("/") if s]
    if not segments:
        return "routes"

    # Find rightmost container suffix.
    container_idx: int | None = None
    for i in range(len(segments) - 1, -1, -1):
        if segments[i] in _CONTAINER_SUFFIXES:
            container_idx = i
            break

    if container_idx is not None:
        container = segments[container_idx]
        right = segments[container_idx + 1:]
        if right:
            # e.g. routes + [v1] → "v1-routes"
            return "-".join(right + [container])
        # No right segments — pair with one left segment for context.
        if container_idx > 0:
            return f"{segments[container_idx - 1]}-{container}"
        return container

    # No container — synthesize from last two segments + "-routes".
    tail = segments[-2:] if len(segments) >= 2 else segments[-1:]
    return "-".join(tail) + "-routes"


def _merge_features(
    members: list[Feature],
    *,
    name: str,
    display_name: str,
) -> Feature:
    """Combine N member features into ONE collapsed Feature.

    - Paths: union (sorted, deduped).
    - Authors: union preserving first-seen order.
    - total_commits / bug_fixes: sum.
    - bug_fix_ratio: recomputed.
    - last_modified: max.
    - health_score: max (most optimistic surviving signal).
    - flows: concatenation (Stage 5.5 will re-key by primary feature).
    - description: longest non-empty.
    - coverage_pct: weighted average by path count, ignoring None.
    - layer / product_feature_id: from first member.
    """
    paths_set: set[str] = set()
    paths_ordered: list[str] = []
    for m in members:
        for p in m.paths:
            if p not in paths_set:
                paths_set.add(p)
                paths_ordered.append(p)
    paths_ordered.sort()

    authors_set: set[str] = set()
    authors_ordered: list[str] = []
    for m in members:
        for a in m.authors:
            if a not in authors_set:
                authors_set.add(a)
                authors_ordered.append(a)

    total_commits = sum(m.total_commits or 0 for m in members)
    bug_fixes = sum(m.bug_fixes or 0 for m in members)
    bug_fix_ratio = (
        round(bug_fixes / total_commits, 4) if total_commits > 0 else 0.0
    )
    last_modified = max(
        (m.last_modified for m in members),
        default=datetime.now(timezone.utc),
    )
    health_score = max((m.health_score or 0.0) for m in members)

    descriptions = [m.description for m in members if m.description]
    description = max(descriptions, key=len) if descriptions else None

    # Weighted-by-path-count coverage average (ignore None).
    cov_pairs = [
        (m.coverage_pct, len(m.paths))
        for m in members
        if m.coverage_pct is not None and m.paths
    ]
    if cov_pairs:
        num = sum(pct * n for pct, n in cov_pairs)
        den = sum(n for _, n in cov_pairs)
        coverage_pct: float | None = round(num / den, 4) if den else None
    else:
        coverage_pct = None

    flows: list = []
    for m in members:
        flows.extend(m.flows)

    bug_fix_prs: list = []
    for m in members:
        bug_fix_prs.extend(m.bug_fix_prs)

    shared_attributions: list = []
    for m in members:
        shared_attributions.extend(m.shared_attributions)

    participants: list = []
    for m in members:
        participants.extend(m.participants)

    shared_participants: list = []
    for m in members:
        shared_participants.extend(m.shared_participants)

    # Stage 2.6 provenance — union members' member_files, dedup by
    # (path, role); first member wins on duplicates (deterministic).
    member_files: list = []
    seen_member_keys: set[tuple[str, str]] = set()
    for m in members:
        for mf in m.member_files:
            key = (mf.path, mf.role)
            if key in seen_member_keys:
                continue
            seen_member_keys.add(key)
            member_files.append(mf)

    primary = members[0]
    return Feature(
        name=name,
        display_name=display_name,
        description=description,
        paths=paths_ordered,
        authors=authors_ordered,
        total_commits=total_commits,
        bug_fixes=bug_fixes,
        bug_fix_ratio=bug_fix_ratio,
        last_modified=last_modified,
        health_score=health_score,
        flows=flows,
        bug_fix_prs=bug_fix_prs,
        coverage_pct=coverage_pct,
        shared_attributions=shared_attributions,
        participants=participants,
        symbol_health_score=primary.symbol_health_score,
        shared_participants=shared_participants,
        layer=primary.layer,
        product_feature_id=primary.product_feature_id,
        member_files=member_files,
    )


def _disambiguate_label(parent_dir: str, base_label: str) -> str:
    """When two parents synthesize to the same ``base_label``, prepend
    the FIRST distinguishing upstream segment.

    Examples:
      base ``v1-routes`` from ``backend/src/server/routes/v1`` →
        ``server-v1-routes``
      base ``v1-routes`` from ``backend/src/ee/routes/v1`` →
        ``ee-v1-routes``

    The chosen segment is the one immediately above the lowest
    container suffix (if any), else the last segment before what's
    already in ``base_label``.
    """
    segments = [s.lower().replace("_", "-")
                for s in parent_dir.split("/") if s]
    # Find rightmost container suffix and grab the segment above it.
    for i in range(len(segments) - 1, -1, -1):
        if segments[i] in _CONTAINER_SUFFIXES:
            if i > 0:
                prefix = segments[i - 1]
                return f"{prefix}-{base_label}"
            break
    # Fallback: prepend the upstream segment if it adds info.
    base_tokens = set(base_label.split("-"))
    for seg in reversed(segments):
        if seg not in base_tokens:
            return f"{seg}-{base_label}"
    # Last resort — pathological case where every segment is already
    # in the base label; append a hash-like suffix derived from the
    # full parent path so output stays unique.
    suffix = str(abs(hash(parent_dir)) % 9000 + 1000)
    return f"{base_label}-{suffix}"


def _display_label(synth_label: str) -> str:
    """Title-case a kebab label for the display field.

    ``v1-routes`` → ``V1 Routes``; ``api-admin-routes`` → ``Api Admin Routes``.
    """
    return " ".join(part.capitalize() for part in synth_label.split("-"))


# ── Public entry point ────────────────────────────────────────────────────


_ROUTE_ONLY_SOURCES = frozenset({
    # Source slugs that emit features at URL-slug granularity. A feature
    # whose source set is a subset of these is treated as collapsible
    # regardless of confidence — these extractors are inherently
    # over-granular for backend monoliths (one router file == one
    # feature, even though the file is just one resource in a much
    # larger product surface).
    #
    # The list is universal: any extractor whose unit-of-emission is
    # "one route file" belongs here. Extractors that emit at the
    # domain grain (package, schema, config, mvc) are NOT here — their
    # features represent real product anchors and must be preserved.
    "route",
    "route-fastify",
})


def _is_route_only_source(sources: set[str] | frozenset[str]) -> bool:
    """``True`` if every source in ``sources`` is a known route-only
    emitter. Empty / unknown sources return ``False`` (be conservative
    and preserve)."""
    if not sources:
        return False
    return sources.issubset(_ROUTE_ONLY_SOURCES)


def collapse_sibling_routes(
    features: list[Feature],
    *,
    confidence_by_name: dict[str, str] | None = None,
    sources_by_name: dict[str, list[str]] | None = None,
    log: "StageLogger | None" = None,
    min_siblings: int = MIN_SIBLINGS,
    min_parent_depth: int = MIN_PARENT_DEPTH,
) -> Stage53Result:
    """Collapse sibling route-files into one feature per parent dir.

    Args:
        features: Stage 5 output (post naming-discipline + S1 dedup).
        confidence_by_name: optional ``{feature_name: confidence}`` map
            from Stage 2 (``"high"`` / ``"medium"`` / ``"low"``).
        sources_by_name: optional ``{feature_name: [source, ...]}`` map
            from Stage 2. The PREFERRED preservation signal: when
            present, a feature is collapsible iff EVERY source is in
            ``_ROUTE_ONLY_SOURCES`` (i.e. it's a pure URL-slug
            singleton with no package/schema/config/mvc anchor). When
            absent, falls back to confidence-only: ``"low"`` collapses,
            ``"high"`` / ``"medium"`` are preserved.
            Rationale: the S3.1 Fastify extractor emits one feature per
            URL slug with medium confidence; preserving on confidence
            alone would block the collapse the sprint was designed for.
            Source-based preservation cleanly separates "domain anchor"
            (Resend → email feature, sourced from ``package``) from
            "route singleton" (one Fastify ``email-router.ts`` file).
        log: optional :class:`StageLogger` for stage-5 telemetry.
        min_siblings: structural threshold; below this, no collapse.
        min_parent_depth: minimum ``parent_dir`` segment count.

    Returns:
        :class:`Stage53Result` with the new feature list + per-group
        telemetry.

    Idempotent on identical input. Pure-Python; no LLM, no I/O.
    """
    if not features:
        return Stage53Result(features=[])

    confidence_by_name = confidence_by_name or {}
    sources_by_name = sources_by_name or {}

    # Index features by parent_dir of their primary (min) path.
    by_parent: dict[str, list[Feature]] = defaultdict(list)
    no_parent: list[Feature] = []
    for f in features:
        parent = _primary_parent_dir(f)
        if parent is None:
            no_parent.append(f)
            continue
        depth = len([s for s in parent.split("/") if s])
        if depth < min_parent_depth:
            no_parent.append(f)
            continue
        by_parent[parent].append(f)

    # Survivors that are not in any collapsing group.
    absorbed_names: set[str] = set()
    collapsed_features: list[Feature] = []
    groups: list[CollapseGroup] = []

    # Pre-compute base labels so we can detect collisions across
    # parents that synthesize to the same name (e.g.
    # ``backend/src/server/routes/v1`` and ``backend/src/ee/routes/v1``
    # both produce ``v1-routes``).
    base_labels: dict[str, str] = {
        parent: _synthesize_parent_label(parent)
        for parent in by_parent
    }
    label_collisions: Counter[str] = Counter(base_labels.values())

    # Process parents in deterministic (sorted) order for reproducibility.
    for parent in sorted(by_parent.keys()):
        siblings = by_parent[parent]
        if len(siblings) < min_siblings:
            continue

        # Build the synthesized parent label. If the base label
        # collides with another parent, prepend the first
        # distinguishing upstream segment so each collapsed feature
        # has a unique name.
        base_label = base_labels[parent]
        if label_collisions[base_label] > 1:
            synth_label = _disambiguate_label(parent, base_label)
        else:
            synth_label = base_label

        # Partition siblings into:
        #   - collapsible: route-shaped AND (low-confidence OR no-conf-map
        #     OR name == synth_label)
        #   - preserved:   anchor (high/medium) OR not route-shaped
        collapsible: list[Feature] = []
        preserved: list[Feature] = []
        anchor_match: Feature | None = None  # member already named synth_label
        for f in siblings:
            if not _is_route_shaped(f):
                preserved.append(f)
                continue
            if f.name == synth_label:
                # The collapsed feature already exists — absorb into
                # it regardless of confidence/source.
                anchor_match = f
                collapsible.append(f)
                continue
            # PREFERRED preservation signal: source set.
            sources = frozenset(sources_by_name.get(f.name, []))
            if sources:
                if _is_route_only_source(sources):
                    # Pure URL-slug singleton — collapsible.
                    collapsible.append(f)
                else:
                    # Has at least one non-route source (package /
                    # schema / config / mvc / etc.) — true product
                    # anchor; preserve.
                    preserved.append(f)
                continue
            # FALLBACK: no source info → use confidence.
            conf = confidence_by_name.get(f.name)
            if conf in ("high", "medium"):
                preserved.append(f)
                continue
            collapsible.append(f)

        if len(collapsible) < min_siblings:
            # After preservation, not enough remained to justify a
            # collapse. Leave the parent alone.
            continue

        # Materialise the merged feature.
        if anchor_match is not None:
            merged_name = anchor_match.name
            merged_display = (
                anchor_match.display_name or _display_label(synth_label)
            )
        else:
            merged_name = synth_label
            merged_display = _display_label(synth_label)

        merged = _merge_features(
            collapsible,
            name=merged_name,
            display_name=merged_display,
        )
        collapsed_features.append(merged)

        member_names = tuple(f.name for f in collapsible)
        for n in member_names:
            absorbed_names.add(n)

        groups.append(CollapseGroup(
            parent_path=parent,
            parent_label=merged_name,
            members=member_names,
            member_count=len(member_names),
        ))

        if log is not None:
            log.info(
                f"sibling_collapse: {merged_name} ← {len(collapsible)} "
                f"siblings under {parent}/",
            )

        # Preserved siblings get re-added by the survivor pass below.

    # Build the final feature list:
    #   - all features that were never in a collapsing group
    #   - preserved siblings (high/medium anchors + non-route-shaped)
    #   - newly-merged collapsed features
    # Preserve original ordering for stability.
    survivors: list[Feature] = []
    seen_collapsed_ids: set[int] = set()
    collapsed_by_name = {f.name: f for f in collapsed_features}

    for f in features:
        if f.name in absorbed_names:
            # If the absorbed-name matches the newly-minted collapsed
            # feature's name (the anchor-match case), emit the merged
            # version once at the original position.
            if f.name in collapsed_by_name:
                merged_f = collapsed_by_name[f.name]
                obj_id = id(merged_f)
                if obj_id not in seen_collapsed_ids:
                    survivors.append(merged_f)
                    seen_collapsed_ids.add(obj_id)
            continue
        survivors.append(f)

    # Append any merged features whose name doesn't match an absorbed
    # feature (the common case — synth label is new).
    for cf in collapsed_features:
        if id(cf) not in seen_collapsed_ids:
            survivors.append(cf)
            seen_collapsed_ids.add(id(cf))

    if log is not None and groups:
        log.info(
            f"sibling_collapse: {len(groups)} group(s); "
            f"{sum(g.member_count for g in groups)} features absorbed; "
            f"post-count {len(survivors)}",
        )

    return Stage53Result(features=survivors, collapse_groups=groups)


__all__ = [
    "MIN_SIBLINGS",
    "MIN_PARENT_DEPTH",
    "CollapseGroup",
    "Stage53Result",
    "collapse_sibling_routes",
]
