"""Stage 6.88 — journey lattice (Product-Spine W5).

Post-abstraction DETERMINISTIC partition of catch-all journeys + exact
subset-duplicate merge, running AFTER 6.7d + every seed channel (the
journey layer is settled) and BEFORE dual-evidence / taxonomy / naming.

The operator case (2026-07-07 panels): the 6.7d abstraction prior is
"one journey per capability" (its jpf corrective says so verbatim), so
a 47-member "Create and manage investigations" or a 51-member "Build
automated workflows" ships as ONE unrecognizable catch-all journey.
The A3 panel-ranked SEV-1 list (Soc0 "View network security" garbage
bucket, papermark UF-003/008/004) is exactly this class.

DOCTRINE (brief §MECHANICS): the partition is DETERMINISTIC — member
flows cluster on evidence axes; the LLM personas only NAME the result
(PM Labeler, selection-not-generation) and REVIEW a proposed split
(Draft Verifier; reject → the original catch-all survives untouched —
the conservative fallback, nothing is ever lost). No LLM ever moves
membership (persona invariant zero).

Evidence axes per member flow (first resolving wins):

  * ``route``   — the tenancy-transparent route family of the flow's
    entry pattern (:func:`spine_anchors._pattern_key_chain`): the first
    meaningful URL segment that is not the capability's own root word
    and not a CRUD leaf (``/api/teams/{id}/domains/verify`` under the
    workflows capability keys ``domains``).
  * ``section`` — the W4 page-interior section of the flow's entry page
    (Stage 6.55 evidence, same-PF pages only — authored vocabulary).
  * ``dir``     — the entry file's first non-structural directory
    segment (tenant-scope-aware), file stem as the last resort.

The flow-verb axis (naming vocab ``flow_verb_classes``) NAMES clusters
("<Verb> <object>") and backs the grain guard; it never partitions —
CRUD variants of one object stay one journey (actor+intent+outcome).

Cluster → journey-candidate bar: ≥ 2 member flows OR ≥ 150 merged span
LOC (the D4 husk-floor bond; a full page a single flow enters is a real
journey, a stray helper is not). A cluster whose key tokens are the
capability's own root stays the CORE — it never splits away from the
parent journey (the parent keeps its id and name over core+residual
members; it dissolves only when every member left for a cluster).

Catch-all detection: an eligible journey whose members cover ≥ 3
qualifying buckets (and would mint ≥ 2 non-core journeys) splits.
Conservation: children + parent residual partition EXACTLY the split
parents' member union — checked, and the plan reverts on any mismatch.

Identity (keeper contract): children take canonical content ids
``UF-L-<sha1(pf|key)[:10]>`` — stable across rescans by construction;
untouched journeys keep their ids byte-identically. Children carry the
binding note ``domain = "lattice:<axis>:<key>"``.

Wiring contract (phase_finalize): when the pass changed anything the
caller re-runs UF conservation (§4.5 — children resettle to the PF
their spans live in), then :func:`dedup_lattice_journeys` (post-
resettle same-key merge), then the donor backstop (I8 stays green).
Downstream stages do the rest: emission integrity rewrites the I14
flow backpointers from final membership; the surface taxonomy scopes
the new journeys; Stage 6.87 polishes displays under the W3 laws.

Kill-switch: ``FAULTLINE_JOURNEY_LATTICE=0`` (default ON) restores
pre-W5 output byte-identically. Deterministic and $0 keyless — the
personas are keyed-only seams injected by the caller.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from faultline.pipeline_v2.dominant_evidence import (
    dominant_evidence_naming_enabled as _dominant_naming_on,
)

logger = logging.getLogger(__name__)

__all__ = [
    "JOURNEY_LATTICE_ENV",
    "JOURNEY_LATTICE_V2_ENV",
    "journey_lattice_enabled",
    "journey_lattice_v2_enabled",
    "run_journey_lattice",
    "dedup_lattice_journeys",
    "fold_thin_lattice_children",
]

JOURNEY_LATTICE_ENV = "FAULTLINE_JOURNEY_LATTICE"

#: W5.1 post-thinning fold kill-switch (default ON). ``=0`` restores the
#: pre-fix lattice output (children left thin by the post-apply resettle).
LATTICE_THIN_FOLD_ENV = "FAULTLINE_LATTICE_THIN_FOLD"

#: W5.2 ACTION-axis kill-switch (default ON; fix B6). ``=0`` restores the
#: pre-B6 object-only lattice output byte-identically. The action axis is an
#: additive second partition dimension that runs ONLY on organic journeys the
#: object axis could not split (single-router REST surfaces collapse to one
#: route family); it never touches the object-axis code path.
JOURNEY_LATTICE_V2_ENV = "FAULTLINE_JOURNEY_LATTICE_V2"

#: W5.2 fix B7 — action-axis I15 eligibility in MAX-CHILD form (default ON).
#: ``=0`` restores B6's parent-only attach floor byte-identically (the
#: eligibility gate then skips a catch-all purely on its pre-split parent
#: share). B7 relaxes that gate to skip ONLY a UNIFORMLY misattached
#: capability (parent AND every minted child below the floor); a diluted
#: parent that still contains a floor-clearing sub-journey splits. Monotone:
#: B7 never un-splits a B6 split (NEW-skip ⊆ OLD-skip).
JOURNEY_LATTICE_B7_ENV = "FAULTLINE_JOURNEY_LATTICE_B7"

#: Fix B25 — verifier-revert slot release (default ON). A plan the Draft
#: Verifier fully reverts leaves its catch-all byte-identical on the board,
#: yet its parent UF was already recorded in the action axis' claimed-UF
#: exclusion set (computed PRE-verifier) — permanently consuming the PF's
#: single action-split slot. Soc0 'Manage cases end-to-end' (waves 12-14):
#: a doomed object plan ('View export' — a 28-member single-router route
#: shard) was honestly rejected, its revert restored the catch-all, and the
#: healthy action plan (browse/view/create/update, all I16-clean) was never
#: built. With B25 ON, a pf whose plans ALL fully reverted — and that never
#: got a pass-1 action plan (a reverted ACTION plan is final; no re-entry)
#: — re-runs the Phase-2b action detection once, screened by ONE additional
#: verifier batch. Hard cap: a slot releases at most ONCE per pf per scan.
#: Keyless byte-neutral by construction (no verifier → no reverts → the
#: path is unreachable). ``=0`` restores the pre-B25 slot behavior
#: byte-identically.
JOURNEY_LATTICE_B25_ENV = "FAULTLINE_JOURNEY_LATTICE_B25"

#: Canonical child-id prefix (content-derived; see module docstring).
_LATTICE_ID_PREFIX = "UF-L-"

#: Journey-candidate bar: a cluster is mintable with >= 2 member flows
#: OR >= this much merged span LOC (the D4 husk-floor bond — an
#: absolute "real journey mass" floor, scale-invariant by not scaling
#: with repo size; same constant class as Stage 6.86's husk floor).
_MIN_CLUSTER_LOC = 150

#: A journey splits when its members cover >= this many qualifying
#: buckets (non-core clusters + the core/residual bucket) — the brief's
#: catch-all bar.
_CATCHALL_MIN_CLUSTERS = 3

#: A split must mint at least this many non-core journeys (a "split"
#: that yields one child is a rename, not a partition).
_MIN_MINTABLE = 2

#: CRUD / leaf-action URL segments — never an object family key (the
#: actor+intent+outcome guard: ``/investigations/{id}/edit`` is the SAME
#: journey as the investigations core, not an "edit" journey).
_CRUD_LEAF_SEGS = frozenset({
    "new", "edit", "create", "update", "delete", "remove", "add",
    "index", "list", "detail", "details", "get", "post", "put", "patch",
    "duplicate", "clone", "toggle", "bulk",
})

#: API-TIER segments (``/api/v1/management/surveys`` vs
#: ``/api/v1/client/{env}/surveys``) — the tier is an ARCHITECTURE split,
#: not a journey object, so it is transparent EXACTLY like tenant scope:
#: only when a deeper meaningful segment exists. A repo whose ``clients``
#: ARE the product entity (``/api/clients/{id}``, CRM class) keeps the
#: key — no deeper segment to descend to. Keyless formbricks probe
#: exhibit: "Manage client" / "View management" journeys.
_TIER_SEGS = frozenset({"management", "client", "internal", "public"})

#: Structural directory segments skipped by the entry-dir axis (never
#: object families; mirrors the 6.7d structure-leak class).
_STRUCTURAL_DIR_SEGS = frozenset({
    "src", "app", "apps", "pages", "page", "api", "routes", "route",
    "packages", "components", "component", "lib", "libs", "utils",
    "util", "server", "client", "modules", "module", "features",
    "feature", "backend", "frontend", "web", "www", "ui", "core",
    "shared", "common", "internal", "services", "service", "handlers",
    "handler", "routers", "router", "controllers", "controller",
    "views", "hooks", "helpers", "types", "styles", "public", "static",
    "assets", "config", "scripts", "tests", "test", "__tests__",
    "trpc", "rest", "graphql", "rpc", "v1", "v2", "v3", "ee",
})

#: Verb-verdict → deterministic journey verb word (naming vocab's
#: ``flow_verb_classes`` verdict space + the "manage" fallback).
_VERB_WORD = {
    "connect": "Connect",
    "ingest": "Ingest",
    "send": "Send",
    "receive": "Receive",
    "run": "Run",
    "view": "View",
    "manage": "Manage",
}

#: Deterministic verb order for dominance ties (vocab order).
_VERB_ORDER = ("connect", "ingest", "send", "receive", "run", "view",
               "manage")


def journey_lattice_enabled() -> bool:
    """Default ON; ``FAULTLINE_JOURNEY_LATTICE=0`` restores pre-W5
    output byte-identically."""
    return os.environ.get(JOURNEY_LATTICE_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def journey_lattice_v2_enabled() -> bool:
    """Action axis (B6). Default ON; ``FAULTLINE_JOURNEY_LATTICE_V2=0``
    restores the pre-B6 object-only lattice output byte-identically."""
    return os.environ.get(JOURNEY_LATTICE_V2_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def journey_lattice_b7_enabled() -> bool:
    """Max-child I15 eligibility (B7). Default ON;
    ``FAULTLINE_JOURNEY_LATTICE_B7=0`` restores B6's parent-only attach floor
    byte-identically (the eligibility gate skips on the pre-split parent share
    alone)."""
    return os.environ.get(JOURNEY_LATTICE_B7_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def journey_lattice_b25_enabled() -> bool:
    """Verifier-revert slot release (B25). Default ON;
    ``FAULTLINE_JOURNEY_LATTICE_B25=0`` restores the pre-B25 behavior
    byte-identically (a fully-reverted plan keeps its pf's action-split
    slot consumed). Keyless scans never reach the gated path (no verifier
    → no reverts), so the flag is inert there in both positions."""
    return os.environ.get(JOURNEY_LATTICE_B25_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# ── Small deterministic helpers ─────────────────────────────────────────


def _norm_token(word: str) -> str:
    """Lower-cased alnum token, plural ``s`` stripped (len > 3) — the
    same light normalization 6.7d's resource matching uses."""
    t = re.sub(r"[^a-z0-9]+", "", str(word or "").lower())
    if len(t) > 3 and t.endswith("s"):
        t = t[:-1]
    return t


