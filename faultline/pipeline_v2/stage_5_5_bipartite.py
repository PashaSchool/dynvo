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

    What keeps the collapse conservative AT THIS STAGE is the
    ``(entry_point_file, entry_point_line)`` pair — both are populated by
    Stage 3 and present here. Two flows sharing a name but beginning at a
    DIFFERENT file or line (e.g. FastAPI's many ``create-item-flow``
    tutorials, each in its own module; or ``create-account-flow`` at line 49
    vs line 61) produce different keys and BOTH survive. Only the hub-file
    fan-out — one physical entry (same file AND line) attributed to N
    features — collapses.

    NOTE: ``line_ranges`` is computed by the LOC expander in
    ``phase_finalize`` (a LATER stage), so at the Stage-5.5 call site it is
    empty for every flow and contributes nothing to the key — the operative
    discriminator is ``entry_point_file`` + ``entry_point_line``. It is kept
    in the key purely as forward-safety: should the pipeline ever populate
    line_ranges before this stage, the key stays at-least-as-conservative
    (never MORE collapse). Verified across 48 cold scans: no two flows share
    ``(name, entry_point_file, entry_point_line)`` yet differ in line_ranges,
    so the 3-tuple and 4-tuple are equivalent in practice today.
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


# ── Flow-name disambiguation (deterministic naming-collision kill) ─────────
#
# Stage 3 names every flow independently per ``<kebab-verb-phrase>-flow``
# (LLM or the deterministic profile slugger) with NO cross-flow uniqueness.
# So GENUINELY-DISTINCT flows — different ``entry_point_file``/line, different
# owning feature — routinely land on the SAME generic label
# (``search-cases-flow`` emitted once from the API search route, once from the
# cases page, once from the cases router). The byte-identical collapse above
# (Steps 0 / 0.5) correctly leaves these alone — they are NOT the same flow —
# so they survive as duplicate *names* in the top-level ``flows[]`` projection
# and dominate ``dup_flow_rate`` (≈1100 naming-collision groups corpus-wide vs
# ≈90 byte-identical).
#
# This pass DISAMBIGUATES (never merges — merging by path-set is corpus-unsafe,
# see ``finding-pathset-merge-refuted``) every genuine collision by inserting
# the flow's distinguishing context BEFORE the ``-flow`` suffix, preserving
# ``rule-flow-naming`` (kebab-case, STARTS with a verb, ENDS in ``-flow``):
#
#   search-cases-flow  ──▶  search-cases-<ctx>-flow
#
# Context source, in deterministic priority:
#   1. ``flow.primary_feature`` — the owning developer-feature (most
#      meaningful; populated by Stage 2/2.6, distinct per colliding flow
#      across all 6 validation repos).
#   2. the entry-point domain — the parent directory of ``entry_point_file``
#      (else its basename stem) — when no primary feature is attributed.
#   3. a minimal stable ordinal — only when two colliding flows STILL share a
#      name after context (same feature + same name, near-identical flows);
#      ordered by ``(entry_point_line, id)`` so it is reproducible across
#      rescans. Last resort, guarantees global uniqueness.
#
# Purely structural: a collision is "this name is carried by >1 distinct Flow
# object" — no magic number, no repo-specific path, scale-invariant.

# Generic verb fillers a context slug may legitimately carry; dropping them
# keeps the disambiguator focused on the DISTINGUISHING noun rather than
# re-stating the journey's own verb. Universal vocabulary, not repo-tuned.
_CTX_STOP_TOKENS = frozenset({
    "flow", "flows", "api", "page", "pages", "route", "routes", "router",
    "handler", "handlers", "view", "views", "endpoint", "endpoints",
})


def _name_key(name: str) -> str:
    """Collision key — matches ``cold_eval._dup_rate`` (lower + strip)."""
    return (name or "").strip().lower()


def _strip_flow_suffix(name: str) -> str:
    """Drop a trailing ``-flow`` / ``-flows`` so context can be inserted
    BEFORE it (keeping the verb-led head intact)."""
    return re.sub(r"-flows?$", "", name)


