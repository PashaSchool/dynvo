"""Product-Spine §4.5 — conservation law UF ⊆ PF (operator decision C).

THE epicenter defect (evidence 2026-07-06 §E-1): 69.9% of the corpus's
UFs have <50% file overlap with the product feature they are attached
to; 53.7% of member-flow entries live in code owned by a DIFFERENT PF;
18.4% of UFs claim more flow-LOC than their PF owns (up to 190×). The
journeys and the features are individually plausible — the JOIN between
them is close to random, because nothing ever required a UF's code to
live inside its PF's ownership (rootcause RC2: three uncoupled
assignment channels).

This module supplies the law, applied at CONSTRUCTION time on both UF
paths:

  * the deterministic Stage 6.7 rollup (``cluster_user_flows``) checks
    every cluster's majority-vote PF against the members' spans/entries
    and resettles violators (:func:`conserved_pfid`);
  * the 6.7d rewrite applies the same check inside ``_finish`` right
    after UF reconstruction — BEFORE the PF-UF backstop and the shared
    reshare ladder, so every later repair operates on
    conservation-clean bindings (:func:`apply_uf_conservation`);
  * a final pass runs in ``phase_finalize`` after the post-6.7d hub
    binding (§4.4 changes dev→PF, so closures move).

THE LAW (deterministic ladder, in order):

  1. VOTES — for every member flow, its line-range spans (fallback: one
     vote per member file) are attributed to the product feature owning
     each file (file → primary dev → dev.product_feature_id). Files
     owned by nobody, by a concern FACET (§4.1), or by the
     Shared-Platform bucket DO NOT VOTE. Entry files vote in a separate
     entry tally under the same ownership rule.
  2. ACCEPT — the incumbent binding stands iff it is a real (non-shared)
     PF holding a strict span-LOC MAJORITY (>50%) AND at least half of
     the counted entry files.
  3. RESETTLE — otherwise the UF re-attaches to the span-argmax real PF
     (ties → most entry files → lexicographic key).
  4. NO SIGNAL — when no real PF owns any span/entry, the incumbent is
     kept for the downstream ladders (reshare / backstop have richer
     options: name-family, carve, docs surface); the OPTIONAL
     ``null_shared_without_signal`` mode (the finalize pass) nulls a
     shared-platform binding instead — a UF may never ship attached to
     Shared Platform.

Zero-loss: journeys are never dropped here — only re-attached.

Accounting corollary (Stage 6.97 extension, same kill-switch): flow-LOC
counts "on" a PF only for span lines inside files the PF's devs
primarily own, per-file clipped at the file's own LOC — so
``loc_flow <= loc`` (on-flow ≤ 100%) BY CONSTRUCTION; span lines outside
the closure land in the ``loc_flow_shared`` channel.

Deterministic, $0 LLM, scale-invariant (majority ratios only).
Kill-switch: ``FAULTLINE_SPINE_CONSERVATION=0`` (default ON).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature, UserFlow

__all__ = [
    "SPINE_CONSERVATION_ENV",
    "SPINE_DEV_REHOME_ENV",
    "conservation_enabled",
    "dev_rehome_enabled",
    "build_file_pf_owner",
    "dev_views_for",
    "member_votes",
    "conserved_pfid",
    "apply_uf_conservation",
    "rehome_shared_flowful_devs",
]

SPINE_CONSERVATION_ENV = "FAULTLINE_SPINE_CONSERVATION"
SPINE_DEV_REHOME_ENV = "FAULTLINE_SPINE_DEV_REHOME"

_SHARED_PF_KEYS = frozenset(("shared-platform", "platform"))


def conservation_enabled() -> bool:
    """Conservation law — default ON, ``FAULTLINE_SPINE_CONSERVATION=0`` off."""
    return os.environ.get(SPINE_CONSERVATION_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def dev_rehome_enabled() -> bool:
    """Dev-grain re-home (W1.1) — default ON, ``FAULTLINE_SPINE_DEV_REHOME=0``
    off. Separate switch so the UF-grain law can be isolated from the
    dev-grain application when bisecting a regression."""
    return os.environ.get(SPINE_DEV_REHOME_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


# ── Ownership resolution ────────────────────────────────────────────────


def build_file_pf_owner(
    dev_views: Iterable[dict[str, Any]],
    real_pf_keys: frozenset[str] | None = None,
) -> dict[str, str]:
    """``{file: pf_key}`` over the developer features' OWNED paths.

    ``dev_views`` rows carry ``name`` / ``paths`` / ``product_feature_id``
    / ``role`` (dict views or attribute objects both accepted). Facet
    devs (§4.1) and devs bound to the shared bucket contribute nothing —
    their files must not vote. When ``real_pf_keys`` is given, only PFs
    in that set count (guards against stale pfids). First claimant wins
    on the rare shared path (input order is stable upstream).
    """
    out: dict[str, str] = {}
    for dev in dev_views:
        get = dev.get if isinstance(dev, dict) else (
            lambda k, _d=dev: getattr(_d, k, None))
        if get("role") == "facet":
            continue
        pfid = get("product_feature_id")
        if not pfid or str(pfid).strip().lower() in _SHARED_PF_KEYS:
            continue
        key = str(pfid)
        if real_pf_keys is not None and key not in real_pf_keys:
            continue
        for p in get("paths") or []:
            out.setdefault(_norm(str(p)), key)
    return out


def dev_views_for(
    developer_features: Iterable[Any],
    dev_to_product: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Uniform dev views for :func:`build_file_pf_owner`.

    ``dev_to_product`` overrides the devs' (possibly not-yet-restamped)
    ``product_feature_id`` — the 6.7d ``_finish`` path passes its fresh
    map; finalize-time callers read the stamped fields.
    """
    views: list[dict[str, Any]] = []
    for d in developer_features:
        if getattr(d, "layer", "developer") != "developer":
            continue
        if dev_to_product is not None:
            slugs = dev_to_product.get(getattr(d, "name", "") or "")
            pfid = slugs[0] if slugs else None
        else:
            pfid = getattr(d, "product_feature_id", None)
        views.append({
            "name": getattr(d, "name", None),
            "paths": list(getattr(d, "paths", None) or []),
            "product_feature_id": pfid,
            "role": getattr(d, "role", None),
        })
    return views


