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
from faultline.pipeline_v2.overturn_ledger import propose_pf_now

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import Feature, Flow, UserFlow

#: W4.2 anchored-husk emission rule (operator exhibit: typebot ``Popup``).
#: Default ON; ``FAULTLINE_ANCHORED_HUSK_DROP=0`` disables.
ANCHORED_HUSK_ENV = "FAULTLINE_ANCHORED_HUSK_DROP"

#: The shared_reason stamped on a husk's unbound shell devs — consumed by
#: ``build_platform_infrastructure_lane`` (added to its accepted set) and
#: validator I22 (any non-empty machine-readable reason).
ANCHORED_HUSK_REASON = "anchored_husk_shell"


def anchored_husk_drop_enabled() -> bool:
    """Default ON; ``FAULTLINE_ANCHORED_HUSK_DROP=0`` off."""
    return os.environ.get(ANCHORED_HUSK_ENV, "1").strip().lower() not in {
        "0", "false",
    }


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
    i.e. contributes zero code by every accounting channel.

    B59 (2026-07-13): ``artifact_ink_loc`` is such a channel too. A feature
    whose owned lines were RECLASSIFIED as artifact ink (locale catalogs,
    generated schemas, test data, seeds — twenty's ``locales`` dev is 10,017
    LOC of pure ``.po``) is NOT content-less: the lines exist and its
    members/coordinates must survive (accounting drains display LOC, never
    membership — dropping it here cost 65 ``path_index`` entries on the
    first B59 gate race). Flag-OFF scans always carry ``None`` here, so the
    predicate is byte-identical to the pre-B59 engine when the lane is off."""
    if getattr(feat, "loc", None) or getattr(feat, "loc_shared", None):
        return False
    if getattr(feat, "artifact_ink_loc", None):
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
    # Product-Spine §4.1 — bare-dir sweep (root-marker paths / member_files
    # entries removed at emission). 0 on a clean spine.
    bare_dir_paths_dropped: int = 0
    # W4.2 anchored-husk rule (operator exhibit: typebot Popup) — anchored
    # PF rows whose ownership evidence is entirely secondary. 0 on a clean
    # spine.
    anchored_husk_pfs_dropped: list[str] = field(default_factory=list)
    anchored_husk_devs_unbound: int = 0
    anchored_husk_devs_rebound: int = 0
    anchored_husk_ufs_rehomed: int = 0
    anchored_husk_seed_ufs_dropped: int = 0
    anchored_husks_kept_journey: list[str] = field(default_factory=list)
    # B52 — stale transport-lane back-references repaired (lane_ref not
    # matching any pfid=None dev uuid). 0 on every pre-B52 / flag-OFF
    # scan; the key is EMITTED ONLY when non-zero (byte-identity).
    lane_refs_cleared: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = self._base_dict()
        if self.lane_refs_cleared:
            out["lane_refs_cleared"] = self.lane_refs_cleared
        return out

    def _base_dict(self) -> dict[str, Any]:
        return {
            "phantom_features_dropped": list(self.phantom_features_dropped),
            "phantom_product_features_dropped": list(
                self.phantom_product_features_dropped
            ),
            "uf_pf_refs_relinked": self.uf_pf_refs_relinked,
            "uf_pf_refs_nulled": self.uf_pf_refs_nulled,
            "flow_backpointers_rewritten": self.flow_backpointers_rewritten,
            "flow_backpointers_nulled": self.flow_backpointers_nulled,
            "bare_dir_paths_dropped": self.bare_dir_paths_dropped,
            "anchored_husk_pfs_dropped": list(self.anchored_husk_pfs_dropped),
            "anchored_husk_devs_unbound": self.anchored_husk_devs_unbound,
            "anchored_husk_devs_rebound": self.anchored_husk_devs_rebound,
            "anchored_husk_ufs_rehomed": self.anchored_husk_ufs_rehomed,
            "anchored_husk_seed_ufs_dropped": (
                self.anchored_husk_seed_ufs_dropped
            ),
            "anchored_husks_kept_journey": list(
                self.anchored_husks_kept_journey
            ),
        }


# ── The three passes ──────────────────────────────────────────────────────


def _flow_home_index(
    features: list["Feature"],
    flows: list["Flow"],
) -> dict[str, str]:
    """flow key → home PF key. Two channels, dev-flow ownership first
    (the same "owning dev's capability" notion the 6.7d backstop's
    ``flow_pf`` uses), then the flow's ``primary_feature`` dev name."""
    dev_pf: dict[str, str] = {}
    home: dict[str, str] = {}
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        name = str(getattr(f, "name", "") or "")
        if not pfid:
            continue
        if name:
            dev_pf.setdefault(name, str(pfid))
        for fl in getattr(f, "flows", None) or []:
            k = _flow_key(fl)
            if k:
                home.setdefault(k, str(pfid))
    for fl in flows:
        k = _flow_key(fl)
        if not k or k in home:
            continue
        primary = str(getattr(fl, "primary_feature", "") or "")
        if primary and primary in dev_pf:
            home[k] = dev_pf[primary]
    return home


