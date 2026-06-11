"""Stage 6.95 — per-entity git-history timeline (deterministic, pure).

For every PRODUCT feature (Layer 2) and every USER FLOW this stage
derives a historical timeline from the single git pass already in
memory (``ScanContext.commits``) — no new git calls, no LLM, no
cross-run persistence (cold-scan rule). The output answers the tech
lead's question "did our tests actually help THIS feature": when did
bug-fixes start, when did the first test appear, and did the bug-fix
share move after tests showed up.

Runs LAST in the finalize phase — after Stage 6.7/6.7c/6.7b (user-flow
membership is final) and after Stage 8 + 8.5 (product-feature ``paths``
aggregates are final), immediately before Stage 7 output.

Entity → file-set resolution (retroactive mapping)
==================================================
Each entity is resolved to its CURRENT file set, applied retroactively
across the whole history window — a known and accepted v1
approximation, quantified per entity by ``history_confidence``:

  - product feature → its post-rollup/backfill ``paths`` aggregate;
  - user flow       → union of its member flows' ``paths``
    (``member_flow_ids`` are flow uuid-or-name keys, matching
    Stage 6.7's ``_flow_key``).

A commit is ATTRIBUTED to an entity when it touches a member file
(exact path match, with the same parent-directory fallback Stage 6
metrics uses to catch deleted/renamed files).

Test-file source (pre/post Stage 6.9 strip)
===========================================
Stage 6.9 strips test files from ``feature.paths`` / ``flow.paths``
BEFORE this stage runs, so member paths alone cannot identify test
commits. We resolve each entity's test-file set from two
code-grounded sources instead:

  1. ``Flow.test_files`` — populated by the deterministic
     :mod:`faultline.pipeline_v2.flow_test_mapper` over HEAD and
     deliberately NOT stripped by Stage 6.9. For a user flow: union of
     member flows' ``test_files``; for a product feature: union across
     its member developer features' flows. This reuses the repo's
     single source of truth for "what test exercises source X".
  2. Directory neighbourhood over HISTORICAL commit paths — the mapper
     only sees HEAD, so test files deleted/renamed since would vanish
     from history. Any path in ``commit.files_changed`` classified by
     :func:`stage_6_9_test_strip.is_test_path` (the same predicate the
     strip uses — not reinvented) counts for an entity when its
     directory, after stripping trailing test segments
     (``__tests__`` / ``tests`` / ``e2e`` / ...), is equal to or a
     descendant of a directory containing one of the entity's member
     files. Repo-root test trees that do not mirror member directories
     are deliberately NOT attributed — cross-entity bleed is worse
     than a missed root-level suite.

A commit counts as a ``test_commit`` for an entity when it is
attributed to the entity's combined (source ∪ test) file set and
touches at least one of the entity's test files.

Scale-invariance of every gate (project hard rule)
==================================================
  - ``test_wave``: weeks whose ``test_commits`` strictly exceed the
    entity's OWN 75th percentile of nonzero weekly test activity — a
    percentile of the entity's own distribution, no absolute count.
  - ``hotspot_emerged``: reuses Stage 6's universal hotspot semantics
    verbatim (``HOTSPOT_BUG_RATIO_MIN`` ratio + ``HOTSPOT_COMMITS_MIN``
    minimum sample), evaluated cumulatively week by week.
  - test-efficacy ACTIVITY GATE: entities below the MEDIAN total
    attributed-commit count among scored entities of the same kind →
    ``insufficient_data``. The median adapts to each repo's own
    activity distribution.
  - ``improved`` / ``worsened``: the share delta must exceed the pooled
    two-proportion standard error — a sample-size-aware band, not a
    tuned threshold.
  - ``health_lite`` trailing window: 13 ISO weeks = one calendar
    quarter (calendar-derived, not corpus-tuned).

``health_lite`` vs ``health_score``
===================================
``health_lite`` reuses the git-derivable core of
:func:`faultline.analyzer.features._calculate_health` — the logistic
over bug-fix share centred at 0.55 with steepness 8, damped by the
same ``min(1, commits/50)`` activity factor — but computes it over the
TRAILING quarter ending at each emitted week and omits the full
formula's scan-time age decay (recency relative to "now" is
meaningless for a historical point; the trailing window itself is the
recency element). Never compare the two directly.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from datetime import date
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable

from faultline.models.types import (
    Commit,
    EntityHistory,
    Feature,
    Flow,
    HistoryEvent,
    HistoryPoint,
    TestEfficacy,
    UserFlow,
)
from faultline.pipeline_v2.stage_6_9_test_strip import (
    _TEST_DIR_SEGMENTS,
    is_test_path,
)
from faultline.pipeline_v2.stage_6_metrics import (
    HOTSPOT_BUG_RATIO_MIN,
    HOTSPOT_COMMITS_MIN,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

__all__ = [
    "stage_6_95_history",
    "compute_entity_history",
    "HEALTH_LITE_TRAILING_WEEKS",
    "TEST_WAVE_PERCENTILE",
    "BIRTH_CONFIDENCE_SPAN_SHARE",
]

# One calendar quarter, expressed in ISO weeks. Calendar-derived (not
# tuned on any corpus repo): "how is this entity trending this quarter"
# is the natural product question for the trailing health composite.
HEALTH_LITE_TRAILING_WEEKS: int = 13

# Percentile of the entity's OWN nonzero weekly test-commit
# distribution a week must STRICTLY exceed to start a test wave.
# Quartile semantics ("upper-quartile burst") — scale-invariant because
# it is computed per entity from that entity's own series.
TEST_WAVE_PERCENTILE: float = 0.75

# ``history_confidence`` cutoff: share of the active span (birth → last
# active week) considered "early timeline". 25% = first quarter of the
# entity's own life — a ratio of its own span, not an absolute window.
BIRTH_CONFIDENCE_SPAN_SHARE: float = 0.25


# ── ISO-week helpers ─────────────────────────────────────────────────────


def _week_label(d: date) -> str:
    """ISO week label ``YYYY-Www`` — same convention as TimelinePoint."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_index(label: str) -> int:
    """Monotonic integer index of an ISO week label (for distance math)."""
    year_s, week_s = label.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1).toordinal() // 7


