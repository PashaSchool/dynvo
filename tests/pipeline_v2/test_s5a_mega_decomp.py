"""S5a — mega-PF decomposition ARMING (FAULTLINE_MEGA_DECOMP_ARM).

NAMED UNITS + ANTI-CASES (spec docs/anchor-arc/fixs5a-mega-decomposition-spec.md):

  Seg A (population-derived roots — routers dialect):
    * admin-mint shape: a non-dialect central-router file
      ``backend/routers/admin.py`` becomes a mint-able nav group ONLY when
      the routes_index population clusters >=2 product route files under
      ``backend/routers`` AND the flag is armed. Lone orphan → no root.
      Dialect (app-router) roots are byte-identical armed vs unarmed.
  Seg B (group → sibling core-identity token match):
    * compliance → compliance-page: a route GROUP with no exact-anchor PF
      resolves to the SIBLING PF whose identity token it uniquely echoes.
    * duplicate-workflow token bridge (novu): several domain-dir groups
      each bridge to their same-named sibling PF.
    * ambiguous token (>=2 PFs) abstains → mints a new group, never guesses.
  Seg C (6.99b organic candidate → B24-class move through the S3 ledger):
    * an organic (non-synthesized) row tripping the anchor-breadth ruler
      moves, journaled at rung "mega" via propose_pf_now; OFF = telemetry
      only. θ-guard: a legitimately-homed organic row is never moved.

  SACRED anti-cases (0 moves): twenty workflows / cal bookings.
  Kill-switch: the two grain params default False → the oracle is
  byte-identical armed-param-False vs a plain (pre-S5a) construction.
"""

from __future__ import annotations

import os

import pytest

from faultline.models.types import UserFlow
from faultline.pipeline_v2.mega_pf_nav_rehome import run_mega_pf_nav_rehome
from faultline.pipeline_v2.overturn_ledger import (
    OverturnLedger,
    install_ledger,
    uninstall_ledger,
)
from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.transport_handoff import (
    MEGA_DECOMP_ARM_ENV,
    GrainTarget,
    TargetGrainIndex,
    mega_decomp_armed,
)


# ── stubs (the B22/B24 test conventions) ────────────────────────────────


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
    def __init__(self, name, anchor_id, surface_scope="product"):
        self.name = name
        self.id = name
        self.uuid = f"pf-{name}"
        self.layer = "product"
        self.anchor_id = anchor_id
        self.surface_scope = surface_scope
        self.paths = []
        self.member_files = []


class Fl:
    def __init__(self, uuid, ep, paths=None, name=""):
        self.uuid = uuid
        self.entry_point_file = ep
        self.name = name
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


class Feat:
    """A 6.99b ``features[]`` entry — holds member flows (the stage builds
    its flow_by_uuid from ``features[].flows``)."""

    def __init__(self, name, flows):
        self.name = name
        self.flows = list(flows)


class Ctx:
    repo_path = "."
    tracked_files: list[str] = []


def _routes_index(files, root_pattern_from=None):
    """Product-scoped routes_index entries (surface_scope=product)."""
    out = []
    for f in files:
        out.append({"file": f, "pattern": "/" + f, "surface_scope": "product"})
    return out


def _anchor(cid, prefix, display):
    return SpineAnchor(canonical_id=cid, key=display.lower(),
                       source="route", display=display,
                       prefixes=(prefix,), sources=frozenset({"route"}))


@pytest.fixture(autouse=True)
def _clean_flag():
    prev = os.environ.pop(MEGA_DECOMP_ARM_ENV, None)
    yield
    if prev is None:
        os.environ.pop(MEGA_DECOMP_ARM_ENV, None)
    else:
        os.environ[MEGA_DECOMP_ARM_ENV] = prev


# ════════════════════════════════════════════════════════════════════════
# Seg A — population-derived roots
# ════════════════════════════════════════════════════════════════════════


def _backend_router_ri():
    # A FastAPI backend: NO _ROUTE_ROOT_SEQS dialect run keys a root
    # inside these files; the population clusters >=2 under backend/routers.
    return _routes_index([
        "backend/routers/admin.py",
        "backend/routers/chat.py",
        "backend/routers/oauth.py",
    ])


