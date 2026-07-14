"""B52 — flow-bearing transport lane (Option A; operator 'трпц A').

The class (B51 mandate-STOP, operator decision 2026-07-13): cal.com
`trpc` is a flow-bearing transport monolith whose residue can NEVER
fully drain (47/66 flows are `api/trpc/*/[trpc].ts` handler flows; ~25
domains have no product surface), so the B51 all-or-nothing decomp is
inert and I9 honestly holds the tile. Option A: the tile leaves the
product layer ANYWAY — matched groups re-home WITH their journeys,
the flowful residue lanes (the validator I9 ws:-anchor exemption,
engine-aligned), and transport-intrinsic journeys stay in user_flows[]
as lane-row references (pfid=None + lane_ref + surface_scope).

Anti-cases (spec §SACRED):
  * flag OFF ⇒ everything byte-identical (B51 all-or-nothing law holds).
  * flowless candidate (documenso keyless trpc) ⇒ laned row telemetry
    byte-identical to the legacy path (no lane_journeys/lane_flows keys).
  * non-ws-anchored candidate ⇒ no-op (cand_pf requires ws:<unit>).
  * a non-transport flowful dev is untouched (the exemption is scoped
    to transport candidates only).
  * mint FORBIDDEN: a "new"-grain journey target demotes to the lane.
  * receiver I8 backstop: a receiver PF that would end journey-less
    pulls its carve back (flows return, chunk vanishes).
  * conservation: ΣUF and Σflows identical (moved or laned, never
    dropped); lane_ref anchors an emitted lane row uuid.
"""

from __future__ import annotations

from faultline.pipeline_v2.transport_handoff import (
    FLOWFUL_TRANSPORT_LANE_ENV,
    TRANSPORT_ROUTER_DECOMP_ENV,
    TargetGrainIndex,
    _flow_subrouter_disc,
    _handler_ns_tokens,
    flowful_transport_lane_enabled,
    run_transport_handoff,
)

UNIT = "packages/trpc"


# ── scene stubs (mirror test_b51_router_decomp) ─────────────────────────


class Dev:
    def __init__(self, name, pfid, paths, flows=()):
        from datetime import datetime, timezone
        self.name = name
        self.uuid = f"dev-{name}"
        self.layer = "developer"
        self.product_feature_id = pfid
        self.paths = list(paths)
        self.member_files = []
        self.flows = list(flows)
        self.shared_reason = None
        self.anchor_id = None
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
        self.paths = []


class Fl:
    def __init__(self, uuid, ranges, ep=None, name=None):
        self.uuid = uuid
        self.name = name or uuid
        self.id = uuid
        self.primary_feature = "trpc"
        self.entry_point_file = ep
        self.line_ranges = [
            {"path": p, "start_line": 1, "end_line": n} for p, n in ranges
        ]
        self.paths = [p for p, _n in ranges]


class UF:
    def __init__(self, id, name, pfid, members=()):
        self.id = id
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.routes = []
        self.lane_ref = None
        self.surface_scope = None


class Ctx:
    repo_path = "."
    tracked_files = []


class NoConsumers:
    cutoff = 10

    def importers_of(self, path):
        return frozenset()

    def unit_file_consumers(self, path):
        return frozenset()


_DEFAULT_PFS = [
    ("api-keys", "route:app/routes/settings/api-keys"),
    ("event-types", "route:app/routes/event-types"),
    ("webhooks", "route:app/routes/settings/webhooks"),
]


def _rf(domain, n=80):
    return f"{UNIT}/server/routers/viewer/{domain}/{domain}.ts"


def _handler(domain):
    return f"apps/web/pages/api/trpc/{domain}/[trpc].ts"


def _scene(dev_flows, ufs, product_pfs=None, extra_pfs=(), extra_paths=(),
           routes_index=None):
    pfs = [PF("trpc", f"ws:{UNIT}")]
    pfs += [PF(n, a) for n, a in (product_pfs or _DEFAULT_PFS)]
    pfs += list(extra_pfs)
    paths = set(extra_paths)
    for fl in dev_flows:
        paths.update(fl.paths)
    trpc_dev = Dev("trpc", "trpc", sorted(paths), flows=list(dev_flows))
    devs = [trpc_dev]
    grain = TargetGrainIndex(
        [], pfs, routes_index=routes_index or [], excluded_units=[UNIT],
        candidate_pf_keys={"trpc"})
    return devs, pfs, list(ufs), list(dev_flows), grain, trpc_dev