# ── Path helpers ─────────────────────────────────────────────────────────


def _dirname(path: str) -> str:
    parent = str(PurePosixPath(path.replace("\\", "/")).parent)
    return "" if parent == "." else parent


def _strip_test_dir_segments(d: str) -> str:
    """Strip TRAILING test directory segments (``src/billing/__tests__``
    → ``src/billing``) so sibling test trees resolve to the product dir
    they mirror."""
    parts = [p for p in d.split("/") if p]
    while parts and parts[-1].lower() in _TEST_DIR_SEGMENTS:
        parts.pop()
    return "/".join(parts)


def _ancestor_chain(d: str) -> Iterable[str]:
    """Yield ``d`` and every ancestor directory, EXCLUDING the repo
    root ``""`` (a root match would attach every test file to every
    entity that owns a root-level file)."""
    while d:
        yield d
        cut = d.rfind("/")
        d = d[:cut] if cut != -1 else ""


# ── Percentile (deterministic, linear interpolation) ────────────────────


def _percentile(values: list[int], q: float) -> float:
    """Linear-interpolation percentile of a nonempty list."""
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(xs[lo])
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


# ── health_lite ──────────────────────────────────────────────────────────


def _health_lite(trailing_commits: int, trailing_bugs: int) -> float:
    """Git-only trailing composite — see module docstring for how this
    differs from the full ``health_score``."""
    if trailing_commits <= 0:
        return 100.0
    share = trailing_bugs / trailing_commits
    base = 100.0 / (1.0 + math.exp(8.0 * (share - 0.55)))
    activity = min(1.0, trailing_commits / 50.0)
    return round(base * activity + base * (1.0 - activity) * 0.8, 1)


# ── Entity resolution ────────────────────────────────────────────────────


