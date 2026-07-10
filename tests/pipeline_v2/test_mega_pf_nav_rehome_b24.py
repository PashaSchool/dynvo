"""B24 — Stage 6.986 mega-PF nav-area journey re-home + floor-gated mint.

Ratified anti-cases (fixb24-design + Phase-2 brief):
  1. Trigger fires ONLY on mega topology (supabase-class): board share
     >= 25% AND >= 3 qualifying nav-area groups; documenso-shape
     (share-dominant, too few groups) and typebot-shape (grouped, not
     dominant) stay untouched; transport-candidate PFs are excluded.
  2. Journey conservation: UF count exact, no other PF loses a journey,
     the source keeps >= 1 (core / no-majority journeys stay).
  3. Attach floor blocks a thin-target move (validator I15 mirror).
  4. Mint == vote grain: the minted PF's anchor IS the voted group cid
     (tenant-descent rung inside the shared TargetGrainIndex).
  5. B16 sibling-unify can NOT re-merge the mint into the source.
  6. All-rung I16 rail: a move that would turn an I16-clean journey
     majority-foreign is refused (contested files abstain first).
  7. Residual-claim abstain: a no-dev-owner file touched by journeys
     landing on DIFFERENT targets never carves (shared substrate).
  8. Flag default OFF; determinism (double run == identical output).
"""

from __future__ import annotations

from collections import Counter

import pytest

from faultline.pipeline_v2.mega_pf_nav_rehome import (
    MEGA_PF_NAV_REHOME_ENV,
    mega_pf_nav_rehome_enabled,
    run_mega_pf_nav_rehome,
)
from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.transport_handoff import (
    TargetGrainIndex,
    _tenant_descend,
)

ROOT = "apps/studio/pages"


# ── scene stubs (the B22 test conventions) ──────────────────────────────


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
        from datetime import datetime, timezone
        self.authors = []
        self.total_commits = 0
        self.bug_fixes = 0
        self.coverage_pct = None
        self.last_modified = datetime.fromtimestamp(0, timezone.utc)
        self.health_score = 0.0


class PF:
    def __init__(self, name, anchor_id, surface_scope="product"):
        self.name = name
        self.uuid = f"pf-{name}"
        self.layer = "product"
        self.anchor_id = anchor_id
        self.surface_scope = surface_scope
        self.paths = []
        self.member_files = []


class Fl:
    def __init__(self, uuid, ep, paths=None):
        self.uuid = uuid
        self.entry_point_file = ep
        self.paths = list(paths) if paths is not None else ([ep] if ep else [])
        self.line_ranges = [
            {"path": p, "start_line": 1, "end_line": 10} for p in self.paths
        ]


class UF:
    def __init__(self, id, name, pfid, members=(), surface_scope="product"):
        self.id = id
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.surface_scope = surface_scope
        self.routes = []


class Ctx:
    repo_path = "."
    tracked_files = []


def _routes_index(files):
    return [{"file": f, "pattern": "/" + f[len(ROOT) + 1:],
             "surface_scope": "product"} for f in files]


def _anchor(cid, prefix, display):
    return SpineAnchor(canonical_id=cid, key=display.lower(),
                       source="route", display=display,
                       prefixes=(prefix,), sources=frozenset({"route"}))


def _grain(anchors, pfs, routes_index, transport_keys=()):
    return TargetGrainIndex(anchors, pfs, routes_index=routes_index,
                            candidate_pf_keys=set(transport_keys),
                            tenant_descent=True)


# ── the supabase-class scene ─────────────────────────────────────────────
#
# source 'projects' (api-anchored) annexed the console tree:
#   settings area (2 UFs) -> existing 'settings' PF
#   logs area (1 UF, 3 flows) -> existing 'logs' PF
#   database area (3 UFs, no PF)  -> MINT at the voted grain
#   core UF (entries under the source's own anchor) -> stays
#   split UF (1/1 areas) -> stays (no strict majority)


