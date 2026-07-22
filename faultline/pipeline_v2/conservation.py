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
import re
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature, UserFlow

__all__ = [
    "SPINE_CONSERVATION_ENV",
    "SPINE_DEV_REHOME_ENV",
    "HOME_AFFINITY_GATE_ENV",
    "conservation_enabled",
    "dev_rehome_enabled",
    "home_affinity_gate_enabled",
    "build_file_pf_owner",
    "dev_views_for",
    "member_votes",
    "conserved_pfid",
    "apply_uf_conservation",
    "apply_home_affinity_gate",
    "build_orphan_flow_gaps",
    "rehome_shared_flowful_devs",
]

SPINE_CONSERVATION_ENV = "FAULTLINE_SPINE_CONSERVATION"
SPINE_DEV_REHOME_ENV = "FAULTLINE_SPINE_DEV_REHOME"
#: B78 homing pack (Seg B/C/D — one system) kill-switch. Default OFF; when
#: armed it enables (Seg B) the conservation home-affinity gate here, (Seg C)
#: the organic-move v3 guards in stage 6.99b, and (Seg D) the mega vacuum
#: census + re-home proposals. Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS`` (append-only, no KEY_SCHEMA bump —
#: the bump rides the separate flip commit). Unset / ``"0"`` / false ⇒ every
#: segment is inert and the scan is byte-identical to main (KS 4-way gate).
HOME_AFFINITY_GATE_ENV = "FAULTLINE_HOME_AFFINITY_GATE"

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


def home_affinity_gate_enabled() -> bool:
    """B78 homing pack (Seg B/C/D) — default **OFF**. Unset / ``"0"`` /
    false ⇒ the affinity gate here, the 6.99b organic-move v3 guards, and
    the mega vacuum census are all inert and the scan is byte-identical to
    main (the KS 4-way kill-switch)."""
    return os.environ.get(HOME_AFFINITY_GATE_ENV, "").strip().lower() in {
        "1", "true",
    }


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


# ── Ownership resolution ────────────────────────────────────────────────


def build_file_pf_owner(
    dev_views: Iterable[dict[str, Any]],
    real_pf_keys: frozenset[str] | None = None,
    *,
    file_owner: dict[str, str] | None = None,
) -> dict[str, str]:
    """``{file: pf_key}`` over the developer features' OWNED paths.

    ``dev_views`` rows carry ``name`` / ``paths`` / ``product_feature_id``
    / ``role`` (dict views or attribute objects both accepted). Facet
    devs (§4.1) and devs bound to the shared bucket contribute nothing —
    their files must not vote. When ``real_pf_keys`` is given, only PFs
    in that set count (guards against stale pfids). First claimant wins
    on the rare shared path (input order is stable upstream).

    S1 owner-oracle: when ``file_owner`` is provided
    (``FAULTLINE_OWNER_ORACLE`` on) it IS the file→PF map — the SAME
    deterministic election Stage 6.97 runs, already carrying the facet /
    shared / stale-pfid coverage view
    (:func:`owner_oracle.OwnerElection.file_pf_owner_map`). It replaces the
    order-sensitive first-claimant tally so a contested file votes for its
    ELECTED owner's PF, not the first-listed dev's. ``None`` (default / flag
    off) → the shipped first-claimant tally → byte-identical.
    """
    if file_owner is not None:
        return file_owner
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


#: Node roles whose spans are LABELED SHARING surfaces, never ownership
#: votes (W4 §4.6): ``interior`` — a product component the entry page
#: renders, attributed in the component owner's file; ``shared`` — high
#: fan-in infrastructure (excluded from core LOC by the model's own
#: contract). Counting them dragged UFs toward component-owning PFs on
#: the 2026-07-07 supabase smoke (I15 lane-aware 0.875 -> 0.25, I16
#: 8.3% -> 62.9%) — evidence of composition is not evidence of home.
_NON_VOTING_NODE_ROLES = frozenset({"interior", "shared"})