def test_seg_a_population_root_forms_admin_group_armed():
    ri = _backend_router_ri()
    idx = TargetGrainIndex([], [], routes_index=ri, population_roots=True)
    assert "backend/routers" in idx.routes_roots
    g = idx.grain_of_file("backend/routers/admin.py")
    assert g is not None and g.kind == "new"
    assert g.key == "route:backend/routers/admin"
    # each central-router leaf keys its OWN nav group (flat-leaf stem rule)
    assert idx.grain_of_file("backend/routers/chat.py").key == \
        "route:backend/routers/chat"


def test_seg_a_off_no_population_root():
    ri = _backend_router_ri()
    idx = TargetGrainIndex([], [], routes_index=ri, population_roots=False)
    assert "backend/routers" not in idx.routes_roots
    assert idx.grain_of_file("backend/routers/admin.py") is None


def test_seg_a_lone_orphan_never_mints_a_root():
    # only ONE product route file under backend/routers → below the
    # population floor (>=2 distinct) → no root even armed.
    ri = _routes_index(["backend/routers/admin.py"])
    idx = TargetGrainIndex([], [], routes_index=ri, population_roots=True)
    assert "backend/routers" not in idx.routes_roots
    assert idx.grain_of_file("backend/routers/admin.py") is None


def test_seg_a_dialect_root_unaffected_by_arming():
    # a Next App-Router repo: the dialect ALREADY keys src/app; arming must
    # add nothing (every file is dialect-rooted, no orphans).
    ri = _routes_index([
        "src/app/dashboard/page.tsx",
        "src/app/settings/page.tsx",
    ])
    off = TargetGrainIndex([], [], routes_index=ri, population_roots=False)
    on = TargetGrainIndex([], [], routes_index=ri, population_roots=True)
    assert set(off.routes_roots) == set(on.routes_roots) == {"src/app"}


# ════════════════════════════════════════════════════════════════════════
# Seg B — group → sibling core-identity token match
# ════════════════════════════════════════════════════════════════════════


def _compliance_scene():
    # A hungry sibling PF 'compliance-page' exists but is anchored ELSEWHERE
    # (a poisoned/foreign prefix — the B69 disease); the route group where
    # its pages actually live has NO exact-anchor PF.
    ri = _routes_index([
        "frontend/src/pages/compliance-page/index.tsx",
        "frontend/src/pages/compliance-page/list.tsx",
    ])
    pfs = [
        PF("network-security", "route:frontend/src/pages/network"),
        PF("compliance-page", "hub:vendor/compliance-page"),  # foreign anchor
    ]
    return ri, pfs


def test_seg_b_sibling_token_feeds_compliance_page_armed():
    ri, pfs = _compliance_scene()
    idx = TargetGrainIndex([], pfs, routes_index=ri,
                           population_roots=True, sibling_tokens=True)
    g = idx.grain_of_file("frontend/src/pages/compliance-page/index.tsx")
    assert g is not None
    assert g == GrainTarget("pf", "compliance-page", display=g.display)
    assert g.kind == "pf" and g.key == "compliance-page"


def test_seg_b_off_mints_new_group():
    ri, pfs = _compliance_scene()
    idx = TargetGrainIndex([], pfs, routes_index=ri,
                           population_roots=True, sibling_tokens=False)
    g = idx.grain_of_file("frontend/src/pages/compliance-page/index.tsx")
    assert g is not None and g.kind == "new"
    assert g.key == "route:frontend/src/pages/compliance-page"


def test_seg_b_ambiguous_token_abstains():
    # TWO PFs carry the 'compliance-page' identity token → ambiguous →
    # never guess; the group mints a new PF (kind == 'new').
    ri, pfs = _compliance_scene()
    pfs.append(PF("compliance-page-mirror", "hub:other/compliance-page"))
    idx = TargetGrainIndex([], pfs, routes_index=ri,
                           population_roots=True, sibling_tokens=True)
    g = idx.grain_of_file("frontend/src/pages/compliance-page/index.tsx")
    assert g is not None and g.kind == "new"


