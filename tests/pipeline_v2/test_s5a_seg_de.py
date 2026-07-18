"""S5a Seg D/E — armed trigger UNION (P1 ∨ P2) + mint mass-rung.

NAMED UNITS (finalized experimenter forms, coordinator 2026-07-18):
  Seg D — F4-p50 = P1 (strict-top ∧ share>=0.25 ∧ gq>=3) OR
          P2 (foreign_share>=0.5 ∧ gq>=2). Both constants are existing
          ruler classes; transport-candidate PFs are EXCLUDED (karakeep
          web class).
    * P2-fires-dupwf: a non-top, majority-FOREIGN-mass source decomposes.
    * P1-still-fires-supabase: the strict-top dominant umbrella still fires.
    * anti-cases twenty / cal / documenso: 0 fires armed.
  Seg E — mint floor: flows>=3 ∧ (ufs>=3 OR mass >= 4 * median-dev-mass).
    * k=4 boundary: a 1-UF group at x4.4 median MINTS, at x3.1 does NOT.
    * supabase storage-lock: a x2.4 domain stays OUT (operator single-mint).

Kill-switch: unarmed (env unset) → the pass takes the B24 P1-only gate and
the pure ufs>=3 mint floor, byte-identical.
"""

from __future__ import annotations

import os

import pytest

from faultline.pipeline_v2.mega_pf_nav_rehome import run_mega_pf_nav_rehome
from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.transport_handoff import (
    MEGA_DECOMP_ARM_ENV,
    TargetGrainIndex,
)


class Dev:
    def __init__(self, name, pfid, paths):
        from datetime import datetime, timezone
        self.name = name
        self.uuid = f"dev-{name}"
        self.layer = "developer"
        self.product_feature_id = pfid
        self.paths = list(paths)
        self.member_files = []
        self.flows = []
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
        self.id = name
        self.uuid = f"pf-{name}"
        self.layer = "product"
        self.anchor_id = anchor_id
        self.surface_scope = "product"
        self.paths = []
        self.member_files = []


class Fl:
    def __init__(self, uuid, ep):
        self.uuid = uuid
        self.entry_point_file = ep
        self.name = ""
        self.paths = [ep] if ep else []
        self.line_ranges = [{"path": p, "start_line": 1, "end_line": 10}
                            for p in self.paths]


class UF:
    def __init__(self, id, name, pfid, members):
        self.id = id
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.surface_scope = "product"
        self.routes = []


class Ctx:
    repo_path = "."
    tracked_files: list[str] = []


def _ri(files):
    return [{"file": f, "pattern": "/" + f, "surface_scope": "product"}
            for f in files]


def _anchor(cid, prefix, display):
    return SpineAnchor(canonical_id=cid, key=display.lower(), source="route",
                       display=display, prefixes=(prefix,),
                       sources=frozenset({"route"}))


def _grain(anchors, pfs, ri, transport=()):
    return TargetGrainIndex(anchors, pfs, routes_index=ri,
                            candidate_pf_keys=set(transport),
                            tenant_descent=True, population_roots=True,
                            sibling_tokens=True)


@pytest.fixture(autouse=True)
def _armed():
    os.environ[MEGA_DECOMP_ARM_ENV] = "1"
    yield
    os.environ.pop(MEGA_DECOMP_ARM_ENV, None)


def _run(devs, pfs, ufs, flows, ri, grain, transport=()):
    return run_mega_pf_nav_rehome(
        devs, pfs, ufs, flows, ri, Ctx(), grain_index=grain,
        transport_candidate_units=transport)


# ════════════════════════════════════════════════════════════════════════
# Seg D P2 — a NON-TOP, majority-foreign-mass source decomposes
# ════════════════════════════════════════════════════════════════════════


