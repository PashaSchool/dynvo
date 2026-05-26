"""Stage 6.9 — test-file output-tree strip (deterministic, no LLM).

This stage is *post-everything tree hygiene*. Despite the ``6_9`` label
it is wired to run LAST in :mod:`faultline.pipeline_v2.run` — after every
Stage 6.x metric pass, after the Stage 8 analyst + Stage 8.5 member
backfill, and after Stage 3.5 flow expansion (which populates
``loc_nodes`` / ``loc_edges``) — immediately before ``stage_7_output``.

What it does
------------
Removes test-file entries from the OUTPUT TREE so the landing app /
MCP / dashboards never surface ``*.test.ts`` or ``__tests__/**`` files
as if they were product code. It is a *display / tree-cleanup* pass.

What it NEVER does
------------------
It NEVER recomputes or mutates any metric scalar — ``coverage_pct``,
``health_score``, ``bug_fix_ratio``, impact, churn, author counts,
etc. Those are computed UPSTREAM in Stage 6 deliberately WITH the test
files present (behavioral coverage *needs* the test files to know what
is covered). Stripping them here would silently corrupt coverage. The
KEY invariant — asserted by the unit tests and the cal.com replay gate
— is that the ``coverage_pct`` distribution is byte-identical before
and after this stage runs.

Edge-case rules (validated by the experimenter on the cached cal.com
scan)
------------------------------------------------------------------
* Features that become **path-empty** after stripping are dropped
  (recall-neutral — the cal.com phantoms ``tests`` / ``e2e`` /
  ``mocks`` / ``checks-csp-login-spec`` are 100 %% test-backed and map
  to no truth entry).
* Flows that become **file/attribution-empty** are dropped.
* A flow whose ``entry_point_file`` is a test path has its entry
  recomputed to the first surviving non-test participant — preferring
  the earliest ``loc_nodes`` entry with ``role == "entry"`` that
  survives, else the top remaining file. If no non-test file survives
  the flow is empty and is dropped.

Disable via ``FAULTLINE_STAGE_6_9_TEST_STRIP=0`` (default ON).

The pure entry point is :func:`strip_test_paths`. It mutates the
in-memory pydantic objects in place and returns a telemetry dict.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

__all__ = [
    "is_test_path",
    "strip_test_paths",
    "stage_6_9_enabled",
    "STAGE_6_9_ENV_FLAG",
]

STAGE_6_9_ENV_FLAG = "FAULTLINE_STAGE_6_9_TEST_STRIP"

# ── APPROVED predicate (implemented exactly as the experimenter validated) ──
_BASENAME_SUFFIXES = (".test.", ".spec.", ".e2e.", ".cy.", "_test.", "_spec.")
_SEGMENTS = {
    "__tests__",
    "__mocks__",
    "tests",
    "test",
    "e2e",
    "cypress",
    "playwright",
    "__fixtures__",
}


def is_test_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a test/mock/fixture file or lives
    under a test directory segment. Frozen predicate — do not retune."""
    if not path or not isinstance(path, str):
        return False
    p = path.lower().replace("\\", "/")
    segs = p.split("/")
    if any(suf in segs[-1] for suf in _BASENAME_SUFFIXES):
        return True
    return any(seg in _SEGMENTS for seg in segs)


def stage_6_9_enabled() -> bool:
    """Stage runs by default; ``FAULTLINE_STAGE_6_9_TEST_STRIP=0`` disables."""
    return os.environ.get(STAGE_6_9_ENV_FLAG, "1").strip() not in {"0", "false", "False"}


# ── tolerant path accessor ──────────────────────────────────────────────
_PATH_ATTRS = ("file", "path", "from_path", "file_path")


def _path_of(entry: Any) -> str | None:
    """Best-effort path extraction from a heterogeneous attribution /
    participant entry — a raw string, a dict, or a pydantic model."""
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for k in _PATH_ATTRS:
            v = entry.get(k)
            if isinstance(v, str):
                return v
        return None
    # pydantic model / dataclass / arbitrary object
    for k in _PATH_ATTRS:
        v = getattr(entry, k, None)
        if isinstance(v, str):
            return v
    return None