def _run(devs, pfs, ufs, flows, grain):
    return run_transport_handoff(
        devs, pfs, ufs, flows, [], Ctx(),
        {UNIT: "S1-transport:name-dep"},
        grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers(),
    )


def _total_flows(devs):
    return sum(len(getattr(d, "flows", []) or []) for d in devs)


# ── flag ──────────────────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch):
    # B62 flip: default ON (KEY_SCHEMA 29). Unset ⇒ enabled; X=0 disables.
    monkeypatch.delenv(FLOWFUL_TRANSPORT_LANE_ENV, raising=False)
    assert flowful_transport_lane_enabled() is True
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "0")
    assert flowful_transport_lane_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    assert flowful_transport_lane_enabled() is True


# ── (c)-grain: api/trpc/<domain>/ handler channel ────────────────────────


def test_handler_tokens_shape():
    assert _handler_ns_tokens(_handler("apiKeys")) == ["apiKeys"]
    # no api/trpc/<domain>/ shape → no tokens
    assert _handler_ns_tokens(_rf("apiKeys")) == []
    assert _handler_ns_tokens("apps/web/pages/api/other/x.ts") == []


def test_handler_grain_off_is_b51_law():
    # Default (B51-only) law: a handler flow is residue immediately.
    fl = Fl("f", [(_handler("apiKeys"), 1)], ep=_handler("apiKeys"))
    assert _flow_subrouter_disc(fl) is None


def test_handler_grain_on_discriminates():
    fl = Fl("f", [(_handler("apiKeys"), 1)], ep=_handler("apiKeys"))
    assert _flow_subrouter_disc(fl, handler_grain=True) == "apiKeys"


def test_handler_grain_transparent_domain_is_residue():
    # `viewer` is a transparent grouping token in the handler grain too.
    fl = Fl("f", [(_handler("viewer"), 1)], ep=_handler("viewer"))
    assert _flow_subrouter_disc(fl, handler_grain=True) is None


def test_routers_files_keep_priority_over_handler():
    fl = Fl("f", [(_rf("apiKeys"), 80), (_handler("webhooks"), 1)],
            ep=_handler("webhooks"))
    # routers chain wins; the handler file contributes no token.
    assert _flow_subrouter_disc(fl, handler_grain=True) == "apiKeys"


# ── drain-then-lane: the cal.com shape ───────────────────────────────────


def _cal_shape(monkeypatch):
    """A matched routers group (apiKeys), a matched HANDLER group
    (webhooks — the (c)-grain), and a transport-intrinsic handler flow
    (admin — no product surface)."""
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    monkeypatch.delenv(TRANSPORT_ROUTER_DECOMP_ENV, raising=False)
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fw = Fl("f-webhooks", [(_handler("webhooks"), 40)],
            ep=_handler("webhooks"), name="manage-webhooks-by-api")
    fh = Fl("f-admin", [(_handler("admin"), 200)], ep=_handler("admin"),
            name="manage-admin-by-trpc")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    uw = UF("UF-2", "Manage webhooks", "trpc", members=["f-webhooks"])
    uh = UF("UF-H", "Admin Users Management", "trpc", members=["f-admin"])
    return _scene([fa, fw, fh], [ua, uw, uh])


def test_drain_then_lane_tile_disappears(monkeypatch):
    devs, pfs, ufs, flows, grain, dev = _cal_shape(monkeypatch)
    tele = _run(devs, pfs, ufs, flows, grain)

    # THE MANDATE: trpc leaves product_features[] entirely.
    assert all(pf.name != "trpc" for pf in pfs)
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    # matched journeys re-homed WITH their flows (routers + (c) grain).
    assert {u.id: u.product_feature_id for u in ufs if u.lane_ref is None} \
        == {"UF-1": "api-keys", "UF-2": "webhooks"}
    # the transport-intrinsic journey rides the lane, never dropped.
    uh = next(u for u in ufs if u.id == "UF-H")
    assert uh.product_feature_id is None
    assert uh.lane_ref == dev.uuid
    assert uh.surface_scope == "platform_infrastructure"
    # the flowful residue LANES (Seg1 ws-anchor carve-out) and carries
    # the B52 provenance anchor (the lane builder keys flow_ids on it).
    assert dev.product_feature_id is None
    assert [fl.uuid for fl in dev.flows] == ["f-admin"]
    from faultline.pipeline_v2.transport_handoff import FLOWFUL_LANE_ANCHOR
    assert dev.anchor_id == FLOWFUL_LANE_ANCHOR
    # telemetry: lane row content + decomp extension.
    row = tele["laned"][0]
    assert row["lane_journeys"] == 1 and row["lane_flows"] == 1
    rd = tele["router_decomp"][UNIT]
    assert rd["journeys_moved"] == 2
    assert rd["lane_journeys"] == 1 and rd["lane_flow_ids"] == 1
    assert tele["pfs_minted"] == 0


