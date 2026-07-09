"""Stage 6.98 — synthesized-journey quality (bug B4, 2026-07-08).

Two deterministic, OUTPUT-ONLY post-passes over the finalized ``user_flows[]``
(they never touch the flow graph, features, coverage, or LOC accounting):

(a) DEMOTION of member-less ``system_flow_recall`` seeds.
    Stage 6.7d/6.7 seed a thin, member-LESS ``UserFlow``
    (``synthesis_reason="system_flow_recall"``, ``member_count=0``) for every
    system route Stage 3 left flow-less. They render as HOLLOW journey rows
    (loc 0, no children) a PM reads as noise; they exist for RECALL bookkeeping
    of uncovered system routes, not as journeys. This pass moves them OUT of
    ``user_flows[]`` into a tracked side-channel ``scan_meta["system_flow_seeds"]``
    (name + routes + pf) and nulls any ``flow.user_flow_id`` backpointer that
    referenced them.

    Validator safety (verified against fresh origin/main eval/validate_scan.py):
      * I7  — a member-less ``system_flow_recall`` UF is a CARVED tracked metric
              (``i7_system_seeds``), never gated; demotion only drops the tracked
              count (Soc0 14 -> 0). No gate flips.
      * I24 — route-group "touch" is computed ONLY from member flows'
              ``entry_point_file`` / ``paths``; member-less seeds contribute ZERO
              touch, and system routes are ``surface_scope="system"`` (excluded
              from I24 product groups). Demotion is I24-neutral — nothing to
              preserve in the side-channel for coverage.
      * I13 — LOC accounting never reads ``user_flows[]``. Neutral.
      * I14 — the backpointer null-out prevents any dangling
              ``flow.user_flow_id`` (Soc0 evidence: 0 seeds had backrefs, but
              the null-out is universal insurance).

(b) REGROUNDING of flow-ful backstop journey names.
    Stage 6.7d synthesizes a thin journey for every flowful product feature no
    journey references (``synthesis_reason="uncovered_product_feature_backstop"``);
    its display name is the generic journey template ("Manage {r}" / "View {r}").
    When a backstop has exactly ONE member flow AND its product feature is a
    GENERIC (non-vendor-composed) surface, the member flow's own code-grounded
    name is a strictly-more-specific journey label and we adopt it. A rename is
    made ONLY when it adds ≥1 content token the template lacks, stays law-clean
    (display-name laws), and does not collide with another journey. Vendor-
    composed surfaces (hub display "<Family> — <Vendor>" — the vendor IS the
    recognizable subject) and ambiguous multi-member backstops KEEP their
    template. Purely deterministic — no LLM, no repo hardcodes, no README.

Kill-switch: ``FAULTLINE_SYNTH_QUALITY`` (default ON; ``=0`` -> both passes
no-op, output byte-identical to pre-B4 main). Output-affecting -> registered in
``scan_result_cache.ENV_OUTPUT_FLAGS``.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Mirror the canonical synthesis-reason tags (kept as string literals so this
# module never imports the heavy synthesis stages — output-only, zero coupling).
SYSTEM_RECALL_REASON = "system_flow_recall"
BACKSTOP_REASON = "uncovered_product_feature_backstop"

SYNTH_QUALITY_ENV = "FAULTLINE_SYNTH_QUALITY"

#: B10 journey-worthiness floor kill-switch (default ON). ``=0`` restores base
#: output byte-identically (no UI-chrome demotion).
UF_WORTHINESS_ENV = "FAULTLINE_UF_WORTHINESS"

#: Pure UI-manipulation verbs (show/hide a component) — never a domain action.
#: Corroborating vocab; the STRUCTURAL trigger is no-domain-resource (below).
_CHROME_VERBS = frozenset({
    "toggle", "collapse", "expand", "show", "hide", "open", "close",
    "reveal", "dismiss", "pin", "unpin", "minimize", "maximize", "fold",
    "unfold",
})
#: UI-chrome component nouns — the object of a chrome affordance. A domain
#: noun (theme, account, resume, invoice…) is deliberately ABSENT so a
#: capability that merely shares a chrome verb (Toggle theme) is not demoted.
_CHROME_NOUNS = frozenset({
    "sidebar", "panel", "drawer", "dialog", "modal", "tooltip", "menu",
    "accordion", "popover", "overlay", "collapsible", "flyout", "navbar",
    "topbar", "dropdown",
})
#: Persistence-write signals on a flow's OWNED symbols — a chrome affordance
#: that persists product state (Toggle theme → setThemeCookie) touches a real
#: domain resource and is NEVER demoted (the structural safety gate).
_PERSIST_KEYWORDS = ("cookie", "localstorage", "sessionstorage", "indexeddb",
                     "persist")

#: Vendor-composed PF display marker (Product-Spine §4.8 hub composition
#: "<Family> — <Vendor>"). A journey on such a surface keeps the vendor-named
#: template — the vendor is the recognizable subject, not a member mechanic.
_HUB_COMPOSE_SEP = " — "

_KEBAB_SPLIT = re.compile(r"[^a-z0-9]+")

#: Universal UI-primitive / structural stop tokens dropped from a member-flow
#: object phrase (they locate a widget, they do not name a capability). This is
#: a UNIVERSAL structural set (not a corpus-tuned magic list): every one is a
#: generic UI container word, scale- and repo-invariant.
_UI_PRIMITIVE_STOP = frozenset({
    "flow", "page", "pages", "view", "views", "modal", "dialog", "drawer",
    "tab", "tabs", "panel", "section", "component", "components", "index",
    "screen", "widget", "row", "card", "list",
})

#: Universal CRUD verb families (B5). A member-flow verb maps to one of the
#: four lifecycle families; a backstop whose members GENUINELY span the
#: lifecycle (≥3 families incl. at least one create/delete) is a "managed
#: resource" journey — the broad verb "Manage" is then the honest verdict
#: (a narrow template verb like "View" undersells it). Same class as
#: ``_UI_PRIMITIVE_STOP``: a UNIVERSAL linguistic set (English CRUD verbs),
#: scale- and repo-invariant — NOT a corpus-tuned magic list, NOT a
#: stack pattern (those live in YAML). ``read`` verbs overlap the vocab's
#: ``view`` flow-verb-class by design; the two are consumed for different
#: verdicts (CRUD-span vs dominant-intent).
_CRUD_FAMILIES: dict[str, frozenset[str]] = {
    "create": frozenset({"create", "add", "new", "insert", "register"}),
    "read": frozenset({"view", "read", "get", "list", "show", "browse",
                       "display", "render", "fetch"}),
    "update": frozenset({"update", "edit", "modify", "change", "configure",
                         "set", "rename", "toggle", "manage"}),
    "delete": frozenset({"delete", "remove", "destroy", "archive"}),
}
_VERB_TO_CRUD: dict[str, str] = {
    v: fam for fam, verbs in _CRUD_FAMILIES.items() for v in verbs
}

#: Verb tokens that carry no resource meaning when aggregating member
#: objects (leading verbs already dropped by ``_split_member``; these are
#: trailing path/scaffold artifacts of a ``short_label`` — ``-src-2`` etc.).
_OBJ_STOP = frozenset({"src", "flow"})


def synth_quality_enabled() -> bool:
    """Default ON; ``FAULTLINE_SYNTH_QUALITY=0`` restores pre-B4 output
    byte-identically (both passes no-op)."""
    return os.environ.get(SYNTH_QUALITY_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def uf_worthiness_enabled() -> bool:
    """Journey-worthiness floor (B10). Default ON; ``FAULTLINE_UF_WORTHINESS=0``
    restores base output byte-identically (no UI-chrome demotion)."""
    return os.environ.get(UF_WORTHINESS_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


#: B13 — honest coverage-marker naming/typing of member-less I8-cover seeds.
#: The SAME flag that gates the backstop own-entry filter (kept in lock-step so
#: the kill-switch restores byte-identical output). Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS``.
BACKSTOP_OWNED_COVER_ENV = "FAULTLINE_BACKSTOP_OWNED_COVER"


def backstop_owned_cover_enabled() -> bool:
    """Default ON; ``FAULTLINE_BACKSTOP_OWNED_COVER=0`` restores the pre-B13
    seed names ('Run X' verb template) and drops the coverage-marker flag."""
    return os.environ.get(BACKSTOP_OWNED_COVER_ENV, "1").strip().lower() \
        not in {"0", "false", "no", "off"}


# ── universal accessors (typed models / namespaces AND raw JSON dicts) ───────
# Production passes pydantic ``UserFlow`` / ``Flow`` objects (attribute access);
# the offline validator-sim + sweep pass raw scan-JSON dicts (item access). Both
# flow through the SAME logic below — zero duplication, so the sim faithfully
# reproduces the production transform.


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def _set(obj: Any, field: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[field] = value
    else:
        setattr(obj, field, value)


# ── (a) demotion of member-less system-flow-recall seeds ─────────────────────


def _is_member_less_system_seed(uf: Any) -> bool:
    """A hollow ``system_flow_recall`` placeholder: synthesized, tagged, with
    ZERO member flows. (``e2e_journey_recall`` seeds are Track-C maintainer
    journeys and are intentionally OUT of scope — only system seeds demote.)"""
    if not bool(_get(uf, "synthesized", False)):
        return False
    if _get(uf, "synthesis_reason", None) != SYSTEM_RECALL_REASON:
        return False
    return not (_get(uf, "member_flow_ids", None) or [])


def demote_system_flow_seeds(
    user_flows: list[Any], flows: list[Any], scan_meta: dict[str, Any],
) -> dict[str, Any]:
    """Move member-less ``system_flow_recall`` UFs out of ``user_flows`` into
    ``scan_meta["system_flow_seeds"]``; null any flow backpointer to them.

    Mutates ``user_flows`` IN PLACE (``[:]`` slice-assign) so every caller
    holding the list sees the filtered set. Deterministic: input order is
    preserved, no set-ordered iteration touches output. Returns telemetry.
    """
    # I8-safety discriminator: a member-less ``system_flow_recall`` seed whose
    # ``product_feature_id`` is a flowless-but-journey-worthy PF is the W5.1
    # LOC-worthy backstop's I8 COVER (it exists *because* no journey references
    # that PF — validator I8). Demoting the SOLE cover of such a PF would
    # re-fire I8. So a seed is demotable only when its PF is None OR still
    # covered by ≥1 OTHER surviving user flow (route-bookkeeping seeds whose PF
    # already carries real journeys — Soc0's inngest seeds under
    # network-security — are always safe). This keeps demotion I8-neutral by
    # construction, no LOC/routes heuristic needed.
    other_pf_cover: dict[str, int] = {}
    for uf in user_flows:
        if _is_member_less_system_seed(uf):
            continue  # a hollow seed is not "other coverage" for I8
        pf = _get(uf, "product_feature_id", None)
        if pf:
            other_pf_cover[pf] = other_pf_cover.get(pf, 0) + 1

    kept: list[Any] = []
    demoted: list[Any] = []
    for uf in user_flows:
        if _is_member_less_system_seed(uf):
            pf = _get(uf, "product_feature_id", None)
            if not pf or other_pf_cover.get(pf, 0) > 0:
                demoted.append(uf)
                continue
        kept.append(uf)

    seeds: list[dict[str, Any]] = []
    for uf in demoted:
        seeds.append({
            "id": _get(uf, "id", None),
            "name": _get(uf, "name", None),
            "product_feature_id": _get(uf, "product_feature_id", None),
            "resource": _get(uf, "resource", None),
            "routes": list(_get(uf, "routes", None) or []),
            "category": _get(uf, "category", None),
            "trigger": _get(uf, "trigger", None),
        })
    # Always record the (possibly empty) side-channel under flag-ON so the
    # bookkeeping is observable and the count is stable across the double-run.
    scan_meta["system_flow_seeds"] = seeds

    cleared = 0
    kept_seed_ids = {_get(uf, "id", None) for uf in kept
                     if _is_member_less_system_seed(uf)}
    if demoted:
        demoted_ids = {_get(uf, "id", None) for uf in demoted}
        demoted_ids.discard(None)
        for fl in flows:  # I14 — no dangling backpointer may survive
            if _get(fl, "user_flow_id", None) in demoted_ids:
                _set(fl, "user_flow_id", None)
                cleared += 1
        user_flows[:] = kept

    return {
        "demoted": len(demoted),
        "kept_i8_cover_seeds": len(kept_seed_ids),
        "backpointers_cleared": cleared,
        "seed_names": [s["name"] for s in seeds],
    }


# ── (a.2) journey-worthiness floor — UI-chrome demotion (B10) ────────────────


def _owned_symbols(fl: Any) -> list[str]:
    """The flow's OWNED symbols (``loc_nodes``) — the flow's own code, NOT the
    shared utils its span traverses (a chrome getter vs a persistence write)."""
    out: list[str] = []
    for nd in (_get(fl, "loc_nodes", None) or []):
        s = _get(nd, "symbol", None)
        if s:
            out.append(str(s))
    return out


def _wtoks(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t]


def _is_ui_chrome_uf(uf: Any, flow_by_id: dict[str, Any]) -> bool:
    """A UF whose members are ALL pure UI-chrome affordances (show/hide a
    component, no domain resource). Most-conservative conjunction:
      1. every member's LEADING verb ∈ chrome verbs (a single CRUD/domain
         member — 'Delete account' — disqualifies the whole UF);
      2. no member persists product state on its OWNED symbols (the structural
         gate: 'Toggle theme' → setThemeCookie survives);
      3. the UF's object noun ∈ chrome nouns (a domain object — theme,
         account — survives even when it shares a chrome verb).
    Smallness / LOC is NEVER a trigger — only this chrome-class conjunction."""
    mids = _get(uf, "member_flow_ids", None) or []
    if not mids:
        return False  # member-less seeds are the system-seed pass's job
    obj_tokens: set[str] = set()
    for mid in mids:
        fl = flow_by_id.get(str(mid))
        toks = _wtoks(_get(fl, "name", "") if fl is not None else "")
        if not toks or toks[0] not in _CHROME_VERBS:
            return False
        obj_tokens.update(toks[1:])
        if fl is not None:
            for sym in _owned_symbols(fl):
                low = sym.lower()
                if any(k in low for k in _PERSIST_KEYWORDS):
                    return False  # persists domain state → journey-worthy
    for src in (_get(uf, "name", ""), _get(uf, "resource", "")):
        obj_tokens.update(_wtoks(src))
    obj_tokens -= _CHROME_VERBS
    obj_tokens -= _OBJ_STOP
    return bool(obj_tokens & _CHROME_NOUNS)


def demote_ui_chrome_ufs(
    user_flows: list[Any], flows: list[Any], scan_meta: dict[str, Any],
) -> dict[str, Any]:
    """Demote UI-chrome UFs out of ``user_flows`` into
    ``scan_meta["ui_chrome_demoted"]``; null their flow backpointers (I14).

    I8-SAFE BY CONSTRUCTION (the B4 discriminator): a chrome UF is demoted only
    when its PF is None OR still covered by >= 1 OTHER (non-chrome) surviving
    user flow. A chrome UF that is its PF's SOLE cover is KEPT — an honest
    recall gap, never an I8 re-fire. Deterministic, input order preserved."""
    flow_by_id = _flow_by_member_id(flows)
    chrome_set = {id(uf) for uf in user_flows if _is_ui_chrome_uf(uf, flow_by_id)}
    other_pf_cover: dict[str, int] = {}
    for uf in user_flows:
        if id(uf) in chrome_set:
            continue
        pf = _get(uf, "product_feature_id", None)
        if pf:
            other_pf_cover[str(pf)] = other_pf_cover.get(str(pf), 0) + 1

    kept: list[Any] = []
    demoted: list[Any] = []
    for uf in user_flows:
        if id(uf) in chrome_set:
            pf = _get(uf, "product_feature_id", None)
            if not pf or other_pf_cover.get(str(pf), 0) > 0:
                demoted.append(uf)
                continue
        kept.append(uf)

    recs = [{
        "id": _get(uf, "id", None),
        "name": _get(uf, "name", None),
        "product_feature_id": _get(uf, "product_feature_id", None),
        "resource": _get(uf, "resource", None),
        "member_flow_ids": list(_get(uf, "member_flow_ids", None) or []),
        "reason": "ui_chrome_affordance",
    } for uf in demoted]
    scan_meta["ui_chrome_demoted"] = recs

    cleared = 0
    if demoted:
        demoted_ids = {_get(uf, "id", None) for uf in demoted}
        demoted_ids.discard(None)
        for fl in flows:  # I14 — no dangling backpointer may survive
            if _get(fl, "user_flow_id", None) in demoted_ids:
                _set(fl, "user_flow_id", None)
                cleared += 1
        user_flows[:] = kept

    return {
        "demoted": len(demoted),
        "kept_sole_cover_chrome": len(chrome_set) - len(demoted),
        "backpointers_cleared": cleared,
        "demoted_names": [r["name"] for r in recs],
    }


# ── (b) regrounding of single-member generic backstop journey names ──────────


def _flow_by_member_id(flows: list[Any]) -> dict[str, Any]:
    """member id (uuid, else name — the ``_flow_key`` id space) -> flow."""
    out: dict[str, Any] = {}
    for fl in flows:
        for key in (_get(fl, "uuid", None), _get(fl, "name", None)):
            if key and str(key) not in out:
                out[str(key)] = fl
    return out


def _member_flow_label(fl: Any) -> str:
    return str(_get(fl, "name", None) or _get(fl, "short_label", None) or "")


def _split_member(label: str) -> tuple[str, list[str]]:
    """"manage-alert-relations-flow" -> ("manage", ["alert", "relations"]).
    Leading token is the journey verb (member-flow names are ``verb-object``
    by construction); UI-primitive / structural tokens drop from the object."""
    toks = [t for t in _KEBAB_SPLIT.split(label.lower()) if t]
    if toks and toks[-1] == "flow":
        toks = toks[:-1]
    if not toks:
        return "", []
    verb = toks[0]
    obj = [t for t in toks[1:] if t not in _UI_PRIMITIVE_STOP]
    return verb, obj


# ── (b.2) multi-member backstop derivation (B5) ──────────────────────────────
#
# B4 named backstop journeys only in the unambiguous SINGLE-member case. B5
# raises the derivation power to the MULTI-member case (2..N members) by
# aggregating member evidence — flow-name verbs + objects, the UF's own
# code-derived ``resource`` — into an actor+intent+outcome verdict. The
# single-member path (b) is unchanged (byte-identical). All rules are
# deterministic, universal (no repo hardcodes, no README, no LLM) and,
# critically, CONSERVATIVE: a change fires only when the members clearly
# out-vote the template. Three shapes fire (validated on the wave-9 corpus):
#
#   * REGROUND — the UF ``resource`` (engine-derived from the members) is
#     token-disjoint from the PF display: the template named the CONTAINER,
#     not what the members DO ("View network security" whose 4 members are
#     all knowledge CRUD → "Manage knowledge"). Re-grounds onto the member
#     resource; needs a confident verb verdict (below) to fire.
#   * CRUD-UPGRADE — members genuinely span the CRUD lifecycle but the
#     template picked a narrow read verb ("View log drains" whose members
#     create/read/update/delete → "Manage log drains").
#   * VERB-CORRECT — a single flow-verb-class strictly dominates the members
#     yet differs from the template verb (the template verb won only via
#     class-precedence, not vote): "Send network security" (6/8 members are
#     view-class, only 2 are send-class) → "View network security".
#
# Anti-cases that DON'T fire (kept template, honest low-confidence "~"): no
# dominant class AND no CRUD span (heterogeneous members); resource matches
# the PF and the template verb already reflects the members; an already-
# specialized (non-template) name; a vendor-composed hub PF.


def _tokset(text: str) -> set[str]:
    return {t for t in _KEBAB_SPLIT.split((text or "").lower()) if t}


def _stem(tok: str) -> str:
    """Cheap plural fold ("drains" -> "drain") for the disjoint test only."""
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") else tok


def _template_shaped(name: str, pf_display: str) -> bool:
    """True when ``name`` is the bare journey TEMPLATE "<verb> <PF display>":
    a leading word followed by tokens that are all in the PF display. A name
    carrying its OWN content token (already specialized — B4 single-member
    renames, authored labels) is NOT template-shaped and is left untouched."""
    words = (name or "").split()
    if len(words) < 2:
        return False
    rest = _tokset(" ".join(words[1:]))
    pf = _tokset(pf_display)
    return bool(rest) and rest <= pf


def _resource_lc(display: str) -> str:
    """Lower-cased resource words of a PF display ("Log Drains" -> "log
    drains"); acronym/brand re-casing is re-applied by the caller's polish."""
    return " ".join(w.lower() for w in (display or "").split())


def _derive_multi_member_name(
    members: list[str],
    resource: str,
    pf_display: str,
    current: str,
    verb_class: dict[str, str],
) -> tuple[str | None, bool]:
    """Verdict for a backstop with ≥2 members. Returns ``(candidate, strong)``
    where ``candidate`` is the un-polished display (or ``None`` to keep the
    template) and ``strong`` marks a ≥2-member resource+action agreement
    (drives the low→medium confidence bump). Pure; no side effects."""
    from collections import Counter

    if not _template_shaped(current, pf_display):
        return None, False  # already specialized — never rewrite

    classes: list[str] = []
    crud: set[str] = set()
    for lbl in members:
        verb, _obj = _split_member(lbl)
        if not verb:
            continue
        cls = verb_class.get(verb)
        if cls:
            classes.append(cls)
        fam = _VERB_TO_CRUD.get(verb)
        if fam:
            crud.add(fam)
    n = len(members)
    if n < 2:
        return None, False

    counts = Counter(classes)
    dom_cls, dom_n = counts.most_common(1)[0] if counts else (None, 0)
    dominant = dom_cls if dom_n * 2 > n else None  # strictly > half
    crud_span = len(crud) >= 3 and bool(crud & {"create", "delete"})

    if crud_span:
        verb_disp: str | None = "Manage"
    elif dominant:
        verb_disp = dominant.capitalize()
    else:
        verb_disp = None  # heterogeneous — no confident intent

    # REGROUND: member resource token-disjoint (plural-folded) from the PF.
    res_toks = _tokset(resource)
    pf_toks = _tokset(pf_display)
    disjoint = bool(res_toks) and not (
        {_stem(t) for t in res_toks} & {_stem(t) for t in pf_toks}
    )
    if disjoint:
        if verb_disp is None:
            return None, False  # disjoint but no confident intent — keep
        if not re.fullmatch(r"[a-z0-9-]+", resource or ""):
            return None, False  # resource carries route/param junk — unsafe
        res_phrase = re.sub(r"[-_]+", " ", resource).strip()
        return f"{verb_disp} {res_phrase}", crud_span

    # resource == PF display from here (template resource retained).
    cur_verb = (current.split() or [""])[0].lower()
    # CRUD-UPGRADE: lifecycle span but a narrow (non-Manage) template verb.
    if crud_span and cur_verb != "manage":
        return f"Manage {_resource_lc(pf_display)}", True
    # VERB-CORRECT: a single class dominates but differs from the template.
    if dominant and verb_class.get(cur_verb) != dominant:
        return f"{dominant.capitalize()} {_resource_lc(pf_display)}", False
    return None, False


def _verb_class_index(vocab: Any) -> dict[str, str]:
    """``verb token -> flow_verb_class`` from the naming vocab (data-driven)."""
    out: dict[str, str] = {}
    for cls, verbs in (vocab.get("flow_verb_classes") or {}).items():
        for v in (verbs or []):
            out[str(v).lower()] = str(cls)
    return out


def reground_backstop_uf_names(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    scan_meta: dict[str, Any],
    vocab: Any | None = None,
) -> dict[str, Any]:
    """Rename single-member GENERIC backstop journeys after their sole member
    flow when that is strictly more specific + safe; keep the template
    otherwise. Deterministic; mutates ``uf.name`` only. Returns telemetry."""
    # Reuse the naming-contract display laws + casing polish (import, never
    # rewrite — this module stays out of the B2 zone).
    from faultline.pipeline_v2.naming_contract import (
        display_law_violations,
        load_naming_vocab,
        polish_display_casing,
    )

    v = vocab if vocab is not None else load_naming_vocab()
    # Noun-ish leading tokens that are NOT journey verbs — a member-flow name
    # led by one of these ("api-user-subscribe") is not a verb-led journey
    # label; keep the template. Data-driven (vocab), not a hardcoded blocklist.
    noun_lead = {
        str(x).lower()
        for key in ("known_acronyms", "structural_segments",
                    "hub_container_segments")
        for x in (v.get(key) or [])
    }
    flow_by_id = _flow_by_member_id(flows)
    verb_class = _verb_class_index(v)
    pf_by_key: dict[str, Any] = {}
    for pf in (product_features or []):
        key = _get(pf, "id", None) or _get(pf, "name", None)
        if key:
            pf_by_key.setdefault(str(key), pf)

    taken = {
        (str(_get(u, "name", "") or "").strip().lower())
        for u in user_flows
    }
    renames: list[dict[str, Any]] = []
    confidence_raised = 0

    for uf in user_flows:
        if _get(uf, "synthesis_reason", None) != BACKSTOP_REASON:
            continue
        mfids = _get(uf, "member_flow_ids", None) or []
        if not mfids:
            continue  # member-less backstop — no evidence to derive from
        pf = pf_by_key.get(str(_get(uf, "product_feature_id", None) or ""))
        pf_display = (
            str(_get(pf, "display_name", None)
                or _get(pf, "name", "") or "")
            if pf is not None else ""
        )
        if _HUB_COMPOSE_SEP in pf_display:
            continue  # vendor-composed surface — vendor is the journey subject

        current = str(_get(uf, "name", "") or "")
        candidate: str | None = None
        strong = False

        if len(mfids) == 1:
            # ── (b) single-member path — B4, unchanged (byte-identical) ──
            fl = flow_by_id.get(str(mfids[0]))
            if fl is None:
                continue
            verb, obj = _split_member(_member_flow_label(fl))
            if not verb.isalpha() or verb in noun_lead or not obj:
                continue  # no clean verb-led member evidence
            cur_tokens = {t for t in _KEBAB_SPLIT.split(current.lower()) if t}
            # Adopt only when the member adds ≥1 token the template lacks.
            if not any(t not in cur_tokens for t in obj):
                continue
            candidate = polish_display_casing(
                verb.capitalize() + " " + " ".join(obj), v)
        else:
            # ── (b.2) multi-member path — B5, aggregate member evidence ──
            members = [
                _member_flow_label(flow_by_id[str(m)])
                for m in mfids if flow_by_id.get(str(m)) is not None
            ]
            raw, strong = _derive_multi_member_name(
                members, str(_get(uf, "resource", "") or ""),
                pf_display, current, verb_class)
            if raw is not None:
                candidate = polish_display_casing(raw, v)

        if candidate is None:
            continue
        cand_l = candidate.strip().lower()
        if cand_l == current.strip().lower():
            continue
        if display_law_violations(candidate, v, pf_display=pf_display or None):
            continue
        if cand_l in taken:  # never collide with another journey
            continue

        taken.discard(current.strip().lower())
        taken.add(cand_l)
        rec: dict[str, Any] = {
            "id": _get(uf, "id", None),
            "product_feature_id": _get(uf, "product_feature_id", None),
            "before": current, "after": candidate,
        }
        _set(uf, "name", candidate)
        # B5: raise low → medium ONLY on a STRONG (≥2 members agreeing on
        # resource + action) multi-member derivation. Single-member renames
        # and weak (verb-correction) renames keep their honest low "~".
        if strong and str(
            _get(uf, "name_confidence", "") or ""
        ).strip().lower() == "low":
            _set(uf, "name_confidence", "medium")
            rec["confidence"] = "medium"
            confidence_raised += 1
        renames.append(rec)

    if renames:
        scan_meta.setdefault("synth_quality", {})["backstop_renamed"] = renames
    return {
        "renamed": len(renames),
        "confidence_raised": confidence_raised,
        "renames": renames,
    }


def _coverage_marker_name(subject: str) -> str:
    """Honest gap-band label for a member-LESS I8-cover seed — a
    route-coverage marker derived from the PF/resource subject (W3-lawful),
    NEVER a journey verb ('Run X'). E.g. ``'Uncovered: SentinelOne routes'``."""
    subj = re.sub(r"\s+", " ", str(subject or "").strip()) or "capability"
    return f"Uncovered: {subj} routes"


def honest_coverage_markers(
    user_flows: list[Any],
    product_features: list[Any],
    scan_meta: dict[str, Any],
) -> dict[str, Any]:
    """B13 Part-2(a) — every SURVIVING member-LESS I8-cover seed (``synthesized``
    + ``member_count == 0``) gets a machine-readable ``is_coverage_marker`` flag
    and an honest ``'Uncovered: <PF> routes'`` name (replacing the ``'Run X'``
    verb template) so ANY viewer renders it as a coverage gap-band, not a
    journey row. Deterministic + idempotent; runs AFTER the hollow-seed
    demotion so only the seeds I8 genuinely needs are marked. No-op (and
    byte-identical) when ``FAULTLINE_BACKSTOP_OWNED_COVER=0``."""
    if not backstop_owned_cover_enabled():
        return {"marked": 0}
    pf_display: dict[str, str] = {}
    for pf in product_features or []:
        key = str(_get(pf, "name", "") or _get(pf, "id", "") or "")
        if key:
            pf_display[key] = str(_get(pf, "display_name", None) or key)
    marked = 0
    for uf in sorted(user_flows, key=lambda u: str(_get(u, "id", ""))):
        if not _get(uf, "synthesized", False):
            continue
        if (_get(uf, "member_count", 0) or 0) != 0:
            continue
        if _get(uf, "member_flow_ids", None):
            continue  # defensive — a real member set is never a marker
        pfid = _get(uf, "product_feature_id", None)
        subject = (pf_display.get(str(pfid)) if pfid else None) \
            or _get(uf, "resource", None) or _get(uf, "name", "")
        _set(uf, "name", _coverage_marker_name(subject))
        _set(uf, "is_coverage_marker", True)
        _set(uf, "name_confidence", "low")
        marked += 1
    if marked:
        scan_meta.setdefault("synth_quality", {})["coverage_markers"] = marked
    return {"marked": marked}


def run_synth_quality(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    scan_meta: dict[str, Any],
    vocab: Any | None = None,
) -> dict[str, Any]:
    """Both B4 passes, in order: demote the hollow system seeds first (so the
    naming collision set excludes soon-removed seeds), then reground backstop
    names over the surviving set. No-op (returns zeros) when the kill-switch is
    off — the caller guards on :func:`synth_quality_enabled`, but this stays
    safe if called directly."""
    if not synth_quality_enabled():
        return {"enabled": False}
    demote_tele = demote_system_flow_seeds(user_flows, flows, scan_meta)
    # B10 journey-worthiness floor — demote UI-chrome affordances (I8-safe;
    # only when its PF keeps other cover). Runs AFTER the member-less system
    # seeds so its other-cover count reflects the final surviving set.
    chrome_tele = (
        demote_ui_chrome_ufs(user_flows, flows, scan_meta)
        if uf_worthiness_enabled() else {"demoted": 0, "backpointers_cleared": 0}
    )
    name_tele = reground_backstop_uf_names(
        user_flows, flows, product_features, scan_meta, vocab=vocab)
    raised = name_tele.get("confidence_raised", 0)
    # B13 Part-2(a) — honest coverage-marker naming/typing of the member-less
    # I8-cover seeds that SURVIVED demotion (the ones I8 genuinely needs).
    marker_tele = honest_coverage_markers(
        user_flows, product_features, scan_meta)
    tele = {
        "enabled": True,
        "backstop_renamed": name_tele["renamed"],
        "backstop_confidence_raised": raised,
        "system_seeds_demoted": demote_tele["demoted"],
        "backpointers_cleared": demote_tele["backpointers_cleared"],
        "ui_chrome_demoted": chrome_tele["demoted"],
        "coverage_markers": marker_tele["marked"],
    }
    sq = scan_meta.setdefault("synth_quality", {})
    sq.update({
        "backstop_renamed_count": name_tele["renamed"],
        "system_seeds_demoted": demote_tele["demoted"],
        "backpointers_cleared": demote_tele["backpointers_cleared"],
    })
    # Additive telemetry — only when B5 raised a confidence, so scans where
    # the multi-member pass is inert stay byte-identical to pre-B5 output.
    if raised:
        sq["backstop_confidence_raised"] = raised
    return tele


__all__ = [
    "SYNTH_QUALITY_ENV",
    "UF_WORTHINESS_ENV",
    "BACKSTOP_OWNED_COVER_ENV",
    "SYSTEM_RECALL_REASON",
    "BACKSTOP_REASON",
    "synth_quality_enabled",
    "uf_worthiness_enabled",
    "backstop_owned_cover_enabled",
    "demote_system_flow_seeds",
    "demote_ui_chrome_ufs",
    "reground_backstop_uf_names",
    "honest_coverage_markers",
    "run_synth_quality",
]