def _flow_lookup(flows: list[Flow]) -> dict[str, Flow]:
    """uuid-or-name → Flow, matching Stage 6.7's ``_flow_key``."""
    by_key: dict[str, Flow] = {}
    for fl in flows:
        if fl.uuid:
            by_key.setdefault(fl.uuid, fl)
        by_key.setdefault(fl.name, fl)
    return by_key


def _entity_specs(
    product_features: list[Feature],
    user_flows: list[UserFlow],
    flows: list[Flow],
    developer_features: list[Feature],
) -> list[tuple[str, Any, list[str], list[str]]]:
    """Resolve each entity to ``(kind, obj, source_paths, test_files)``.

    Order is deterministic: product features first (input order), then
    user flows (input order).
    """
    flow_by_key = _flow_lookup(flows)
    specs: list[tuple[str, Any, list[str], list[str]]] = []

    # Product features: paths aggregate + member dev features' flow tests.
    dev_by_pf: dict[str, list[Feature]] = defaultdict(list)
    for df in developer_features:
        if df.product_feature_id:
            dev_by_pf[df.product_feature_id].append(df)
    for pf in product_features:
        tests: dict[str, None] = {}
        for df in dev_by_pf.get(pf.name, []):
            for fl in df.flows:
                for t in fl.test_files:
                    tests[t] = None
        specs.append(("product_feature", pf, list(pf.paths), list(tests)))

    # User flows: member flows' paths + test_files unions.
    for uf in user_flows:
        paths: dict[str, None] = {}
        tests = {}
        for mid in uf.member_flow_ids:
            member = flow_by_key.get(mid)
            if member is None:
                continue
            for p in member.paths:
                paths[p] = None
            for t in member.test_files:
                tests[t] = None
        specs.append(("user_flow", uf, list(paths), list(tests)))
    return specs


# ── Per-entity computation ───────────────────────────────────────────────


class _EntityAccumulator:
    """Buckets one entity's attributed commits into ISO weeks."""

    def __init__(self, idx: int, kind: str, obj: Any,
                 source_paths: list[str], test_files: list[str]) -> None:
        self.idx = idx
        self.kind = kind
        self.obj = obj
        # Member paths that are themselves test files count toward the
        # test set (Stage 8 aggregates may retain them pre-strip).
        self.source_set = frozenset(source_paths)
        self.test_set = set(test_files) | {
            p for p in source_paths if is_test_path(p)
        }
        self.member_dirs = frozenset(
            d for d in (_dirname(p) for p in source_paths) if d
        )
        # week label → mutable bucket
        self.weeks: dict[str, dict[str, Any]] = {}
        self.total_commits = 0
        # path → first-touch week index (for history_confidence +
        # hotspot emergence we also keep per-file cumulative counters).
        self.first_touch: dict[str, int] = {}

    def is_local_test(self, path: str) -> bool:
        """Historical test path attribution — see module docstring."""
        if path in self.test_set:
            return True
        if not is_test_path(path):
            return False
        stripped = _strip_test_dir_segments(_dirname(path))
        return any(d in self.member_dirs for d in _ancestor_chain(stripped))

    def add_commit(
        self,
        commit: Commit,
        week: str,
        touched_sources: list[str],
        touched_tests: list[str],
    ) -> None:
        bucket = self.weeks.get(week)
        if bucket is None:
            bucket = {
                "commits": 0,
                "bug_fixes": 0,
                "test_commits": 0,
                "files": set(),
                # per-file (total, bugs) increments this week, for the
                # cumulative hotspot walk
                "file_hits": [],
            }
            self.weeks[week] = bucket
        bucket["commits"] += 1
        if commit.is_bug_fix:
            bucket["bug_fixes"] += 1
        if touched_tests:
            bucket["test_commits"] += 1
        wi = _week_index(week)
        for p in touched_sources:
            bucket["files"].add(p)
            bucket["file_hits"].append((p, bool(commit.is_bug_fix)))
            if p not in self.first_touch or wi < self.first_touch[p]:
                self.first_touch[p] = wi
        for p in touched_tests:
            bucket["files"].add(p)
        self.total_commits += 1


