"""Emission integrity — the single output-layer pass that guarantees the
scan JSON is referentially self-consistent before Stage 7 writes it.

Three classes of referential / phantom defect were measured by the
board validator (I1-I14) across corpus scans; all three are *emission*
bugs — the data is right, the cross-references between arrays are not:

  I12  dangling ``user_flows[].product_feature_id`` — a UF references a
       product-feature key that no ``product_features[]`` row carries.
       Root cause: capability names were slugged by **different**
       normalizers at different emission points (6.7d's collapsing
       regex ``_slug`` vs the analyst / nav ``.replace(" ","-")`` which
       does NOT collapse ``_`` / multi-space / tabs / unicode), so a
       name with a special char produced two divergent keys.

  I14  stale ``flows[].user_flow_id`` backpointers — Stage 6.7 stamps
       each flow's owning-UF id, but Stage 6.7d REWRITES ``user_flows[]``
       with fresh ids and never re-stamps the backpointers, leaving them
       pointing at pre-abstraction UF ids.

  I2   phantom feature — a developer or product feature that carries 0
       owned loc, 0 shared loc and 0 flows, whether its paths are
       structural root markers (``.`` / ``""`` / ``..``) OR real
       (non-marker) files/paths that Stage 6.97's counting rules
       resolved to zero countable lines (RC2-4 extension, 2026-07-06:
       a comma-typo'd / genuinely-empty / fully-excluded file is
       content-less exactly like a root marker — "onyx
       connector-module-init" carried one real path,
       ``freshdesk/__init__,py`` — a typo'd, 0-byte file in the onyx
       repo itself — and owned zero flows). A row with no real code
       and no journeys is a phantom and must be dropped (only the
       shared-platform BUCKET ROW itself is exempt — it is honestly
       shared; see the emission-integrity bucket-immunity narrowing
       below, workspace-anchor markers do NOT save a content-less row).

The fix is twofold:

  1. **One slug function** — :func:`canonical_slug` is the single
     capability/PF-name normalizer. Every emission point that slugs a
     capability name delegates to it (6.7d ``_slug``, ``nav_taxonomy``
     and ``stage_8_analyst`` ``_slugify``). It is byte-identical to the
     historical 6.7d regex for single-spaced ASCII labels (so digests
     stay stable for the common case) and only differs — deterministically
     and consistently on BOTH sides of a reference — for the special-char
     labels that previously diverged.

  2. **A round-trip guarantee** — :func:`enforce_emission_integrity`
     runs LAST, after every UF / PF / flow mutation, and enforces the
     three invariants directly on the final arrays. Even if a future
     stage introduces a fresh divergence, the emitted JSON is repaired
     (and the repair is recorded in ``scan_meta.emission_integrity``).

Deterministic, $0 LLM, output-layer only. Never mutates the central
flow *graph* (node spans, entry points) — only the cross-reference
scalar fields (``flow.user_flow_id``, ``uf.product_feature_id``) and
the membership of phantom rows.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import Feature, Flow, UserFlow


# ── The single normalizer ────────────────────────────────────────────────

_SEP_RE = re.compile(r"[\s_/]+")
_DASH_RE = re.compile(r"-+")


def canonical_slug(name: str | None) -> str:
    """The ONE capability / product-feature name normalizer.

    Rules (in order):

      * Unicode is folded to ASCII via NFKD (``é`` → ``e``); characters
        with no ASCII form are dropped. This is the IDENTITY transform
        for pure-ASCII input, so digests on the (overwhelmingly common)
        ASCII path are unchanged.
      * Lower-cased, outer whitespace stripped.
      * Every run of whitespace / underscore / slash collapses to a
        single ``-``.
      * Runs of ``-`` collapse to one; leading/trailing ``-`` stripped.

    Punctuation that historically survived (``&``, ``(``, ``)``, ``,``,
    ``.``) is DELIBERATELY preserved so the value is byte-identical to
    the legacy 6.7d ``_slug`` for ASCII labels — the fix is that BOTH
    sides of every reference now run through THIS function, so a name
    like ``"Poll Editing & Management"`` yields the SAME key wherever it
    is slugged.
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = _SEP_RE.sub("-", s.strip().lower())
    return _DASH_RE.sub("-", s).strip("-")


# ── Structural-marker / anchor helpers (mirror the loc-truth stage) ───────

_ROOT_MARKERS = frozenset(("", ".", ".."))
_SHARED_PF_KEYS = frozenset(("shared-platform", "platform"))
_WORKSPACE_ANCHOR_MARKER = "workspace anchor"


