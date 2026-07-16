"""Seg C — UF synth quality laws L-C1..L-C4 (B71, ``FAULTLINE_NAMING_PACK``, OFF).

Census class C (67 echo/dup UF families): the synthesizer stamps ``verb + noun``
per cluster with no board name-census, so it echoes PF names, splits multiword
verbs, and duplicates same-noun-head families. Four laws, mechanism over the
engine's OWN template-verb vocab (never a new dictionary):

* **L-C1 echo-fold** — a UF whose noun-phrase equals its PF name (verb from the
  template set) is a bare echo; it FOLDS into the RICH canonical UF of the same
  PF (documenso ``User`` -> ``Manage users``, ``Teams`` -> ``Manage teams``;
  plane ``View propel``; cal ``Manage di``). Direction is fixed: the echo dies,
  the rich journey survives (census §4 anti-case-direction) — and a PF whose
  ONLY journey is the echo keeps it (never kill the last journey).
* **L-C2 verb-phrase integrity** — a multiword verb (``Set up``) is never split;
  a STUTTER (the second token is itself a verb — ``Manage create topic`` — or a
  broken particle splice — ``Browse up Slack`` / ``Create up inbox``) is repaired
  by dropping the stray token. Known multiword verbs (``set up``, ``sign in``…)
  are protected.
* **L-C3 same-noun-head families** — same-PF UFs sharing a noun-head (cal routing
  forms x5, novu topics x3) fold ONLY when their member span-sets overlap
  (coordinates decide, not the name).
* **L-C4 board uniqueness** — duplicate UF names on one board (cal ``Org`` x2)
  are collisions: folded (same PF + overlapping spans) or flagged to qualify.

PURE plan (no mutation); ``apply_uf_synth_fold`` applies it behind
``naming_pack_enabled`` — OFF/unset never runs, so UF names/membership are
byte-identical (the B40 byte-stable-name law holds when the flag is off; this is
a NAMING_PACK-gated, sanctioned name change when on). Folds union members
(conservation). The ON-path effect on user_flows[] conservation + the name-census
fall is the operator's keyed A/B to certify.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from faultline.pipeline_v2.census_join import flow_span_records, index_flows_by_uf

__all__ = [
    "MULTIWORD_VERBS",
    "PARTICLES",
    "noun_phrase",
    "is_pf_echo",
    "has_verb_stutter",
    "repair_stutter",
    "name_richness",
    "noun_head",
    "spans_overlap",
    "UfSynthPlan",
    "plan_uf_synth",
    "apply_uf_synth_fold",
]

#: Protected multiword verbs — ``(lead, particle)`` pairs that are ONE verb and
#: must never read as a stutter. Small, universal English phrasal-verb set (not a
#: per-repo list); corroborates the "don't split a multiword verb" law.
MULTIWORD_VERBS = frozenset({
    ("set", "up"), ("sign", "in"), ("sign", "out"), ("log", "in"),
    ("log", "out"), ("opt", "in"), ("opt", "out"), ("back", "up"),
    ("check", "in"), ("check", "out"), ("roll", "back"), ("lock", "in"),
})

#: Verb particles that only appear mid-phrase — a leading verb followed by one of
#: these (that is NOT a protected multiword verb) is a broken splice.
PARTICLES = frozenset({"up", "in", "out", "on", "off", "down"})

_WS = re.compile(r"\s+")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").strip()).lower()


def _toks(text: str) -> list[str]:
    return [t for t in _SPLIT.split((text or "").lower()) if t]


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok


def _tokset_stemmed(text: str) -> set[str]:
    return {_stem(t) for t in _toks(text)}


def noun_phrase(name: str, verbs: frozenset[str]) -> str:
    """The name with leading template-verb tokens stripped: ``Manage users`` ->
    ``users``, ``View propel`` -> ``propel``, ``User`` -> ``user`` (no verb)."""
    words = name.split()
    i = 0
    while i < len(words) and _norm(words[i]) in verbs:
        i += 1
    return " ".join(words[i:]) if i < len(words) else " ".join(words)


def is_pf_echo(
    name: str, pf_display: str, resource: str, verbs: frozenset[str],
) -> bool:
    """True when ``name`` is a bare echo of its PF: its noun-phrase (verb from
    the template set, or a bare noun) equals the PF display or the resource
    (stem-tolerant). Carries no own content token."""
    if not name:
        return False
    np = _tokset_stemmed(noun_phrase(name, verbs))
    if not np:
        return False
    pf = _tokset_stemmed(pf_display)
    res = _tokset_stemmed(resource)
    return (bool(pf) and np == pf) or (bool(res) and np == res)


def has_verb_stutter(name: str, verbs: frozenset[str]) -> bool:
    """True when the second token is a stray verb/particle — a multiword verb was
    split and recombined (``Browse up Slack``, ``Manage create topic``). Protected
    multiword verbs (``set up``…) are NOT stutters."""
    words = _toks(name)
    if len(words) < 2:
        return False
    lead, second = words[0], words[1]
    if (lead, second) in MULTIWORD_VERBS:
        return False
    return second in verbs or second in PARTICLES


def repair_stutter(name: str, verbs: frozenset[str]) -> str:
    """Drop the stray second token: ``Browse up Slack`` -> ``Browse Slack``,
    ``Manage create topic`` -> ``Manage topic``, ``Create up inbox`` ->
    ``Create inbox``. Casing of the surviving words is preserved."""
    words = name.split()
    if len(words) < 2:
        return name
    repaired = [words[0]] + words[2:]
    return " ".join(repaired) if len(repaired) > 1 else name


def name_richness(name: str, pf_display: str, verbs: frozenset[str]) -> int:
    """Count of the name's OWN content tokens — noun-phrase tokens that are not in
    the PF display. Zero => a bare echo; higher => a specialized journey."""
    np = _tokset_stemmed(noun_phrase(name, verbs))
    return len(np - _tokset_stemmed(pf_display))


def noun_head(name: str, verbs: frozenset[str]) -> str:
    """The stemmed head noun (last noun-phrase token) — the L-C3 family key."""
    toks = [_stem(t) for t in _toks(noun_phrase(name, verbs))]
    return toks[-1] if toks else ""


def spans_overlap(a: Any, b: Any) -> bool:
    """True when two flows'/UFs' member span-sets share any covered line in a
    common path (coordinates decide L-C3 family folds, not the name)."""
    ra, rb = flow_span_records(a), flow_span_records(b)
    for path, aranges in ra.items():
        for bs, be in rb.get(path, []):
            for as_, ae in aranges:
                if as_ <= be and bs <= ae:
                    return True
    return False


# ── plan ──────────────────────────────────────────────────────────────────────


@dataclass
class UfSynthPlan:
    rename: dict[str, str] = field(default_factory=dict)     # L-C2
    fold: dict[str, str] = field(default_factory=dict)       # L-C1 / L-C3 loser->winner
    collisions: list[tuple[str, str]] = field(default_factory=list)  # L-C4
    reasons: dict[str, str] = field(default_factory=dict)

    def survivors(self, ufs: list[Any]) -> list[Any]:
        gone = set(self.fold)
        return [u for u in ufs if str(getattr(u, "id", "")) not in gone]


def _uf_id(uf: Any) -> str:
    return str(getattr(uf, "id", "") or "")


def _uf_name(uf: Any, plan: "UfSynthPlan") -> str:
    """The effective name for the echo test — the L-C2 repair if one applies."""
    return plan.rename.get(_uf_id(uf), str(getattr(uf, "name", "") or ""))


def plan_uf_synth(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    verbs: frozenset[str],
) -> UfSynthPlan:
    """Compute the L-C1..L-C4 plan. Deterministic (input order + stable winner
    tiebreaks). Applies at most one fold per UF."""
    plan = UfSynthPlan()
    pf_disp: dict[str, str] = {}
    for pf in product_features:
        key = str(getattr(pf, "name", "") or "")
        pf_disp[key] = str(getattr(pf, "display_name", None) or key)
    # Only REAL journeys are eligible — synthesized coverage markers / backstops
    # carry generated names + I8-cover semantics and must never fold/rename here.
    eligible = [
        u for u in user_flows
        if not getattr(u, "is_coverage_marker", False)
        and not getattr(u, "synthesized", False)
    ]
    members = index_flows_by_uf(eligible, flows)

    # L-C2 — verb-phrase integrity (rename; feeds the echo test's effective name)
    for uf in eligible:
        nm = str(getattr(uf, "name", "") or "")
        if has_verb_stutter(nm, verbs):
            fixed = repair_stutter(nm, verbs)
            if fixed and _norm(fixed) != _norm(nm):
                plan.rename[_uf_id(uf)] = fixed
                plan.reasons[_uf_id(uf)] = "lc2_verb_stutter"

    by_pf: dict[str, list[Any]] = defaultdict(list)
    for uf in eligible:
        pfid = str(getattr(uf, "product_feature_id", "") or "")
        if pfid:  # a UF with no PF binding cannot echo a PF
            by_pf[pfid].append(uf)

    for pf_id, ufs in by_pf.items():
        disp = pf_disp.get(pf_id, "")

        def _res(u: Any) -> str:
            return str(getattr(u, "resource", "") or "")

        def _has_lead_verb(name: str) -> bool:
            toks = _toks(name)
            return bool(toks) and toks[0] in verbs

        echoes = [u for u in ufs
                  if is_pf_echo(_uf_name(u, plan), disp, _res(u), verbs)]
        rich_nonecho = [u for u in ufs if u not in echoes
                        and name_richness(_uf_name(u, plan), disp, verbs) > 0]

        # L-C1 — fold BARE PF-echoes into a canonical survivor. Two shapes:
        #   (a) a non-echo rich journey exists ("Configure propel pipeline") ->
        #       EVERY echo of the PF ("View propel") folds into the richest one;
        #   (b) all rows are echoes but a VERBFUL echo ("Manage users") coexists
        #       with a bare-noun echo ("User") -> the bare noun folds into the
        #       verbful one.
        # Distinct-VERB echo families (all verbful, no rich sibling — cal routing
        # forms) are NOT L-C1; they defer to L-C3's span gate. The winner always
        # survives (census §4). A PF with a single lone echo keeps it.
        winner = None
        fold_set: list[Any] = []
        if rich_nonecho and echoes:
            winner = min(rich_nonecho, key=lambda u: (
                -name_richness(_uf_name(u, plan), disp, verbs),
                -len(members.get(_uf_id(u), [])), _uf_id(u)))
            fold_set = echoes
        elif echoes and len(echoes) >= 2:
            verbful = [e for e in echoes if _has_lead_verb(_uf_name(e, plan))]
            bare = [e for e in echoes if not _has_lead_verb(_uf_name(e, plan))]
            if verbful and bare:
                winner = min(verbful, key=lambda u: (
                    -len(members.get(_uf_id(u), [])), _uf_id(u)))
                fold_set = bare
        if winner is not None:
            wid = _uf_id(winner)
            for e in fold_set:
                eid = _uf_id(e)
                if eid != wid:
                    plan.fold[eid] = wid
                    plan.reasons.setdefault(eid, "lc1_pf_echo")

        # L-C3 — same-noun-head family fold, GATED by member span overlap.
        live = [u for u in ufs if _uf_id(u) not in plan.fold]
        heads: dict[str, list[Any]] = defaultdict(list)
        for u in live:
            heads[noun_head(_uf_name(u, plan), verbs)].append(u)
        for head, group in heads.items():
            if not head or len(group) < 2:
                continue
            anchor = min(group, key=_uf_id)
            am = members.get(_uf_id(anchor), [])
            for u in group:
                if u is anchor or _uf_id(u) in plan.fold:
                    continue
                um = members.get(_uf_id(u), [])
                if any(spans_overlap(a, b) for a in am for b in um):
                    plan.fold[_uf_id(u)] = _uf_id(anchor)
                    plan.reasons.setdefault(_uf_id(u), "lc3_family_overlap")

    # L-C4 — board-wide duplicate names (post-fold surviving real journeys).
    seen: dict[str, str] = {}
    for u in eligible:
        if _uf_id(u) in plan.fold:
            continue
        key = _norm(_uf_name(u, plan))
        if key in seen:
            plan.collisions.append((seen[key], _uf_id(u)))
        else:
            seen[key] = _uf_id(u)
    return plan


def apply_uf_synth_fold(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    verbs: frozenset[str],
) -> dict[str, Any]:
    """Apply the plan IN PLACE: rename (L-C2), fold echoes/families into the
    winner (union member_flow_ids — conservation), drop folded rows. Only ever
    called behind ``naming_pack_enabled`` -> OFF path never runs -> byte-identical.
    Returns telemetry."""
    plan = plan_uf_synth(user_flows, flows, product_features, verbs)
    by_id = {_uf_id(u): u for u in user_flows}
    for uid, new_name in plan.rename.items():
        u = by_id.get(uid)
        if u is not None and _uf_id(u) not in plan.fold:
            u.name = new_name
    for loser_id, winner_id in plan.fold.items():
        loser, winner = by_id.get(loser_id), by_id.get(winner_id)
        if loser is None or winner is None:
            continue
        wm = list(getattr(winner, "member_flow_ids", None) or [])
        for m in getattr(loser, "member_flow_ids", None) or []:
            if m not in wm:
                wm.append(m)
        if hasattr(winner, "member_flow_ids"):
            winner.member_flow_ids = wm
            if hasattr(winner, "member_count"):
                winner.member_count = len(wm)
        prev = list(getattr(winner, "previous_names", None) or []) \
            if hasattr(winner, "previous_names") else None
        if prev is not None:
            ln = str(getattr(loser, "name", "") or "")
            if ln and ln not in prev:
                prev.append(ln)
                winner.previous_names = prev
    user_flows[:] = plan.survivors(user_flows)
    reasons = plan.reasons
    return {
        "lc1_echo_folded": sum(1 for r in reasons.values() if r == "lc1_pf_echo"),
        "lc2_stutter_repaired": len(plan.rename),
        "lc3_family_folded": sum(1 for r in reasons.values() if r == "lc3_family_overlap"),
        "lc4_collisions": len(plan.collisions),
        "total_folded": len(plan.fold),
    }
