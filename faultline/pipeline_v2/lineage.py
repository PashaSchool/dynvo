"""Stable feature/flow UUIDs via Jaccard-overlap lineage matching.

Sprint 1 (2026-05-23) — first piece of the SaaS infra critical path.
Mints UUIDs for every feature + flow on first scan, then on every
subsequent scan matches new features back to base features by file-set
Jaccard overlap so dashboards / MCP tools can track a feature across
its lifetime even when its LLM-derived name drifts.

Cold-scan principle (memory/rule-cold-scan.md) compliance
=========================================================

This module does NOT introduce engine-side persistence. The base
scan is an EXPLICIT input passed via ``--base-scan-path`` on the CLI
(or via the ``base_scan`` kwarg in tests). When no base scan is
supplied the algorithm mints fresh uuid4s for everything — which is
the cold-case behaviour. Lineage metadata is layered on TOP of the
existing scan output; no scan-time decision is altered by base-scan
contents.

Match semantics
===============

For each NEW feature N, compute Jaccard overlap J(N, B) for every
base feature B. Then classify:

  * exact-rename — overlap ≥ ``rename_threshold`` (default 0.70) AND
    name differs → reuse B.uuid, append B.name to ``previous_names``.
  * carry-forward — overlap ≥ ``rename_threshold`` AND name matches
    → reuse B.uuid silently.
  * split — at least 2 new features share base B with overlap ≥
    ``related_threshold`` (default 0.40). The HIGHEST-overlap new
    feature inherits B.uuid; the others get fresh UUIDs with
    ``split_from=B.uuid``.
  * merge — one new feature N has ≥2 base features with overlap ≥
    ``related_threshold``. The HIGHEST-overlap base wins (its UUID
    reused); the others are recorded in ``merged_from=[uuid,...]``.
  * pure-new — no base feature reaches ``related_threshold`` → mint
    a new uuid4.

Both directions (split + merge) are computed simultaneously; the
algorithm prioritises the rename / carry-forward path before the
split/merge bookkeeping because that's the common case (90%+ of
features in a small-diff scan stay 1:1).

UUID uniqueness invariant
=========================

The function asserts no UUID appears twice in the returned list.
This is a defensive guard; the algorithm itself only re-uses a
base UUID exactly once (the highest-overlap match), so collisions
should never occur.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Default thresholds — exposed via CLI flag for tuning per stack.
RENAME_THRESHOLD = 0.70
RELATED_THRESHOLD = 0.40


@dataclass
class LineageRecord:
    """Per-feature (or per-flow) lineage assignment for one scan.

    ``uuid`` is what gets stamped onto the output Feature.
    ``previous_names`` accumulates rename history (most-recent last).
    ``split_from`` is set when this feature originated as part of a
    split off of a single base feature; ``merged_from`` is set when
    this feature absorbs multiple base features.
    """

    name: str
    uuid: str
    previous_names: list[str] = field(default_factory=list)
    split_from: str | None = None
    merged_from: list[str] = field(default_factory=list)


@dataclass
class LineageStats:
    """Telemetry for ``scan_meta`` so operators can debug stability."""

    base_count: int = 0
    new_count: int = 0
    carried_forward: int = 0
    renamed: int = 0
    split: int = 0
    merged: int = 0
    fresh: int = 0
    rename_threshold: float = RENAME_THRESHOLD
    related_threshold: float = RELATED_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_count": self.base_count,
            "new_count": self.new_count,
            "carried_forward": self.carried_forward,
            "renamed": self.renamed,
            "split": self.split,
            "merged": self.merged,
            "fresh": self.fresh,
            "rename_threshold": self.rename_threshold,
            "related_threshold": self.related_threshold,
        }


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


def _mint_uuid(name: str = "", seq: int = 0, ns: str = "") -> str:
    """CONTENT-DERIVED uuid — sha256 of (namespace, name, per-name sequence).

    uuid4 minting regenerated every identity on every run: two scans of an
    IDENTICAL repo state produced fully-disjoint flow/feature uuids, which
    (a) made semantically-identical outputs byte-different, and (b) leaked
    into LLM prompts via member ids (dup-named flows key by uuid), churning
    content-hash cache keys (supabase determinism arc, 2026-07-02). A
    name+sequence hash is stable for identical content, unique within a
    scan (seq = occurrence counter per name; _assert_unique still guards),
    and changes naturally when content changes. Empty-args fallback keeps
    uuid4 for legacy callers.
    """
    if not name:
        return _uuid.uuid4().hex
    import hashlib
    return hashlib.sha256(
        f"flmint-v1|{ns}|{name}|{seq}".encode("utf-8")).hexdigest()[:32]


class _SeqMinter:
    """Per-name occurrence counter → deterministic uuid per identity."""

    def __init__(self, ns: str) -> None:
        self._ns = ns
        self._seen: dict[str, int] = {}

    def mint(self, name: str) -> str:
        seq = self._seen.get(name, 0)
        self._seen[name] = seq + 1
        return _mint_uuid(name, seq, self._ns)


def _path_set(item: dict[str, Any]) -> frozenset[str]:
    """Pull the file-set from a feature/flow dict in a tolerant way."""
    paths = item.get("paths") or []
    return frozenset(str(p) for p in paths if p)


def assign_feature_lineage(
    new_features: list[dict[str, Any]],
    base_features: list[dict[str, Any]] | None,
    *,
    rename_threshold: float = RENAME_THRESHOLD,
    related_threshold: float = RELATED_THRESHOLD,
    _ns: str = "feature",
) -> tuple[list[LineageRecord], LineageStats]:
    """Compute UUID + lineage metadata for every entry in ``new_features``.

    Args:
        new_features: list of dicts with at minimum ``name`` and
            ``paths`` keys. The function does NOT mutate these dicts;
            it returns ``LineageRecord`` objects in the same order.
        base_features: previous scan's features (same shape). When
            ``None`` or empty, all new features become ``fresh`` with
            new UUIDs.
        rename_threshold: Jaccard cutoff for rename / carry-forward.
        related_threshold: Jaccard cutoff for split / merge bookkeeping.

    Returns:
        ``(records, stats)`` — one record per new feature in input
        order, plus telemetry.
    """
    stats = LineageStats(
        rename_threshold=rename_threshold,
        related_threshold=related_threshold,
    )
    new_paths = [_path_set(f) for f in new_features]
    new_names = [str(f.get("name", "")) for f in new_features]
    stats.new_count = len(new_features)

    base = list(base_features or [])
    stats.base_count = len(base)

    if not base:
        minter = _SeqMinter(_ns)
        records = [
            LineageRecord(name=n, uuid=str(f.get("uuid") or minter.mint(n)))
            for n, f in zip(new_names, new_features)
        ]
        stats.fresh = len(records)
        _assert_unique(records)
        return records, stats

    base_paths = [_path_set(b) for b in base]
    base_names = [str(b.get("name", "")) for b in base]
    base_uuids = [str(b.get("uuid") or "") for b in base]
    # Mint UUIDs for any base feature that pre-dates the lineage system.
    _base_minter = _SeqMinter(f"{_ns}-base")
    base_uuids = [
        u or _base_minter.mint(bn) for u, bn in zip(base_uuids, base_names)
    ]
    _fresh_minter = _SeqMinter(_ns)

    # ── Step 1 — compute full overlap matrix ────────────────────────
    # overlaps[new_idx] = sorted list of (jaccard, base_idx)
    overlaps: list[list[tuple[float, int]]] = []
    for n_idx, np in enumerate(new_paths):
        row: list[tuple[float, int]] = []
        for b_idx, bp in enumerate(base_paths):
            j = _jaccard(np, bp)
            if j >= related_threshold:
                row.append((j, b_idx))
        row.sort(reverse=True)  # highest overlap first
        overlaps.append(row)

    # ── Step 2 — competition for base UUIDs ─────────────────────────
    # For each base, find which new features want it (i.e. best match
    # ≥ rename_threshold). The new feature with HIGHEST overlap wins.
    # All other claimants of the same base get treated as split-from
    # (if rename_threshold) or merged-from (handled below).
    #
    # base_winner[b_idx] = (n_idx, jaccard) | None
    base_winner: list[tuple[int, float] | None] = [None] * len(base)
    for n_idx, row in enumerate(overlaps):
        if not row:
            continue
        top_j, top_b = row[0]
        if top_j < rename_threshold:
            continue
        cur = base_winner[top_b]
        if cur is None or top_j > cur[1]:
            base_winner[top_b] = (n_idx, top_j)

    # Reverse map: n_idx -> base_idx it wins (only the top match counts)
    n_wins_base: dict[int, int] = {}
    for b_idx, w in enumerate(base_winner):
        if w is not None:
            n_idx, _ = w
            # First-write wins if same n_idx hits two bases — shouldn't
            # happen since we picked top-1 per new feature, but guard.
            n_wins_base.setdefault(n_idx, b_idx)

    # ── Step 3 — assign records ─────────────────────────────────────
    records = []
    for n_idx, feat in enumerate(new_features):
        name = new_names[n_idx]
        row = overlaps[n_idx]
        winning_base = n_wins_base.get(n_idx)

        if winning_base is not None:
            b_uuid = base_uuids[winning_base]
            b_name = base_names[winning_base]
            # carry previous_names from base record if present
            prev = list(base[winning_base].get("previous_names") or [])
            if b_name and b_name != name and b_name not in prev:
                prev.append(b_name)
                stats.renamed += 1
            else:
                stats.carried_forward += 1
            # merge: did this new feature also claim other bases
            # above related_threshold?
            merged_from = [
                base_uuids[b_idx]
                for j, b_idx in row
                if b_idx != winning_base and j >= related_threshold
            ]
            if merged_from:
                stats.merged += 1
            rec = LineageRecord(
                name=name,
                uuid=b_uuid,
                previous_names=prev,
                merged_from=merged_from,
            )
            records.append(rec)
            continue

        # No rename-threshold win — could be a split (lost the race
        # to a higher-overlap sibling on the same base) or pure new.
        # Find the highest-overlap base where this new feature was
        # NOT the winner.
        split_from: str | None = None
        for j, b_idx in row:
            if j >= related_threshold:
                w = base_winner[b_idx]
                if w is not None and w[0] != n_idx:
                    split_from = base_uuids[b_idx]
                    stats.split += 1
                    break
        if split_from is None:
            stats.fresh += 1
        rec = LineageRecord(
            name=name,
            uuid=_fresh_minter.mint(name),
            split_from=split_from,
        )
        records.append(rec)

    _assert_unique(records)
    return records, stats


def _assert_unique(records: Iterable[LineageRecord]) -> None:
    seen: set[str] = set()
    for r in records:
        if r.uuid in seen:
            raise ValueError(
                f"lineage: duplicate UUID {r.uuid!r} for {r.name!r}"
            )
        seen.add(r.uuid)


# ── Convenience wrappers ────────────────────────────────────────────


def assign_flow_lineage(
    new_flows: list[dict[str, Any]],
    base_flows: list[dict[str, Any]] | None,
    *,
    rename_threshold: float = RENAME_THRESHOLD,
    related_threshold: float = RELATED_THRESHOLD,
) -> tuple[list[LineageRecord], LineageStats]:
    """Same algorithm as :func:`assign_feature_lineage` for flows.

    Flow ``name`` keys are typically of the form ``"checkout-flow"`` —
    we treat them as opaque slugs. ``paths`` is the file-set of the
    flow's participants.
    """
    return assign_feature_lineage(
        new_flows,
        base_flows,
        rename_threshold=rename_threshold,
        related_threshold=related_threshold,
        _ns="flow",
    )


__all__ = [
    "RENAME_THRESHOLD",
    "RELATED_THRESHOLD",
    "LineageRecord",
    "LineageStats",
    "assign_feature_lineage",
    "assign_flow_lineage",
]