def test_conservation_sigma_flows_and_ufs(monkeypatch):
    devs, pfs, ufs, flows, grain, dev = _cal_shape(monkeypatch)
    ufs_before = len(ufs)
    flows_before = _total_flows(devs)
    _run(devs, pfs, ufs, flows, grain)

    assert len(ufs) == ufs_before                       # ΣUF conserved
    assert _total_flows(devs) == flows_before           # Σflows conserved
    # split: product-homed chunks + lane residue == total.
    chunk_flows = sum(
        len(d.flows) for d in devs
        if d.product_feature_id not in (None, "trpc"))
    lane_flows = sum(
        len(d.flows) for d in devs if d.product_feature_id is None)
    assert chunk_flows + lane_flows == flows_before
    # lineage: moved flows keep their uuids.
    all_uuids = {fl.uuid for d in devs for fl in d.flows}
    assert all_uuids == {"f-apikeys", "f-webhooks", "f-admin"}


def test_flag_off_b51_law_byte_identical(monkeypatch):
    """OFF ⇒ the B51 all-or-nothing law holds verbatim: the cal shape
    abstains (residue>0), trpc keeps its whole tile."""
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "0")  # default ON post-B62
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fh = Fl("f-admin", [(_handler("admin"), 200)], ep=_handler("admin"),
            name="manage-admin")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    uh = UF("UF-H", "Admin", "trpc", members=["f-admin"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa, fh], [ua, uh])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert "router_decomp" not in tele
    assert any(pf.name == "trpc" for pf in pfs)
    assert ufs[0].product_feature_id == "trpc"
    assert ufs[0].lane_ref is None and ufs[1].lane_ref is None


# ── receiver I8 backstop ─────────────────────────────────────────────────


def test_receiver_without_journey_pulls_back(monkeypatch):
    """A matched group whose receiver would end flowful-but-journey-less
    (no existing refs, the group's journey does NOT follow — its majority
    mass is residue) pulls its carve back: the receiver stays exactly as
    OFF, the flows ride the lane with their journey."""
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    extra = [PF("credits", "route:app/routes/credits")]
    f_cr = Fl("f-credits", [(_rf("credits"), 10)], ep=_rf("credits"),
              name="manage-credits")
    f_h = Fl("f-admin", [(_handler("admin"), 200)], ep=_handler("admin"),
             name="manage-admin")
    f_s = Fl("f-sso", [(_handler("saml"), 150)], ep=_handler("saml"),
             name="manage-saml")
    # ONE journey spans all three flows — its attach at `credits` is
    # 1/3 < 0.34 (the I15 floor), so it does NOT follow the carve.
    u = UF("UF-X", "Credits and admin", "trpc",
           members=["f-credits", "f-admin", "f-sso"])
    devs, pfs, ufs, flows, grain, dev = _scene(
        [f_cr, f_h, f_s], [u], extra_pfs=extra)
    tele = _run(devs, pfs, ufs, flows, grain)

    # pullback: no chunk, all flows back on the (now laned) dev.
    assert not any("router-decomp" in str(d.name) for d in devs)
    assert {fl.uuid for fl in dev.flows} == {"f-credits", "f-admin", "f-sso"}
    assert dev.product_feature_id is None          # residue lanes
    # credits PF untouched (still journey-less and flow-less — as OFF).
    assert any(pf.name == "credits" for pf in pfs)
    # the journey rides the lane.
    assert ufs[0].lane_ref == dev.uuid
    assert ufs[0].product_feature_id is None
    # telemetry recorded the pullback.
    rd = tele["router_decomp"][UNIT]
    assert rd["pulled_back"] == ["credits"]
    assert "credits" not in rd["matched"]


