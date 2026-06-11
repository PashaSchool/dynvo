"""Shared single git pass for multi-subpath scans.

The git history parse (``get_commits``) dominates scan wall-time on
large repos. When the engine scans N monorepo sub-projects in one
invocation, the legacy path repeats that parse N times (once per
``--subpath``). This module captures the repo's git state ONCE
(:func:`fetch_git_snapshot`) and partitions it per subpath purely
in-memory (:func:`partition_snapshot`), reproducing exactly what the
per-subpath ``get_tracked_files(repo, src=subpath)`` +
``get_commits(repo, days, src=subpath)`` calls return today.

Equivalence semantics (vs ``faultline/analyzer/git.py``)
========================================================

``get_tracked_files``
---------------------

The scoped call traverses the SAME HEAD tree as the unscoped call and
filters with ``Path(item.path).is_relative_to(src)`` — a pure segment-
prefix test. Filtering the unscoped snapshot with
``scope_files_to_subpath`` (segment-prefix match + relativize) is
therefore exactly equivalent: skip_dirs / skip_extensions /
skip_filenames are applied to the full repo-relative path in both
cases, and sibling dirs (``apps/web`` vs ``apps/web-extra``) cannot
cross-match. Note Stage 0 relativizes the scoped result anyway, so the
partitioned view returns subpath-relative paths directly.

``get_commits`` (fast path, ``git log --name-only -z``)
-------------------------------------------------------

The scoped call adds ``-- <subpath>`` (pathspec) and then filters +
relativizes each surviving commit's ``--name-only`` list in memory,
dropping commits whose filtered list is empty. Partitioning the
UNSCOPED log the same way (filter ``files_changed`` by prefix,
relativize, drop empty) reproduces it because:

  - ``--name-only`` prints the same per-commit file list with or
    without the pathspec (the pathspec only SELECTS commits), and the
    legacy code already applies the identical in-memory filter on top.
  - Merge commits print NO file list under ``git log --name-only``
    (no ``-m``/``--first-parent``), so they carry
    ``files_changed == []`` in the unscoped snapshot and are dropped
    by the empty-after-filter rule — matching the scoped log, where
    they are either simplified away or also empty-filtered.
  - Rename-only commits selected by the pathspec but with no
    in-subtree ``--name-only`` entry are empty-filtered by BOTH paths
    (see the existing comment in ``_get_commits_fast``).
  - Ordering: both are the same newest-first traversal; partitioning
    preserves snapshot order.
  - ``Commit`` fields other than ``files_changed`` (sha8 / message /
    author / date / is_bug_fix / pr_number) are derived from the
    commit header only and are unaffected by scoping.

KNOWN DIVERGENCES (documented, not silently papered over)
---------------------------------------------------------

1. ``--max-count`` truncation. The scoped ``git log -- <subpath>``
   applies ``--max-count`` to the SELECTED commit stream, so on a repo
   with more than ``max_commits`` commits in the window it can reach
   deeper history than the unscoped log the snapshot was built from.
   This IS detectable: when ``len(snapshot.commits) == max_commits``
   the snapshot is flagged ``truncated=True`` and
   :func:`partition_snapshot` refuses to partition
   (:class:`SnapshotNotPartitionable`). Callers (``run_pipeline_multi``)
   fall back to legacy per-subpath git calls FOR THAT REPO — slower,
   but exactly equivalent.

2. Git history simplification. ``git log -- <subpath>`` (no
   ``--full-history``) prunes commits whose subtree change was later
   reverted / duplicated, and follows only one parent of TREESAME
   merges. The in-memory partition keeps every commit whose
   ``--name-only`` list touches the subtree — a superset in those
   pathological topologies (revert pairs, evil merges, cherry-pick
   dupes). Replicating TREESAME pruning in memory would require
   per-commit subtree OIDs, which ``--name-only`` does not carry, and
   it is NOT detectable from the snapshot alone. For linear and
   normal feature-branch histories the outputs are identical (verified
   by the fixture equivalence test); the legacy GitPython FALLBACK
   path (``repo.iter_commits`` + in-memory filter, used when the fast
   subprocess fails) has exactly the partition's semantics already —
   i.e. the engine has long accepted this membership rule whenever the
   fast path is unavailable.

3. Non-monotonic commit dates. Both the scoped and unscoped fast paths
   ``break`` at the first commit older than the cutoff; an out-of-order
   young commit appearing after an old one in the scoped-selected
   stream could differ. ``--since`` already filters at the git level so
   this only matters at the cutoff boundary on clock-skewed histories.
   Not detectable in memory; same acceptance rationale as (2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from faultline.analyzer.git import (
    DEFAULT_MAX_COMMITS,
    get_commits,
    get_tracked_files,
    load_repo,
    normalize_subpath,
    scope_files_to_subpath,
)
from faultline.models.types import Commit


class SnapshotNotPartitionable(RuntimeError):
    """The snapshot cannot be safely partitioned in memory.

    Raised when the snapshot's commit list hit the ``max_commits``
    ceiling — a scoped ``git log -- <subpath>`` could then select
    commits the snapshot never saw. Callers must fall back to the
    legacy per-subpath git calls (equivalence first, speed second).
    """


@dataclass(frozen=True)
class GitSnapshot:
    """One whole-repo git pass, captured once and partitioned N times.

    ``tracked_files`` are repo-root-relative (HEAD tree, noise-filtered
    exactly like ``get_tracked_files``); ``commits`` carry repo-root-
    relative ``files_changed``. ``truncated`` is True when the commit
    list hit ``max_commits`` — see :class:`SnapshotNotPartitionable`.
    """

    repo_path: Path
    days: int
    max_commits: int
    tracked_files: tuple[str, ...]
    commits: tuple[Commit, ...]
    truncated: bool


@dataclass(frozen=True)
class PartitionedView:
    """A subpath's slice of a :class:`GitSnapshot`.

    ``tracked_files`` and every commit's ``files_changed`` are
    SUBPATH-RELATIVE — i.e. exactly the shape Stage 0 hands downstream
    after its own ``scope_files_to_subpath`` pass. Commits that touch
    nothing under the subpath are dropped (order otherwise preserved).
    """

    subpath: str  # normalized, no trailing slash
    tracked_files: tuple[str, ...]
    commits: tuple[Commit, ...]


def fetch_git_snapshot(
    repo_path: str | Path,
    *,
    days: int = 365,
    max_commits: int = DEFAULT_MAX_COMMITS,
) -> GitSnapshot:
    """Run the repo-level git pass ONCE: tracked files + commit history.

    One ``load_repo`` + one ``get_tracked_files(src=None)`` + one
    ``get_commits(src=None)`` — the exact calls Stage 0 makes for a
    whole-repo scan today (do not duplicate git logic here).
    """
    resolved = Path(repo_path).resolve()
    repo = load_repo(str(resolved))
    tracked = get_tracked_files(repo, src=None)
    commits = get_commits(repo, days=days, max_commits=max_commits, src=None)
    return GitSnapshot(
        repo_path=resolved,
        days=days,
        max_commits=max_commits,
        tracked_files=tuple(tracked),
        commits=tuple(commits),
        truncated=len(commits) >= max_commits,
    )


def partition_snapshot(
    snapshot: GitSnapshot,
    subpath: str,
) -> PartitionedView:
    """Slice ``snapshot`` to ``subpath`` purely in memory.

    Reproduces ``scope_files_to_subpath(get_tracked_files(repo, src=sp), sp)``
    and ``get_commits(repo, days, src=sp)`` — see the module docstring
    for the field-by-field equivalence argument and the documented
    divergences. Raises :class:`SnapshotNotPartitionable` when the
    snapshot was truncated at ``max_commits`` (fall back to per-subpath
    git calls in that case).
    """
    norm = normalize_subpath(subpath)
    if norm is None:
        raise ValueError(
            f"partition_snapshot requires a non-empty subpath, got {subpath!r}"
        )
    if snapshot.truncated:
        raise SnapshotNotPartitionable(
            f"snapshot of {snapshot.repo_path} hit max_commits="
            f"{snapshot.max_commits}; a scoped git log could reach deeper "
            "history than the snapshot captured — use per-subpath git calls"
        )

    tracked = scope_files_to_subpath(list(snapshot.tracked_files), norm)

    commits: list[Commit] = []
    for commit in snapshot.commits:
        scoped_files = scope_files_to_subpath(commit.files_changed, norm)
        if not scoped_files:
            # Commit touched nothing under the subpath (or is a merge /
            # rename-only record with an empty in-subtree list) — drop
            # it, exactly like the legacy scoped paths do.
            continue
        commits.append(
            commit.model_copy(update={"files_changed": scoped_files}),
        )

    return PartitionedView(
        subpath=norm,
        tracked_files=tuple(tracked),
        commits=tuple(commits),
    )


__all__ = [
    "GitSnapshot",
    "PartitionedView",
    "SnapshotNotPartitionable",
    "fetch_git_snapshot",
    "partition_snapshot",
]