def _flow_span_weights(flow: Any) -> dict[str, int]:
    """``{file: span_lines}`` for one member flow.

    Preference order:
      1. graph NODES with line spans, EXCLUDING the labeled-sharing
         roles (``interior`` / ``shared``) — the flow's OWN narrative
         body is what votes;
      2. legacy ``line_ranges`` (pre-node scans only — no role info
         exists there, and no interior spans either);
      3. one vote per member file (file-grain fallback).
    """
    weights: dict[str, int] = {}
    nodes = _flow_get(flow, "nodes") or []
    any_lined_node = False
    for nd in nodes:
        get = nd.get if isinstance(nd, dict) else (
            lambda k, _n=nd: getattr(_n, k, None))
        lines = get("lines")
        if not lines or len(lines) != 2:
            continue
        any_lined_node = True
        if str(get("role") or "") in _NON_VOTING_NODE_ROLES:
            continue
        path = get("file")
        if not path:
            continue
        try:
            span = max(int(lines[1]) - int(lines[0]) + 1, 1)
        except (TypeError, ValueError):
            continue
        weights[_norm(str(path))] = weights.get(_norm(str(path)), 0) + span
    if weights:
        return weights
    if not any_lined_node:
        # Legacy scans without a node graph: line_ranges carry no
        # interior spans by construction, so they may vote.
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
    container_pf_keys: frozenset[str] | None = None,
) -> tuple[str | None, bool]:
    """Apply the conservation ladder to ONE user flow.

    Returns ``(pf_key, resettled)`` — ``pf_key`` may equal the incumbent
    (law satisfied / no signal), a different real PF (resettled), or
    ``None`` (shared incumbent with no signal under the finalize mode).

    ``container_pf_keys`` (B77 Seg 4, ``FAULTLINE_RESIDUAL_CITABILITY``):
    monorepo ws-pkg CONTAINER PFs are packaging, never a journey home —
    a container is NOT a valid rung-3 resettle target (the 502m bypass:
    377/502 votes of container-INHERITED members majority-shipped the
    journey onto the ws-container-PF, around the Seg-C "no journey lives
    in a container" guard). When every voted candidate is a container the
    ladder falls through to rung-4 semantics (existing doctrine: keep the
    incumbent for the richer downstream ladders / null a shared binding
    under the finalize mode). ``None`` (default / flag off) → the shipped
    ladder, byte-identical.
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
    pool = set(span_votes) | set(entry_votes)
    if container_pf_keys:
        pool -= container_pf_keys
        if not pool:
            # B77 Seg 4 — every voted candidate is a ws-container:
            # packaging signal only. Rung-4 semantics (no valid target).
            if incumbent_shared and null_shared_without_signal:
                return None, True
            return incumbent_key, False
    candidates = sorted(
        pool,
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

    # B77 Seg 4 (FAULTLINE_RESIDUAL_CITABILITY) — ws-container PFs are
    # never a valid resettle target (packaging, not a home). Derived with
    # the same "ws:" anchor marker Seg C uses; ``None`` when the flag is
    # unset (or no containers exist) keeps the ladder byte-identical.
    container_keys: frozenset[str] | None = None
    from faultline.pipeline_v2.stage_6_7c_uf_splitter import (
        residual_citability_enabled,
    )
    if residual_citability_enabled():
        from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
            _container_pf_keys,
        )
        container_keys = _container_pf_keys(product_features) or None

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
            container_pf_keys=container_keys,
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


# ── B78 Seg B — home-affinity gate (non-dev channel) ────────────────────
#
# THE DISEASE (B78 forensics-canon, §B78 ledger): conservation rung-1 votes
# file→dev.product_feature_id, so when a mint-time vacuum PF ANNEXED a
# journey's member files the vote inherits the poison and the ladder
# silently settles the journey onto the thief — 59.2% of the corpus is
# ``tok0`` (the journey NAME shares zero content tokens with its home) and
# 503 UF carry a deterministic BETTER-HOME. This gate re-reads the home on a
# channel the annexation cannot poison — the journey's own NAME tokens and
# the candidate PFs' STRUCTURAL path breadth (NEVER the dev→PF owner map) —
# and, instead of accepting the poisoned vote silently, emits an ARBITER
# proposal (S3 ledger, rung ``affinity-rehome``) to the better home.
#
# Non-circular by construction (probe-canon circular-ruler trap): a vacuum
# can steal FILES (poisoning file→PF and thus a home's path share) but it
# cannot make a wrong-named PF share the journey's NAME tokens, and the path
# rail demands a rival with >= 2x the home's own entry share — a genuine
# structural owner, not the first-claimant thief. Both rails ARE the
# operator's census lines (tok0 / better-home) verbatim, so the cured class
# equals the measured class.

#: Universal English function words (scale-invariant — not per-repo tuning);
#: stripped from a journey's content tokens alongside the vocab verb classes.
_AFFINITY_FUNCTION_WORDS = frozenset({
    "and", "or", "the", "a", "an", "of", "for", "with", "in", "on", "to",
    "by", "from", "at", "as", "is", "its", "new", "existing", "all", "your",
    "this", "that", "into", "via", "per", "no",
})


def _aff_sing(t: str) -> str:
    """Census ``sing()`` — the plural fold the operator's tok0/better-home
    lines use (kept identical so the cured class == the measured class)."""
    return t[:-1] if t.endswith("s") and len(t) > 3 else t


def _aff_tokens(*texts: str) -> set[str]:
    """All singular-folded word tokens (len>=2) — the PF-side vocabulary
    (census ``toks``: no verb strip, so a PF named 'Cases' keeps 'case')."""
    out: set[str] = set()
    for t in texts:
        for w in re.split(r"[^a-z0-9]+", str(t or "").lower()):
            if len(w) >= 2:
                out.add(_aff_sing(w))
    return out


def _aff_content_tokens(text: str, verb_toks: frozenset[str]) -> set[str]:
    """UF-side content tokens — ``_aff_tokens`` minus the vocab verb classes
    and the function words (census ``content_toks``). The verb set is derived
    from the naming vocab (mechanism over curated YAML, never a hardcoded
    list — rule-no-magic-tuning)."""
    return {
        t for t in _aff_tokens(text)
        if t not in verb_toks and t not in _AFFINITY_FUNCTION_WORDS
    }


def _better_home(
    uf_content: set[str],
    home_key: str,
    entries: list[str],
    pf_tokens: dict[str, set[str]],
    pf_paths: dict[str, set[str]],
) -> str | None:
    """The deterministic better-home for a ``tok0`` journey (census v2):

      * bh_tok — a DIFFERENT PF whose NAME tokens cover >= 50% of the
        journey's content tokens (name affinity the vacuum cannot fake);
      * bh_path — a DIFFERENT PF whose path-set holds a strictly-dominant
        share of the member entry files (>= 0.5 AND >= 2x the home's share).

    Name affinity wins (least poisonable channel); the path rail is the
    fallback. ``None`` when neither names a wider owner. Deterministic
    (sorted scan; first max wins)."""
    best_tok: tuple[float, str | None] = (0.0, None)
    if uf_content:
        for k in sorted(pf_tokens):
            if k == home_key:
                continue
            cov = len(uf_content & pf_tokens[k]) / len(uf_content)
            if cov > best_tok[0]:
                best_tok = (cov, k)
    if best_tok[1] is not None and best_tok[0] >= 0.5:
        return best_tok[1]
    if entries:
        n = len(entries)
        home_share = (
            sum(1 for e in entries if e in pf_paths.get(home_key, set())) / n
        )
        best_path: tuple[float, str | None] = (0.0, None)
        for k in sorted(pf_paths):
            if k == home_key:
                continue
            sh = sum(1 for e in entries if e in pf_paths[k]) / n
            if sh > best_path[0]:
                best_path = (sh, k)
        if (best_path[1] is not None and best_path[0] >= 0.5
                and best_path[0] >= 2 * home_share):
            return best_path[1]
    return None


def apply_home_affinity_gate(
    user_flows: list["UserFlow"],
    developer_features: list["Feature"],
    product_features: list["Feature"],
) -> dict[str, Any]:
    """B78 Seg B — re-home ``tok0`` journeys that carry a deterministic
    better home, via an S3 arbiter proposal (rung ``affinity-rehome``).
    Mutates ``product_feature_id`` in place (through the ledger); returns
    telemetry. OFF (flag unset) ⇒ never called ⇒ byte-identical to main.

    Anti-cases (unit-pinned): a journey whose home shares >= 1 content token
    is NOT ``tok0`` and is never touched ('Browse, filter, and manage cases'
    keeps 'cases'); a shared / container / facet home or such a target is
    refused; the source PF's LAST journey is never stripped (no-orphan, the
    B77 law); a synthesized backstop journey is skipped (conservation-clean
    by construction)."""
    tele: dict[str, Any] = {
        "enabled": home_affinity_gate_enabled(), "checked": 0,
        "proposed": 0, "tok0": 0, "orphan_guarded": 0, "moves": [],
    }
    if not home_affinity_gate_enabled() or not user_flows:
        return tele

    from faultline.pipeline_v2.naming_contract import (
        _verb_class_tokens,
        load_naming_vocab,
    )
    verb_toks = frozenset(_verb_class_tokens(load_naming_vocab()))

    # Container PFs (packaging, never a journey home — the same ``ws:`` marker
    # conservation's rung-3 guard uses) are excluded as better-home targets.
    container_keys: frozenset[str] = frozenset()
    from faultline.pipeline_v2.stage_6_7c_uf_splitter import (
        residual_citability_enabled,
    )
    if residual_citability_enabled():
        from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
            _container_pf_keys,
        )
        container_keys = _container_pf_keys(product_features) or frozenset()

    def _pf_key(pf: Any) -> str:
        return str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")

    # Candidate homes/targets: real product PFs only (no shared/container).
    pf_tokens: dict[str, set[str]] = {}
    pf_paths: dict[str, set[str]] = {}
    for pf in product_features:
        key = _pf_key(pf)
        if not key or key.strip().lower() in _SHARED_PF_KEYS:
            continue
        if key in container_keys:
            continue
        pf_tokens[key] = _aff_tokens(
            getattr(pf, "name", "") or "",
            getattr(pf, "display_name", "") or "",
        )
        pf_paths[key] = {
            _norm(str(p)) for p in (getattr(pf, "paths", None) or [])
        }

    flow_by_id: dict[str, Any] = {}
    for d in developer_features:
        for fl in getattr(d, "flows", None) or []:
            for k in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
                if k and k not in flow_by_id:
                    flow_by_id[k] = fl

    uf_count: dict[str, int] = {}
    for uf in user_flows:
        h = str(getattr(uf, "product_feature_id", None) or "")
        if h:
            uf_count[h] = uf_count.get(h, 0) + 1

    from faultline.pipeline_v2.overturn_ledger import propose_pf_now

    for uf in user_flows:
        if getattr(uf, "synthesized", False):
            continue
        home = str(getattr(uf, "product_feature_id", None) or "")
        if not home or home not in pf_tokens:
            continue  # shared / container / lane / unknown home — not ours
        uf_ct = _aff_content_tokens(getattr(uf, "name", "") or "", verb_toks)
        if not uf_ct:
            continue  # verb-only / empty name — no content signal
        if uf_ct & pf_tokens[home]:
            continue  # NOT tok0 — home is name-tied, never touched
        tele["tok0"] += 1
        tele["checked"] += 1
        entries: list[str] = []
        seen: set[str] = set()
        for mid in (getattr(uf, "member_flow_ids", None) or []):
            fl = flow_by_id.get(mid)
            ep = _entry_file_of(fl) if fl is not None else None
            if ep:
                e = _norm(str(ep))
                if e not in seen:
                    seen.add(e)
                    entries.append(e)
        target = _better_home(uf_ct, home, entries, pf_tokens, pf_paths)
        if target is None or target == home:
            continue
        if uf_count.get(home, 0) <= 1:
            tele["orphan_guarded"] += 1
            continue  # no-orphan (B77) — never strip a PF's last journey
        propose_pf_now(uf, target, rung="affinity-rehome")
        uf_count[home] -= 1
        uf_count[target] = uf_count.get(target, 0) + 1
        tele["proposed"] += 1
        if len(tele["moves"]) < 40:
            tele["moves"].append({
                "uf": str(getattr(uf, "id", "") or ""),
                "name": str(getattr(uf, "name", "") or ""),
                "from": home, "to": target,
            })
    return tele


# ── B78-it2 Goal 1 — orphan-flow gap stamp (Seg B rider, Stage 6.995) ───
#
# THE CLASS (B78-it2 forensics, Soc0 org-members): the LLM journey layer
# can dissolve a real journey ('Manage organization members', mc=4) and
# leave its member flows SILENTLY orphaned — ``user_flow_id=None``, homed
# only by an app-shell container dev — while the board reports nothing.
# The deterministic minimum (operator ruling, 2026-07-22): at the 6.995
# conservation checkpoint, a cohort of UF-less flows that carries
# page/product evidence becomes a labeled ``coverage_gaps[]`` claim
# (kind="orphan_flow") — journey-debt made visible, never silence. The
# regression VERDICT itself stays with the operator's confound-free keyed
# A/B; this stamp only breaks the silence.

#: UI-component entry extensions — the structural "page/product surface"
#: proxy (an interaction component authored for a view layer), the same
#: cross-stack class ``nav_parent._CODE_EXTS`` hardcodes. Not per-repo.
_ORPHAN_UI_EXTS = (".tsx", ".jsx", ".vue", ".svelte")

#: Framework-conventional feature-grouping dirs (the ``features/<name>/``
#: layout class) — structural convention, not a per-repo path.
_ORPHAN_GROUP_DIRS = frozenset({"features", "modules"})


def _entry_feature_dir(path: str) -> str | None:
    """The feature-DIR segment right after a grouping dir, when it is a
    directory (never the file itself): ``frontend/src/features/
    organization-members/MembersTab.tsx`` → ``organization-members``."""
    parts = str(path or "").replace("\\", "/").split("/")
    for i in range(len(parts) - 2):
        if parts[i] in _ORPHAN_GROUP_DIRS:
            return parts[i + 1]
    return None


def build_orphan_flow_gaps(
    user_flows: list["UserFlow"],
    developer_features: list["Feature"],
    product_features: list["Feature"],
) -> tuple[list[Any], dict[str, Any]]:
    """B78-it2 Goal 1 — one ``CoverageGap(kind="orphan_flow")`` per
    qualifying orphan-flow cohort. Pure read (no flow / UF / PF mutation);
    the caller appends the rows to the active B45 channel. OFF (flag
    unset) ⇒ ``([], tele)`` — byte-identical to main.

    A cohort = the UF-less flows (claimed by NO journey's
    ``member_flow_ids`` and carrying no ``user_flow_id``) whose UI-component
    entry files share one feature-dir. It qualifies when:

      * >= 2 distinct entry files (the R2 capability floor — one stray
        component is not journey material), AND
      * >= 1 member flow's name shares a content token with the dir (the
        mandate's OWN-resource evidence — 'manage-organization-members-flow'
        under ``features/organization-members/``).

    Anti-cases (unit-pinned): a flow any journey claims never stamps; a
    backend/plumbing orphan (non-UI entry) never stamps; a single-entry
    cohort never stamps; a cohort with no span evidence is skipped and
    counted (B45 law — a gap without spans is never emitted)."""
    tele: dict[str, Any] = {
        "enabled": home_affinity_gate_enabled(),
        "cohorts": 0, "flows": 0, "rows": [], "skipped_no_spans": 0,
    }
    rows: list[Any] = []
    if not home_affinity_gate_enabled():
        return rows, tele

    import hashlib

    from faultline.models.types import CoverageGap, FlowLineRange
    from faultline.pipeline_v2.naming_contract import (
        _verb_class_tokens,
        load_naming_vocab,
    )
    from faultline.pipeline_v2.stage_6_97b_uf_loc import union_span_len

    verb_toks = frozenset(_verb_class_tokens(load_naming_vocab()))

    claimed: set[str] = set()
    for uf in user_flows or []:
        for mid in getattr(uf, "member_flow_ids", None) or []:
            if mid:
                claimed.add(str(mid))

    cohorts: dict[str, list[Any]] = {}
    for dev in developer_features or []:
        for fl in getattr(dev, "flows", None) or []:
            if getattr(fl, "user_flow_id", None):
                continue
            keys = {
                str(k) for k in (
                    getattr(fl, "uuid", None), getattr(fl, "name", None),
                ) if k
            }
            if keys & claimed:
                continue
            ep = str(getattr(fl, "entry_point_file", "") or "")
            if not ep.endswith(_ORPHAN_UI_EXTS):
                continue
            fdir = _entry_feature_dir(ep)
            if not fdir:
                continue
            cohorts.setdefault(fdir, []).append(fl)

    pf_keys: set[str] = set()
    for pf in product_features or []:
        k = str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")
        if k:
            pf_keys.add(k)

    for fdir in sorted(cohorts):
        flows = sorted(
            cohorts[fdir], key=lambda f: str(getattr(f, "name", "") or ""))
        entries = sorted({
            str(getattr(f, "entry_point_file", "") or "") for f in flows
        })
        if len(entries) < 2:
            continue  # R2 floor — one component is not journey material
        dir_toks = _aff_tokens(fdir)
        own_resource = any(
            _aff_content_tokens(
                str(getattr(f, "name", "") or ""), verb_toks) & dir_toks
            for f in flows
        )
        if not own_resource:
            continue
        # Entry-file spans from the flows' own line_ranges (per-file union).
        span_ranges: dict[str, list[tuple[int, int]]] = {}
        for f in flows:
            ep = str(getattr(f, "entry_point_file", "") or "")
            for rec in getattr(f, "line_ranges", None) or []:
                p = str(getattr(rec, "path", "") or "")
                if p != ep:
                    continue
                s = int(getattr(rec, "start_line", 0) or 0)
                e = int(getattr(rec, "end_line", 0) or 0)
                if s and e >= s:
                    span_ranges.setdefault(p, []).append((s, e))
        if not span_ranges:
            tele["skipped_no_spans"] += 1
            continue  # B45 law — a gap without spans is never emitted
        spans = [
            FlowLineRange(
                path=p,
                start_line=min(s for s, _ in span_ranges[p]),
                end_line=max(e for _, e in span_ranges[p]),
            )
            for p in sorted(span_ranges)
        ]
        loc = sum(union_span_len(span_ranges[p]) for p in sorted(span_ranges))
        pf_ref = fdir if fdir in pf_keys else None
        label = f"Orphaned journey material: {fdir}"
        gid = "GAP-" + hashlib.sha1(
            f"{pf_ref or ''}|orphan_flow|{label}".encode("utf-8"),
        ).hexdigest()[:10]
        rows.append(CoverageGap(
            id=gid,
            product_feature_id=pf_ref,
            kind="orphan_flow",
            label=label,
            routes=[],
            surface_files=spans,
            loc=loc,
            synthesis_reason="orphan_flow_gap",
        ))
        tele["cohorts"] += 1
        tele["flows"] += len(flows)
        tele["rows"].append({
            "dir": fdir,
            "flows": [str(getattr(f, "name", "") or "") for f in flows][:8],
            "gap_id": gid,
            "loc": loc,
        })
    return rows, tele