#: camelCase / PascalCase boundary (``CaseDetailPage`` → 3 words).
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

#: Trailing UI-container words dropped from MULTI-token keys — a page
#: component names its OBJECT, not a "page" family (``ChatPage`` keys
#: ``chat`` and merges with the chat evidence family; a single-token
#: ``view``/``page`` key survives untouched — papermark ``/view``).
_TRAILING_CONTAINER_TOKENS = frozenset({
    "page", "pages", "screen", "screens", "tab", "tabs",
})


def _key_words(text: str) -> list[str]:
    """Camel-split words of a segment; trailing container words AND
    trailing CRUD-leaf words dropped (multi-word only) — a detail/edit
    view of an object is the SAME journey as the object
    (``CaseDetailPage`` → ``case``; ``bulk-update`` → ``bulk`` → the
    single-token CRUD check then drops the segment entirely). Casing
    preserved for display derivation."""
    parts = [w for w in re.split(r"[^A-Za-z0-9]+",
                                 _CAMEL_RE.sub("-", str(text or ""))) if w]
    while len(parts) > 1 and (
        parts[-1].lower() in _TRAILING_CONTAINER_TOKENS
        or _norm_token(parts[-1]) in _CRUD_LEAF_SEGS
    ):
        parts.pop()
    return parts


def _norm_key(text: str) -> str:
    """Normalized multi-word cluster key: camel-split, container-word
    dropped, per-word ``_norm_token``, joined by ``-`` ("AI Copilot" →
    "ai-copilot", "CaseDetailPage" → "case-detail")."""
    toks = [_norm_token(w) for w in _key_words(text)]
    return "-".join(t for t in toks if t)


def _key_tokens(key: str) -> frozenset[str]:
    return frozenset(t for t in key.split("-") if t)


def _flow_member_id(flow: Any) -> str:
    """uuid first, name fallback — mirrors Stage 6.7's ``_flow_key``."""
    return str(getattr(flow, "uuid", "") or getattr(flow, "name", "") or "")


def _entry_file_of(flow: Any) -> str | None:
    entry = getattr(flow, "entry_point_file", None)
    if entry:
        return str(entry)
    ep = getattr(flow, "entry_point", None)
    if ep is not None:
        path = ep.get("path") if isinstance(ep, dict) else getattr(ep, "path", None)
        if path:
            return str(path)
    return None


def _flow_span_loc(flow: Any) -> int:
    """Merged span LOC of one flow — the degenerate-span ruler's span
    source (``nodes[].lines`` merged per file; attribution spans as the
    fallback)."""
    spans_by_file: dict[str, list[tuple[int, int]]] = {}
    for n in (getattr(flow, "nodes", None) or []):
        lines = getattr(n, "lines", None)
        if lines:
            spans_by_file.setdefault(str(getattr(n, "file", "")), []).append(
                (int(lines[0]), int(lines[1])))
    if not spans_by_file:
        for a in (getattr(flow, "flow_symbol_attributions", None) or []):
            spans_by_file.setdefault(str(getattr(a, "file", "")), []).append(
                (int(getattr(a, "line_start", 0)),
                 int(getattr(a, "line_end", 0))))
    loc = 0
    for file_spans in spans_by_file.values():
        merged: list[tuple[int, int]] = []
        for s, e in sorted(file_spans):
            if merged and s <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        loc += sum(e - s + 1 for s, e in merged)
    return loc


def _pf_key_of(pf: Any) -> str:
    return str(getattr(pf, "name", "") or "")


def _pf_display_of(pf: Any) -> str:
    return str(
        getattr(pf, "display_name", None) or getattr(pf, "name", "") or ""
    )


def _pf_root_tokens(pf: Any) -> frozenset[str]:
    """The capability's own root vocabulary: slug words + display words
    + the anchor path's LAST segment words. A cluster keyed inside this
    set is the capability CORE, not a sub-journey."""
    toks: set[str] = set()
    for source in (getattr(pf, "name", None), getattr(pf, "display_name", None)):
        for w in re.split(r"[^A-Za-z0-9]+", str(source or "")):
            t = _norm_token(w)
            if t:
                toks.add(t)
    aid = str(getattr(pf, "anchor_id", None) or "")
    if ":" in aid:
        tail = aid.split(":", 1)[1].replace("\\", "/").rstrip("/")
        last = tail.rsplit("/", 1)[-1]
        for w in re.split(r"[^A-Za-z0-9]+", last):
            t = _norm_token(w)
            if t:
                toks.add(t)
    return frozenset(toks)


# ── Evidence axes ───────────────────────────────────────────────────────


def _route_family(
    entry_file: str | None,
    patterns_by_file: Mapping[str, list[str]],
    root_toks: frozenset[str],
    spine_vocab: dict[str, Any],
    version_re: re.Pattern[str],
) -> tuple[str, str] | None:
    """``(norm_key, phrase)`` from the flow's entry route pattern(s) —
    the first meaningful chain segment beyond the capability root that
    is not a CRUD leaf. Deterministic: patterns sorted, first hit wins."""
    if not entry_file:
        return None
    from faultline.pipeline_v2.spine_anchors import _pattern_key_chain

    for pattern in sorted(patterns_by_file.get(entry_file, ())):
        chain = _pattern_key_chain(pattern, spine_vocab, version_re)
        for i, seg in enumerate(chain):
            tok = _norm_token(seg)
            if not tok or tok in root_toks or tok in _CRUD_LEAF_SEGS:
                continue
            if tok in _TIER_SEGS and any(
                _norm_token(s) and _norm_token(s) not in _CRUD_LEAF_SEGS
                for s in chain[i + 1:]
            ):
                continue  # architecture tier, deeper object exists
            key = _norm_key(seg)
            if not key or key in _CRUD_LEAF_SEGS:
                continue  # reduced to a bare CRUD leaf — same journey
            return key, " ".join(_key_words(seg))
    return None


def _section_family(
    entry_file: str | None,
    pf_key: str,
    interior_pages: Mapping[str, Mapping[str, Any]],
    pf_key_by_display: Mapping[str, str],
) -> tuple[str, str] | None:
    """``(norm_key, phrase)`` from the entry page's FIRST product
    section label (W4 interior evidence) — same-PF pages only, so the
    authored label is verifiable against this capability's subtree."""
    if not entry_file:
        return None
    info = interior_pages.get(entry_file)
    if not info:
        return None
    owner = pf_key_by_display.get(str(info.get("pf") or "").strip().lower())
    if owner != pf_key:
        return None
    for label in info.get("sections") or ():
        key = _norm_key(str(label))
        if key:
            return key, str(label).strip()
    return None


def _dir_family(
    entry_file: str | None,
    root_toks: frozenset[str],
    tenant_scope: frozenset[str],
) -> tuple[str, str] | None:
    """``(norm_key, phrase)`` from the entry file's directory chain —
    first non-structural, non-root, non-CRUD segment (tenant-scope
    addressing skipped like the route axis); file stem as last resort."""
    if not entry_file:
        return None
    segs = [s for s in entry_file.replace("\\", "/").split("/") if s]
    if not segs:
        return None
    *dirs, fname = segs
    candidates: list[str] = []
    for i, seg in enumerate(dirs):
        low = seg.lower()
        if low in _STRUCTURAL_DIR_SEGS:
            continue
        if re.match(r"^[\[\($:{<*_.]", seg):
            continue  # dynamic / group / private segments
        if (low in tenant_scope and i + 1 < len(dirs)
                and re.match(r"^[\[\($:{<*]", dirs[i + 1])):
            continue  # pure tenant addressing — transparent
        candidates.append(seg)
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", fname)
    if not candidates and stem:
        low_stem = stem.lower()
        if (low_stem not in _STRUCTURAL_DIR_SEGS
                and low_stem not in {"page", "layout", "route", "+page",
                                     "+server", "default"}
                and not re.match(r"^[\[\($:{<*_.]", stem)):
            candidates.append(stem)
    for i, seg in enumerate(candidates):
        tok = _norm_token(seg)
        if not tok or tok in root_toks or tok in _CRUD_LEAF_SEGS:
            continue
        if tok in _TIER_SEGS and any(
            _norm_token(s) and _norm_token(s) not in _CRUD_LEAF_SEGS
            for s in candidates[i + 1:]
        ):
            continue  # architecture tier, deeper object exists
        key = _norm_key(seg)
        if not key or key in _CRUD_LEAF_SEGS:
            continue  # reduced to a bare CRUD leaf — same journey
        return key, " ".join(_key_words(seg))
    return None


