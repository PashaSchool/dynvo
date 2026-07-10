"""Stage 6.986 — mega-PF nav-area journey re-home + floor-gated mint (B24).

THE PROBLEM (q1 supabase dossier, operator-ratified 2026-07-10): one
product feature can annex a whole console route tree and become the
board's journey umbrella — supabase ``projects`` homes 29/87 journeys
(33% of the board) while 19 distinct studio nav areas hide inside it
and the SAME board carries starved siblings for many of them
(``settings`` 28.9K LOC with one member-less seed journey). All 29 UFs
are entry-owner coherent (the fold owns the entire
``apps/studio/pages/project/[ref]/**`` tree), so the I16 ruler is
satisfied vacuously and Stage 6.99 cannot fire — the correct grain is
route-subtree (nav-area) ownership, the B22a fold-annexation family.

THE FIX (operator decision "B-narrow + single database mint",
generalized — mechanisms, not vocabularies): under
``FAULTLINE_MEGA_PF_NAV_REHOME`` (default OFF; the flip decision comes
from the keyed supabase OFF/ON A/B):

TRIGGER (both prongs, board's strict top-UF-count PF only):
  * T1 nav-multiplicity — the PF's homed journeys strict-majority-
    resolve into >= 3 distinct non-core qualifying nav-area groups
    (3 = the B6 journey-lattice ``_CATCHALL_MIN_CLUSTERS`` bar one
    level up; qualifying = >= 2 UFs or >= 3 member flows — the
    journey-candidate / full-CRUD-signature floor family).
  * T2 board dominance — the PF holds >= 25% of the board's homed
    journeys (scale-invariant share; corpus-calibrated on 13 boards:
    observed top shares 0.367 / 0.333 / 0.237 / 0.208 / … — a natural
    gap in [0.237, 0.333]; 0.25 is the coarsest round fraction inside
    it, no magic precision).
  Transport-candidate PFs (Stage 6.985's units) are structurally
  excluded as sources AND as targets — plumbing folds are B22's case.

GRAIN: ONE oracle — :class:`transport_handoff.TargetGrainIndex` with
its B24 ``tenant_descent`` rung (``project/[ref]/database`` keys
``database``, not ``project``). The vote, the trigger census and the
mint all consult the same instance (the B22 condition-4 invariant).

MOVE (per journey, B20 strict-majority shape): a journey whose member
entry files strict-majority-resolve (>50% of resolvable entries) to an
EXISTING sibling PF re-homes there, gated by
  * the ATTACH FLOOR — projected lane-aware attach at the target
    (validator I15 mirror, ``_ATTACH_FLOOR`` = 0.34) over the
    POST-carve planned scopes; thin targets never receive a journey;
  * the all-rung I16 rail — a move that would turn an I16-clean
    journey majority-foreign under the planned owner map is refused;
  * surface rail — journey and target must share ``surface_scope``
    (the B-full docs-anchor trap stays closed);
  * same-app rail (B22a family) — the target's anchor must live under
    the same routes root as the journey's entries;
  * orphan guard (B20/I8) — the source always keeps >= 1 journey.

MINT: a nav-area group with NO existing sibling mints its own PF ONLY
with >= 3 UFs AND >= 3 member flows (the lattice floor family — this
is what keeps supabase to exactly ONE mint, ``database``) — its anchor
lineage is the route-group cid itself (an allowed product route group
by construction). Journeys in below-floor groups STAY.

CARVE (files follow their journeys — the I15 healer, 8.9.x/6.985
discipline via ``_carve_chunk``/``_move_carved_flows``/
``_strip_carved_files``): a span file follows a moved journey iff
  * it is dev-owned by the SOURCE PF and no other planned move claims
    it for a different target (contested files abstain), or
  * it has NO dev owner at stage-time (residual mass) and EVERY
    journey on the board that touches it lands on the SAME target —
    the transport-handoff r2 seed rule at journey grain; shared
    substrate (layouts, shared editors) is never annexed.
Files owned by OTHER product PFs or the lane NEVER carve. A carve
that would empty its source dev re-homes the whole dev instead, so no
flowful dev is ever stranded file-less (the I9 flowful-dev guard
posture: nothing lanes here at all).

CONSERVATION (operator law): journeys are re-homed ONLY — a journey
that cannot strict-majority-resolve, fails a rail, or belongs to a
below-floor group STAYS. Verified post-apply: UF count exact, no
OTHER PF loses a journey, the source keeps >= 1 — violations raise
under pytest/``FAULTLINE_STRICT_CONSERVATION=1`` and warn-telemeter in
prod (the 6.985 invariant shape with a persisting source).

ORDERING: phase_finalize, immediately AFTER Stage 6.985 (the journey
layer is final; transport folds already resolved) and BEFORE the 6.97
LOC prefetch — the mint + carve are loc-stamped, path_indexed and
I23-read like any other PF with zero extra plumbing. Stage 6.99 (B20)
stays downstream as a safety net: B24 moves are entry-coherent
post-carve, so 6.99 sees them clean. Stage 6.88 sibling-unify runs
EARLIER and cannot re-merge the mint (different parent namespace + no
dev-grain suffix — verified against its co-identity rails).

Deterministic, $0 LLM. Kill-switch: flag unset/0 → byte-identical
output (the stage is not entered); trigger not firing → no scan_meta
key, no output change (the 6.985 inertness convention).
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, cast

from faultline.pipeline_v2.transport_handoff import (
    _ATTACH_FLOOR,
    TargetGrainIndex,
    _attr,
    _build_owner_map,
    _carve_chunk,
    _i16_flagged,
    _move_carved_flows,
    _owned_of,
    _strict_conservation,
    _strip_carved_files,
    _uf_flow_files,
)

__all__ = [
    "MEGA_PF_NAV_REHOME_ENV",
    "mega_pf_nav_rehome_enabled",
    "run_mega_pf_nav_rehome",
]

MEGA_PF_NAV_REHOME_ENV = "FAULTLINE_MEGA_PF_NAV_REHOME"

#: Provenance marker stamped on carved chunks / re-homed devs (I22
#: explainability; the ``_HANDOFF_MARKER`` convention).
_B24_MARKER = "nav-rehome"

# ── constants — every one an existing ruler / constant class ────────────
#: T2 dominance prong (share of the board's homed journeys).
_TRIGGER_SHARE = 0.25
#: T1 prong — distinct qualifying non-core nav groups (B6 catch-all bar).
_TRIGGER_MIN_GROUPS = 3
#: A group qualifies with >= this many UFs … (journey-candidate bar)
_GROUP_QUALIFY_UFS = 2
#: … or >= this many member flows (lattice full-CRUD signature).
_GROUP_QUALIFY_FLOWS = 3
#: Mint floor: a NEW capability is its own small journey lattice.
_MINT_MIN_UFS = 3
_MINT_MIN_FLOWS = 3


def mega_pf_nav_rehome_enabled() -> bool:
    """Default OFF (Phase-2 landing posture; the default flip is the
    keyed supabase OFF/ON A/B decision, not this branch)."""
    return os.environ.get(MEGA_PF_NAV_REHOME_ENV, "0").strip().lower() in {
        "1", "true",
    }


class _NeutralLane:
    """``_i16_flagged`` lane shim: B24 has no candidate lane unit — the
    lane-neutral file set plays that role (owner ``None`` entries are
    already skipped by the ruler; this keeps the B21 convention
    explicit)."""

    def __init__(self, neutral: frozenset[str]) -> None:
        self._neutral = neutral

    def in_lane(self, path: str) -> bool:
        return path in self._neutral


def _core_identity(pf: Any) -> set[str]:
    """The PF's capability-identity tokens (anchor terminal + name,
    ``normalize_anchor_key``-normalized) — the lattice CORE rail: a nav
    group matching the source's own identity never leaves the parent."""
    from faultline.pipeline_v2.spine_anchors import (
        _DYNAMIC_RE,
        normalize_anchor_key,
    )

    out: set[str] = set()
    aid = str(_attr(pf, "anchor_id") or "")
    if ":" in aid:
        segs = [s for s in aid.split(":", 1)[1].split("/")
                if s and not _DYNAMIC_RE.match(s)]
        if segs:
            out.add(normalize_anchor_key(segs[-1]))
    out.add(normalize_anchor_key(str(_attr(pf, "name") or "")))
    out.discard("")
    return out


