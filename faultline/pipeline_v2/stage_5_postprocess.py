"""Stage 5 — post-process naming-discipline pass (no LLM).

Pure Python. Applies the Fix A/B/C/D + bare-``references`` subset of
``faultline.analyzer.post_process`` to the merged Stage 2 + Stage 4
``DeveloperFeature`` list, then upgrades each survivor to a public
:class:`faultline.models.types.Feature` ready for Stage 6 metrics
enrichment.

What this stage does
====================

  1. Convert ``DeveloperFeature`` (the pipeline-v2 internal record) to
     ``Feature`` (the public schema record), preserving the Layer 1
     fields ``layer="developer"`` + ``product_feature_id=None``.
  2. **Sprint A1** — validate FALLBACK (Stage 4) features against two
     quality gates BEFORE naming discipline runs:

       - **filesystem-existence**: every ``path`` in a fallback feature
         must resolve to an extant file under ``ctx.repo_path``. LLMs
         occasionally hallucinate paths; this catches them cleanly.
       - **anchor Jaccard dedup**: if a fallback feature's slug tokens
         overlap a Stage 2 anchor at Jaccard ≥ 0.7, the anchor wins
         (deterministic provenance is more trustworthy).

     Deterministic features (Stage 2) are NEVER validated — they are
     ground truth by construction. Only Stage 4 residuals run the
     gate.

  3. Run a TRIMMED subset of ``post_process`` on the surviving features:

       - Fix A — empty-name drop
       - Fix B — uncategorized catch-all drop
       - Fix C — demo / references / examples package drop
       - bare-``references`` shared-infra drop (post_process commit
         7067839, via ``_NOISE_NAMES``)
       - Fix D — ``_slugify_names`` final-pass normalisation

  4. Skip the legacy aggregator paths:

       - ``merge_sub_features``        — sonnet_scanner-specific
       - ``reattribute_noise_files``   — pre-existing data
       - ``refine_by_path_signal``     — pre-existing data
       - ``extract_overlooked_top_dirs`` — Go-style monolith bias
       - ``commit_prefix_enrichment_pass`` — git-prefix mining
       - The mega-bucket / triple-slug / marketing-flow branches of
         ``drop_noise_features`` (sonnet-scanner output shapes only).

What this stage does NOT do
===========================

  - No LLM calls.
  - No mutation of ``Feature.layer`` or ``Feature.product_feature_id``.
  - No flow rewriting (Stage 3 owns flows; this only filters by name).
  - No filesystem-validation on Stage 2 deterministic features. Their
    paths come from extractor manifests / route scans — already on disk
    by construction.

Idempotent on identical input.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from faultline.analyzer.post_process import (
    _DEMO_PREFIXES,
    _NOISE_NAMES,
    _is_uncategorized,
    _slugify_names,
)
from faultline.models.types import Feature, Flow, FlowSymbolAttribution
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    _jaccard,
    _slug_tokens,
)
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows, FlowSpec

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# Jaccard threshold for "fallback feature duplicates a Stage 2 anchor".
# Matches the merge threshold in stage_2_reconcile._should_merge so the
# semantics line up: if Stage 2 would have merged them, Stage 5 drops
# the fallback in favour of the deterministic anchor.
_ANCHOR_DEDUP_JACCARD = 0.7


# ── Telemetry shape ───────────────────────────────────────────────────────


@dataclass
class Stage5Drops:
    """Per-reason counters for fallback-feature validation drops.

    Surfaced in ``scan_meta.validation_drops`` by the orchestrator.
    """

    filesystem_missing: int = 0
    anchor_duplicate: int = 0
    junk_name: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "filesystem_missing": self.filesystem_missing,
            "anchor_duplicate": self.anchor_duplicate,
            "junk_name": self.junk_name,
        }


@dataclass
class DedupMerge:
    """One sibling-workspace merge event for telemetry / diagnostics."""

    merged_name: str
    from_names: list[str]
    from_paths_sample: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "merged_name": self.merged_name,
            "from": list(self.from_names),
            "paths_sample": list(self.from_paths_sample),
        }


@dataclass
class Stage5Result:
    """Output of :func:`stage_5_postprocess`.

    Attributes:
        features: surviving public Feature records (Layer 1).
        validation_drops: per-reason counters for telemetry.
        drop_log: list of ``(name, reason)`` tuples for the StageLogger.
        dedup_merges: sibling-workspace merge events (Sprint S1).
    """

    features: list[Feature]
    validation_drops: Stage5Drops = field(default_factory=Stage5Drops)
    drop_log: list[tuple[str, str]] = field(default_factory=list)
    dedup_merges: list[DedupMerge] = field(default_factory=list)


# ── DeveloperFeature → public Feature conversion ──────────────────────────


def _flow_spec_to_flow(spec: FlowSpec) -> Flow:
    """Bridge :class:`FlowSpec` into the public :class:`Flow` schema.

    Stage 6 will enrich the Flow with git-blame data (authors,
    timeline, bug-fix metrics). For now we emit the minimal shape so
    serialisation roundtrips cleanly.
    """
    # Sprint C1 — prefer call-graph reach when populated. Falls back
    # to the entry_point_file single-path shape for backward-compat
    # with tests that construct FlowSpec without running Stage 3's
    # reach post-pass.
    if spec.reach_paths:
        paths = list(spec.reach_paths)
    elif spec.entry_point_file:
        paths = [spec.entry_point_file]
    else:
        paths = []
    # Sprint C2 — bridge per-flow symbol attributions to the public
    # ``FlowSymbolAttribution`` pydantic model. ``FlowSpec`` stores
    # tuples of ``flow_symbols.FlowSymbolAttribution`` (a frozen
    # dataclass); convert each one. Defensive against legacy callers
    # that constructed FlowSpec without running the post-pass.
    flow_attrs: list[FlowSymbolAttribution] = []
    for attr in (spec.symbol_attributions or ()):
        # ``attr`` is a frozen dataclass; pull the five fields by name.
        flow_attrs.append(FlowSymbolAttribution(
            file=getattr(attr, "file", ""),
            symbol=getattr(attr, "symbol", "<file>"),
            line_start=int(getattr(attr, "line_start", 1) or 1),
            line_end=int(getattr(attr, "line_end", 1) or 1),
            role=getattr(attr, "role", "support"),
        ))
    return Flow(
        name=spec.name,
        description=spec.description or None,
        entry_point_file=spec.entry_point_file,
        entry_point_line=spec.entry_point_line,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flow_symbol_attributions=flow_attrs,
    )


def _dev_feature_to_feature(
    dev: DeveloperFeature,
    flows: list[FlowSpec] | None = None,
) -> Feature:
    """Bridge a Stage 2/3/4 :class:`DeveloperFeature` to a public
    :class:`Feature`. Layer 1 fields are stamped explicitly so the
    downstream FeatureMap validator routes this entry to
    ``developer_features``.
    """
    return Feature(
        name=dev.name,
        display_name=dev.display_name,
        description=dev.rationale or None,
        paths=list(dev.paths),
        authors=[],          # Stage 6 fills these.
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[_flow_spec_to_flow(f) for f in (flows or [])],
        layer="developer",
        product_feature_id=None,
    )


def _is_demo_name(name: str) -> bool:
    """Replicates Fix C's demo / references / examples drop predicate."""
    return any(
        name == p.rstrip("-/") or name.startswith(p) for p in _DEMO_PREFIXES
    )


