"""Stage 2.6 — import-closure membership (deterministic, Phase 1).

Why this stage exists
=====================

File-level membership was broken for ANCHOR features (the heart of the
product per the 2026-06 external accuracy review): a route / FastAPI /
MVC feature kept ONLY its anchor files, while the service / util /
component files those anchors import ended up in Stage-4 residual
junk-drawers (or nowhere). Measured baseline (eval/membership/BASELINE.md):
documenso micro file P=0.057 R=0.423; inbox-zero P=0.155 R=0.300.

Five root causes, and how this stage answers them:

  1. *No stage owned "pull imported files into the anchor feature"* —
     this stage owns it now.
  2. *Stage 3 flow-reach is gated by MIN_EXPORTS_FOR_FLOW_DETECTION=3*
     (typical FastAPI router has 0–2 exports) — the gate stays (it
     guards LLM cost) but route-anchored features now get deterministic
     closure regardless of export count.
  3. *Stage 6.3's structural fallback seeds from the first function* —
     this stage seeds from the WHOLE FILE (the union of every
     function's imports), and Stage 6.3 CASE E was fixed to seed all
     functions.
  4. *Orphan service files fall into Stage-4 junk-drawers* — files
     attached here are removed from the unattributed pool BEFORE
     Stage 4 runs, so junk-drawers shrink organically. That is why this
     pass runs at 2.6 (between Stage 2 reconcile and Stage 3/4) rather
     than as a 6.35 sibling of the import-tree enrichment: Stage 4
     consumes the pool, and Stage 6 metrics / 6.5 clustering / Stage 8
     Layer-2 all consume ``feature.paths`` downstream.
  5. *Ownership was exclusive* — the new model is PRIMARY + SHARED:
     ``feature.paths`` stays the exclusive primary surface (metrics
     attribute commits by primary only — no double counting), while
     ``member_files`` records every claim with role / confidence /
     evidence (see :class:`faultline.models.types.MemberFile`).

Algorithm
=========

For each anchor-sourced feature (sources intersecting
:data:`ANCHOR_SOURCES` — declared entry points only):

  1. **Closure BFS** over static imports, seeded from ALL anchor files
     (directory paths expand to the tracked source files under them).
     File-level: a file's outgoing edges are the union of every import
     statement in it — no first-function bias. Depth is bounded by
     Stage 6.3's existing budget (:data:`DEFAULT_MAX_DEPTH`), newly
     reached files per feature by :data:`DEFAULT_MAX_FILES_PER_FEATURE`,
     vendor/test files excluded by the existing markers. Resolution
     reuses Stage 6.3's machinery (tsconfig alias map + relative
     fallback for TS/JS, dotted-module resolution for Python).
  2. **Fan-in cap** — the guard against "every feature = whole repo".
     A candidate file claimed (reached) by many distinct features is
     shared infrastructure. The threshold is scale-invariant:

         T = max(3, P90 of the per-file claim-count distribution)

     i.e. a file in the top decile of the repo's OWN fan-in
     distribution, with a structural floor of 3 ("three independent
     claimants" — same convention as Stage 4's saturation window; a
     pairwise share is not infrastructure). Files at or above T get
     ``role="shared"`` provenance on every claimant and stay in the
     unattributed pool (Stage 4 may still name them honestly as a
     shared-infra cluster).
  3. **Primary election** — below the cap, the file attaches to exactly
     ONE feature's ``paths``: highest confidence first (closure
     confidence = 1/(1+depth), so the shallowest importer wins), then
     highest source priority (Stage 2's ``_SOURCE_PRIORITY``), then
     feature name ascending (deterministic). Losing claimants keep a
     non-primary ``role="closure"`` provenance record.
  4. **Co-commit secondary signal** (cheap — ``ctx.commits`` is already
     in memory): a still-unattributed file F attaches to feature A when
     F co-occurs with A's anchor files in ≥ k commits AND that is a
     majority (≥ 50 %) of F's own commits. k is scale-invariant:
     max(2, P75 of the repo's own nonzero co-commit counts) — and
     commits larger than the repo's P95 commit size are ignored
     (formatting / vendoring sweeps carry no coupling signal). Ties on
     (share, count) between two features → skip (ambiguous beats
     wrong). Confidence = min(0.45, share): always below a direct
     depth-1 import (0.5). This catches configs / migrations invisible
     to imports while staying precision-first.

Every existing path of every deterministic feature is also recorded as
``role="anchor"`` (confidence 1.0, primary) so ``member_files`` is a
complete ledger, not a delta.

NO LLM. NO network. Deterministic given the repo + git window.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.tsconfig_paths import (
    AliasEntry,
    build_path_alias_map,
    resolve_ts_import,
)
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    _priority,
)
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FILES_PER_FEATURE,
    _SourceCache,
    _fallback_relative_resolve,
    _is_vendor_or_test,
    _resolve_py_module_simple,
    _suffix,
    _SLICEABLE_EXTENSIONS,
    _TS_EXTS,
)

if TYPE_CHECKING:
    from faultline.models.types import Commit, MemberFile
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Anchor-source set ────────────────────────────────────────────────────
#
# Declared entry points only — sources whose paths the maintainer
# explicitly registered as a route / controller surface. Package /
# library / schema / config sources are NOT closure roots: their paths
# are containers or supporting evidence, and Stage 6.3 already handles
# their reverse-import expansion.

ANCHOR_SOURCES: frozenset[str] = frozenset({
    "route",
    "fastapi-route",
    "route-fastify",
    "route-express",
    "go-router",
    "django-route",
    "rails-routes",
    "mvc",
})

# Structural floor for the fan-in cap: a file must be claimed by at
# least three distinct features before it can be called shared
# infrastructure (same "three sources of confirmation" convention as
# Stage 4's SAT_WINDOW — a pairwise share is a legitimate attachment).
_FAN_IN_FLOOR = 3

# Co-commit gate constants (both are structural, not tuned):
#   - the share of F's own commits that must co-occur with the anchors
#     is a MAJORITY boundary (0.5), a ratio that behaves identically on
#     a 10-commit file and a 1000-commit file;
#   - the absolute count floor is 2 ("two sources of confirmation" —
#     one shared commit is no evidence), raised by the repo's own P75
#     when the repo is co-commit-dense.
_CO_COMMIT_MAJORITY = 0.5
_CO_COMMIT_COUNT_FLOOR = 2
# Co-commit confidence cap: strictly below a depth-1 import claim
# (1 / (1 + 1) = 0.5) so a direct static import always outranks git
# coincidence in the primary election.
_CO_COMMIT_CONFIDENCE_CAP = 0.45


# ── Result dataclasses ───────────────────────────────────────────────────


@dataclass
class ClosureTelemetry:
    """Aggregate telemetry for the stage artifact + scan_meta."""

    anchor_features: int = 0
    candidate_files: int = 0
    fan_in_threshold: int = 0
    closure_attached: int = 0
    co_commit_attached: int = 0
    shared_infra_files: int = 0
    reclaimed_dir_grained: int = 0
    unattributed_before: int = 0
    unattributed_after: int = 0
    elapsed_sec: float = 0.0
    per_feature: list[dict[str, Any]] = field(default_factory=list)
    attached_sample: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor_features": self.anchor_features,
            "candidate_files": self.candidate_files,
            "fan_in_threshold": self.fan_in_threshold,
            "closure_attached": self.closure_attached,
            "co_commit_attached": self.co_commit_attached,
            "shared_infra_files": self.shared_infra_files,
            "reclaimed_dir_grained": self.reclaimed_dir_grained,
            "unattributed_before": self.unattributed_before,
            "unattributed_after": self.unattributed_after,
            "elapsed_sec": self.elapsed_sec,
            "per_feature": self.per_feature,
            "attached_sample": self.attached_sample,
        }


@dataclass
class ClosureResult:
    """Public output of :func:`run_membership_closure`.

    ``features`` is the SAME list the caller passed in (features are
    mutated in place — paths extended, member_files populated).
    ``unattributed`` is the shrunken pool Stage 4 should consume.
    """

    features: list[DeveloperFeature]
    unattributed: list[str]
    telemetry: ClosureTelemetry


# ── Percentile helper (nearest-rank, matches stage_6_metrics style) ──────


def _nearest_rank(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile of a pre-sorted nonempty int list."""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    rank = max(1, -(-int(pct * 100) * n // 100))  # ceil(pct * n), 1-indexed
    return sorted_values[min(rank, n) - 1]


# ── Import resolution (file-level) ───────────────────────────────────────


def _resolve_file_imports(
    rel: str,
    cache: _SourceCache,
    alias_map: list[AliasEntry],
    tracked_files: frozenset[str],
) -> list[str]:
    """Return the in-repo files ``rel`` statically imports.

    Whole-file granularity: the union of every import statement in the
    file (no symbol/body filtering — that is exactly the first-function
    bias this stage exists to avoid). Order is deterministic (sorted).
    """
    suffix = _suffix(rel)
    if suffix not in _SLICEABLE_EXTENSIONS:
        return []
    imports = cache.imports(rel)
    if not imports:
        return []
    targets: set[str] = set()
    # Dedup by specifier: many locals share one module.
    for spec in set(imports.values()):
        target: str | None
        if suffix in _TS_EXTS:
            target = resolve_ts_import(
                rel, spec, alias_map=alias_map, tracked_files=tracked_files,
            )
            if target is None:
                target = _fallback_relative_resolve(rel, spec, tracked_files)
        else:
            target = _resolve_py_module_simple(rel, spec, tracked_files)
        if target and target != rel:
            targets.add(target)
    return sorted(targets)


# ── Anchor-file seeding ──────────────────────────────────────────────────


def _seed_files_for(
    feature: DeveloperFeature,
    tracked_files: frozenset[str],
) -> list[str]:
    """Expand the feature's paths to concrete tracked seed files.

    File entries pass through; directory entries contribute the
    sliceable source files under them. Vendor/test files never seed.
    """
    seeds: list[str] = []
    seen: set[str] = set()
    for p in feature.paths:
        if not p or p == ".":
            continue
        norm = p.rstrip("/")
        if norm in tracked_files:
            candidates = [norm]
        else:
            prefix = norm + "/"
            candidates = sorted(
                f for f in tracked_files if f.startswith(prefix)
            )
        for c in candidates:
            if c in seen:
                continue
            if _is_vendor_or_test(c):
                continue
            if _suffix(c) not in _SLICEABLE_EXTENSIONS:
                continue
            seen.add(c)
            seeds.append(c)
    return seeds


def _closure_for_feature(
    seeds: list[str],
    *,
    cache: _SourceCache,
    alias_map: list[AliasEntry],
    tracked_files: frozenset[str],
    max_depth: int,
    max_new_files: int,
) -> dict[str, int]:
    """BFS the import graph from ``seeds``; return ``{file: min_depth}``
    for every NON-SEED file reached (depth ≥ 1).

    The BFS traverses THROUGH any non-vendor tracked file (including
    files owned by other features — a route may reach an orphan util
    via an owned service), but the caller only claims files from the
    unattributed pool.
    """
    depths: dict[str, int] = {}
    seed_set = set(seeds)
    visited: set[str] = set(seeds)
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    while queue:
        if len(depths) >= max_new_files:
            break
        rel, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for target in _resolve_file_imports(
            rel, cache, alias_map, tracked_files,
        ):
            if target in visited:
                continue
            visited.add(target)
            if target not in tracked_files or _is_vendor_or_test(target):
                continue
            if target not in seed_set:
                depths[target] = depth + 1
                if len(depths) >= max_new_files:
                    return depths
            queue.append((target, depth + 1))
    return depths


# ── Member-file helpers ──────────────────────────────────────────────────


def _member_file(
    path: str,
    role: str,
    confidence: float,
    evidence: str,
    primary: bool,
) -> "MemberFile":
    from faultline.models.types import MemberFile

    return MemberFile(
        path=path,
        role=role,  # type: ignore[arg-type]
        confidence=round(confidence, 4),
        evidence=evidence,
        primary=primary,
    )


def _closure_confidence(depth: int) -> float:
    """Monotone-decaying confidence for a closure claim at ``depth``.

    1 / (1 + depth): direct import = 0.5, two hops = 0.333, … —
    a fixed functional form, not a tuned constant.
    """
    return 1.0 / (1.0 + depth)


def _max_source_priority(feature: DeveloperFeature) -> int:
    return max((_priority(s) for s in feature.sources), default=0)


# ── Co-commit signal ─────────────────────────────────────────────────────


def _co_commit_claims(
    *,
    anchor_features: list[DeveloperFeature],
    anchor_files_by_feature: dict[str, set[str]],
    unattributed: set[str],
    commits: list["Commit"],
) -> dict[str, tuple[str, int, float]]:
    """Compute co-commit attachments.

    Returns ``{file: (feature_name, co_count, share)}`` for every
    unattributed file that passes the gate with a UNIQUE best feature.
    """
    if not commits or not anchor_features:
        return {}

    # Ignore sweeping commits (> P95 of the repo's own commit-size
    # distribution): formatting / vendoring / mass-rename commits touch
    # everything and carry no coupling signal.
    sizes = sorted(len(c.files_changed) for c in commits)
    max_size = _nearest_rank(sizes, 0.95)
    usable = [
        c for c in commits
        if c.files_changed and len(c.files_changed) <= max(1, max_size)
    ]

    co: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    for commit in usable:
        changed = set(commit.files_changed)
        orphans = changed & unattributed
        if not orphans:
            continue
        touched_features = [
            f.name for f in anchor_features
            if anchor_files_by_feature[f.name] & changed
        ]
        for fp in orphans:
            totals[fp] += 1
            for name in touched_features:
                co[fp][name] += 1

    if not co:
        return {}

    # Scale-invariant count floor: max(2, P75 of nonzero co-counts).
    all_counts = sorted(
        cnt for by_feat in co.values() for cnt in by_feat.values()
    )
    k = max(_CO_COMMIT_COUNT_FLOOR, _nearest_rank(all_counts, 0.75))

    prio = {f.name: _max_source_priority(f) for f in anchor_features}
    out: dict[str, tuple[str, int, float]] = {}
    for fp, by_feat in co.items():
        tot = totals.get(fp, 0)
        if tot <= 0:
            continue
        passing = [
            (cnt / tot, cnt, prio.get(name, 0), name)
            for name, cnt in by_feat.items()
            if cnt >= k and (cnt / tot) >= _CO_COMMIT_MAJORITY
        ]
        if not passing:
            continue
        passing.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
        best = passing[0]
        if len(passing) > 1:
            second = passing[1]
            if (second[0], second[1]) == (best[0], best[1]):
                # Two features tie on (share, count) — ambiguous beats
                # wrong; leave the file for Stage 4.
                continue
        out[fp] = (best[3], best[1], best[0])
    return out


# ── Public entry point ───────────────────────────────────────────────────


def run_membership_closure(
    features: list[DeveloperFeature],
    unattributed: list[str],
    ctx: "ScanContext",
    *,
    log: "StageLogger | None" = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_new_files_per_feature: int = DEFAULT_MAX_FILES_PER_FEATURE,
) -> ClosureResult:
    """Run the Stage 2.6 import-closure membership pass.

    Mutates ``features`` in place (paths + member_files) and returns
    the shrunken unattributed pool. See the module docstring for the
    full rule set.
    """
    t0 = time.monotonic()
    telemetry = ClosureTelemetry()
    telemetry.unattributed_before = len(unattributed)

    repo_path = Path(ctx.repo_path)
    tracked_files = frozenset(ctx.tracked_files)
    unattributed_set = set(unattributed)

    by_name = {f.name: f for f in features}

    # Files some feature lists EXPLICITLY (exact path entry, not via a
    # directory prefix). Explicitly-listed files are settled ownership;
    # directory-grained coverage is weak evidence (junk-drawer
    # directories swallow whole trees), so dir-covered files remain
    # claimable by the closure — the strongest specific evidence wins
    # the primary slot, per the review's primary+shared model.
    # Majority-claim guard: a "feature" explicitly listing at least
    # half of the repo's tracked files is a junk drawer (e.g. a
    # package-anchor that swallowed a whole workspace), not specific
    # ownership — its claims must not shield files from the closure.
    # Structural majority rule, scale-invariant by construction.
    half_repo = len(tracked_files) / 2
    exact_owned: set[str] = {
        p
        for f in features
        if sum(1 for q in f.paths if q in tracked_files) < half_repo
        for p in f.paths
        if p in tracked_files
    }

    # ── Anchor provenance for EVERY deterministic feature ────────────
    for f in features:
        existing = {m.path for m in f.member_files}
        for p in f.paths:
            if p in existing:
                continue
            f.member_files.append(_member_file(
                p, "anchor", 1.0,
                f"stage-2 anchor (sources={','.join(f.sources)})",
                True,
            ))

    anchor_features = [
        f for f in features if ANCHOR_SOURCES & set(f.sources)
    ]
    telemetry.anchor_features = len(anchor_features)
    if not anchor_features:
        telemetry.unattributed_after = len(unattributed)
        telemetry.elapsed_sec = round(time.monotonic() - t0, 3)
        return ClosureResult(features, list(unattributed), telemetry)

    cache = _SourceCache(repo_path)
    alias_map = build_path_alias_map(repo_path)

    # ── Phase 1 — per-feature closure BFS ────────────────────────────
    anchor_files_by_feature: dict[str, set[str]] = {}
    closure_by_feature: dict[str, dict[str, int]] = {}
    for f in anchor_features:
        seeds = _seed_files_for(f, tracked_files)
        anchor_files_by_feature[f.name] = set(seeds)
        if not seeds:
            closure_by_feature[f.name] = {}
            continue
        closure_by_feature[f.name] = _closure_for_feature(
            seeds,
            cache=cache,
            alias_map=alias_map,
            tracked_files=tracked_files,
            max_depth=max_depth,
            max_new_files=max_new_files_per_feature,
        )

    # ── Phase 2 — fan-in classification over ORPHAN candidates ───────
    # Claims are only counted on files in the unattributed pool: files
    # owned by another feature are traversal waypoints, not candidates.
    claimants_by_file: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for fname, depths in closure_by_feature.items():
        for fp, depth in depths.items():
            # Claimable: true orphans AND files covered only by another
            # feature's directory prefix. Explicitly-listed files stay
            # traversal waypoints (settled ownership).
            if fp not in exact_owned:
                claimants_by_file[fp].append((fname, depth))

    telemetry.candidate_files = len(claimants_by_file)
    claim_counts = sorted(len(v) for v in claimants_by_file.values())
    fan_in_threshold = max(
        _FAN_IN_FLOOR, _nearest_rank(claim_counts, 0.90),
    )
    telemetry.fan_in_threshold = fan_in_threshold

    closure_attached_by_feature: dict[str, int] = defaultdict(int)
    attached: set[str] = set()

    for fp in sorted(claimants_by_file):
        claims = claimants_by_file[fp]
        n_claims = len(claims)
        if n_claims >= fan_in_threshold:
            # Shared infrastructure — provenance only, stays orphan.
            telemetry.shared_infra_files += 1
            for fname, depth in sorted(claims):
                by_name[fname].member_files.append(_member_file(
                    fp, "shared", _closure_confidence(depth),
                    f"import fan-in: claimed by {n_claims} features "
                    f"(threshold {fan_in_threshold}), depth {depth}",
                    False,
                ))
            continue
        # Primary election: confidence desc (depth asc), source
        # priority desc, name asc.
        ranked = sorted(
            claims,
            key=lambda c: (
                c[1],
                -_max_source_priority(by_name[c[0]]),
                c[0],
            ),
        )
        winner_name, winner_depth = ranked[0]
        winner = by_name[winner_name]
        winner.paths = tuple(winner.paths) + (fp,)
        winner.member_files.append(_member_file(
            fp, "closure", _closure_confidence(winner_depth),
            f"static import closure from anchors (depth {winner_depth})",
            True,
        ))
        closure_attached_by_feature[winner_name] += 1
        attached.add(fp)
        if fp not in unattributed_set:
            telemetry.reclaimed_dir_grained += 1
        for fname, depth in ranked[1:]:
            by_name[fname].member_files.append(_member_file(
                fp, "closure", _closure_confidence(depth),
                f"static import closure from anchors (depth {depth}); "
                f"primary={winner_name}",
                False,
            ))
        if len(telemetry.attached_sample) < 10:
            telemetry.attached_sample.append({
                "file": fp, "feature": winner_name, "role": "closure",
            })

    telemetry.closure_attached = len(attached)
    unattributed_set -= attached

    # ── Phase 3 — co-commit secondary signal ─────────────────────────
    co_claims = _co_commit_claims(
        anchor_features=anchor_features,
        anchor_files_by_feature=anchor_files_by_feature,
        unattributed=unattributed_set,
        commits=list(ctx.commits or []),
    )
    for fp in sorted(co_claims):
        fname, cnt, share = co_claims[fp]
        feat = by_name[fname]
        feat.paths = tuple(feat.paths) + (fp,)
        feat.member_files.append(_member_file(
            fp, "co-commit", min(_CO_COMMIT_CONFIDENCE_CAP, share),
            f"co-committed with anchors in {cnt} commits "
            f"({share:.0%} of the file's own commits)",
            True,
        ))
        unattributed_set.discard(fp)
        if len(telemetry.attached_sample) < 10:
            telemetry.attached_sample.append({
                "file": fp, "feature": fname, "role": "co-commit",
            })
    telemetry.co_commit_attached = len(co_claims)

    # ── Assemble output ──────────────────────────────────────────────
    new_unattributed = [p for p in unattributed if p in unattributed_set]
    telemetry.unattributed_after = len(new_unattributed)
    telemetry.elapsed_sec = round(time.monotonic() - t0, 3)
    for f in anchor_features:
        n_closure = closure_attached_by_feature.get(f.name, 0)
        n_total = len(f.paths)
        telemetry.per_feature.append({
            "name": f.name,
            "anchor_files": len(anchor_files_by_feature.get(f.name, ())),
            "closure_reached": len(closure_by_feature.get(f.name, {})),
            "closure_attached": n_closure,
            "paths_total": n_total,
        })
        if log and n_closure:
            log.emit(
                f.name,
                f"closure attached {n_closure} file(s) "
                f"(paths now {n_total})",
            )
    if log:
        log.info(
            f"closure: anchor_features={telemetry.anchor_features} "
            f"candidates={telemetry.candidate_files} "
            f"fan_in_threshold={telemetry.fan_in_threshold} "
            f"attached={telemetry.closure_attached} "
            f"co_commit={telemetry.co_commit_attached} "
            f"shared_infra={telemetry.shared_infra_files} "
            f"unattributed {telemetry.unattributed_before}"
            f"→{telemetry.unattributed_after}",
        )

    return ClosureResult(features, new_unattributed, telemetry)


__all__ = [
    "ANCHOR_SOURCES",
    "ClosureResult",
    "ClosureTelemetry",
    "run_membership_closure",
]