def _endpoints_of(edge: Any) -> tuple[str | None, str | None]:
    """Return ``(from_path, to_path)`` for a loc_edge / edge entry."""
    if isinstance(edge, dict):
        fp = edge.get("from_path")
        tp = edge.get("to_path")
    else:
        fp = getattr(edge, "from_path", None)
        tp = getattr(edge, "to_path", None)
    return (fp if isinstance(fp, str) else None, tp if isinstance(tp, str) else None)


def _filter_seq(seq: Any) -> tuple[list[Any], int]:
    """Drop entries whose resolved path is a test path. Returns
    ``(kept, removed_count)``. A ``None`` input or non-list is returned
    unchanged with a zero count."""
    if not isinstance(seq, list):
        return seq, 0
    kept: list[Any] = []
    removed = 0
    for e in seq:
        pth = _path_of(e)
        if pth is not None and is_test_path(pth):
            removed += 1
            continue
        kept.append(e)
    return kept, removed


def _filter_edges(seq: Any) -> tuple[list[Any], int]:
    """Drop an edge if EITHER endpoint is a test path."""
    if not isinstance(seq, list):
        return seq, 0
    kept: list[Any] = []
    removed = 0
    for e in seq:
        fp, tp = _endpoints_of(e)
        if (fp and is_test_path(fp)) or (tp and is_test_path(tp)):
            removed += 1
            continue
        kept.append(e)
    return kept, removed


def _strip_attr(obj: Any, attr: str, *, edges: bool = False) -> int:
    """Filter a single sequence attribute on ``obj`` in place. No-op when
    the attribute is missing / not a list. Returns the removed count."""
    cur = getattr(obj, attr, None)
    if cur is None:
        return 0
    new, removed = (_filter_edges(cur) if edges else _filter_seq(cur))
    if removed:
        setattr(obj, attr, new)
    return removed


# Fields swept per object kind. Tolerant: missing attrs are skipped.
_FEATURE_LIST_ATTRS = (
    "paths",
    "symbol_attributions",
    "shared_attributions",
    "participants",
    "shared_participants",
)
_FLOW_LIST_ATTRS = (
    "flow_symbol_attributions",
    "loc_symbol_attributions",
    "loc_nodes",
    "paths",
    "participants",
    "shared_participants",
    "symbol_attributions",
    "hotspot_files",
)
_FLOW_EDGE_ATTRS = ("loc_edges", "edges")


def _feature_path_empty(feature: Any) -> bool:
    paths = getattr(feature, "paths", None)
    return not paths


def _flow_is_empty(flow: Any) -> bool:
    """A flow is empty (drop-worthy) when it has no surviving files AND
    no surviving symbol/participant attributions."""
    for attr in (
        "paths",
        "participants",
        "flow_symbol_attributions",
        "loc_symbol_attributions",
        "loc_nodes",
        "symbol_attributions",
    ):
        v = getattr(flow, attr, None)
        if v:
            return False
    return True


def _recompute_entry(flow: Any) -> bool:
    """If ``entry_point_file`` is a test path, point it at the first
    surviving non-test file: prefer the earliest surviving ``loc_nodes``
    entry with ``role == "entry"``, else the top remaining ``paths``
    file. Returns ``True`` if the entry was recomputed.

    Caller drops the flow afterwards if it is empty (no non-test file)."""
    epf = getattr(flow, "entry_point_file", None)
    if not (epf and is_test_path(epf)):
        return False

    new_entry: str | None = None
    # Prefer a surviving loc_node with role == entry.
    for node in getattr(flow, "loc_nodes", None) or []:
        role = node.get("role") if isinstance(node, dict) else getattr(node, "role", None)
        npath = _path_of(node)
        if role == "entry" and npath and not is_test_path(npath):
            new_entry = npath
            break
    # Else any surviving non-test loc_node.
    if new_entry is None:
        for node in getattr(flow, "loc_nodes", None) or []:
            npath = _path_of(node)
            if npath and not is_test_path(npath):
                new_entry = npath
                break
    # Else top remaining path.
    if new_entry is None:
        for p in getattr(flow, "paths", None) or []:
            if isinstance(p, str) and not is_test_path(p):
                new_entry = p
                break

    if new_entry is not None:
        flow.entry_point_file = new_entry
        # The richer FlowEntryPoint mirror, if present, should not point
        # at a stripped file either.
        ep = getattr(flow, "entry_point", None)
        if ep is not None:
            ep_path = getattr(ep, "path", None)
            if ep_path and is_test_path(ep_path):
                ep.path = new_entry
        return True
    # No surviving non-test entry — leave entry_point_file as-is; the
    # flow will be dropped by the empty check.
    return False


