"""B22 — Stage 6.985 transport-lane journey-conservation handoff.

Ratified anti-cases (fixb22-design §8 + Phase-2 brief):
  1. Conservation gate refuses the WHOLE lane on ONE unresolved UF
     (zero-product-vote class) — no journey moved, PF stays, telemetry.
  2. Strict rung (r1) re-homes on a >50% span-mass majority.
  3. Plurality rung (r3) re-homes with a per-UF telemetry marker; a
     50/50 tie NEVER re-homes; the sub-flag kills the rung.
  4. Atomic target grain: the vote's NEW-target grain == the minted
     PF's anchor (ONE oracle, design risk #1).
  5. Flag=0 inert + candidates=∅ inert.
  6. UF-count / journey-conservation invariant (raises under pytest).
  7. Synthesized member_count=0 UFs re-home by route-URL only.
  8. Hub cutoff is scale-invariant and monotone.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.transport_handoff import (
    TRANSPORT_HANDOFF_ENV,
    TRANSPORT_HANDOFF_PLURALITY_ENV,
    GrainTarget,
    TargetGrainIndex,
    _conservation_violations,
    hub_cutoff,
    run_transport_handoff,
    transport_handoff_enabled,
    transport_plurality_enabled,
)

UNIT = "packages/rpc"


# ── scene stubs ──────────────────────────────────────────────────────────


class Dev:
    def __init__(self, name, pfid, paths, flows=()):
        self.name = name
        self.uuid = f"dev-{name}"
        self.layer = "developer"
        self.product_feature_id = pfid
        self.paths = list(paths)
        self.member_files = []
        self.flows = list(flows)
        self.shared_reason = None
        self.anchor_id = None
        # aggregate_product_feature reads these:
        from datetime import datetime, timezone
        self.authors = []
        self.total_commits = 0
        self.bug_fixes = 0
        self.coverage_pct = None
        self.last_modified = datetime.fromtimestamp(0, timezone.utc)
        self.health_score = 0.0


class PF:
    def __init__(self, name, anchor_id):
        self.name = name
        self.uuid = f"pf-{name}"
        self.layer = "product"
        self.anchor_id = anchor_id


class Fl:
    def __init__(self, uuid, ranges, ep=None, paths=None):
        self.uuid = uuid
        self.entry_point_file = ep
        self.line_ranges = [
            {"path": p, "start_line": 1, "end_line": n} for p, n in ranges
        ]
        # the validator's I15 flow-file surface (attach-floor mirror);
        # defaults to the span files, exactly like real flows.
        self.paths = list(paths) if paths is not None else \
            [p for p, _n in ranges]


class UF:
    def __init__(self, id, name, pfid, members=(), routes=()):
        self.id = id
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.routes = list(routes)


class Ctx:
    repo_path = "."
    tracked_files = []


class NoConsumers:
    """r2 stub: nothing resolves through consumers."""

    cutoff = 10

    def importers_of(self, path):
        return frozenset()

    def unit_file_consumers(self, path):
        return frozenset()


def _anchors():
    return [
        SpineAnchor(canonical_id="route:app/routes/billing", key="billing",
                    source="route", display="Billing",
                    prefixes=("app/routes/billing",),
                    sources=frozenset({"route"})),
        SpineAnchor(canonical_id="route:app/routes/admin", key="admin",
                    source="route", display="Admin",
                    prefixes=("app/routes/admin",),
                    sources=frozenset({"route"})),
        # A shell anchor over everything — must never answer.
        SpineAnchor(canonical_id="ws:app", key="app", source="ws-app",
                    display="App", prefixes=("app",),
                    sources=frozenset({"ws-app"})),
        # The candidate's own ws anchor — excluded.
        SpineAnchor(canonical_id=f"ws:{UNIT}", key="rpc", source="ws-pkg",
                    display="Rpc", prefixes=(UNIT,),
                    sources=frozenset({"ws-pkg"})),
    ]


#: routes_index — establishes the routes ROOT (app/routes) the grain's
#: route-GROUP channel derives NEW targets from ("embed" is unminted).
ROUTES = [
    {"file": "app/routes/billing/index.tsx", "pattern": "/billing"},
    {"file": "app/routes/admin/index.tsx", "pattern": "/admin"},
    {"file": "app/routes/embed/page.tsx", "pattern": "/embed"},
    {"file": "app/routes/embed/sign.tsx", "pattern": "/embed/:id"},
]


def _scene(extra_ufs=(), extra_flows=(), extra_devs=()):
    """Candidate PF 'rpc' (ws:packages/rpc) + product PFs billing/admin +
    an unminted embed route group. Two journeys resolve (strict via
    billing ownership; new-target via the embed route grain)."""
    pfs = [PF("rpc", f"ws:{UNIT}"), PF("billing", "route:app/routes/billing"),
           PF("admin", "route:app/routes/admin")]
    devs = [
        Dev("rpc-router", "rpc", [f"{UNIT}/server/router.ts"]),
        # annexed product dev: files under the embed route group.
        Dev("rpc-embed", "rpc", ["app/routes/embed/page.tsx",
                                 "app/routes/embed/sign.tsx"]),
        Dev("billing", "billing", ["app/routes/billing/index.tsx"]),
        Dev("admin", "admin", ["app/routes/admin/index.tsx"]),
    ] + list(extra_devs)
    flows = [
        Fl("f-bill", [("app/routes/billing/index.tsx", 60)],
           ep="app/routes/billing/index.tsx"),
        Fl("f-embed", [("app/routes/embed/page.tsx", 80)],
           ep="app/routes/embed/page.tsx"),
    ] + list(extra_flows)
    ufs = [
        UF("UF-001", "Manage billing", "rpc", members=["f-bill"]),
        UF("UF-002", "Embed documents", "rpc", members=["f-embed"]),
    ] + list(extra_ufs)
    grain = TargetGrainIndex(
        _anchors(), pfs, routes_index=ROUTES, excluded_units=[UNIT],
        candidate_pf_keys={"rpc"})
    return devs, pfs, ufs, flows, grain


def _run(devs, pfs, ufs, flows, grain, candidates=None, routes_index=None):
    return run_transport_handoff(
        devs, pfs, ufs, flows, routes_index or ROUTES, Ctx(),
        candidates if candidates is not None
        else {UNIT: "S2-transport:name-dep-fanout"},
        grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers(),
    )


# ── 5. flags / inertness ─────────────────────────────────────────────────


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv(TRANSPORT_HANDOFF_ENV, raising=False)
    assert transport_handoff_enabled() is True


def test_flag_zero_off(monkeypatch):
    monkeypatch.setenv(TRANSPORT_HANDOFF_ENV, "0")
    assert transport_handoff_enabled() is False


def test_plurality_flag(monkeypatch):
    monkeypatch.delenv(TRANSPORT_HANDOFF_PLURALITY_ENV, raising=False)
    assert transport_plurality_enabled() is True
    monkeypatch.setenv(TRANSPORT_HANDOFF_PLURALITY_ENV, "0")
    assert transport_plurality_enabled() is False


def test_no_candidates_inert():
    devs, pfs, ufs, flows, grain = _scene()
    tele = _run(devs, pfs, ufs, flows, grain, candidates={})
    assert tele["laned"] == [] and tele["ufs_rehomed"] == 0
    assert [u.product_feature_id for u in ufs] == ["rpc", "rpc"]
    assert len(pfs) == 3


# ── 2. strict rung + 4. atomic grain (vote == mint) ─────────────────────


def test_strict_rung_and_atomic_mint_grain():
    devs, pfs, ufs, flows, grain = _scene()
    tele = _run(devs, pfs, ufs, flows, grain)

    # gate PASSed: candidate laned, journeys conserved 2→2 on new homes.
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    assert tele["ufs_rehomed"] == 2
    assert ufs[0].product_feature_id == "billing"          # r1 strict
    rungs = tele["rungs"][UNIT]
    assert rungs.get("r1-strict") == 2

    # ATOMIC GRAIN: the vote's NEW target == the minted PF's anchor.
    minted = [pf for pf in pfs if getattr(pf, "anchor_id", None)
              == "route:app/routes/embed"]
    assert len(minted) == 1
    assert ufs[1].product_feature_id == minted[0].name
    assert tele["laned"][0]["minted"] == {
        "route:app/routes/embed": minted[0].name}

    # the annexed dev moved WITH its journey; the router dev laned.
    dev_by_name = {d.name: d for d in devs}
    assert dev_by_name["rpc-embed"].product_feature_id == minted[0].name
    assert dev_by_name["rpc-router"].product_feature_id is None
    assert dev_by_name["rpc-router"].shared_reason == "technology_instrument"

    # the candidate PF left the product layer.
    assert all(pf.name != "rpc" for pf in pfs)


def test_grain_index_is_single_oracle():
    """grain_of_file answers drive BOTH the vote and the mint: the
    embed file's grain is the 'new' target the mint used above."""
    _devs, pfs, _ufs, _flows, grain = _scene()
    t = grain.grain_of_file("app/routes/embed/page.tsx")
    assert t == GrainTarget("new", "route:app/routes/embed",
                            display="Embed")
    # owned-by-PF anchors answer the PF; shells never answer.
    assert grain.grain_of_file("app/routes/billing/index.tsx") == \
        GrainTarget("pf", "billing", display="Billing")
    assert grain.grain_of_file("app/other/loose.tsx") is None
    # candidate-unit files never grain (lane is never a target).
    assert grain.grain_of_file(f"{UNIT}/server/router.ts") is None


