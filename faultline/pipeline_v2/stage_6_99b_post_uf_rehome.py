"""Stage 6.99b — post-UF PF-homing hygiene rehome (B69-v2).

THE DISEASE (papermark keyed exhibit, B69 phase-1/phase-2 forensics): a
twin-slug ``_merge_anchors`` coalesce hands a generic/foreign route anchor a
DEEP poisoned prefix, so at Stage 6.86 the ENTRY-fold rung ("most specific
match wins") lets that anchor annex a dev on ONE flow-entry file even though
a competing minting anchor echo-matches the dev's owned files
multiplicatively wider (papermark: 'faq' folds dev 'datarooms' on 1/64 files
while the 'dataroom' anchor holds 25/64 = 39%). Journey-layer heirs then wear
the wrong home: a ``route_group_recall`` seed of pure dataroom pages lands
under PF 'faqs' with a PF-noun name ('View faqs').

WHY POST-UF AND NOT MINT-TIME (the banked fix/b69-pf-homing lesson): a
mint-time re-home cascades — ``route_group_recall`` consumes the dev→PF
assignment through BOTH its seed-plurality vote AND its touched-set hole
detection, so one dev rebind re-seeds groups, invalidates the uf-refine and
persona LLM caches, and redraws the whole board (39/38 row churn from
redirect=1 on the keyed A/B). This rail runs at 6.99b — AFTER journeys,
seeds, naming, the labeler, and B31 — so the ONLY rows that can change are
the rows it explicitly touches (surgical blast radius; the B33 post-UF
doctrine: mint-time bars/moves break conservation, post-UF surgery does not).

WHY NOT THE I16 RULER: Stage 6.99 (``stage_6_99_i16_rehome``) judges by the
``path_index → dev → product_feature_id`` owner map — which is POISONED by
the very annexation being cured (the annexed pages genuinely belong to the
mis-assigned dev, so they LOOK home-owned and I16 stays blind). This rail
judges by ANCHOR PREFIX BREADTH over the journey's member entry files —
mint-time structural truth, exported by Stage 6.86 as a side-channel
(``homing_hygiene_anchor_registry``) that never reaches the output JSON.

THE RULER (banked Seg A, re-used verbatim): re-home only when the current
home's anchor matches a MINORITY of the row's member entries
(``home_share < _THETA``) AND a competing minted PF's anchor matches a
multiplicatively-wider share (``rival >= _RATIO × home`` AND
``rival >= _UNION_FLOOR`` — the same random-tail floor validator I15 gates
on). SCALE-INVARIANT ratios over the row's own member set — no per-repo
constants (rule-no-magic-tuning).

ACTION SCOPE — SYNTHESIZED rows only (the mandate's disease class: the
PF-noun seed machinery mis-homed by the mint fold). An organic / lattice
row tripping the same ruler is recorded as a telemetry-only candidate
(``organic_candidates``): the cal.com control drive surfaced 5 such rows
(an echo-PF 'features' debt class) and moving LLM-drawn journeys belongs
to the I16/B24 family, not this rail — the control repo's strict-no-op
gate is part of the mandate.

ACTIONS:
* fold-into-existing — when the receiver PF holds an ORGANIC journey that
  already cites one of the row's member flows, wears the same folded name,
  or holds the same singular-folded resource, the row's members fold into
  it and the row dissolves (a rehomed seed must not shadow the journey
  that legitimately covers the surface).
* rehome — otherwise the row moves (``product_feature_id``) and is renamed
  from its OWN group resource via the journey templates (rename-on-rehome,
  C′). The rename is law-gated (``display_law_violations`` — including the
  B69-v2 bare-verb/dev-grain-token law) and collision-gated against the
  FINAL board; a failed rename keeps the old name and is counted honestly.

NAMED GUARDS (unit anti-cases, phase-1 §4 + v2 forensics):
* θ-guard — a home holding a member majority is legitimate (UF-013 'Create
  and manage data rooms' holds real dataroom-scoped faqs routes: NOT moved).
* signal-without-target — no rival above the floor ⇒ do NOT invent a move
  (mupdf/misc-API seeds: barred anchors never mint, so no target exists;
  the B49 lesson).
* I8 orphan guard — never strip the source PF's last journey (mirror of the
  I16 rail's guard).
* lane/None — a lane or non-minted home has no anchor in the registry and
  is skipped; lane rows are never re-home targets either.

Default OFF (``FAULTLINE_HOMING_HYGIENE`` unset) ⇒ the rail never runs ⇒
byte-identical output. $0 — deterministic, no LLM, no I/O.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping

from faultline.pipeline_v2.naming_contract import (
    _degrime_sing,
    _flow_verb_verdict,
    _resource_phrase,
    _verb_class_tokens,
    display_law_violations,
    homing_hygiene_enabled,
    load_naming_vocab,
    polish_display_casing,
)

#: θ — the legitimate-home majority threshold (the mint's own constant).
_THETA = 0.5
#: Random-tail floor — the SAME constant validator I15 / mint rider R2 use.
_UNION_FLOOR = 0.34
#: Multiplicative breadth ratio — the rival must be at least this multiple
#: of the home's share (papermark exhibit fires at 24×; 3× keeps a wide
#: safety margin while staying scale-invariant).
_BREADTH_RATIO = 3.0

__all__ = [
    "homing_hygiene_enabled",
    "run_post_uf_rehome",
]


def _attr(o: Any, name: str) -> Any:
    return o.get(name) if isinstance(o, dict) else getattr(o, name, None)


def _pf_key(pf: Any) -> str:
    return str(_attr(pf, "id") or _attr(pf, "name") or "")


def _pf_display(pf: Any) -> str:
    return str(_attr(pf, "display_name") or _attr(pf, "name") or "")


def _folded(name: Any) -> str:
    return str(name or "").strip().lower()


def _sing_tokens(*texts: str) -> set[str]:
    """Singular-folded word tokens of the given texts (the B50 fold — the
    same normalization Law-A's echo guard uses)."""
    out: set[str] = set()
    for t in texts:
        for w in re.split(r"[^a-z0-9]+", str(t or "").lower()):
            if w:
                out.add(_degrime_sing(w))
    return out


def _home_noun_echo(
    uf: Any, home_pf: Any, flow_by_uuid: Mapping[str, Any],
) -> bool:
    """The phase-1 §3 'different-domain members' predicate, mechanized: a
    row whose OWN resource or member-flow NAMES echo the home PF's noun is
    home-TIED — its members belong to the home's domain even when they live
    inside another PF's route subtree (papermark 'Manage dataroom FAQs':
    faq-CRUD flows under the datarooms API subtree — dataroom-SCOPED faqs
    legally home to PF faqs; the offline drive proved the bare breadth
    ruler would move them). Only a row with NO structural match AND NO noun
    tie is annexed booty."""
    home_toks = _sing_tokens(
        str(_attr(home_pf, "name") or ""), _pf_display(home_pf))
    if not home_toks:
        return False
    row_toks = _sing_tokens(str(_attr(uf, "resource") or ""))
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(str(fid))
        if fl is not None:
            row_toks |= _sing_tokens(str(_attr(fl, "name") or ""))
    return bool(home_toks & row_toks)


def _member_entries(uf: Any, flow_by_uuid: Mapping[str, Any]) -> list[str]:
    """Member-flow entry files (deduped, order-preserving) — the same
    evidence unit the I16 ruler counts, judged here by anchor breadth."""
    out: list[str] = []
    seen: set[str] = set()
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(str(fid))
        ep = str(_attr(fl, "entry_point_file") or "") if fl is not None else ""
        if ep and ep not in seen:
            seen.add(ep)
            out.append(ep)
    return out


def _rename_on_rehome(
    uf: Any,
    receiver_pf: Any,
    flow_by_uuid: Mapping[str, Any],
    vocab: Mapping[str, Any],
    taken: dict[str, str],
) -> str | None:
    """C′ — deterministic rename of a rehomed SYNTHESIZED row from its OWN
    group resource ('View faqs' seed of dataroom pages, resource='datarooms'
    → 'View datarooms'). Returns the new display or ``None`` when the row
    keeps its name (no resource, render law-dirty — including the B69-v2
    bare-verb/dev-grain-token law — collision, or a no-op)."""
    resource = str(_attr(uf, "resource") or "").strip()
    if not resource:
        return None
    # A resource that is itself a verb-class token names an ACTION, not a
    # thing ('manage' from .../folders/manage) — templating it yields
    # 'Manage manage' / 'View manage' garbage; refuse (honest keep).
    verb_toks = _verb_class_tokens(vocab)
    if all(t in verb_toks
           for t in re.split(r"[-_\s]+", resource.lower()) if t):
        return None
    member_names = [
        str(_attr(flow_by_uuid.get(str(fid)), "name") or "")
        for fid in (_attr(uf, "member_flow_ids") or [])
        if flow_by_uuid.get(str(fid)) is not None
    ]
    verdict = _flow_verb_verdict(member_names, vocab)
    templates: Mapping[str, Any] = vocab.get("journey_templates") or {}
    tmpl = (templates.get("generic") or {}).get(verdict) or "Manage {r}"
    own = re.sub(r"[-_]+", " ", resource).strip()
    phrase = _resource_phrase(own, vocab)
    if not phrase:
        return None
    cand = polish_display_casing(tmpl.replace("{r}", phrase), vocab)
    old = str(_attr(uf, "name") or "")
    # Structural degeneracy guard (independent of the display laws): a
    # render that collapses to a single word or echoes its verb into the
    # resource ('Manage manage' from a route-group literally named
    # 'manage') is never a name — the bare-'Manage' class of the banked
    # branch must be impossible at THIS author too.
    toks = [t for t in _folded(cand).split() if t]
    if (len(toks) < 2
            or any(a == b for a, b in zip(toks, toks[1:]))):
        return None
    if (not cand or _folded(cand) == _folded(old)
            or _folded(cand) in taken
            or display_law_violations(
                cand, vocab, pf_display=_pf_display(receiver_pf) or None)):
        return None
    return cand


def run_post_uf_rehome(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
    anchor_registry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Re-home anchor-breadth-foreign journeys to their structurally wider
    home; fold into an organic receiver twin when one exists. Mutates
    ``user_flows`` (``product_feature_id`` / ``name`` / membership; may
    remove folded rows) in place. Returns telemetry (always — the cal.com
    strict-no-op gate needs proof the rail evaluated and stayed inert)."""
    tele: dict[str, Any] = {
        "enabled": True, "checked": 0, "rehomed": 0, "folded": 0,
        "renamed": 0, "rename_kept": 0, "orphan_guarded": 0,
        "signal_no_target": 0, "moves": [], "folds": [], "renames": [],
    }
    if not anchor_registry:
        tele["skipped"] = "no_anchor_registry"
        return tele

    vocab = load_naming_vocab()
    flow_by_uuid: dict[str, Any] = {}
    for f in features:
        for fl in (_attr(f, "flows") or []):
            u = _attr(fl, "uuid")
            if u:
                flow_by_uuid[str(u)] = fl

    pf_by_key: dict[str, Any] = {}
    anchor_by_pf: dict[str, Any] = {}
    for pf in product_features:
        key = _pf_key(pf)
        if not key:
            continue
        pf_by_key[key] = pf
        a = anchor_registry.get(str(_attr(pf, "anchor_id") or ""))
        if a is not None:
            anchor_by_pf[key] = a

    # I8 orphan guard state (mirror of the I16 rail).
    uf_count: Counter = Counter(
        str(_attr(u, "product_feature_id") or "") for u in user_flows
        if _attr(u, "product_feature_id"))
    # Final-board display registry for the collision-gated rename.
    taken: dict[str, str] = {}
    for u in user_flows:
        taken.setdefault(_folded(_attr(u, "name")), str(_attr(u, "id") or ""))

    # Pass 1 — plan (pure); Pass 2 — apply. Planning over a stable board
    # keeps the rail deterministic and side-effect-ordered.
    plans: list[dict[str, Any]] = []
    for uf in user_flows:
        pfid = str(_attr(uf, "product_feature_id") or "")
        if not pfid:
            continue
        home_anchor = anchor_by_pf.get(pfid)
        if home_anchor is None:
            continue  # lane / non-minted home — never judged here
        entries = _member_entries(uf, flow_by_uuid)
        n = len(entries)
        if not n:
            continue
        tele["checked"] += 1
        home_share = len(home_anchor.matched_set(entries)) / n
        if home_share >= _THETA:
            continue  # θ-guard: the home owns a member majority (UF-013)
        if home_share > 0.0:
            # home-evidence guard: the home anchor structurally matches at
            # least ONE member entry — the row holds home-native evidence
            # ('View dataroom analytics and audit log' carries faqs' own
            # app/(ee)/api/faqs route). A row is annexed booty only with
            # ZERO home tie; under-firing beats an invented move (B49).
            tele["home_evidence_guarded"] = (
                tele.get("home_evidence_guarded", 0) + 1)
            continue
        if _home_noun_echo(uf, pf_by_key.get(pfid), flow_by_uuid):
            # noun-echo guard (phase-1 §3 different-domain predicate):
            # member flow names / own resource echo the home PF's noun —
            # dataroom-SCOPED faqs stay under faqs ('Manage dataroom FAQs').
            tele["home_echo_guarded"] = (
                tele.get("home_echo_guarded", 0) + 1)
            continue
        rival_key: str | None = None
        rival_share = 0.0
        for key in sorted(anchor_by_pf):
            if key == pfid:
                continue
            s = len(anchor_by_pf[key].matched_set(entries)) / n
            if s > rival_share:
                rival_key, rival_share = key, s
        if (rival_key is None
                or rival_share < _UNION_FLOOR
                or rival_share < _BREADTH_RATIO * home_share):
            if rival_share > 0.0 or home_share < _THETA:
                # minority home, but no decisively wider owner exists —
                # the mupdf class: signal without a target, do NOT move.
                tele["signal_no_target"] += 1
            continue
        if not bool(_attr(uf, "synthesized")):
            # ACTION SCOPE = synthesized rows only (the mandate's disease
            # class: PF-noun seed machinery mis-homed by the mint fold).
            # An organic/lattice row tripping the same ruler is recorded
            # as a telemetry-only candidate — the cal.com control drive
            # showed 5 such rows (an echo-PF 'features' debt class), and
            # moving LLM-drawn journeys belongs to the I16/B24 family, not
            # this rail (strict-no-op control gate; B49: never invent a
            # move outside the proven exhibit class).
            tele["organic_candidates"] = (
                tele.get("organic_candidates", 0) + 1)
            if len(tele.setdefault("organic_candidate_rows", [])) < 10:
                tele["organic_candidate_rows"].append({
                    "uf": str(_attr(uf, "id") or ""),
                    "name": str(_attr(uf, "name") or ""),
                    "from": pfid, "to": rival_key,
                    "home_share": round(home_share, 3),
                    "rival_share": round(rival_share, 3),
                })
            continue
        plans.append({
            "uf": uf, "from": pfid, "to": rival_key,
            "home_share": round(home_share, 3),
            "rival_share": round(rival_share, 3),
        })

    for plan in plans:
        uf = plan["uf"]
        pfid, target = plan["from"], plan["to"]
        if uf_count[pfid] <= 1:
            tele["orphan_guarded"] += 1
            continue  # never strip the source PF's last journey (I8)
        uid = str(_attr(uf, "id") or "")
        member_ids = [str(m) for m in (_attr(uf, "member_flow_ids") or [])]
        # fold-into-existing: an ORGANIC receiver journey that already cites
        # one of the row's member flows, wears the same folded name, or
        # holds the SAME singular-folded resource (papermark: the rehomed
        # 'View faqs' seed, resource='datarooms', dissolves into organic
        # 'Browse and filter datarooms', resource='dataroom' — the journey
        # that legitimately covers the surface). Rungs ranked: member
        # overlap > name twin > resource twin; first receiver row in board
        # order wins (deterministic).
        organic = [
            other for other in user_flows
            if other is not uf and not bool(_attr(other, "synthesized"))
            and str(_attr(other, "product_feature_id") or "") == target
        ]
        fold_target = None
        member_set = set(member_ids)
        for other in organic:  # rung 1 — member overlap
            if {str(m) for m in (_attr(other, "member_flow_ids") or [])} \
                    & member_set:
                fold_target = other
                break
        if fold_target is None:  # rung 2 — name twin
            for other in organic:
                if _folded(_attr(other, "name")) == \
                        _folded(_attr(uf, "name")):
                    fold_target = other
                    break
        if fold_target is None:  # rung 3 — resource twin
            row_res = _sing_tokens(str(_attr(uf, "resource") or ""))
            if row_res:
                for other in organic:
                    if _sing_tokens(
                            str(_attr(other, "resource") or "")) == row_res:
                        fold_target = other
                        break
        if fold_target is not None:
            existing = [
                str(m) for m in (_attr(fold_target, "member_flow_ids") or [])]
            merged = existing + [m for m in member_ids if m not in existing]
            fold_target.member_flow_ids = merged
            fold_target.member_count = len(merged)
            user_flows.remove(uf)
            uf_count[pfid] -= 1
            tele["folded"] += 1
            tele["folds"].append({
                "uf": uid, "name": str(_attr(uf, "name") or ""),
                "into": str(_attr(fold_target, "id") or ""),
                "from": pfid, "to": target,
            })
            continue
        uf.product_feature_id = target
        uf_count[pfid] -= 1
        uf_count[target] += 1
        tele["rehomed"] += 1
        tele["moves"].append({
            "uf": uid, "name": str(_attr(uf, "name") or ""),
            "from": pfid, "to": target,
            "home_share": plan["home_share"],
            "rival_share": plan["rival_share"],
        })
        # C′ — rename-on-rehome, SYNTHESIZED rows only (the PF-noun-named
        # seed class); organic rows keep their LLM-drawn names.
        if bool(_attr(uf, "synthesized")):
            new = _rename_on_rehome(
                uf, pf_by_key.get(target), flow_by_uuid, vocab, taken)
            if new is not None:
                old = str(_attr(uf, "name") or "")
                uf.name = new
                taken[_folded(new)] = uid
                tele["renamed"] += 1
                tele["renames"].append(
                    {"uf": uid, "before": old, "after": new})
            else:
                tele["rename_kept"] += 1
    return tele