def _context_tokens(flow: Flow, primary: str | None) -> list[str]:
    """Distinguishing context tokens for ``flow``, in source priority.

    Returns kebab tokens (already slugified, deduped, stop-tokens dropped).
    Empty when the flow carries neither a primary feature nor an entry file —
    the caller then falls through to the ordinal tier.
    """
    raw = ""
    src = primary or flow.primary_feature
    if src:
        raw = src
    else:
        epf = flow.entry_point_file or ""
        if epf:
            parts = [p for p in epf.replace("\\", "/").split("/") if p]
            if len(parts) >= 2:
                raw = parts[-2]                      # parent directory
            elif parts:
                raw = parts[-1].rsplit(".", 1)[0]    # basename stem
    slug = _slugify(raw)
    if not slug:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in slug.split("-"):
        if not tok or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _disambiguated_name(
    base_no_suffix: str, ctx_tokens: list[str], *, trim: bool,
) -> str:
    """Build ``<base>-<ctx>-flow`` from a verb-led base + context tokens.

    ``trim=True`` produces the SHORT variant: context tokens already present
    in the base (so ``search-cases`` + ctx ``cases-page`` → ``search-cases-page``
    not ``search-cases-cases-page``) and generic fillers (``api``/``page``/
    ``route``…) are dropped. ``trim=False`` produces the FULL variant: only
    exact base-token repeats are dropped (avoids ``x-x``) — used when the
    short variant would not be unique within the collision group, so two
    distinct contexts that share a filler token (``api-cases`` vs ``cases-page``)
    still get distinct names (``…-api-cases`` vs ``…-cases-page``) instead of
    being forced to an ordinal. The verb-led head is never touched.
    """
    base_tokens = {t for t in base_no_suffix.split("-") if t}
    if trim:
        kept = [
            t for t in ctx_tokens
            if t not in base_tokens and t not in _CTX_STOP_TOKENS
        ]
    else:
        kept = [t for t in ctx_tokens if t not in base_tokens]
    if not kept:
        # Guard emptied the context (ctx fully contained in / filtered out of
        # the base). Keep the verbatim context so the name still changes.
        kept = ctx_tokens
    suffix = "-".join(kept)
    return f"{base_no_suffix}-{suffix}-flow" if suffix else f"{base_no_suffix}-flow"