def _flow_get(flow: Any, key: str) -> Any:
    return flow.get(key) if isinstance(flow, dict) else getattr(flow, key, None)


def _flow_span_weights(flow: Any) -> dict[str, int]:
    """``{file: span_lines}`` for one member flow.

    Line-range spans when the flow carries them (merged upstream into
    non-overlapping per-file ranges by Stage 3.5); fallback: one vote per
    member file — a flow without resolved spans still votes, just with
    file-grain weight.
    """
    weights: dict[str, int] = {}
    for lr in _flow_get(flow, "line_ranges") or []:
        get = lr.get if isinstance(lr, dict) else (
            lambda k, _l=lr: getattr(_l, k, None))
        path = get("path")
        if not path:
            continue
        try:
            start = int(get("start_line") or 0)
            end = int(get("end_line") or 0)
        except (TypeError, ValueError):
            continue
        span = max(end - start + 1, 1)
        weights[_norm(str(path))] = weights.get(_norm(str(path)), 0) + span
    if weights:
        return weights
    for p in _flow_get(flow, "paths") or []:
        weights[_norm(str(p))] = weights.get(_norm(str(p)), 0) + 1
    return weights


def member_votes(
    members: Iterable[Any],
    file_pf_owner: dict[str, str],
) -> tuple[dict[str, int], dict[str, int]]:
    """``(span_votes, entry_votes)`` per PF key over the member flows.

    Span votes weight by line-range LOC (file-count fallback); entry
    votes count entry files. Unowned / facet / shared files vote in
    neither tally (§4.5: "entries in shared/facet files don't vote").
    """
    span_votes: dict[str, int] = {}
    entry_votes: dict[str, int] = {}
    for m in members:
        for path, weight in _flow_span_weights(m).items():
            owner = file_pf_owner.get(path)
            if owner is not None:
                span_votes[owner] = span_votes.get(owner, 0) + weight
        entry = _flow_get(m, "entry_point_file")
        if entry:
            owner = file_pf_owner.get(_norm(str(entry)))
            if owner is not None:
                entry_votes[owner] = entry_votes.get(owner, 0) + 1
    return span_votes, entry_votes