def test_seg_b_dupwf_multi_domain_token_bridge():
    # novu duplicate-workflow: several domain-dir groups each bridge to
    # their same-named sibling PF (the token→sibling 47% mass).
    domains = ["analytics", "subscribers", "translations"]
    files = [f"apps/web/src/pages/{d}/index.tsx" for d in domains]
    ri = _routes_index(files)
    pfs = [PF("duplicate-workflow", "ws:apps/web")]
    pfs += [PF(d, f"hub:vendor/{d}") for d in domains]  # foreign anchors
    idx = TargetGrainIndex([], pfs, routes_index=ri,
                           population_roots=True, sibling_tokens=True)
    for d in domains:
        g = idx.grain_of_file(f"apps/web/src/pages/{d}/index.tsx")
        assert g is not None and g.kind == "pf" and g.key == d, d


# ════════════════════════════════════════════════════════════════════════
# Seg A end-to-end — admin-mint shape through the mega pass
# ════════════════════════════════════════════════════════════════════════


def _soc0_admin_scene():
    """network-security is the board-top umbrella; its admin/chat/oauth
    journeys live under the non-dialect backend/routers tree. Armed, the
    admin group (3 UFs / 3 flows) mints its own PF; chat/oauth (2 UFs each)
    qualify T1 but sit below the mint floor → stay."""
    A = "backend/routers/admin.py"
    C = "backend/routers/chat.py"
    O = "backend/routers/oauth.py"
    ri = _routes_index([A, C, O])
    flows = [
        Fl("f-a1", A, [A]), Fl("f-a2", A, [A]), Fl("f-a3", A, [A]),
        Fl("f-c1", C, [C]), Fl("f-c2", C, [C]),
        Fl("f-o1", O, [O]), Fl("f-o2", O, [O]),
        Fl("f-b1", "app/billing/one.ts"), Fl("f-b2", "app/billing/two.ts"),
    ]
    ufs = [
        UF("UF-a1", "Manage admins", "network-security", ["f-a1"]),
        UF("UF-a2", "Get admin chats", "network-security", ["f-a2"]),
        UF("UF-a3", "Post admin deletes", "network-security", ["f-a3"]),
        UF("UF-c1", "Browse chats", "network-security", ["f-c1"]),
        UF("UF-c2", "Post chat", "network-security", ["f-c2"]),
        UF("UF-o1", "Connect oauth", "network-security", ["f-o1"]),
        UF("UF-o2", "Revoke oauth", "network-security", ["f-o2"]),
        UF("UF-b1", "Billing A", "billing", ["f-b1"]),
        UF("UF-b2", "Billing B", "billing", ["f-b2"]),
    ]
    devs = [
        Dev("api-admin", "network-security", [A, C, O]),
        Dev("billing-ui", "billing",
            ["app/billing/one.ts", "app/billing/two.ts"]),
    ]
    pfs = [
        PF("network-security", "route:backend/routers/netsec"),
        PF("billing", "route:app/billing"),
    ]
    anchors = [
        _anchor("route:app/billing", "app/billing", "Billing"),
    ]
    return devs, pfs, ufs, flows, ri, anchors


def _grain(anchors, pfs, ri, armed):
    return TargetGrainIndex(
        anchors, pfs, routes_index=ri, tenant_descent=True,
        population_roots=armed, sibling_tokens=armed)


def test_seg_a_admin_mint_shape_armed():
    devs, pfs, ufs, flows, ri, anchors = _soc0_admin_scene()
    grain = _grain(anchors, pfs, ri, armed=True)
    tele = run_mega_pf_nav_rehome(devs, pfs, ufs, flows, ri, Ctx(),
                                  grain_index=grain)
    assert tele["triggered"] == ["network-security"]
    # admin PF minted from the 3 admin journeys
    assert tele["pfs_minted"] == 1
    minted = tele["mints"][0]
    assert minted["cid"] == "route:backend/routers/admin"
    assert minted["ufs"] == 3
    homes = {u.id: u.product_feature_id for u in ufs}
    assert homes["UF-a1"] == homes["UF-a2"] == homes["UF-a3"] == minted["pf"]
    # chat/oauth qualified T1 but sit below the mint floor → they STAY
    assert homes["UF-c1"] == homes["UF-c2"] == "network-security"
    assert homes["UF-o1"] == homes["UF-o2"] == "network-security"
    # conservation: billing untouched, source keeps journeys
    assert homes["UF-b1"] == homes["UF-b2"] == "billing"
    assert "conservation_violations" not in tele