def _disambiguate_colliding_flow_names(features: list[Feature]) -> dict[str, int]:
    """Rename genuinely-distinct flows that share a generic name, IN PLACE.

    Operates on the post-collapse feature list (Steps 0 / 0.5 already removed
    byte-identical duplicates, so every remaining same-named pair is a TRUE
    naming collision between distinct flows). Two deterministic passes:

      Pass A — for every name carried by >1 distinct Flow, insert each flow's
      context (:func:`_context_tokens`) before ``-flow``
      (:func:`_disambiguated_name`). Per group we first try the SHORT context
      variant (fillers/redundant tokens trimmed); if that would not be unique
      WITHIN the group, we retry that group with the FULL context variant so
      two distinct contexts sharing a filler token still get distinct names
      instead of being forced to an ordinal. Flows whose name is unique are
      untouched (byte-identical output).

      Pass B — recompute the global name multiset; any name STILL shared by
      >1 flow (same feature + same name, or a context-insertion that happened
      to re-collide with a pre-existing name) gets a minimal stable ordinal
      (``-2``, ``-3``, …) inserted before ``-flow``, ordered by
      ``(entry_point_line, id, original index)``. The first occurrence keeps
      the no-ordinal form. This guarantees ``flows[]`` names are unique.

    The Flow objects are SHARED between ``Feature.flows`` and the top-level
    projection built later in this stage, so mutating ``flow.name`` here once
    updates both views; ``flow.id`` is (re)stamped from the new name in Step 2,
    and the later lineage uuid + expander ``short_label``/``display_name`` all
    derive from it. Idempotent: a second run finds no collisions and is a
    no-op.

    Returns telemetry (collision groups, flows renamed by context, flows
    renamed by ordinal).
    """
    flows: list[Flow] = [
        fl for feat in features for fl in (getattr(feat, "flows", None) or [])
    ]
    if not flows:
        return {
            "naming_collision_groups": 0,
            "flows_disambiguated_by_context": 0,
            "flows_disambiguated_by_ordinal": 0,
        }

    # ── Pass A — context insertion on genuine collisions ────────────────
    groups: dict[str, list[Flow]] = {}
    for fl in flows:
        groups.setdefault(_name_key(fl.name), []).append(fl)
    collision_groups = {k: g for k, g in groups.items() if len(g) > 1}

    by_context = 0
    for members in collision_groups.values():
        # Compute each member's context once; flows with no context (no
        # primary feature AND no entry file) stay on their original name and
        # drop to the ordinal tier in Pass B.
        ctxs: list[tuple[Flow, list[str], str]] = []
        for fl in members:
            ctx = _context_tokens(fl, fl.primary_feature)
            if ctx:
                ctxs.append((fl, ctx, _strip_flow_suffix(fl.name)))
        if not ctxs:
            continue
        # Prefer the SHORT variant; if it is NOT unique within the group, fall
        # through to the FULL variant (distinct primary features usually differ
        # in more than a filler token, so it separates them). Whichever variant
        # is first fully-unique within the group is used; if neither is (e.g.
        # identical primary feature), the FULL variant is applied and the
        # residual dups are handed to the ordinal tier in Pass B.
        cand: dict[int, str] = {}
        for trim in (True, False):
            cand = {
                id(fl): _disambiguated_name(base, ctx, trim=trim)
                for fl, ctx, base in ctxs
            }
            keys = [_name_key(v) for v in cand.values()]
            if len(set(keys)) == len(keys):
                break  # this variant is unique within the group — use it
        for fl, _ctx, _base in ctxs:
            new = cand[id(fl)]
            if _name_key(new) != _name_key(fl.name):
                fl.name = new
                by_context += 1

    # ── Pass B — ordinal fallback → guaranteed-unique names ─────────────
    # Recompute the multiset over the (now mostly-disambiguated) names.
    counts: dict[str, int] = {}
    for fl in flows:
        k = _name_key(fl.name)
        counts[k] = counts.get(k, 0) + 1
    residual = {k for k, c in counts.items() if c > 1}

    by_ordinal = 0
    if residual:
        # Deterministic ordering inside each residual group so the survivor
        # (no ordinal) and the numbering are reproducible across rescans.
        order_index = {id(fl): i for i, fl in enumerate(flows)}
        residual_members: dict[str, list[Flow]] = {}
        for fl in flows:
            k = _name_key(fl.name)
            if k in residual:
                residual_members.setdefault(k, []).append(fl)
        # Names already taken (unique ones) so an ordinal never re-collides.
        taken = {k for k, c in counts.items() if c == 1}
        for _key, members in residual_members.items():
            members.sort(
                key=lambda f: (
                    f.entry_point_line if f.entry_point_line is not None else -1,
                    f.id or "",
                    order_index[id(f)],
                ),
            )
            base = _strip_flow_suffix(members[0].name)
            taken.add(_name_key(members[0].name))  # first keeps the bare form
            ordinal = 2
            for fl in members[1:]:
                cand = f"{base}-{ordinal}-flow"
                while _name_key(cand) in taken:
                    ordinal += 1
                    cand = f"{base}-{ordinal}-flow"
                fl.name = cand
                taken.add(_name_key(cand))
                by_ordinal += 1
                ordinal += 1

    return {
        "naming_collision_groups": len(collision_groups),
        "flows_disambiguated_by_context": by_context,
        "flows_disambiguated_by_ordinal": by_ordinal,
    }


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

    # ── Step 0.6 — disambiguate naming collisions (deterministic) ────
    # The byte-identical collapses above remove flows that are the SAME flow.
    # What remains can still share a generic NAME across DISTINCT flows
    # (different entry point / owning feature) because Stage 3 names each flow
    # independently with no cross-flow uniqueness — the dominant dup_flow_rate
    # residual. Rename (never merge) each genuine collision by inserting its
    # primary feature (else entry-point domain, else a stable ordinal) BEFORE
    # the ``-flow`` suffix, preserving rule-flow-naming. Runs BEFORE id stamping
    # (Step 2) so flow.id — and the later lineage uuid + expander short_label /
    # display_name — all derive from the disambiguated name. The flow COUNT is
    # unchanged; only colliding names change; unique names are byte-untouched.
    disambig = _disambiguate_colliding_flow_names(features)

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
        # Step 0.6 naming-collision disambiguation (rename, never merge).
        "naming_collision_groups": disambig["naming_collision_groups"],
        "flows_disambiguated_by_context": disambig["flows_disambiguated_by_context"],
        "flows_disambiguated_by_ordinal": disambig["flows_disambiguated_by_ordinal"],
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
