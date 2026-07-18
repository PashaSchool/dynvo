"""B37-ph2 — dispatch-mint homing to the target-file owner PF ($0).

The cal.com wave15 exhibit: a dispatch mint exists
(``run-dailyvideo-video-api-adapter-flow``) yet the PF that OWNS the
target file (``App Store — Cal Video``, anchored at the ``dailyvideo``
subtree) stays flowless-with-marker, because the mint's user-flow was
homed to the dev-feature of FIRST ATTRIBUTION — the dev the mint flow
was attached to at Stage-3 mint time (``dispatch_registry.mint_dispatch_seeds``
picks ``owner_of[target_file]``, an unowned / sibling-PF dev on the live
boards) — not to the PF whose anchor subtree encloses the target.

This pass walks the **path_index / anchor-chain of the target** and
re-homes a predominantly-dispatch user flow to the PF that owns the mint's
target file:

  1. the path_index dev→PF owner (``stage_6_99_i16_rehome._file_owner_pf``,
     the exact ruler i16-rehome applies) when the file is owned by a real
     emitted PF — so a re-home this pass makes is never reverted by i16 (the
     two read the same owner);
  2. else the anchor-chain enclosing PF (longest ``anchor_id`` filesystem
     prefix) — the starved-subtree case (supabase's pf=None studio mints),
     where i16 sees the entries as unowned and never reverts either.

The owner is the target-file OWNER, NOT the dev-of-first-attribution (the
dev the mint FLOW was attached to at Stage-3 mint time, which the UF
inherited): the target file's dev is re-homed across finalize, so its final
path_index owner can differ from the frozen rollup home — the mismatch this
pass corrects. Leading with path_index (not the file-path anchor) keeps the
two rulers agreeing: a mint whose target lives in a domain package but is
dev-owned by another feature (midday ``packages/accounting/.../fortnox.ts``
owned by the app-store feature) stays where its dev is, not where its path
points.

Runs AFTER the final path_index refresh + the flowless-PF backstops and
BEFORE the ``synth_quality`` gap arbitration (phase_finalize): the target
PF gains a member-ful journey ⇒ its flowless marker demotes ⇒ no coverage
gap (cal.com Cal Video dissolves), while a source the move leaves flowless
never spawns a NEW marker (the backstop already passed while the source was
still covered) — no ping-pong gap.

Guards (conservative by construction):
  * only user flows whose members are a STRICT MAJORITY dispatch mints
    (``description`` prefix ``"dispatch registry "``) are candidates — a
    single dispatch mint inside a rich journey never moves it;
  * the target owner must be a strict-majority agreement among those
    dispatch mints AND a real emitted PF key;
  * NO-OP guard — if the current home already owns the target (or the
    owner is None / lane), nothing changes (byte-identity for
    correctly-homed mints).

The shared resolver :func:`build_anchor_owner_resolver` is reused by the
Stage 6.987 devgrain-demote I9 rider (flowful demoted devs home by the
same target-owner machinery instead of the platform_infrastructure lane).

Flag ``FAULTLINE_DISPATCH_HOMING_B37P2`` — default ON since the
2026-07-12 flip (KEY_SCHEMA v28, coupled with the B33 devgrain gate);
``=0`` is byte-identical to pre-B37-ph2.
"""

from __future__ import annotations
from faultline.pipeline_v2.overturn_ledger import propose_pf

import os
from collections import Counter
from typing import Any, Callable

__all__ = [
    "DISPATCH_HOMING_ENV",
    "DISPATCH_DESC_PREFIX",
    "dispatch_homing_enabled",
    "build_anchor_owner_resolver",
    "home_dispatch_mints",
]

DISPATCH_HOMING_ENV = "FAULTLINE_DISPATCH_HOMING_B37P2"

#: The mint marker: ``dispatch_registry.mint_dispatch_seeds`` stamps every
#: seed's ``description`` with this prefix (``"dispatch registry <file>"``).
DISPATCH_DESC_PREFIX = "dispatch registry "