def _build_history(acc: _EntityAccumulator, *, gated: bool) -> EntityHistory | None:
    """Assemble the EntityHistory from one accumulator. ``gated`` is the
    activity-gate verdict computed across entities of the same kind."""
    if not acc.weeks:
        return None

    ordered = sorted(acc.weeks.items(), key=lambda kv: _week_index(kv[0]))
    week_indices = [_week_index(w) for w, _ in ordered]
    birth_week = ordered[0][0]

    # ── Weekly points (sparse) + trailing health_lite ────────────────
    points: list[HistoryPoint] = []
    for i, (week, b) in enumerate(ordered):
        wi = week_indices[i]
        lo = wi - (HEALTH_LITE_TRAILING_WEEKS - 1)
        t_commits = t_bugs = 0
        j = i
        while j >= 0 and week_indices[j] >= lo:
            t_commits += ordered[j][1]["commits"]
            t_bugs += ordered[j][1]["bug_fixes"]
            j -= 1
        points.append(
            HistoryPoint(
                week=week,
                commits=b["commits"],
                bug_fixes=b["bug_fixes"],
                bugfix_share=round(b["bug_fixes"] / b["commits"], 3),
                test_commits=b["test_commits"],
                files_touched=len(b["files"]),
                health_lite=_health_lite(t_commits, t_bugs),
            ),
        )

    # ── Events ───────────────────────────────────────────────────────
    events: list[HistoryEvent] = [
        HistoryEvent(kind="birth", week=birth_week),
    ]

    first_test_week: str | None = None
    for pt in points:
        if pt.test_commits > 0:
            first_test_week = pt.week
            break
    if first_test_week is not None:
        events.append(HistoryEvent(kind="first_test", week=first_test_week))

    # test_wave — weeks strictly above the entity's own P75 of nonzero
    # weekly test activity; contiguous wave weeks collapse to one event
    # at the run's start week.
    nonzero_tests = [pt.test_commits for pt in points if pt.test_commits > 0]
    if nonzero_tests:
        threshold = _percentile(nonzero_tests, TEST_WAVE_PERCENTILE)
        prev_was_wave = False
        prev_wi: int | None = None
        for i, pt in enumerate(points):
            is_wave = pt.test_commits > threshold
            contiguous = (
                prev_was_wave
                and prev_wi is not None
                and week_indices[i] == prev_wi + 1
            )
            if is_wave and not contiguous:
                events.append(
                    HistoryEvent(
                        kind="test_wave",
                        week=pt.week,
                        detail=f"test_commits={pt.test_commits}",
                    ),
                )
            prev_was_wave = is_wave
            prev_wi = week_indices[i]

    # hotspot_emerged — cumulative per-file walk, same thresholds as the
    # existing Stage 6 hotspot logic.
    file_totals: dict[str, int] = defaultdict(int)
    file_bugs: dict[str, int] = defaultdict(int)
    hotspot_done = False
    for week, b in ordered:
        if hotspot_done:
            break
        emerged: list[str] = []
        for path, is_bug in b["file_hits"]:
            file_totals[path] += 1
            if is_bug:
                file_bugs[path] += 1
        for path, is_bug in b["file_hits"]:
            total = file_totals[path]
            if total >= HOTSPOT_COMMITS_MIN and (
                file_bugs[path] / total >= HOTSPOT_BUG_RATIO_MIN
            ):
                emerged.append(path)
        if emerged:
            events.append(
                HistoryEvent(
                    kind="hotspot_emerged",
                    week=week,
                    detail=sorted(emerged)[0],
                ),
            )
            hotspot_done = True

    events.sort(key=lambda e: (_week_index(e.week), e.kind))

    # ── test_efficacy ────────────────────────────────────────────────
    efficacy = _test_efficacy(points, first_test_week, gated=gated)

    # ── history_confidence ───────────────────────────────────────────
    confidence = _history_confidence(acc, week_indices)

    return EntityHistory(
        birth_week=birth_week,
        weekly=points,
        events=events,
        test_efficacy=efficacy,
        history_confidence=confidence,
    )