# ── Sprint A1 — fallback validation gates ─────────────────────────────────


def _validate_fallback_paths_exist(
    feature: DeveloperFeature, repo_path: Path,
) -> str | None:
    """Return a drop-reason string if any path in ``feature.paths`` is
    missing on disk under ``repo_path``; else ``None``.

    Uses :class:`pathlib.Path` exclusively for cross-platform parity
    with the rest of the codebase (no ``os.path.exists``).
    """
    for p in feature.paths:
        # Skip blank entries defensively; ``_build_developer_features``
        # already filters them but we don't want to grant absence here.
        if not p:
            return f"path_not_found:<empty>"
        candidate = repo_path / p
        if not candidate.exists():
            return f"path_not_found:{p}"
    return None


def _find_duplicate_anchor(
    feature: DeveloperFeature, anchors: list[DeveloperFeature],
) -> tuple[str, float] | None:
    """If ``feature`` slug-Jaccards an anchor at ≥ ``_ANCHOR_DEDUP_JACCARD``,
    return ``(anchor_name, jaccard)``; else ``None``.

    Reuses Stage 2's tokeniser + Jaccard helpers so we don't introduce
    a third implementation.
    """
    feat_tokens = _slug_tokens(feature.name)
    if not feat_tokens:
        return None
    best: tuple[str, float] | None = None
    for anchor in anchors:
        anchor_tokens = _slug_tokens(anchor.name)
        if not anchor_tokens:
            continue
        score = _jaccard(feat_tokens, anchor_tokens)
        if score >= _ANCHOR_DEDUP_JACCARD and (best is None or score > best[1]):
            best = (anchor.name, score)
    return best