def test_receiver_with_existing_journey_keeps_carve(monkeypatch):
    """Same scene, but the receiver ALREADY has a journey → the carve
    stands (no I8 risk), the spanning journey still lanes (its majority
    is residue)."""
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    extra = [PF("credits", "route:app/routes/credits")]
    f_cr = Fl("f-credits", [(_rf("credits"), 10)], ep=_rf("credits"),
              name="manage-credits")
    f_h = Fl("f-admin", [(_handler("admin"), 200)], ep=_handler("admin"),
             name="manage-admin")
    f_s = Fl("f-sso", [(_handler("saml"), 150)], ep=_handler("saml"),
             name="manage-saml")
    u = UF("UF-X", "Credits and admin", "trpc",
           members=["f-credits", "f-admin", "f-sso"])
    u_ex = UF("UF-E", "Buy credits", "credits", members=[])
    devs, pfs, ufs, flows, grain, dev = _scene(
        [f_cr, f_h, f_s], [u, u_ex], extra_pfs=extra)
    tele = _run(devs, pfs, ufs, flows, grain)

    chunk = next(d for d in devs if d.product_feature_id == "credits"
                 and d is not dev)
    assert [fl.uuid for fl in chunk.flows] == ["f-credits"]
    rd = tele["router_decomp"][UNIT]
    assert "pulled_back" not in rd
    assert ufs[0].lane_ref == dev.uuid  # spanning journey lanes


# ── mint forbidden ───────────────────────────────────────────────────────


def test_no_mint_new_grain_demotes_to_lane(monkeypatch):
    """A journey whose r1 target is a NEW route-group grain demotes to
    the lane (receivers are EXISTING PFs only); nothing is minted."""
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    route_file = "apps/web/app/embed/page.tsx"
    fe = Fl("f-embed", [(route_file, 120)], ep=route_file,
            name="embed-page")
    ue = UF("UF-E", "Embed booking", "trpc", members=["f-embed"])
    routes_index = [
        {"file": route_file, "pattern": "/embed", "surface_scope": "product"},
    ]
    devs, pfs, ufs, flows, grain, dev = _scene(
        [fe], [ue], routes_index=routes_index)
    tele = _run(devs, pfs, ufs, flows, grain)

    assert tele["pfs_minted"] == 0
    assert all(pf.name != "trpc" for pf in pfs)     # candidate still lanes
    assert ufs[0].product_feature_id is None
    assert ufs[0].lane_ref == dev.uuid
    assert not any(getattr(pf, "anchor_id", "") == "route:apps/web/app/embed"
                   for pf in pfs)


# ── flowless candidate (documenso keyless SACRED) ────────────────────────


def test_flowless_candidate_laned_row_is_legacy_shape(monkeypatch):
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    pfs = [PF("trpc", f"ws:{UNIT}"),
           PF("api-keys", "route:app/routes/settings/api-keys")]
    dev = Dev("trpc", "trpc", [f"{UNIT}/server/context.ts"], flows=[])
    grain = TargetGrainIndex(
        [], pfs, routes_index=[], excluded_units=[UNIT],
        candidate_pf_keys={"trpc"})
    tele = run_transport_handoff(
        [dev], pfs, [], [], [], Ctx(),
        {UNIT: "S1-transport:name-dep"}, grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers())

    assert "router_decomp" not in tele
    assert all(pf.name != "trpc" for pf in pfs)
    # the laned telemetry row carries NO B52 keys — byte-identical shape
    # to the legacy flowless path (documenso keyless SACRED).
    row = tele["laned"][0]
    assert "lane_journeys" not in row and "lane_flows" not in row
    assert row == {"unit": UNIT, "pf": "trpc", "ufs": 0, "rungs": {},
                   "minted": {}}


# ── scoping anti-cases ───────────────────────────────────────────────────