def dispatch_homing_enabled() -> bool:
    """B37-ph2 — default ON since the 2026-07-12 flip (KEY_SCHEMA v28,
    coupled with the B33 devgrain gate: 6.987's flowful demoted devs home
    via this machinery — without it the demote re-creates I9). Keyed
    papermark proof: I9=0, lane 43->42; keyless byte-identical no-op
    elsewhere. ``FAULTLINE_DISPATCH_HOMING_B37P2=0`` restores the
    pre-B37-ph2 homes byte-identically — explicit off stays a valid
    kill-switch forever."""
    return os.environ.get(DISPATCH_HOMING_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _attr(o: Any, name: str) -> Any:
    return o.get(name) if isinstance(o, dict) else getattr(o, name, None)


def _pf_key(pf: Any) -> str:
    return str(_attr(pf, "id") or _attr(pf, "name") or "")


def _anchor_segments(pf: Any) -> tuple[str, ...]:
    """The PF's ``anchor_id`` filesystem path split into path segments
    (``fdir:apps/studio/components/interfaces/Functions`` → the 5 segments).
    A bare URL-slug anchor (``route:logout``) yields a single ``logout``
    segment that never segment-prefix-matches a real file path — so no kind
    filter is needed."""
    aid = str(_attr(pf, "anchor_id") or "")
    if ":" not in aid:
        return ()
    tail = aid.split(":", 1)[1].strip("/")
    return tuple(s for s in tail.split("/") if s)


def build_anchor_owner_resolver(
    product_features: list[Any],
) -> Callable[[str | None], str | None]:
    """Return ``resolve(target_file) -> PF key`` whose anchor filesystem path
    is the LONGEST segment-prefix of ``target_file`` (tie → PF key, alpha).
    ``None`` when no PF anchor encloses the file.

    This is the anchor-chain walk (the target owner) — distinct from the
    dev→PF map (the dev-of-first-attribution's home we correct away from)."""
    entries: list[tuple[int, str, tuple[str, ...]]] = []
    for pf in product_features:
        segs = _anchor_segments(pf)
        key = _pf_key(pf)
        if segs and key:
            entries.append((len(segs), key, segs))
    # Longest-prefix first, then alpha key — deterministic winner.
    entries.sort(key=lambda e: (-e[0], e[1]))

    def resolve(target_file: str | None) -> str | None:
        if not target_file:
            return None
        tsegs = tuple(
            s for s in str(target_file).replace("\\", "/").split("/") if s
        )
        for nseg, key, segs in entries:
            if nseg <= len(tsegs) and tsegs[:nseg] == segs:
                return key
        return None

    return resolve


def _dispatch_flow_index(features: list[Any]) -> dict[str, str]:
    """``flow uuid -> target_file`` for every dispatch-registry mint (the
    seeds carry the ``"dispatch registry "`` description prefix and their
    ``entry_point_file`` is the registry target)."""
    idx: dict[str, str] = {}
    for f in features:
        for fl in (_attr(f, "flows") or []):
            desc = str(_attr(fl, "description") or "")
            if not desc.startswith(DISPATCH_DESC_PREFIX):
                continue
            uid = _attr(fl, "uuid")
            ep = _attr(fl, "entry_point_file")
            if uid and ep:
                idx[str(uid)] = str(ep)
    return idx


def _build_target_owner(
    features: list[Any],
    product_features: list[Any],
    path_index: Any,
    pf_keys: set[str],
) -> Callable[[str | None], str | None]:
    """``owner(target_file) -> PF key`` walking the path_index THEN the
    anchor-chain, i16-consistently:

      1. the path_index dev→PF owner (``_file_owner_pf``, the exact ruler
         Stage 6.99 i16-rehome applies) when the file is owned by a REAL
         emitted PF — so a re-home the homing makes is never reverted by
         i16 (they read the same owner);
      2. else (unowned / lane / product-layer name absent from the emitted
         key-set) the anchor-chain enclosing PF — the starved-subtree case
         (supabase's pf=None studio mints), where i16 sees the entries as
         unowned and so never reverts either.

    Deliberately NOT the reverse: leading with the anchor-chain (file path)
    mis-attributes a mint whose target lives in a domain package but is
    dev-owned by another PF (midday ``packages/accounting/.../fortnox.ts``
    owned by the app-store feature) — the anchor path says ``accounting``,
    the real owner is ``app-store``; leading with path_index keeps the two
    rulers agreeing."""
    anchor_resolve = build_anchor_owner_resolver(product_features)
    entry_owner: dict[str, Any] = {}
    if path_index:
        from faultline.pipeline_v2.stage_6_99_i16_rehome import _file_owner_pf
        feat_by_uuid = {
            _attr(f, "uuid"): f
            for f in (list(features) + list(product_features))
            if _attr(f, "uuid")
        }
        entry_owner = _file_owner_pf(path_index, feat_by_uuid, frozenset())

    def owner(target_file: str | None) -> str | None:
        if not target_file:
            return None
        o = entry_owner.get(target_file)
        if isinstance(o, str) and o in pf_keys:
            return o  # path_index dev→PF (i16-consistent)
        return anchor_resolve(target_file)  # unowned/lane → anchor-chain

    return owner


def home_dispatch_mints(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
    path_index: Any = None,
) -> dict[str, Any]:
    """Re-home predominantly-dispatch user flows to their target-owner PF
    (path_index dev→PF first, anchor-chain fallback). Mutates
    ``uf.product_feature_id`` in place; returns telemetry. Intended to run
    AFTER the final path_index refresh + the flowless-PF backstops and
    BEFORE the synth_quality gap arbitration: the target PF gains a
    member-ful journey (its marker demotes), while a source the move leaves
    flowless never spawns a NEW marker (the backstop already passed while it
    was covered). See module docstring for the guards."""
    tele: dict[str, Any] = {
        "enabled": True, "rehomed": 0, "candidates": 0,
        "skipped_noop": 0, "skipped_no_owner": 0, "skipped_not_majority": 0,
        "skipped_orphan_guard": 0, "moves": [],
    }
    dispatch_target = _dispatch_flow_index(features)
    if not dispatch_target:
        return tele
    pf_keys = {_pf_key(pf) for pf in product_features} - {""}
    resolve = _build_target_owner(
        features, product_features, path_index, pf_keys)

    # Orphan guard (I8, the Stage 6.99 i16-rehome precedent): a move must
    # never strip the SOURCE PF's LAST member-ful journey — that silently
    # re-arms I8 (midday 'Run accounting' is accounting's only journey; i16
    # refuses to move it for exactly this reason). Track a running member-ful
    # UF count per home and never let it fall to zero.
    uf_count: Counter = Counter(
        _attr(u, "product_feature_id") for u in user_flows
        if _attr(u, "product_feature_id") and (_attr(u, "member_flow_ids")))

    for uf in user_flows:
        member_ids = [str(m) for m in (_attr(uf, "member_flow_ids") or [])]
        if not member_ids:
            continue
        owners = [
            resolve(dispatch_target[m])
            for m in member_ids if m in dispatch_target
        ]
        if not owners:
            continue  # PF without dispatch mints untouched
        tele["candidates"] += 1
        # Dispatch mints must be a STRICT MAJORITY of the flow's members —
        # a single mint inside a rich journey never moves the journey.
        if len(owners) * 2 <= len(member_ids):
            tele["skipped_not_majority"] += 1
            continue
        resolved = Counter(o for o in owners if o)
        if not resolved:
            tele["skipped_no_owner"] += 1
            continue
        # Strict-majority target-owner among the dispatch mints (tie → alpha).
        top_owner, top_ct = sorted(
            resolved.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if top_ct * 2 <= len(owners):
            tele["skipped_no_owner"] += 1  # no strict-majority owner agreement
            continue
        pfid = _attr(uf, "product_feature_id")
        if top_owner not in pf_keys:
            tele["skipped_no_owner"] += 1
            continue
        if top_owner == pfid:
            tele["skipped_noop"] += 1  # current home already owns the target
            continue
        if pfid and uf_count.get(pfid, 0) <= 1:
            # Orphan guard — do NOT strip the source PF's last journey (I8).
            tele["skipped_orphan_guard"] += 1
            continue
        propose_pf(uf, top_owner, rung="dispatch")
        if pfid:
            uf_count[pfid] -= 1
        uf_count[top_owner] += 1
        tele["rehomed"] += 1
        if len(tele["moves"]) < 50:
            tele["moves"].append({
                "uf": _attr(uf, "name"), "from": pfid, "to": top_owner,
                "dispatch_members": f"{len(owners)}/{len(member_ids)}",
            })
    return tele