# ── Cluster / plan data shapes ──────────────────────────────────────────


@dataclass
class _Cluster:
    key: str                       # normalized cluster key (canonical id base)
    axis: str                      # section | route | dir (best evidence seen)
    phrase: str                    # display phrase for the object
    mids: list[str] = field(default_factory=list)   # member ids, pooled order
    verbs: list[str] = field(default_factory=list)  # per-member verb verdicts
    loc: int = 0
    is_core: bool = False


#: Axis priority for phrase/axis when clusters merge (authored beats URL
#: beats directory).
_AXIS_RANK = {"section": 0, "route": 1, "dir": 2}


@dataclass
class _NameItem:
    """Duck-typed pending item for the PM Labeler (naming_contract
    ``_PendingItem`` attribute contract)."""

    kind: str
    key: str
    current: str
    candidates: list[str]
    context: dict[str, Any] = field(default_factory=dict)
    obj: Any = None
    pf_display: str | None = None


def _dominant_verb(verbs: Iterable[str]) -> str:
    counts: dict[str, int] = {}
    for v in verbs:
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return "manage"
    return sorted(
        counts,
        key=lambda v: (-counts[v], _VERB_ORDER.index(v)
                       if v in _VERB_ORDER else len(_VERB_ORDER)),
    )[0]


def _child_id(pf_key: str, cluster_key: str) -> str:
    digest = hashlib.sha1(
        f"{pf_key}|{cluster_key}".encode("utf-8")).hexdigest()
    return f"{_LATTICE_ID_PREFIX}{digest[:10]}"


def _intent_for_verb(verdict: str) -> str:
    """UF ``intent`` for a verb verdict (mirrors 6.7d ``_INTENT``)."""
    return {
        "connect": "execute", "ingest": "bulk", "send": "execute",
        "receive": "execute", "run": "execute", "view": "browse",
        "manage": "manage",
    }.get(verdict, "manage")


def _deterministic_name(
    verdict: str,
    phrase: str,
    pf_display: str,
    naming_vocab: Mapping[str, Any],
    member_evidence: Sequence[tuple[str, str]] | None = None,
) -> tuple[str, list[str]]:
    """``(name, candidates)`` — verb-led "<Verb> <object>" templates in
    W3-law-clean form (first clean candidate wins; the list feeds the
    PM Labeler).

    ``member_evidence`` (B78 Seg H, ``FAULTLINE_DOMINANT_EVIDENCE_NAMING``)
    — ``(flow-name, entry-file)`` pairs of the child's members. When the
    flag is armed the object phrase drops content tokens with member
    support < 0.34 (drop-only; an emptied phrase keeps the original —
    reject ⇒ the unchanged deterministic channel). ``None`` / flag OFF ⇒
    byte-identical."""
    from faultline.pipeline_v2.naming_contract import (
        _resource_phrase,
        display_law_violations,
        polish_display_casing,
    )

    obj = _resource_phrase(phrase, naming_vocab)
    if member_evidence is not None:
        from faultline.pipeline_v2.dominant_evidence import (
            dominant_evidence_naming_enabled,
            strip_display_tokens,
            unsupported_display_tokens,
        )
        if dominant_evidence_naming_enabled() and obj:
            _drop = unsupported_display_tokens(
                obj, member_evidence, resource="", vocab=naming_vocab)
            if _drop:
                _stripped = strip_display_tokens(obj, _drop, naming_vocab)
                if _stripped:
                    obj = _stripped
    primary = _VERB_WORD.get(verdict, "Manage")
    raw: list[str] = [f"{primary} {obj}"]
    for alt in ("Manage", "View", "Configure"):
        cand = f"{alt} {obj}"
        if cand not in raw:
            raw.append(cand)
    polished = [polish_display_casing(c, naming_vocab) for c in raw]
    clean = [
        c for c in polished
        if not display_law_violations(c, naming_vocab,
                                      pf_display=pf_display or None)
    ]
    if clean:
        return clean[0], clean
    return polished[0], polished


# ── Phase 1: exact subset-duplicate merge ───────────────────────────────


def _merge_subset_duplicates(
    user_flows: list[Any],
    eligible_ids: set[str],
    tele: dict[str, Any],
) -> None:
    """Drop journey A when A's member set ⊆ B's member set on the SAME
    capability (the Soc0 hunts byte-for-byte nesting). Equal sets keep
    the lexicographically-smaller id. Members are already inside B —
    nothing is lost."""
    by_pf: dict[str, list[Any]] = {}
    for uf in user_flows:
        if str(getattr(uf, "id", "") or "") not in eligible_ids:
            continue
        pfid = getattr(uf, "product_feature_id", None)
        if not pfid:
            continue
        by_pf.setdefault(str(pfid), []).append(uf)

    drop_ids: set[str] = set()
    for pfid in sorted(by_pf):
        group = sorted(
            by_pf[pfid],
            key=lambda u: (len(getattr(u, "member_flow_ids", None) or []),
                           str(getattr(u, "id", "") or "")),
        )
        sets = {
            str(u.id): set(getattr(u, "member_flow_ids", None) or [])
            for u in group
        }
        by_id = {str(u.id): u for u in group}
        for i, a in enumerate(group):
            aid = str(a.id)
            if aid in drop_ids or not sets[aid]:
                continue
            for b in group[i + 1:]:
                bid = str(b.id)
                if bid in drop_ids:
                    continue
                if sets[aid] == sets[bid]:
                    dropped, kept = bid, aid  # equal sets — later id goes
                elif sets[aid] < sets[bid]:
                    dropped, kept = aid, bid  # strict subset — A goes
                else:
                    continue
                drop_ids.add(dropped)
                tele["subset_merged"] += 1
                if len(tele["subset_merged_pairs"]) < 10:
                    tele["subset_merged_pairs"].append({
                        "dropped": getattr(by_id[dropped], "name", ""),
                        "into": getattr(by_id[kept], "name", ""),
                        "pf": pfid,
                        "members": len(sets[dropped]),
                    })
                if dropped == aid:
                    break
    if drop_ids:
        user_flows[:] = [
            u for u in user_flows
            if str(getattr(u, "id", "") or "") not in drop_ids
        ]


# ── Action axis (W5.2 / B6) — within-resource intent partition ──────────

#: Action-family vocab file (B6). Runtime source of truth; byte-identical
#: authoring copy at eval/journey-action-families.yaml (drift-guarded).
_ACTION_FAMILIES_FILE = "journey-action-families.yaml"

#: Deterministic child order + display word + reused verb verdict per action
#: family. Reads carry the "view" verdict (intent ``browse``); writes + the
#: domain-action ("act") family carry "manage".
_ACTION_FAMILY_ORDER = ("browse", "view", "create", "update", "delete", "act")
_ACTION_WORD = {
    "browse": "Browse", "view": "View", "create": "Create",
    "update": "Update", "delete": "Delete", "act": "Manage",
}
_ACTION_VERDICT = {
    "browse": "view", "view": "view", "create": "manage",
    "update": "manage", "delete": "manage", "act": "manage",
}

#: The validator's I15 attach-overlap floor (eval/validate_scan.py), applied
#: at ACTION-child mint time — a reused ruler, not new tuning. A journey whose
#: non-lane flow files sit < 34% inside its PF scope is misattached by the
#: board's own measure; minting children of a borderline capability
#: re-measures the same weak mass N times and multiplies I15 rows (papermark
#: 'workflows' class: parent passed lane-aware by a hair, five children at
#: 0.29-0.33 each minted a fresh row).
_I15_ATTACH_FLOOR = 0.34


def _flow_files_of(flow: Any) -> set[str]:
    """The validator's ``_spine_flow_files`` ruler: ``paths`` first, the
    entry-point file as the fallback."""
    ps = getattr(flow, "paths", None) or []
    if not ps:
        e = _entry_file_of(flow)
        ps = [e] if e else []
    return {str(p) for p in ps}


def _lane_attach_share(
    files: set[str], scope: set[str], lane_files: frozenset[str] | set[str],
) -> float | None:
    """Lane-aware attach share (validator I15 ruler): lane-owned files are
    neutral ground a journey traverses — excluded from the denominator.
    ``None`` = lane-only file set (the validator skips those UFs)."""
    eff = files - lane_files
    if not eff:
        return None
    return len(eff & scope) / len(eff)


def load_action_families() -> dict[str, Any]:
    """Packaged action-family vocab (cached by the loader — pure data)."""
    from faultline.pipeline_v2.data import load_yaml

    return load_yaml(_ACTION_FAMILIES_FILE)