# ── 1. conservation gate refusal ─────────────────────────────────────────


def test_gate_refuses_whole_lane_on_one_unresolved_uf():
    orphan_flow = Fl("f-orphan", [("packages/other/util.ts", 40)])
    orphan = UF("UF-003", "Browse GitHub metrics", "rpc",
                members=["f-orphan"])
    devs, pfs, ufs, flows, grain = _scene(
        extra_ufs=[orphan], extra_flows=[orphan_flow])
    tele = _run(devs, pfs, ufs, flows, grain)

    # ONE zero-vote journey blocks the WHOLE candidate: nothing moves.
    assert tele["laned"] == []
    assert tele["ufs_rehomed"] == 0
    blocked = tele["conservation_blocked"][UNIT]
    assert blocked["pf"] == "rpc" and blocked["ufs_homed"] == 3
    reasons = {b["uf"]: b["reason"] for b in blocked["blocked"]}
    assert reasons == {"UF-003": "zero_product_votes"}
    # exact flag-OFF state: homes, PF list and devs untouched.
    assert [u.product_feature_id for u in ufs] == ["rpc", "rpc", "rpc"]
    assert any(pf.name == "rpc" for pf in pfs)
    assert {d.name: d.product_feature_id for d in devs}["rpc-embed"] == "rpc"