def _drop_anchored_husks(
    features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    result: EmissionIntegrityResult,
) -> list["Feature"]:
    """Pass 1a — W4.2 anchored-husk rule (operator exhibits: typebot
    ``Popup`` fdir 13 files / 0 flows / 0 owned LOC; Soc0
    ``module-store-page`` / ``private-connectors-page`` /
    ``start-investigation-page`` route husks; documenso
    ``sign.$token+``).

    An ANCHORED product feature whose ownership evidence is entirely
    secondary — zero owned LOC and zero on-flow LOC on the row AND on
    every bound dev, zero flows on the row — is an emission shell: every
    line its files carry is owned (and surfaced) by OTHER features, so
    the row duplicates their story under a name no code backs. Such rows
    survive the phantom pass only through their ``loc_shared`` view (the
    Popup class: 1,185 shared LOC, nothing owned). A bound dev may still
    CARRY a flow (each Soc0 route husk holds one real page journey) —
    what makes the row a husk is that the flow contributes no owned
    lines; the journey is real, the ROW is not. The rule:

      * the PF row is dropped (its anchor remains a legal sub-anchor /
        naming candidate on later scans — nothing is blacklisted);
      * journeys that referenced the husk re-home: first by the
        plurality HOME capability of their member flows (the flow's
        owning dev's PF — the I16 ruler), else by the plurality PRIMARY
        OWNER of the husk's member files (files follow their true
        owners: the Soc0 husks' 16/17 files are integration-feature
        code, so the journey follows the code). A SYNTHESIZED journey
        with no derivable home is dropped with the husk (a seed pointing
        at a husk is a fake row); a REAL journey with no derivable home
        KEEPS the husk alive (``anchored_husks_kept_journey`` — never
        orphan a real journey);
      * FLOWLESS shell devs unbind to the platform-infrastructure lane
        with ``shared_reason="anchored_husk_shell"`` — zero-loss,
        I22-visible; a FLOWFUL shell dev instead REBINDS to the husk's
        landing target (the lane law: a dev with ≥1 flow never lanes) —
        no target derivable ⇒ the husk stays.

    Platform bucket exempt as everywhere. Kill-switch
    ``FAULTLINE_ANCHORED_HUSK_DROP=0``.
    """
    if not anchored_husk_drop_enabled():
        return product_features

    devs_by_pf: dict[str, list["Feature"]] = {}
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        if pfid:
            devs_by_pf.setdefault(str(pfid), []).append(f)

    def _zero_owned(feat: "Feature") -> bool:
        """No owned lines anywhere: ``loc`` and ``loc_flow`` both falsy.
        Flows are NOT checked at dev grain — a husk's dev may hold a
        real journey whose every line lives on foreign-owned files."""
        return not (getattr(feat, "loc", None)
                    or getattr(feat, "loc_flow", None))

    # B53 v5 — the husk test judges ORGANIC mass at FILE grain. The
    # Stage-6.885b drain moves member files at the dev level, so a
    # foldproof husk (typebot Popup: the OFF board lanes it via THIS
    # rule, loc=0) could come back flag-ON wearing drain-contributed LOC.
    # A carve-dev-identity subtraction (v4) is NOT enough: on the real
    # board the drained files' 6.97 ``_primary`` went to an ORGANIC
    # sibling dev of the target (typebot dev 'popup': 12 paths beat the
    # carve's 2 on dircount ⇒ organic dev loc=203, carve loc=0) — drain
    # mass wearing an organic dev's clothes. The provenance therefore
    # lives at the FILE, not the dev: ``drain_paths`` = the union of
    # carve-dev path lists (structured ``fold:b53_domain_drain->`` anchor
    # marker), and a row/dev is ORGANICALLY zero-owned iff its owned loc
    # is fully explained by the drained files it lists (per-file ``loc``
    # stamps are provenance-level — the same count on every claimant —
    # so the credit is owner-independent). loc_flow prong semantics
    # unchanged. A husk verdict then runs the UNCHANGED machinery below —
    # flowless shell devs (organic AND carve alike) unbind to the
    # platform lane carrying the drained files (zero-loss, I22-visible;
    # the lane row's LOC vs the OFF board's 0 is the accepted honest
    # delta). Boards with no drain marks: ``drain_paths`` is empty, the
    # credit is 0, and the test reduces to ``_zero_owned`` verbatim
    # (flag-OFF ⇒ byte-identical trivially).
    from faultline.pipeline_v2.ws_blob_domain_drain import is_drain_carve_dev

    drain_paths: set[str] = set()
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        if is_drain_carve_dev(f):
            drain_paths.update(
                str(p) for p in (getattr(f, "paths", None) or []) if p)

    if drain_paths:
        # path → provenance-level per-file loc (6.97 member stamps; the
        # SAME value on every claimant, so any feature's stamp serves).
        drain_file_loc: dict[str, int] = {}
        for f in list(features) + list(product_features):
            for mf in (getattr(f, "member_files", None) or []):
                p = (mf.get("path") if isinstance(mf, dict)
                     else getattr(mf, "path", None))
                if p is None or str(p) not in drain_paths:
                    continue
                floc = (mf.get("loc") if isinstance(mf, dict)
                        else getattr(mf, "loc", None))
                if floc:
                    drain_file_loc.setdefault(str(p), int(floc))

        def _organic_zero(feat: "Feature") -> bool:
            """ORGANIC zero-owned: no owned-loc-contributing file outside
            ``drain_paths`` — owned loc ≤ the drained-file credit of the
            paths the feature lists (owner-independent)."""
            if getattr(feat, "loc_flow", None):
                return False
            credit = sum(
                drain_file_loc.get(str(p), 0)
                for p in (getattr(feat, "paths", None) or [])
                if str(p) in drain_paths
            )
            return (getattr(feat, "loc", None) or 0) <= credit
    else:
        _organic_zero = _zero_owned

    candidates: list["Feature"] = []
    for pf in product_features:
        if _is_platform_bucket(pf) or not getattr(pf, "anchor_id", None):
            continue
        if not _organic_zero(pf) or (getattr(pf, "flows", None) or []):
            continue
        if any(not _organic_zero(d)
               for d in devs_by_pf.get(_pf_key(pf), [])):
            continue
        candidates.append(pf)
    if not candidates:
        return product_features

    home_of = _flow_home_index(features, flows)
    husk_keys = {_pf_key(pf) for pf in candidates}

    # file → primary-owner PF (first dev in features[] order primary-
    # owning the path — the path_index convention), husks excluded.
    file_owner_pf: dict[str, str] = {}
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        if not pfid or str(pfid) in husk_keys:
            continue
        for p in (getattr(f, "paths", None) or []):
            file_owner_pf.setdefault(str(p), str(pfid))

    def _member_paths(pf: "Feature") -> list[str]:
        out: list[str] = []
        for mf in (getattr(pf, "member_files", None) or []):
            p = (mf.get("path") if isinstance(mf, dict)
                 else getattr(mf, "path", None))
            if p:
                out.append(str(p))
        return out or [str(p) for p in (getattr(pf, "paths", None) or [])]

    def _top(votes: Counter[str]) -> str | None:
        if not votes:
            return None
        (_key, n), = votes.most_common(1)
        return sorted(k for k, v in votes.items() if v == n)[0]

    ufs_by_pf: dict[str, list["UserFlow"]] = {}
    for uf in user_flows:
        ref = str(getattr(uf, "product_feature_id", None) or "")
        if ref in husk_keys:
            ufs_by_pf.setdefault(ref, []).append(uf)

    dropped_keys: set[str] = set()
    uf_moves: list[tuple["UserFlow", str]] = []
    uf_drops: list["UserFlow"] = []
    for pf in sorted(candidates, key=lambda p: _pf_key(p)):
        key = _pf_key(pf)
        owner_votes: Counter[str] = Counter()
        for p in _member_paths(pf):
            owner = file_owner_pf.get(p)
            if owner:
                owner_votes[owner] += 1
        fallback = _top(owner_votes)
        moves: list[tuple["UserFlow", str]] = []
        drops: list["UserFlow"] = []
        blocked = False
        for uf in ufs_by_pf.get(key, []):
            votes: Counter[str] = Counter()
            for mid in getattr(uf, "member_flow_ids", None) or []:
                target = home_of.get(str(mid))
                if target and target not in husk_keys:
                    votes[target] += 1
            target = _top(votes) or fallback
            if target:
                moves.append((uf, target))
            elif getattr(uf, "synthesized", None):
                drops.append(uf)
            else:
                blocked = True  # a real journey with no derivable home
                break
        # A flowful shell dev must land somewhere real (the lane law).
        flowful = [d for d in devs_by_pf.get(key, [])
                   if getattr(d, "flows", None)]
        if not blocked and flowful and not fallback:
            blocked = True
        if blocked:
            result.anchored_husks_kept_journey.append(
                str(getattr(pf, "display_name", None)
                    or getattr(pf, "name", "?")))
            continue
        dropped_keys.add(key)
        uf_moves.extend(moves)
        uf_drops.extend(drops)
        result.anchored_husk_pfs_dropped.append(
            str(getattr(pf, "display_name", None)
                or getattr(pf, "name", "?")))
        aid = str(getattr(pf, "anchor_id", None) or "")
        for dev in devs_by_pf.get(key, []):
            if getattr(dev, "flows", None) and fallback:
                propose_pf_now(dev, fallback, rung="emission-I12")
                dev.anchor_id = f"fold:anchored-husk->{aid}"
                if getattr(dev, "shared_reason", None):
                    dev.shared_reason = None
                result.anchored_husk_devs_rebound += 1
            else:
                propose_pf_now(dev, None, rung="emission-I12")
                dev.anchor_id = None
                dev.shared_reason = ANCHORED_HUSK_REASON
                result.anchored_husk_devs_unbound += 1

    if not dropped_keys:
        return product_features

    for uf, target in uf_moves:
        propose_pf_now(uf, target, rung="emission-I12")
        result.anchored_husk_ufs_rehomed += 1
    if uf_drops:
        gone = {id(u) for u in uf_drops}
        user_flows[:] = [u for u in user_flows if id(u) not in gone]
        result.anchored_husk_seed_ufs_dropped = len(uf_drops)
    return [
        pf for pf in product_features if _pf_key(pf) not in dropped_keys
    ]


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
            propose_pf_now(uf, relinked, rung="emission-I12")
            result.uf_pf_refs_relinked += 1
        else:
            propose_pf_now(uf, None, rung="emission-I12")
            result.uf_pf_refs_nulled += 1


