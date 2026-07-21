"""B76 — metrics recompute-on-emission (Stage 6.998, flag-gated).

Forensics class: MINT-TIME METRIC ZEROING (BUG_LEDGER §ФОРЕНЗИКА
CHURN-UNDERCOUNT, VERIFIED byte-exact 2026-07-21). Stage 6 stamps
commit metrics ONCE (phase_enrich → ``stage_6_metrics``) BEFORE the
Layer-2 surgery; the post-Stage-6 mint factories (8.9 subdecompose,
6.87 lane excavation, file-lane, provenance re-home) then birth
features with ``total_commits=0`` / ``bug_fixes=0`` but with
*inherited* ``authors`` / ``health_score`` / ``last_modified`` from
the source blob's deep copy — "impossible" rows (``tc==0 ∧
authors>0``: Soc0 72 / twenty 149 / novu 95). The PF layer summed
metrics over CONTRIBUTORS (both losing the zeroed mass and
double-counting shared commits: dev-sum 7370 > 5654 repo commits) —
Alerts 5 vs ~63 real commits, 15 authors vs 2, health half-made of
zero-evidence 100.0 placeholders.

Mechanism (spec docs/anchor-arc/fixb76-metrics-recompute-spec.md):

  1. RECOMPUTE ON EMISSION — re-run ``_attach_commit_metrics`` over the
     FINAL developer-feature list after the last membership-mutating
     stage before Stage 7 (post-layer2 / post-UF-rehome slot in
     ``phase_finalize``). The commit index is O(commits).
  2. PF metrics from the PF's OWN path-set (never sum-over-
     contributors) with per-commit DEDUP — a commit counts once per
     PF no matter how many files/contributors it touches.
  3. PF health from the PF's own aggregated commit list via
     ``_calculate_health`` — zero-evidence contributors no longer
     inject placeholder 100.0 into an averaged health.
  4. Mint factories stamp an honest null-state
     (:func:`mint_null_state`) instead of deep-copying identity —
     see the four call sites.
  5. Invariant (validate_scan candidate): ``total_commits==0 ⇒
     authors==[] ∧ health_confidence=="insufficient"`` — holds by
     construction after the recompute.

SACRED: display-only for structural layers — the pass never touches
membership (``paths`` / ``member_files`` / ``flows`` / UF homes) and
never touches ``hotspot_files`` (not part of the affected class).

Kill-switch: ``FAULTLINE_METRICS_RECOMPUTE`` (default OFF) — unset /
``0`` never enters the pass and keeps the mint factories' legacy
inheritance byte-identical.

No LLM. No network. Deterministic and idempotent (same input ⇒ same
stamped values; the underlying sweep is a pure function of features ×
commits).
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.stage_6_metrics import (
    _attach_commit_metrics,
    _finalize_feature_metrics,
)

if TYPE_CHECKING:
    from faultline.models.types import Commit, Feature

logger = logging.getLogger(__name__)

#: Kill-switch — default OFF (unset/``0`` ⇒ byte-identical to main).
METRICS_RECOMPUTE_ENV = "FAULTLINE_METRICS_RECOMPUTE"


def metrics_recompute_enabled() -> bool:
    """B76 — default **OFF** (unset/``0`` ⇒ byte-identical to main)."""
    return os.environ.get(METRICS_RECOMPUTE_ENV, "").strip().lower() in {
        "1", "true",
    }


def mint_null_state() -> dict[str, Any]:
    """Honest zero-evidence state for a mint-factory birth (spec pt. 4).

    Mirrors the ``_finalize_feature_metrics`` zero-commit convention
    exactly: epoch ``last_modified`` (the deterministic placeholder —
    never scan wall-clock), back-compat numeric ``health_score`` 100.0
    explicitly confidence-marked ``"insufficient"``, and NO inherited
    ``authors``. Idempotent over factories that already stamp part of
    this state (file-lane). The emission recompute then replaces the
    placeholders with measured values for every row the final
    membership actually attributes commits to.
    """
    return {
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "authors": [],
        "last_modified": datetime.fromtimestamp(0, tz=timezone.utc),
        "health_score": 100.0,
        "health_confidence": "insufficient",
    }


def _attach_pf_commit_metrics(
    product_features: list["Feature"],
    commits: list["Commit"],
) -> None:
    """Stamp commit metrics on each PF from its OWN path-set (spec pt. 2+3).

    Differs from the developer sweep (``_build_file_to_feature_index``)
    in ONE deliberate way: a file claimed by several PFs credits EVERY
    claiming PF (own-path-set law — PF path unions may legitimately
    overlap), instead of last-writer-wins. Per-commit dedup is the
    ``touched`` set: a commit counts once per PF regardless of how many
    of its files (or former contributors) it touches. The directory
    fallback (deleted/renamed files) only fires when the parent dir
    maps unambiguously to a single PF — mirroring the dev-sweep
    ambiguity skip.

    Health/authors/last_modified stamping is delegated to
    ``_finalize_feature_metrics`` — the PF's health comes from its own
    aggregated commit list via ``_calculate_health``; zero-evidence
    PFs stamp ``health_confidence="insufficient"`` with no contributor
    placeholder averaging.
    """
    if not product_features:
        return

    path_claims: dict[str, set[str]] = defaultdict(set)
    dir_owners: dict[str, set[str]] = defaultdict(set)
    for pf in product_features:
        for p in pf.paths or []:
            path_claims[p].add(pf.name)
            parent = str(Path(p).parent)
            if parent != ".":
                dir_owners[parent].add(pf.name)
    dir_to_pf: dict[str, str] = {}
    ambiguous_dirs: set[str] = set()
    for parent, owners in dir_owners.items():
        if len(owners) == 1:
            dir_to_pf[parent] = next(iter(owners))
        else:
            ambiguous_dirs.add(parent)

    pf_commits: dict[str, list["Commit"]] = defaultdict(list)
    pf_authors: dict[str, set[str]] = defaultdict(set)
    pf_last_modified: dict[str, datetime] = {}

    for commit in commits:
        touched: set[str] = set()
        for fp in commit.files_changed:
            names = path_claims.get(fp)
            if names:
                touched.update(names)
                continue
            parent = str(Path(fp).parent)
            if parent in ambiguous_dirs:
                continue
            nm = dir_to_pf.get(parent)
            if nm:
                touched.add(nm)
        for nm in touched:
            pf_commits[nm].append(commit)
            pf_authors[nm].add(commit.author)
            existing = pf_last_modified.get(nm)
            if existing is None or commit.date > existing:
                pf_last_modified[nm] = commit.date

    _finalize_feature_metrics(
        product_features, pf_commits, pf_authors, pf_last_modified,
    )


def _impossible_count(rows: list["Feature"]) -> int:
    """Census: rows claiming authors with zero attributed commits."""
    return sum(
        1 for f in rows
        if (getattr(f, "total_commits", 0) or 0) == 0
        and len(getattr(f, "authors", None) or []) > 0
    )


def run_metrics_recompute(
    features: list["Feature"],
    product_features: list["Feature"],
    commits: list["Commit"],
) -> dict[str, Any]:
    """Recompute commit metrics over the FINAL emitted membership.

    Mutates metric fields in place (``total_commits`` / ``bug_fixes`` /
    ``bug_fix_ratio`` / ``authors`` / ``last_modified`` /
    ``health_score`` / ``health_confidence``) on both layers; touches
    nothing else. Returns deterministic content-derived telemetry.
    """
    dev_before = _impossible_count(features)
    pf_before = _impossible_count(product_features)

    # (1) Developer layer — the literal Stage-6 sweep re-run over the
    # post-surgery list (exclusive paths ⇒ identical semantics).
    _attach_commit_metrics(features, commits)

    # (2)+(3) PF layer — own path-set, per-commit dedup, own health.
    _attach_pf_commit_metrics(product_features, commits)

    tele = {
        "dev_rows": len(features),
        "pf_rows": len(product_features),
        "impossible_dev_before": dev_before,
        "impossible_dev_after": _impossible_count(features),
        "impossible_pf_before": pf_before,
        "impossible_pf_after": _impossible_count(product_features),
    }
    logger.info(
        "metrics_recompute: dev %d rows (impossible %d -> %d), "
        "pf %d rows (impossible %d -> %d)",
        tele["dev_rows"], dev_before, tele["impossible_dev_after"],
        tele["pf_rows"], pf_before, tele["impossible_pf_after"],
    )
    return tele


__all__ = [
    "METRICS_RECOMPUTE_ENV",
    "metrics_recompute_enabled",
    "mint_null_state",
    "run_metrics_recompute",
]