# ── 3. plurality rung ────────────────────────────────────────────────────


def _plurality_scene(masses):
    """A third journey whose span mass splits per ``masses`` =
    [(path, mass)] — no strict majority anywhere."""
    fl = Fl("f-mix", [(p, m) for p, m in masses],
            ep=masses[0][0])
    uf = UF("UF-004", "View audit log", "rpc", members=["f-mix"])
    return _scene(extra_ufs=[uf], extra_flows=[fl])


def test_plurality_rehomes_with_marker():
    # billing 30 / admin 20 / embed 20 → top 30/70 ≤ 0.5, top1 > top2.
    devs, pfs, ufs, flows, grain = _plurality_scene([
        ("app/routes/billing/index.tsx", 30),
        ("app/routes/admin/index.tsx", 20),
        ("app/routes/embed/page.tsx", 20)])
    tele = _run(devs, pfs, ufs, flows, grain)
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    uf = next(u for u in ufs if u.id == "UF-004")
    assert uf.product_feature_id == "billing"
    assert tele["rungs"][UNIT].get("r3-plurality") == 1
    move = next(m for m in tele["moves"] if m["uf"] == "UF-004")
    assert move["rung"] == "r3-plurality"   # the per-UF telemetry marker


def test_5050_tie_never_rehomes():
    # billing 30 / admin 30 → exactly 50%: strict fails AND plurality
    # refuses (top1 must STRICTLY beat top2) → gate blocks the lane.
    devs, pfs, ufs, flows, grain = _plurality_scene([
        ("app/routes/billing/index.tsx", 30),
        ("app/routes/admin/index.tsx", 30)])
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    reasons = {b["uf"]: b["reason"]
               for b in tele["conservation_blocked"][UNIT]["blocked"]}
    assert reasons == {"UF-004": "split"}
    assert next(u for u in ufs if u.id == "UF-004").product_feature_id \
        == "rpc"