def _supabase_scene():
    S = f"{ROOT}/project/[ref]/settings"
    L = f"{ROOT}/project/[ref]/logs"
    D = f"{ROOT}/project/[ref]/database"
    API = f"{ROOT}/api/platform/projects"

    route_files = [
        f"{S}/general.tsx", f"{S}/api.tsx",
        f"{L}/index.tsx", f"{L}/explorer.tsx",
        f"{D}/tables.tsx", f"{D}/backups.tsx", f"{D}/triggers.tsx",
        f"{API}/index.ts",
        f"{ROOT}/project/[ref].tsx",
    ]
    flows = [
        # settings UFs (2 flows each; bodies include a settings-dev file)
        Fl("f-s1a", f"{S}/general.tsx",
           [f"{S}/general.tsx", "apps/studio/ifc/Settings/Panel.tsx",
            "apps/studio/ifc/shared_layout.tsx"]),
        Fl("f-s1b", f"{S}/api.tsx", [f"{S}/api.tsx", "settings-body.tsx"]),
        Fl("f-s2a", f"{S}/general.tsx",
           [f"{S}/general.tsx", "settings-body.tsx"]),
        Fl("f-s2b", f"{S}/api.tsx", [f"{S}/api.tsx", "settings-body.tsx"]),
        # logs UF (3 flows)
        Fl("f-l1", f"{L}/index.tsx", [f"{L}/index.tsx", "logs-body.tsx"]),
        Fl("f-l2", f"{L}/explorer.tsx",
           [f"{L}/explorer.tsx", "logs-body.tsx"]),
        Fl("f-l3", f"{L}/index.tsx", [f"{L}/index.tsx", "logs-body.tsx"]),
        # database UFs (2 flows each; d1 carries a UNIQUE residual file)
        Fl("f-d1a", f"{D}/tables.tsx",
           [f"{D}/tables.tsx", "apps/studio/ifc/Db/Grid.tsx",
            "apps/studio/ifc/shared_layout.tsx"]),
        Fl("f-d1b", f"{D}/tables.tsx", [f"{D}/tables.tsx"]),
        Fl("f-d2a", f"{D}/backups.tsx", [f"{D}/backups.tsx"]),
        Fl("f-d2b", f"{D}/backups.tsx", [f"{D}/backups.tsx"]),
        Fl("f-d3a", f"{D}/triggers.tsx", [f"{D}/triggers.tsx"]),
        Fl("f-d3b", f"{D}/triggers.tsx", [f"{D}/triggers.tsx"]),
        # core + split
        Fl("f-c1", f"{API}/index.ts"),
        Fl("f-c2", f"{ROOT}/project/[ref].tsx"),
        Fl("f-x1", f"{S}/general.tsx"),
        Fl("f-x2", f"{L}/index.tsx"),
        # other PF's journeys
        Fl("f-b1", "apps/studio/billing/one.ts"),
        Fl("f-b2", "apps/studio/billing/two.ts"),
    ]
    ufs = [
        UF("UF-01", "Manage settings", "projects", ["f-s1a", "f-s1b"]),
        UF("UF-02", "Manage API keys", "projects", ["f-s2a", "f-s2b"]),
        UF("UF-03", "View logs", "projects", ["f-l1", "f-l2", "f-l3"]),
        UF("UF-04", "Manage tables", "projects", ["f-d1a", "f-d1b"]),
        UF("UF-05", "Manage backups", "projects", ["f-d2a", "f-d2b"]),
        UF("UF-06", "Manage triggers", "projects", ["f-d3a", "f-d3b"]),
        UF("UF-07", "Project overview", "projects", ["f-c1", "f-c2"]),
        UF("UF-08", "Split journey", "projects", ["f-x1", "f-x2"]),
        UF("UF-09", "Billing A", "billing", ["f-b1"]),
        UF("UF-10", "Billing B", "billing", ["f-b2"]),
    ]
    devs = [
        # source dev owns the annexed console pages (+ split entries)
        Dev("projects-app", "projects", [
            f"{S}/general.tsx", f"{S}/api.tsx",
            f"{L}/index.tsx", f"{L}/explorer.tsx",
            f"{D}/tables.tsx", f"{D}/backups.tsx", f"{D}/triggers.tsx",
            f"{API}/index.ts", f"{ROOT}/project/[ref].tsx",
        ]),
        Dev("settings-ui", "settings",
            ["settings-body.tsx", "apps/studio/ifc/Settings/Panel.tsx"]),
        Dev("logs-ui", "logs", ["logs-body.tsx"]),
        Dev("billing-ui", "billing",
            ["apps/studio/billing/one.ts", "apps/studio/billing/two.ts"]),
        # NOTE: apps/studio/ifc/Db/Grid.tsx + shared_layout.tsx have NO
        # dev owner — residual mass (Grid unique to UF-04; layout shared
        # between UF-01 and UF-04 → must abstain).
    ]
    pfs = [
        PF("projects", f"route:{API}"),
        PF("settings", f"route:{S}"),
        PF("logs", f"route:{L}"),
        PF("billing", "route:apps/studio/billing"),
    ]
    for pf in pfs:
        pf.paths = []
    anchors = [
        _anchor(f"route:{API}", API, "Projects"),
        _anchor(f"route:{S}", S, "Settings"),
        _anchor(f"route:{L}", L, "Logs"),
        _anchor("route:apps/studio/billing", "apps/studio/billing",
                "Billing"),
    ]
    ri = _routes_index(route_files)
    grain = _grain(anchors, pfs, ri)
    return devs, pfs, ufs, flows, ri, grain