# ── The decision ladder ─────────────────────────────────────────────────


def conserved_pfid(
    members: Iterable[Any],
    file_pf_owner: dict[str, str],
    incumbent: str | None,
    *,
    null_shared_without_signal: bool = False,
) -> tuple[str | None, bool]:
    """Apply the conservation ladder to ONE user flow.

    Returns ``(pf_key, resettled)`` — ``pf_key`` may equal the incumbent
    (law satisfied / no signal), a different real PF (resettled), or
    ``None`` (shared incumbent with no signal under the finalize mode).
    """
    span_votes, entry_votes = member_votes(members, file_pf_owner)
    incumbent_key = str(incumbent) if incumbent else None
    incumbent_shared = bool(
        incumbent_key
        and incumbent_key.strip().lower() in _SHARED_PF_KEYS,
    )

    if not span_votes and not entry_votes:
        # Rung 4 — no real-PF signal: leave the binding for the richer
        # downstream ladders; the finalize mode nulls a shared binding
        # (a UF may never ship attached to Shared Platform).
        if incumbent_shared and null_shared_without_signal:
            return None, True
        return incumbent_key, False

    total_span = sum(span_votes.values())
    total_entry = sum(entry_votes.values())

    if (
        incumbent_key
        and not incumbent_shared
        and total_span
        and span_votes.get(incumbent_key, 0) * 2 > total_span
        and (
            not total_entry
            or entry_votes.get(incumbent_key, 0) * 2 >= total_entry
        )
    ):
        return incumbent_key, False  # Rung 2 — law already satisfied.

    # Rung 3 — resettle to the span-argmax real PF (entry tally breaks
    # span ties; lexicographic key is the deterministic last resort).
    candidates = sorted(
        set(span_votes) | set(entry_votes),
        key=lambda k: (
            -span_votes.get(k, 0),
            -entry_votes.get(k, 0),
            k,
        ),
    )
    chosen = candidates[0]
    return chosen, chosen != incumbent_key


# ── Typed pass (6.7d _finish + phase_finalize) ──────────────────────────


def apply_uf_conservation(
    user_flows: list["UserFlow"],
    developer_features: list["Feature"],
    product_features: list["Feature"],
    *,
    dev_to_product: dict[str, tuple[str, ...]] | None = None,
    null_shared_without_signal: bool = False,
) -> dict[str, Any]:
    """Enforce the law over typed arrays IN PLACE; returns telemetry.

    ``dev_to_product`` overrides the devs' (possibly not-yet-restamped)
    ``product_feature_id`` — the 6.7d ``_finish`` path passes its fresh
    map; the finalize pass reads the stamped fields. Synthesized backstop
    UFs are skipped (built from their PF's own flows — conservation-clean
    by construction, and they exist to satisfy I8).
    """
    tele: dict[str, Any] = {
        "enabled": conservation_enabled(), "checked": 0, "resettled": 0,
        "nulled_shared": 0, "donors_left_uncovered": 0,
    }
    if not conservation_enabled() or not user_flows:
        return tele

    pf_keys = frozenset(
        str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")
        for pf in product_features
    ) - {""}

    file_pf_owner = build_file_pf_owner(
        dev_views_for(developer_features, dev_to_product),
        real_pf_keys=pf_keys,
    )

    # Member-flow lookup: uuid first, name fallback (mirrors _flow_key).
    flow_by_id: dict[str, Any] = {}
    for d in developer_features:
        for fl in getattr(d, "flows", None) or []:
            for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
                if key and key not in flow_by_id:
                    flow_by_id[key] = fl

    before_cover = {
        getattr(uf, "product_feature_id", None) for uf in user_flows
    }
    for uf in user_flows:
        if getattr(uf, "synthesized", False):
            continue
        members = [
            flow_by_id[mid]
            for mid in (getattr(uf, "member_flow_ids", None) or [])
            if mid in flow_by_id
        ]
        if not members:
            continue
        tele["checked"] += 1
        incumbent = getattr(uf, "product_feature_id", None)
        chosen, moved = conserved_pfid(
            members, file_pf_owner, incumbent,
            null_shared_without_signal=null_shared_without_signal,
        )
        if moved:
            uf.product_feature_id = chosen
            if chosen is None:
                tele["nulled_shared"] += 1
            else:
                tele["resettled"] += 1

    # Donor visibility: PFs that were covered before but lost every UF
    # (validators I8 read this; the in-6.7d ordering runs the backstop
    # AFTER this pass exactly so it can re-cover them).
    after_cover = {
        getattr(uf, "product_feature_id", None) for uf in user_flows
    }
    tele["donors_left_uncovered"] = len(
        (before_cover - after_cover) - {None},
    )
    return tele