def test_seg_a_admin_mint_off_bails_no_mint():
    devs, pfs, ufs, flows, ri, anchors = _soc0_admin_scene()
    grain = _grain(anchors, pfs, ri, armed=False)
    tele = run_mega_pf_nav_rehome(devs, pfs, ufs, flows, ri, Ctx(),
                                  grain_index=grain)
    # without population roots the backend/routers journeys get NO grain
    # vote → 0 qualifying groups → T1 fails → the pass never decomposes.
    assert tele["triggered"] == []
    assert tele["pfs_minted"] == 0
    assert all(u.product_feature_id == "network-security"
               for u in ufs if u.id.startswith("UF-a")
               or u.id.startswith("UF-c") or u.id.startswith("UF-o"))


def test_seg_a_admin_mint_determinism():
    for _ in range(2):
        devs, pfs, ufs, flows, ri, anchors = _soc0_admin_scene()
        grain = _grain(anchors, pfs, ri, armed=True)
        tele = run_mega_pf_nav_rehome(devs, pfs, ufs, flows, ri, Ctx(),
                                      grain_index=grain)
        got = (tele["pfs_minted"], sorted(m["cid"] for m in tele["mints"]),
               tele["ufs_rehomed"])
    assert got == (1, ["route:backend/routers/admin"], 3)


# ════════════════════════════════════════════════════════════════════════
# SACRED anti-cases — 0 moves on twenty workflows / cal bookings
# ════════════════════════════════════════════════════════════════════════


def _twenty_scene():
    """twenty: 'workflows' is board-top but its journeys live in deep
    internal modules that are NOT product routes → no grain votes → 0
    qualifying nav groups → the pass never fires (armed or not)."""
    M = "packages/twenty-front/src/modules/workflow"
    flows = [Fl("f-w1", f"{M}/a.ts"), Fl("f-w2", f"{M}/b.ts"),
             Fl("f-w3", f"{M}/c.ts"), Fl("f-s1", "app/settings/one.ts")]
    ufs = [
        UF("UF-w1", "Build workflows", "workflows", ["f-w1"]),
        UF("UF-w2", "Trigger workflows", "workflows", ["f-w2"]),
        UF("UF-w3", "Run workflows", "workflows", ["f-w3"]),
        UF("UF-s1", "Settings", "settings", ["f-s1"]),
    ]
    devs = [Dev("wf", "workflows", [f"{M}/a.ts", f"{M}/b.ts", f"{M}/c.ts"]),
            Dev("st", "settings", ["app/settings/one.ts"])]
    pfs = [PF("workflows", "ws:packages/twenty-front"),
           PF("settings", "route:app/settings")]
    ri = _routes_index(["app/settings/one.ts"])
    return devs, pfs, ufs, flows, ri, []


def test_anticase_twenty_workflows_zero_moves_armed():
    devs, pfs, ufs, flows, ri, anchors = _twenty_scene()
    grain = _grain(anchors, pfs, ri, armed=True)
    tele = run_mega_pf_nav_rehome(devs, pfs, ufs, flows, ri, Ctx(),
                                  grain_index=grain)
    assert tele["triggered"] == []
    assert tele["ufs_rehomed"] == 0 and tele["pfs_minted"] == 0
    assert all(u.product_feature_id == "workflows"
               for u in ufs if u.id.startswith("UF-w"))