def _run(devs, pfs, ufs, flows, ri, grain, **kw):
    return run_mega_pf_nav_rehome(
        devs, pfs, ufs, flows, ri, Ctx(), grain_index=grain, **kw)


# ── 1. trigger topology ──────────────────────────────────────────────────


def test_trigger_fires_on_mega_topology_and_rehomes():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == ["projects"]
    homes = {u.id: u.product_feature_id for u in ufs}
    assert homes["UF-01"] == "settings"
    assert homes["UF-02"] == "settings"
    assert homes["UF-03"] == "logs"
    assert homes["UF-04"] == homes["UF-05"] == homes["UF-06"] == "database"
    # conservation stays
    assert homes["UF-07"] == "projects"          # core rail
    assert homes["UF-08"] == "projects"          # no strict majority
    assert homes["UF-09"] == homes["UF-10"] == "billing"


def test_no_trigger_documenso_shape_share_without_groups():
    # dominant share, but journeys don't cluster into >=3 nav groups
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # strip the database + logs journeys' members down to ONE area
    for u in ufs:
        if u.id in ("UF-04", "UF-05", "UF-06", "UF-03"):
            u.member_flow_ids = ["f-x1"]  # settings-area entry only
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == []
    assert all(u.product_feature_id == "projects"
               for u in ufs if u.id.startswith("UF-0") and u.id <= "UF-08")


def test_no_trigger_typebot_shape_groups_without_share():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # dilute the board: 30 extra journeys on other PFs → share < 0.25
    extra = [UF(f"UF-x{i}", f"Extra {i}", "billing", ["f-b1"])
             for i in range(30)]
    tele = _run(devs, pfs, ufs + extra, flows, ri, grain)
    assert tele["triggered"] == []


def test_no_trigger_when_top_pf_is_transport_candidate():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    pfs[0].anchor_id = "ws:packages/projects"  # transport-shaped source
    tele = run_mega_pf_nav_rehome(
        devs, pfs, ufs, flows, ri, Ctx(), grain_index=grain,
        transport_candidate_units=["packages/projects"])
    assert tele["triggered"] == []


def test_no_trigger_without_strict_top():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # tie the top: billing gets as many journeys as projects (8)
    extra = [UF(f"UF-y{i}", f"B{i}", "billing", ["f-b1"]) for i in range(6)]
    tele = _run(devs, pfs, ufs + extra, flows, ri, grain)
    assert tele["triggered"] == []


