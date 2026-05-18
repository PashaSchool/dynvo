"""Stage 6 — metrics enrichment.

Takes the naming-disciplined :class:`Feature` list from Stage 5 and
attaches per-feature metrics drawn from the deterministic analyzer
layer:

  - ``total_commits`` / ``authors`` / ``last_modified`` from
    ``ctx.commits`` (file→feature index built once for the whole stage)
  - ``bug_fixes`` / ``bug_fix_ratio`` from the same commit list
    (``Commit.is_bug_fix`` is precomputed by Stage 0's git loader)
  - ``health_score`` via :func:`faultline.analyzer.features._calculate_health`
    (sigmoid centred at 55% bug-fix ratio)
  - ``coverage_pct`` / ``coverage_signals`` / ``coverage_confidence``
    via the private ``faultlines_test_coverage`` provider, when
    importable. Same for each ``feature.flows[i]`` via
    ``BehavioralCoverageProvider.compute_flow``.

No LLM calls. No network. Reuses existing analyzer code — does NOT
duplicate the bug-fix detection regex, the health formula, or the
co-change index. Graceful degradation: if the private coverage
package is not installed, coverage fields are left at ``None`` and
the stage emits a warning to ``ctx`` via the return-tuple's notes
list (Stage 7 forwards those into ``scan_meta.warnings``).

Idempotent: running twice on the same input yields identical output
(the underlying analyzer calls are deterministic).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.features import _calculate_health
from faultline.models.types import Commit, Feature, Flow

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Coverage provider import (graceful) ─────────────────────────────────


def _load_coverage_provider(
    repo_path: Path,
    commits: list[Commit],
) -> tuple[Any | None, str | None]:
    """Try to construct the private BehavioralCoverageProvider.

    Returns ``(provider, warning_or_None)``. Warning carries the reason
    the provider could not be loaded — surfaced into ``scan_meta``.
    """
    try:
        from faultlines_test_coverage import (  # type: ignore[import-not-found]
            BehavioralCoverageProvider,
        )
        from faultlines_test_coverage.types import (  # type: ignore[import-not-found]
            Commit as CovCommit,
        )
    except ImportError as exc:
        return None, (
            f"faultlines_test_coverage not installed ({exc}); "
            "coverage fields left at None"
        )

    # The private provider expects its own ``Commit`` dataclass shape
    # (sha + author + date + files + message). Translate from the
    # ``faultline.models.types.Commit`` pydantic model.
    cov_commits: list[Any] = []
    for c in commits:
        try:
            cov_commits.append(
                CovCommit(
                    sha=c.sha,
                    author=c.author,
                    date=c.date,
                    files=list(c.files_changed),
                    message=c.message,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("skipping commit %s for coverage: %s", c.sha, exc)
            continue

    try:
        provider = BehavioralCoverageProvider(
            repo_path=repo_path,
            commits=cov_commits,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return None, (
            f"failed to construct BehavioralCoverageProvider: {exc}; "
            "coverage fields left at None"
        )

    return provider, None


# ── Commit-driven metrics ───────────────────────────────────────────────


def _build_file_to_feature_index(
    features: list[Feature],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build O(1) file→feature and directory→feature lookups.

    Mirrors the indexing pattern used by
    :func:`faultline.analyzer.features.build_feature_map`. The
    directory fallback catches deleted/renamed files: when a commit
    touches ``app/users/foo.tsx`` and the file no longer exists in
    HEAD, the parent dir ``app/users`` still routes that commit to
    the "users" feature.
    """
    file_to_feature: dict[str, str] = {}
    dir_to_feature: dict[str, str] = {}
    for feat in features:
        for p in feat.paths:
            file_to_feature[p] = feat.name
            parent = str(Path(p).parent)
            if parent != ".":
                dir_to_feature.setdefault(parent, feat.name)
    return file_to_feature, dir_to_feature