def _lane_ref_integrity(
    features: list["Feature"],
    user_flows: list["UserFlow"],
    result: EmissionIntegrityResult,
) -> None:
    """B52 — ``product_feature_id=None`` is LEGAL iff ``lane_ref`` is a
    LIVE lane resident (a pfid=None dev's uuid — the exact value the
    lane row will carry). A stale/dangling ``lane_ref`` (its dev was
    dropped or re-homed after Stage 6.985) is cleared together with the
    lane scope, so the journey falls through to ``assign_terminal_homes``
    instead of shipping a dangling reference. Same repair for a
    ``lane_ref`` on a PRODUCT-homed journey (a later stage re-homed it —
    the back-reference is dead). No-op (byte-identical) when no UF
    carries ``lane_ref`` — every pre-B52 / flag-OFF scan."""
    refs_present = any(
        getattr(uf, "lane_ref", None) for uf in user_flows
    )
    if not refs_present:
        return
    lane_uuids = {
        str(getattr(f, "uuid", "") or "")
        for f in features
        if getattr(f, "layer", "developer") == "developer"
        and getattr(f, "product_feature_id", None) is None
        and getattr(f, "uuid", None)
    }
    for uf in user_flows:
        ref = getattr(uf, "lane_ref", None)
        if not ref:
            continue
        if getattr(uf, "product_feature_id", None) is not None:
            uf.lane_ref = None  # product-homed — the back-ref is dead
            result.lane_refs_cleared += 1
        elif str(ref) not in lane_uuids:
            uf.lane_ref = None
            if getattr(uf, "surface_scope", None) == \
                    "platform_infrastructure":
                uf.surface_scope = None
            result.lane_refs_cleared += 1


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