def _action_family(name: str, vocab: Mapping[str, Any]) -> str | None:
    """Coarse user-intent family from a flow name's LEADING verb (the leading
    token of a code-generated flow name IS its action). No secondary-token
    fallback — precision over recall; an unclassified head folds to residual
    and stays with the parent. GET-class reads split by an id marker
    (collection ``browse`` vs single-member ``view``)."""
    toks = [t for t in re.split(r"[^a-z0-9]+", str(name or "").lower()) if t]
    if not toks:
        return None
    head = toks[0]
    id_markers = {str(m).lower() for m in (vocab.get("id_markers") or ("id",))}
    has_id = bool(id_markers & set(toks))
    if head in {str(v).lower() for v in (vocab.get("browse") or ())}:
        return "browse"
    if head in {str(v).lower() for v in (vocab.get("read") or ())}:
        return "view" if has_id else "browse"
    if head in {str(v).lower() for v in (vocab.get("create") or ())}:
        return "create"
    if head in {str(v).lower() for v in (vocab.get("update") or ())}:
        return "update"
    if head in {str(v).lower() for v in (vocab.get("delete") or ())}:
        return "delete"
    if head in {str(v).lower() for v in (vocab.get("act") or ())}:
        return "act"
    return None


def _action_child_name(
    family: str, pf_display: str, naming_vocab: Mapping[str, Any],
) -> tuple[str, list[str]]:
    """``(name, candidates)`` — "<ActionWord> <resource>" in W3-law-clean form
    (mirrors :func:`_deterministic_name`; first clean candidate wins)."""
    from faultline.pipeline_v2.naming_contract import (
        _resource_phrase,
        display_law_violations,
        polish_display_casing,
    )

    obj = _resource_phrase(pf_display, naming_vocab) or pf_display
    word = _ACTION_WORD.get(family, "Manage")
    raw: list[str] = [f"{word} {obj}"]
    for alt in ("Manage", "View", "Configure"):
        cand = f"{alt} {obj}"
        if cand not in raw:
            raw.append(cand)
    polished = [polish_display_casing(c, naming_vocab) for c in raw]
    clean = [
        c for c in polished
        if not display_law_violations(c, naming_vocab, pf_display=pf_display or None)
    ]
    if clean:
        return clean[0], clean
    return polished[0], polished


def _is_lattice_born(uf: Any) -> bool:
    """A journey already produced/partitioned by the lattice — the action axis
    only ever operates on ORGANIC (6.7d-abstracted) journeys."""
    return (
        str(getattr(uf, "id", "") or "").startswith(_LATTICE_ID_PREFIX)
        or str(getattr(uf, "domain", "") or "").startswith("lattice:")
    )


def _resource_is_action_domain(
    uf: Any, naming_vocab: Mapping[str, Any],
) -> bool:
    """True when the journey's RESOURCE word is itself one of the engine's
    own flow-verb-class members (``auth``/``callback``/``sync``/``login``…) —
    the "resource" is an ACTION DOMAIN, not a countable entity, so a CRUD
    partition is meaningless grain-noise ("Create auth" from POST /logout).
    Reuses the naming vocab's existing semantic table — no new dictionary."""
    classes: Mapping[str, Any] = naming_vocab.get("flow_verb_classes") or {}
    verb_words = {
        _norm_token(str(w))
        for verbs in classes.values() for w in (verbs or ())
    }
    verb_words.discard("")
    for tok in re.split(r"[^A-Za-z0-9]+", str(getattr(uf, "resource", "") or "")):
        if _norm_token(tok) in verb_words:
            return True
    return False


# ── Stage runner ────────────────────────────────────────────────────────