def test_plurality_subflag_off_blocks(monkeypatch):
    monkeypatch.setenv(TRANSPORT_HANDOFF_PLURALITY_ENV, "0")
    devs, pfs, ufs, flows, grain = _plurality_scene([
        ("app/routes/billing/index.tsx", 30),
        ("app/routes/admin/index.tsx", 20),
        ("app/routes/embed/page.tsx", 20)])
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    reasons = {b["uf"]: b["reason"]
               for b in tele["conservation_blocked"][UNIT]["blocked"]}
    assert reasons == {"UF-004": "split"}


def _rail_scene(ep1, ep2):
    """A genuinely distributed 2-member journey with attach ≥ 0.34 at
    the billing plurality target (2 of 5 flow files inside billing
    scope) so the RAIL, not the attach floor, decides."""
    fl = Fl("f-mix", [("app/routes/billing/index.tsx", 30),
                      ("app/routes/billing/extra.tsx", 10),
                      ("app/routes/admin/index.tsx", 30),
                      ("app/routes/embed/page.tsx", 20)],
            ep=ep1)
    fl2 = Fl("f-mix2", [], ep=ep2, paths=[])
    uf = UF("UF-004", "View audit log", "rpc", members=["f-mix", "f-mix2"])
    return _scene(
        extra_ufs=[uf], extra_flows=[fl, fl2],
        extra_devs=[Dev("billing2", "billing",
                        ["app/routes/billing/extra.tsx"])])


def test_plurality_i16_rail_refuses():
    # Plurality target = billing (40/90 top, no strict), but the
    # journey's member ENTRIES sit on the candidate's OWN annexed files
    # (pre-state: owned by the dissolving home → I16-clean today) which
    # the plan re-homes to the embed target → post-move the entries are
    # majority-foreign to billing → a NEW I16 row → rail refuses → gate
    # blocks (measured rail, not vibes; a PRE-flagged journey would be
    # exempt — only the clean→flagged transition counts).
    devs, pfs, ufs, flows, grain = _rail_scene(
        "app/routes/embed/page.tsx", "app/routes/embed/sign.tsx")
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    reasons = {b["uf"]: b["reason"]
               for b in tele["conservation_blocked"][UNIT]["blocked"]}
    assert reasons == {"UF-004": "plurality_i16_rail"}


def test_plurality_preflagged_journey_exempt_from_rail():
    # The SAME distributed journey but its entries are ALREADY foreign
    # today (owned by admin while homed to rpc → pre-flagged I16 row):
    # moving it cannot CREATE a row, so the rail allows the plurality
    # re-home (ratified wording: zero NEW I16 rows).
    devs, pfs, ufs, flows, grain = _rail_scene(
        "app/routes/admin/index.tsx", "app/routes/admin/index.tsx")
    tele = _run(devs, pfs, ufs, flows, grain)
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    assert next(u for u in ufs if u.id == "UF-004").product_feature_id \
        == "billing"
    assert tele["rungs"][UNIT].get("r3-plurality") == 1


# ── 7. synthesized route-only UFs ────────────────────────────────────────


def test_synthesized_uf_rehomes_by_route_url():
    synth = UF("UF-005", "Uncovered: rpc routes", "rpc",
               routes=["/embed/:id", "/embed"])
    devs, pfs, ufs, flows, grain = _scene(extra_ufs=[synth])
    routes_index = [
        {"file": "app/routes/embed/page.tsx", "pattern": "/embed"},
        {"file": "app/routes/embed/sign.tsx", "pattern": "/embed/:id"},
        {"file": "app/routes/billing/index.tsx", "pattern": "/billing"},
    ]
    tele = _run(devs, pfs, ufs, flows, grain, routes_index=routes_index)
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    minted = next(pf for pf in pfs
                  if getattr(pf, "anchor_id", None)
                  == "route:app/routes/embed")
    uf = next(u for u in ufs if u.id == "UF-005")
    assert uf.product_feature_id == minted.name
    assert tele["rungs"][UNIT].get("route-url") == 1