def test_non_ws_anchored_candidate_is_noop(monkeypatch):
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    pfs = [PF("trpc", "route:apps/web/app/trpc"),  # NOT a ws: anchor
           PF("api-keys", "route:app/routes/settings/api-keys")]
    fl = Fl("f-x", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"))
    dev = Dev("trpc", "trpc", [_rf("apiKeys")], flows=[fl])
    u = UF("UF-1", "X", "trpc", members=["f-x"])
    grain = TargetGrainIndex(
        [], pfs, routes_index=[], excluded_units=[UNIT],
        candidate_pf_keys={"trpc"})
    tele = run_transport_handoff(
        [dev], pfs, [u], [fl], [], Ctx(),
        {UNIT: "S1-transport:name-dep"}, grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers())

    assert tele["laned"] == []
    assert any(pf.name == "trpc" for pf in pfs)
    assert u.product_feature_id == "trpc" and u.lane_ref is None
    assert dev.product_feature_id == "trpc"


def test_non_transport_flowful_dev_untouched(monkeypatch):
    """A flowful dev OUTSIDE the candidate never rides the exemption —
    the stage only ever mutates candidate entities."""
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    devs, pfs, ufs, flows, grain, dev = _cal_shape(monkeypatch)
    other_fl = Fl("f-other", [("apps/web/app/other/page.tsx", 50)],
                  ep="apps/web/app/other/page.tsx", name="other")
    other = Dev("billing-core", None, ["apps/web/app/other/page.tsx"],
                flows=[other_fl])
    other.shared_reason = "genuinely_shared_infra"
    devs.append(other)
    _run(devs, pfs, ufs, flows, grain)

    assert other.product_feature_id is None
    assert other.shared_reason == "genuinely_shared_infra"  # untouched
    assert [f.uuid for f in other.flows] == ["f-other"]


# ── Seg3 plumbing: lane builder / terminal home / emission integrity ────


def test_lane_builder_flow_ids_and_journeys(monkeypatch):
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        _SHARED_REASON_INSTRUMENT,
        build_platform_infrastructure_lane,
    )
    from faultline.pipeline_v2.transport_handoff import FLOWFUL_LANE_ANCHOR
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "1")
    fl = Fl("f-admin", [(_handler("admin"), 200)], ep=_handler("admin"))
    resident = Dev("trpc", None, [_handler("admin")], flows=[fl])
    resident.shared_reason = _SHARED_REASON_INSTRUMENT
    resident.anchor_id = FLOWFUL_LANE_ANCHOR      # B52 provenance
    flowless = Dev("scripts", None, ["scripts/build.ts"], flows=[])
    flowless.shared_reason = _SHARED_REASON_INSTRUMENT
    # a PRE-EXISTING flowful lane resident (documenso openpage-api class:
    # laned by another stage, NO provenance anchor) must stay
    # byte-identical under the flag — the E==C SACRED exhibit.
    legacy = Dev("openpage-api", None, ["packages/openpage-api/x.ts"],
                 flows=[Fl("f-legacy", [("packages/openpage-api/x.ts", 9)])])
    legacy.shared_reason = _SHARED_REASON_INSTRUMENT
    uf = UF("UF-H", "Admin Users Management", None, members=["f-admin"])
    uf.lane_ref = resident.uuid
    rows = build_platform_infrastructure_lane(
        [resident, flowless, legacy], user_flows=[uf])

    r_trpc = next(r for r in rows if r["name"] == "trpc")
    assert r_trpc["flow_ids"] == ["f-admin"]
    assert r_trpc["journeys"] == [
        {"id": "UF-H", "name": "Admin Users Management"}]
    r_scripts = next(r for r in rows if r["name"] == "scripts")
    assert "flow_ids" not in r_scripts and "journeys" not in r_scripts
    r_legacy = next(r for r in rows if r["name"] == "openpage-api")
    assert "flow_ids" not in r_legacy and "journeys" not in r_legacy
    assert r_legacy["flows"] == 1                 # legacy count untouched


def test_lane_builder_off_no_new_keys(monkeypatch):
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        _SHARED_REASON_INSTRUMENT,
        build_platform_infrastructure_lane,
    )
    # Both default ON post-B62; pin OFF so the lane's accepted-reason set is
    # the pre-I22 world (the ANNEXATION_GUARD-only _SHARED_REASON_DEV_ARTIFACT
    # addition stays inert) and no FLOWFUL flow_ids/journeys keys appear.
    monkeypatch.setenv(FLOWFUL_TRANSPORT_LANE_ENV, "0")
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "0")
    fl = Fl("f-x", [(_handler("admin"), 200)], ep=_handler("admin"))
    resident = Dev("trpc", None, [_handler("admin")], flows=[fl])
    resident.shared_reason = _SHARED_REASON_INSTRUMENT
    rows = build_platform_infrastructure_lane([resident], user_flows=[])
    assert "flow_ids" not in rows[0] and "journeys" not in rows[0]
    assert rows[0]["flows"] == 1  # the legacy count field is untouched