def run_journey_lattice(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
    routes_index: Iterable[Mapping[str, Any]] | None = None,
    *,
    interior_evidence: Mapping[str, Any] | None = None,
    labeler: Callable[[list[Any]], dict[str, Any]] | None = None,
    verifier: Callable[[list[dict[str, Any]]], dict[str, bool]] | None = None,
) -> dict[str, Any]:
    """Apply the journey lattice IN PLACE; return telemetry.

    ``labeler`` / ``verifier`` are the Wave-3 persona callables
    (keyed scans; ``None`` keyless → deterministic templates, splits
    apply unreviewed — the engine is deterministic by construction).
    """
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.naming_contract import (
        _flow_verb_verdict,
        display_law_violations,
        load_naming_vocab,
    )
    from faultline.pipeline_v2.spine_anchors import load_spine_vocab

    tele: dict[str, Any] = {
        "enabled": True,
        "applied": False,
        "pfs_scanned": 0,
        "eligible_ufs": 0,
        "subset_merged": 0,
        "subset_merged_pairs": [],
        "catchalls_detected": 0,
        "catchalls_split": 0,
        "journeys_created": 0,
        "members_moved": 0,
        "parents_dissolved": 0,
        "parents_kept_residual": 0,
        "verifier_rejects": 0,
        "conservation_reverts": 0,
        "clusters_qualified": 0,
        "samples": [],
    }
    if not user_flows or not product_features:
        return tele

    naming_vocab = load_naming_vocab()
    spine_vocab = load_spine_vocab()
    version_re = re.compile(
        str(spine_vocab.get("version_segment_pattern") or r"^v\d+$"))
    tenant_scope = frozenset(
        str(s).lower() for s in (spine_vocab.get("tenant_scope_segments") or ())
    )

    # Flow lookup + per-flow caches (uuid first, name fallback).
    flow_by_mid: dict[str, Any] = {}
    for d in features:
        for fl in getattr(d, "flows", None) or []:
            for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
                if key and str(key) not in flow_by_mid:
                    flow_by_mid[str(key)] = fl

    # Entry-ownership map (conservation exclusions: developer layer,
    # non-facet, PF-stamped). A cluster with NO owned member entry has
    # no attachment evidence to stand alone as a journey — its files are
    # lane/unowned mass (papermark pages/api/teams/*, Soc0
    # backend/services/*), and minting it just exposes the unowned
    # entries as majority-foreign rows at 1-member grain (keyed A/B
    # round-2 finding: every new I16 row was exactly this class). Those
    # members FOLD BACK to their parent — status quo ante — until the
    # excavation arc (W4.3) gives the files an owner; then the same
    # cluster keys mint the journeys properly on the next scan.
    entry_owner: dict[str, str] = {}
    for d in features:
        if str(getattr(d, "layer", "developer") or "developer") != "developer":
            continue
        if getattr(d, "role", None) == "facet":
            continue
        pfid = getattr(d, "product_feature_id", None)
        if not pfid:
            continue
        for p in getattr(d, "paths", None) or []:
            entry_owner.setdefault(str(p), str(pfid))

    def _has_owned_entry(mids: list[str]) -> bool:
        for m in mids:
            fl = flow_by_mid.get(m)
            if fl is None:
                continue
            e = _entry_file_of(fl)
            if e and e in entry_owner:
                return True
        return False

    patterns_by_file: dict[str, list[str]] = {}
    for r in routes_index or ():
        f = str(r.get("file") or "")
        p = str(r.get("pattern") or "")
        if f and p and p not in patterns_by_file.setdefault(f, []):
            patterns_by_file[f].append(p)

    interior_pages: Mapping[str, Mapping[str, Any]] = (
        (interior_evidence or {}).get("pages") or {}
    )
    pf_by_key: dict[str, Any] = {}
    pf_key_by_display: dict[str, str] = {}
    for pf in product_features:
        k = _pf_key_of(pf)
        if not k:
            continue
        pf_by_key.setdefault(k, pf)
        pf_key_by_display.setdefault(_pf_display_of(pf).strip().lower(), k)

    # Eligibility: interactive, non-synthesized, flow-ful, real-PF-bound.
    eligible: list[Any] = []
    for uf in user_flows:
        if str(getattr(uf, "category", "") or "interactive") != "interactive":
            continue
        if getattr(uf, "synthesized", False):
            continue
        if not (getattr(uf, "member_flow_ids", None) or []):
            continue
        pfid = getattr(uf, "product_feature_id", None)
        if not pfid or str(pfid) not in pf_by_key:
            continue
        eligible.append(uf)
    tele["eligible_ufs"] = len(eligible)
    eligible_ids = {str(getattr(u, "id", "") or "") for u in eligible}

    # ── Phase 1 — exact subset-duplicate merge (the hunts case) ────
    before_n = len(user_flows)
    _merge_subset_duplicates(user_flows, eligible_ids, tele)
    if len(user_flows) != before_n:
        tele["applied"] = True
        live_ids = {str(getattr(u, "id", "") or "") for u in user_flows}
        eligible = [
            u for u in eligible
            if str(getattr(u, "id", "") or "") in live_ids
        ]

    # ── Phase 2 — per-PF evidence clustering + split plans ─────────
    by_pf: dict[str, list[Any]] = {}
    for uf in eligible:
        by_pf.setdefault(str(uf.product_feature_id), []).append(uf)

    verb_cache: dict[str, str] = {}

    def _verb_of(mid: str) -> str:
        v = verb_cache.get(mid)
        if v is None:
            fl = flow_by_mid.get(mid)
            name = str(getattr(fl, "name", "") or mid) if fl is not None else mid
            v = _flow_verb_verdict([name], naming_vocab)
            verb_cache[mid] = v
        return v

    plans: list[dict[str, Any]] = []
    for pf_key in sorted(by_pf):
        pf = pf_by_key[pf_key]
        pf_display = _pf_display_of(pf)
        root_toks = _pf_root_tokens(pf)
        group = sorted(by_pf[pf_key], key=lambda u: str(u.id))
        tele["pfs_scanned"] += 1

        # Pooled member evidence (a flow appearing in two journeys is
        # attributed to its FIRST owner by id order — determinism).
        seen_mids: set[str] = set()
        pooled: list[tuple[Any, str]] = []  # (uf, mid)
        for uf in group:
            for mid in uf.member_flow_ids or []:
                if mid in seen_mids:
                    continue
                seen_mids.add(mid)
                pooled.append((uf, mid))

        clusters: dict[str, _Cluster] = {}
        residual_mids: dict[str, list[str]] = {str(u.id): [] for u in group}
        owner_of = {mid: uf for uf, mid in pooled}

        def _evidence(mid: str) -> tuple[str, str, str] | None:
            fl = flow_by_mid.get(mid)
            if fl is None:
                return None
            entry = _entry_file_of(fl)
            hit = _route_family(
                entry, patterns_by_file, root_toks, spine_vocab, version_re)
            if hit:
                return ("route", *hit)
            hit = _section_family(
                entry, pf_key, interior_pages, pf_key_by_display)
            if hit:
                return ("section", *hit)
            hit = _dir_family(entry, root_toks, tenant_scope)
            if hit:
                return ("dir", *hit)
            return None

        # Segments with DEEPER continuations anywhere in this PF's pool —
        # an object family has substructure (/notes/add); an action leaf
        # never does (/billing/pause). Feeds the action-key guard below.
        segs_with_children: set[str] = set()
        for uf, mid in pooled:
            fl = flow_by_mid.get(mid)
            entry = _entry_file_of(fl) if fl is not None else None
            for pattern in patterns_by_file.get(entry or "", ()):
                msegs = [
                    _norm_token(sg) for sg in str(pattern).split("/")
                    if sg and not re.match(r"^[\[\($:{<*]", sg)
                ]
                msegs = [sg for sg in msegs if sg]
                for sg in msegs[:-1]:
                    segs_with_children.add(sg)

        for uf, mid in pooled:
            ev = _evidence(mid)
            if ev is None:
                residual_mids[str(uf.id)].append(mid)
                continue
            axis, key, phrase = ev
            cl = clusters.get(key)
            if cl is None:
                cl = clusters[key] = _Cluster(key=key, axis=axis, phrase=phrase)
            elif _AXIS_RANK.get(axis, 9) < _AXIS_RANK.get(cl.axis, 9):
                cl.axis, cl.phrase = axis, phrase
            cl.mids.append(mid)
            fl = flow_by_mid.get(mid)
            cl.loc += _flow_span_loc(fl) if fl is not None else 0
            cl.verbs.append(_verb_of(mid))

        # Key-token-subset merge across axes ("ai" joins "ai-copilot";
        # a route family and its authored section label are ONE object —
        # the actor+intent+outcome guard across evidence sources). The
        # shorter (root) key absorbs; canonical id base = root key.
        keys_by_size = sorted(
            clusters, key=lambda k: (len(_key_tokens(k)), k))
        merged_into: dict[str, str] = {}
        for i, small in enumerate(keys_by_size):
            if small in merged_into:
                continue
            small_toks = _key_tokens(small)
            for big in keys_by_size[i + 1:]:
                if big in merged_into:
                    continue
                if small_toks and small_toks <= _key_tokens(big):
                    root, absorbed = small, big
                    merged_into[absorbed] = root
                    a, b = clusters[root], clusters[absorbed]
                    a.mids.extend(b.mids)
                    a.verbs.extend(b.verbs)
                    a.loc += b.loc
                    if _AXIS_RANK.get(b.axis, 9) < _AXIS_RANK.get(a.axis, 9):
                        a.axis, a.phrase = b.axis, b.phrase
        for absorbed in merged_into:
            clusters.pop(absorbed, None)

        # Core detection + the candidate bar.
        for key in sorted(clusters):
            cl = clusters[key]
            toks = _key_tokens(key)
            if toks and toks <= root_toks:
                cl.is_core = True
                continue
            # ACTION-KEY guard (keyed round-3 forensics, the billing
            # shards: /billing/pause + /billing/unpause + /billing/
            # upgrade shredded a clean 10-flow journey into 'Manage
            # pause'/'Manage unpause'/'Manage upgrade'/'Manage manage').
            # A single-token key sitting in VERB position of its member
            # flow names is the flows' ACTION, not an object family —
            # same actor+intent+outcome as the parent core. Mechanism,
            # not vocabulary: the flows themselves testify.
            if len(toks) == 1:
                tok = next(iter(toks))
                action_votes = 0
                for m in cl.mids:
                    fl = flow_by_mid.get(m)
                    name = str(getattr(fl, "name", "") or "") if fl else ""
                    # (a) verb position: the flow's own name leads with
                    # the key token ("pause-subscription-flow" / pause).
                    if _norm_token(re.split(
                        r"[^a-z0-9]+", name.lower(), 1,
                    )[0]) == tok:
                        action_votes += 1
                        continue
                    # (b) terminal leaf under the capability root: the
                    # key is the LAST meaningful segment of the member's
                    # route pattern, a root word precedes it, the key
                    # has NO deeper continuation anywhere in this PF's
                    # pool (an object family has substructure —
                    # /notes/add; /billing/pause never does), and the
                    # cluster is small (<= 3 — action leaves are thin;
                    # a 12-flow terminal family is a real sub-surface).
                    # A terminal key with no root prefix (papermark
                    # /view/[linkId]) is a real surface and keeps.
                    if (len(cl.mids) > 3
                            or tok in segs_with_children):
                        continue
                    entry = _entry_file_of(fl) if fl is not None else None
                    for pattern in patterns_by_file.get(entry or "", ()):
                        segs = [
                            _norm_token(sg)
                            for sg in str(pattern).split("/")
                            if sg and not re.match(r"^[\[\($:{<*]", sg)
                        ]
                        segs = [sg for sg in segs if sg]
                        if (segs and segs[-1] == tok
                                and any(sg in root_toks
                                        for sg in segs[:-1])):
                            action_votes += 1
                            break
                if action_votes * 2 >= len(cl.mids):
                    cl.is_core = True
                    tele["clusters_action_folded"] = (
                        tele.get("clusters_action_folded", 0) + 1)
        qualifying: dict[str, _Cluster] = {}
        for key in sorted(clusters):
            cl = clusters[key]
            if cl.is_core:
                continue
            if len(cl.mids) >= 2 or cl.loc >= _MIN_CLUSTER_LOC:
                qualifying[key] = cl
                tele["clusters_qualified"] += 1
            else:
                cl.is_core = True  # sub-bar → folds to core/residual
        # Fold core + sub-bar members back to their owning journeys.
        for key in sorted(clusters):
            cl = clusters[key]
            if not cl.is_core:
                continue
            for mid in cl.mids:
                residual_mids[str(owner_of[mid].id)].append(mid)

        # Catch-all detection per journey.
        split_ufs: list[Any] = []
        for uf in group:
            uid = str(uf.id)
            member_set = set(uf.member_flow_ids or [])
            covered = sum(
                1 for cl in qualifying.values()
                if member_set & set(cl.mids)
            )
            buckets = covered + (1 if residual_mids.get(uid) else 0)
            mintable = covered
            if buckets >= _CATCHALL_MIN_CLUSTERS and mintable >= _MIN_MINTABLE:
                split_ufs.append(uf)
                tele["catchalls_detected"] += 1
        if not split_ufs:
            continue
        split_ids = {str(u.id) for u in split_ufs}

        # Materialize children from SPLIT journeys' members only; the
        # bar re-applies to the materialized subset (no shrapnel mints).
        children: list[dict[str, Any]] = []
        for key in sorted(qualifying):
            cl = qualifying[key]
            mids = [m for m in cl.mids if str(owner_of[m].id) in split_ids]
            if not mids:
                continue
            loc = sum(
                _flow_span_loc(flow_by_mid[m]) for m in mids
                if m in flow_by_mid
            )
            if len(mids) < 2 and loc < _MIN_CLUSTER_LOC:
                for m in mids:
                    residual_mids[str(owner_of[m].id)].append(m)
                continue
            if not _has_owned_entry(mids):
                # No attachment evidence — fold back (see entry_owner).
                tele["clusters_unowned_folded"] = (
                    tele.get("clusters_unowned_folded", 0) + 1)
                for m in mids:
                    residual_mids[str(owner_of[m].id)].append(m)
                continue
            verdict = _dominant_verb(
                [_verb_of(m) for m in mids])
            # B78 Seg H — member evidence for the ratio gate (built only
            # when the flag is armed; None ⇒ byte-identical).
            _seg_h_ev: list[tuple[str, str]] | None = None
            if _dominant_naming_on():
                _seg_h_ev = [
                    (str(getattr(flow_by_mid[m], "display_name", None)
                         or getattr(flow_by_mid[m], "name", "") or ""),
                     str(_entry_file_of(flow_by_mid[m]) or ""))
                    for m in mids if m in flow_by_mid
                ]
            name, candidates = _deterministic_name(
                verdict, cl.phrase, pf_display, naming_vocab,
                member_evidence=_seg_h_ev)
            children.append({
                "id": _child_id(pf_key, key),
                "key": key,
                "axis": cl.axis,
                "phrase": cl.phrase,
                "name": name,
                "candidates": candidates,
                "verdict": verdict,
                "mids": mids,
                "loc": loc,
            })
        if len(children) < _MIN_MINTABLE:
            continue  # partition collapsed at materialization — keep as-is

        # Non-cluster members of split journeys stay with their parent.
        residual_by_uf: dict[str, list[str]] = {}
        claimed = {m for ch in children for m in ch["mids"]}
        for uf in split_ufs:
            uid = str(uf.id)
            residual = [
                m for m in (uf.member_flow_ids or []) if m not in claimed
            ]
            residual_by_uf[uid] = residual
        plans.append({
            "pf_key": pf_key,
            "pf_display": pf_display,
            "split_ufs": split_ufs,
            "children": children,
            "residual_by_uf": residual_by_uf,
            "owner_by_mid": {
                m: str(u.id) for u, m in pooled
                if str(u.id) in split_ids
            },
        })

    # ── Phase 2b — ACTION-grain partition (W5.2 / B6, V2-flag-guarded) ──
    # The object axis (route/section/dir) cannot separate a single-router REST
    # surface: `_route_family` keys off the flow's ENTRY FILE and returns the
    # first meaningful segment across ALL of that file's patterns, so 29
    # endpoints in one `routers/cases.py` collapse to ONE route family and the
    # journey never reaches the catch-all bar. The action axis partitions such
    # a journey by USER INTENT — the leading verb of each member flow name —
    # into browse/view/create/update/delete/act families, each independently
    # clearing the engine's journey bar (>= 2 members OR >= 150 span LOC). A
    # split fires only when >= min_action_families (3) families qualify: action
    # families are always partially present (reads + writes), so 2 is the noise
    # floor and >= 3 is the genuine full-CRUD resource-management signature.
    # Runs ONLY on organic journeys the object axis left unsplit; emits the same
    # plan shape, so Phase 3/4/5 (verifier / apply / labeler) are unchanged.
    # B25 — the Phase-2b detection closure escapes the V2 block through this
    # handle so Phase 3b (verifier-revert slot release) can re-run it for a
    # pf whose plan fully reverted; stays None when the V2 axis is off.
    action_plan_builder: (
        Callable[[str, set[str]], dict[str, Any] | None] | None) = None
    if journey_lattice_v2_enabled():
        object_split_ids = {
            str(getattr(u, "id", "") or "")
            for plan in plans for u in plan["split_ufs"]
        }
        action_vocab = load_action_families()
        min_action_families = max(
            int(action_vocab.get("min_action_families", 3) or 3), _MIN_MINTABLE
        )
        # Lane files (validator I15 denominator exclusion): at 6.88 time the
        # platform-infrastructure lane residents are exactly the developer
        # features WITHOUT a product attachment (they move to the
        # platform_infrastructure[] array at emission).
        lane_files: set[str] = set()
        for d in features:
            lay = str(getattr(d, "layer", "developer") or "developer")
            if lay != "developer":
                continue
            if getattr(d, "product_feature_id", None):
                continue
            for p in (getattr(d, "paths", None) or []):
                lane_files.add(str(p))
        # PF member scope (validator I15 ruler): pf.paths ∪ member dev paths.
        pf_scope_map: dict[str, set[str]] = {}
        for pf in product_features:
            k = _pf_key_of(pf)
            if k:
                pf_scope_map.setdefault(k, set()).update(
                    str(p) for p in (getattr(pf, "paths", None) or []))
        for d in features:
            dpf = getattr(d, "product_feature_id", None)
            if dpf and str(dpf) in pf_scope_map:
                pf_scope_map[str(dpf)].update(
                    str(p) for p in (getattr(d, "paths", None) or []))

        def _mids_files(mids: list[str]) -> set[str]:
            """Union of the validator's I15 flow files over a member set —
            the ``_lane_attach_share`` numerator source (shared by the parent
            attach and the B7 per-child attach)."""
            fs: set[str] = set()
            for m in mids:
                fl = flow_by_mid.get(m)
                if fl is not None:
                    fs |= _flow_files_of(fl)
            return fs

        def _action_plan_for_pf(
            pf_key: str, excluded_uf_ids: set[str],
        ) -> dict[str, Any] | None:
            """Phase-2b action detection for ONE pf — the exact inline loop
            body, parameterized on the claimed-UF exclusion set so the B25
            slot release (Phase 3b) can re-run it after the Draft Verifier
            fully reverts a pf's plan. Returns the plan (caller appends) or
            ``None``; mutates the shared telemetry exactly as the inline
            form did."""
            pf = pf_by_key[pf_key]
            pf_display = _pf_display_of(pf)
            # Action-split AT MOST ONE journey per PF — the largest organic
            # journey the object axis left unsplit (the resource's catch-all).
            # Splitting sibling journeys too would collide on the content-
            # derived child ids (same pf + same action family) and over-
            # fragment the capability. Ties broken by smallest id (stable).
            pf_candidates = [
                u for u in by_pf[pf_key]
                if str(getattr(u, "id", "") or "") not in excluded_uf_ids
                and not _is_lattice_born(u)
                and not _resource_is_action_domain(u, naming_vocab)
            ]
            if not pf_candidates:
                return None
            a_uf = sorted(
                pf_candidates,
                key=lambda u: (-len(getattr(u, "member_flow_ids", None) or []),
                               str(getattr(u, "id", "") or "")),
            )[0]
            a_uid = str(getattr(a_uf, "id", "") or "")
            a_member_ids = list(getattr(a_uf, "member_flow_ids", None) or [])
            # I15 attach eligibility (validator ruler) on the PRE-SPLIT parent
            # — its non-lane flow files inside the PF scope. A sub-floor parent
            # is misattached by the board's own measure; splitting re-measures
            # the same weak mass N times and multiplies I15 rows (papermark
            # 'workflows': parent 0.327, every child <= 0.288). B6 skipped on
            # the parent share alone. B7 (default) DEFERS the skip to the
            # max-child gate below: a low parent share is often only an
            # averaging artifact — a few lane-diluted / stray members dilute an
            # otherwise-rich catch-all, and the 6.88-time lane approximation
            # sinks the mean below the floor on the live board (Soc0 'Manage
            # cases': parent skipped live yet its 'View cases' child is 1.0).
            pf_scope = pf_scope_map.get(pf_key, set())
            parent_share = _lane_attach_share(
                _mids_files(a_member_ids), pf_scope, lane_files)
            parent_below = (
                parent_share is not None and parent_share < _I15_ATTACH_FLOOR)
            if parent_below and not journey_lattice_b7_enabled():
                # B6 behaviour (flag off): the parent share alone gates.
                tele["action_parent_attach_skipped"] = (
                    tele.get("action_parent_attach_skipped", 0) + 1)
                return None
            # Cluster members by action family (head verb).
            a_fam_mids: dict[str, list[str]] = {}
            for mid in a_member_ids:
                fl = flow_by_mid.get(mid)
                fam = _action_family(
                    str(getattr(fl, "name", "") or "")
                    if fl is not None else "", action_vocab)
                if fam is not None:
                    a_fam_mids.setdefault(fam, []).append(mid)
            # Qualifying families: >= 2 members STRICT — no LOC arm. An
            # object family with one 150-LOC page is real journey MASS, but
            # ONE flow's head verb is a single datapoint, not a corroborated
            # intent family; a 1-member "Update monitors" child is noise.
            a_qual: dict[str, list[str]] = {}
            for fam, fam_members in a_fam_mids.items():
                if len(fam_members) >= 2:
                    a_qual[fam] = fam_members
            if len(a_qual) < min_action_families:
                return None
            # Materialize one child per qualifying family (owned-entry
            # gated — an unowned family folds back to residual, exactly
            # like the object axis).
            a_children: list[dict[str, Any]] = []
            a_claimed: set[str] = set()
            res_slug = _norm_key(pf_display) or pf_key
            a_resource = (
                str(getattr(a_uf, "resource", "") or "") or pf_display)
            for fam in _ACTION_FAMILY_ORDER:
                fam_members = a_qual.get(fam) or []
                if not fam_members:
                    continue
                # Majority-owned entry gate — the I16 ruler applied at MINT
                # time: a family whose known-owner entries are majority-
                # foreign (> 0.5) would be born a misattached journey the
                # coarse parent kept under the radar; it folds to residual
                # (status quo ante). chk == 0 (no owned entry at all) folds
                # too — no attachment evidence to stand alone (the object
                # axis' _has_owned_entry contract, strengthened to the
                # validator's own threshold for the finer action grain).
                a_chk = a_mis = 0
                for m in fam_members:
                    fl = flow_by_mid.get(m)
                    e = _entry_file_of(fl) if fl is not None else None
                    own = entry_owner.get(e or "")
                    if own:
                        a_chk += 1
                        if own != pf_key:
                            a_mis += 1
                if a_chk == 0 or a_mis * 2 > a_chk:
                    tele["clusters_unowned_folded"] = (
                        tele.get("clusters_unowned_folded", 0) + 1)
                    continue
                fam_loc = sum(
                    _flow_span_loc(flow_by_mid[m]) for m in fam_members
                    if m in flow_by_mid)
                a_name, a_name_candidates = _action_child_name(
                    fam, pf_display, naming_vocab)
                a_key = f"{res_slug}-{fam}"
                a_children.append({
                    "id": _child_id(pf_key, f"action:{a_key}"),
                    "key": a_key,
                    "resource": a_resource or a_key.replace("-", " "),
                    "axis": "action",
                    "phrase": pf_display,
                    "name": a_name,
                    "candidates": a_name_candidates,
                    "verdict": _ACTION_VERDICT.get(fam, "manage"),
                    "mids": list(fam_members),
                    "loc": fam_loc,
                })
                a_claimed.update(fam_members)
            if len(a_children) < min_action_families:
                return None
            # B7 max-child I15 eligibility (reached only with the flag ON and a
            # sub-floor parent): skip ONLY when the capability is misattached at
            # EVERY grain — the parent AND every minted child sit below the
            # floor (papermark 'workflows'). When even one action-child clears
            # the floor the capability owns a genuinely-attached sub-journey
            # (Soc0 'View cases': 9 GET-by-id flows, share 1.0) that the diluted
            # parent mean hides and that MUST be surfaced. Gating on the max
            # child (not the mean) is robust to the 6.88-time lane noise and is
            # a monotone relaxation of B6 (never un-splits a B6 split). A
            # lane-only child (share None → the validator skips it) counts as
            # clearing — it is never itself an I15 row.
            if parent_below:
                child_clears = False
                for ch in a_children:
                    cs = _lane_attach_share(
                        _mids_files(ch["mids"]), pf_scope, lane_files)
                    if cs is None or cs >= _I15_ATTACH_FLOOR:
                        child_clears = True
                        break
                if not child_clears:
                    tele["action_parent_attach_skipped"] = (
                        tele.get("action_parent_attach_skipped", 0) + 1)
                    return None
            a_residual = [m for m in a_member_ids if m not in a_claimed]
            tele["catchalls_detected"] += 1
            tele["action_catchalls_detected"] = (
                tele.get("action_catchalls_detected", 0) + 1)
            return {
                "pf_key": pf_key,
                "pf_display": pf_display,
                "split_ufs": [a_uf],
                "children": a_children,
                "residual_by_uf": {a_uid: a_residual},
                "owner_by_mid": {m: a_uid for m in a_member_ids},
            }

        action_plan_builder = _action_plan_for_pf
        for pf_key in sorted(by_pf):
            a_plan = _action_plan_for_pf(pf_key, object_split_ids)
            if a_plan is not None:
                plans.append(a_plan)

    # ── Phase 3 — Draft Verifier over split CHILDREN (keyed seam) ──
    # One item per proposed CHILD journey (per-plan verdicts let one
    # weak 1-member child poison a whole honest partition — keyed
    # round-4 exhibit: the Soc0 investigations plan died to two thin
    # riders). An explicitly rejected child folds its members back to
    # the parent; a plan whose surviving children drop below the mint
    # bar reverts entirely — the catch-all survives byte-identically
    # (conservative fallback, never lose).
    if plans and verifier is not None:
        def _verifier_items(
            plan_list: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for plan in plan_list:
                parents_line = " + ".join(
                    f"'{getattr(u, 'name', '')}' ({len(u.member_flow_ids or [])} flows)"
                    for u in plan["split_ufs"]
                )
                for ch in plan["children"]:
                    items.append({
                        "id": ch["id"],
                        "kind": "lattice_split",
                        "draft": ch["name"],
                        "pf_display": plan["pf_display"],
                        "context": {
                            "split_from": parents_line,
                            "capability": plan["pf_display"],
                            "evidence": f"{ch['axis']}:{ch['key']}",
                            "members": len(ch["mids"]),
                            "member_flows_sample": [
                                str(getattr(flow_by_mid.get(m), "name", m) or m)
                                for m in ch["mids"][:5]
                            ],
                        },
                    })
            return items

        def _screen_plans(
            plan_list: list[dict[str, Any]],
            verdicts: Mapping[str, Any],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            """Fold explicit child rejects; split plans into (kept,
            fully-reverted). Exactly the pre-B25 verdict fold — reverted
            plans are merely RECORDED for the Phase-3b slot release."""
            kept: list[dict[str, Any]] = []
            reverted: list[dict[str, Any]] = []
            for plan in plan_list:
                surviving = []
                for ch in plan["children"]:
                    if verdicts.get(ch["id"]) is False:
                        tele["verifier_rejects"] += 1
                        # Rejected child's members stay with the parent.
                        for m in ch["mids"]:
                            uid = plan["owner_by_mid"].get(m)
                            if uid is not None:
                                plan["residual_by_uf"].setdefault(
                                    uid, [],
                                ).append(m)
                    else:
                        surviving.append(ch)
                if len(surviving) >= _MIN_MINTABLE:
                    plan["children"] = surviving
                    kept.append(plan)
                elif surviving or plan["children"]:
                    tele["plans_reverted_verifier"] = (
                        tele.get("plans_reverted_verifier", 0) + 1)
                    reverted.append(plan)
            return kept, reverted

        # B25 bookkeeping — pfs that already spent their one action shot in
        # pass 1 (kept OR reverted): a reverted ACTION plan is final and is
        # never re-asked (the no-re-entry hard rule).
        action_pfs_pass1 = {
            str(plan["pf_key"]) for plan in plans
            if any(ch.get("axis") == "action" for ch in plan["children"])
        }
        try:
            verdicts = verifier(_verifier_items(plans)) or {}
        except Exception as exc:  # noqa: BLE001 — persona never breaks a scan
            verdicts = {}
            tele["verifier_error"] = str(exc)
        plans, reverted_plans = _screen_plans(plans, verdicts)

        # ── Phase 3b — B25 verifier-revert slot release (keyed seam) ──
        # A fully-reverted plan leaves its catch-all byte-identical on the
        # board, yet its parent UF already consumed the pf's one split slot
        # via the PRE-verifier exclusion set — so the action axis never got
        # its turn (Soc0 'Manage cases end-to-end': the object axis claimed
        # the catch-all with a doomed single-router route shard, the
        # verifier honestly killed it, and the healthy action plan was
        # never built). Release the slot ONCE: re-run the Phase-2b action
        # detection for each such pf (excluding UFs claimed by KEPT plans),
        # screen the recovered plans in ONE additional verifier batch, and
        # apply survivors through the unchanged Phase-4/5 path. Bounded by
        # construction: at most one release per pf per scan, action plans
        # never re-enter, and there is no second recovery round.
        if (reverted_plans and action_plan_builder is not None
                and journey_lattice_b25_enabled()):
            kept_uf_ids = {
                str(getattr(u, "id", "") or "")
                for plan in plans for u in plan["split_ufs"]
            }
            released_pfs: set[str] = set()
            recovered: list[dict[str, Any]] = []
            for plan in reverted_plans:
                r_pf = str(plan["pf_key"])
                if r_pf in released_pfs or r_pf in action_pfs_pass1:
                    continue  # hard cap: at most ONE release per pf per scan
                released_pfs.add(r_pf)
                r_plan = action_plan_builder(r_pf, kept_uf_ids)
                if r_plan is not None:
                    recovered.append(r_plan)
            if released_pfs:
                tele["slots_released"] = len(released_pfs)
                tele["slot_release_pfs"] = sorted(released_pfs)
            if recovered:
                try:
                    r_verdicts = verifier(_verifier_items(recovered)) or {}
                except Exception as exc:  # noqa: BLE001
                    r_verdicts = {}
                    tele["verifier_error"] = str(exc)
                r_kept, _r_reverted = _screen_plans(recovered, r_verdicts)
                tele["slot_release_recovered"] = len(r_kept)
                plans.extend(r_kept)

    # ── Phase 4 — apply plans (conservation-checked) ───────────────
    applied_children: list[tuple[dict[str, Any], Any]] = []
    for plan in plans:
        split_ufs = plan["split_ufs"]
        before_union: set[str] = set()
        for uf in split_ufs:
            before_union.update(uf.member_flow_ids or [])
        after_union: set[str] = set()
        for ch in plan["children"]:
            after_union.update(ch["mids"])
        for uid, residual in plan["residual_by_uf"].items():
            after_union.update(residual)
        if before_union != after_union:
            tele["conservation_reverts"] += 1
            logger.warning(
                "journey_lattice: conservation mismatch on %s — plan reverted",
                plan["pf_key"],
            )
            continue  # defensive — never lose a member

        tele["applied"] = True
        tele["catchalls_split"] += len(split_ufs)
        sample = {
            "pf": plan["pf_key"],
            "parents": [str(getattr(u, "name", "")) for u in split_ufs],
            "children": [],
        }
        for ch in plan["children"]:
            routes: list[str] = []
            for m in ch["mids"]:
                fl = flow_by_mid.get(m)
                entry = _entry_file_of(fl) if fl is not None else None
                for p in patterns_by_file.get(entry or "", ()):
                    if p not in routes:
                        routes.append(p)
            child = UserFlow(
                id=ch["id"],
                name=ch["name"],
                resource=ch.get("resource") or ch["key"].replace("-", " "),
                domain=f"lattice:{ch['axis']}:{ch['key']}",
                product_feature_id=plan["pf_key"],
                intent=_intent_for_verb(ch["verdict"]),
                member_flow_ids=list(ch["mids"]),
                member_count=len(ch["mids"]),
                routes=sorted(routes),
                refined=False,
                name_confidence=(
                    "high" if ch["axis"] in {"section", "route"} else "low"
                ),
            )
            user_flows.append(child)
            applied_children.append((ch, child))
            tele["journeys_created"] += 1
            tele["members_moved"] += len(ch["mids"])
            if len(sample["children"]) < 8:
                sample["children"].append({
                    "id": ch["id"], "name": ch["name"],
                    "axis": ch["axis"], "key": ch["key"],
                    "members": len(ch["mids"]),
                })
        drop_parents: set[str] = set()
        for uf in split_ufs:
            uid = str(uf.id)
            residual = plan["residual_by_uf"].get(uid, [])
            if residual:
                uf.member_flow_ids = residual
                uf.member_count = len(residual)
                routes = []
                for m in residual:
                    fl = flow_by_mid.get(m)
                    entry = _entry_file_of(fl) if fl is not None else None
                    for p in patterns_by_file.get(entry or "", ()):
                        if p not in routes:
                            routes.append(p)
                uf.routes = sorted(routes)
                tele["parents_kept_residual"] += 1
            else:
                drop_parents.add(uid)
                tele["parents_dissolved"] += 1
        if drop_parents:
            user_flows[:] = [
                u for u in user_flows
                if str(getattr(u, "id", "") or "") not in drop_parents
            ]
        if len(tele["samples"]) < 12:
            tele["samples"].append(sample)

    # ── Phase 5 — PM Labeler over child names (keyed seam) ─────────
    if applied_children and labeler is not None:
        pending: list[_NameItem] = []
        for ch, child in applied_children:
            pf = pf_by_key.get(str(child.product_feature_id) or "")
            pf_display = _pf_display_of(pf) if pf is not None else ""
            member_names = [
                str(getattr(flow_by_mid.get(m), "name", m) or m)
                for m in (child.member_flow_ids or [])
            ][:8]
            pending.append(_NameItem(
                kind="uf",
                key=str(child.id),
                current=str(child.name),
                candidates=list(ch["candidates"]),
                context={
                    "pf_display": pf_display,
                    "evidence": f"{ch['axis']}:{ch['key']}",
                    "member_flows": member_names,
                },
                obj=child,
                pf_display=pf_display or None,
            ))
        try:
            lab_result = dict(labeler(pending) or {})
        except Exception as exc:  # noqa: BLE001 — persona never breaks a scan
            lab_result = {"error": str(exc)}
        choices = lab_result.pop("choices", None) or {}
        applied_picks = 0
        for item in pending:
            pick = choices.get(item.key)
            if not isinstance(pick, str) or not pick.strip():
                continue
            pick = " ".join(pick.split())
            if display_law_violations(
                pick, naming_vocab, pf_display=item.pf_display,
            ):
                continue  # defense in depth — laws re-checked at apply
            if pick != str(getattr(item.obj, "name", "") or ""):
                item.obj.name = pick
                applied_picks += 1
        lab_result["applied"] = applied_picks
        tele["labeler"] = lab_result

    return tele


# ── Post-conservation dedup (wiring calls AFTER apply_uf_conservation) ──


def dedup_lattice_journeys(user_flows: list[Any]) -> dict[str, Any]:
    """Merge lattice-born journeys that landed on the SAME capability
    with the SAME evidence object after the conservation resettle
    (two capabilities' catch-alls each shed a ``domains`` cluster;
    conservation re-homes both children to the domains capability —
    they are ONE journey). Also folds a lattice child into an existing
    journey of the same capability whose ``resource`` matches its key.
    Deterministic; members union-preserved (conservation of flows)."""
    tele = {"merged": 0, "into_existing": 0}
    by_pf: dict[str, list[Any]] = {}
    for uf in user_flows:
        pfid = getattr(uf, "product_feature_id", None)
        if pfid:
            by_pf.setdefault(str(pfid), []).append(uf)

    drop_ids: set[str] = set()
    for pfid in sorted(by_pf):
        group = by_pf[pfid]
        lattice = sorted(
            (u for u in group
             if str(getattr(u, "id", "") or "").startswith(_LATTICE_ID_PREFIX)),
            key=lambda u: str(u.id),
        )
        if not lattice:
            continue
        others = [
            u for u in group
            if not str(getattr(u, "id", "") or "").startswith(_LATTICE_ID_PREFIX)
        ]
        for child in lattice:
            if str(child.id) in drop_ids:
                continue
            key = str(getattr(child, "domain", "") or "").rsplit(":", 1)[-1]
            if not key:
                continue
            # (a) an existing journey whose resource matches the key.
            target = None
            for u in others:
                if _norm_key(str(getattr(u, "resource", "") or "")) == key:
                    target = u
                    break
            # (b) an earlier lattice sibling with the same key.
            if target is None:
                for u in lattice:
                    if u is child or str(u.id) in drop_ids:
                        continue
                    ukey = str(getattr(u, "domain", "") or "").rsplit(":", 1)[-1]
                    if ukey == key and str(u.id) < str(child.id):
                        target = u
                        break
            if target is None:
                continue
            merged = list(dict.fromkeys(
                (target.member_flow_ids or []) + (child.member_flow_ids or [])
            ))
            target.member_flow_ids = merged
            target.member_count = len(merged)
            routes = list(dict.fromkeys(
                (getattr(target, "routes", None) or [])
                + (getattr(child, "routes", None) or [])
            ))
            target.routes = sorted(routes)
            drop_ids.add(str(child.id))
            if str(getattr(target, "id", "") or "").startswith(_LATTICE_ID_PREFIX):
                tele["merged"] += 1
            else:
                tele["into_existing"] += 1
    if drop_ids:
        user_flows[:] = [
            u for u in user_flows
            if str(getattr(u, "id", "") or "") not in drop_ids
        ]
    return tele


def _lattice_thin_fold_enabled() -> bool:
    """Default ON; ``FAULTLINE_LATTICE_THIN_FOLD=0`` disables the W5.1
    post-thinning fold-back (restores the pre-fix child set)."""
    return os.environ.get(LATTICE_THIN_FOLD_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def fold_thin_lattice_children(
    user_flows: list[Any], flows: list[Any],
) -> dict[str, Any]:
    """W5.1 fix — fold degenerate lattice children back into a sibling.

    A lattice child born legitimately (``>= 2`` members OR a ``>= 150``-LOC
    single, per the creation bar) can be stripped BELOW that floor by the
    post-apply conservation resettle / dedup, ending with EXACTLY ONE member
    whose merged span-LOC is ``< _MIN_CLUSTER_LOC``. That is a shred — its
    lone member folds back into a sibling journey of the SAME product
    feature (the surviving split parent when present, else the fullest
    non-shred sibling).

    Conservation-safe: the member is UNION-merged into the host BEFORE the
    child is dropped (never lost). A child that is the ONLY journey on its PF
    (no eligible host) is LEFT in place — folding it would leave the PF
    journey-less (validator I8). The ``>= 150``-LOC single is the intended
    garbage-bucket rescue arm (w5lattice §E7) and is never folded.
    Deterministic (sorted iteration). Runs AFTER ``apply_uf_conservation`` +
    ``dedup_lattice_journeys``."""
    tele: dict[str, Any] = {"checked": 0, "folded": 0, "no_host": 0}
    if not _lattice_thin_fold_enabled():
        return tele
    flow_by_mid: dict[str, Any] = {}
    for fl in flows:
        mid = _flow_member_id(fl)
        if mid and mid not in flow_by_mid:
            flow_by_mid[mid] = fl

    by_pf: dict[str, list[Any]] = {}
    for uf in user_flows:
        pfid = getattr(uf, "product_feature_id", None)
        if pfid:
            by_pf.setdefault(str(pfid), []).append(uf)

    drop_ids: set[str] = set()
    for pfid in sorted(by_pf):
        group = by_pf[pfid]
        children = sorted(
            (u for u in group
             if str(getattr(u, "id", "") or "").startswith(_LATTICE_ID_PREFIX)),
            key=lambda u: str(u.id),
        )
        for child in children:
            if str(child.id) in drop_ids:
                continue
            mids = list(child.member_flow_ids or [])
            if len(mids) != 1:
                continue  # the 1-member shred is the only fold target
            span = (_flow_span_loc(flow_by_mid[mids[0]])
                    if mids[0] in flow_by_mid else 0)
            tele["checked"] += 1
            if span >= _MIN_CLUSTER_LOC:
                continue  # LOC-qualified single — intended rescue, keep
            # Host = a sibling on the same PF: not this child, not dropped,
            # and not itself a shred (>= 2 members). Prefer the surviving
            # split parent (non-lattice), then the fullest sibling, then the
            # lowest id — a total, deterministic order.
            host = None
            best_key: tuple[int, int, str] | None = None
            for u in group:
                if u is child or str(getattr(u, "id", "") or "") in drop_ids:
                    continue
                u_mids = u.member_flow_ids or []
                if len(u_mids) <= 1:
                    continue  # never fold a shred into another shred
                is_lattice = str(
                    getattr(u, "id", "") or "").startswith(_LATTICE_ID_PREFIX)
                key = (1 if is_lattice else 0, -len(u_mids),
                       str(getattr(u, "id", "") or ""))
                if best_key is None or key < best_key:
                    best_key = key
                    host = u
            if host is None:
                tele["no_host"] += 1
                continue  # sole journey on the PF (or all-shred) — leave it
            merged = list(dict.fromkeys((host.member_flow_ids or []) + mids))
            host.member_flow_ids = merged
            host.member_count = len(merged)
            routes = list(dict.fromkeys(
                (getattr(host, "routes", None) or [])
                + (getattr(child, "routes", None) or [])
            ))
            host.routes = sorted(routes)
            drop_ids.add(str(child.id))
            tele["folded"] += 1
    if drop_ids:
        user_flows[:] = [
            u for u in user_flows
            if str(getattr(u, "id", "") or "") not in drop_ids
        ]
    return tele