def _grain_token(cid: str) -> str:
    """Terminal path segment of a group cid (its area token)."""
    return cid.rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def _root_of(path: str, roots: tuple[str, ...]) -> str | None:
    return next((r for r in roots
                 if path == r or path.startswith(r + "/")), None)


def _resolve_uf(
    uf: Any,
    flow_by_uuid: Mapping[str, Any],
    grain: TargetGrainIndex,
    source_key: str,
    core: set[str],
) -> tuple[tuple[str, str] | None, int, Counter]:
    """B20 strict-majority vote over the journey's member entry files
    at the nav grain: ``(("pf"|"new"|"core", key) | None, counted,
    votes)``. ``core`` = an explicit stay verdict (the source's own
    grain); ``None`` = no strict majority (the journey stays)."""
    from faultline.pipeline_v2.spine_anchors import normalize_anchor_key

    votes: Counter = Counter()
    counted = 0
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(fid)
        ep = _attr(fl, "entry_point_file") if fl is not None else None
        if not ep:
            continue
        t = grain.grain_of_file(str(ep))
        if t is None:
            continue
        counted += 1
        if t.kind == "pf" and t.key == source_key:
            votes[("core", source_key)] += 1
        elif t.kind == "new" and normalize_anchor_key(
                _grain_token(t.key)) in core:
            votes[("core", source_key)] += 1
        else:
            votes[(t.kind, t.key)] += 1
    if not counted:
        return None, 0, votes
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], str(kv[0])))
    top, ct = ranked[0]
    if ct * 2 > counted:
        return top, counted, votes
    return None, counted, votes