def _is_root_marker(path: str) -> bool:
    """True for a repo-root / whole-repo structural marker path."""
    return str(path or "").strip().strip("/").strip() in _ROOT_MARKERS


def _has_real_path(feat: "Feature") -> bool:
    """True when the feature owns at least one non-marker path."""
    return any(not _is_root_marker(p) for p in (getattr(feat, "paths", None) or []))


def _is_anchor_or_platform(feat: "Feature") -> bool:
    """Guard: workspace anchors and the shared-platform bucket are honest
    residents even with only structural paths — never dropped as phantom."""
    if _WORKSPACE_ANCHOR_MARKER in (getattr(feat, "description", None) or "").lower():
        return True
    name = canonical_slug(getattr(feat, "name", None))
    pfid = getattr(feat, "product_feature_id", None) or ""
    return name in _SHARED_PF_KEYS or pfid in _SHARED_PF_KEYS


def _feature_zero_loc(feat: "Feature") -> bool:
    """True when a feature has no owned loc, no shared loc and no flows —
    i.e. contributes zero code by every accounting channel."""
    if getattr(feat, "loc", None) or getattr(feat, "loc_shared", None):
        return False
    if getattr(feat, "flows", None):
        return False
    return True


def _is_platform_bucket(feat: "Feature") -> bool:
    """Only the shared/platform bucket row ITSELF is immune to phantom
    drop — residents assigned to it (product_feature_id) are not; a
    content-less resident of the bucket is still a phantom (Soc0 'ai',
    pf_id=shared-platform, sole path '.')."""
    name = canonical_slug(getattr(feat, "name", None))
    return name in _SHARED_PF_KEYS


def _is_phantom(feat: "Feature") -> bool:
    """A phantom carries zero owned loc, zero shared loc and zero flows —
    REGARDLESS of whether its paths are structural root markers (``.``)
    or real (non-marker) paths that simply never resolved to any
    countable code (RC2-4, 2026-07-06: a genuinely-empty / typo'd /
    fully test-or-generated-excluded file leaves Stage 6.97's per-file
    map empty for that feature, so ``loc``/``loc_shared`` land on 0 the
    same as a marker-only row — "фіча без коду" either way). A feature
    with ANY counted file is rescued at Stage 6.97 (the largest file's
    loc floors ``owned`` — see ``stage_6_97_feature_loc.apply_feature_loc``)
    so this predicate only ever fires for the genuinely content-less
    case. The workspace-anchor marker does NOT save a content-less row
    (Soc0 'ai': description carries the anchor marker yet its only path
    is the repo-root '.') — only the platform bucket itself is immune."""
    return _feature_zero_loc(feat) and not _is_platform_bucket(feat)


def _pf_key(pf: "Feature") -> str:
    """The referential key a UF's ``product_feature_id`` must match — the
    SAME rule the board validator uses (``pf.id or pf.name``)."""
    return str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")


# ── Result / telemetry ────────────────────────────────────────────────────


@dataclass
class EmissionIntegrityResult:
    """Per-scan outcome, surfaced in ``scan_meta.emission_integrity``."""

    phantom_features_dropped: list[str] = field(default_factory=list)
    phantom_product_features_dropped: list[str] = field(default_factory=list)
    uf_pf_refs_relinked: int = 0
    uf_pf_refs_nulled: int = 0
    flow_backpointers_rewritten: int = 0
    flow_backpointers_nulled: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "phantom_features_dropped": list(self.phantom_features_dropped),
            "phantom_product_features_dropped": list(
                self.phantom_product_features_dropped
            ),
            "uf_pf_refs_relinked": self.uf_pf_refs_relinked,
            "uf_pf_refs_nulled": self.uf_pf_refs_nulled,
            "flow_backpointers_rewritten": self.flow_backpointers_rewritten,
            "flow_backpointers_nulled": self.flow_backpointers_nulled,
        }


# ── The three passes ──────────────────────────────────────────────────────


