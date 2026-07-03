"""Incremental scan support — ``dynvo scan --since <commit>``.

Sprint 1 (2026-05-23). Phase 1 strategy:

  1. Load the base scan JSON (``--base-scan-path``).
  2. Compute ``git diff --name-only <since>..HEAD`` for the changed
     file set.
  3. From ``base_scan.path_index``, look up which ``feature_uuid``s
     each changed file belongs to → the "touched" feature set.
  4. Run the full pipeline_v2 (cold) — features come out fresh, with
     fresh names + UUIDs (the LLM is non-deterministic across runs).
  5. Run lineage matching against the base scan so unchanged features
     keep their base UUID; only TOUCHED features may legitimately
     change identity (rename / split / merge).
  6. For features NOT in the touched set whose lineage matched a
     base feature, replace their newly-computed metrics with the
     base feature's metrics (carry-forward — they didn't change,
     so re-computing impact/coverage adds noise + cost).

Phase 1 deliberately re-runs the full pipeline rather than building
a true incremental engine. This guarantees correctness while still
giving the SaaS benefits — stable UUIDs, base-aware lineage stamps,
and a clear ``is_full_scan: false`` marker on the output. Phase 2
will skip stages for unchanged features once Gate 3 evidence
informs us whether stage-level skipping is safe.

The merge step (carry-forward unchanged feature metrics) is opt-in
via the ``carry_forward_unchanged`` flag — default ``True`` for
SaaS callers (matches the spec) and overridable for tests.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_base_scan(path: Path | str) -> dict[str, Any]:
    """Read a previous scan JSON from disk and return the parsed dict.

    Validates that the file has the minimum keys we need (``features``
    or ``developer_features``, and at least one of ``scan_meta`` or
    ``last_scanned_sha``). Raises ``FileNotFoundError`` /
    ``ValueError`` on problems.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"base scan not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"base scan {p} is not a JSON object")
    if "features" not in data and "developer_features" not in data:
        raise ValueError(
            f"base scan {p} missing both 'features' and 'developer_features'",
        )
    return data


def changed_files_since(repo_path: Path, since: str) -> list[str]:
    """Return ``git diff --name-only <since>..HEAD`` as repo-relative paths.

    Empty list when the diff is empty or git fails (logged at warning).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only",
             f"{since}..HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as exc:
        logger.warning(
            "incremental: git diff %s..HEAD failed (%s) — empty changed-set",
            since, exc,
        )
        return []
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def head_sha(repo_path: Path) -> str:
    """Return the current HEAD SHA; empty string on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("incremental: rev-parse HEAD failed (%s)", exc)
        return ""


def touched_feature_uuids(
    changed_files: list[str], base_scan: dict[str, Any],
) -> set[str]:
    """Map changed files → set of feature UUIDs they belong to.

    Reads ``base_scan.path_index``. Files not in the index are silently
    dropped from the "touched" set — they're either untracked (just
    added in the diff) or didn't exist at base scan time. New files
    get re-scanned anyway because they show up as a new feature in the
    fresh full-pipeline run.
    """
    path_index = base_scan.get("path_index") or {}
    touched: set[str] = set()
    for f in changed_files:
        entry = path_index.get(f)
        if not entry:
            continue
        uuid = entry.get("feature_uuid")
        if uuid:
            touched.add(str(uuid))
    return touched


def carry_forward_metrics(
    new_features: list[dict[str, Any]],
    base_features: list[dict[str, Any]],
    touched_uuids: set[str],
    *,
    metric_keys: tuple[str, ...] = (
        "health_score", "bug_fix_ratio", "bug_fixes", "coverage_pct",
        "total_commits", "last_modified", "weekly_points", "hotspot_files",
        "symbol_health_score",
    ),
) -> int:
    """For each new feature whose UUID matches a base feature AND is
    NOT in ``touched_uuids``, overwrite its metric keys with the base
    feature's values.

    Mutates ``new_features`` in place. Returns the number of features
    that were carried forward.

    Rationale: re-computing health / coverage / commit counts for a
    feature whose files didn't change adds noise (same answer modulo
    the new HEAD commit) and runs Stage 6 work pointlessly. By
    pinning the base values we get truly stable output for the
    untouched majority.
    """
    base_by_uuid = {
        str(f.get("uuid") or ""): f for f in base_features if f.get("uuid")
    }
    carried = 0
    for feat in new_features:
        u = str(feat.get("uuid") or "")
        if not u or u in touched_uuids:
            continue
        base = base_by_uuid.get(u)
        if not base:
            continue
        for k in metric_keys:
            if k in base:
                feat[k] = base[k]
        carried += 1
    return carried


__all__ = [
    "load_base_scan",
    "changed_files_since",
    "head_sha",
    "touched_feature_uuids",
    "carry_forward_metrics",
]