# ── Dev-grain application (W1.1, validator I9) ──────────────────────────


def _entry_file_of(flow: Any) -> str | None:
    entry = _flow_get(flow, "entry_point_file")
    if entry:
        return str(entry)
    ep = _flow_get(flow, "entry_point")
    if ep is not None:
        path = ep.get("path") if isinstance(ep, dict) else getattr(ep, "path", None)
        if path:
            return str(path)
    return None


def rehome_shared_flowful_devs(
    developer_features: list["Feature"],
    product_features: list["Feature"],
) -> dict[str, Any]:
    """Re-home shared-resident flowful surface devs to the PF their own
    flows' code lives in (validator I9: no flowful route-owning dev may
    ride the shared bucket).

    The observed mechanism (Soc0 dev ``api``, 2026-07-06 validation wave;
    pre-existing and draw-variant across pre-W1 scans): the 6.7d Call-2
    re-attribution scatters a small router dev into ``shared-platform``
    even though its flows' spans and entries sit squarely inside ONE real
    PF's ownership. That is the §4.5 law at DEV grain — the binding
    contradicts the code — so the same ladder applies: the dev re-homes
    only on the conservation ACCEPT bar (strict span-LOC majority AND at
    least half the counted entries, over :func:`member_votes` of the
    dev's OWN flows). No signal → the dev stays put (an honest shared
    resident never moves on a guess).

    Guards: developer layer only; facets never move (§4.1); workspace
    anchors never move (their flow sample spans the whole workspace, not
    one capability); only devs with >= 1 flow ENTERING inside their own
    paths qualify (a dev whose flows all enter elsewhere is a passive
    library, not a product surface). Deterministic; mutates
    ``product_feature_id`` in place; returns telemetry.
    Kill-switch: ``FAULTLINE_SPINE_DEV_REHOME=0``.
    """
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        _is_workspace_anchor,
    )

    tele: dict[str, Any] = {
        "enabled": dev_rehome_enabled(), "checked": 0, "rehomed": 0,
        "sample": [],
    }
    if not dev_rehome_enabled():
        return tele

    pf_keys = frozenset(
        str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")
        for pf in product_features
    ) - {""}
    file_pf_owner = build_file_pf_owner(
        dev_views_for(developer_features), real_pf_keys=pf_keys,
    )

    for dev in developer_features:
        if getattr(dev, "layer", "developer") != "developer":
            continue
        if getattr(dev, "role", None) == "facet":
            continue
        pfid = getattr(dev, "product_feature_id", None)
        if not pfid or str(pfid).strip().lower() not in _SHARED_PF_KEYS:
            continue
        flows = list(getattr(dev, "flows", None) or [])
        if not flows or _is_workspace_anchor(dev):
            continue
        own_paths = {_norm(str(p)) for p in (getattr(dev, "paths", None) or [])}
        if not any(
            (e := _entry_file_of(fl)) and _norm(e) in own_paths
            for fl in flows
        ):
            continue  # passive library — not a product surface
        tele["checked"] += 1
        span_votes, entry_votes = member_votes(flows, file_pf_owner)
        if not span_votes:
            continue  # no owned-code signal — honest shared resident
        total_span = sum(span_votes.values())
        total_entry = sum(entry_votes.values())
        best = sorted(
            span_votes,
            key=lambda k: (-span_votes[k], -entry_votes.get(k, 0), k),
        )[0]
        if span_votes[best] * 2 <= total_span:
            continue  # no strict majority — leave for richer ladders
        if total_entry and entry_votes.get(best, 0) * 2 < total_entry:
            continue
        dev.product_feature_id = best
        tele["rehomed"] += 1
        if len(tele["sample"]) < 20:
            tele["sample"].append({"dev": dev.name, "pf": best})
    return tele