def _test_efficacy(
    points: list[HistoryPoint],
    pivot_week: str | None,
    *,
    gated: bool,
) -> TestEfficacy:
    """Before/after bug-fix-share comparison around ``first_test``.

    The pivot week belongs to the AFTER window (it carries the first
    test commit). Windows are activity-weighted by construction: shares
    are computed over commit counts, not over calendar weeks.
    """
    if gated:
        return TestEfficacy(
            verdict="insufficient_data",
            reason="below median activity among scored entities of this kind",
        )
    if pivot_week is None:
        return TestEfficacy(
            verdict="insufficient_data",
            reason="no test commit observed in the scan window",
        )
    pivot_wi = _week_index(pivot_week)
    before_n = before_bugs = after_n = after_bugs = 0
    for pt in points:
        if _week_index(pt.week) < pivot_wi:
            before_n += pt.commits
            before_bugs += pt.bug_fixes
        else:
            after_n += pt.commits
            after_bugs += pt.bug_fixes
    if before_n == 0 or after_n == 0:
        return TestEfficacy(
            verdict="insufficient_data",
            pivot_week=pivot_week,
            commits_before=before_n,
            commits_after=after_n,
            reason="empty window on one side of first_test",
        )
    share_before = before_bugs / before_n
    share_after = after_bugs / after_n
    # Pooled two-proportion standard error — sample-size-aware band.
    pooled = (before_bugs + after_bugs) / (before_n + after_n)
    se = math.sqrt(
        max(pooled * (1.0 - pooled), 1e-12) * (1.0 / before_n + 1.0 / after_n),
    )
    delta = share_before - share_after
    if delta > se:
        verdict = "improved"
    elif -delta > se:
        verdict = "worsened"
    else:
        verdict = "no_change"
    return TestEfficacy(
        verdict=verdict,  # type: ignore[arg-type]
        bugfix_share_before=round(share_before, 3),
        bugfix_share_after=round(share_after, 3),
        commits_before=before_n,
        commits_after=after_n,
        pivot_week=pivot_week,
    )


def _history_confidence(
    acc: _EntityAccumulator,
    week_indices: list[int],
) -> float:
    """Share of current member files that existed by birth + 25% of the
    active span. Files in HEAD never touched inside the scan window
    predate it (hence predate the cutoff) and count as existing."""
    if not acc.source_set:
        return 0.0
    birth_wi = week_indices[0]
    span = week_indices[-1] - birth_wi
    cutoff = birth_wi + math.ceil(span * BIRTH_CONFIDENCE_SPAN_SHARE)
    existing = sum(
        1
        for p in acc.source_set
        if acc.first_touch.get(p, birth_wi) <= cutoff
    )
    return round(existing / len(acc.source_set), 3)


# ── Public entry points ──────────────────────────────────────────────────


def compute_entity_history(
    source_paths: list[str],
    test_files: list[str],
    commits: list[Commit],
    *,
    gated: bool = False,
) -> EntityHistory | None:
    """Compute one entity's history in isolation (test/debug helper).

    The production path is :func:`stage_6_95_history`, which shares the
    commit sweep across all entities and computes the activity gate
    from the cross-entity median.
    """
    acc = _EntityAccumulator(0, "adhoc", None, source_paths, test_files)
    _sweep_commits([acc], commits)
    return _build_history(acc, gated=gated)


