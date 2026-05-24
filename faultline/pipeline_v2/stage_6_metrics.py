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

from faultline.analyzer.features import (
    _build_weekly_timeline,
    _calculate_health,
    _calculate_weighted_health,
    _collect_prs,
    _compute_line_scoped_health,
    _is_test_file,
)
from faultline.models.types import Commit, Feature, Flow, TimelinePoint

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
    remote_url: str = "",
    blame_index: Any = None,
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

        # Sprint 3 — bug-fix PRs (parity with legacy analyze).
        try:
            feat.bug_fix_prs = _collect_prs(c_for_feat, remote_url)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("bug_fix_prs failed for %s: %s", feat.name, exc)

        # Sprint 3 — symbol-weighted health score.
        # Tier 1: line-scoped via BlameIndex when available + feature has
        #   participants or shared_attributions populated.
        # Tier 2: file-fraction weighting via _calculate_weighted_health
        #   when shared_attributions present but no BlameIndex.
        # Tier 3: None (default) when neither applies.
        sym_health: float | None = None
        scoring_input = (
            list(feat.participants) if feat.participants else
            list(feat.shared_attributions) if feat.shared_attributions else []
        )
        if blame_index is not None and scoring_input:
            try:
                line_scoped = _compute_line_scoped_health(
                    feat.name, scoring_input, commits, blame_index,
                )
                if line_scoped is not None:
                    sym_health = line_scoped[0]
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug("line-scoped health failed for %s: %s", feat.name, exc)
        if sym_health is None and feat.shared_attributions and total > 0:
            try:
                # File-fraction weighting needs per-commit weights.
                # Approximation: weight = attributed_lines / total_file_lines
                # per shared file, max across commit's touched files.
                file_weights = {
                    a.file_path: min(
                        a.attributed_lines / a.total_file_lines, 1.0,
                    ) if a.total_file_lines > 0 else 1.0
                    for a in feat.shared_attributions
                }
                commit_weights: dict[str, float] = {}
                for c in c_for_feat:
                    w = 0.0
                    for fp in c.files_changed:
                        if fp in file_weights:
                            w = max(w, file_weights[fp])
                        elif file_to_feature.get(fp) == feat.name:
                            w = max(w, 1.0)
                    commit_weights[c.sha] = w or 1.0
                sym_health = _calculate_weighted_health(
                    c_for_feat, commit_weights,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug("weighted health failed for %s: %s", feat.name, exc)
        feat.symbol_health_score = (
            round(sym_health, 1) if sym_health is not None else None
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


# ── Flow-level metrics (hotspots / weekly / bus_factor / trend / tests)─


def _attach_flow_metrics(
    features: list[Feature],
    commits: list[Commit],
    remote_url: str = "",
) -> None:
    """Mutate each ``Flow`` on each ``Feature`` in place with the
    historical metrics legacy ``build_flows_metrics`` produced:
    ``total_commits``, ``bug_fixes``, ``bug_fix_ratio``, ``authors``,
    ``last_modified``, ``health_score``, ``bug_fix_prs``,
    ``test_file_count``, ``weekly_points``, ``bus_factor``,
    ``health_trend``, ``hotspot_files``.

    NO LLM — pure deterministic git replay over ``commits``.
    """
    # Build global file→flow index across all features. Flow names
    # collide across features (multiple "create" flows), so key by
    # (feature_name, flow_name) tuple internally.
    file_to_flow: dict[str, tuple[str, str]] = {}
    dir_to_flow: dict[str, tuple[str, str]] = {}
    flow_lookup: dict[tuple[str, str], Flow] = {}
    flow_dirs: dict[tuple[str, str], set[str]] = {}
    flow_path_set: dict[tuple[str, str], set[str]] = {}

    for feat in features:
        for flow in feat.flows:
            key = (feat.name, flow.name)
            flow_lookup[key] = flow
            flow_path_set[key] = set(flow.paths)
            flow_dirs[key] = {str(Path(p).parent) for p in flow.paths}
            for p in flow.paths:
                file_to_flow.setdefault(p, key)
                parent = str(Path(p).parent)
                if parent != ".":
                    dir_to_flow.setdefault(parent, key)

    if not flow_lookup:
        return

    flow_commits: dict[tuple[str, str], list[Commit]] = defaultdict(list)
    flow_authors: dict[tuple[str, str], set[str]] = defaultdict(set)
    flow_last_modified: dict[tuple[str, str], datetime] = {}
    flow_test_only_commits: dict[tuple[str, str], list[Commit]] = defaultdict(list)
    seen_test_shas: dict[tuple[str, str], set[str]] = defaultdict(set)

    for commit in commits:
        touched: set[tuple[str, str]] = set()
        for fp in commit.files_changed:
            key = file_to_flow.get(fp)
            if key is None:
                parent = str(Path(fp).parent)
                key = dir_to_flow.get(parent)
            if key is not None:
                touched.add(key)
        for key in touched:
            flow_commits[key].append(commit)
            flow_authors[key].add(commit.author)
            existing = flow_last_modified.get(key)
            if existing is None or commit.date > existing:
                flow_last_modified[key] = commit.date

        # Test-only commits — capture commits that touch adjacent test
        # files inside a flow dir even if they touch no source file.
        for key, dirs in flow_dirs.items():
            if commit.sha in seen_test_shas[key]:
                continue
            for fp in commit.files_changed:
                if _is_test_file(fp) and str(Path(fp).parent) in dirs:
                    flow_test_only_commits[key].append(commit)
                    seen_test_shas[key].add(commit.sha)
                    break

    for key, flow in flow_lookup.items():
        c_for_flow = flow_commits.get(key, [])
        total = len(c_for_flow)
        bug_fixes = sum(1 for c in c_for_flow if c.is_bug_fix)
        bug_fix_ratio = bug_fixes / total if total > 0 else 0.0

        paths_set = flow_path_set[key]

        # Test files: explicit test files in flow.paths + adjacent test
        # files touched by any commit in this flow's dir.
        adjacent_test_files: set[str] = set()
        for c in flow_test_only_commits.get(key, []):
            for f in c.files_changed:
                if _is_test_file(f):
                    adjacent_test_files.add(f)
        test_file_count = (
            sum(1 for p in flow.paths if _is_test_file(p))
            + len(adjacent_test_files)
        )

        # Weekly timeline: source-file commits + test-only commits
        # deduplicated by sha.
        seen_for_timeline = {c.sha for c in c_for_flow}
        timeline_commits = list(c_for_flow) + [
            c for c in flow_test_only_commits.get(key, [])
            if c.sha not in seen_for_timeline
        ]
        try:
            weekly_points: list[TimelinePoint] = _build_weekly_timeline(
                timeline_commits, paths_set,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("weekly timeline failed for %s/%s: %s",
                         key[0], key[1], exc)
            weekly_points = []

        # Bus factor: authors with ≥20 % of flow commits.
        threshold = max(1, total * 0.2)
        author_counts: dict[str, int] = {}
        for c in c_for_flow:
            author_counts[c.author] = author_counts.get(c.author, 0) + 1
        bus_factor = max(
            1, sum(1 for cnt in author_counts.values() if cnt >= threshold),
        )

        # Health trend: first-half vs second-half bug ratio (>0 = improving).
        health_trend: float | None = None
        if len(weekly_points) >= 4:
            mid = len(weekly_points) // 2

            def _bug_ratio(pts: list[TimelinePoint]) -> float:
                total_c = sum(p.total_commits for p in pts)
                bf = sum(p.bug_fix_commits for p in pts)
                return bf / total_c if total_c > 0 else 0.0

            health_trend = round(
                _bug_ratio(weekly_points[:mid])
                - _bug_ratio(weekly_points[mid:]),
                3,
            )

        # Hotspot files: source files with >40 % bug ratio + ≥3 commits.
        file_total: dict[str, int] = {}
        file_bugs: dict[str, int] = {}
        for c in c_for_flow:
            for f in c.files_changed:
                if f in paths_set and not _is_test_file(f):
                    file_total[f] = file_total.get(f, 0) + 1
                    if c.is_bug_fix:
                        file_bugs[f] = file_bugs.get(f, 0) + 1
        hotspot_files = sorted(
            [
                f for f, t in file_total.items()
                if t >= 3 and file_bugs.get(f, 0) / t > 0.4
            ],
            key=lambda f: -(file_bugs.get(f, 0) / file_total[f]),
        )[:5]

        flow.total_commits = total
        flow.bug_fixes = bug_fixes
        flow.bug_fix_ratio = round(bug_fix_ratio, 3)
        flow.authors = sorted(flow_authors.get(key, set()))
        flow.last_modified = flow_last_modified.get(
            key, datetime.now(tz=timezone.utc),
        )
        flow.health_score = (
            _calculate_health(bug_fix_ratio, total, c_for_flow)
            if total > 0 else 100.0
        )
        try:
            flow.bug_fix_prs = _collect_prs(c_for_flow, remote_url)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("bug_fix_prs failed for flow %s/%s: %s",
                         key[0], key[1], exc)
        flow.test_file_count = test_file_count
        flow.weekly_points = weekly_points
        flow.bus_factor = bus_factor
        flow.health_trend = health_trend
        flow.hotspot_files = hotspot_files


# ── Lcov-derived file coverage overlay ─────────────────────────────────


def _attach_lcov_coverage(
    features: list[Feature],
    file_coverage: dict[str, float],
) -> int:
    """Overlay file-level lcov coverage onto features and flows.

    The legacy ``analyze`` pipeline computes per-feature ``coverage_pct``
    as the mean of file-level coverage % across non-test source files.
    This overrides any behavioral coverage already set, because lcov
    data is authoritative (runtime ground truth) while behavioral
    coverage is heuristic.

    Suffix-matching mirrors ``build_flows_metrics`` — lcov paths are
    sometimes absolute, sometimes leading-slash-prefixed; match either
    direction.

    Returns the number of features that received a coverage score.
    """
    if not file_coverage:
        return 0

    def _lookup(path: str) -> float | None:
        # Direct hit first.
        if path in file_coverage:
            return file_coverage[path]
        # Suffix match (lcov absolute or sub-path).
        for cov_path, pct in file_coverage.items():
            if cov_path.endswith(path) or path.endswith(cov_path.lstrip("/")):
                return pct
        return None

    scored = 0
    for feat in features:
        cov_values = []
        for p in feat.paths:
            if _is_test_file(p):
                continue
            pct = _lookup(p)
            if pct is not None:
                cov_values.append(pct)
        if cov_values:
            feat.coverage_pct = round(sum(cov_values) / len(cov_values), 1)
            scored += 1

        for flow in feat.flows:
            flow_vals = []
            for p in flow.paths:
                if _is_test_file(p):
                    continue
                pct = _lookup(p)
                if pct is not None:
                    flow_vals.append(pct)
            if flow_vals:
                flow.coverage_pct = round(sum(flow_vals) / len(flow_vals), 1)

    return scored


# ── Public entry point ──────────────────────────────────────────────────


def stage_6_metrics(
    features: list[Feature],
    ctx: "ScanContext",
    *,
    coverage_path: str | None = None,
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

    # Resolve remote_url once for bug_fix_prs URL minting.
    remote_url = ""
    try:
        from faultline.analyzer.git import get_remote_url, load_repo
        repo = load_repo(str(ctx.repo_path))
        remote_url = get_remote_url(repo) or ""
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("stage_6_metrics: remote_url lookup failed: %s", exc)

    # 1) Commit-driven metrics — always run (no external deps).
    try:
        _attach_commit_metrics(
            features, ctx.commits, remote_url=remote_url, blame_index=None,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "stage_6_metrics: commit-metric pass failed: %s; features keep "
            "their Stage 5 placeholder values", exc,
        )

    # 1b) Flow-level commit metrics — hotspots / weekly / bus_factor / trend.
    try:
        _attach_flow_metrics(features, ctx.commits, remote_url=remote_url)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "stage_6_metrics: flow-metric pass failed: %s; flows keep "
            "their previous values", exc,
        )

    # 2) Behavioral coverage (heuristic) — best-effort.
    provider, warning = _load_coverage_provider(ctx.repo_path, ctx.commits)
    if provider is not None:
        try:
            scored = _attach_coverage(features, provider)
            logger.info(
                "stage_6_metrics: behavioral coverage scored on %d/%d features",
                scored, len(features),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("stage_6_metrics: coverage pass failed: %s", exc)
    else:
        logger.info("stage_6_metrics: behavioral coverage unavailable — %s", warning)

    # 3) Lcov-derived file coverage (Sprint 3) — overrides behavioral
    # when present. Skipped silently when --coverage flag not set and
    # no lcov.info auto-detected at the repo root.
    try:
        from faultline.analyzer.coverage import read_coverage
        file_coverage = read_coverage(
            str(ctx.repo_path), coverage_path=coverage_path,
        )
        if file_coverage:
            scored = _attach_lcov_coverage(features, file_coverage)
            logger.info(
                "stage_6_metrics: lcov coverage attached to %d/%d features "
                "(%d files in report)",
                scored, len(features), len(file_coverage),
            )
        elif coverage_path:
            logger.warning(
                "stage_6_metrics: --coverage=%s yielded zero file entries",
                coverage_path,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning("stage_6_metrics: lcov coverage pass failed: %s", exc)

    return features


__all__ = ["stage_6_metrics"]
