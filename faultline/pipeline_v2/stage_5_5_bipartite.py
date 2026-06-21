"""Stage 5.5 — bipartite feature ↔ flow store + blast-radius metric.

Pure Python. No LLM. Runs AFTER Stage 5 post-process (so we operate
on the cleaned, slugified, naming-disciplined feature list) and BEFORE
Stage 6 metrics enrichment.

Why this stage exists
=====================

Today's containment view (``Feature.flows[]``) forces every flow to
have exactly one parent. But cross-cutting flows — auth checks,
logging, validation, telemetry — naturally belong to MANY features.
Picking one arbitrarily loses the "shared infrastructure" signal that
is half our moat vs Sourcegraph.

This stage promotes the existing per-feature lists into a bipartite
graph stored as ``feature_flow_edges[]`` while keeping the containment
view intact for the landing app's existing renderer. Each flow gets:

  * ``id``                         — global stable id (``primary::slug``)
  * ``primary_feature``            — canonical owner (from Stage 3)
  * ``secondary_features``         — cross-cutting attachments
  * ``shared_with_flows_count``    — flows sharing ≥1 path with this one
  * ``shared_with_features_count`` — ``len(secondary_features)``
  * ``cross_cutting``              — convenience flag

Algorithm
=========

Deterministic, two cheap passes over the post-Stage-5 feature list
(preceded by two duplicate-collapse passes):

  0.  Collapse provably-identical duplicate flows WITHIN a feature
      (post feature-merge concatenation) and ACROSS features (one hub
      file is a member of N features, so Stage 3 emits the same physical
      flow once per containing feature). Cross-feature survivors keep one
      primary owner; the other features fold into ``secondary_features``.
  1. Build ``path → set[feature_name]`` from ``Feature.paths``.
  2. For each ``Flow``:
       a. Resolve ``primary_feature`` (the feature that owns this flow
          in the containment view).
       b. Compute ``secondary_features`` = union over flow paths of
          ``path_to_features[p]``, minus the primary, minus the empty
          set.
       c. Mint ``flow.id`` = ``f"{primary}::{slug}"``.
  3. For each pair of flows ``(a, b)`` with a ≠ b, increment a counter
     when they share ≥1 path. The result is ``shared_with_flows_count``
     per flow.
  4. Emit one ``FeatureFlowEdge`` with type=``primary`` per flow plus
     one with type=``secondary`` per (flow, secondary feature) pair.
  5. Return the top-level ``flows[]`` projection AND the edge list,
     PLUS a Stage5_5 telemetry dict for ``scan_meta``.

Invariants
==========

  * Every flow has exactly one primary edge.
  * Secondary features never include the primary feature.
  * ``len(top_level.flows)`` == sum of ``len(f.flows)`` across all
    features (every flow has exactly one primary owner).
  * ``bipartite_edges_primary`` == ``len(top_level.flows)``.
  * Two flows that share zero paths produce ``shared_with_flows_count
    = 0``. A flow with zero paths produces 0 for both counts.

No LLM, no network, no git — pure in-memory computation over the
existing Stage 5 output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from faultline.models.types import Feature, FeatureFlowEdge, Flow

if TYPE_CHECKING:
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)


# ── Result shape ──────────────────────────────────────────────────────────


@dataclass
class Stage5_5Result:
    """Bipartite store + telemetry produced by Stage 5.5.

    Attributes:
        features: the post-Stage-5 features, with each contained
            ``Flow`` mutated in place so the new bipartite fields
            (``id``, ``primary_feature``, ``secondary_features``,
            ``shared_with_flows_count``, ``shared_with_features_count``,
            ``cross_cutting``) are populated.
        flows: top-level projection — every flow, once, in stable order
            (sorted by ``id``).
        edges: bipartite edge list. Every flow contributes exactly one
            ``type="primary"`` edge plus zero-or-more ``type="secondary"``
            edges.
        telemetry: counts to fold into ``scan_meta``.
    """

    features: list[Feature]
    flows: list[Flow]
    edges: list[FeatureFlowEdge]
    telemetry: dict[str, int] = field(default_factory=dict)


# ── Internal helpers ──────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Kebab-case slug used inside the global ``Flow.id``."""
    if not text:
        return ""
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _flow_id(primary: str, flow_name: str) -> str:
    """Mint the global stable id ``"{primary}::{slug}"``.

    We don't reuse ``flow_name`` verbatim because Stage 3 emits human
    labels that may carry stray casing or punctuation; we want a stable
    debuggable form across rescans.
    """
    return f"{primary}::{_slugify(flow_name)}"