def _sweep_commits(accs: list[_EntityAccumulator], commits: list[Commit]) -> None:
    """One pass over the commit list, attributing each commit to every
    matching entity. Mirrors Stage 6's exact-path + parent-dir-fallback
    attribution, extended with the test-file neighbourhood rule."""
    # Global indexes: file → entity idxs / dir → entity idxs.
    by_file: dict[str, list[int]] = defaultdict(list)
    by_dir: dict[str, list[int]] = defaultdict(list)
    by_test_file: dict[str, list[int]] = defaultdict(list)
    for acc in accs:
        for p in acc.source_set:
            by_file[p].append(acc.idx)
            d = _dirname(p)
            if d:
                by_dir[d].append(acc.idx)
        for t in acc.test_set:
            by_test_file[t].append(acc.idx)
    acc_by_idx = {a.idx: a for a in accs}

    for commit in commits:
        week = _week_label(commit.date.date())
        touched_src: dict[int, list[str]] = defaultdict(list)
        touched_tst: dict[int, list[str]] = defaultdict(list)
        for fp in commit.files_changed:
            hit_idxs = by_file.get(fp)
            if hit_idxs:
                for i in hit_idxs:
                    touched_src[i].append(fp)
            else:
                # Deleted/renamed file: parent-dir fallback (Stage 6
                # metrics parity).
                for i in by_dir.get(_dirname(fp), []):
                    touched_src[i].append(fp)
            # Test attribution — explicit test set, else neighbourhood.
            if fp in by_test_file:
                for i in by_test_file[fp]:
                    touched_tst[i].append(fp)
            elif is_test_path(fp):
                stripped = _strip_test_dir_segments(_dirname(fp))
                seen: set[int] = set()
                for d in _ancestor_chain(stripped):
                    for i in by_dir.get(d, []):
                        seen.add(i)
                for i in sorted(seen):
                    touched_tst[i].append(fp)
        for i in sorted(set(touched_src) | set(touched_tst)):
            acc_by_idx[i].add_commit(
                commit, week, touched_src.get(i, []), touched_tst.get(i, []),
            )


def stage_6_95_history(
    product_features: list[Feature],
    user_flows: list[UserFlow],
    flows: list[Flow],
    developer_features: list[Feature],
    commits: list[Commit],
) -> dict[str, Any]:
    """Attach ``history`` to every product feature + user flow.

    Mutates the entity objects in place; returns the stage telemetry
    dict for ``scan_meta`` / the stage artifact. Deterministic: same
    inputs → identical output (stable iteration order everywhere; the
    only inputs are the in-memory commit list and the final entities).
    """
    t0 = time.monotonic()
    specs = _entity_specs(
        product_features, user_flows, flows, developer_features,
    )
    accs = [
        _EntityAccumulator(i, kind, obj, paths, tests)
        for i, (kind, obj, paths, tests) in enumerate(specs)
    ]
    if accs and commits:
        _sweep_commits(accs, commits)

    # Activity gate per kind: median total attributed commits among
    # SCORED entities (>=1 attributed commit) of the same kind.
    medians: dict[str, float] = {}
    for kind in ("product_feature", "user_flow"):
        totals = sorted(
            a.total_commits for a in accs
            if a.kind == kind and a.total_commits > 0
        )
        if totals:
            n = len(totals)
            mid = n // 2
            medians[kind] = (
                float(totals[mid]) if n % 2
                else (totals[mid - 1] + totals[mid]) / 2.0
            )

    telemetry: dict[str, Any] = {
        "product_features_total": len(product_features),
        "user_flows_total": len(user_flows),
        "product_features_scored": 0,
        "user_flows_scored": 0,
        "product_features_gated": 0,
        "user_flows_gated": 0,
        "verdicts": {
            "improved": 0, "worsened": 0,
            "no_change": 0, "insufficient_data": 0,
        },
    }
    for acc in accs:
        gated = (
            acc.total_commits > 0
            and acc.total_commits < medians.get(acc.kind, 0.0)
        )
        history = _build_history(acc, gated=gated)
        acc.obj.history = history
        short = "product_features" if acc.kind == "product_feature" else "user_flows"
        if history is not None:
            telemetry[f"{short}_scored"] += 1
            telemetry["verdicts"][history.test_efficacy.verdict] += 1
            if gated:
                telemetry[f"{short}_gated"] += 1
    telemetry["elapsed_sec"] = round(time.monotonic() - t0, 3)
    return telemetry
