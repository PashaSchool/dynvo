"""Multi-subpath engine entry point — engine(repo, subpaths[]).

``run_pipeline_multi`` runs the per-tree pipeline once per selected
monorepo sub-project while making the expensive repo-level git pass
(tracked files + history parse) exactly ONCE, via
:func:`faultline.pipeline_v2.git_snapshot.fetch_git_snapshot`. Each
per-subpath run receives the shared snapshot and partitions it in
memory (see :mod:`faultline.pipeline_v2.git_snapshot` for the
equivalence semantics vs the legacy per-subpath git calls).

Failure semantics mirror the CLI's keep-going loop: a per-subpath
failure (including the Stage 0 fail-loud ``SubpathScopeError`` guards)
is caught, recorded on that subpath's :class:`MultiScanResult`, and the
remaining subpaths still run. Sequential execution only — parallelism
is explicitly out of scope for Phase 1.

Fallback to per-subpath git calls: when the shared snapshot cannot be
captured (``load_repo`` failure) or cannot be safely partitioned
(commit list truncated at ``max_commits`` — a scoped ``git log`` could
then reach deeper history than the snapshot saw), the snapshot is NOT
injected and every per-subpath run performs its own git calls exactly
as today. Equivalence first, speed second.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from faultline.pipeline_v2.git_snapshot import GitSnapshot, fetch_git_snapshot
from faultline.pipeline_v2.run import DEFAULT_MODEL, run_pipeline_v2

logger = logging.getLogger(__name__)


@dataclass
class MultiScanResult:
    """Outcome of one subpath's scan inside a multi-subpath run.

    Exactly one of (``result``, ``error``) is set. ``out_path`` is the
    written FeatureMap JSON path on success, ``None`` on failure.
    ``result`` is the full ``run_pipeline_v2`` return value (``path`` +
    every ``scan_meta`` key, including ``subpath`` and
    ``shared_git_pass``).
    """

    subpath: str
    out_path: Path | None
    result: dict[str, Any] | None
    error: str | None


def run_pipeline_multi(
    repo_path: Path | str,
    subpaths: list[str],
    *,
    model: str = DEFAULT_MODEL,
    days: int = 365,
    llm_reconcile: bool = False,
    run_id: str | None = None,
    max_tree_depth: int | None = None,
    since: str | None = None,
    base_scan_path: Path | str | None = None,
    lineage_jaccard_threshold: float | None = None,
    org_id: str | None = None,
    on_subpath_start: Callable[[str], None] | None = None,
    on_subpath_end: Callable[[MultiScanResult], None] | None = None,
) -> list[MultiScanResult]:
    """Scan N monorepo sub-projects with ONE shared git pass.

    Args mirror :func:`run_pipeline_v2` (minus ``out_path`` — each
    subpath writes its own timestamped FeatureMap — and ``subpath``,
    which is supplied per entry). ``on_subpath_start`` /
    ``on_subpath_end`` are optional progress hooks so callers (the CLI)
    can keep their per-scope progress lines exactly as before.

    Returns one :class:`MultiScanResult` per subpath, in input order.
    Never raises for a per-subpath failure — inspect ``.error``.
    """
    repo_path = Path(repo_path).resolve()
    if not subpaths:
        raise ValueError("run_pipeline_multi requires at least one subpath")

    # ── Shared git pass — ONCE per repository ───────────────────────
    snapshot: GitSnapshot | None
    try:
        snapshot = fetch_git_snapshot(repo_path, days=days)
    except Exception as exc:  # noqa: BLE001 — keep-going semantics
        # Snapshot capture failed (e.g. not a git repo). Don't fail the
        # whole batch here: let each per-subpath run hit the same error
        # through its own legacy git calls so it's recorded per entry.
        logger.warning(
            "run_pipeline_multi: shared git snapshot failed (%s: %s) — "
            "falling back to per-subpath git calls",
            type(exc).__name__, exc,
        )
        snapshot = None
    if snapshot is not None and snapshot.truncated:
        # Documented divergence #1 (see git_snapshot.py): a snapshot
        # truncated at max_commits cannot be partitioned equivalently.
        logger.warning(
            "run_pipeline_multi: snapshot hit max_commits=%d — "
            "falling back to per-subpath git calls (equivalence first)",
            snapshot.max_commits,
        )
        snapshot = None

    results: list[MultiScanResult] = []
    for sp in subpaths:
        if on_subpath_start is not None:
            on_subpath_start(sp)
        try:
            res = run_pipeline_v2(
                repo_path,
                model=model,
                days=days,
                out_path=None,
                llm_reconcile=llm_reconcile,
                run_id=run_id,
                max_tree_depth=max_tree_depth,
                since=since,
                base_scan_path=base_scan_path,
                lineage_jaccard_threshold=lineage_jaccard_threshold,
                org_id=org_id,
                subpath=sp,
                git_snapshot=snapshot,
            )
            entry = MultiScanResult(
                subpath=sp,
                out_path=Path(str(res["path"])),
                result=res,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 — record + continue
            # Fail-loud guards (SubpathScopeError etc.) still raise per
            # subpath; we record them and keep going — mirroring the
            # CLI's historical multi-subpath loop.
            entry = MultiScanResult(
                subpath=sp,
                out_path=None,
                result=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(entry)
        if on_subpath_end is not None:
            on_subpath_end(entry)
    return results


__all__ = [
    "MultiScanResult",
    "run_pipeline_multi",
]