def _dupwf_scene():
    """'duplicate-workflow' is NOT the board top ('agents' has more UFs), but
    97% of its member mass is FOREIGN domain-dirs (analytics/subscribers)
    that echo hungry sibling PFs → P2 fires (foreign_share>=0.5, gq>=2)."""
    AN = "src/app/analytics"
    SUB = "src/app/subscribers"
    AG = "src/app/agents"
    ri = _ri([f"{AN}/index.tsx", f"{AN}/detail.tsx", f"{AN}/edit.tsx",
              f"{SUB}/index.tsx", f"{SUB}/detail.tsx", f"{SUB}/edit.tsx",
              f"{AG}/index.tsx"])
    flows = [Fl("f-an1", f"{AN}/index.tsx"), Fl("f-an2", f"{AN}/detail.tsx"),
             Fl("f-an3", f"{AN}/edit.tsx"),
             Fl("f-su1", f"{SUB}/index.tsx"), Fl("f-su2", f"{SUB}/detail.tsx"),
             Fl("f-su3", f"{SUB}/edit.tsx"),
             Fl("f-ag1", f"{AG}/index.tsx"), Fl("f-ag2", f"{AG}/index.tsx"),
             Fl("f-ag3", f"{AG}/index.tsx"), Fl("f-c1", "apps/web/core.ts")]
    ufs = [
        # agents = board top (3 UFs) — NOT a source
        UF("UF-ag1", "View agents", "agents", ["f-ag1"]),
        UF("UF-ag2", "Run agents", "agents", ["f-ag2"]),
        UF("UF-ag3", "Edit agents", "agents", ["f-ag3"]),
        # duplicate-workflow (foreign-mass source; each group has 3 flows)
        UF("UF-an", "Browse analytics", "duplicate-workflow",
           ["f-an1", "f-an2", "f-an3"]),
        UF("UF-su", "Manage subscribers", "duplicate-workflow",
           ["f-su1", "f-su2", "f-su3"]),
        # a core journey (no nav-grain vote) the source keeps (I8 guard)
        UF("UF-core", "Duplicate workflow", "duplicate-workflow", ["f-c1"]),
    ]
    devs = [
        Dev("agents", "agents", [f"{AG}/index.tsx"]),
        # source member devs: analytics + subscribers (foreign identity) +
        # a tiny core dev — 55/60 mass is foreign.
        Dev("analytics", "duplicate-workflow",
            [f"{AN}/index.tsx", f"{AN}/detail.tsx"] + [f"{AN}/f{i}.ts"
                                                       for i in range(28)]),
        Dev("subscribers", "duplicate-workflow",
            [f"{SUB}/index.tsx", f"{SUB}/detail.tsx"] + [f"{SUB}/f{i}.ts"
                                                         for i in range(23)]),
        Dev("dupwf-core", "duplicate-workflow", ["apps/web/core.ts"]),
    ]
    pfs = [
        PF("agents", "route:src/app/agents"),
        PF("duplicate-workflow", "ws:apps/web"),
        # hungry siblings under the SAME routes root (same-app rail) at the
        # group grain — their journeys were annexed by duplicate-workflow.
        PF("analytics", "route:src/app/analytics"),
        PF("subscribers", "route:src/app/subscribers"),
    ]
    anchors = [_anchor("route:src/app/agents", "src/app/agents", "Agents")]
    return devs, pfs, ufs, flows, ri, _grain(anchors, pfs, ri)


def test_seg_d_p2_fires_on_non_top_dupwf():
    devs, pfs, ufs, flows, ri, grain = _dupwf_scene()
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == ["duplicate-workflow"]
    assert tele.get("fired_prong") == "P2"
    # the foreign-domain journeys bridge to their hungry siblings
    homes = {u.id: u.product_feature_id for u in ufs}
    assert homes["UF-an"] == "analytics"
    assert homes["UF-su"] == "subscribers"
    # agents (board top) is untouched
    assert homes["UF-ag1"] == homes["UF-ag2"] == homes["UF-ag3"] == "agents"


def test_seg_d_p2_transport_candidate_excluded():
    # the same source, but declared a transport candidate (karakeep web
    # class) → excluded from decomposition, 0 fires.
    devs, pfs, ufs, flows, ri, grain = _dupwf_scene()
    grain = _grain([_anchor("route:src/app/agents", "src/app/agents",
                            "Agents")], pfs, ri, transport=["apps/web"])
    tele = _run(devs, pfs, ufs, flows, ri, grain, transport=["apps/web"])
    assert tele["triggered"] == []


