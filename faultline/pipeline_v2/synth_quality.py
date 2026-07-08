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


def synth_quality_enabled() -> bool:
    """Default ON; ``FAULTLINE_SYNTH_QUALITY=0`` restores pre-B4 output
    byte-identically (both passes no-op)."""
    return os.environ.get(SYNTH_QUALITY_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


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

    for uf in user_flows:
        if _get(uf, "synthesis_reason", None) != BACKSTOP_REASON:
            continue
        mfids = _get(uf, "member_flow_ids", None) or []
        if len(mfids) != 1:
            continue  # only the unambiguous single-member case
        pf = pf_by_key.get(str(_get(uf, "product_feature_id", None) or ""))
        pf_display = (
            str(_get(pf, "display_name", None)
                or _get(pf, "name", "") or "")
            if pf is not None else ""
        )
        if _HUB_COMPOSE_SEP in pf_display:
            continue  # vendor-composed surface — vendor is the journey subject
        fl = flow_by_id.get(str(mfids[0]))
        if fl is None:
            continue
        verb, obj = _split_member(_member_flow_label(fl))
        if not verb.isalpha() or verb in noun_lead or not obj:
            continue  # no clean verb-led member evidence

        current = str(_get(uf, "name", "") or "")
        cur_tokens = {t for t in _KEBAB_SPLIT.split(current.lower()) if t}
        # Adopt only when the member adds ≥1 content token the template lacks.
        if not any(t not in cur_tokens for t in obj):
            continue

        candidate = polish_display_casing(
            verb.capitalize() + " " + " ".join(obj), v)
        cand_l = candidate.strip().lower()
        if cand_l == current.strip().lower():
            continue
        if display_law_violations(candidate, v, pf_display=pf_display or None):
            continue
        if cand_l in taken:  # never collide with another journey
            continue

        taken.discard(current.strip().lower())
        taken.add(cand_l)
        renames.append({
            "id": _get(uf, "id", None),
            "product_feature_id": _get(uf, "product_feature_id", None),
            "before": current, "after": candidate,
        })
        _set(uf, "name", candidate)

    if renames:
        scan_meta.setdefault("synth_quality", {})["backstop_renamed"] = renames
    return {"renamed": len(renames), "renames": renames}


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
    name_tele = reground_backstop_uf_names(
        user_flows, flows, product_features, scan_meta, vocab=vocab)
    tele = {
        "enabled": True,
        "backstop_renamed": name_tele["renamed"],
        "system_seeds_demoted": demote_tele["demoted"],
        "backpointers_cleared": demote_tele["backpointers_cleared"],
    }
    scan_meta.setdefault("synth_quality", {}).update({
        "backstop_renamed_count": name_tele["renamed"],
        "system_seeds_demoted": demote_tele["demoted"],
        "backpointers_cleared": demote_tele["backpointers_cleared"],
    })
    return tele


__all__ = [
    "SYNTH_QUALITY_ENV",
    "SYSTEM_RECALL_REASON",
    "BACKSTOP_REASON",
    "synth_quality_enabled",
    "demote_system_flow_seeds",
    "reground_backstop_uf_names",
    "run_synth_quality",
]