def _strip_flow(flow: Any, stats: dict[str, int]) -> None:
    """Strip every test entry from a single Flow object in place."""
    for attr in _FLOW_LIST_ATTRS:
        removed = _strip_attr(flow, attr)
        if removed:
            stats["paths_removed"] += removed
    for attr in _FLOW_EDGE_ATTRS:
        removed = _strip_attr(flow, attr, edges=True)
        if removed:
            stats["paths_removed"] += removed


def _iter_unique(objs: Iterable[Any]) -> list[Any]:
    """De-duplicate by object identity, preserving order. The same Flow
    object appears in both ``Feature.flows`` (containment) and the
    top-level bipartite list; we must strip / count it exactly once."""
    seen: set[int] = set()
    out: list[Any] = []
    for o in objs:
        if id(o) in seen:
            continue
        seen.add(id(o))
        out.append(o)
    return out


def strip_test_paths(features: list[Any], flows: list[Any]) -> dict[str, int]:
    """Strip test-file entries from the feature / flow OUTPUT TREE.

    Mutates ``features`` and ``flows`` (and any ``Feature.flows``
    containment lists) IN PLACE, dropping features / flows that become
    empty and recomputing test entry points. NEVER touches metric
    scalars.

    Returns telemetry::

        {paths_removed, features_dropped, flows_dropped,
         flow_entries_recomputed}
    """
    stats = {
        "paths_removed": 0,
        "features_dropped": 0,
        "flows_dropped": 0,
        "flow_entries_recomputed": 0,
    }

    features = features if isinstance(features, list) else []
    flows = flows if isinstance(flows, list) else []

    # ── 1. Strip every Flow exactly once (across both views) ──────────
    flow_objs = _iter_unique(
        list(flows)
        + [fl for f in features for fl in (getattr(f, "flows", None) or [])]
    )
    for fl in flow_objs:
        _strip_flow(fl, stats)
        if _recompute_entry(fl):
            stats["flow_entries_recomputed"] += 1

    # ── 2. Decide which flows to drop (empty after strip) ─────────────
    drop_flow_ids = {id(fl) for fl in flow_objs if _flow_is_empty(fl)}
    stats["flows_dropped"] = len(drop_flow_ids)

    if drop_flow_ids:
        # Prune top-level list in place.
        flows[:] = [fl for fl in flows if id(fl) not in drop_flow_ids]
        # Prune each feature's containment list in place.
        for f in features:
            fl_list = getattr(f, "flows", None)
            if isinstance(fl_list, list):
                fl_list[:] = [fl for fl in fl_list if id(fl) not in drop_flow_ids]

    # ── 3. Strip feature-level surfaces ───────────────────────────────
    for f in features:
        for attr in _FEATURE_LIST_ATTRS:
            removed = _strip_attr(f, attr)
            if removed:
                stats["paths_removed"] += removed

    # ── 4. Drop features that became path-empty ───────────────────────
    drop_feature_ids = {id(f) for f in features if _feature_path_empty(f)}
    stats["features_dropped"] = len(drop_feature_ids)
    if drop_feature_ids:
        features[:] = [f for f in features if id(f) not in drop_feature_ids]

    return stats