def _cal_scene():
    """cal.com: 'bookings' clusters into nav groups (T1 could fire) but it
    holds < 25% of the board's journeys → T2 blocks (the real cal blocker,
    probe share 0.092). Even armed, nothing moves."""
    B = "app/bookings"
    flows = [Fl("f-b1", f"{B}/upcoming/page.tsx", [f"{B}/upcoming/page.tsx"]),
             Fl("f-b2", f"{B}/past/page.tsx", [f"{B}/past/page.tsx"])]
    # 2 booking UFs, 8 other-PF UFs → bookings share = 2/10 = 0.20 < 0.25
    ufs = [UF("UF-b1", "Browse bookings", "bookings", ["f-b1"]),
           UF("UF-b2", "Filter bookings", "bookings", ["f-b2"])]
    other_flows = []
    for i in range(8):
        fid = f"f-x{i}"
        other_flows.append(Fl(fid, f"app/admin/p{i}/page.tsx",
                              [f"app/admin/p{i}/page.tsx"]))
        ufs.append(UF(f"UF-x{i}", f"Admin {i}", "admin", [fid]))
    flows += other_flows
    devs = [Dev("bk", "bookings",
                [f"{B}/upcoming/page.tsx", f"{B}/past/page.tsx"]),
            Dev("ad", "admin",
                [f"app/admin/p{i}/page.tsx" for i in range(8)])]
    pfs = [PF("bookings", "route:app/bookings"),
           PF("admin", "route:app/admin")]
    ri = _routes_index([f"{B}/upcoming/page.tsx", f"{B}/past/page.tsx"]
                       + [f"app/admin/p{i}/page.tsx" for i in range(8)])
    return devs, pfs, ufs, flows, ri, []


def test_anticase_cal_bookings_zero_moves_armed():
    devs, pfs, ufs, flows, ri, anchors = _cal_scene()
    grain = _grain(anchors, pfs, ri, armed=True)
    tele = run_mega_pf_nav_rehome(devs, pfs, ufs, flows, ri, Ctx(),
                                  grain_index=grain)
    # 'admin' is the strict top (8), and even it holds 8/10 = 0.80; the
    # POINT is bookings never sheds a journey.
    assert all(u.product_feature_id == "bookings"
               for u in ufs if u.id.startswith("UF-b"))


# ════════════════════════════════════════════════════════════════════════
# Seg C — 6.99b organic candidate → B24-class move through the S3 ledger
# ════════════════════════════════════════════════════════════════════════


def _uf(uid, name, pfid, members, synthesized=False, resource=""):
    return UserFlow(id=uid, name=name, product_feature_id=pfid,
                    intent="manage", resource=resource,
                    member_flow_ids=list(members),
                    member_count=len(members), synthesized=synthesized)


def _organic_scene():
    from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
        run_post_uf_rehome,
    )
    reg = {
        "route:network": _anchor("route:network", "backend/routers/netsec",
                                 "Network"),
        "route:admin": _anchor("route:admin", "backend/routers/admin",
                               "Admin"),
    }
    fl_a1 = Fl("f-a1", "backend/routers/admin/list.py", name="list-admins")
    fl_a2 = Fl("f-a2", "backend/routers/admin/delete.py", name="delete-admin")
    fl_n1 = Fl("f-n1", "backend/routers/netsec/scan.py", name="run-scan")
    devs = [Feat("d1", [fl_a1, fl_a2, fl_n1])]
    # organic (LLM-drawn) row homed to network but whose members live wholly
    # under the admin anchor: home_share 0.0, rival(admin)=1.0.
    booty = _uf("UF-001", "Manage admins", "network", ["f-a1", "f-a2"])
    keeper = _uf("UF-002", "Run network security", "network", ["f-n1"])
    return run_post_uf_rehome, reg, devs, booty, keeper


def test_seg_c_off_organic_telemetry_only():
    # MECHANICAL flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): the OFF
    # world is now the explicit kill-switch (unset arms Seg C).
    os.environ[MEGA_DECOMP_ARM_ENV] = "0"
    run, reg, devs, booty, keeper = _organic_scene()
    pfs = [PF("network", "route:network"), PF("admin", "route:admin")]
    tele = run([booty, keeper], devs, pfs, reg)   # explicit kill-switch
    assert tele.get("organic_candidates") == 1
    assert tele["rehomed"] == 0
    assert booty.product_feature_id == "network"   # NOT moved


def test_seg_c_organic_bridge_moves_through_ledger():
    os.environ[MEGA_DECOMP_ARM_ENV] = "1"
    run, reg, devs, booty, keeper = _organic_scene()
    pfs = [PF("network", "route:network"), PF("admin", "route:admin")]
    led = OverturnLedger()
    install_ledger(led)
    try:
        tele = run([booty, keeper], devs, pfs, reg)
    finally:
        uninstall_ledger()
    assert tele["rehomed"] == 1
    assert tele.get("organic_rehomed") == 1
    assert booty.product_feature_id == "admin"     # moved to the wider owner
    assert keeper.product_feature_id == "network"  # I8: source keeps a row
    # the move was journaled through the S3 ledger AS a B24-class (mega) rung
    mega_uf = [e for e in led.entries
               if e.kind == "uf" and e.rung == "mega" and e.new == "admin"]
    assert mega_uf, "organic move must journal at rung 'mega'"
    assert mega_uf[0].writer == "mega:converted-site"