def _dedup_identical_flows(features: list[Feature]) -> int:
    """Collapse provably-identical duplicate flows within each feature, IN PLACE.

    Two flows are duplicates only when their ``(name, entry_point_file,
    entry_point_line)`` match exactly — same journey, same entry point. The
    first occurrence (stable order) is kept; later copies are dropped. Flows
    with no resolved entry point are never merged on entry alone, so an
    entry-less flow only collapses against another entry-less flow of the SAME
    name (key = (name, None, None)).

    Returns the number of dropped duplicate flows (telemetry).
    """
    dropped = 0
    for feat in features:
        flows = getattr(feat, "flows", None)
        if not flows:
            continue
        seen: set[tuple[str, str | None, int | None]] = set()
        kept: list[Flow] = []
        for fl in flows:
            key = (fl.name, fl.entry_point_file, fl.entry_point_line)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            kept.append(fl)
        if len(kept) != len(flows):
            feat.flows = kept
    return dropped


def _flow_identity_key(
    flow: Flow,
) -> tuple[str, str | None, int | None, tuple[tuple[str, int, int], ...]]:
    """Byte-identity key for the cross-feature duplicate collapse.

    A flow is *the same logical flow* — regardless of which feature it was
    attributed to — when it shares ALL of:

      * ``name``              — the journey label,
      * ``entry_point_file``  — where the journey begins,
      * ``entry_point_line``  — the exact entry line,
      * the sorted set of ``line_ranges`` — the flow's own code span.

    The line-range component is what keeps the collapse conservative: two
    flows that share a name and entry FILE but cover DIFFERENT lines (e.g.
    FastAPI's many ``create-item-flow`` tutorials, each in its own module at
    its own line) produce different keys and BOTH survive. Only provably
    byte-identical entry+span copies — the hub-file fan-out where one
    physical entry is a member of N features — collapse.
    """
    ranges = tuple(
        sorted(
            (lr.path, lr.start_line, lr.end_line)
            for lr in (flow.line_ranges or [])
        ),
    )
    return (flow.name, flow.entry_point_file, flow.entry_point_line, ranges)


def _entry_anchor_owners(features: Iterable[Feature]) -> dict[str, dict[str, float]]:
    """Map ``entry_file -> {feature_name: anchor_confidence}``.

    A feature *anchors* a file when its ``member_files`` carries that path
    with ``role == "anchor"`` (a Stage 1/2 deterministic extractor declared
    it as the feature's own surface — the strongest ownership signal we
    have). This is used to pick the canonical primary owner when one
    physical flow was duplicated across many features: the anchor owner
    beats a feature that merely reaches the file via closure / co-commit /
    shared import. ``confidence`` lets us break ties before falling back to
    a stable lexicographic order.

    Features with no ``member_files`` (e.g. Stage-4 residual features) simply
    don't appear here; the caller falls back to the lexicographic tie-break.
    """
    out: dict[str, dict[str, float]] = {}
    for feat in features:
        for mf in getattr(feat, "member_files", None) or []:
            if getattr(mf, "role", None) != "anchor":
                continue
            path = getattr(mf, "path", None)
            if not path:
                continue
            conf = float(getattr(mf, "confidence", 0.0) or 0.0)
            owners = out.setdefault(path, {})
            # Keep the highest anchor confidence seen for this (file, feature).
            if conf > owners.get(feat.name, -1.0):
                owners[feat.name] = conf
    return out


