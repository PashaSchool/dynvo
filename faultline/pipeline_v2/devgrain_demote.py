"""Stage 6.987 — devgrain-leaf PF demote (B33 v2, post-journey-layer).

`route:`/`fdir:`-anchored PF candidates whose leaf name is a plumbing
screen or a journey-step token (welcome, getting-started, access-denied,
redirect-*, *-callback, *-onboarding …) mint as standalone product
features with 0 flows; the UF-namer then invents fake journeys under
them (papermark ``Welcome`` route:welcome 6982 LOC; cal.com
``Getting Started`` fdir:apps/web/modules/getting-started; novu
``Access Denied`` + ``Redirect To Legacy Studio Auth``).

WHY POST-UF (the v1 lesson, wave-2 2026-07-11): a mint-time bar killed
twenty ``Onboarding`` and silently dropped its 9 SUBSTANTIVE wizard
journeys (66→58 UFs) — journey conservation violated. UFs do not exist
at 6.86 mint time, so NO mint-time bar can be conservation-safe. The
discriminator between disease and non-target is the JOURNEY PROFILE,
which only exists after the journey layer settles: disease = 1-2
synthesized micro rows (member_count ≤ 3); non-targets carry rich
journey sets (twenty 9 UFs mc2-3, typebot mc11, supabase logout mc7).

Mechanism (corroboration only — the token set never kills alone):

  * eligible: PF anchor kind ``route:``/``fdir:``; normalized leaf
    token-matches the closed YAML set (``journey_step_leaf_tokens`` in
    spine-anchor-vocab.yaml, B30 data-not-code precedent); NOT
    nav-declared (the author's IA word — kan/midday ``Onboarding``,
    novu ``Error`` MUST survive); board nav readable (empty
    ``nav_keys`` ⇒ the exemption signal is unreadable — Vue/keyless
    boards — the pass abstains board-wide, honest-abstain + telemetry);
  * demote iff the PF's FINAL journey profile is micro: ≤ 2 UFs AND
    every UF ``member_count`` ≤ 3 (count-grain constants, the
    widget-micro / giant-catchall precedent — no per-repo tuning).
    Demotion = PF row removed; devs re-point to the nearest surviving
    ancestor PF by anchor-prefix containment (paths/member_files union,
    the husk-post-uf-fold mechanics) — else they stay L1/unowned
    exactly as a never-minted anchor's devs would; the synthesized
    micro-UFs DROP with telemetry (they are artifacts of the PF's
    existence, not recall);
  * else: honest abstain + telemetry (the PF stays — substantive
    journeys are never touched; conservation by construction).

Runs AFTER the journey layer is final (6.7 rollup / splitter / refiner /
6.7d rewrite / recall seeds / lattice / e2e truth+orphan / 6.985
transport / 6.986 mega-PF) and BEFORE the 6.97 LOC prefetch, the
flowless-PF marker backstops (W5.1 loc-worthy, B4 synth-quality) and
emission_integrity — so no marker is ever synthesized for a demoted PF
and every downstream integrity rail sees the demoted state.

Deterministic, $0 LLM, scale-invariant. Flag
``FAULTLINE_FDIR_DEVGRAIN_GATE`` — default OFF until the
topology-breadth flip commit; OFF is byte-identical to pre-B33.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.spine_anchors import (
    load_spine_vocab,
    normalize_anchor_key,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "FDIR_DEVGRAIN_GATE_ENV",
    "fdir_devgrain_gate_enabled",
    "journey_step_leaf_tokens",
    "is_journey_step_leaf",
    "run_devgrain_demote",
]

FDIR_DEVGRAIN_GATE_ENV = "FAULTLINE_FDIR_DEVGRAIN_GATE"

#: Surface suffixes stripped ONCE from a leaf to recover its journey/plumbing
#: token (b8c ``_domain_family`` precedent: ``access-denied-page`` →
#: ``access-denied``). ``-page`` is the common route-surface token; the RN /
#: legacy ``-screen`` / ``-view`` cover the rest.
_DEVGRAIN_LEAF_SUFFIXES = ("-page", "-screen", "-view")

#: Demote floors — count-grain constants (the widget-micro /
#: giant-catchall precedent, no per-repo tuning): the disease profile is
#: 1-2 synthesized micro rows; ANY richer journey set vetoes the demote
#: (the twenty ``Onboarding`` 9-journey lesson).
_MAX_PROFILE_UFS = 2
_MAX_PROFILE_MEMBER_COUNT = 3

#: The never-minted semantic for devs with no surviving ancestor PF —
#: mirrors stage_6_86's ``_SHARED_REASON_BAR`` (a barred anchor's devs).
_SHARED_REASON_BAR = "sub_mint_bar_surface"


def fdir_devgrain_gate_enabled() -> bool:
    """B33 — default OFF; ``FAULTLINE_FDIR_DEVGRAIN_GATE=1`` enables the
    post-UF devgrain-leaf demote pass. OFF (byte-identical to pre-B33)
    until the topology-breadth default-ON flip commit."""
    return os.environ.get(FDIR_DEVGRAIN_GATE_ENV, "0").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def journey_step_leaf_tokens(vocab: dict[str, Any]) -> dict[str, frozenset[str]]:
    """The B33 closed devgrain-token set (data — ``spine-anchor-vocab.yaml``,
    NOT hardcoded Python): ``exact`` leaf membership, ``prefix`` first-token
    matches, ``suffix`` last-token matches. Returned as frozensets so every
    match is a membership test — the matcher iterates NOTHING (the recorded
    set-iteration nondeterminism class stays out of this rail)."""
    block = vocab.get("journey_step_leaf_tokens") or {}
    return {
        "exact": frozenset(block.get("exact") or ()),
        "prefix": frozenset(block.get("prefix") or ()),
        "suffix": frozenset(block.get("suffix") or ()),
    }


def is_journey_step_leaf(key: str, tokens: dict[str, frozenset[str]]) -> bool:
    """``True`` when a normalized anchor *key* names a plumbing screen or a
    journey step. Strip ONE trailing surface suffix (``-page``/``-screen``/
    ``-view``), then match: exact-set membership, OR first ``-``-token in
    ``prefix`` (``redirect-*``), OR last ``-``-token in ``suffix``
    (``*-callback``/``*-onboarding``/``*-redirect``). Closed-set discipline
    (B30) — no fuzzy or substring matching."""
    if not key:
        return False
    stem = key
    for suf in _DEVGRAIN_LEAF_SUFFIXES:
        if stem.endswith(suf) and len(stem) > len(suf):
            stem = stem[: -len(suf)]
            break
    if stem in tokens["exact"]:
        return True
    parts = stem.split("-")
    if parts[0] in tokens["prefix"]:
        return True
    return parts[-1] in tokens["suffix"]


def _anchor_kind_and_tail(anchor_id: str) -> tuple[str, str]:
    """``(kind, path-tail)`` of a PF's anchor canonical id
    (``fdir:apps/web/modules/onboarding`` → ``("fdir",
    "apps/web/modules/onboarding")``)."""
    kind, _, tail = anchor_id.partition(":")
    return kind, tail.strip("/")


def _leaf_key(tail: str) -> str:
    """Normalized leaf key of an anchor tail (last path segment through the
    anchors' own house normalizer)."""
    leaf = tail.rsplit("/", 1)[-1] if tail else ""
    return normalize_anchor_key(leaf)


def _nav_declared(leaf: str, nav_keys: frozenset[str]) -> bool:
    """The author declared this leaf in the product IA: the normalized leaf
    OR its surface-suffix-stripped stem appears in the board's nav keys
    (``access-denied-page`` is declared by an ``/access-denied`` nav href)."""
    if leaf in nav_keys:
        return True
    for suf in _DEVGRAIN_LEAF_SUFFIXES:
        if leaf.endswith(suf) and len(leaf) > len(suf):
            return leaf[: -len(suf)] in nav_keys
    return False


def run_devgrain_demote(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list[Any],
    nav_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """See module docstring. Mutates ``product_features`` / ``user_flows`` /
    dev stamps in place; returns telemetry for ``scan_meta.devgrain_demote``.
    """
    tele: dict[str, Any] = {
        "enabled": True, "eligible": 0,
        "demoted": [], "abstained": [], "nav_declared_skipped": [],
        "dropped_ufs": {}, "devs_repointed": 0, "devs_unowned": 0,
    }
    if not fdir_devgrain_gate_enabled():
        tele["enabled"] = False
        return tele
    if not nav_keys:
        # Board-wide honest abstain: the nav-exemption signal is unreadable
        # (Vue/keyless boards) — the pass acts on nothing.
        tele["journey_step_leaf_abstained"] = True
        return tele

    tokens = journey_step_leaf_tokens(load_spine_vocab())

    ufs_by_pf: dict[str, list[Any]] = defaultdict(list)
    for uf in user_flows:
        pfid = getattr(uf, "product_feature_id", None)
        if pfid:
            ufs_by_pf[str(pfid)].append(uf)

    # Phase 1 — decide every demotion against the SAME pre-pass board
    # (deterministic: sorted by PF name; no order-dependent cascades).
    demote: list["Feature"] = []
    for pf in sorted(product_features,
                     key=lambda p: str(getattr(p, "name", "") or "")):
        key = str(getattr(pf, "name", "") or "")
        aid = str(getattr(pf, "anchor_id", None) or "")
        kind, tail = _anchor_kind_and_tail(aid)
        if kind not in {"route", "fdir"}:
            continue
        leaf = _leaf_key(tail)
        if not is_journey_step_leaf(leaf, tokens):
            continue
        tele["eligible"] += 1
        if _nav_declared(leaf, nav_keys):
            # Product by the author's word — the gate NEVER fires here.
            tele["nav_declared_skipped"].append(key)
            continue
        profile = ufs_by_pf.get(key) or []
        if (len(profile) <= _MAX_PROFILE_UFS and all(
                (getattr(u, "member_count", 0) or 0)
                <= _MAX_PROFILE_MEMBER_COUNT for u in profile)):
            demote.append(pf)
        else:
            tele["abstained"].append({
                "pf": key, "ufs": len(profile),
                "max_member_count": max(
                    (getattr(u, "member_count", 0) or 0) for u in profile),
            })
    if not demote:
        return tele

    demote_keys = {str(getattr(pf, "name", "") or "") for pf in demote}

    def _anchor_path(pf: "Feature") -> str | None:
        aid = str(getattr(pf, "anchor_id", None) or "")
        if ":" not in aid:
            return None
        return aid.split(":", 1)[1].strip("/") or None

    def _ancestor_target(tail: str) -> "Feature | None":
        """The nearest SURVIVING ancestor PF by anchor-prefix containment
        (longest enclosing path-anchored capability; tie → name)."""
        best: tuple[int, str, "Feature"] | None = None
        for other in product_features:
            okey = str(getattr(other, "name", "") or "")
            if okey in demote_keys or okey.strip().lower() in (
                "platform", "shared-platform",
            ):
                continue
            opath = _anchor_path(other)
            if not opath:
                continue
            if tail.startswith(opath + "/"):
                cand = (len(opath), okey, other)
                if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                    best = cand
        return best[2] if best else None

    devs_by_pf: dict[str, list["Feature"]] = defaultdict(list)
    for f in developer_features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        if pfid:
            devs_by_pf[str(pfid)].append(f)

    # Phase 2 — apply (sorted; the husk-post-uf-fold mechanics).
    dropped_uf_ids: set[int] = set()
    for pf in sorted(demote, key=lambda p: str(getattr(p, "name", "") or "")):
        key = str(getattr(pf, "name", "") or "")
        aid = str(getattr(pf, "anchor_id", None) or "")
        _kind, tail = _anchor_kind_and_tail(aid)
        # DROP the synthesized micro-UFs — artifacts of the PF's existence,
        # not recall (telemetry keeps the names for the wave census).
        dropped = sorted(
            str(getattr(u, "name", "") or "") for u in ufs_by_pf.get(key, []))
        if dropped:
            tele["dropped_ufs"][key] = dropped
            dropped_uf_ids.update(id(u) for u in ufs_by_pf.get(key, []))
        target = _ancestor_target(tail)
        for m in sorted(devs_by_pf.get(key, ()),
                        key=lambda d: str(getattr(d, "name", "") or "")):
            if target is not None:
                m.product_feature_id = str(getattr(target, "name", "") or "")
                m.anchor_id = f"fold:devgrain-demote->{aid}"
                if getattr(m, "shared_reason", None):
                    m.shared_reason = None
                tele["devs_repointed"] += 1
            else:
                # No minting ancestor — L1/unowned, exactly as a
                # never-minted anchor's devs would be (natural demotion,
                # never a silent deletion; the devs keep their files).
                m.product_feature_id = None
                m.anchor_id = None
                m.shared_reason = _SHARED_REASON_BAR
                tele["devs_unowned"] += 1
        if target is not None:
            # paths + member_files union onto the target (dedup, stable
            # order) — the fold_unreferenced_vendor_husks mechanics.
            seen_p = set(getattr(target, "paths", None) or [])
            merged_p = list(getattr(target, "paths", None) or [])
            for p in (getattr(pf, "paths", None) or []):
                if p not in seen_p:
                    seen_p.add(p)
                    merged_p.append(p)
            target.paths = merged_p
            seen_mf = {
                (mf.get("path") if isinstance(mf, dict)
                 else getattr(mf, "path", None))
                for mf in (getattr(target, "member_files", None) or [])
            }
            for mf in (getattr(pf, "member_files", None) or []):
                mfp = (mf.get("path") if isinstance(mf, dict)
                       else getattr(mf, "path", None))
                if mfp and mfp not in seen_mf:
                    seen_mf.add(mfp)
                    target.member_files.append(mf)
        tele["demoted"].append({
            "pf": key, "anchor": aid,
            "into": (str(getattr(target, "name", "") or "")
                     if target is not None else None),
            "ufs_dropped": len(dropped),
        })

    product_features[:] = [
        pf for pf in product_features
        if str(getattr(pf, "name", "") or "") not in demote_keys
    ]
    if dropped_uf_ids:
        user_flows[:] = [u for u in user_flows if id(u) not in dropped_uf_ids]
    return tele