def _validate_residual(
    residual: list[DeveloperFeature],
    anchors: list[DeveloperFeature],
    repo_path: Path,
) -> tuple[list[DeveloperFeature], Stage5Drops, list[tuple[str, str]]]:
    """Apply the Sprint A1 fallback-validation gates.

    Returns ``(survivors, drops, drop_log)``. Order of checks:

      1. filesystem-existence (cheap I/O — paths that don't exist
         cannot be a real feature, no matter what the name looks like).
      2. anchor-Jaccard dedup (semantic — duplicates a deterministic
         anchor we already kept).

    Naming-discipline (junk-name) is NOT applied here — Fix A/B/C/D
    runs uniformly over deterministic + fallback features in the next
    pass. We bump :attr:`Stage5Drops.junk_name` from that pass via the
    drop-log so the telemetry stays whole.
    """
    survivors: list[DeveloperFeature] = []
    drops = Stage5Drops()
    drop_log: list[tuple[str, str]] = []

    for feat in residual:
        # Gate 1 — filesystem existence.
        missing = _validate_fallback_paths_exist(feat, repo_path)
        if missing is not None:
            drops.filesystem_missing += 1
            drop_log.append((feat.name, missing))
            continue
        # Gate 2 — dedup vs Stage 2 anchors.
        dup = _find_duplicate_anchor(feat, anchors)
        if dup is not None:
            anchor_name, score = dup
            drops.anchor_duplicate += 1
            drop_log.append(
                (feat.name, f"duplicate_of_anchor:{anchor_name}:jaccard={score:.2f}"),
            )
            continue
        survivors.append(feat)

    return survivors, drops, drop_log


# ── Naming-discipline pass ────────────────────────────────────────────────


def _apply_naming_discipline(
    features: list[Feature],
) -> tuple[list[Feature], list[tuple[str, str, int]]]:
    """Apply Fix A + Fix B + Fix C + bare-references + Fix D.

    Returns ``(survivors, dropped)``. ``dropped`` is a list of
    ``(name, reason, path_count)`` tuples for telemetry.
    """
    cleaned: list[Feature] = []
    dropped: list[tuple[str, str, int]] = []

    for f in features:
        name = f.name
        path_count = len(f.paths)

        # Fix A — empty-name drop.
        if not name or not name.strip():
            dropped.append((name, "empty name (Fix A)", path_count))
            continue

        # Fix B — uncategorized catch-all drop (incl. multi-slash).
        if _is_uncategorized(name):
            dropped.append((name, "uncategorized catch-all (Fix B)", path_count))
            continue

        # Fix C — demo / references / examples package drop.
        if _is_demo_name(name):
            dropped.append((name, "demo/example package (Fix C)", path_count))
            continue

        # Bare 'references' drop (post_process commit 7067839).
        if name in _NOISE_NAMES:
            dropped.append((name, "shared-infra/noise", path_count))
            continue

        cleaned.append(f)

    # Fix D — final-pass slugification.
    cleaned, slug_dropped = _slugify_names(cleaned)
    dropped.extend(
        (name, f"slug: {reason}", n) for (name, reason, n) in slug_dropped
    )

    return cleaned, dropped