def test_synthesized_uf_without_matching_route_blocks():
    synth = UF("UF-005", "Uncovered: rpc routes", "rpc",
               routes=["/nowhere"])
    devs, pfs, ufs, flows, grain = _scene(extra_ufs=[synth])
    tele = _run(devs, pfs, ufs, flows, grain, routes_index=[
        {"file": "app/routes/billing/index.tsx", "pattern": "/billing"}])
    assert tele["laned"] == []
    reasons = {b["uf"]: b["reason"]
               for b in tele["conservation_blocked"][UNIT]["blocked"]}
    assert reasons["UF-005"] in {"zero_product_votes", "split"}


# ── new-target-without-devs gate leg ─────────────────────────────────────


def test_new_target_without_contributing_dev_blocks():
    """A journey demands a NEW grain no dev re-homes to → minting it
    would be a phantom PF → the gate refuses the lane."""
    devs, pfs, ufs, flows, grain = _scene()
    # strip the annexed dev: the embed grain keeps UF demand, loses devs.
    devs = [d for d in devs if d.name != "rpc-embed"]
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    blocked = tele["conservation_blocked"][UNIT]["blocked"]
    assert any(b["reason"] == "new_target_without_devs" for b in blocked)
    assert [u.product_feature_id for u in ufs] == ["rpc", "rpc"]


# ── 6. conservation invariant ────────────────────────────────────────────


def test_conservation_invariant_detects_loss():
    ufs = [UF("UF-001", "a", "billing")]
    v = _conservation_violations(2, {"billing": 1, "admin": 1}, ufs, set())
    assert any("uf_count 2 -> 1" in x for x in v)
    assert any("pf 'admin' journeys 1 -> 0" in x for x in v)


def test_conservation_invariant_flags_dangling_laned_ref():
    ufs = [UF("UF-001", "a", "rpc")]
    v = _conservation_violations(1, {"rpc": 1}, ufs, {"rpc"})
    assert any("still on laned 'rpc'" in x for x in v)


def test_happy_path_conserves_counts():
    devs, pfs, ufs, flows, grain = _scene()
    before = len(ufs)
    tele = _run(devs, pfs, ufs, flows, grain)
    assert len(ufs) == before
    assert "conservation_violations" not in tele
    # every journey still has a home and none points at the laned PF.
    assert all(u.product_feature_id not in (None, "rpc") for u in ufs)


# ── 8. hub cutoff — scale-invariant + monotone ───────────────────────────


def test_hub_cutoff_floor_and_scale():
    assert hub_cutoff(0) == 10
    assert hub_cutoff(500) == 10          # floor
    assert hub_cutoff(2600) == 26         # ≈ the calibrated documenso 25


def test_hub_cutoff_monotone():
    vals = [hub_cutoff(n) for n in range(0, 20001, 250)]
    assert vals == sorted(vals)


# ── PHASE-2 REWORK (2026-07-10): attach floor + flowful-dev guard ────────


def test_attach_floor_blocks_thin_rehome():
    """A strict span-mass majority whose target the journey's own flow
    files barely touch (attach < 0.34, the validator's I15 ruler) is
    UNRESOLVED → gate refuses (keyed A/B exhibit: 'Copy document
    recipient link' cov 0.005 → fresh I15+I16 rows)."""
    f1 = Fl("f-thin1", [("app/routes/billing/index.tsx", 60)],
            ep="app/routes/billing/index.tsx")
    f2 = Fl("f-thin2", [("packages/other/a.ts", 20),
                        ("packages/other/b.ts", 20)])
    uf = UF("UF-006", "Thin journey", "rpc", members=["f-thin1", "f-thin2"])
    devs, pfs, ufs, flows, grain = _scene(extra_ufs=[uf],
                                          extra_flows=[f1, f2])
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    blocked = {b["uf"]: b for b in
               tele["conservation_blocked"][UNIT]["blocked"]}
    assert blocked["UF-006"]["reason"] == "attach_floor"
    assert blocked["UF-006"]["attach"] == 0.333  # 1/3 < 0.34
    # exact flag-OFF state.
    assert next(u for u in ufs if u.id == "UF-006").product_feature_id \
        == "rpc"
    assert any(pf.name == "rpc" for pf in pfs)