def _pick_primary_owner(
    members: list[tuple[Flow, str]],
    anchor_owners: dict[str, dict[str, float]],
) -> int:
    """Index into ``members`` of the canonical primary owner for a dup group.

    ``members`` is the list of ``(flow, owning_feature_name)`` copies that
    share one :func:`_flow_identity_key`. Selection is fully deterministic
    and scale-invariant (no magic numbers, no repo-specific paths):

      1. Prefer copies whose owning feature ANCHORS the flow's
         ``entry_point_file`` (``role == "anchor"`` in ``member_files``).
         When the entry file has anchor owners, only those copies are
         eligible; a feature that merely reaches the file via
         closure/co-commit/shared never wins the primary over a true
         anchor.
      2. Among the eligible copies, rank by anchor confidence (descending)
         so a 1.0 anchor beats a decayed claim.
      3. Break remaining ties by the lexicographically smallest feature
         name. This is stable across rescans and independent of feature
         iteration order — the same repo always elects the same owner.

    The two losing fan-out copies' features become the survivor's
    ``secondary_features`` (handled by the caller), so no attribution is
    lost — the flow keeps one primary edge + N secondary edges, exactly the
    bipartite contract, instead of N duplicate flow rows.
    """
    entry_file = members[0][0].entry_point_file
    file_anchors = anchor_owners.get(entry_file or "", {})

    def sort_key(item: tuple[int, tuple[Flow, str]]) -> tuple[int, float, str]:
        _idx, (_flow, owner) = item
        is_anchor = owner in file_anchors
        conf = file_anchors.get(owner, 0.0)
        # Eligibility flag first (anchors win), then confidence desc, then
        # name asc. Negate the booleans/conf so ``min`` selects the winner.
        return (0 if is_anchor else 1, -conf, owner)

    best_idx, _ = min(enumerate(members), key=sort_key)
    return best_idx


def _collapse_cross_feature_duplicate_flows(features: list[Feature]) -> int:
    """Collapse byte-identical duplicate flows spread ACROSS features, IN PLACE.

    The companion to :func:`_dedup_identical_flows`: that one removes
    duplicates *within* a single feature (post feature-merge concatenation);
    this one removes the orthogonal shape where ONE physical flow was
    attributed once to EACH of the many features that contain its entry file
    (hub files — a Go ``main.go`` registering N routes, a FastAPI router, a
    shared TS endpoints module). Stage 3 generates the flow independently per
    containing feature, so each copy carries a different ``primary_feature``
    (and later a different uuid) but an identical
    :func:`_flow_identity_key`.

    For every group of ≥2 such copies we:

      1. Elect ONE survivor via :func:`_pick_primary_owner` (anchor-owner of
         the entry file, tie-broken by confidence then feature name).
      2. Fold every *other* copy's owning feature into the survivor's
         ``secondary_features`` (de-duplicated, primary excluded), so the
         cross-cutting attribution is preserved as secondary edges rather
         than as duplicate flow rows.
      3. Remove the loser ``Flow`` objects from their features'
         ``flows[]`` lists. Because Stage 5.5's downstream stages share the
         same ``Flow`` instances between ``Feature.flows`` and the top-level
         projection, pruning the containment lists here is sufficient — the
         Step-5 projection re-walks ``Feature.flows`` and the losers are
         simply gone.

    Returns the number of dropped duplicate flows (telemetry). Pure,
    deterministic, no LLM.
    """
    anchor_owners = _entry_anchor_owners(features)

    # Group (flow, owner_feature_name) copies by byte-identity key, in stable
    # feature/flow iteration order so the survivor election is reproducible.
    groups: dict[
        tuple[str, str | None, int | None, tuple[tuple[str, int, int], ...]],
        list[tuple[Flow, str]],
    ] = {}
    for feat in features:
        for fl in getattr(feat, "flows", None) or []:
            groups.setdefault(_flow_identity_key(fl), []).append((fl, feat.name))

    # Flow objects to remove from each feature's containment list.
    losers_by_owner: dict[str, set[int]] = {}
    # Merge map: survivor Flow -> set of loser feature names to add as secondary.
    merge_secondaries: list[tuple[Flow, set[str]]] = []
    dropped = 0

    for members in groups.values():
        if len(members) < 2:
            continue
        keep_idx = _pick_primary_owner(members, anchor_owners)
        survivor_flow, survivor_owner = members[keep_idx]
        loser_features: set[str] = set()
        for idx, (loser_flow, loser_owner) in enumerate(members):
            if idx == keep_idx:
                continue
            loser_features.add(loser_owner)
            losers_by_owner.setdefault(loser_owner, set()).add(id(loser_flow))
            dropped += 1
        loser_features.discard(survivor_owner)
        if loser_features:
            merge_secondaries.append((survivor_flow, loser_features))

    if not dropped:
        return 0

    # Prune loser Flow objects out of each feature's containment list.
    for feat in features:
        ids_to_drop = losers_by_owner.get(feat.name)
        if not ids_to_drop:
            continue
        feat.flows = [fl for fl in feat.flows if id(fl) not in ids_to_drop]

    # Fold loser feature names into each survivor's secondary_features.
    for survivor_flow, extra in merge_secondaries:
        merged = set(survivor_flow.secondary_features or []) | extra
        merged.discard(survivor_flow.primary_feature or "")
        # The primary owner is re-stamped in Step 2; exclude it defensively
        # here too via the owner recorded at election time is implicit since
        # ``extra`` already had it discarded.
        survivor_flow.secondary_features = sorted(merged)

    return dropped