# ── Sprint S1 — sibling-workspace duplicate merge ─────────────────────────
#
# Bug being fixed: ``bug-duplicate-feature-emission``.
#
# Symptom: Turborepo / pnpm-workspace / Cargo-workspace monorepos that
# host the same logical capability across MULTIPLE workspace dirs
# (``apps/image-proxy/``, ``apps/image-proxy-aws/``, ``packages/image-proxy/``)
# emit it as TWO features named ``image-proxy`` and ``image-proxy-2``.
#
# Why Stage 2 doesn't catch this:
#   1. Stage 2 merges by slug Jaccard ≥ 0.7 across deterministic anchors.
#      Two anchors with name == ``image-proxy`` *do* merge there.
#   2. The duplicate emission happens when one feature comes from a
#      deterministic source (Stage 2 → ``image-proxy``) and the other
#      comes from Stage 4 residual fallback. Stage 5's anchor-Jaccard
#      gate (threshold 0.7) catches *exact* slug-token overlap, but
#      misses pairs like ``image-proxy`` vs ``image-proxy-package``
#      (Jaccard 2/3 = 0.67) or pairs that emerge identical-then-collide
#      AFTER Fix D's ``_slugify_names`` suffixes the second one to
#      ``image-proxy-2``.
#
# Fix shape (structural, universal — no thresholds, no per-repo paths):
#   After naming discipline runs, scan the post-slugify feature list for
#   pairs (a, b) where:
#     - ``b.name == f"{a.name}-{N}"`` for some integer ``N >= 2``
#       (i.e. ``b`` is the slugify-collision-suffix of ``a``)
#     - ``a.paths`` and ``b.paths`` are disjoint (no overlap)
#     - The base token ``a.name`` (or each of its kebab tokens) appears
#       as a path component in BOTH ``a`` and ``b`` paths
#       (proves they refer to the same logical workspace member,
#       not unrelated features that happened to slug-collide)
#   When all three hold → MERGE: union paths, keep ``a``'s name and
#   display_name, prefer the longer description, sum total_commits,
#   union authors, take max health_score.
#
# Memory: ``bug-duplicate-feature-emission`` documents the symptom on
# inbox-zero (``Image Proxy`` + ``image-proxy-2``). The fix lives here
# because Stage 5 is the LAST point where slug collisions can be
# observed — earlier stages emit at different granularity.

_SUFFIX_RE = re.compile(r"^(?P<base>.+?)-(?P<n>\d+)$")


def _path_components(paths: list[str]) -> set[str]:
    """Return the set of all path components across ``paths``.

    Components are kebab-normalised (lowercase, underscores → dashes)
    so a folder named ``image_proxy`` matches the slug ``image-proxy``.
    """
    out: set[str] = set()
    for p in paths:
        if not p:
            continue
        for part in PurePosixPath(p).parts:
            if not part or part == ".":
                continue
            normalised = part.lower().replace("_", "-")
            out.add(normalised)
    return out


def _shares_workspace_token(
    base_slug: str,
    a_paths: list[str],
    b_paths: list[str],
) -> bool:
    """``True`` if the base slug (or one of its kebab tokens) appears as
    a path component in BOTH ``a_paths`` and ``b_paths``.

    Structural rule from the bug-memory: this proves both features live
    in workspace dirs that share the same member token (``apps/X/...``
    + ``packages/X/...``), not unrelated features that happened to
    suffix-collide.
    """
    a_components = _path_components(a_paths)
    b_components = _path_components(b_paths)
    if not a_components or not b_components:
        return False
    # Try the whole base slug first (handles ``image-proxy`` directly
    # if a workspace dir is named that), then each kebab token.
    candidates: list[str] = [base_slug]
    candidates.extend(t for t in base_slug.split("-") if t)
    for token in candidates:
        if not token:
            continue
        a_match = any(token in c for c in a_components)
        b_match = any(token in c for c in b_components)
        if a_match and b_match:
            return True
    return False