def _full_paths(f: Any) -> list[str]:
    """The validator's FULL ``paths`` scope view (owned + shared claims)
    — the B22 rework attach-mirror convention."""
    return [str(p) for p in (_attr(f, "paths") or [])] or _owned_of(f)


def run_mega_pf_nav_rehome(
    developer_features: list[Any],
    product_features: list[Any],
    user_flows: list[Any],
    flows: list[Any],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    extractor_signals: dict[str, list[Any]] | None = None,
    feature_flow_edges: list[Any] | None = None,
    transport_candidate_units: Iterable[str] = (),
    grain_index: TargetGrainIndex | None = None,
) -> dict[str, Any]:
    """Stage 6.986 entrypoint — see module docstring.

    Mutates ``user_flows`` / ``developer_features`` /
    ``product_features`` in place ONLY per a verified plan; returns
    telemetry for ``scan_meta.mega_pf_nav_rehome`` (the caller writes
    the key only when the trigger fired — inertness convention).
    ``grain_index`` is an injection point for tests/sims; the default
    builds the SHARED oracle with the tenant-descent rung ON."""
    tele: dict[str, Any] = {
        "enabled": True, "triggered": [], "census": [],
        "moves": [], "mints": [], "stays": [],
        "floor_drops": [], "i16_rail_drops": [],
        "ufs_rehomed": 0, "devs_rehomed": 0, "devs_carved": 0,
        "pfs_minted": 0, "residual_claimed": 0,
    }
    if not user_flows or not product_features:
        return tele

    devs = [f for f in developer_features
            if _attr(f, "layer", "developer") == "developer"
            and _attr(f, "name")]
    flow_by_uuid: dict[str, Any] = {}
    for fl in flows or []:
        u = _attr(fl, "uuid")
        if u:
            flow_by_uuid[str(u)] = fl
    for f in devs:  # test scenes / degraded inputs (6.985 convention)
        for fl in (_attr(f, "flows") or []):
            u = _attr(fl, "uuid")
            if u and str(u) not in flow_by_uuid:
                flow_by_uuid[str(u)] = fl

    pf_by_key = {(str(_attr(pf, "id") or _attr(pf, "name"))): pf
                 for pf in product_features
                 if (_attr(pf, "id") or _attr(pf, "name"))}

    # Transport-candidate PFs: neither source nor target (6.985's case).
    transport_pf_keys: set[str] = set()
    for unit in transport_candidate_units or ():
        want = f"ws:{str(unit).strip('/')}"
        for pf in product_features:
            if str(_attr(pf, "anchor_id") or "") == want:
                key = str(_attr(pf, "id") or _attr(pf, "name") or "")
                if key:
                    transport_pf_keys.add(key)
                break

    # ── T2: strict top + dominance share ────────────────────────────
    home_counter: Counter = Counter(
        str(_attr(u, "product_feature_id"))
        for u in user_flows if _attr(u, "product_feature_id"))
    total_homed = sum(home_counter.values())
    if not total_homed:
        return tele
    ranked_homes = sorted(home_counter.items(),
                          key=lambda kv: (-kv[1], kv[0]))
    tele["census"] = [
        {"pf": k, "ufs": c, "share": round(c / total_homed, 3)}
        for k, c in ranked_homes[:5]
    ]
    top_key, top_count = ranked_homes[0]
    if len(ranked_homes) > 1 and ranked_homes[1][1] == top_count:
        return tele  # no STRICT maximum — no umbrella to arbitrate
    if top_count / total_homed < _TRIGGER_SHARE:
        return tele
    if top_key in transport_pf_keys or top_key not in pf_by_key:
        return tele
    source_key = top_key
    source_pf = pf_by_key[source_key]

    # ── THE grain oracle (shared class, tenant-descent rung ON) ─────
    if grain_index is None:
        from faultline.pipeline_v2.spine_anchors import build_spine_anchors
        anchors = build_spine_anchors(
            devs, routes_index, ctx, extractor_signals, frozenset())
        grain_index = TargetGrainIndex(
            anchors, product_features,
            routes_index=routes_index,
            candidate_pf_keys=transport_pf_keys,
            tenant_descent=True,
        )
    roots = grain_index.routes_roots
    core = _core_identity(source_pf)

    homed = sorted(
        (u for u in user_flows
         if str(_attr(u, "product_feature_id") or "") == source_key),
        key=lambda u: str(_attr(u, "id") or ""))
    resolutions: list[tuple[Any, tuple[str, str] | None]] = [
        (u, _resolve_uf(u, flow_by_uuid, grain_index, source_key, core)[0])
        for u in homed
    ]

    # ── T1: qualifying non-core nav-group census ────────────────────
    group_stats: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"ufs": 0, "flows": 0})
    for u, tgt in resolutions:
        if tgt is None or tgt[0] == "core":
            continue
        group_stats[tgt]["ufs"] += 1
        group_stats[tgt]["flows"] += len(_attr(u, "member_flow_ids") or [])
    qualifying = {g for g, s in group_stats.items()
                  if s["ufs"] >= _GROUP_QUALIFY_UFS
                  or s["flows"] >= _GROUP_QUALIFY_FLOWS}
    tele["qualifying_groups"] = sorted(f"{k}:{v}" for k, v in qualifying)
    if len(qualifying) < _TRIGGER_MIN_GROUPS:
        return tele

    tele["triggered"] = [source_key]
    owner_map, neutral_files = _build_owner_map(devs)
    # duck-typed for _i16_flagged (it reads only .in_lane) — cast keeps
    # the B22 signature untouched.
    lane_shim = cast(Any, _NeutralLane(neutral_files))
    uf_count_before = len(user_flows)
    uf_home_before = dict(home_counter)

    # ── plan: raw moves + mint demand ───────────────────────────────
    def _stay(u: Any, reason: str) -> None:
        tele["stays"].append({"uf": str(_attr(u, "id") or ""),
                              "name": str(_attr(u, "name") or ""),
                              "reason": reason})

    raw_moves: list[tuple[Any, str, str]] = []   # (uf, kind, key)
    mint_groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"ufs": [], "flows": 0})
    for u, tgt in resolutions:
        if tgt is None:
            _stay(u, "no_strict_majority")
            continue
        kind, key = tgt
        if kind == "core":
            _stay(u, "core_group")
            continue
        if kind == "pf":
            tpf = pf_by_key.get(key)
            if tpf is None or key in transport_pf_keys:
                _stay(u, "target_unavailable")
                continue
            if str(_attr(u, "surface_scope") or "product") != \
                    str(_attr(tpf, "surface_scope") or "product"):
                _stay(u, "surface_scope_mismatch")
                continue
            taid = str(_attr(tpf, "anchor_id") or "")
            tpath = taid.split(":", 1)[-1] if ":" in taid else ""
            troot = _root_of(tpath, roots)
            eroots: Counter = Counter()
            for fid in (_attr(u, "member_flow_ids") or []):
                fl = flow_by_uuid.get(fid)
                ep = _attr(fl, "entry_point_file") if fl is not None else None
                if ep:
                    eroots[_root_of(str(ep), roots)] += 1
            eroot = eroots.most_common(1)[0][0] if eroots else None
            if troot is None or eroot is None or troot != eroot:
                _stay(u, "cross_app_target")
                continue
            raw_moves.append((u, "pf", key))
        else:  # NEW group → mint demand
            g = mint_groups[key]
            g["ufs"].append(u)
            g["flows"] += len(_attr(u, "member_flow_ids") or [])

    for cid in sorted(mint_groups):
        g = mint_groups[cid]
        if len(g["ufs"]) >= _MINT_MIN_UFS and g["flows"] >= _MINT_MIN_FLOWS:
            for u in g["ufs"]:
                raw_moves.append((u, "mint", cid))
        else:
            for u in g["ufs"]:
                _stay(u, f"below_mint_floor({len(g['ufs'])}ufs/"
                         f"{g['flows']}flows)")

    if not raw_moves:
        return tele

    # ── plan: carve + attach floor + I16 rail (fixed point) ─────────
    uf_ffiles = {id(u): _uf_flow_files(u, flow_by_uuid) for u, _t in
                 resolutions}

    def _touch_targets(planned: Mapping[int, str]) -> dict[str, set[str]]:
        touch: dict[str, set[str]] = defaultdict(set)
        for u in user_flows:
            tgt = planned.get(
                id(u), str(_attr(u, "product_feature_id") or "") or None)
            if not tgt:
                continue
            ff = uf_ffiles.get(id(u))
            if ff is None:
                ff = _uf_flow_files(u, flow_by_uuid)
                uf_ffiles[id(u)] = ff
            for p in ff:
                touch[p].add(tgt)
        return touch

    moves = list(raw_moves)
    carved_into: dict[str, set[str]] = {}
    residual_claimed: set[str] = set()
    for _round in range(len(raw_moves) + 1):
        planned = {id(u): key for u, _k, key in moves}
        touch = _touch_targets(planned)
        carved_into = defaultdict(set)
        residual_claimed = set()
        moved_targets_of: dict[str, set[str]] = defaultdict(set)
        for u, _kind, key in moves:
            for p in uf_ffiles[id(u)]:
                moved_targets_of[p].add(key)
        for u, kind, key in moves:
            for p in sorted(uf_ffiles[id(u)]):
                if p in owner_map:
                    if owner_map[p] is not None \
                            and str(owner_map[p]) == source_key \
                            and moved_targets_of[p] == {key}:
                        carved_into[key].add(p)     # source-owned follows
                elif touch.get(p) == {key}:
                    carved_into[key].add(p)         # uniquely-owned residual
                    residual_claimed.add(p)
            if kind == "mint":  # the group's own route subtree follows
                pref = key.split(":", 1)[1]
                for p, o in owner_map.items():
                    if o is not None and str(o) == source_key and (
                            p == pref or p.startswith(pref + "/")):
                        carved_into[key].add(p)

        # projected scopes (validator I15 view: pf.paths + FULL dev paths)
        planned_scope: dict[str, set[str]] = defaultdict(set)
        for pf in product_features:
            k = str(_attr(pf, "id") or _attr(pf, "name") or "")
            if k:
                planned_scope[k].update(
                    str(p) for p in (_attr(pf, "paths") or []))
        for f in devs:
            pfid = _attr(f, "product_feature_id")
            if pfid:
                planned_scope[str(pfid)].update(_full_paths(f))
        for key, files in carved_into.items():
            planned_scope[key].update(files)
            planned_scope[source_key] -= files

        # planned owner map (for the I16 rail)
        planned_owner = dict(owner_map)
        for key, files in carved_into.items():
            for p in files:
                planned_owner[p] = key

        dropped: list[tuple[Any, str, str, str, float | None]] = []
        kept: list[tuple[Any, str, str]] = []
        for u, kind, key in moves:
            eff = uf_ffiles[id(u)] - neutral_files
            attach: float | None = None
            if len(_attr(u, "member_flow_ids") or []) >= 2 and eff:
                attach = len(eff & planned_scope[key]) / len(eff)
                if attach < _ATTACH_FLOOR:
                    dropped.append((u, kind, key, "attach_floor", attach))
                    continue
            pre = _i16_flagged(u, source_key, flow_by_uuid, owner_map,
                               lane_shim)
            post = _i16_flagged(u, key, flow_by_uuid, planned_owner,
                                lane_shim)
            if post and not pre:
                dropped.append((u, kind, key, "i16_rail", attach))
                continue
            kept.append((u, kind, key))
        if not dropped:
            moves = kept
            break
        for u, kind, key, why, attach in dropped:
            row = {"uf": str(_attr(u, "id") or ""),
                   "name": str(_attr(u, "name") or ""),
                   "kind": kind, "to": key,
                   **({"attach": round(attach, 3)}
                      if attach is not None else {})}
            (tele["floor_drops"] if why == "attach_floor"
             else tele["i16_rail_drops"]).append(row)
            _stay(u, why)
        moves = kept

    # mint groups that lost their floor quorum to drops fold back; a
    # mint with no source-owned carved mass would be a PHANTOM (the
    # 6.985 contributing-dev rule) and folds back too.
    live_mint_ufs: Counter = Counter(
        key for _u, kind, key in moves if kind == "mint")
    demoted = {cid for cid, ct in live_mint_ufs.items()
               if ct < _MINT_MIN_UFS}
    for cid in live_mint_ufs:
        if cid in demoted:
            continue
        files = carved_into.get(cid) or set()
        if not any(p in owner_map and owner_map[p] is not None
                   and str(owner_map[p]) == source_key for p in files):
            demoted.add(cid)
    if demoted:
        for u, kind, key in list(moves):
            if kind == "mint" and key in demoted:
                moves.remove((u, kind, key))
                carved_into.pop(key, None)
                _stay(u, "mint_quorum_lost")
        live_mint_ufs = Counter(
            key for _u, kind, key in moves if kind == "mint")

    if not moves:
        return tele

    # orphan guard (B20/I8): the source must keep >= 1 journey.
    if len(moves) >= len(homed):
        tele["stays"].append({"uf": None, "name": None,
                              "reason": "orphan_guard_all_would_leave"})
        return tele

    # ── apply (verified plan only) ──────────────────────────────────
    from faultline.pipeline_v2.nav_taxonomy import aggregate_product_feature
    from faultline.pipeline_v2.stage_6_86_anchored_mint import _slug

    strict = _strict_conservation()
    edges_by_flow_id: dict[str, list[Any]] = defaultdict(list)
    for e in (feature_flow_edges or []):
        edges_by_flow_id[str(_attr(e, "flow_id") or "")].append(e)

    source_devs = sorted(
        (f for f in devs
         if str(_attr(f, "product_feature_id") or "") == source_key),
        key=lambda x: str(_attr(x, "name") or ""))
    dev_owned = {str(_attr(f, "name") or ""): set(_owned_of(f))
                 for f in source_devs}

    # per-target carve execution (chunks / whole-dev re-homes)
    contrib_by_target: dict[str, list[Any]] = defaultdict(list)
    rehomed_whole: dict[str, str] = {}   # dev name → target key
    for key in sorted(carved_into):
        files = carved_into[key]
        if not files:
            continue
        residual_here = sorted(p for p in files if p not in owner_map)
        for f in source_devs:
            name = str(_attr(f, "name") or "")
            if name in rehomed_whole:
                continue
            mine = sorted(files & dev_owned[name])
            if not mine:
                continue
            if len(mine) >= len(dev_owned[name]):
                # carve would EMPTY the dev → the whole dev re-homes
                # (6.985 discipline; keeps every flowful dev pathful).
                rehomed_whole[name] = key
                contrib_by_target[key].append(f)
                continue
            chunk = _carve_chunk(f, key, mine, marker=_B24_MARKER)
            _move_carved_flows(f, chunk, set(mine), edges_by_flow_id)
            _strip_carved_files(f, set(mine))
            developer_features.append(chunk)
            devs.append(chunk)
            contrib_by_target[key].append(chunk)
            tele["devs_carved"] += 1
        if residual_here:
            # residual mass rides the target's first chunk; if none, a
            # dedicated chunk minted off the largest source dev template.
            hosts = [c for c in contrib_by_target[key]
                     if str(_attr(c, "name") or "") not in rehomed_whole]
            if hosts:
                host = hosts[0]
                host.paths = sorted(set(_attr(host, "paths") or [])
                                    | set(residual_here))
            elif source_devs:
                template = max(
                    source_devs,
                    key=lambda x: (len(_attr(x, "paths") or []),
                                   str(_attr(x, "name") or "")))
                chunk = _carve_chunk(template, key, residual_here,
                                     marker=_B24_MARKER)
                developer_features.append(chunk)
                devs.append(chunk)
                contrib_by_target[key].append(chunk)
                tele["devs_carved"] += 1
            tele["residual_claimed"] += len(residual_here)

    # mints (aggregate_product_feature — the 6.985 excavator shape)
    used_slugs = set(pf_by_key) | {"platform", "shared-platform"}
    minted_key: dict[str, str] = {}
    for u, kind, key in moves:
        if kind != "mint" or key in minted_key:
            continue
        display = grain_index.display_of(key)
        slug = _slug(display) or _slug(key.rsplit(":", 1)[-1])
        if slug in used_slugs:
            n = 2
            base = slug
            while slug in used_slugs:
                slug = _slug(f"{base} {n}")
                n += 1
        used_slugs.add(slug)
        contrib = contrib_by_target.get(key) or []
        pf = aggregate_product_feature(
            name=slug,
            display_name=display,
            description=(
                f"Capability anchored at {key} "
                f"({len(contrib)} developer feature(s); "
                f"{_B24_MARKER} carve of '{source_key}')."
            ),
            contrib=contrib,
        )
        pf.layer = "product"
        pf.anchor_id = key
        pf.surface_scope = _attr(source_pf, "surface_scope")
        product_features.append(pf)
        pf_by_key[slug] = pf
        minted_key[key] = slug
        tele["pfs_minted"] += 1
        tele["mints"].append({"cid": key, "pf": slug,
                              "ufs": live_mint_ufs.get(key, 0)})

    def _final_key(kind: str, key: str) -> str:
        return minted_key[key] if kind == "mint" else key

    # stamp carved chunks / whole re-homed devs
    for key, contrib in sorted(contrib_by_target.items()):
        fkey = minted_key.get(key, key)
        for c in contrib:
            c.product_feature_id = fkey
            c.anchor_id = f"fold:{_B24_MARKER}->{key}"
            if _attr(c, "shared_reason"):
                c.shared_reason = None
    tele["devs_rehomed"] = len(rehomed_whole)

    # the source PF row sheds the carved files (its scope must not keep
    # claiming mass that now belongs to the targets — I23 body truth;
    # unlike 6.985 the source PERSISTS, so this is explicit here).
    all_carved: set[str] = set()
    for files in carved_into.values():
        all_carved |= files
    if all_carved:
        src_paths = [p for p in (_attr(source_pf, "paths") or [])
                     if str(p) not in all_carved]
        source_pf.paths = src_paths
        kept_members = []
        for m in (_attr(source_pf, "member_files") or []):
            mp = m.get("path") if isinstance(m, dict) else \
                getattr(m, "path", None)
            if mp not in all_carved:
                kept_members.append(m)
        source_pf.member_files = kept_members

    # journey re-homes
    for u, kind, key in sorted(
            moves, key=lambda m: str(_attr(m[0], "id") or "")):
        fkey = _final_key(kind, key)
        u.product_feature_id = fkey
        tele["ufs_rehomed"] += 1
        if len(tele["moves"]) < 60:
            tele["moves"].append({
                "uf": str(_attr(u, "id") or ""),
                "name": str(_attr(u, "name") or ""),
                "kind": kind, "to": fkey,
            })

    # ── hard conservation invariant (source persists) ───────────────
    violations: list[str] = []
    if len(user_flows) != uf_count_before:
        violations.append(
            f"uf_count {uf_count_before} -> {len(user_flows)}")
    after: Counter = Counter(
        str(_attr(u, "product_feature_id"))
        for u in user_flows if _attr(u, "product_feature_id"))
    for k, before in sorted(uf_home_before.items()):
        if k == source_key:
            continue
        if after.get(k, 0) < before:
            violations.append(f"pf '{k}' journeys {before} -> "
                              f"{after.get(k, 0)}")
    if after.get(source_key, 0) < 1:
        violations.append(f"source '{source_key}' stripped to zero journeys")
    if violations:
        tele["conservation_violations"] = violations
        if strict:
            raise AssertionError(
                "mega_pf_nav_rehome conservation violated: "
                + "; ".join(violations))
    return tele