def enforce_gap_ref_integrity(
    coverage_gaps: list[Any],
    product_features: list["Feature"],
) -> tuple[list[Any], dict[str, Any]]:
    """B45 — I12 round-trip for the ``coverage_gaps[]`` channel.

    Every emitted ``gap.product_feature_id`` must be an emitted PF key (the
    same contract Pass 2 enforces for ``user_flows[]``). A gap whose ref is
    not a direct key is first canonical-relinked against the PF key-set; a
    still-orphan gap is DROPPED (never nulled — an anonymous gap claim is
    meaningless) and recorded in the returned telemetry.

    Runs at Stage 6.98, AFTER the gaps are built (they inherit each marker's
    already-reconciled ``product_feature_id``, so this is a defensive guard —
    it fires only when a gap's home PF vanished between reconciliation and
    emission). Returns ``(kept_gaps, telemetry)``; never mutates the PF list.
    """
    pf_keys = {_pf_key(pf) for pf in product_features if _pf_key(pf)}
    canon_to_key: dict[str, str] = {}
    for key in pf_keys:
        canon_to_key.setdefault(canonical_slug(key), key)

    kept: list[Any] = []
    dropped: list[dict[str, Any]] = []
    relinked = 0
    for gap in coverage_gaps:
        ref = getattr(gap, "product_feature_id", None)
        if ref and ref in pf_keys:
            kept.append(gap)
            continue
        relink = canon_to_key.get(canonical_slug(ref)) if ref else None
        if relink is not None:
            gap.product_feature_id = relink
            relinked += 1
            kept.append(gap)
            continue
        dropped.append({
            "id": str(getattr(gap, "id", "") or ""),
            "label": str(getattr(gap, "label", "") or ""),
            "product_feature_id": ref,
            "reason": "orphan_pf_ref",
        })
    tele: dict[str, Any] = {"orphans_dropped": len(dropped), "relinked": relinked}
    if dropped:
        tele["dropped"] = dropped  # complete record — the no-silent-drop law
    return kept, tele


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
    # Product-Spine §4.1 — final bare-dir sweep (root markers only at this
    # layer; the tracked-file-aware directory guard lives at Stage 2). Runs
    # FIRST so a feature whose only "content" was a whole-repo marker is
    # then correctly classified as a phantom below. Kill-switch shared with
    # the claim-time guard: FAULTLINE_SPINE_BAREDIR=0.
    from faultline.pipeline_v2.spine_hygiene import strip_bare_dir_feature_paths

    result.bare_dir_paths_dropped = strip_bare_dir_feature_paths(
        list(features) + list(product_features),
    )
    # W4.2 anchored-husk rule BEFORE the phantom pass: the husk drop
    # re-homes / removes the dependent journeys itself, so I12 below
    # reconciles against an already-consistent reference set.
    product_features = _drop_anchored_husks(
        features, product_features, user_flows, flows, result,
    )
    features, product_features = _drop_phantoms(features, product_features, result)
    _reconcile_uf_pf_refs(product_features, user_flows, result)
    _lane_ref_integrity(features, user_flows, result)
    _rewrite_flow_backpointers(user_flows, flows, result)
    return features, product_features, result


__all__ = [
    "ANCHORED_HUSK_ENV",
    "ANCHORED_HUSK_REASON",
    "anchored_husk_drop_enabled",
    "canonical_slug",
    "enforce_emission_integrity",
    "enforce_gap_ref_integrity",
    "EmissionIntegrityResult",
]
