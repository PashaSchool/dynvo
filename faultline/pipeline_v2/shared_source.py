"""Shared per-scan repo snapshot state (perf wave 2, R4).

The audit found the "every stage re-reads the repo" tax: at least five
independent ``_SourceCache(repo_path)`` instances (Stages 2.6, 6.3,
8.6.7 technology-instruments, 8.8, 6.86), plus ``extract_signatures``
re-parsing the whole tracked-file list for BOTH ``build_reach_context``
call sites (Stage 3 flow-reach enrichment and the Stage 3.5 expander) —
22.4 s + 14.6 s profiled on lobe-chat. All of these are pure derivations
of the SAME immutable inputs (the checked-out tree + the tracked-file
list), so one lazily-populated, shared, read-only holder dedupes the
reads without any output-visible effect: content is identical by
construction because the same pure functions run over the same bytes.

Design rules
============

* **Ctx-plumbing only.** ``run_pipeline_v2`` creates ONE instance per
  scan run and stashes it on ``ScanContext.shared_source``. Every
  adopting stage does ``getattr(ctx, "shared_source", None)`` and keeps
  its local-construction fallback, so stages stay independently
  testable and replay-able (the replay serializer EXCLUDES this field —
  a replayed stage sees ``None`` and builds locally, same bytes).
* **Guarded serving.** Accessors verify the caller's ``repo_path`` (and
  for the reach context also the ``tracked_files`` content) matches the
  scan-root snapshot taken at construction; a mismatch (per-workspace
  scoped contexts, snapshot worktrees, tests with synthetic roots)
  returns ``None`` → the caller falls back to local construction.
* **Deepcopy-transparent.** The orchestrator wraps stage hand-offs in
  ``_isolate`` (``copy.deepcopy``); this object intentionally survives
  that boundary AS ITSELF (``__deepcopy__``/``__copy__`` return
  ``self``): it is a read-only derived cache, not stage state — copying
  it would both break the sharing (the whole point) and choke on the
  internal ``threading.Lock``. Nothing here is ever mutated after
  population, so the isolation contract stays intact in spirit: no
  stage can corrupt another stage's INPUT through it.
* **Thread-safe lazy init.** Stages 6.3's worker threads already share
  one ``_SourceCache`` today; the lock here only serializes FIRST
  construction. ``_SourceCache.hits`` keeps its documented raciness —
  the only output surface is ``stage_6_3_cache_hits``, which is already
  normalized-volatile for the snapshot gate.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.pipeline_v2.flow_reach import ReachContext
    from faultline.pipeline_v2.stage_0_intake import ScanContext
    from faultline.pipeline_v2.stage_6_3_import_tree import _SourceCache

logger = logging.getLogger(__name__)

__all__ = ["SharedSourceState", "shared_source_cache", "shared_reach_context"]


class SharedSourceState:
    """Lazily-populated, read-only shared caches for one scan run."""

    def __init__(self, ctx: "ScanContext") -> None:
        self._repo_path = Path(ctx.repo_path)
        # Content snapshot for the reach-context guard: the reach context
        # is a pure function of (repo_path, tracked_files, tree); serve
        # the shared one ONLY to callers whose inputs are equal.
        self._tracked_snapshot: list[str] = list(ctx.tracked_files)
        self._lock = threading.Lock()
        self._source_cache: "_SourceCache | None" = None
        self._reach_context: "ReachContext | None" = None

    # ── deepcopy transparency (see module docstring) ─────────────────
    def __deepcopy__(self, memo: dict) -> "SharedSourceState":
        return self

    def __copy__(self) -> "SharedSourceState":
        return self

    # ── accessors ────────────────────────────────────────────────────
    def source_cache(self, repo_path: Path | str) -> "_SourceCache | None":
        """The shared ``_SourceCache`` — or ``None`` when ``repo_path``
        is not this scan's root (caller falls back to a local one)."""
        try:
            if Path(repo_path) != self._repo_path:
                return None
        except (TypeError, ValueError):
            return None
        with self._lock:
            if self._source_cache is None:
                from faultline.pipeline_v2.stage_6_3_import_tree import (
                    _SourceCache,
                )
                self._source_cache = _SourceCache(self._repo_path)
            return self._source_cache

    def reach_context(self, ctx: "ScanContext") -> "ReachContext | None":
        """The shared ``ReachContext`` — or ``None`` when ``ctx`` does
        not match the scan-root snapshot (scoped/per-workspace ctx)."""
        try:
            if Path(ctx.repo_path) != self._repo_path:
                return None
            if list(ctx.tracked_files) != self._tracked_snapshot:
                return None
        except (TypeError, ValueError):
            return None
        with self._lock:
            if self._reach_context is None:
                from faultline.pipeline_v2.flow_reach import (
                    build_reach_context,
                )
                self._reach_context = build_reach_context(ctx)
            return self._reach_context


def shared_source_cache(ctx: Any, repo_path: Path | str) -> "_SourceCache | None":
    """One-liner adoption helper: the ctx-shared ``_SourceCache`` for
    ``repo_path``, or ``None`` (→ caller constructs locally)."""
    state = getattr(ctx, "shared_source", None)
    if state is None:
        return None
    try:
        return state.source_cache(repo_path)
    except Exception:  # noqa: BLE001 — sharing is best-effort, never fatal
        logger.debug("shared_source: source_cache adoption failed", exc_info=True)
        return None


def shared_reach_context(ctx: Any) -> "ReachContext | None":
    """One-liner adoption helper: the ctx-shared ``ReachContext`` for
    this exact ctx content, or ``None`` (→ caller builds locally)."""
    state = getattr(ctx, "shared_source", None)
    if state is None:
        return None
    try:
        return state.reach_context(ctx)
    except Exception:  # noqa: BLE001 — sharing is best-effort, never fatal
        logger.debug("shared_source: reach_context adoption failed", exc_info=True)
        return None
