"""Journey lattice (W5) — deterministic catch-all journey partition.

THE PROBLEM (operator case + A3 panels, 2026-07-07): the journey layer
systematically lands at jpf ≈ 1.0 — one journey per product feature —
because the 6.7d abstraction prior compresses toward "a journal per
capability". Soc0's `Investigations` (15.8K LOC) ships as ONE
"Create and manage investigations" (47 members); papermark's
"Build automated workflows" carries 51 members of which ~40 are the
whole `pages/api/teams/**` surface. A PM cannot use either row.

THE DOCTRINE: the partition is DETERMINISTIC — evidence clusters decide
membership; the LLM only NAMES (PM Labeler, selection-not-generation)
and REVIEWS (Draft Verifier). No LLM ever moves a member.

MECHANICS (post-6.7d, post-seed lattice pass):
  1. For each interactive, member-ful UF: cluster its member flows by
     an OBJECT key derived from evidence axes:
       (а) verb family      — ``flow_verb_classes`` vocab (naming stack);
       (б) interior section — W4 ``role="interior"`` node dirs;
       (в) route family     — param-stripped entry file dir (the
                              route_group_recall ``_group_dir`` rule);
       (г) entry-dir family — last non-structural path segment (file
                              stem when the dir is a generic container).
     The PRIMARY partition key is (object, sub-object|section): the
     object names WHAT the journey is about (``investigations``), the
     sub-object the distinct outcome surface (``notes``, ``lifecycle``,
     ``fork``, ``domains``, ``slack``). The verb family deliberately
     does NOT split on its own — browse+CRUD of one object is ONE
     actor+intent+outcome grain (the guard from the brief); verbs pick
     the display template instead.
  2. A cluster qualifies as a journey candidate with
     max(2, ceil(6% of members)) flows — a scale-invariant floor (the
     brief's single-flow LOC prong was dropped: flow spans routinely
     beat any absolute bound and it minted 1-member page-shrapnel, the
     exact class the A3 panels flagged). Non-qualifying clusters fold
     into the CORE cluster (the one carrying the UF's own resource,
     else the largest).
  3. Catch-all detection: a UF whose members span >= 3 qualifying
     clusters SPLITS into per-cluster journeys. Conservation: every
     member lands in exactly one cluster — nothing is lost, loc_flow
     channels re-truth automatically at Stage 6.97 (which derives them
     from final ``member_flow_ids``).
  4. Subset-duplicate merge: UF-A ⊂ UF-B (member sets, same PF) →
     A merges into B (the Soc0 hunts case). GARBAGE-BUCKET DISSOLVE:
     a UF with zero qualifying clusters, >= 3 distinct member objects
     and a resource matching none of them (Soc0 "View network
     security") re-homes each member into the sibling UF whose
     resource IS its object; members with no honest home stay.
  5. Names: candidates composed from cluster signals (dominant verb
     family template × object phrase). Keyed: PM Labeler picks per
     batch (selection); keyless: the deterministic template ships as
     is. W3 naming laws still apply downstream (run_naming_contract
     polices every UF after this pass). Draft Verifier reviews each
     SPLIT (keyed): reject → the original catch-all stays untouched —
     the conservative fallback NEVER loses members.
  6. New journeys carry content-derived ids (``UF-lat-<hash8>`` over
     pf|object|sub-object — stable across rescans) and the binding
     note ``synthesis_reason="lattice:<axis>"``; ``synthesized`` stays
     False (they are real journeys with members — the synthesized
     naming override must not template them onto the PF display).
     Existing UF ids survive wherever no split fires; a split UF keeps
     its id and name on the CORE cluster.

Kill-switch: ``FAULTLINE_JOURNEY_LATTICE=0`` (default ON).
Zones honored: no mint/lane/excavation contact; stage_6_55 read-only
(interior evidence is consumed from flow nodes, never recomputed).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

if TYPE_CHECKING:
    from faultline.models.types import Flow, UserFlow

_ENV = "FAULTLINE_JOURNEY_LATTICE"

#: Scale-invariant cluster floor: a cluster qualifies with
#: max(2, ceil(6% of the UF's members)) flows. The brief's single-flow
#: LOC prong was DROPPED after the wave4 dry-run: flow spans routinely
#: exceed any absolute bound, so it minted 1-member page-shrapnel
#: journeys — exactly the class the A3 panels flagged as noise
#: (rule-no-magic-tuning: a member-share ratio is scale-invariant,
#: an absolute LOC bar on spans is not).
_MIN_CLUSTER_FLOWS = 2
_CLUSTER_MEMBER_SHARE = 0.06
#: A UF is a catch-all when its members span at least this many
#: qualifying clusters (brief §2).
_CATCHALL_MIN_CLUSTERS = 3

_PARAM_SEG = re.compile(r"^(\[.*\]|\{.*\}|:.+|\(.*\))$")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")

#: Path segments that never name an object (generic containers) — kept
#: intentionally small and universal (mirrors the structural vocabulary
#: class used across the spine; extended per-vocab at call time).
_GENERIC_SEGS = frozenset({
    "api", "apis", "routers", "routes", "views", "pages", "app", "src",
    "backend", "frontend", "server", "client", "lib", "utils", "handlers",
    "controllers", "endpoints", "rest", "v1", "v2", "v3", "features",
    "components", "modules", "services",
})

#: Tokens that never name a sub-object (noise, HTTP verbs, quantity/
#: scope MODIFIERS — "bulk-lifecycle" is about lifecycle, not "bulk").
#: The ACTION tail is a corroboration vocabulary (mechanisms-over-
#: vocabularies doctrine): an action token names an intent, not an
#: outcome surface — "detectors approve/reject" is the approval step
#: of ONE journey, not two journeys (wave4 dry-run anti-cases; the
#: anti-case tests pin 'settings'/'notes'/'downloads' as REAL subs).
_NOISE_TOKENS = frozenset({
    "flow", "id", "int", "obj", "api", "v1", "v2", "v3", "get", "post",
    "put", "patch", "delete", "head", "options", "all", "new", "index",
    "list", "detail", "details", "page", "bulk", "batch", "multi",
    "single", "own", "self",
    # action tokens (intent, not outcome surface)
    "approve", "reject", "verify", "validate", "refresh", "suggest",
    "generate", "calculate", "duplicate", "create", "update", "edit",
    "manage", "view", "browse", "check", "toggle", "enable", "disable",
})


def _noise_token(t: str) -> bool:
    if t in _NOISE_TOKENS:
        return True
    # composed identifier tails: teamid / workflowid / domainslug
    return (t.endswith("id") and len(t) > 3) or t.endswith("slug")

_INTENT_BY_VERB = {
    "view": "browse", "connect": "manage", "ingest": "execute",
    "send": "execute", "receive": "execute", "run": "execute",
    "manage": "manage",
}


def lattice_enabled() -> bool:
    return os.environ.get(_ENV, "1").strip().lower() not in {"0", "false"}


def _tokens(s: str) -> list[str]:
    return [t for t in _TOKEN_RE.split((s or "").lower()) if t]


def _singular(t: str) -> str:
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    return t[:-1] if t.endswith("s") and len(t) > 3 else t


def _strip_page(t: str) -> str:
    """``chatpage``/``dashboardpage`` name the OBJECT, not the page —
    frontend page-file stems carry the suffix by convention."""
    return t[:-4] if t.endswith("page") and len(t) > 6 else t


def _flow_key(fl: Any) -> str:
    return str(getattr(fl, "uuid", "") or getattr(fl, "name", "") or "")


def _flow_loc(fl: Any) -> int:
    """Flow body LOC per the 6.97 convention source (line_ranges spans)."""
    total = 0
    for lr in getattr(fl, "line_ranges", None) or []:
        start = getattr(lr, "start_line", None)
        end = getattr(lr, "end_line", None)
        if isinstance(start, int) and isinstance(end, int):
            total += max(0, end - start + 1)
    return total


_HTTP_TOKENS = frozenset({"get", "post", "put", "patch", "delete",
                          "head", "options"})


def _verb_family(fl: Any, vocab: Mapping[str, Any]) -> str:
    """Single-flow verb family via the naming stack's vocab classes
    (fixed class order, ``manage`` fallback — mirrors
    naming_contract._flow_verb_verdict). Bare HTTP-method tokens from
    route-derived flow names are WEAK evidence: ``post-api-…-fork`` is
    a REST create, not a "send" journey — they only decide when no
    real verb token is present (get → view, mutating methods → manage).
    """
    classes: Mapping[str, Any] = vocab.get("flow_verb_classes") or {}
    toks = set(_tokens(str(getattr(fl, "name", "") or getattr(fl, "id", ""))))
    strong = toks - _HTTP_TOKENS
    for cls in ("connect", "ingest", "send", "receive", "run", "view"):
        if any(t in strong for t in (classes.get(cls) or ())):
            return cls
    if "get" in toks and not (toks & (_HTTP_TOKENS - {"get"})):
        return "view"
    return "manage"


def _route_family(fl: Any) -> str | None:
    """Param-stripped entry-file dir (route_group_recall._group_dir rule)."""
    ep = getattr(fl, "entry_point_file", None)
    if not ep:
        return None
    segs = str(ep).split("/")[:-1]
    while segs and _PARAM_SEG.match(segs[-1]):
        segs.pop()
    return "/".join(segs) or None


def _entry_object(fl: Any) -> str | None:
    """Object named by the entry path: the deepest non-param,
    non-generic segment; the FILE STEM when every dir segment is a
    generic container (backend/routers/investigations.py →
    ``investigations``)."""
    ep = str(getattr(fl, "entry_point_file", "") or "")
    if not ep:
        return None
    segs = [s for s in ep.split("/") if s]
    stem = segs[-1]
    stem = stem[:stem.find(".")] if "." in stem else stem
    for seg in reversed(segs[:-1]):
        if _PARAM_SEG.match(seg):
            continue
        low = seg.lower()
        if low in _GENERIC_SEGS or len(low) < 3:
            continue
        return _singular(low)
    low = _strip_page(stem.lower())
    if low not in _GENERIC_SEGS and not _noise_token(low) and len(low) >= 3:
        return _singular(low)
    return None


def _section_family(fl: Any) -> str | None:
    """Dominant W4 interior section: most common parent dir basename of
    ``role="interior"`` node files (ties break lexicographically)."""
    counts: Counter[str] = Counter()
    for nd in getattr(fl, "nodes", None) or []:
        if getattr(nd, "role", None) != "interior":
            continue
        f = str(getattr(nd, "file", "") or "")
        if "/" in f:
            counts[f.rsplit("/", 2)[-2].lower()] += 1
    if not counts:
        return None
    best = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return _singular(best[0])


def _sub_object(fl: Any, obj: str | None) -> str | None:
    """Sub-object token from the flow's route-derived name: the first
    informative token AFTER the object token (``patch-api-
    investigations-investigation-id-notes-note-id`` → ``notes``).
    Deterministic, derived from the extractor-built flow identity —
    the same artifact the validator/hunter rulers read."""
    if not obj:
        return None
    toks = _tokens(str(getattr(fl, "name", "") or getattr(fl, "id", "")))
    try:
        # match the object or its singular/plural twin
        idx = next(i for i, t in enumerate(toks)
                   if _singular(t) == obj or t == obj)
    except StopIteration:
        return None
    for t in toks[idx + 1:]:
        st = _singular(t)
        if st == obj or _noise_token(t) or _PARAM_SEG.match(t):
            continue
        if len(t) < 3 or st in _GENERIC_SEGS or t in _GENERIC_SEGS:
            continue
        return st
    return None


def _cluster_key(fl: Any, vocab: Mapping[str, Any]) -> tuple[str, str]:
    """(object, facet) partition key. Facet = sub-object, else interior
    section, else '' (the object core). Object falls back to the route
    family so entry-less/odd flows still land deterministically."""
    obj = _entry_object(fl)
    sub = _sub_object(fl, obj)
    if sub is None:
        sec = _section_family(fl)
        sub = sec if (sec and sec != obj) else ""
    if obj is None:
        obj = (_route_family(fl) or "misc").rsplit("/", 1)[-1].lower()
        obj = _singular(obj)
    return obj, sub or ""


def _dominant_axis(fl_list: list[Any], obj: str, sub: str) -> str:
    """Which evidence axis produced this cluster — the binding note."""
    if not sub:
        return "entry-dir"
    if any(_sub_object(f, _entry_object(f)) == sub for f in fl_list):
        return "route"
    return "section"


def _display_sub(fls: list[Any], obj: str, sub: str) -> str:
    """The natural display token for a sub-object: the most common RAW
    spelling among the member flows (``notes`` over the normalized
    ``note``), ties lexicographic. Falls back to the normalized key."""
    counts: Counter[str] = Counter()
    for fl in fls:
        for t in _tokens(str(getattr(fl, "name", "") or getattr(fl, "id", ""))):
            if _singular(t) == sub and not _noise_token(t):
                counts[t] += 1
    if not counts:
        return sub
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _humanize(*parts: str) -> str:
    toks: list[str] = []
    for p in parts:
        toks.extend(t for t in re.split(r"[-_\s]+", p) if t)
    return " ".join(toks)


def _template_name(
    verb: str, obj: str, sub: str, vocab: Mapping[str, Any],
) -> str:
    """Deterministic keyless name: ``journey_templates.generic[verb]``
    over the object phrase (W3 vocabulary; polished downstream)."""
    templates = (vocab.get("journey_templates") or {}).get("generic") or {}
    tmpl = templates.get(verb) or templates.get("manage") or "Manage {r}"
    phrase = _humanize(_singular(obj), sub) if sub else _humanize(obj)
    name = tmpl.replace("{r}", phrase)
    try:
        from faultline.pipeline_v2.naming_contract import (
            polish_display_casing,
        )
        return polish_display_casing(name, vocab)
    except Exception:  # noqa: BLE001 — casing polish is cosmetic
        return name


def _lattice_uf_id(pf_key: str, obj: str, sub: str) -> str:
    h = hashlib.sha256(
        f"lattice-v1|{pf_key}|{obj}|{sub}".encode("utf-8")).hexdigest()[:8]
    return f"UF-lat-{h}"


def apply_journey_lattice(
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    product_features: list[Any],
    *,
    vocab: Mapping[str, Any] | None = None,
    labeler: Callable[[list[Any]], dict[str, Any] | None] | None = None,
    verifier: Callable[[list[dict[str, Any]]], dict[str, bool]] | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """Run the lattice pass in place; return telemetry.

    Mutates ``user_flows`` (splits append, subset-dupes drop, members
    redistribute) and remaps ``Flow.user_flow_id``. Never touches
    features / product features / lanes.
    """
    tele: dict[str, Any] = {
        "enabled": lattice_enabled(),
        "pfs_scanned": 0, "catchalls_split": 0, "journeys_created": 0,
        "subset_merged": 0, "verifier_rejects": 0,
        "splits": [],
    }
    if not tele["enabled"] or not user_flows:
        return tele
    if vocab is None:
        from faultline.pipeline_v2.naming_contract import load_naming_vocab

        vocab = load_naming_vocab()

    flow_by_key: dict[str, Any] = {}
    for fl in flows:
        for k in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
            if k:
                flow_by_key.setdefault(str(k), fl)

    def _members(uf: Any) -> list[Any]:
        out = []
        for mid in getattr(uf, "member_flow_ids", None) or []:
            fl = flow_by_key.get(str(mid))
            if fl is not None:
                out.append(fl)
        return out

    # ── subset-duplicate merge (brief §3, the hunts case) ────────────
    interactive = [
        u for u in user_flows
        if str(getattr(u, "category", "") or "interactive") != "system"
        and not getattr(u, "synthesized", False)
    ]
    by_pf: dict[str, list[Any]] = {}
    for u in interactive:
        by_pf.setdefault(str(getattr(u, "product_feature_id", "") or ""),
                         []).append(u)
    dropped: set[int] = set()
    merged_into: dict[str, str] = {}  # dropped UF id → surviving UF id
    for pf_key, group in sorted(by_pf.items()):
        sets = [(u, {str(m) for m in (u.member_flow_ids or [])})
                for u in group]
        for i, (ua, sa) in enumerate(sets):
            if not sa or id(ua) in dropped:
                continue
            for j, (ub, sb) in enumerate(sets):
                if i == j or id(ub) in dropped or not sb:
                    continue
                if sa < sb or (sa == sb and i < j):
                    # A ⊂ B (or equal — first wins): A merges into B
                    dropped.add(id(ua))
                    merged_into[str(getattr(ua, "id", "") or "")] = str(
                        getattr(ub, "id", "") or "")
                    tele["subset_merged"] += 1
                    if log is not None:
                        log.info(
                            f"lattice: subset-merge '{ua.name}' "
                            f"({len(sa)}m) ⊂ '{ub.name}' ({len(sb)}m)")
                    break
    if dropped:
        user_flows[:] = [u for u in user_flows if id(u) not in dropped]
        for fl in flows:
            cur = str(getattr(fl, "user_flow_id", "") or "")
            if cur in merged_into:
                fl.user_flow_id = merged_into[cur]

    # ── catch-all detection + split (brief §1-2) ─────────────────────
    pf_keys_with_ufs = {
        str(getattr(u, "product_feature_id", "") or "")
        for u in user_flows if getattr(u, "product_feature_id", None)
    }
    tele["pfs_scanned"] = len(pf_keys_with_ufs)

    from faultline.models.types import UserFlow  # local: import cycle safety

    new_ufs: list[Any] = []
    for uf in list(user_flows):
        if str(getattr(uf, "category", "") or "interactive") == "system":
            continue
        if getattr(uf, "synthesized", False):
            continue  # seeds are thin by design — never lattice targets
        members = _members(uf)
        if len(members) < _CATCHALL_MIN_CLUSTERS:
            continue
        clusters: dict[tuple[str, str], list[Any]] = {}
        for fl in members:
            clusters.setdefault(_cluster_key(fl, vocab), []).append(fl)

        # qualification + fold of thin clusters into the core
        uf_resource = _singular(str(getattr(uf, "resource", "") or "").lower())
        import math

        floor = max(_MIN_CLUSTER_FLOWS,
                    math.ceil(len(members) * _CLUSTER_MEMBER_SHARE))
        qualified: dict[tuple[str, str], list[Any]] = {}
        thin: list[Any] = []
        for key, fls in clusters.items():
            if len(fls) >= floor:
                qualified[key] = fls
            else:
                thin.extend(fls)
        if len(qualified) < _CATCHALL_MIN_CLUSTERS:
            continue  # not a catch-all — leave untouched

        # core = the cluster named by the UF's own resource, else largest
        def _core_rank(kv: tuple[tuple[str, str], list[Any]]):
            (obj, sub), fls = kv
            own = 0 if (obj == uf_resource and not sub) else 1
            return (own, -len(fls), obj, sub)

        core_key = min(qualified.items(), key=_core_rank)[0]
        qualified[core_key].extend(thin)

        # build children (deterministic order), core keeps the original
        children: list[Any] = []
        pf_key = str(getattr(uf, "product_feature_id", "") or "")
        for (obj, sub) in sorted(k for k in qualified if k != core_key):
            fls = qualified[(obj, sub)]
            verbs = Counter(_verb_family(f, vocab) for f in fls)
            verb = min(verbs.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            axis = _dominant_axis(fls, obj, sub)
            sub_disp = _display_sub(fls, obj, sub) if sub else ""
            child = UserFlow(
                id=_lattice_uf_id(pf_key, obj, sub),
                name=_template_name(verb, obj, sub_disp, vocab),
                description=None,
                domain=getattr(uf, "domain", None),
                product_feature_id=pf_key or None,
                intent=_INTENT_BY_VERB.get(verb, "other"),
                resource=obj,
                member_flow_ids=sorted(_flow_key(f) for f in fls),
                member_count=len(fls),
                routes=sorted({rf for rf in
                               (_route_family(f) for f in fls) if rf}),
                category=getattr(uf, "category", "interactive"),
                refined=bool(getattr(uf, "refined", False)),
                synthesis_reason=f"lattice:{axis}",
            )
            children.append(child)
        if not children:
            continue

        # Draft Verifier reviews the SPLIT (keyed); reject → keep the
        # original catch-all untouched (conservative fallback — brief §4).
        if verifier is not None:
            draft_id = str(getattr(uf, "id", "") or "")
            draft = {
                "id": draft_id,
                "kind": "journey_lattice_split",
                "draft": (
                    f"'{uf.name}' ({len(members)} flows) → "
                    + "; ".join(
                        f"'{c.name}' ({c.member_count})" for c in children)
                    + f"; core keeps {len(qualified[core_key])}"
                ),
                "pf": pf_key,
                "member_flows": [
                    str(getattr(f, "name", "") or "") for f in members][:12],
            }
            try:
                verdicts = verifier([draft]) or {}
            except Exception:  # noqa: BLE001 — persona never breaks a scan
                verdicts = {}
            if verdicts.get(draft_id) is False:
                tele["verifier_rejects"] += 1
                continue

        # commit: original UF becomes the core cluster's journey
        core_fls = qualified[core_key]
        uf.member_flow_ids = sorted(_flow_key(f) for f in core_fls)
        uf.member_count = len(core_fls)
        for child in children:
            for mid in child.member_flow_ids:
                moved = flow_by_key.get(mid)
                if moved is not None:
                    moved.user_flow_id = child.id
        new_ufs.extend(children)
        tele["catchalls_split"] += 1
        tele["journeys_created"] += len(children)
        if len(tele["splits"]) < 20:
            tele["splits"].append({
                "uf": str(getattr(uf, "id", "")), "name": uf.name,
                "children": [
                    {"id": c.id, "name": c.name, "members": c.member_count}
                    for c in children],
                "core_kept": len(core_fls),
            })
        if log is not None:
            log.info(
                f"lattice: split '{uf.name}' → {len(children)} journeys "
                f"(+core {len(core_fls)})")

    # ── garbage-bucket dissolve (A3 panel: Soc0 "View network security")
    # A UF whose members are pairwise-unrelated singletons (ZERO
    # qualifying clusters, >= 3 distinct objects) and whose own resource
    # matches NONE of them is not a journey — it is a dump. Members
    # re-home DETERMINISTICALLY into the sibling UF whose resource IS
    # their object (largest member_count, then id, wins ties); members
    # with no honest home STAY (conservative — never invent a target).
    tele["garbage_dissolved"] = 0
    tele["members_rehomed"] = 0
    uf_by_resource: dict[str, list[Any]] = {}
    for u in user_flows:
        if str(getattr(u, "category", "") or "interactive") == "system":
            continue
        r = _singular(str(getattr(u, "resource", "") or "").lower())
        if r:
            uf_by_resource.setdefault(r, []).append(u)
    for uf in list(user_flows):
        if str(getattr(uf, "category", "") or "interactive") == "system":
            continue
        # member-less seed channels (system/route-group) stay out; a
        # BACKSTOP-synthesized UF with live members is the dump class
        # itself (Soc0 "View network security" ships as
        # uncovered_product_feature_backstop) — it IS a dissolve target.
        reason = str(getattr(uf, "synthesis_reason", "") or "")
        if getattr(uf, "synthesized", False) and \
                reason != "uncovered_product_feature_backstop":
            continue
        if uf in new_ufs:
            continue
        members = _members(uf)
        if len(members) < 4:
            continue
        keyed = [(fl, _cluster_key(fl, vocab)) for fl in members]
        objs = Counter(k[0] for _, k in keyed)
        floor2 = max(_MIN_CLUSTER_FLOWS,
                     -int(-len(members) * _CLUSTER_MEMBER_SHARE // 1))
        cluster_sizes = Counter(k for _, k in keyed)
        qualifying = sum(1 for n in cluster_sizes.values() if n >= floor2)
        # the split pass owns structured UFs; a dump has almost none —
        # and no dominant same-object core to keep it honest
        if qualifying >= _CATCHALL_MIN_CLUSTERS or len(objs) < 3:
            continue
        if max(objs.values()) > floor2:
            continue  # a dominant object core — misnamed maybe, dump no
        own = _singular(str(getattr(uf, "resource", "") or "").lower())
        if own in objs:
            continue  # honestly named after some of its members
        moved: set[str] = set()
        for fl, (obj, _sub) in keyed:
            homes = [u for u in uf_by_resource.get(obj, []) if u is not uf]
            if not homes:
                continue
            home = min(homes, key=lambda u: (
                -int(getattr(u, "member_count", 0) or 0),
                str(getattr(u, "id", "") or "")))
            key = _flow_key(fl)
            home.member_flow_ids = sorted(
                set(home.member_flow_ids or []) | {key})
            home.member_count = len(home.member_flow_ids)
            fl.user_flow_id = str(getattr(home, "id", "") or "")
            moved.add(key)
        if not moved:
            continue
        uf.member_flow_ids = sorted(
            m for m in (uf.member_flow_ids or []) if str(m) not in moved)
        uf.member_count = len(uf.member_flow_ids)
        tele["garbage_dissolved"] += 1
        tele["members_rehomed"] += len(moved)
        if log is not None:
            log.info(
                f"lattice: dissolved '{uf.name}' — {len(moved)} members "
                f"re-homed by object; {uf.member_count} stay (no honest home)")
        if not uf.member_flow_ids:
            user_flows.remove(uf)

    # PM Labeler names the new journeys (keyed; selection-not-generation).
    if new_ufs and labeler is not None:
        pf_disp: dict[str, str] = {}
        for pf in product_features:
            pf_k = str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")
            pf_disp[pf_k] = str(
                getattr(pf, "display_name", None) or getattr(pf, "name", ""))
        pending = []
        for c in new_ufs:
            pending.append(_LatticePending(
                kind="user_flow", key=c.id, current=c.name,
                candidates=[c.name],
                context={
                    "pf": pf_disp.get(str(c.product_feature_id or ""), ""),
                    "resource": c.resource,
                    "member_flows": list(c.member_flow_ids)[:8],
                },
                obj=c,
                pf_display=pf_disp.get(str(c.product_feature_id or ""), ""),
            ))
        try:
            out = labeler(pending) or {}
        except Exception:  # noqa: BLE001
            out = {}
        for c in new_ufs:
            chosen = (out.get("choices") or {}).get(c.id)
            if isinstance(chosen, str) and chosen.strip():
                c.name = chosen.strip()

    user_flows.extend(new_ufs)
    return tele


class _LatticePending:
    """Duck-typed twin of naming_contract._PendingItem (labeler batch row)."""

    __slots__ = ("kind", "key", "current", "candidates", "context", "obj",
                 "pf_display")

    def __init__(self, *, kind, key, current, candidates, context, obj,
                 pf_display):
        self.kind = kind
        self.key = key
        self.current = current
        self.candidates = candidates
        self.context = context
        self.obj = obj
        self.pf_display = pf_display


__all__ = ["apply_journey_lattice", "lattice_enabled"]