def _merge_two_features(primary: Feature, sibling: Feature) -> Feature:
    """Fold ``sibling`` into ``primary``, returning the merged Feature.

    Path-union preserves order from ``primary`` then appends new paths
    from ``sibling`` (deterministic). Display_name + description from
    ``primary`` win; ``sibling`` values fall back only if ``primary``
    is empty. Numerics combine additively (commits, bug_fixes) and
    by max (health_score). Authors union. Flows concatenated (Stage 5.5
    bipartite store will re-key them).
    """
    seen_paths: set[str] = set(primary.paths)
    combined_paths: list[str] = list(primary.paths)
    for p in sibling.paths:
        if p not in seen_paths:
            combined_paths.append(p)
            seen_paths.add(p)

    combined_authors: list[str] = list(primary.authors)
    seen_authors: set[str] = set(primary.authors)
    for a in sibling.authors:
        if a not in seen_authors:
            combined_authors.append(a)
            seen_authors.add(a)

    total_commits = (primary.total_commits or 0) + (sibling.total_commits or 0)
    bug_fixes = (primary.bug_fixes or 0) + (sibling.bug_fixes or 0)
    bug_fix_ratio = (bug_fixes / total_commits) if total_commits > 0 else 0.0
    last_modified = max(primary.last_modified, sibling.last_modified)
    health_score = max(primary.health_score or 0.0, sibling.health_score or 0.0)

    return Feature(
        name=primary.name,
        display_name=primary.display_name or sibling.display_name,
        description=primary.description or sibling.description,
        paths=combined_paths,
        authors=combined_authors,
        total_commits=total_commits,
        bug_fixes=bug_fixes,
        bug_fix_ratio=round(bug_fix_ratio, 4),
        last_modified=last_modified,
        health_score=health_score,
        flows=list(primary.flows) + list(sibling.flows),
        bug_fix_prs=list(primary.bug_fix_prs) + list(sibling.bug_fix_prs),
        coverage_pct=(
            primary.coverage_pct if primary.coverage_pct is not None
            else sibling.coverage_pct
        ),
        shared_attributions=(
            list(primary.shared_attributions)
            + list(sibling.shared_attributions)
        ),
        participants=list(primary.participants) + list(sibling.participants),
        symbol_health_score=(
            primary.symbol_health_score
            if primary.symbol_health_score is not None
            else sibling.symbol_health_score
        ),
        shared_participants=(
            list(primary.shared_participants)
            + list(sibling.shared_participants)
        ),
        layer=primary.layer,
        product_feature_id=(
            primary.product_feature_id or sibling.product_feature_id
        ),
    )


