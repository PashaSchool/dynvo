"""Stage 6.996 — plumbing-UF reclassification (B78 Seg F, post-journey-layer).

The "next-shell" disease after the mass rescue: journeys are alive, but a
tail of them are DEV-GRAIN plumbing wearing a journey label — synchronous
data-access / integration helpers the UF-namer templated into user_flows[]
('Fetch OpenAPI specifications', 'Check trial status', 'Lookup user by
email', cal.com 'Run <Vendor> jobs' x25). They are not user journeys; they
are the system/dev layer.

WHY NO EXISTING RUNG CATCHES THEM (forensics canon, 138-UF corpus census
2026-07-21): Stage 6.987 ``devgrain_demote`` reads the PF-grain anchor leaf
(and board-abstains on keyless Soc0); Stage 6.7e is a selection budget;
B40 is a confidence STAMP, never a demote; Stage 6.8b ``system_flows`` reads
a route's cron/queue/webhook TRIGGER (a synchronous lookup helper carries
none). **No rung reads the SHAPE of the UF itself.** This one does.

MECHANISM — extends the proven 6.987 (journey_step closed-set vocab) + B68
(terminal 4-way: "uncovered/noise does not exist; a demote MUST reclassify")
machinery to UF grain. A UF reclassifies to ``category="system"`` (+
``surface_scope="system"``, the existing system/dev-layer channel — Stage
6.8b, ``surface_taxonomy.classify_user_flow`` treats system as authoritative)
iff ALL hold:

  (a) PLUMBING NAME SHAPE — leading token is a strong data-access verb
      (fetch/lookup/query) OR an ambiguous verb (run/post/get/check) paired
      with a tech-object (jobs/operations/spec.../status/...). Closed-set
      vocab in ``data/plumbing-uf-vocab.yaml`` (with anti-cases in comments);
      the name gate is corroboration, NEVER sufficient alone.
  (b) MICRO PROFILE — ``member_count <= 2`` (the 5-member 'Run MCP
      operations' anti-case survives HERE — object matches, count vetoes).
  (c) NO PRODUCT SURFACE — routeless (any route is a navigable surface: the
      cal.com 'Run alls' routes=41 / 'Run forms' routes=10 KEEPs) AND no
      member file is a product PAGE (P1 page-evidence, reused verbatim from
      ``leafroute_promotion._page_files``). This is the failure-archaeology
      user-reachable veto (a post-UF demote must never eat user-facing —
      ``reclassify_service_internals`` it6 precedent).

CONSERVATION (B77 no-orphan): only ``category`` + ``surface_scope`` flip;
membership (``member_flow_ids`` / ``product_feature_id`` / member spans) is
READ-ONLY. Every fate is recorded BY ID in
``scan_meta["plumbing_uf_reclass"]`` (mirrors ``reclassify_service_internals``
"members untouched, fate recorded by id"). The name is NOT touched (naming
is frozen — this runs AFTER Stage 7 naming_contract).

Deterministic, $0 LLM, scale-invariant. Kill-switch
``FAULTLINE_PLUMBING_UF_RECLASS`` — DEFAULT OFF: unset / ``0`` / false / off
→ the pass never runs and user_flows[] + scan_meta stay byte-identical to
main. Flipped only by its own keyed A/B (a later flip commit).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from faultline.pipeline_v2.data import load_yaml

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import UserFlow

__all__ = [
    "PLUMBING_UF_RECLASS_ENV",
    "plumbing_uf_reclass_enabled",
    "load_plumbing_vocab",
    "plumbing_name_verdict",
    "run_plumbing_uf_reclass",
]

PLUMBING_UF_RECLASS_ENV = "FAULTLINE_PLUMBING_UF_RECLASS"

_DATA_FILE = "plumbing-uf-vocab.yaml"

#: Micro-profile ceiling — a count-grain constant (no per-repo tuning),
#: matching the devgrain_demote / spec 'mc<=2' band. The 5-member 'Run MCP
#: operations' anti-case sits just above it.
_MAX_MEMBER_COUNT = 2

#: The reclassification target — the existing system/dev layer channel
#: (typed as the Literal the UserFlow.category field expects).
_SYSTEM: Literal["system"] = "system"

#: Word tokenizer: lowercase alphanumeric runs (so '8x8', 'v1' survive as
#: single tokens; punctuation like '(MCP)' splits away).
_WORD = re.compile(r"[a-z0-9]+")


def plumbing_uf_reclass_enabled() -> bool:
    """Default **OFF**. Unset / ``0`` / false / no / off → the stage is not
    entered and the scan is byte-identical to pre-Seg-F. Flipped only by its
    own keyed A/B (a separate flip commit carries the KEY_SCHEMA bump)."""
    return os.environ.get(PLUMBING_UF_RECLASS_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@lru_cache(maxsize=1)
def load_plumbing_vocab() -> dict[str, frozenset[str]]:
    """Load the closed plumbing vocab as frozensets (membership tests only —
    the matcher iterates NOTHING, keeping the set-iteration nondeterminism
    class out of this rail). Missing/empty file → empty sets → inert pass."""
    block = (load_yaml(_DATA_FILE) or {}).get("plumbing_uf") or {}

    def _fs(key: str) -> frozenset[str]:
        return frozenset(
            str(t).strip().lower()
            for t in (block.get(key) or ())
            if str(t).strip()
        )

    return {
        "verbs_strong": _fs("verbs_strong"),
        "verbs_object": _fs("verbs_object"),
        "objects": _fs("objects"),
    }


def _tokens(name: str) -> list[str]:
    return _WORD.findall(str(name or "").lower())


def plumbing_name_verdict(
    name: str, vocab: dict[str, frozenset[str]] | None = None
) -> str | None:
    """Gate (a). Return the firing evidence tag (``"verb_strong:<v>"`` /
    ``"verb_object:<v>+<obj>"``) when *name* reads as a plumbing helper, else
    ``None``.

    Closed-set discipline (B30): a LEADING strong verb fires alone; a leading
    ambiguous verb fires ONLY with a tech-object token anywhere in the name.
    No fuzzy/substring matching. The name gate is corroboration — the caller
    still enforces the structural gates (b)/(c)."""
    v = vocab or load_plumbing_vocab()
    toks = _tokens(name)
    if not toks:
        return None
    head = toks[0]
    if head in v["verbs_strong"]:
        return f"verb_strong:{head}"
    if head in v["verbs_object"]:
        obj = next((t for t in toks[1:] if t in v["objects"]), None)
        if obj is not None:
            return f"verb_object:{head}+{obj}"
    return None


# ── house dict/object-agnostic getters (mirror terminal_classification) ──────


def _member_surface_files(
    uf: "UserFlow", flows_by_id: dict[str, Any]
) -> list[str]:
    """Entry file + touched paths of every member flow (deduped, stable
    order). Empty when no member resolves (a fully synthetic row) — such a
    row simply carries no page evidence."""
    out: list[str] = []
    seen: set[str] = set()
    for mid in uf.member_flow_ids or []:
        fl = flows_by_id.get(str(mid))
        if fl is None:
            continue
        cands = [getattr(fl, "entry_point_file", None)]
        cands.extend(getattr(fl, "paths", None) or [])
        for c in cands:
            sp = str(c or "")
            if sp and sp not in seen:
                seen.add(sp)
                out.append(sp)
    return out


def run_plumbing_uf_reclass(
    user_flows: list["UserFlow"],
    flows_by_id: dict[str, Any],
    page_ri_files: set[str],
) -> dict[str, Any]:
    """See module docstring. Mutates eligible UFs (``category`` +
    ``surface_scope`` → ``"system"``) in place; returns telemetry for
    ``scan_meta["plumbing_uf_reclass"]``. Caller guards on
    :func:`plumbing_uf_reclass_enabled` — a safe no-op if called with the
    flag off."""
    tele: dict[str, Any] = {
        "enabled": True,
        "candidates": 0,
        "reclassified": [],
        "abstained": [],
        "by_verb": {},
    }
    if not plumbing_uf_reclass_enabled():
        tele["enabled"] = False
        return tele

    # Lazy import: the OFF path never pays it, and it keeps the P1 page-detect
    # a single source of truth (spec: reuse leafroute, do not duplicate).
    from faultline.pipeline_v2.leafroute_promotion import _page_files

    vocab = load_plumbing_vocab()
    by_verb: dict[str, int] = {}

    for uf in user_flows:
        # System-only demote of INTERACTIVE product journeys. A UF that is
        # already system (Stage 6.8b / reclassify_service_internals), a
        # coverage marker, a synthesized/backstop row, or a transport-laned
        # row is a different channel — never re-touched here.
        if str(getattr(uf, "category", "") or "") != "interactive":
            continue
        if (getattr(uf, "is_coverage_marker", False)
                or getattr(uf, "synthesized", False)
                or getattr(uf, "synthesis_reason", None)
                or getattr(uf, "lane_ref", None)):
            continue

        verdict = plumbing_name_verdict(getattr(uf, "name", "") or "", vocab)
        if verdict is None:
            continue  # not a plumbing NAME shape — the vast majority

        tele["candidates"] += 1
        name = str(getattr(uf, "name", "") or "")
        uid = str(getattr(uf, "id", "") or "")

        # ── gate (b): micro profile ──────────────────────────────────────
        mc = int(getattr(uf, "member_count", 0) or 0)
        if mc == 0:
            mc = len(getattr(uf, "member_flow_ids", None) or [])
        if mc > _MAX_MEMBER_COUNT:
            tele["abstained"].append(
                {"id": uid, "name": name, "reason": "member_count", "mc": mc})
            continue

        # ── gate (c): no product surface — routeless AND no member page ───
        if getattr(uf, "routes", None):
            tele["abstained"].append({
                "id": uid, "name": name, "reason": "routed",
                "routes": len(uf.routes)})
            continue
        surfaces = _member_surface_files(uf, flows_by_id)
        if surfaces and _page_files(surfaces, page_ri_files):
            tele["abstained"].append(
                {"id": uid, "name": name, "reason": "page_surface"})
            continue

        # ── reclassify — the ONLY mutation: category + surface_scope ──────
        uf.category = _SYSTEM
        uf.surface_scope = _SYSTEM
        head = verdict.split(":", 1)[1].split("+", 1)[0]
        by_verb[head] = by_verb.get(head, 0) + 1
        tele["reclassified"].append({
            "id": uid, "name": name, "mc": mc,
            "evidence": verdict, "into": _SYSTEM,
        })

    tele["reclassified"].sort(key=lambda r: r["id"])
    tele["abstained"].sort(key=lambda r: r["id"])
    tele["by_verb"] = dict(sorted(by_verb.items()))
    tele["count"] = len(tele["reclassified"])
    return tele