# ════════════════════════════════════════════════════════════════════════
# Seg D P1 — the strict-top dominant umbrella still fires (armed)
# ════════════════════════════════════════════════════════════════════════


def _supabase_p1_scene():
    # the ratified B24 tenant shape: project/[ref]/<area> keys <area>.
    ROOT = "apps/studio/pages"
    S = f"{ROOT}/project/[ref]/settings"
    L = f"{ROOT}/project/[ref]/logs"
    D = f"{ROOT}/project/[ref]/database"
    API = f"{ROOT}/api/platform/projects"
    ri = _ri([f"{S}/a.tsx", f"{L}/a.tsx", f"{D}/a.tsx", f"{D}/b.tsx",
              f"{API}/index.ts"])
    flows = [Fl("f-s1", f"{S}/a.tsx"), Fl("f-s2", f"{S}/a.tsx"),
             Fl("f-l1", f"{L}/a.tsx"), Fl("f-l2", f"{L}/a.tsx"),
             Fl("f-d1", f"{D}/a.tsx"), Fl("f-d2", f"{D}/b.tsx"),
             Fl("f-d3", f"{D}/a.tsx"), Fl("f-pc", f"{API}/index.ts"),
             Fl("f-o1", "apps/other/x.ts")]
    ufs = [
        UF("UF-s1", "Manage settings", "projects", ["f-s1"]),
        UF("UF-s2", "Manage API", "projects", ["f-s2"]),
        UF("UF-l1", "View logs", "projects", ["f-l1"]),
        UF("UF-l2", "Explore logs", "projects", ["f-l2"]),
        UF("UF-d1", "Manage tables", "projects", ["f-d1"]),
        UF("UF-d2", "Manage backups", "projects", ["f-d2"]),
        UF("UF-d3", "Manage triggers", "projects", ["f-d3"]),
        UF("UF-pc", "Project overview", "projects", ["f-pc"]),  # core → stays
        UF("UF-o1", "Other", "other", ["f-o1"]),
    ]
    devs = [Dev("proj", "projects",
                [f"{S}/a.tsx", f"{L}/a.tsx", f"{D}/a.tsx", f"{D}/b.tsx",
                 f"{API}/index.ts"]),
            Dev("other", "other", ["apps/other/x.ts"])]
    pfs = [PF("projects", f"route:{API}"),
           PF("settings", f"route:{S}"),
           PF("logs", f"route:{L}"),
           PF("other", "route:apps/other")]
    anchors = [_anchor(f"route:{API}", API, "Projects"),
               _anchor(f"route:{S}", S, "Settings"),
               _anchor(f"route:{L}", L, "Logs")]
    return devs, pfs, ufs, flows, ri, _grain(anchors, pfs, ri)


def test_seg_d_p1_still_fires_dominant_umbrella():
    devs, pfs, ufs, flows, ri, grain = _supabase_p1_scene()
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == ["projects"]
    assert tele.get("fired_prong") == "P1"
    homes = {u.id: u.product_feature_id for u in ufs}
    assert homes["UF-s1"] == "settings" and homes["UF-l1"] == "logs"
    assert homes["UF-o1"] == "other"   # conservation


# ════════════════════════════════════════════════════════════════════════
# Seg D anti-cases — 0 fires armed
# ════════════════════════════════════════════════════════════════════════


def test_seg_d_anticase_healthy_documenso_no_fire():
    # a healthy top PF: 1 nav group, 0 foreign mass → neither prong.
    ROOT = "apps/web/app"
    ri = _ri([f"{ROOT}/team/page.tsx", f"{ROOT}/team/settings.tsx"])
    flows = [Fl("f-t1", f"{ROOT}/team/page.tsx"),
             Fl("f-t2", f"{ROOT}/team/settings.tsx"),
             Fl("f-o1", "apps/web/other.ts")]
    ufs = [UF("UF-t1", "Manage team", "team", ["f-t1"]),
           UF("UF-t2", "Team settings", "team", ["f-t2"]),
           UF("UF-o1", "Other", "other", ["f-o1"])]
    devs = [Dev("team", "team",
                [f"{ROOT}/team/page.tsx", f"{ROOT}/team/settings.tsx"]),
            Dev("other", "other", ["apps/web/other.ts"])]
    pfs = [PF("team", f"route:{ROOT}/team"), PF("other", "route:apps/web")]
    grain = _grain([_anchor(f"route:{ROOT}/team", f"{ROOT}/team", "Team")],
                   pfs, ri)
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == []


def test_seg_d_anticase_twenty_workflows_no_fire_armed():
    # twenty 'workflows': board-top by UFs but its mass lives in deep
    # internal modules (no nav routes) → gq=0, foreign_share=0 → neither
    # prong fires even armed.
    M = "packages/twenty-front/src/modules/workflow"
    ri = _ri(["app/settings/s.tsx"])
    flows = [Fl("f-w1", f"{M}/a.ts"), Fl("f-w2", f"{M}/b.ts"),
             Fl("f-w3", f"{M}/c.ts"), Fl("f-s1", "app/settings/s.tsx")]
    ufs = [UF("UF-w1", "Build workflows", "workflows", ["f-w1"]),
           UF("UF-w2", "Run workflows", "workflows", ["f-w2"]),
           UF("UF-w3", "Trigger workflows", "workflows", ["f-w3"]),
           UF("UF-s1", "Settings", "settings", ["f-s1"])]
    devs = [Dev("workflow", "workflows",
                [f"{M}/a.ts", f"{M}/b.ts", f"{M}/c.ts"]),
            Dev("settings", "settings", ["app/settings/s.tsx"])]
    pfs = [PF("workflows", "ws:packages/twenty-front"),
           PF("settings", "route:app/settings")]
    tele = _run(devs, pfs, ufs, flows, ri, _grain([], pfs, ri))
    assert tele["triggered"] == []


def test_seg_d_anticase_cal_bookings_no_fire_armed():
    # cal 'bookings': not majority-foreign, and the trpc top is a transport
    # candidate (excluded) → nothing fires armed.
    B = "apps/web/app/bookings"
    ri = _ri([f"{B}/upcoming.tsx", f"{B}/past.tsx"])
    flows = [Fl("f-b1", f"{B}/upcoming.tsx"), Fl("f-b2", f"{B}/past.tsx"),
             Fl("f-t1", "apps/web/trpc/a.ts")]
    ufs = [UF("UF-b1", "Browse bookings", "bookings", ["f-b1"]),
           UF("UF-b2", "Filter bookings", "bookings", ["f-b2"]),
           UF("UF-t1", "trpc", "trpc", ["f-t1"])]
    devs = [Dev("bookings", "bookings",
                [f"{B}/upcoming.tsx", f"{B}/past.tsx"]),
            Dev("trpc", "trpc", ["apps/web/trpc/a.ts"])]
    pfs = [PF("bookings", f"route:{B}"), PF("trpc", "ws:apps/web/trpc")]
    grain = _grain([], pfs, ri, transport=["apps/web/trpc"])
    tele = _run(devs, pfs, ufs, flows, ri, grain, transport=["apps/web/trpc"])
    assert tele["triggered"] == []
    assert all(u.product_feature_id == "bookings"
               for u in ufs if u.id.startswith("UF-b"))


# ════════════════════════════════════════════════════════════════════════
# Seg E — mint mass-rung (k=4) + supabase storage-lock
# ════════════════════════════════════════════════════════════════════════