def _build_path_to_features(features: Iterable[Feature]) -> dict[str, set[str]]:
    """Reverse-index ``Feature.paths`` + ``Feature.shared_attributions``
    so we can ask "who reaches into path P?".

    Two sources are consulted:

      * ``Feature.paths`` — primary path attribution (Stage 2). Every
        tracked file is owned by exactly one feature here.
      * ``Feature.shared_attributions[*].file_path`` — a feature that
        symbol-attributes into a file it doesn't own is still
        considered a reacher for blast-radius purposes. Per the
        Sprint B1 spec: "A flow whose primary feature owns ALL its
        paths plus another feature reaching in via shared_attributions
        → still counts as secondary."

    Without this second source, cross-feature flow attribution is
    structurally zero on the v2 pipeline because every flow's
    ``Flow.paths`` is its single ``entry_point_file`` (which by
    construction lives under exactly one feature's ``paths``).
    """
    out: dict[str, set[str]] = {}
    for feat in features:
        for path in feat.paths:
            if not path:
                continue
            out.setdefault(path, set()).add(feat.name)
        for attr in getattr(feat, "shared_attributions", None) or []:
            attr_path = getattr(attr, "file_path", None)
            if not attr_path:
                continue
            out.setdefault(attr_path, set()).add(feat.name)
    return out


# ── Public entry point ────────────────────────────────────────────────────