# ── 2. conservation invariant ────────────────────────────────────────────


def test_journey_conservation_holds():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    before_count = len(ufs)
    before_homes = Counter(u.product_feature_id for u in ufs)
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"]
    assert "conservation_violations" not in tele
    assert len(ufs) == before_count
    after_homes = Counter(u.product_feature_id for u in ufs)
    for key, n in before_homes.items():
        if key != "projects":
            assert after_homes.get(key, 0) >= n, key
    assert after_homes["projects"] >= 1          # orphan guard
    assert sum(after_homes.values()) == before_count


# ── 3. attach floor ──────────────────────────────────────────────────────


def test_attach_floor_blocks_thin_target_move():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # bury the settings journeys in foreign-owned mass: attach < 0.34
    foreign = [f"apps/studio/billing/f{i}.ts" for i in range(8)]
    devs.append(Dev("billing-extra", "billing", foreign))
    for fl in flows:
        if fl.uuid in ("f-s1a", "f-s1b"):
            fl.paths = [fl.entry_point_file] + foreign
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == ["projects"]
    dropped = {d["uf"] for d in tele["floor_drops"]}
    assert "UF-01" in dropped
    assert next(u for u in ufs if u.id == "UF-01").product_feature_id \
        == "projects"
    # the healthy sibling journeys still move
    assert next(u for u in ufs if u.id == "UF-03").product_feature_id \
        == "logs"


# ── 4. mint == vote grain (tenant descent inside the shared oracle) ─────


def test_mint_anchor_is_the_voted_group_cid():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    cid = f"route:{ROOT}/project/[ref]/database"
    # the oracle itself answers the descended grain for a database file
    t = grain.grain_of_file(f"{ROOT}/project/[ref]/database/tables.tsx")
    assert t is not None and t.kind == "new" and t.key == cid
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    minted = [pf for pf in pfs if getattr(pf, "anchor_id", "") == cid]
    assert len(minted) == 1
    assert tele["mints"] == [{"cid": cid, "pf": "database", "ufs": 3}]
    # every database journey points at the minted slug
    assert {u.product_feature_id for u in ufs
            if u.id in ("UF-04", "UF-05", "UF-06")} == {"database"}


def test_tenant_descent_walk():
    assert _tenant_descend(["project", "[ref]", "database", "backups.tsx"]) \
        == ("database", "project/[ref]/database")
    assert _tenant_descend(["org", "[slug]", "sso.tsx"]) \
        == ("sso", "org/[slug]/sso")
    assert _tenant_descend(["support", "new.tsx"]) is None       # no pair
    assert _tenant_descend(["project", "[ref].tsx"]) is None     # detail leaf
    assert _tenant_descend(["documents", "[id]", "edit.tsx"]) is None  # CRUD
    assert _tenant_descend(["project", "[ref]", "page.tsx"]) is None   # stem
    assert _tenant_descend(["a", "[x]", "b", "[y]", "c", "d.tsx"]) \
        == ("c", "a/[x]/b/[y]/c")                                # multi-pair
    # transparent protocol/version hop (the live "api"-mint exhibit)
    assert _tenant_descend(["api", "incidents", "banner.ts"]) \
        == ("incidents", "api/incidents")
    assert _tenant_descend(["api", "v1", "functions", "x.ts"]) \
        == ("functions", "api/v1/functions")
    assert _tenant_descend(["api", "route.ts"]) is None
    assert _tenant_descend(["api", "v1", "edit.ts"]) is None     # CRUD leaf


def test_descent_off_by_default_keeps_b22_grain():
    devs, pfs, ufs, flows, ri, _grain_on = _supabase_scene()
    anchors = [
        _anchor(f"route:{ROOT}/api/platform/projects",
                f"{ROOT}/api/platform/projects", "Projects"),
    ]
    off = TargetGrainIndex(anchors, pfs, routes_index=ri)
    on = TargetGrainIndex(anchors, pfs, routes_index=ri,
                          tenant_descent=True)
    p = f"{ROOT}/project/[ref]/database/tables.tsx"
    t_off, t_on = off.grain_of_file(p), on.grain_of_file(p)
    assert t_on is not None and t_on.key.endswith("/database")
    assert t_off is None or not t_off.key.endswith("/database")