def _merge_sibling_workspace_duplicates(
    features: list[Feature],
) -> tuple[list[Feature], list[DedupMerge]]:
    """Merge sibling-workspace duplicates introduced by slug suffixing.

    Runs AFTER :func:`_apply_naming_discipline`. For each suffixed
    feature ``X-N`` (N integer ≥ 2), checks whether the un-suffixed
    base ``X`` is also present AND the two features satisfy the
    structural guard (disjoint paths + shared workspace-member token).
    When both hold, the suffixed feature is folded into the base.

    Returns ``(survivors, merges)``. ``merges`` is a list of
    :class:`DedupMerge` records suitable for ``scan_meta`` telemetry.

    Edge cases handled:
      - Multiple suffixes of the same base (``X``, ``X-2``, ``X-3``)
        all collapse into ``X`` in one pass.
      - The base ``X`` may itself not be present (``X-2`` and ``X-3``
        but no ``X``) — in that case we leave them alone; the dedup
        only fires when the un-suffixed base anchor exists.
      - Order-independent: surviving features keep their original
        position in the input list.
    """
    if not features:
        return [], []

    by_name: dict[str, Feature] = {f.name: f for f in features}
    # ``base_slug -> list of (suffix_int, feature)``
    suffix_groups: dict[str, list[tuple[int, Feature]]] = {}
    for f in features:
        m = _SUFFIX_RE.match(f.name)
        if not m:
            continue
        base = m.group("base")
        try:
            n = int(m.group("n"))
        except ValueError:
            continue
        if n < 2:
            continue
        # Only consider suffixed siblings if the un-suffixed base
        # actually exists in this feature list. Otherwise the suffix
        # might be intentional (e.g. ``v2``, ``http2``) rather than a
        # collision-resolver artefact.
        if base not in by_name:
            continue
        suffix_groups.setdefault(base, []).append((n, f))

    if not suffix_groups:
        return list(features), []

    # Plan merges: for each base, attempt to absorb each suffixed
    # sibling IFF it passes the structural guard.
    absorbed_names: set[str] = set()
    merges: list[DedupMerge] = []
    # Track running merged feature per base so consecutive absorptions
    # combine cleanly (paths union grows across multiple siblings).
    merged_by_base: dict[str, Feature] = {}

    for base, suffixed in suffix_groups.items():
        primary = by_name[base]
        running = primary
        absorbed_for_base: list[str] = []
        # Process in suffix-order for determinism (-2 before -3).
        for _, sibling in sorted(suffixed, key=lambda t: t[0]):
            if not _shares_workspace_token(
                base, list(running.paths), list(sibling.paths),
            ):
                continue
            # Strict disjoint-paths guard: if the two features share any
            # path the cross-feature attribution pass in Stage 2 should
            # have already handled them — skip to avoid double-counting.
            running_paths = set(running.paths)
            if running_paths & set(sibling.paths):
                continue
            running = _merge_two_features(running, sibling)
            absorbed_names.add(sibling.name)
            absorbed_for_base.append(sibling.name)

        if absorbed_for_base:
            merged_by_base[base] = running
            sample_paths = list(running.paths)[:6]
            merges.append(
                DedupMerge(
                    merged_name=base,
                    from_names=[base, *absorbed_for_base],
                    from_paths_sample=sample_paths,
                ),
            )

    if not absorbed_names:
        return list(features), []

    survivors: list[Feature] = []
    for f in features:
        if f.name in absorbed_names:
            continue
        if f.name in merged_by_base:
            survivors.append(merged_by_base[f.name])
        else:
            survivors.append(f)
    return survivors, merges


# ── Public entry point ────────────────────────────────────────────────────


def stage_5_postprocess(
    deterministic: list[DeveloperFeature],
    residual: list[DeveloperFeature],
    flows_by_feature: dict[str, list[FlowSpec]] | None = None,
    ctx: "ScanContext | None" = None,
) -> list[Feature]:
    """Validate + naming-discipline the merged feature list.

    Sprint A1 ordering:

      1. Validate residual (Stage 4) features against filesystem +
         anchor-dedup gates. Deterministic features are exempt.
      2. Concatenate survivors with deterministic features.
      3. Apply Fix A/B/C/D + bare-references naming discipline.

    Args:
        deterministic: Stage 2 reconciled features (high/medium
            confidence).
        residual: Stage 4 LLM-fallback features (low confidence).
        flows_by_feature: optional ``{feature_name: [FlowSpec, ...]}``
            mapping from Stage 3. When None, every feature emits with
            an empty ``flows`` list.
        ctx: Stage 0 context. Required when ``residual`` is non-empty
            (the filesystem-existence gate needs ``ctx.repo_path``).
            For backwards compatibility, when ``ctx is None`` AND
            ``residual`` is non-empty we skip filesystem validation
            and emit a warning via the module logger.

    Returns:
        list of :class:`Feature` records with naming discipline applied
        and ``layer="developer"`` stamped.

    Note:
        Callers that need the per-reason drop counters (orchestrator
        building ``scan_meta.validation_drops``) should use
        :func:`stage_5_postprocess_with_telemetry` instead, which
        returns a :class:`Stage5Result`.
    """
    return stage_5_postprocess_with_telemetry(
        deterministic=deterministic,
        residual=residual,
        flows_by_feature=flows_by_feature,
        ctx=ctx,
    ).features