def test_seg_c_synthesized_row_arm_independent():
    # a synthesized disease-class row still rehomes at the native 6.99b rung
    # regardless of the S5a flag (existing B69-v2 behavior, unchanged).
    os.environ[MEGA_DECOMP_ARM_ENV] = "0"
    run, reg, devs, _booty, keeper = _organic_scene()
    sick = _uf("UF-003", "View admins", "network", ["f-a1", "f-a2"],
               synthesized=True, resource="admins")
    pfs = [PF("network", "route:network"), PF("admin", "route:admin")]
    led = OverturnLedger()
    install_ledger(led)
    try:
        tele = run([sick, keeper], devs, pfs, reg)
    finally:
        uninstall_ledger()
    assert tele["rehomed"] == 1
    assert sick.product_feature_id == "admin"
    rungs = {e.rung for e in led.entries if e.kind == "uf" and e.new == "admin"}
    assert rungs == {"6.99b"}   # synthesized → native rung, NOT mega


def test_seg_c_anticase_theta_guard_home_majority_stays_armed():
    # a LEGITIMATELY-homed organic row (home anchor holds a member majority)
    # is never moved, even armed — the θ-guard.
    os.environ[MEGA_DECOMP_ARM_ENV] = "1"
    from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
        run_post_uf_rehome,
    )
    reg = {
        "route:network": _anchor("route:network", "backend/routers/netsec",
                                 "Network"),
        "route:admin": _anchor("route:admin", "backend/routers/admin",
                               "Admin"),
    }
    fl_n1 = Fl("f-n1", "backend/routers/netsec/scan.py", name="scan")
    fl_n2 = Fl("f-n2", "backend/routers/netsec/rules.py", name="rules")
    fl_a1 = Fl("f-a1", "backend/routers/admin/list.py", name="list")
    devs = [Feat("d1", [fl_n1, fl_n2, fl_a1])]
    # 2/3 members are network-owned → home_share 0.667 >= θ → stays.
    legit = _uf("UF-010", "Run network security", "network",
                ["f-n1", "f-n2", "f-a1"])
    keeper = _uf("UF-011", "Manage audit", "network", ["f-n1"])
    pfs = [PF("network", "route:network"), PF("admin", "route:admin")]
    tele = run_post_uf_rehome([legit, keeper], devs, pfs, reg)
    assert tele["rehomed"] == 0
    assert legit.product_feature_id == "network"


# ════════════════════════════════════════════════════════════════════════
# Kill-switch — the grain oracle is byte-identical when the params are OFF
# ════════════════════════════════════════════════════════════════════════


def test_arm_off_grain_oracle_byte_identical():
    ri = _routes_index([
        "src/app/dashboard/page.tsx",
        "backend/routers/admin.py",
        "backend/routers/chat.py",
    ])
    pfs = [PF("dashboard", "route:src/app/dashboard"),
           PF("admin", "hub:vendor/admin")]
    plain = TargetGrainIndex([], pfs, routes_index=ri)      # pre-S5a shape
    off = TargetGrainIndex([], pfs, routes_index=ri,
                           population_roots=False, sibling_tokens=False)
    assert plain.routes_roots == off.routes_roots
    probe = ["src/app/dashboard/page.tsx", "backend/routers/admin.py",
             "backend/routers/chat.py", "some/other/file.ts"]
    assert [plain.grain_of_file(p) for p in probe] == \
           [off.grain_of_file(p) for p in probe]


def test_flag_helper_default_on():
    # SEMANTIC flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): unset ⇒ ON
    # (the _clean_flag autouse fixture guarantees the unset precondition).
    assert mega_decomp_armed() is True
    os.environ[MEGA_DECOMP_ARM_ENV] = "1"
    assert mega_decomp_armed() is True
    os.environ[MEGA_DECOMP_ARM_ENV] = "0"
    assert mega_decomp_armed() is False