# ── 5. B16 sibling-unify can NOT re-merge the mint ───────────────────────


def test_b16_sibling_unify_does_not_remerge_mint():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    _run(devs, pfs, ufs, flows, ri, grain)
    from faultline.pipeline_v2.stage_6_88_sibling_unify import (
        unify_sibling_anchors,
    )
    names_before = sorted(getattr(pf, "name", "") for pf in pfs)
    tele = unify_sibling_anchors(ufs, devs, pfs)
    assert sorted(getattr(pf, "name", "") for pf in pfs) == names_before
    assert not any("database" in (m.get("winner", ""), m.get("loser", ""))
                   for m in tele["merges"])
    assert {u.product_feature_id for u in ufs
            if u.id in ("UF-04", "UF-05", "UF-06")} == {"database"}


# ── 6. all-rung I16 rail (with the contested-carve fixed point) ─────────


def test_i16_rail_blocks_foreign_entry_move():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    L = f"{ROOT}/project/[ref]/logs"
    # UF-03's entries: one carvable + two CONTESTED source-owned files
    # (also flow files of UF-01 → target settings), so post-carve the
    # journey would be majority-foreign at 'logs' while I16-clean today.
    sh1, sh2 = f"{L}/sh1.tsx", f"{L}/sh2.tsx"
    for d in devs:
        if d.name == "projects-app":
            d.paths += [sh1, sh2]
    flows.append(Fl("f-l4", sh1, [sh1, "logs-body.tsx"]))
    flows.append(Fl("f-l5", sh2, [sh2, "logs-body.tsx"]))
    uf3 = next(u for u in ufs if u.id == "UF-03")
    uf3.member_flow_ids = ["f-l1", "f-l4", "f-l5"]
    # contest sh1/sh2 into the settings journeys' flow surface
    for fl in flows:
        if fl.uuid in ("f-s1a", "f-s2a"):
            fl.paths = fl.paths + [sh1, sh2]
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == ["projects"]
    rail = {d["uf"] for d in tele["i16_rail_drops"]}
    floor = {d["uf"] for d in tele["floor_drops"]}
    assert "UF-03" in rail | floor  # refused, never shipped foreign
    assert uf3.product_feature_id == "projects"


# ── 7. residual-claim abstain ────────────────────────────────────────────


def test_shared_residual_never_carves_unique_residual_does():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    _run(devs, pfs, ufs, flows, ri, grain)
    carved = set()
    for d in devs:
        if getattr(d, "anchor_id", "") and "nav-rehome" in str(d.anchor_id):
            carved.update(d.paths)
    assert "apps/studio/ifc/Db/Grid.tsx" in carved         # unique → follows
    assert "apps/studio/ifc/shared_layout.tsx" not in carved  # shared → stays


# ── 8. flag posture + determinism ───────────────────────────────────────


def test_flag_default_on(monkeypatch):
    # Default flipped ON by the 2026-07-10 keyed supabase A/B decision;
    # =0 remains the kill-switch back to the pre-B24 board.
    monkeypatch.delenv(MEGA_PF_NAV_REHOME_ENV, raising=False)
    assert mega_pf_nav_rehome_enabled() is True
    monkeypatch.setenv(MEGA_PF_NAV_REHOME_ENV, "0")
    assert mega_pf_nav_rehome_enabled() is False
    monkeypatch.setenv(MEGA_PF_NAV_REHOME_ENV, "1")
    assert mega_pf_nav_rehome_enabled() is True
    monkeypatch.setenv(MEGA_PF_NAV_REHOME_ENV, "true")
    assert mega_pf_nav_rehome_enabled() is True