def test_terminal_home_skips_lane_ref(monkeypatch):
    from faultline.pipeline_v2.uf_terminal_home import assign_terminal_homes
    monkeypatch.delenv("FAULTLINE_SPINE_UF_TERMINAL_HOME", raising=False)
    pf = PF("api-keys", "route:app/routes/settings/api-keys")
    owner = Dev("api-keys-dev", "api-keys",
                ["apps/web/app/settings/api-keys/page.tsx"])
    laned = UF("UF-L", "Admin", None, members=[])
    laned.lane_ref = "dev-trpc"
    orphan = UF("UF-O", "Orphan", None, members=[])
    tele = assign_terminal_homes([laned, orphan], [owner], [pf])

    assert laned.product_feature_id is None          # lane row survives
    assert laned.lane_ref == "dev-trpc"
    assert orphan.product_feature_id == "api-keys"   # real orphan homed
    assert tele["orphans"] == 1                      # lane row not counted


def test_emission_integrity_lane_ref_contract():
    from faultline.pipeline_v2.emission_integrity import (
        enforce_emission_integrity,
    )
    resident = Dev("trpc", None, [_handler("admin")],
                   flows=[Fl("f-a", [(_handler("admin"), 5)])])
    pf = PF("api-keys", "route:app/routes/settings/api-keys")
    # real paths + loc so the phantom pass keeps the PF (stub hygiene).
    pf.paths = ["apps/web/app/settings/api-keys/page.tsx"]
    pf.loc = 500
    pf.member_files = []
    ok = UF("UF-OK", "Admin", None, members=["f-a"])
    ok.lane_ref = resident.uuid
    ok.surface_scope = "platform_infrastructure"
    dangling = UF("UF-D", "Ghost", None, members=[])
    dangling.lane_ref = "dev-vanished"
    dangling.surface_scope = "platform_infrastructure"
    stale = UF("UF-S", "Rehomed later", "api-keys", members=[])
    stale.lane_ref = resident.uuid
    _f, _p, result = enforce_emission_integrity(
        [resident], [pf], [ok, dangling, stale], [])

    assert ok.lane_ref == resident.uuid              # valid → kept
    assert dangling.lane_ref is None                 # dangling → cleared
    assert dangling.surface_scope is None
    assert stale.lane_ref is None                    # product-homed → dead
    assert stale.product_feature_id == "api-keys"
    assert result.lane_refs_cleared == 2
    assert result.as_dict()["lane_refs_cleared"] == 2


def test_emission_integrity_no_lane_refs_key_when_clean():
    from faultline.pipeline_v2.emission_integrity import (
        EmissionIntegrityResult,
    )
    # byte-identity: the key is ABSENT on every pre-B52 / flag-OFF scan.
    assert "lane_refs_cleared" not in EmissionIntegrityResult().as_dict()


def test_userflow_serializer_omits_none_lane_ref():
    from faultline.models.types import UserFlow
    base = dict(id="UF-1", name="X", description="", persona="user",
                entry_point="", steps=[], member_flow_ids=[],
                member_count=0, intent="manage", resource="x")
    d0 = UserFlow(**base).model_dump(mode="json")
    assert "lane_ref" not in d0
    d1 = UserFlow(**base, lane_ref="dev-trpc").model_dump(mode="json")
    assert d1["lane_ref"] == "dev-trpc"


def test_surface_taxonomy_skips_lane_ref():
    from faultline.pipeline_v2.surface_taxonomy import (
        SurfaceScopeClassifier,
        _tag_user_flows,
    )
    uf = UF("UF-L", "Admin", None, members=[])
    uf.lane_ref = "dev-trpc"
    uf.surface_scope = "platform_infrastructure"
    uf.category = None
    counts = _tag_user_flows(
        [uf], {}, frozenset(), SurfaceScopeClassifier({}, ()))
    assert uf.surface_scope == "platform_infrastructure"  # not overwritten
    assert counts == {"platform_infrastructure": 1}