def _mass_rung_scene(chat_extra_files):
    """netsec fires P1 (admin/chat/oauth qualifying groups). 'chat' is a
    1-UF domain (below the UF mint floor) but a massive dev — Seg E mints it
    when its apportioned mass clears k=4 * the board median dev mass."""
    A = "backend/routers/admin.py"
    C = "backend/routers/chat.py"
    O = "backend/routers/oauth.py"
    ri = _ri([A, C, O])
    flows = [Fl("f-a1", A), Fl("f-a2", A), Fl("f-a3", A),
             Fl("f-c1", C), Fl("f-c2", C), Fl("f-c3", C),
             Fl("f-o1", O), Fl("f-o2", O),
             Fl("f-b1", "app/b1.ts"), Fl("f-b2", "app/b2.ts")]
    ufs = [
        UF("UF-a1", "Manage admins", "netsec", ["f-a1"]),
        UF("UF-a2", "Get admin chats", "netsec", ["f-a2"]),
        UF("UF-a3", "Post admin", "netsec", ["f-a3"]),
        # chat = ONE UF with 3 flows (below UF floor, above flow floor)
        UF("UF-c1", "Manage chat", "netsec", ["f-c1", "f-c2", "f-c3"]),
        UF("UF-o1", "Connect oauth", "netsec", ["f-o1"]),
        UF("UF-o2", "Revoke oauth", "netsec", ["f-o2"]),
        UF("UF-b1", "B one", "billing", ["f-b1"]),
        UF("UF-b2", "B two", "billing", ["f-b2"]),
    ]
    # median dev mass: the small devs sit at 10 paths → median 10.
    small = [Dev(f"s{i}", "billing", [f"pkg{i}/f{j}.ts" for j in range(10)])
             for i in range(5)]
    admin_dev = Dev("api-admin", "netsec", [A, O])
    # chat dev: token 'chat' echoes the chat group; its mass is the knob.
    chat_dev = Dev("chat", "netsec",
                   [C] + [f"backend/routers/chat/f{i}.py"
                          for i in range(chat_extra_files)])
    devs = [admin_dev, chat_dev] + small
    pfs = [PF("netsec", "route:backend/routers/netsec"),
           PF("billing", "route:app")]
    return devs, pfs, ufs, flows, ri, _grain([], pfs, ri)


def test_seg_e_mass_rung_mints_at_x44():
    # chat dev = 44 paths, median 10 → x4.4 >= k=4 → the 1-UF chat MINTS.
    devs, pfs, ufs, flows, ri, grain = _mass_rung_scene(chat_extra_files=43)
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == ["netsec"]
    minted = {m["cid"] for m in tele["mints"]}
    assert "route:backend/routers/chat" in minted
    assert "route:backend/routers/admin" in minted   # UF-floor mint too
    # the 1-UF chat journey rehomed onto its mass-minted PF
    chat_uf = next(u for u in ufs if u.id == "UF-c1")
    assert chat_uf.product_feature_id != "netsec"


def test_seg_e_mass_rung_holds_below_k4_storage_lock():
    # chat dev = 31 paths, median 10 → x3.1 < k=4 → the 1-UF chat STAYS
    # (the supabase 'storage' x2.4 single-mint lock class).
    devs, pfs, ufs, flows, ri, grain = _mass_rung_scene(chat_extra_files=30)
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    minted = {m["cid"] for m in tele["mints"]}
    assert "route:backend/routers/chat" not in minted
    chat_uf = next(u for u in ufs if u.id == "UF-c1")
    assert chat_uf.product_feature_id == "netsec"   # stayed
    # admin (UF-floor) still mints — the mass rung is additive, not a gate
    assert "route:backend/routers/admin" in minted


def test_seg_e_off_pure_uf_floor():
    # unarmed: the mass rung is inert; the 1-UF chat can NEVER mint even at
    # x4.4 (byte-identical B24 floor: ufs>=3 AND flows>=3).
    os.environ[MEGA_DECOMP_ARM_ENV] = "0"
    devs, pfs, ufs, flows, ri, _grain_on = _mass_rung_scene(chat_extra_files=43)
    # unarmed grain (no population roots) → backend/routers not a root →
    # netsec gets no groups → the pass never fires at all.
    grain = TargetGrainIndex([], pfs, routes_index=ri, tenant_descent=True,
                             population_roots=False, sibling_tokens=False)
    tele = _run(devs, pfs, ufs, flows, ri, grain)
    assert tele["triggered"] == []
    assert all(u.product_feature_id == "netsec"
               for u in ufs if u.id.startswith("UF-c"))
