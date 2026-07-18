"""Stage 6.7a — deterministic UF pre-clustering (S2 Seg A, flag OFF).

The keyed UF layer's STRUCTURE (which flows compose which journey) is decided
today by LLM draws: 6.7c splits mega-UFs (Sonnet), 6.7d rewrites the whole
user_flows[] at journey grain (Sonnet). The 2026-07-18 probe measured the two
failure classes that follow: FAIL-OPEN (LLM dies mid-scan → the raw rollup
passes through: healthy 78-82 UFs vs 264 on the degraded Soc0 board) and
RESAMPLE (1 drifting flow of 980 flips the whole-batch cache key → −26% UF
count). Both are STRUCTURE failures — the deterministic substrate already
knows the clusters (94.9% stable flow-uuids).

Under ``FAULTLINE_UF_DET_AGGREGATION`` the journey STRUCTURE is computed
deterministically here and the LLM layer may ONLY NAME it (precedent law
"LLM-abstraction P0: fit = output-layer"): the 6.7b refiner keeps refining
name/description/intent/ui_tier/acceptance per domain (its contract already
forbids membership changes), while the structural LLM stages (6.7c split,
6.7d rewrite) are skipped by phase_finalize. Consequence — UF-COUNT is
invariant to LLM death AND to resampling, structurally.

The cluster rule (calibrated on the healthy Soc0 13:25Z artifacts, $0):
ONE cluster PER ROLLUP DOMAIN — the purest structural rule available (zero
tuned constants; scale-invariant). On the calibration artifacts it reproduces
the healthy GRAIN exactly (82 clusters vs the healthy run's 82 journey UFs)
and keeps 93% of the healthy journeys' domain fences (75/80 of the Sonnet
journeys draw from exactly one rollup domain). What it does NOT reproduce is
Sonnet's semantic member SELECTION: the healthy 6.7d run dropped 186/292
rollup UFs (54% of member slots) with NO structural predicate (kept vs
dropped profiles are indistinguishable on every structural axis — measured),
so its member-sets match this partition at mean Jaccard 0.52 (restricted to
the surviving-member universe; 0.36 raw), and even an ORACLE grouping of
atomic rollup UFs tops out at 63% of journeys at J>=0.8. That selection is
the LLM's semantic judgment and is deliberately NOT imitated here:
CONSERVATION is law (spec SACRED: no journey may be lost in pre-clustering —
fate-tally zero scattered), so clusters carry the FULL member union.

Deterministic, no LLM, no I/O. Default OFF; =0/unset leaves user_flows[]
untouched and the LLM stages gated exactly as before (byte-identical).
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import UserFlow

DET_AGGREGATION_ENV = "FAULTLINE_UF_DET_AGGREGATION"

# Fixed intent vocabulary order — majority tie-break only (a stable total
# order, not a preference weighting).
_INTENT_ORDER = (
    "author", "browse", "lifecycle", "execute", "manage", "bulk", "export",
    "other",
)


def det_aggregation_enabled() -> bool:
    """Default OFF — set ``FAULTLINE_UF_DET_AGGREGATION=1`` to arm."""
    return os.environ.get(DET_AGGREGATION_ENV, "0").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _is_channel_row(u: "UserFlow") -> bool:
    """A NON-journey channel row that must pass through the fold UNTOUCHED.

    Wave-gauntlet Class 1 (2026-07-18, marker-flag-contract 0->9 on Soc0):
    the rollup input carries member-less SYSTEM SEEDS
    (``synthesis_reason="system_flow_recall"``, 'Run <domain>') and other
    synthesized/marker rows. Folding them into a domain cluster STRIPPED
    ``synthesis_reason`` — so the downstream synth-quality demote / B45
    gap-channel machinery no longer recognised them and the board shipped
    mc=0 rows without ``is_coverage_marker`` (a B23-contract violation:
    a marker is FLAGGED, never judged by name). Channel rows are not
    journey STRUCTURE: they keep their identity, flags and members (if
    any) verbatim, and the LLM-path channel machinery handles them
    exactly as on the OFF path. Consequence: no member-less cluster can
    exist (every clustered constituent has >= 1 member).
    """
    if not (u.member_flow_ids or []):
        return True
    if getattr(u, "synthesized", False) or getattr(u, "synthesis_reason", None):
        return True
    if getattr(u, "is_coverage_marker", False):
        return True
    return False


def _dominant(ufs: list["UserFlow"]) -> "UserFlow":
    """The cluster's representative constituent: largest member_count, then
    smallest id — a deterministic, evidence-grounded choice (the heaviest
    journey carries the domain's primary vocabulary)."""
    return sorted(
        ufs, key=lambda u: (-(u.member_count or 0), u.id or ""),
    )[0]


def _majority_intent(ufs: list["UserFlow"]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for u in ufs:
        counts[u.intent or "other"] += 1
    # max count; ties resolved by the fixed vocabulary order.
    def rank(intent: str) -> tuple[int, int]:
        try:
            pos = _INTENT_ORDER.index(intent)
        except ValueError:
            pos = len(_INTENT_ORDER)
        return (-counts[intent], pos)
    return sorted(counts, key=rank)[0]


def _majority_pf(ufs: list["UserFlow"]) -> str | None:
    counts: dict[str, int] = defaultdict(int)
    for u in ufs:
        if u.product_feature_id:
            counts[u.product_feature_id] += 1
    if not counts:
        return None
    return sorted(counts, key=lambda s: (-counts[s], s))[0]


def aggregate_user_flows(
    user_flows: list["UserFlow"],
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Fold the deterministic rollup UFs into domain-grain journey clusters.

    Pure function: reads only the given UFs, returns NEW UserFlow objects
    (the inputs are not mutated) + telemetry with a per-input fate tally.
    Canonical order everywhere (sorted domains, sorted member unions) makes
    the output invariant to input order and to PYTHONHASHSEED.
    """
    telemetry: dict[str, Any] = {
        "enabled": True,
        "input_ufs": len(user_flows),
        "clusters": 0,
        "domains": 0,
        "members_in": 0,
        "members_out": 0,
        "singleton_clusters": 0,
        "merged_clusters": 0,
        "passthrough_channel_rows": 0,
        "scattered": 0,          # conservation law: MUST stay 0
        "fate": {},              # input UF id -> output id (every input named)
    }
    if not user_flows:
        return [], telemetry

    from faultline.models.types import UserFlow

    # Channel rows (member-less / synthesized / markers) are NOT journey
    # structure — they pass through verbatim (Class 1 fix, see
    # :func:`_is_channel_row`); only member-ful organic journeys cluster.
    journey_rows: list["UserFlow"] = []
    channel_rows: list["UserFlow"] = []
    for u in user_flows:
        (channel_rows if _is_channel_row(u) else journey_rows).append(u)
    telemetry["passthrough_channel_rows"] = len(channel_rows)

    by_domain: dict[str, list["UserFlow"]] = defaultdict(list)
    for u in journey_rows:
        by_domain[str(u.domain) if u.domain is not None else ""].append(u)

    telemetry["domains"] = len(by_domain)
    members_in = 0
    for u in user_flows:
        members_in += len(set(u.member_flow_ids or []))
    telemetry["members_in"] = members_in

    clusters: list["UserFlow"] = []
    fate: dict[str, str] = {}
    members_out = 0
    for n, (dom_key, ufs) in enumerate(
        sorted(by_domain.items(), key=lambda kv: kv[0]), start=1,
    ):
        # Canonical constituent order (id) — stable regardless of input order.
        ufs = sorted(ufs, key=lambda u: u.id or "")
        dom = _dominant(ufs)
        member_union: set[str] = set()
        routes: set[str] = set()
        cross: set[str] = set()
        ac_draft = 0
        for u in ufs:
            member_union.update(u.member_flow_ids or [])
            routes.update(u.routes or [])
            cross.update(u.cross_links or [])
            ac_draft += u.ac_draft_count or 0
        cid = f"UF-{n:03d}"
        cluster = UserFlow(
            id=cid,
            name=dom.name,
            description=None,
            domain=dom.domain,
            product_feature_id=_majority_pf(ufs),
            intent=_majority_intent(ufs),
            resource=dom.resource,
            member_flow_ids=sorted(member_union),
            member_count=len(member_union),
            routes=sorted(routes),
            cross_links=sorted(cross),
            ac_draft_count=ac_draft,
            acceptance=[],
            coverage_pct=None,
            ui_tier=None,
            category=dom.category,
            trigger=dom.trigger,
            refined=False,
            name_confidence=dom.name_confidence,
        )
        clusters.append(cluster)
        members_out += len(member_union)
        for u in ufs:
            fate[u.id] = cid
        if len(ufs) == 1:
            telemetry["singleton_clusters"] += 1
        else:
            telemetry["merged_clusters"] += 1

    # ── channel-row passthrough (renumbered AFTER the clusters so ids stay
    # unique in the canonical "UF-NNN" format; every field — synthesis_reason,
    # is_coverage_marker, members, category/trigger — is preserved verbatim;
    # the emission I14 backpointer rewrite re-syncs flow.user_flow_id).
    out: list["UserFlow"] = list(clusters)
    next_n = len(clusters)
    for u in sorted(channel_rows, key=lambda r: r.id or ""):
        next_n += 1
        new_id = f"UF-{next_n:03d}"
        fate[u.id] = new_id
        u = u.model_copy(update={"id": new_id})
        out.append(u)
        members_out += len(set(u.member_flow_ids or []))

    telemetry["clusters"] = len(clusters)
    telemetry["members_out"] = members_out
    telemetry["fate"] = fate
    telemetry["scattered"] = sum(
        1 for u in user_flows if u.id not in fate
    )
    return out, telemetry


# ── S2 Seg A iter-3 — readability regrain (panel-spot blockers, 2026-07-18) ─
#
# The keyed panel CONDITIONAL-PASSED stability but blocked the merge on three
# VERIFIED classes, all fixed here deterministically under the same flag:
#
# B1 — a cluster named by ONE member hides the rest ('Send cases' mc=33 = the
#      whole cases router incl. bulk/export/group — no "send" there) and any
#      mc >= ~25 row is unreadable regardless of its name. Fix: rows over the
#      READABILITY bar SPLIT by member NAME-TOKEN FAMILIES (flow names are
#      engine-derived from route paths — route-grounded transitively); child
#      names are built from the family's OWN members (majority verb-class +
#      family tokens), never from a single row.
# B2 — CRUD oversplit: the lattice action axis ships 'Create/Update/Delete
#      <resource>' dev-grain leaves as journeys (ON=28 vs OFF=0). Fix: action
#      siblings of one (pf, resource) COLLAPSE into 'Manage <resource>' UNLESS
#      a sibling carries its own route anchor (member entry files disjoint
#      from the rest — the B4/B6/B50 family rule); plus the raw 'lattice:*'
#      token never ships in the displayed domain field.
# B3 — buried spine ('Configure autonomous SOC …'): a small journey family
#      swallowed by a big cluster resurfaces via the same family split (its
#      distinct name-token family mints a first-class child).
#
# Everything below is pure + deterministic ($0, no LLM, no I/O); the wiring in
# phase_finalize runs it right after the journey lattice, ONLY under
# FAULTLINE_UF_DET_AGGREGATION. Regrain children carry the lattice id prefix
# ("UF-L-r…") so the uf_synth_fold exemption + gauntlet treat them as
# sanctioned partition rows.

#: The panel-sanctioned readability bar (operator ruling 2026-07-18): a journey
#: over ~25 members is unreadable whatever its name. An explicit product
#: constant (like flows_per_feature_cap=12), not a tuned detection threshold.
READABILITY_MC_BAR = 25

#: Structural floor for a mintable family — mirrors the lattice's _MIN_MINTABLE
#: (>= 2 members; a 1-member family is not a journey).
MIN_FAMILY_MEMBERS = 2

_REGRAIN_ID_PREFIX = "UF-L-r"   # lattice-family prefix -> fold-exempt + born


def _clean_flow_tokens(name: str, verbs: frozenset[str]) -> tuple[list[str], str]:
    """``('create-case-bulk-flow') -> (['case','bulk'], 'create')``.

    Splits a flow name on '-', drops the trailing 'flow', consumes the
    leading verb-class tokens (the engine's OWN template vocab — mechanism,
    not a new dictionary). Returns (object tokens, leading verb or '')."""
    toks = [t for t in str(name or "").lower().split("-") if t]
    if toks and toks[-1] == "flow":
        toks = toks[:-1]
    verb = ""
    while toks and toks[0] in verbs:
        verb = verb or toks[0]
        toks = toks[1:]
    while toks and toks[0] in _PREFIX_TOKENS:
        toks = toks[1:]
    return toks, verb


def _sing(tok: str) -> str:
    return tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok


#: Route-grammar CONNECTOR tokens — structural glue in engine flow names
#: ('view-case-by-case-id'), never an object family. Grammar of the engine's
#: own name templates, not a per-repo vocabulary.
_CONNECTOR_TOKENS = frozenset({"by", "id", "ids", "all", "of", "for"})

#: Route-scaffold PREFIX tokens — the engine derives raw flow names from
#: route paths, so every '/api/…' route yields an 'api-…' name at regrain
#: time (flow_name_v2 renames AFTER this pass). 'api' is path scaffolding,
#: never the object root ('api-admin-chat…' roots at 'admin').
_PREFIX_TOKENS = frozenset({"api"})


def _family_of(tokens: list[str], root: str, depth: int = 0) -> str:
    """The member's family key inside a cluster rooted at ``root``:
    same-root members family on their qualifier token at ``depth``
    ('case-comments' -> 'comments'); foreign-rooted members family on their
    own root ('detector-bulk' under root 'finding' -> 'detector' — the
    misfile class surfaces under its honest object name). A CONNECTOR
    qualifier ('case-by-case-id') is core CRUD, not a family. ``depth``
    advances when a partition is DEGENERATE (one family == the whole
    cluster): 'threat-hunt-article/adopt/phase…' all family at 'hunt' on
    depth 0; depth 1 surfaces article/adopt/phase — the buried workflow."""
    if not tokens:
        return ""
    if _sing(tokens[0]) == _sing(root):
        pos = 1 + depth
        if len(tokens) > pos and tokens[pos] not in _CONNECTOR_TOKENS:
            return _sing(tokens[pos])
        return ""
    return _sing(tokens[depth]) if depth < len(tokens) else ""


def _majority_root(member_names: list[str], verbs: frozenset[str]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for nm in member_names:
        toks, _ = _clean_flow_tokens(nm, verbs)
        if toks:
            counts[_sing(toks[0])] += 1
    if not counts:
        return ""
    return sorted(counts, key=lambda t: (-counts[t], t))[0]


def _family_verb(verbs_seen: list[str]) -> str:
    """One verb-class for the family name: unanimous verb keeps it; any mix
    is 'manage' (the CRUD umbrella)."""
    distinct = {v for v in verbs_seen if v}
    if len(distinct) == 1:
        return next(iter(distinct))
    return "manage"


def _title(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _plural(tok: str) -> str:
    return tok if tok.endswith("s") else tok + "s"


def _entry_of(fl: Any) -> str:
    return str(getattr(fl, "entry_point_file", "") or "")


def collapse_crud_action_children(
    user_flows: list["UserFlow"],
    flows_by_id: dict[str, Any],
    verbs: frozenset[str],
) -> dict[str, Any]:
    """B2 — collapse the lattice ACTION-axis CRUD leaves of one (pf, resource)
    into one 'Manage <resource>' row, UNLESS a leaf carries its own route
    anchor (its member ENTRY FILES are disjoint from every sibling's — a
    genuinely separate surface survives). Mutates in place; conservation by
    member union."""
    tele = {"groups": 0, "collapsed_children": 0, "route_anchored_kept": 0}
    groups: dict[tuple[str, str], list["UserFlow"]] = defaultdict(list)
    for u in user_flows:
        dom = str(u.domain or "")
        if not str(u.id or "").startswith("UF-L-"):
            continue
        if not dom.startswith("lattice:action:"):
            continue
        key = dom.split(":", 2)[2]
        root = _sing(key.split("-")[0]) if key else ""
        pf = str(u.product_feature_id or "")
        if root:
            groups[(pf, root)].append(u)

    dead: set[str] = set()
    for (pf, root), kids in sorted(groups.items()):
        if len(kids) < 2:
            continue
        tele["groups"] += 1
        kids = sorted(kids, key=lambda u: (-(u.member_count or 0), u.id or ""))
        entry_sets = {
            u.id: {
                _entry_of(flows_by_id[m]) for m in (u.member_flow_ids or [])
                if m in flows_by_id and _entry_of(flows_by_id[m])
            }
            for u in kids
        }
        mergeable: list["UserFlow"] = []
        for u in kids:
            others: set[str] = set()
            for v in kids:
                if v is not u:
                    others |= entry_sets.get(v.id, set())
            mine = entry_sets.get(u.id, set())
            if mine and others and not (mine & others):
                tele["route_anchored_kept"] += 1     # own surface — survives
                continue
            mergeable.append(u)
        if len(mergeable) < 2:
            continue
        canon = mergeable[0]
        union: set[str] = set()
        routes: set[str] = set()
        for u in mergeable:
            union.update(u.member_flow_ids or [])
            routes.update(u.routes or [])
            if u is not canon:
                dead.add(u.id)
                tele["collapsed_children"] += 1
        canon.member_flow_ids = sorted(union)
        canon.member_count = len(union)
        canon.routes = sorted(routes)
        canon.name = f"Manage {_plural(root)}"
        canon.resource = root
        canon.intent = "manage"
    if dead:
        user_flows[:] = [u for u in user_flows if u.id not in dead]
    return tele


def split_oversized_ufs(
    user_flows: list["UserFlow"],
    flows_by_id: dict[str, Any],
    verbs: frozenset[str],
) -> dict[str, Any]:
    """B1/B3 — any journey over READABILITY_MC_BAR splits by member
    name-token FAMILIES; each qualifying family (>= MIN_FAMILY_MEMBERS) mints
    a first-class child NAMED FROM ITS OWN MEMBERS (majority verb-class +
    family token); the residual keeps the parent. Deterministic ids from
    content (pf|parent|family). Conservation: members partition exactly."""
    import hashlib as _h

    tele = {"parents_split": 0, "children_minted": 0, "members_moved": 0}
    new_rows: list["UserFlow"] = []
    for u in user_flows:
        # Channel rows (markers / synthesized recall) keep their identity —
        # never partitioned here (same law as the 6.7a fold passthrough).
        if u.is_coverage_marker or u.synthesis_reason:
            continue
        members = list(u.member_flow_ids or [])
        if len(members) < READABILITY_MC_BAR:
            continue
        names = {
            m: str(getattr(flows_by_id.get(m), "name", "") or m)
            for m in members
        }
        root = _majority_root(list(names.values()), verbs)
        # Depth ladder: a DEGENERATE partition (one family holding the whole
        # over-bar cluster — 'threat-hunt-*' all family at 'hunt') carries no
        # information; advance one token deeper (max 3 — flow-name grammar
        # depth) until the partition separates something.
        qualifying: dict[str, list[str]] = {}
        fams: dict[str, list[str]] = {}
        fam_verbs: dict[str, list[str]] = {}
        fam_kind: dict[str, str] = {}
        fam_repr: dict[str, list[str]] = {}
        fam_depth = 0
        for depth in range(3):
            fams = defaultdict(list)
            fam_verbs = defaultdict(list)
            fam_kind = {}   # 'qual' (root's qualifier) | 'foreign'
            fam_repr = {}
            fam_depth = depth
            for m in members:
                toks, verb = _clean_flow_tokens(names[m], verbs)
                fam = _family_of(toks, root, depth)
                kind = (
                    "qual" if toks and _sing(toks[0]) == _sing(root)
                    else "foreign"
                )
                fams[fam].append(m)
                fam_verbs[fam].append(verb)
                fam_kind.setdefault(fam, kind)
                fam_repr.setdefault(fam, toks)
            qualifying = {
                f: ms for f, ms in fams.items()
                if f and len(ms) >= MIN_FAMILY_MEMBERS
            }
            degenerate = (
                len(qualifying) == 1
                and len(next(iter(qualifying.values()))) >= len(members)
            )
            if qualifying and not degenerate:
                break
            qualifying = {} if degenerate else qualifying
        if not qualifying:
            continue
        tele["parents_split"] += 1
        residual: list[str] = [
            m for f, ms in fams.items() if f not in qualifying for m in ms
        ]
        for fam in sorted(qualifying):
            ms = sorted(qualifying[fam])
            verb = _family_verb(fam_verbs[fam])
            # Child name from the family's OWN members (panel B1): a
            # qualifier family reads under its root object + the full
            # qualifier path at the split depth ('Manage case comments';
            # depth-1: 'Manage threat hunt articles'); a FOREIGN-rooted
            # family reads under its own honest object path ('Manage
            # detectors' — the misfile class surfaces).
            repr_toks = fam_repr.get(fam) or []
            if fam_kind.get(fam) == "qual":
                mid = [t for t in repr_toks[1:1 + fam_depth]
                       if t not in _CONNECTOR_TOKENS]
                parts = [_sing(root)] + mid + [_plural(fam)]
            else:
                mid = [t for t in repr_toks[:fam_depth]
                       if t not in _CONNECTOR_TOKENS and _sing(t) != _sing(fam)]
                parts = mid + [_plural(fam)]
            fam_disp = " ".join(parts)
            child = u.model_copy(update={
                "id": _REGRAIN_ID_PREFIX + _h.sha1(
                    f"{u.product_feature_id}|{u.id}|{fam}".encode()
                ).hexdigest()[:10],
                "name": f"{_title(verb)} {fam_disp}",
                "resource": fam,
                "member_flow_ids": ms,
                "member_count": len(ms),
                "routes": sorted(u.routes or []),
                "acceptance": [],
                "ac_draft_count": 0,
                "refined": False,
            })
            new_rows.append(child)
            tele["children_minted"] += 1
            tele["members_moved"] += len(ms)
        u.member_flow_ids = sorted(residual)
        u.member_count = len(residual)
        # Panel B1: the residual parent must not keep a one-member misname
        # ('Send cases' over core case-CRUD). Rename it from its OWN
        # remaining members: majority verb-class over the residual + the
        # cluster's root object.
        if residual and root:
            res_verbs = [
                _clean_flow_tokens(names.get(m, m), verbs)[1]
                for m in residual
            ]
            u.name = f"{_title(_family_verb(res_verbs))} {_plural(root)}"
            u.resource = root
    if new_rows:
        # A parent whose members ALL moved into children dissolves (channel
        # rows are untouched by construction — they are never over the bar
        # as splittable journeys with families).
        gone = {
            u.id for u in user_flows
            if not (u.member_flow_ids or [])
            and not u.is_coverage_marker and not u.synthesis_reason
            and u.id not in {n.id for n in new_rows}
        }
        user_flows[:] = [
            u for u in user_flows if u.id not in gone
        ] + new_rows
        tele["parents_dissolved"] = len(gone)
    return tele


def sanitize_lattice_domains(user_flows: list["UserFlow"]) -> int:
    """B2 display law — the raw 'lattice:*' token never ships in the domain
    field: replace with the row's resource (or None). The lattice-born
    predicate downstream keys on the ID prefix, which is untouched."""
    n = 0
    for u in user_flows:
        if str(u.domain or "").startswith("lattice:"):
            res = str(u.resource or "")
            # A resource that itself carries the raw token is no substitute.
            u.domain = res if res and not res.startswith("lattice:") else None
            n += 1
    return n


def readability_regrain(
    user_flows: list["UserFlow"],
    flows: list[Any],
    verbs: frozenset[str],
) -> dict[str, Any]:
    """The full iter-3 pass: CRUD collapse -> oversized split -> domain
    sanitation. Mutates ``user_flows`` in place; returns telemetry with a
    conservation check (member universe before == after)."""
    flows_by_id: dict[str, Any] = {}
    for f in flows:
        key = str(getattr(f, "uuid", "") or getattr(f, "name", "") or "")
        if key:
            flows_by_id[key] = f
        nm = str(getattr(f, "name", "") or "")
        if nm:
            flows_by_id.setdefault(nm, f)
    before: set[str] = set()
    for u in user_flows:
        before.update(u.member_flow_ids or [])
    tele: dict[str, Any] = {"enabled": True}
    tele["crud_collapse"] = collapse_crud_action_children(
        user_flows, flows_by_id, verbs,
    )
    tele["oversplit"] = split_oversized_ufs(user_flows, flows_by_id, verbs)
    # Second (final) pass: a minted child can itself exceed the bar when a
    # FOREIGN-rooted family is large (a 28-member 'team' family); it re-splits
    # once by ITS OWN root's qualifier families. Two passes are the fixpoint
    # for name-token grammar (root -> qualifier); deeper nesting has no
    # further token axis, so a still-large row after pass 2 stays honestly.
    tele["oversplit_pass2"] = split_oversized_ufs(
        user_flows, flows_by_id, verbs,
    )
    tele["domains_sanitized"] = sanitize_lattice_domains(user_flows)
    after: set[str] = set()
    for u in user_flows:
        after.update(u.member_flow_ids or [])
    tele["members_before"] = len(before)
    tele["members_after"] = len(after)
    tele["members_lost"] = len(before - after)   # conservation: MUST be 0
    return tele


__all__ = [
    "DET_AGGREGATION_ENV",
    "MIN_FAMILY_MEMBERS",
    "READABILITY_MC_BAR",
    "aggregate_user_flows",
    "collapse_crud_action_children",
    "det_aggregation_enabled",
    "readability_regrain",
    "sanitize_lattice_domains",
    "split_oversized_ufs",
]