def test_attach_floor_single_flow_exempt():
    """The validator's I15 gate only fires on UFs with >=2 member flows
    — the floor mirrors that carve exactly (no over-blocking)."""
    f1 = Fl("f-solo", [("app/routes/billing/index.tsx", 60),
                       ("packages/other/a.ts", 20),
                       ("packages/other/b.ts", 20)],
            ep="app/routes/billing/index.tsx")
    uf = UF("UF-006", "Single-flow journey", "rpc", members=["f-solo"])
    devs, pfs, ufs, flows, grain = _scene(extra_ufs=[uf],
                                          extra_flows=[f1])
    tele = _run(devs, pfs, ufs, flows, grain)
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    assert next(u for u in ufs if u.id == "UF-006").product_feature_id \
        == "billing"


def test_i16_rail_guards_every_rung():
    """Rework: the zero-NEW-I16-rows rail applies to r1/r2 too (the
    keyed A/B showed an r2 re-home minting a fresh row). A STRICT
    re-home whose entries end majority-foreign post-move is refused."""
    f1 = Fl("f-mix", [("app/routes/billing/index.tsx", 50),
                      ("app/routes/billing/extra.tsx", 10),
                      ("app/routes/admin/index.tsx", 20),
                      ("app/routes/embed/page.tsx", 20)],
            ep="app/routes/embed/page.tsx")
    f2 = Fl("f-mix2", [], ep="app/routes/embed/sign.tsx", paths=[])
    uf = UF("UF-006", "Strict but foreign-entry", "rpc",
            members=["f-mix", "f-mix2"])
    devs, pfs, ufs, flows, grain = _scene(
        extra_ufs=[uf], extra_flows=[f1, f2],
        extra_devs=[Dev("billing2", "billing",
                        ["app/routes/billing/extra.tsx"])])
    # billing 60/100 non-lane mass = r1-strict; attach 2/5 = 0.4 ok;
    # entries (embed x2) re-owned to the embed target => 2/2 foreign
    # to billing => NEW I16 row => refused with the generic rail tag.
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    reasons = {b["uf"]: b["reason"]
               for b in tele["conservation_blocked"][UNIT]["blocked"]}
    assert reasons == {"UF-006": "i16_rail"}


def test_flowful_dev_never_lanes():
    """Rework (validator I9): a dev with attached flows must never land
    in the platform lane — it re-homes with a resolved journey or the
    candidate is refused."""
    router_flow = Fl("f-router", [(f"{UNIT}/server/router.ts", 30)],
                     ep=f"{UNIT}/server/router.ts")
    devs, pfs, ufs, flows, grain = _scene(extra_flows=[router_flow])
    dev_by_name = {d.name: d for d in devs}
    dev_by_name["rpc-router"].flows = [router_flow]  # flowful router dev
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["laned"] == []
    blocked = tele["conservation_blocked"][UNIT]["blocked"]
    stranded = next(b for b in blocked
                    if b["reason"] == "flowful_dev_would_lane")
    assert ["rpc-router", 0] in stranded["top2"]
    # I9-shape structural assert: NO flowful dev sits in the lane.
    for d in devs:
        if d.flows:
            assert not (d.product_feature_id is None
                        and d.shared_reason == "technology_instrument")


def test_flowless_router_dev_still_lanes():
    """The guard is flow-keyed, not name-keyed: the flowless router dev
    lanes exactly as before."""
    devs, pfs, ufs, flows, grain = _scene()
    tele = _run(devs, pfs, ufs, flows, grain)
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    d = next(x for x in devs if x.name == "rpc-router")
    assert d.product_feature_id is None
    assert d.shared_reason == "technology_instrument"