def test_determinism_double_run():
    out = []
    for _ in range(2):
        devs, pfs, ufs, flows, ri, grain = _supabase_scene()
        tele = _run(devs, pfs, ufs, flows, ri, grain)
        out.append((
            tele["moves"], tele["mints"],
            sorted((u.id, str(u.product_feature_id)) for u in ufs),
            sorted(getattr(pf, "name", "") for pf in pfs),
            sorted(getattr(d, "name", "") for d in devs),
        ))
    assert out[0] == out[1]


# ── 9. flowful devs never stranded ──────────────────────────────────────


def test_no_flowful_dev_left_pathless():
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # give the source dev flows so carves must keep it coherent
    src = next(d for d in devs if d.name == "projects-app")
    src.flows = [f for f in flows if f.uuid.startswith(("f-s", "f-d"))]
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"]
    for d in devs:
        if getattr(d, "flows", None):
            assert getattr(d, "paths", []), \
                f"flowful dev '{d.name}' stranded pathless"
        assert getattr(d, "product_feature_id", "x") is not None or \
            not getattr(d, "flows", None)


# ── 10. member-less seeds never dilute the dominance census ─────────────


def test_memberless_seeds_do_not_dilute_share():
    """Live diag (keyless supabase 2026-07-10): at 6.986-time the board
    still carries member-less recall/system seeds that later stages
    demote — with them in the denominator the umbrella read 0.245 and
    the trigger missed. Member-less rows count on NEITHER side."""
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # 30 member-less seeds homed to billing: with the old all-homed
    # census projects' share would be 8/40 = 0.20 < 0.25 (no trigger).
    seeds = [UF(f"UF-s{i}", f"Run seed {i}", "billing", members=())
             for i in range(30)]
    tele = _run(devs, pfs, ufs + seeds, flows, ri, grain)
    assert tele["triggered"] == ["projects"]
    # seeds are untouched (they can neither vote nor move)
    assert all(u.product_feature_id == "billing" for u in seeds)


# ── 11. non-product-surface journeys never dilute the census ────────────


def test_nonproduct_surface_homes_do_not_dilute_share():
    """Live diag #2 (keyless supabase): the 6.986 board still carries
    journeys homed to blog/careers-class PFs that the emission
    partitioner later moves off the product board — with them in the
    denominator the umbrella read 0.245. Homes the SurfaceScopeClassifier
    scopes non-product count on NEITHER side."""
    devs, pfs, ufs, flows, ri, grain = _supabase_scene()
    # a marketing PF whose paths are routes_index-scoped 'marketing'
    mk_files = [f"apps/www/pages/blog/p{i}.tsx" for i in range(4)]
    blog = PF("blog", "route:apps/www/pages/blog")
    blog.paths = list(mk_files)
    pfs.append(blog)
    ri2 = ri + [{"file": f, "pattern": "/" + f.split("pages/")[-1],
                 "surface_scope": "marketing"} for f in mk_files]
    for i, f in enumerate(mk_files):
        flows.append(Fl(f"f-mk{i}", f, [f]))
    # 30 marketing journeys homed to blog: with them in the denominator
    # projects' share would be 8/40 = 0.20 < 0.25 (no trigger).
    mk_ufs = [UF(f"UF-m{i}", f"Read blog {i}", "blog",
                 [f"f-mk{i % 4}"]) for i in range(30)]
    grain2 = _grain(
        [a for a in (
            _anchor(f"route:{ROOT}/api/platform/projects",
                    f"{ROOT}/api/platform/projects", "Projects"),
            _anchor(f"route:{ROOT}/project/[ref]/settings",
                    f"{ROOT}/project/[ref]/settings", "Settings"),
            _anchor(f"route:{ROOT}/project/[ref]/logs",
                    f"{ROOT}/project/[ref]/logs", "Logs"),
        )], pfs, ri2)
    tele = run_mega_pf_nav_rehome(
        devs, pfs, ufs + mk_ufs, flows, ri2, Ctx(), grain_index=grain2)
    assert tele["triggered"] == ["projects"]
    assert all(u.product_feature_id == "blog" for u in mk_ufs)
