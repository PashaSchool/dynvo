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

import hashlib
import os
import re
from typing import Any

# Mirror the canonical synthesis-reason tags (kept as string literals so this
# module never imports the heavy synthesis stages — output-only, zero coupling).
SYSTEM_RECALL_REASON = "system_flow_recall"
BACKSTOP_REASON = "uncovered_product_feature_backstop"
#: Track-C e2e orphan-journey recall seeds (``e2e_truth.E2E_ORPHAN_REASON``) —
#: maintainer-AUTHORED journey placeholders, never renamed under B23.
E2E_RECALL_REASON = "e2e_journey_recall"
#: Route-group recall rows (``route_group_recall._REASON``) and the 6.7d
#: promoted-capability backstop subclass — the remaining synthesized
#: recall-row families the B31 distinct-names law covers.
ROUTE_GROUP_REASON = "route_group_recall"
PROMOTED_BACKSTOP_REASON = "promoted_capability_backstop"

#: B31 — every synthesized recall-row family: rows the engine minted for
#: RECALL bookkeeping (coverage markers, route-group recall, PF backstops).
#: These are the rows whose display names may collapse onto one generic
#: template string (wave-14 class 1: documenso 'Manage tRPC' ×7) because
#: authored-channel protection exempts them from the naming contract's
#: uniqueness law while a persona channel reverts them to the template.
_RECALL_REASONS = frozenset({
    E2E_RECALL_REASON,
    ROUTE_GROUP_REASON,
    BACKSTOP_REASON,
    SYSTEM_RECALL_REASON,
    PROMOTED_BACKSTOP_REASON,
})

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


#: B23 — real code coordinates + authored-label preservation for member-less
#: coverage markers. Gates BOTH halves in lock-step: (a) the surface-span
#: attachment (``attach_marker_surface_coords``) and (b) the e2e authored-label
#: carve inside ``honest_coverage_markers`` (Track-C playwright names are the
#: maintainer's own journey labels — renaming them to ``'Uncovered: <PF>
#: routes'`` collapsed 13-18 DISTINCT journeys per board into duplicate rows).
#: Registered in ``scan_result_cache.ENV_OUTPUT_FLAGS``.
MARKER_SURFACE_COORDS_ENV = "FAULTLINE_MARKER_SURFACE_COORDS"


def marker_surface_coords_enabled() -> bool:
    """Default ON; ``FAULTLINE_MARKER_SURFACE_COORDS=0`` restores today's
    markers byte-identically (no surface spans, e2e labels renamed to the
    B13 ``'Uncovered: <PF> routes'`` template)."""
    return os.environ.get(MARKER_SURFACE_COORDS_ENV, "1").strip().lower() \
        not in {"0", "false", "no", "off"}


#: B31 — distinct display names for synthesized recall rows. Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS``.
RECALL_ROW_NAMES_ENV = "FAULTLINE_RECALL_ROW_NAMES"