def stage_5_postprocess_with_telemetry(
    deterministic: list[DeveloperFeature],
    residual: list[DeveloperFeature],
    flows_by_feature: dict[str, list[FlowSpec]] | None = None,
    ctx: "ScanContext | None" = None,
) -> Stage5Result:
    """Telemetry-rich variant of :func:`stage_5_postprocess`.

    Returns a :class:`Stage5Result` exposing both the surviving
    features AND the per-reason fallback drop counters + drop log.
    """
    flows_by_feature = flows_by_feature or {}

    # ── Step 1 — validate fallback features ─────────────────────────
    validated_residual: list[DeveloperFeature]
    drops = Stage5Drops()
    drop_log: list[tuple[str, str]] = []

    if residual:
        if ctx is None:
            logger.warning(
                "stage_5_postprocess: residual features present but ctx=None;"
                " skipping filesystem-existence validation (paths may not exist)",
            )
            validated_residual = list(residual)
        else:
            validated_residual, drops, drop_log = _validate_residual(
                list(residual), list(deterministic), Path(ctx.repo_path),
            )
            for name, reason in drop_log:
                logger.info(
                    "stage_5_postprocess: dropped fallback %s (%s)", name, reason,
                )
    else:
        validated_residual = []

    # ── Step 2 — assemble combined list + convert to public Feature ─
    combined: list[DeveloperFeature] = list(deterministic) + validated_residual
    public_features: list[Feature] = [
        _dev_feature_to_feature(dev, flows_by_feature.get(dev.name, []))
        for dev in combined
    ]

    # ── Step 3 — naming discipline (Fix A/B/C/D) ────────────────────
    cleaned, name_dropped = _apply_naming_discipline(public_features)

    # Track which fallback features were dropped by naming discipline
    # so the validation_drops telemetry stays whole.
    fallback_names = {f.name for f in validated_residual}
    for name, reason, n in name_dropped:
        logger.info(
            "stage_5_postprocess: dropped %s (%s, %d files)", name, reason, n,
        )
        if name in fallback_names:
            drops.junk_name += 1
            drop_log.append((name, f"junk_name:{reason}"))

    # ── Step 4 — sibling-workspace duplicate merge (Sprint S1) ──────
    cleaned, dedup_merges = _merge_sibling_workspace_duplicates(cleaned)
    for m in dedup_merges:
        logger.info(
            "stage_5_postprocess: dedup-merged %s ← %s",
            m.merged_name, m.from_names,
        )
        drop_log.append(
            (m.merged_name, f"dedup_merged_from:{','.join(m.from_names[1:])}"),
        )

    return Stage5Result(
        features=cleaned,
        validation_drops=drops,
        drop_log=drop_log,
        dedup_merges=dedup_merges,
    )


# ── Convenience adapter for callers using FeatureWithFlows ────────────────


def stage_5_from_stage3_result(
    deterministic: list[DeveloperFeature],
    stage3_features_with_flows: list[FeatureWithFlows],
    residual: list[DeveloperFeature],
    ctx: "ScanContext | None" = None,
) -> list[Feature]:
    """Variant for callers that already hold the Stage 3 output shape.

    Builds the ``flows_by_feature`` index from ``stage3_features_with_flows``
    keyed by ``feature.name``, then delegates to :func:`stage_5_postprocess`.

    Note: Stage 4 residual features carry no flows (Stage 3 ran before
    Stage 4) — they emit with ``flows=[]``.
    """
    flows_by_feature = {
        fwf.feature.name: fwf.flows for fwf in stage3_features_with_flows
    }
    return stage_5_postprocess(
        deterministic=deterministic,
        residual=residual,
        flows_by_feature=flows_by_feature,
        ctx=ctx,
    )


def stage_5_from_stage3_result_with_telemetry(
    deterministic: list[DeveloperFeature],
    stage3_features_with_flows: list[FeatureWithFlows],
    residual: list[DeveloperFeature],
    ctx: "ScanContext | None" = None,
) -> Stage5Result:
    """Telemetry-rich :func:`stage_5_from_stage3_result` for orchestrators."""
    flows_by_feature = {
        fwf.feature.name: fwf.flows for fwf in stage3_features_with_flows
    }
    return stage_5_postprocess_with_telemetry(
        deterministic=deterministic,
        residual=residual,
        flows_by_feature=flows_by_feature,
        ctx=ctx,
    )


__all__ = [
    "DedupMerge",
    "Stage5Drops",
    "Stage5Result",
    "stage_5_postprocess",
    "stage_5_postprocess_with_telemetry",
    "stage_5_from_stage3_result",
    "stage_5_from_stage3_result_with_telemetry",
]