def stage_5_5_bipartite(
    features: list[Feature],
    *,
    log: "StageLogger | None" = None,
) -> Stage5_5Result:
    """Compute the bipartite store + blast-radius metrics.

    Args:
        features: the Stage 5 output — features with their primary
            flows already attached via ``Feature.flows[]``.
        log: optional :class:`StageLogger` for per-edge / per-blast
            structured events.

    Returns:
        A :class:`Stage5_5Result` with the mutated feature list (so
        per-flow fields are populated for downstream stages), the
        top-level flow projection, the edge list, and a telemetry dict.

    Notes:
        Stage 6 expects ``Feature.flows[]`` populated so health / cost
        enrichment still hangs off the containment view. We do NOT
        strip it.
    """
    # ── Step 0 — collapse provably-identical duplicate flows ────────
    # Feature-merge stages upstream (sibling collapse, multi-workspace /
    # multi-subpath union) concatenate flow lists, so a single feature can end
    # up with the SAME flow many times — identical name AND entry point. Stage
    # 3's entry-point dedup only runs per raw LLM response, so it never sees
    # these post-merge copies. Collapse them here, BEFORE ids/uuids are stamped,
    # so each distinct flow gets exactly one id (and one row in the top-level
    # projection below). Only provably-identical flows are merged
    # (name + entry_point_file + entry_point_line); same entry / different name
    # is left alone.
    dropped_dupes = _dedup_identical_flows(features)

    # ── Step 0.5 — collapse byte-identical duplicate flows ACROSS features ─
    # The orthogonal shape Step 0 cannot reach: a HUB file (a Go ``main.go``
    # registering N routes, a FastAPI router, a shared TS endpoints module) is
    # a member/anchor of many features, so Stage 3's per-feature flow detection
    # emits the SAME physical flow once per containing feature — each copy with
    # a different ``primary_feature`` but an identical (name, entry_point_file,
    # entry_point_line, line_ranges). Left alone these become N duplicate rows
    # in the top-level ``flows[]`` projection (the dup_flow_rate bug). Collapse
    # each group to ONE survivor (anchor-owner of the entry file, tie-broken by
    # confidence then feature name) and fold the other features into the
    # survivor's ``secondary_features`` — one primary edge + N secondary edges,
    # never N duplicate flows. Flows differing in entry-line OR line-ranges are
    # genuinely distinct and are preserved. Runs BEFORE id/uuid stamping, the
    # top-level projection, and Stage 6.7 UF rollup.
    dropped_cross = _collapse_cross_feature_duplicate_flows(features)
    # Secondary features the cross-feature collapse folded in, keyed by the
    # survivor Flow's identity, so Step 2's path-overlap pass can UNION them
    # in instead of clobbering them.
    folded_secondaries: dict[int, list[str]] = {
        id(fl): list(fl.secondary_features)
        for feat in features
        for fl in (getattr(feat, "flows", None) or [])
        if fl.secondary_features
    }

    # ── Step 1 — reverse-index paths to feature names ───────────────
    path_to_features = _build_path_to_features(features)

    # ── Step 2 — walk every flow once, populate per-flow fields ─────
    # Collect (Flow, primary_feature_name) pairs in stable iteration
    # order so the top-level ``flows[]`` projection is reproducible
    # across rescans.
    all_flows: list[tuple[Flow, str]] = []
    for feat in features:
        primary = feat.name
        for flow in feat.flows:
            # Primary attribution is the containing feature; this also
            # canonicalises Stage 3's pre-B1 absence of these fields.
            flow.primary_feature = primary
            flow.id = _flow_id(primary, flow.name)

            # Cross-cutting attachments derived from path ownership.
            secondaries: set[str] = set()
            for path in flow.paths or []:
                owners = path_to_features.get(path) or set()
                for owner in owners:
                    if owner != primary:
                        secondaries.add(owner)
            # UNION the feature names the Step-0.5 cross-feature collapse
            # folded in (the other features that owned a byte-identical copy
            # of this flow). They are real secondary attachments — preserve
            # them alongside the path-overlap signal.
            for owner in folded_secondaries.get(id(flow), ()):
                if owner != primary:
                    secondaries.add(owner)
            flow.secondary_features = sorted(secondaries)
            flow.shared_with_features_count = len(secondaries)
            flow.cross_cutting = bool(secondaries)

            all_flows.append((flow, primary))

    # ── Step 3 — pairwise "share at least one path" → counters ──────
    # The intuitive O(N^2) is fine for our scale (Layer 1 produces a
    # few hundred flows max); we keep it deterministic instead of
    # introducing a hash-based shortcut that would obscure the math.
    flow_path_sets: list[set[str]] = [
        set(flow.paths or []) for (flow, _) in all_flows
    ]
    for i in range(len(all_flows)):
        paths_i = flow_path_sets[i]
        if not paths_i:
            continue
        shared_count = 0
        for j in range(len(all_flows)):
            if i == j:
                continue
            if paths_i & flow_path_sets[j]:
                shared_count += 1
        all_flows[i][0].shared_with_flows_count = shared_count

    # ── Step 4 — emit edges ────────────────────────────────────────
    edges: list[FeatureFlowEdge] = []
    for flow, primary in all_flows:
        assert flow.id is not None  # set above
        # Primary edge — one per flow.
        edges.append(
            FeatureFlowEdge(
                feature=primary,
                flow_id=flow.id,
                type="primary",
                reason=None,
            ),
        )
        if log is not None:
            log.info(
                f"edge primary feature={primary} flow_id={flow.id}",
                feature=primary,
                flow_id=flow.id,
                edge_type="primary",
            )

        # Secondary edges — one per cross-cutting feature.
        for sec in flow.secondary_features:
            edges.append(
                FeatureFlowEdge(
                    feature=sec,
                    flow_id=flow.id,
                    type="secondary",
                    reason="path-overlap",
                ),
            )
            if log is not None:
                # ``reason`` is the first positional param of
                # StageLogger.info — pass the structured edge-reason
                # under a different key so it lands in ``**extra``.
                log.info(
                    f"edge secondary feature={sec} flow_id={flow.id} "
                    f"reason=path-overlap",
                    feature=sec,
                    flow_id=flow.id,
                    edge_type="secondary",
                    edge_reason="path-overlap",
                )

        if log is not None:
            log.info(
                f"blast-radius flow_id={flow.id} "
                f"shared_with_flows={flow.shared_with_flows_count} "
                f"shared_with_features={flow.shared_with_features_count}",
                feature=primary,
                flow_id=flow.id,
                shared_with_flows=flow.shared_with_flows_count,
                shared_with_features=flow.shared_with_features_count,
            )

    # ── Step 5 — top-level projection ──────────────────────────────
    # Stable order: by id. The Flow instances themselves are SHARED
    # with Feature.flows[] (we don't deep-copy) — they're the same
    # object, mutated in place. Pydantic serialises them identically
    # in both locations, which is what we want.
    top_level_flows: list[Flow] = sorted(
        (flow for (flow, _) in all_flows), key=lambda f: f.id or "",
    )

    # ── Telemetry ──────────────────────────────────────────────────
    edges_primary = sum(1 for e in edges if e.type == "primary")
    edges_secondary = sum(1 for e in edges if e.type == "secondary")
    cross_cutting_flows = sum(
        1 for (flow, _) in all_flows if flow.cross_cutting
    )
    max_shared_with_flows = max(
        (flow.shared_with_flows_count for (flow, _) in all_flows),
        default=0,
    )
    max_shared_with_features = max(
        (flow.shared_with_features_count for (flow, _) in all_flows),
        default=0,
    )

    telemetry: dict[str, int] = {
        "bipartite_edges_total": len(edges),
        "bipartite_edges_primary": edges_primary,
        "bipartite_edges_secondary": edges_secondary,
        "cross_cutting_flows_count": cross_cutting_flows,
        "flows_total": len(all_flows),
        "duplicate_flows_dropped": dropped_dupes + dropped_cross,
        "duplicate_flows_dropped_within_feature": dropped_dupes,
        "duplicate_flows_dropped_cross_feature": dropped_cross,
        "max_shared_with_flows": max_shared_with_flows,
        "max_shared_with_features": max_shared_with_features,
    }

    return Stage5_5Result(
        features=features,
        flows=top_level_flows,
        edges=edges,
        telemetry=telemetry,
    )


__all__ = ["stage_5_5_bipartite", "Stage5_5Result"]