def recall_row_names_enabled() -> bool:
    """Default ON; ``FAULTLINE_RECALL_ROW_NAMES=0`` restores today's recall-row
    display names byte-identically (colliding generic templates kept)."""
    return os.environ.get(RECALL_ROW_NAMES_ENV, "1").strip().lower() \
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
    *,
    stem_agreement: bool = False,
) -> tuple[str | None, bool]:
    """Verdict for a backstop with ≥2 members. Returns ``(candidate, strong)``
    where ``candidate`` is the un-polished display (or ``None`` to keep the
    template) and ``strong`` marks a ≥2-member resource+action agreement
    (drives the low→medium confidence bump). Pure; no side effects.

    B40 (``stem_agreement``, FAULTLINE_NAME_EVIDENCE_RUNGS): fold ``_stem``
    singularization into the member-object concurrence so a plural/singular
    mismatch (``onboarding`` vs ``onboardings``) stops degrading the agreement
    signal — a resource stem shared by ≥2 members widens ``strong`` (confidence
    ONLY). The returned CANDIDATE is byte-identical regardless of the flag."""
    from collections import Counter

    if not _template_shaped(current, pf_display):
        return None, False  # already specialized — never rewrite

    classes: list[str] = []
    crud: set[str] = set()
    obj_stems: list[set[str]] = []
    for lbl in members:
        verb, obj = _split_member(lbl)
        if stem_agreement and obj:
            obj_stems.append({_stem(t) for t in obj})
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

    # B40 — singular-folded object concurrence: a resource stem shared by ≥2
    # members is a genuine agreement rung (name-neutral; widens ``strong`` only).
    obj_concur = False
    if stem_agreement and len(obj_stems) >= 2:
        stem_counts: dict[str, int] = {}
        for s in obj_stems:
            for t in s:
                stem_counts[t] = stem_counts.get(t, 0) + 1
        obj_concur = any(c >= 2 for c in stem_counts.values())

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
        return f"{verb_disp} {res_phrase}", (crud_span or obj_concur)

    # resource == PF display from here (template resource retained).
    cur_verb = (current.split() or [""])[0].lower()
    # CRUD-UPGRADE: lifecycle span but a narrow (non-Manage) template verb.
    if crud_span and cur_verb != "manage":
        return f"Manage {_resource_lc(pf_display)}", True
    # VERB-CORRECT: a single class dominates but differs from the template.
    if dominant and verb_class.get(cur_verb) != dominant:
        return f"{dominant.capitalize()} {_resource_lc(pf_display)}", obj_concur
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
        name_evidence_rungs_enabled,
        polish_display_casing,
    )

    v = vocab if vocab is not None else load_naming_vocab()
    rungs_on = name_evidence_rungs_enabled()
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
                pf_display, current, verb_class,
                stem_agreement=rungs_on)
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
            # B40 — a ≥2-member resource+action agreement is the fired rung.
            # Stamp the audit trail (replacing Law C's low ``missing:*`` list,
            # which no longer applies now that the lift fired). Flag-gated: OFF
            # leaves name_evidence untouched (byte-identical, still None).
            if rungs_on:
                _set(uf, "name_evidence", ["member-agreement"])
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
    byte-identical) when ``FAULTLINE_BACKSTOP_OWNED_COVER=0``.

    B23 carve (behind ``FAULTLINE_MARKER_SURFACE_COORDS``, default ON): a
    Track-C e2e seed (``synthesis_reason="e2e_journey_recall"``) KEEPS its
    maintainer-authored playwright label — renaming those to the PF-subject
    template collapsed 13-18 DISTINCT authored journeys per board into
    identical ``'Uncovered: tRPC routes'`` rows (the template has no
    collision guard and the subject is the shared home PF). The flag and
    the low confidence still apply — the row is still a member-less
    placeholder — only the NAME is preserved. ``=0`` restores the B13
    rename byte-identically."""
    if not backstop_owned_cover_enabled():
        return {"marked": 0}
    preserve_e2e = marker_surface_coords_enabled()
    pf_display: dict[str, str] = {}
    for pf in product_features or []:
        key = str(_get(pf, "name", "") or _get(pf, "id", "") or "")
        if key:
            pf_display[key] = str(_get(pf, "display_name", None) or key)
    marked = 0
    labels_preserved = 0
    for uf in sorted(user_flows, key=lambda u: str(_get(u, "id", ""))):
        if not _get(uf, "synthesized", False):
            continue
        if (_get(uf, "member_count", 0) or 0) != 0:
            continue
        if _get(uf, "member_flow_ids", None):
            continue  # defensive — a real member set is never a marker
        if preserve_e2e and \
                _get(uf, "synthesis_reason", None) == E2E_RECALL_REASON:
            # B23 — authored label kept; marker typing still applies.
            _set(uf, "is_coverage_marker", True)
            _set(uf, "name_confidence", "low")
            marked += 1
            labels_preserved += 1
            continue
        pfid = _get(uf, "product_feature_id", None)
        subject = (pf_display.get(str(pfid)) if pfid else None) \
            or _get(uf, "resource", None) or _get(uf, "name", "")
        _set(uf, "name", _coverage_marker_name(subject))
        _set(uf, "is_coverage_marker", True)
        _set(uf, "name_confidence", "low")
        marked += 1
    if marked:
        scan_meta.setdefault("synth_quality", {})["coverage_markers"] = marked
    if labels_preserved:
        # Additive telemetry — key absent on scans with no e2e markers so
        # marker-less boards stay byte-identical under flag ON.
        scan_meta.setdefault("synth_quality", {})[
            "coverage_marker_labels_preserved"] = labels_preserved
    return {"marked": marked, "labels_preserved": labels_preserved}


# ── B23 — real code coordinates for member-less coverage markers ─────────────


#: B38 (2026-07-11) — marker coordinate integrity: a member-less coverage
#: marker that ends the surface-attach pass with ZERO spans is a gap claim
#: with zero evidence (wave15 breach: cal.com 20 e2e rows wearing authored
#: labels — 'Can delete user account' — all homed to PF `trpc`; midday
#: GoCardless UF-078; typebot Pixel UF-103). The attach pass's honesty
#: gates are correct — SHIPPING the bare row is the bug. Default ON since the
#: 2026-07-11 keyed cal.com proof (flipped at merge; A3 docstring fix — this
#: block previously read "Default OFF", stale after the flip).
#: Registered in ``scan_result_cache.ENV_OUTPUT_FLAGS``.
MARKER_COORDS_REQUIRED_ENV = "FAULTLINE_MARKER_COORDS_REQUIRED"


def marker_coords_required() -> bool:
    """Default ON — an UNSET env resolves to ``"1"`` (ON) since the
    2026-07-11 keyed cal.com proof (markers 21->1, all 20 suppressions
    recorded in ``scan_meta.synth_quality.suppressed_markers`` — the board
    hides evidence-less gap rows, the machine record keeps the gap counted).
    Only ``"1"``/``"true"``/``"True"`` are ON; any other value restores the
    rows."""
    return os.environ.get(MARKER_COORDS_REQUIRED_ENV, "1").strip() in {
        "1", "true", "True",
    }


def _is_member_less_marker(uf: Any) -> bool:
    """The SAME structural predicate ``honest_coverage_markers`` uses: a
    synthesized UF with zero member flows (any synthesis reason)."""
    if not bool(_get(uf, "synthesized", False)):
        return False
    if (_get(uf, "member_count", 0) or 0) != 0:
        return False
    return not (_get(uf, "member_flow_ids", None) or [])


# ── B45 — coverage_gaps[] gap channel ────────────────────────────────────────
#
# The member-less I8-cover markers (``_is_member_less_marker`` — the loc-worthy
# / owned-cover / system-route / e2e-orphan seeds ``honest_coverage_markers``
# types) used to ship as hollow ``user_flows[]`` rows wearing an ``Uncovered:
# <PF> routes`` name (or a preserved e2e authored label), indistinguishable
# from real journeys on the board / MCP / PR comments. B45 segregates them into
# a dedicated top-level ``coverage_gaps[]`` array so a gap can never be mistaken
# for a journey. One typed gap per SURVIVING marker (a strict bijection with the
# old rows; the B38-suppressed markers are counted in the shared
# ``suppressed_markers`` ledger — the no-silent-gap law).
#
# Three modes (``FAULTLINE_COVERAGE_GAP_CHANNEL``): off (default) is
# byte-identical to pre-B45; dual emits gaps AND keeps the marker rows (a
# bijection instrument — one scan, two worlds); full emits gaps and REMOVES the
# marker rows from ``user_flows[]``. Member-FUL recall rows (route_group_recall,
# the member-ful 6.7d backstop) are NEVER markers and are never touched.

#: B45 gap-channel kill-switch. Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS``.
COVERAGE_GAP_CHANNEL_ENV = "FAULTLINE_COVERAGE_GAP_CHANNEL"


def coverage_gap_channel_mode() -> str:
    """B45 — the gap-channel mode ∈ {``"off"``, ``"dual"``, ``"full"``}.

    ``off`` (default: unset / ``""`` / ``"0"`` / ``"off"``) = byte-identical to
    pre-B45 (no gaps, ``coverage_gaps`` key absent). ``dual`` emits gaps AND
    keeps the member-less marker rows in ``user_flows[]``. ``full`` emits gaps
    and REMOVES the marker rows. Any UNRECOGNISED value falls back to ``off``
    (fail-safe to byte-identity)."""
    raw = os.environ.get(COVERAGE_GAP_CHANNEL_ENV, "").strip().lower()
    return raw if raw in {"dual", "full"} else "off"


def _flowful_pf_set(developer_features: list[Any] | None) -> set[str]:
    """PF keys that OWN ≥1 flow at Stage 6.98 — mirrors the 6.7d ``flowful``
    set (``pf_flows`` non-empty, built from ``dev.flows``). Used to split the
    two BYTE-IDENTICAL 6.7d backstop seeds: a loc-worthy seed's PF is FLOWLESS
    (absent here → ``loc_worthy``), an owned-cover seed's PF is FLOWFUL
    (present → ``owned_cover``)."""
    out: set[str] = set()
    for dev in developer_features or []:
        pfid = _get(dev, "product_feature_id", None)
        if pfid and (_get(dev, "flows", None) or []):
            out.add(str(pfid))
    return out


def _gap_kind(uf: Any, flowful_pfs: set[str]) -> str:
    """Map a member-less marker to its gap ``kind`` from (mint-site, reason).

    e2e is unambiguous by reason. The three ``system_flow_recall`` producers
    share one reason string, so the discriminators are the row's own
    attributes:

      * the route-group site (``stage_6_7.resynthesize_system_ufs``) is the
        ONLY one that stamps a ``trigger`` (and non-empty ``routes``) → the
        honest superset ``system_route``;
      * the two 6.7d backstop arms emit BYTE-IDENTICAL rows (both
        ``category="system"``, ``ui_tier="no-ui"``, ``routes=[]``,
        ``trigger=None``) — the ONLY difference is upstream PF flow-fulness
        (loc-worthy fires for a FLOWLESS PF, owned-cover for a FLOWFUL one),
        reconstructed via :func:`_flowful_pf_set`. Absent that positive
        flowful signal (or an unknown/lane PF) the honest default is
        ``loc_worthy`` — "journey-worthy surface with no attachable flow".
    """
    if _get(uf, "synthesis_reason", None) == E2E_RECALL_REASON:
        return "e2e_orphan"
    if _get(uf, "trigger", None) or (_get(uf, "routes", None) or []):
        return "system_route"
    pfid = _get(uf, "product_feature_id", None)
    if pfid and str(pfid) in flowful_pfs:
        return "owned_cover"
    return "loc_worthy"


def _clear_candidates(uf: Any) -> None:
    """Drop the mint-side candidate ledger without ADDING a key to dict
    inputs (the offline sim must not grow new keys on untouched rows)."""
    if isinstance(uf, dict):
        uf.pop("surface_candidate_files", None)
    elif getattr(uf, "surface_candidate_files", None) is not None:
        setattr(uf, "surface_candidate_files", None)


def attach_marker_surface_coords(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    developer_features: list[Any] | None,
    scan_meta: dict[str, Any],
) -> dict[str, Any]:
    """B23 — attach REAL code coordinates to member-less coverage markers.

    Every surviving marker points at a trigger surface the engine already
    resolved once: the e2e orphan resolver's route-family files (carried
    from the mint via ``surface_candidate_files``), the 6.7 system
    route-group files (same carrier), or — fallback for 6.7d-minted seeds
    that carry no candidates — the home PF's ``member_files``. This pass
    turns that surface into whole-file ``(path, 1, loc)`` spans on
    ``uf.surface_files`` so Stage 6.97b can stamp an honest ``loc > 0``
    ("uncovered surface: N files / M LOC"), under two HONESTY gates:

      * CLAIMED-FILE filter — a file any flow already traverses
        (``entry_point_file`` / ``paths`` / ``shared_paths``) is covered
        code and never re-attributed to an "Uncovered" row. A marker whose
        ENTIRE surface is already claimed (the cal.com thin-shim class:
        journeys mis-bound through 3-18-LOC tRPC router files because the
        scan's routes_index carries no page routes) attaches NOTHING and is
        counted in ``residual_claimed`` — forcing coordinates through those
        shims would be fake precision; the upstream routes_index gap owns
        that fix.
      * MEASURED-LOC filter — only files with a positive ``loc`` in the
        dev/PF ``member_files`` ledger get a span (whole-file end line =
        the ledger LOC; honest for an uncovered surface — no flow ever
        traced a finer grain there). Unmeasured files are counted, never
        guessed.

    Deterministic (sorted UFs / files; no set iteration on the output
    path), $0 LLM, output-only. No-op (byte-identical output) when
    ``FAULTLINE_MARKER_SURFACE_COORDS=0`` or the B13 marker regime is off
    (``FAULTLINE_BACKSTOP_OWNED_COVER=0`` — lock-step: spans exist only
    where the marker flag exists).
    """
    if not (marker_surface_coords_enabled() and backstop_owned_cover_enabled()):
        return {"attached": 0}

    # Files already covered by ANY flow — conservative claim set (mirrors
    # the validator's I24 touch + the path_index flow_uuids ruler).
    claimed: set[str] = set()
    for fl in flows or []:
        ep = _get(fl, "entry_point_file", None)
        if ep:
            claimed.add(str(ep))
        for p in (_get(fl, "paths", None) or []):
            if p:
                claimed.add(str(p))
        for sp in (_get(fl, "shared_paths", None) or []):
            p = _get(sp, "path", None)
            if p:
                claimed.add(str(p))

    # file → LOC ledger (dev-feature + PF member_files; first writer wins —
    # the ledgers agree on file LOC, iteration is input-list-ordered).
    file_loc: dict[str, int] = {}
    for feat in list(developer_features or []) + list(product_features or []):
        for m in (_get(feat, "member_files", None) or []):
            p = _get(m, "path", None)
            loc = _get(m, "loc", None)
            if p and str(p) not in file_loc and isinstance(loc, int) \
                    and not isinstance(loc, bool) and loc > 0:
                file_loc[str(p)] = loc

    # Home-PF member files — the fallback surface for system seeds minted
    # without a carried candidate set (the 6.7d flowless/no-own-entry arms).
    pf_files: dict[str, list[str]] = {}
    for pf in product_features or []:
        key = str(_get(pf, "name", "") or _get(pf, "id", "") or "")
        if not key:
            continue
        paths = sorted({
            str(_get(m, "path", None))
            for m in (_get(pf, "member_files", None) or [])
            if _get(m, "path", None)
        })
        if paths:
            pf_files[key] = paths

    attached = 0
    residual_claimed = 0
    residual_unmeasured = 0
    residual_no_surface = 0
    for uf in sorted(user_flows, key=lambda u: str(_get(u, "id", ""))):
        if not _is_member_less_marker(uf):
            _clear_candidates(uf)  # hygiene — plumbing never outlives 6.98
            continue
        if _get(uf, "surface_files", None):
            _clear_candidates(uf)
            continue  # idempotent — never re-derive attached spans
        cand = [str(c) for c in (_get(uf, "surface_candidate_files", None)
                                 or []) if c]
        if not cand and \
                _get(uf, "synthesis_reason", None) == SYSTEM_RECALL_REASON:
            # Home-PF member files — fallback for the 6.7d system arms only.
            # An e2e marker without carried candidates must NEVER inherit a
            # whole PF surface (a mega-PF home would hand one maintainer
            # journey a 1000+-file span — over-broad, not its surface).
            pfid = _get(uf, "product_feature_id", None)
            cand = pf_files.get(str(pfid), []) if pfid else []
        cand = sorted(set(cand))
        _clear_candidates(uf)
        if not cand:
            residual_no_surface += 1
            continue
        unclaimed = [p for p in cand if p not in claimed]
        if not unclaimed:
            residual_claimed += 1  # thin-shim class — honest 0, no fake spans
            continue
        spans: list[dict[str, Any]] = [
            {"path": p, "start_line": 1, "end_line": file_loc[p]}
            for p in unclaimed if p in file_loc
        ]
        if not spans:
            residual_unmeasured += 1
            continue
        if isinstance(uf, dict):
            uf["surface_files"] = spans
        else:
            # Live path — typed spans on the pydantic model (duck-typed
            # fixtures accept the same objects; attribute access only).
            from faultline.models.types import FlowLineRange
            setattr(uf, "surface_files", [FlowLineRange(**s) for s in spans])
        attached += 1

    tele = {
        "attached": attached,
        "residual_claimed": residual_claimed,
        "residual_unmeasured": residual_unmeasured,
        "residual_no_surface": residual_no_surface,
    }
    if attached or residual_claimed or residual_unmeasured \
            or residual_no_surface:
        # Additive telemetry — the key exists only on boards that HAVE
        # markers, so marker-less scans stay byte-identical under flag ON.
        scan_meta.setdefault("synth_quality", {})["surface_coords"] = dict(tele)
    return tele


def suppress_no_coords_markers(
    user_flows: list[Any],
    scan_meta: dict[str, Any],
) -> dict[str, Any]:
    """B38 — drop member-less coverage markers that carry ZERO coordinates.

    Runs AFTER :func:`attach_marker_surface_coords`: any member-less marker
    still without ``surface_files`` is a display row claiming a gap with no
    evidence — the wave15 operator breach (cal.com 20 authored-label e2e
    rows, midday GoCardless, typebot Pixel). Per the B38 spec the honest
    treatment is suppression + telemetry (the `residual_claimed` class in
    particular means the code IS covered — under sibling PFs — so the
    "Uncovered" claim is wrong, not just bare).

    Discovery is BY FLAG/STRUCTURE, never by name prefix — the warden
    lesson: 20 of the 22 wave15 breaches wore authored e2e labels, not
    'Uncovered:'. In-place ``user_flows[:]`` mutation (demote precedent).
    Kill-switch: default OFF; returns zeros without the flag.
    """
    if not marker_coords_required():
        return {"enabled": False, "suppressed": 0}
    kept: list[Any] = []
    dropped: list[dict[str, Any]] = []
    for uf in user_flows:
        if _is_member_less_marker(uf) and not _get(uf, "surface_files", None):
            # FULL machine record — the no-silent-gap law: the board hides
            # the evidence-less row, scan_meta keeps the gap counted.
            dropped.append({
                "id": str(_get(uf, "id", "") or ""),
                "name": str(_get(uf, "name", "") or ""),
                "pf": str(_get(uf, "product_feature_id", "") or ""),
                "reason": "no_resolvable_coords",
            })
            continue
        kept.append(uf)
    if dropped:
        user_flows[:] = kept
        block = scan_meta.setdefault("synth_quality", {})
        block["markers_suppressed_no_coords"] = len(dropped)
        block["suppressed_markers"] = dropped  # complete, never truncated
    return {"enabled": True, "suppressed": len(dropped)}


# ── B31 — distinct display names for synthesized recall rows ─────────────────
#
# Wave-14 class 1: recall rows (e2e_journey_recall / route_group_recall / PF
# backstops) collapse onto ONE generic template string per PF — documenso
# 'Manage tRPC' ×7, 'Manage team' ×11; supabase 'Manage projects' ×3. Two
# structural causes, both immune to the naming contract's own uniqueness law:
#
#   * authored-channel PROTECTION — a UF whose id is in the Track-C authored
#     map is exempt from ``_apply_uf_name_laws`` (Law A) BY DESIGN, but a
#     keyed persona channel (Draft Verifier revert / PM-Labeler pick) can
#     still write the generic ``<Verb> <PF-display>`` template onto it with
#     no collision guard — the protection then locks the collision in;
#   * template echo — several route-group/backstop rows under one PF derive
#     the same ``<Verb> <PF-display>`` echo when their (intent, resource)
#     evidence never reaches the display channel.
#
# The fix is a LAST-WRITER display law at Stage 6.98 (this module runs after
# the naming contract): every synthesized recall row in a display-name
# collision group is re-derived from its OWN row data, authored-first —
#
#   rung 1  authored label (Track-C mint carrier ``authored_label``) verbatim;
#   rung 2  authored label + " (<PF display>)" (cross-PF authored twins);
#   rung 3  ``<intent-verb> <resource>`` — deterministic journey template
#           over the row's own (intent, resource) fields;
#   rung 4  rung 3 + " (<route terminal>)" (deepest route's last clean word);
#   rung 5  rung 3 + " (<PF display>)";
#   rung 6  current name + " (<route terminal>)";
#   rung 7  current name + " (<PF display>)".
#
# Every candidate is casing-polished + display-law-gated + uniqueness-gated
# against the LIVE board (per-board uniqueness by construction; a row whose
# whole ladder collides keeps its name and is counted, never suffixed with a
# number). Organic journeys are NEVER touched. Deterministic, $0 LLM,
# display-channel only. Gap-band eligibility of mc=0 markers is carried by
# the STRUCTURAL fields (``is_coverage_marker`` / ``synthesis_reason``),
# never by the display name — this pass preserves both fields untouched.
#
# Kill-switch: ``FAULTLINE_RECALL_ROW_NAMES`` (default ON; ``=0`` restores
# today's names byte-identically). Registered in
# ``scan_result_cache.ENV_OUTPUT_FLAGS``.

#: intent → journey-language display template. MIRRORS
#: ``stage_6_7_user_flows.NAME_TMPL`` (kept as literals so this output-only
#: module never imports the heavy synthesis stage — same precedent as the
#: synthesis-reason tags above). ``other`` deliberately maps to the broad
#: ``Manage`` verdict instead of the bare ``{r}`` echo: a bare-resource
#: display would mint a single-noun / PF-twin name, which the display laws
#: reject anyway.
_INTENT_TMPL: dict[str, str] = {
    "author": "Create & edit {r}",
    "browse": "Browse & filter {r}",
    "lifecycle": "Transition {r} through its lifecycle",
    "execute": "Run {r}",
    "manage": "Manage {r}",
    "bulk": "Bulk-manage {r}",
    "export": "Export {r}",
}

#: A resource/terminal must be a clean kebab/word token before it may enter a
#: display (same guard class as ``reground_backstop_uf_names``): route params
#: (``:param``), globs (``**``), dialect glyphs (``$``, ``+``) never leak.
_CLEAN_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]*")


def _is_recall_row(uf: Any) -> bool:
    """A synthesized recall-bookkeeping row (marker or thin recall UF) —
    the ONLY rows this pass may rename. Organic journeys never qualify."""
    if not bool(_get(uf, "synthesized", False)):
        return False
    return _get(uf, "synthesis_reason", None) in _RECALL_REASONS


def _clear_authored_label(uf: Any) -> None:
    """Drop the mint-side authored-label carrier without ADDING a key to
    dict inputs (the offline sim must not grow new keys on untouched rows)."""
    if isinstance(uf, dict):
        uf.pop("authored_label", None)
    elif getattr(uf, "authored_label", None) is not None:
        setattr(uf, "authored_label", None)


def _resource_phrase_b31(resource: str) -> str | None:
    """``legacy-editor`` → ``legacy editor``; ``None`` when the resource is
    absent or carries route/param junk (unsafe for a display)."""
    res = str(resource or "").strip().lower()
    if not res or not _CLEAN_TOKEN_RE.fullmatch(res):
        return None
    phrase = re.sub(r"[-_]+", " ", res).strip()
    return phrase or None


def _route_terminals(uf: Any) -> list[str]:
    """Deterministic route-terminal qualifiers for one recall row: for each
    route (deepest first, then lexicographic) the LAST clean path word —
    params/globs/dialect glyphs skipped. ``/sign/:param/complete`` →
    ``complete``; ``/sign/:param`` → ``sign``."""
    out: list[str] = []
    routes = [str(r) for r in (_get(uf, "routes", None) or []) if r]
    for route in sorted(
        routes, key=lambda r: (-len([s for s in r.split("/") if s]), r)
    ):
        for seg in reversed([s for s in route.split("/") if s]):
            s = seg.strip().lower()
            if not _CLEAN_TOKEN_RE.fullmatch(s):
                continue  # :param / ** / $dialect / numeric — never a word
            term = re.sub(r"[-_]+", " ", s).strip()
            if term and term not in out:
                out.append(term)
            break
    return out


def _recall_name_candidates(
    uf: Any, pf_display: str, current: str,
) -> list[str]:
    """The B31 rung ladder (raw, un-polished candidate strings, ranked).
    Authored-first (B23 law), then the (intent, resource) composition, then
    route-terminal / PF qualifiers — see the pass docstring above."""
    out: list[str] = []

    def _add(c: str | None) -> None:
        c = " ".join(str(c or "").split())
        if c and c not in out:
            out.append(c)

    authored = str(_get(uf, "authored_label", None) or "").strip()
    if authored:
        _add(authored)
        if pf_display:
            _add(f"{authored} ({pf_display})")

    res_phrase = _resource_phrase_b31(str(_get(uf, "resource", "") or ""))
    composed: str | None = None
    if res_phrase:
        intent = str(_get(uf, "intent", "") or "").strip().lower()
        tmpl = _INTENT_TMPL.get(intent) or _INTENT_TMPL["manage"]
        composed = tmpl.format(r=res_phrase)
    # A terminal that merely repeats the resource phrase qualifies nothing
    # ("Manage admin (admin)") — skip it.
    terminals = [t for t in _route_terminals(uf) if t != (res_phrase or "")]
    if composed:
        _add(composed)
        for term in terminals:
            _add(f"{composed} ({term})")
        if pf_display:
            _add(f"{composed} ({pf_display})")
    if current:
        for term in terminals:
            _add(f"{current} ({term})")
        if pf_display:
            _add(f"{current} ({pf_display})")
    return out


def distinct_recall_row_names(
    user_flows: list[Any],
    product_features: list[Any],
    scan_meta: dict[str, Any],
    vocab: Any | None = None,
) -> dict[str, Any]:
    """B31 — per-board display-name uniqueness for synthesized recall rows.

    For every display-name collision group that contains ≥1 synthesized
    recall row, re-derive EACH such row's name from its own data via the
    rung ladder (authored label → (intent, resource) composition →
    route-terminal / PF qualifier). Organic rows are never touched; a row
    whose entire ladder collides keeps its current name (counted in
    ``residual_collisions`` — honest failure beats a numeric suffix).

    Deterministic (sorted groups / rows / terminals), idempotent (a second
    run finds no collision groups), $0 LLM, display-channel only —
    ``is_coverage_marker`` / ``synthesis_reason`` / membership / ids are
    untouched, so gap-band eligibility (structural, name-independent) is
    preserved by construction. No-op (byte-identical output) when
    ``FAULTLINE_RECALL_ROW_NAMES=0``.
    """
    if not recall_row_names_enabled():
        return {"renamed": 0}
    # Reuse the naming-contract display laws + casing polish (import, never
    # rewrite — this module stays out of the naming-contract zone).
    from faultline.pipeline_v2.naming_contract import (
        display_law_violations,
        load_naming_vocab,
        polish_display_casing,
    )

    v = vocab if vocab is not None else load_naming_vocab()
    b23_on = marker_surface_coords_enabled()

    pf_display: dict[str, str] = {}
    for pf in product_features or []:
        key = str(_get(pf, "name", "") or _get(pf, "id", "") or "")
        if key:
            pf_display[key] = str(_get(pf, "display_name", None) or key)

    def _fold(name: Any) -> str:
        return str(name or "").strip().lower()

    groups: dict[str, list[Any]] = {}
    for uf in user_flows:
        f = _fold(_get(uf, "name", ""))
        if f:
            groups.setdefault(f, []).append(uf)

    taken: set[str] = set(groups)
    renames: list[dict[str, Any]] = []
    residual = 0
    kept = 0

    for fold_name in sorted(k for k, g in groups.items() if len(g) > 1):
        grp = groups[fold_name]
        movers: list[Any] = []
        holders = 0
        for uf in grp:
            # Under FAULTLINE_MARKER_SURFACE_COORDS=0 the e2e markers are
            # intentionally back on the B13 ``'Uncovered: <PF> routes'``
            # naming regime — this pass must not fight that kill-switch.
            if _is_recall_row(uf) and not (
                not b23_on
                and _get(uf, "synthesis_reason", None) == E2E_RECALL_REASON
                and _is_member_less_marker(uf)
            ):
                movers.append(uf)
            else:
                holders += 1
        if not movers:
            continue
        if holders == 0:
            # No organic holder — the base name is vacated; the FIRST mover
            # whose ladder re-derives it may honestly re-adopt it.
            taken.discard(fold_name)
        for uf in sorted(movers, key=lambda u: str(_get(u, "id", ""))):
            current = str(_get(uf, "name", "") or "")
            pfid = str(_get(uf, "product_feature_id", None) or "")
            pfd = pf_display.get(pfid, "")
            new_name: str | None = None
            for raw in _recall_name_candidates(uf, pfd, current):
                cand = polish_display_casing(raw, v)
                cf = _fold(cand)
                if not cf or cf in taken:
                    continue
                if display_law_violations(cand, v, pf_display=pfd or None):
                    continue
                new_name = cand
                break
            if new_name is None:
                taken.add(_fold(current))  # ladder exhausted — honest keep
                residual += 1
                continue
            taken.add(_fold(new_name))
            if new_name == current:
                kept += 1  # re-adopted its own (now unique) name
                continue
            renames.append({
                "id": _get(uf, "id", None),
                "product_feature_id": _get(uf, "product_feature_id", None),
                "synthesis_reason": _get(uf, "synthesis_reason", None),
                "before": current,
                "after": new_name,
            })
            _set(uf, "name", new_name)

    # Hygiene — the authored-label carrier never outlives 6.98 (mirrors the
    # ``surface_candidate_files`` contract; dict rows never GROW a key).
    for uf in user_flows:
        _clear_authored_label(uf)

    tele = {
        "renamed": len(renames),
        "kept": kept,
        "residual_collisions": residual,
    }
    if renames or residual:
        # Additive telemetry — the key exists only on boards that HAD a
        # recall-row collision, so clean boards stay byte-identical under
        # flag ON.
        sq = scan_meta.setdefault("synth_quality", {})
        sq["recall_row_names"] = {
            "renamed": [dict(r) for r in renames],
            "residual_collisions": residual,
        }
    return tele


def _snapshot_authored_labels(user_flows: list[Any]) -> dict[str, str]:
    """B45 — capture each surviving member-less marker's RAW authored label
    (``authored_label`` carrier) BEFORE ``distinct_recall_row_names`` clears
    it, keyed by the row's ``id``. Only e2e-orphan markers carry one; the
    system kinds map to an empty snapshot entry (absent)."""
    out: dict[str, str] = {}
    for uf in user_flows:
        if not _is_member_less_marker(uf):
            continue
        al = _get(uf, "authored_label", None)
        if al:
            out[str(_get(uf, "id", "") or "")] = str(al)
    return out


def emit_coverage_gaps(
    user_flows: list[Any],
    product_features: list[Any],
    developer_features: list[Any] | None,
    scan_meta: dict[str, Any],
    authored_by_id: dict[str, str],
    mode: str,
) -> list[Any]:
    """B45 — build the typed ``coverage_gaps[]`` from the SURVIVING member-less
    markers (post demote / suppress / rename), ONE gap per marker (a strict
    bijection with the old rows). In ``full`` mode the gap's marker row is then
    REMOVED from ``user_flows[]`` (converted to a gap — I8 reformulation: a
    typed gap is valid PF cover, so this is NOT a coverage loss); in ``dual``
    the rows stay (bijection instrument). Returns the (ref-integrity-filtered,
    deterministically-ordered) gap list. Off mode never reaches here.

    ``label`` is the marker's FINAL display name (board-unique by the B31
    recall-row naming pass, so ``(pf, kind, label)`` — and thus the content
    hash id — is unique per board). ``authored_label`` carries the RAW
    maintainer label for e2e markers (the B23 carve). ``surface_files`` /
    ``loc`` reuse the marker's already-attached B38 spans (mirrors 6.97b), so
    a gap without spans cannot exist (those markers were B38-suppressed).
    """
    from faultline.models.types import CoverageGap, FlowLineRange
    from faultline.pipeline_v2.emission_integrity import (
        enforce_gap_ref_integrity,
    )
    from faultline.pipeline_v2.stage_6_97b_uf_loc import union_span_len

    flowful = _flowful_pf_set(developer_features)
    # Deterministic input order: markers in user_flows order (already stable).
    markers = [uf for uf in user_flows if _is_member_less_marker(uf)]

    pairs: list[tuple[Any, Any]] = []  # (marker_row, gap)
    for uf in markers:
        pfid = _get(uf, "product_feature_id", None)
        pf = str(pfid) if pfid else ""
        kind = _gap_kind(uf, flowful)
        label = str(_get(uf, "name", "") or "")
        gid = "GAP-" + hashlib.sha1(
            f"{pf}|{kind}|{label}".encode("utf-8")).hexdigest()[:10]
        spans: list[Any] = []
        loc_by_file: dict[str, list[tuple[int, int]]] = {}
        for rec in (_get(uf, "surface_files", None) or []):
            p = _get(rec, "path", None)
            s = _get(rec, "start_line", None)
            e = _get(rec, "end_line", None)
            if p is not None and s is not None and e is not None:
                spans.append(FlowLineRange(
                    path=str(p), start_line=int(s), end_line=int(e)))
                loc_by_file.setdefault(str(p), []).append((int(s), int(e)))
        # Surface LOC — per-file UNION of the spans (mirrors Stage 6.97b
        # ``_uf_surface_loc`` exactly; computed here so it is dict/object
        # -agnostic and does not depend on 6.97b having stamped the marker).
        loc = sum(union_span_len(loc_by_file[p]) for p in sorted(loc_by_file))
        gap = CoverageGap(
            id=gid,
            product_feature_id=(pf or None),
            kind=kind,  # type: ignore[arg-type]
            label=label,
            authored_label=authored_by_id.get(str(_get(uf, "id", "") or "")),
            routes=[str(r) for r in (_get(uf, "routes", None) or []) if r],
            surface_files=spans,
            loc=loc,
            synthesis_reason=_get(uf, "synthesis_reason", None),
        )
        pairs.append((uf, gap))

    # Ref-integrity — drop a gap whose home PF matches no emitted PF (defensive;
    # markers inherit an already-I12-reconciled ref, so this fires only when a
    # PF vanished between reconciliation and emission). Its marker then stays a
    # row (never silently removed in full mode) — no silent gap loss.
    gaps = [g for _, g in pairs]
    kept_gaps, ref_tele = enforce_gap_ref_integrity(gaps, product_features)
    kept_gap_ids = {id(g) for g in kept_gaps}
    surviving_markers = [uf for uf, g in pairs if id(g) in kept_gap_ids]

    # Deterministic ordering: (product_feature_id, id). Python's stable sort
    # preserves the marker input order for any (pf, id) tie.
    kept_gaps.sort(key=lambda g: (
        str(_get(g, "product_feature_id", "") or ""),
        str(_get(g, "id", "") or ""),
    ))

    converted = 0
    if mode == "full" and surviving_markers:
        rm = {id(uf) for uf in surviving_markers}
        user_flows[:] = [uf for uf in user_flows if id(uf) not in rm]
        converted = len(surviving_markers)

    sq = scan_meta.setdefault("synth_quality", {})
    sq["gap_channel_mode"] = mode
    sq["gaps_emitted"] = len(kept_gaps)
    # The B38 suppressor already ran; its count is the gaps that could not be
    # emitted (no evidence) — surfaced here so the bijection is auditable:
    # gaps_emitted + gaps_suppressed_no_coords == surviving member-less markers.
    sq["gaps_suppressed_no_coords"] = int(
        sq.get("markers_suppressed_no_coords", 0) or 0)
    sq["marker_rows_converted"] = converted
    if ref_tele.get("orphans_dropped"):
        # Additive — only when a gap was actually dropped, so clean boards
        # carry no extra key.
        sq["gap_channel_ref_integrity"] = ref_tele
    return kept_gaps


def run_synth_quality(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    scan_meta: dict[str, Any],
    vocab: Any | None = None,
    developer_features: list[Any] | None = None,
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
    # B23 — real code coordinates for the surviving markers (surface spans
    # from the mint-carried candidates / home-PF fallback; claimed-file and
    # measured-loc honesty gates). Runs AFTER the marker typing so the span
    # ledger exists exactly where the marker flag exists.
    coords_tele = attach_marker_surface_coords(
        user_flows, flows, product_features, developer_features, scan_meta)
    # B38 — zero-coordinate markers must not ship as display rows.
    suppress_tele = suppress_no_coords_markers(user_flows, scan_meta)
    # B45 — snapshot each surviving marker's RAW authored label BEFORE B31
    # renames + clears the carrier (only when the gap channel is armed, so the
    # off path stays byte-identical — no read, no snapshot, no side effects).
    gap_mode = coverage_gap_channel_mode()
    authored_snapshot = (
        _snapshot_authored_labels(user_flows) if gap_mode != "off" else {})
    # B31 — distinct display names for synthesized recall rows. Runs LAST
    # (this module is the final display writer of the scan): every naming
    # channel above — contract, personas, reground, marker typing — has
    # spoken, so any surviving collision is final unless resolved here.
    recall_tele = distinct_recall_row_names(
        user_flows, product_features, scan_meta, vocab=vocab)
    # B45 — the gap channel: build coverage_gaps[] from the SURVIVING member-less
    # markers (now with final, board-unique display names) and, in full mode,
    # remove those marker rows from user_flows[]. off => never runs => the
    # coverage_gaps key is absent and user_flows is byte-identical to pre-B45.
    # KEY-PRESENCE CONTRACT: consumers detect the gap-channel world by the
    # key's presence ("coverage_gaps" in scan — the warden gap-channel-leak
    # class + the flowless-silent gap exemption key off it), so dual/full
    # carry a LIST — possibly EMPTY (a zero-gap board still declares the
    # channel) — while off carries None (key absent, byte-identity).
    coverage_gaps: list[Any] | None = None
    if gap_mode != "off":
        coverage_gaps = emit_coverage_gaps(
            user_flows, product_features, developer_features, scan_meta,
            authored_snapshot, gap_mode)
    tele = {
        "enabled": True,
        "backstop_renamed": name_tele["renamed"],
        "backstop_confidence_raised": raised,
        "system_seeds_demoted": demote_tele["demoted"],
        "backpointers_cleared": demote_tele["backpointers_cleared"],
        "ui_chrome_demoted": chrome_tele["demoted"],
        "coverage_markers": marker_tele["marked"],
        "surface_coords_attached": coords_tele.get("attached", 0),
        "markers_suppressed_no_coords": suppress_tele.get("suppressed", 0),
        "recall_rows_renamed": recall_tele.get("renamed", 0),
        # B45 — the built gap objects, threaded to the Stage-7 result. None
        # in off mode (key absent from the output); a list — possibly
        # empty — in dual/full (key ALWAYS present: the channel-presence
        # contract).
        "coverage_gaps": coverage_gaps,
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
    "MARKER_SURFACE_COORDS_ENV",
    "RECALL_ROW_NAMES_ENV",
    "COVERAGE_GAP_CHANNEL_ENV",
    "SYSTEM_RECALL_REASON",
    "BACKSTOP_REASON",
    "E2E_RECALL_REASON",
    "ROUTE_GROUP_REASON",
    "PROMOTED_BACKSTOP_REASON",
    "synth_quality_enabled",
    "uf_worthiness_enabled",
    "backstop_owned_cover_enabled",
    "marker_surface_coords_enabled",
    "recall_row_names_enabled",
    "coverage_gap_channel_mode",
    "emit_coverage_gaps",
    "demote_system_flow_seeds",
    "demote_ui_chrome_ufs",
    "reground_backstop_uf_names",
    "honest_coverage_markers",
    "attach_marker_surface_coords",
    "distinct_recall_row_names",
    "run_synth_quality",
]