def _drop_phantoms(
    features: list["Feature"],
    product_features: list["Feature"],
    result: EmissionIntegrityResult,
) -> tuple[list["Feature"], list["Feature"]]:
    """Pass 1 — drop marker-only / 0-loc / 0-flow phantom rows (dev + PF),
    then drop any product feature that lost its last surviving member."""
    kept_dev: list["Feature"] = []
    for f in features:
        if _is_phantom(f):
            result.phantom_features_dropped.append(
                str(getattr(f, "display_name", None) or getattr(f, "name", "?"))
            )
        else:
            kept_dev.append(f)

    kept_pf: list["Feature"] = []
    for pf in product_features:
        if _is_phantom(pf):
            result.phantom_product_features_dropped.append(
                str(getattr(pf, "display_name", None) or getattr(pf, "name", "?"))
            )
        else:
            kept_pf.append(pf)

    # A PF whose only members were just-dropped dev phantoms (or which had
    # no members to begin with) is itself content-less — remove it so I12's
    # PF key-set is honest. Membership: dev.product_feature_id == pf key.
    live_member_keys = {
        getattr(f, "product_feature_id", None)
        for f in kept_dev
        if getattr(f, "product_feature_id", None)
    }
    survivors: list["Feature"] = []
    for pf in kept_pf:
        key = _pf_key(pf)
        # Keep PFs that still have a member, OR that carry real code/flows of
        # their own (aggregated rollup), OR the protected platform bucket.
        if (
            key in live_member_keys
            or _has_real_path(pf)
            or getattr(pf, "flows", None)
            or _is_anchor_or_platform(pf)
        ):
            survivors.append(pf)
        else:
            result.phantom_product_features_dropped.append(
                str(getattr(pf, "display_name", None) or getattr(pf, "name", "?"))
            )
    return kept_dev, survivors


def _reconcile_uf_pf_refs(
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    result: EmissionIntegrityResult,
) -> None:
    """Pass 2 — I12 round-trip guarantee: every emitted
    ``uf.product_feature_id`` must be an emitted PF key. When a UF's ref
    is not a direct key, try a canonical re-slug match against the PF
    key-set; if that also fails, null the dangling ref."""
    pf_keys = {_pf_key(pf) for pf in product_features if _pf_key(pf)}
    # Canonical-key → actual-emitted-key, so a divergent-but-equivalent ref
    # can be relinked to the real PF key rather than nulled.
    canon_to_key: dict[str, str] = {}
    for key in pf_keys:
        canon_to_key.setdefault(canonical_slug(key), key)

    for uf in user_flows:
        ref = getattr(uf, "product_feature_id", None)
        if not ref or ref in pf_keys:
            continue
        relinked = canon_to_key.get(canonical_slug(ref))
        if relinked is not None:
            uf.product_feature_id = relinked
            result.uf_pf_refs_relinked += 1
        else:
            uf.product_feature_id = None
            result.uf_pf_refs_nulled += 1


def _flow_key(flow: "Flow") -> str:
    """Stable flow identity — uuid when present, else name. Mirrors
    ``stage_6_7_user_flows._flow_key`` and the ``user_flow_id`` stamp."""
    return str(getattr(flow, "uuid", None) or getattr(flow, "name", "") or "")


def _rewrite_flow_backpointers(
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    result: EmissionIntegrityResult,
) -> None:
    """Pass 3 — I14: re-derive ``flow.user_flow_id`` from the FINAL
    ``user_flows[].member_flow_ids`` ownership.

    Primary rule for a flow claimed by several UFs: the FIRST user flow
    (in emitted ``user_flows`` order) whose ``member_flow_ids`` lists the
    flow wins — deterministic and independent of dict iteration order.
    A flow owned by no surviving UF gets ``user_flow_id = None``.
    """
    owner: dict[str, str] = {}
    for uf in user_flows:
        uf_id = getattr(uf, "id", None)
        if not uf_id:
            continue
        for mid in getattr(uf, "member_flow_ids", None) or []:
            owner.setdefault(mid, uf_id)

    for f in flows:
        new_id = owner.get(_flow_key(f))
        old_id = getattr(f, "user_flow_id", None)
        if new_id == old_id:
            continue
        f.user_flow_id = new_id
        if new_id is None:
            result.flow_backpointers_nulled += 1
        else:
            result.flow_backpointers_rewritten += 1


def enforce_emission_integrity(
    features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    flows: list["Flow"],
) -> tuple[list["Feature"], list["Feature"], EmissionIntegrityResult]:
    """Run the three emission-integrity passes and return the repaired
    ``(features, product_features)`` lists plus a telemetry result.

    ``user_flows`` and ``flows`` are mutated in place (scalar
    cross-reference fields only). The feature lists are returned because
    phantom rows are removed — callers must rebind their references.

    Order matters: phantom drop first (may remove a PF), so I12 reconciles
    against the SURVIVING PF key-set; backpointer rewrite last, over the
    final UF membership.
    """
    result = EmissionIntegrityResult()
    features, product_features = _drop_phantoms(features, product_features, result)
    _reconcile_uf_pf_refs(product_features, user_flows, result)
    _rewrite_flow_backpointers(user_flows, flows, result)
    return features, product_features, result


__all__ = [
    "canonical_slug",
    "enforce_emission_integrity",
    "EmissionIntegrityResult",
]