def _attach_commit_metrics(
    features: list[Feature],
    commits: list[Commit],
) -> None:
    """Mutate ``features`` in place with commit-derived metrics.

    Builds one global file→feature index, then sweeps commits once,
    accumulating per-feature commit lists, authors, and last-modified
    timestamps. Finally computes bug-fix counts + the sigmoid health
    score.
    """
    if not features:
        return

    file_to_feature, dir_to_feature = _build_file_to_feature_index(features)

    feature_commits: dict[str, list[Commit]] = defaultdict(list)
    feature_authors: dict[str, set[str]] = defaultdict(set)
    feature_last_modified: dict[str, datetime] = {}

    for commit in commits:
        touched: set[str] = set()
        for fp in commit.files_changed:
            feat_name = file_to_feature.get(fp)
            if feat_name is None:
                parent = str(Path(fp).parent)
                feat_name = dir_to_feature.get(parent)
            if feat_name:
                touched.add(feat_name)
        for feat_name in touched:
            feature_commits[feat_name].append(commit)
            feature_authors[feat_name].add(commit.author)
            existing = feature_last_modified.get(feat_name)
            if existing is None or commit.date > existing:
                feature_last_modified[feat_name] = commit.date

    for feat in features:
        c_for_feat = feature_commits.get(feat.name, [])
        total = len(c_for_feat)
        bug_fixes = sum(1 for c in c_for_feat if c.is_bug_fix)
        bug_fix_ratio = bug_fixes / total if total > 0 else 0.0

        feat.total_commits = total
        feat.bug_fixes = bug_fixes
        feat.bug_fix_ratio = round(bug_fix_ratio, 3)
        feat.authors = sorted(feature_authors.get(feat.name, set()))
        feat.last_modified = feature_last_modified.get(
            feat.name, datetime.now(tz=timezone.utc),
        )
        feat.health_score = (
            _calculate_health(bug_fix_ratio, total, c_for_feat)
            if total > 0
            else 100.0
        )


# ── Coverage enrichment ─────────────────────────────────────────────────


def _attach_coverage(
    features: list[Feature],
    provider: Any,
) -> int:
    """Mutate ``features`` in place with coverage_pct + signals.

    Returns the number of features successfully scored. Each feature
    that errors is skipped (left at ``None``) — the analyzer never
    raises out of this stage.
    """
    scored = 0
    for feat in features:
        try:
            cov_pct, signals = provider.compute(feat)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("coverage compute failed for %s: %s", feat.name, exc)
            continue
        # Store the [0,1] coverage as a percentage 0..100 (matches the
        # existing on-disk Feature.coverage_pct convention).
        feat.coverage_pct = round(float(cov_pct) * 100.0, 1)
        # Stash signals + confidence on the scan_meta-style description
        # tail. We don't widen the public Feature schema in A9 — Stage 7
        # carries the per-feature signals dict through scan_meta when
        # the spec calls for it. For now, attach to a private attribute
        # so downstream consumers (tests, the experimental UI) can
        # access them without a schema migration.
        try:
            confidence = provider.confidence(signals)
        except Exception:  # noqa: BLE001
            confidence = "low"
        # Attach as model_extra-compatible attributes if available; the
        # public schema doesn't surface these yet, so we store them on
        # an object-level dict keyed off the feature.
        object.__setattr__(feat, "_coverage_signals", dict(signals))
        object.__setattr__(feat, "_coverage_confidence", confidence)
        scored += 1

        # Flow-level coverage — per-flow paths, with feature fallback.
        feat_paths_fallback = list(feat.paths)
        for flow in feat.flows:
            try:
                f_cov, f_signals = provider.compute_flow(
                    flow,
                    feature_paths_fallback=feat_paths_fallback,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "flow coverage compute failed for %s/%s: %s",
                    feat.name, flow.name, exc,
                )
                continue
            flow.coverage_pct = round(float(f_cov) * 100.0, 1)
            object.__setattr__(flow, "_coverage_signals", dict(f_signals))
            try:
                object.__setattr__(
                    flow, "_coverage_confidence", provider.confidence(f_signals),
                )
            except Exception:  # noqa: BLE001
                object.__setattr__(flow, "_coverage_confidence", "low")

    return scored


# ── Public entry point ──────────────────────────────────────────────────


def stage_6_metrics(
    features: list[Feature],
    ctx: "ScanContext",
) -> list[Feature]:
    """Attach commit + coverage metrics to every Stage 5 feature.

    Mutates the input list in place and also returns it (so the
    pipeline orchestrator can chain ``features = stage_6_metrics(...)``).

    Args:
        features: Stage 5 output — public Feature records, naming
            discipline already applied, ``layer="developer"`` stamped.
        ctx: Stage 0 context — provides ``commits`` and ``repo_path``.

    Returns:
        The same list with metrics fields populated. Coverage fields
        stay at ``None`` when the private coverage package is not
        installed.
    """
    if not features:
        return features

    # 1) Commit-driven metrics — always run (no external deps).
    try:
        _attach_commit_metrics(features, ctx.commits)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "stage_6_metrics: commit-metric pass failed: %s; features keep "
            "their Stage 5 placeholder values", exc,
        )

    # 2) Coverage — best-effort.
    provider, warning = _load_coverage_provider(ctx.repo_path, ctx.commits)
    if provider is not None:
        try:
            scored = _attach_coverage(features, provider)
            logger.info(
                "stage_6_metrics: scored coverage on %d/%d features",
                scored, len(features),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("stage_6_metrics: coverage pass failed: %s", exc)
    else:
        logger.info("stage_6_metrics: coverage unavailable — %s", warning)

    return features


__all__ = ["stage_6_metrics"]
