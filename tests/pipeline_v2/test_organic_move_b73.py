"""B73 — organic-move: strict+gated organic UF re-home at the 6.99b rail.

Operator ratification 2026-07-17 + fork-A ruling 2026-07-19: armed
(``FAULTLINE_ORGANIC_MOVE=1``) the rule REPLACES the S5a mega-organic
handling of the rail's organic candidates with the ratified constants
(``home_share==0.0 ∧ rival_share>=0.8`` inclusive) + the product→dev
direction-gate; PURE arbiter moves (rung ``organic-move``) — no rename,
no fold, and NO I8 orphan-guard (explicit ruling: 7/14 census moves are
the sole UF of their from-PF — the disease IS a mis-homed sole journey).

MOVE shapes are the CURRENT (KS32, 2026-07-19) census rows — the probe's
31-row list is the OLD world (stale-mandate law; re-census 16/10 boards):
  * typebot UF-046 'Create, duplicate, and delete typebots'
    space(schema:)→typebots(route:) — product→product, SOLE-UF from-PF.
  * typebot UF-005 blink family openai-block→blink-block (ws-pkg→ws-pkg).
  * cal.com UF-107 trpc→platform-libraries — dev→dev ALLOWED (spec
    exhibit; ws:packages→ws:packages).
  * kan UF-017 email(ws:packages)→auth(route:) — dev→product allowed.
BOUNDARY: rival_share == 0.8 EXACTLY moves (>= inclusive, as ratified);
synthetic 0.0/0.79 does NOT (organic_below_strict).
DIRECTION-GATE (blocked, the un-burial pair): hoppscotch UF-012
teams(route:)→hoppscotch-backend(ws:packages) and reactive-resume UF-016
builder(route:)→docx(ws:packages) — блоковані ON-armed навіть під
default-ON mega (the mega path buried them on the KS32 boards; fork-A
un-buries). ANTI: mega-решта неторкана — unset flag ⇒ the mega-armed
organic path and the synthesized disease-class path behave exactly as
main; determinism ×2 + sorted uf-id apply order.
"""

from __future__ import annotations

import copy

from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
    organic_move_enabled,
    run_post_uf_rehome,
)


class Fl:
    def __init__(self, uuid, ep, name):
        self.uuid = uuid
        self.entry_point_file = ep
        self.name = name


class Dev:
    def __init__(self, name, flows):
        self.name = name
        self.flows = flows


class PF:
    def __init__(self, name, anchor_id, display=None):
        self.name = name
        self.id = name
        self.anchor_id = anchor_id
        self.display_name = display or name.title()
        self.layer = "product"


class UF:
    _n = 0

    def __init__(self, name, pfid, members, *, synthesized=False,
                 resource=None, uid=None):
        UF._n += 1
        self.id = uid or f"UF-{UF._n:03d}"
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.synthesized = synthesized
        self.resource = resource


def _anchor(cid, prefixes):
    return SpineAnchor(
        canonical_id=cid, key=cid.split(":", 1)[-1], source="route",
        display=cid.split(":", 1)[-1].title(), prefixes=tuple(prefixes))


def _on(monkeypatch):
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")


# ── Scene builders (current-census shapes) ───────────────────────────────


def _typebot_scene():
    """typebot UF-046: organic row homed on PF 'space' (schema: anchor,
    zero member match) while PF 'typebots' (route:) matches every member —
    0.0/1.0, product→product, and 'space' holds NO other UF (sole-UF
    from-PF: the no-orphan-guard exception is load-bearing here)."""
    UF._n = 0
    registry = {
        "schema:space": _anchor(
            "schema:space", ["packages/schemas/features/space"]),
        "route:typebots": _anchor(
            "route:typebots", ["apps/builder/src/pages/typebots"]),
    }
    pf_space = PF("space", "schema:space")
    pf_typebots = PF("typebots", "route:typebots")
    fls = [
        Fl("f-t1", "apps/builder/src/pages/typebots/index.tsx",
           "list-typebots-flow"),
        Fl("f-t2", "apps/builder/src/pages/typebots/create.tsx",
           "create-typebot-flow"),
        Fl("f-t3", "apps/builder/src/pages/typebots/dup.tsx",
           "duplicate-typebot-flow"),
    ]
    devs = [Dev("d1", fls)]
    mover = UF("Create, duplicate, and delete typebots", "space",
               ["f-t1", "f-t2", "f-t3"], uid="UF-046")
    keeper = UF("Browse typebot gallery", "typebots", ["f-t1"], uid="UF-100")
    return registry, [pf_space, pf_typebots], devs, mover, keeper


def _burial_scene():
    """hoppscotch UF-012: organic row on product PF 'teams' (route:) whose
    members all live in ws:packages/hoppscotch-backend — the mega path
    moved (BURIED) it on the KS32 boards; the B73 direction-gate blocks."""
    UF._n = 0
    registry = {
        "route:teams": _anchor(
            "route:teams",
            ["packages/hoppscotch-sh-admin/src/pages/teams"]),
        "ws:packages/hoppscotch-backend": _anchor(
            "ws:packages/hoppscotch-backend",
            ["packages/hoppscotch-backend"]),
    }
    pf_teams = PF("teams", "route:teams")
    pf_backend = PF("hoppscotch-backend", "ws:packages/hoppscotch-backend")
    fls = [
        Fl("f-m1", "packages/hoppscotch-backend/src/mock/serve.ts",
           "serve-mock-flow"),
        Fl("f-m2", "packages/hoppscotch-backend/src/mock/logs.ts",
           "inspect-logs-flow"),
        Fl("f-m3", "packages/hoppscotch-backend/src/admin/infra.ts",
           "manage-infra-flow"),
        Fl("f-tm", "packages/hoppscotch-sh-admin/src/pages/teams/index.vue",
           "manage-teams-flow"),
    ]
    devs = [Dev("d1", fls)]
    buried = UF("Manage and serve mock servers", "teams",
                ["f-m1", "f-m2"], uid="UF-012")
    # DISJOINT member set (else the mega-armed anti-case would fold, not
    # rehome — the fold rungs fire on member overlap).
    other = UF("Manage backend infra", "hoppscotch-backend", ["f-m3"],
               uid="UF-200")
    # a second teams-native row so the NATIVE mega apply path (which keeps
    # its I8 orphan-guard) can rehome in the unset-flag anti-case; the
    # organic-move lane deliberately has NO such guard (fork-A ruling).
    teams_native = UF("Manage teams", "teams", ["f-tm"], uid="UF-201")
    return registry, [pf_teams, pf_backend], devs, buried, other, teams_native


def _devdev_scene():
    """cal.com UF-107: trpc(ws:packages)→platform-libraries(ws:packages) —
    dev→dev is ALLOWED (spec exhibit)."""
    UF._n = 0
    registry = {
        "ws:packages/trpc": _anchor(
            "ws:packages/trpc", ["packages/trpc"]),
        "ws:packages/platform/libraries": _anchor(
            "ws:packages/platform/libraries", ["packages/platform/libraries"]),
    }
    pf_trpc = PF("trpc", "ws:packages/trpc")
    pf_lib = PF("platform-libraries", "ws:packages/platform/libraries")
    fls = [
        Fl("f-p1", "packages/platform/libraries/src/getBookings.ts",
           "get-bookings-flow"),
        Fl("f-p2", "packages/platform/libraries/src/getBookingInfo.ts",
           "get-booking-info-flow"),
    ]
    devs = [Dev("d1", fls)]
    mover = UF("GetAllUserBookings getBookingInfo", "trpc",
               ["f-p1", "f-p2"], uid="UF-107")
    keeper = UF("Manage tRPC routers", "trpc", ["f-x"], uid="UF-300")
    return registry, [pf_trpc, pf_lib], devs, mover, keeper


# ── MOVE shapes ──────────────────────────────────────────────────────────


def test_move_typebot_uf046_sole_uf_from_pf(monkeypatch):
    """Census MOVE + the NO-orphan-guard exception (fork-A ruling): 'space'
    holds ONLY the mover — it still moves (the disease IS a mis-homed sole
    journey); pure move: name unchanged, no fold, arbiter-only write."""
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    ufs = [mover, keeper]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert mover.product_feature_id == "typebots"
    assert mover.name == "Create, duplicate, and delete typebots"  # no rename
    assert mover in ufs                                            # no fold
    assert tele["organic_moved"] == 1
    assert tele["organic_moves"] == [{
        "uf": "UF-046", "name": "Create, duplicate, and delete typebots",
        "from": "space", "to": "typebots",
        "home_share": 0.0, "rival_share": 1.0,
    }]
    # native counters untouched by the organic lane:
    assert tele["rehomed"] == 0 and tele["folded"] == 0
    assert tele["orphan_guarded"] == 0


def test_move_devdev_allowed_cal_uf107(monkeypatch):
    """dev→dev (ws-pkg→ws-pkg) passes the direction-gate (spec exhibit
    cal UF-107 trpc→platform-libraries)."""
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _devdev_scene()
    ufs = [mover, keeper]
    tele = run_post_uf_rehome(ufs, devs, pfs, registry)
    assert mover.product_feature_id == "platform-libraries"
    assert tele["organic_moved"] == 1
    assert tele.get("organic_blocked_direction", 0) == 0


def test_move_devproduct_allowed_kan_uf017(monkeypatch):
    """dev→product (ws:packages/email → route: auth) passes the gate —
    only product→dev demotion is blocked."""
    _on(monkeypatch)
    UF._n = 0
    registry = {
        "ws:packages/email": _anchor(
            "ws:packages/email", ["packages/email"]),
        "route:auth": _anchor(
            "route:auth", ["apps/web/src/pages/api/auth"]),
    }
    pfs = [PF("email", "ws:packages/email"),
           PF("auth", "route:auth")]
    fls = [Fl("f-a1", "apps/web/src/pages/api/auth/signin.ts",
              "init-auth-flow")]
    devs = [Dev("d1", fls)]
    mover = UF("Initialize authentication flow", "email", ["f-a1"],
               uid="UF-017")
    other = UF("Manage auth providers", "auth", ["f-a1"], uid="UF-400")
    tele = run_post_uf_rehome([mover, other], devs, pfs, registry)
    assert mover.product_feature_id == "auth"
    assert tele["organic_moved"] == 1


def test_move_wspkg_family_typebot_uf005(monkeypatch):
    """ws-pkg→ws-pkg block family (openai-block→blink-block) moves —
    both platform-layer, gate silent."""
    _on(monkeypatch)
    UF._n = 0
    registry = {
        "ws:packages/forge/blocks/openai": _anchor(
            "ws:packages/forge/blocks/openai",
            ["packages/forge/blocks/openai"]),
        "ws:packages/forge/blocks/blink": _anchor(
            "ws:packages/forge/blocks/blink",
            ["packages/forge/blocks/blink"]),
    }
    pfs = [PF("openai-block", "ws:packages/forge/blocks/openai"),
           PF("blink-block", "ws:packages/forge/blocks/blink")]
    fls = [Fl("f-b1", "packages/forge/blocks/blink/src/user-data.ts",
              "browse-blink-user-data-flow"),
           Fl("f-b0", "packages/forge/blocks/openai/src/index.ts",
              "configure-openai-flow")]
    devs = [Dev("d1", fls)]
    mover = UF("Browse and filter Blink user data", "openai-block",
               ["f-b1"], uid="UF-005")
    other = UF("Configure OpenAI block", "openai-block", ["f-b0"],
               uid="UF-500")
    tele = run_post_uf_rehome([mover, other], devs, pfs, registry)
    assert mover.product_feature_id == "blink-block"
    assert tele["organic_moved"] == 1


# ── BOUNDARY (0.8 inclusive; 0.79 stays) ─────────────────────────────────


def _boundary_scene(n_match: int, n_total: int):
    UF._n = 0
    registry = {
        "route:home": _anchor("route:home", ["apps/web/src/pages/home"]),
        "route:rival": _anchor("route:rival", ["apps/web/src/pages/rival"]),
    }
    pfs = [PF("home", "route:home"), PF("rival", "route:rival")]
    fls, members = [], []
    for i in range(n_total):
        base = "rival" if i < n_match else "elsewhere"
        fl = Fl(f"f-{i}", f"apps/web/src/pages/{base}/e{i}.tsx",
                f"flow-{i}")
        fls.append(fl)
        members.append(fl.uuid)
    home_fl = Fl("f-h", "apps/web/src/pages/home/h.tsx", "home-flow")
    fls.append(home_fl)
    devs = [Dev("d1", fls)]
    mover = UF("Browse rival things", "home", members, uid="UF-001")
    other = UF("Home journey", "home", ["f-h"], uid="UF-900")
    return registry, pfs, devs, mover, other


def test_boundary_rival_exactly_0_8_moves(monkeypatch):
    """rival_share == 0.8 EXACTLY (4/5) moves — >= inclusive, as ratified
    (the langfuse UF-008 0.8-boundary class of the probe)."""
    _on(monkeypatch)
    registry, pfs, devs, mover, other = _boundary_scene(4, 5)
    tele = run_post_uf_rehome([mover, other], devs, pfs, registry)
    assert mover.product_feature_id == "rival"
    assert tele["organic_moves"][0]["rival_share"] == 0.8


def test_anticase_boundary_0_79_stays(monkeypatch):
    """Synthetic 0.0/0.79 (79/100) — below the ratified floor: recorded as
    below-strict, NEVER moved (the band the mega rung would have moved)."""
    _on(monkeypatch)
    registry, pfs, devs, mover, other = _boundary_scene(79, 100)
    tele = run_post_uf_rehome([mover, other], devs, pfs, registry)
    assert mover.product_feature_id == "home"
    assert tele.get("organic_moved", 0) == 0
    assert tele["organic_below_strict"] == 1


# ── DIRECTION-GATE (the un-burial pair, ON-armed under default mega) ─────


def test_direction_gate_blocks_hoppscotch_uf012(monkeypatch):
    """product→dev BLOCKED: teams(route:)→hoppscotch-backend(ws:packages).
    ON-armed with mega at its DEFAULT (armed, KS32 flip) — the exact
    un-burial shape: the mega path moved this row on the boards; fork-A
    keeps it home and journals the block."""
    _on(monkeypatch)
    monkeypatch.delenv("FAULTLINE_MEGA_DECOMP_ARM", raising=False)  # default ON
    registry, pfs, devs, buried, other, teams_native = _burial_scene()
    tele = run_post_uf_rehome([buried, other, teams_native], devs, pfs,
                              registry)
    assert buried.product_feature_id == "teams"          # NOT buried
    assert tele.get("organic_moved", 0) == 0
    assert tele["organic_blocked_direction"] == 1
    assert tele["organic_blocked_rows"] == [{
        "uf": "UF-012", "name": "Manage and serve mock servers",
        "from": "teams", "to": "hoppscotch-backend",
        "from_layer": "product", "to_layer": "platform",
        "reason": "direction",
    }]


def test_direction_gate_blocks_reactive_resume_uf016(monkeypatch):
    """The second un-burial shape: builder(route:)→docx(ws:packages)."""
    _on(monkeypatch)
    UF._n = 0
    registry = {
        "route:builder": _anchor(
            "route:builder", ["apps/web/src/routes/builder"]),
        "ws:packages/docx": _anchor(
            "ws:packages/docx", ["packages/docx"]),
    }
    pfs = [PF("builder", "route:builder"), PF("docx", "ws:packages/docx")]
    fls = [Fl("f-d1", "packages/docx/src/export.ts", "export-docx-flow"),
           Fl("f-r1", "apps/web/src/routes/builder/page.tsx",
              "build-resume-flow")]
    devs = [Dev("d1", fls)]
    buried = UF("Export as DOCX file", "builder", ["f-d1"], uid="UF-016")
    other = UF("Build resume", "builder", ["f-r1"], uid="UF-600")
    tele = run_post_uf_rehome([buried, other], devs, pfs, registry)
    assert buried.product_feature_id == "builder"
    assert tele["organic_blocked_direction"] == 1


def test_ws_apps_is_product_layer(monkeypatch):
    """ws:apps/* is PRODUCT (workspace_shell_roots — the spine ws-app
    class): a move from ws:apps/web into ws:packages/* is product→dev and
    blocks; the shell-root data comes from spine-anchor-vocab.yaml."""
    _on(monkeypatch)
    UF._n = 0
    registry = {
        "ws:apps/web": _anchor("ws:apps/web", ["apps/web"]),
        "ws:packages/core": _anchor("ws:packages/core", ["packages/core"]),
    }
    pfs = [PF("web", "ws:apps/web"), PF("core", "ws:packages/core")]
    fls = [Fl("f-c1", "packages/core/src/thing.ts", "thing-flow"),
           Fl("f-w1", "apps/web/src/index.tsx", "web-flow")]
    devs = [Dev("d1", fls)]
    mover = UF("Do the thing", "web", ["f-c1"], uid="UF-001")
    other = UF("Web journey", "web", ["f-w1"], uid="UF-700")
    tele = run_post_uf_rehome([mover, other], devs, pfs, registry)
    assert mover.product_feature_id == "web"
    assert tele["organic_blocked_direction"] == 1


# ── ANTI: unset flag ⇒ branch un-entered (mega-решта неторкана) ─────────


def test_anticase_unset_flag_mega_armed_path_unchanged(monkeypatch):
    """Kill-switch pair at the unit grain: the SAME burial scene, flag
    UNSET, mega at its armed default — the organic candidate moves via the
    mega rung exactly as main does today (b24_class journaled). This is
    the replaced behavior fork-A supersedes ONLY under the flag."""
    monkeypatch.delenv("FAULTLINE_ORGANIC_MOVE", raising=False)
    monkeypatch.delenv("FAULTLINE_MEGA_DECOMP_ARM", raising=False)
    registry, pfs, devs, buried, other, teams_native = _burial_scene()
    tele = run_post_uf_rehome([buried, other, teams_native], devs, pfs,
                              registry)
    assert buried.product_feature_id == "hoppscotch-backend"  # main behavior
    assert tele.get("organic_rehomed", 0) == 1
    assert tele.get("organic_moved", 0) == 0
    assert "organic_moves" not in tele
    assert "organic_blocked_rows" not in tele


def test_anticase_unset_flag_mega_off_telemetry_only(monkeypatch):
    """Flag unset + mega kill-switched ⇒ telemetry-only candidate, exactly
    the pre-flip law (byte-parity with the B69-v2 anti-case)."""
    monkeypatch.delenv("FAULTLINE_ORGANIC_MOVE", raising=False)
    monkeypatch.setenv("FAULTLINE_MEGA_DECOMP_ARM", "0")
    registry, pfs, devs, buried, other, teams_native = _burial_scene()
    tele = run_post_uf_rehome([buried, other, teams_native], devs, pfs,
                              registry)
    assert buried.product_feature_id == "teams"
    assert tele["organic_candidates"] == 1
    assert tele.get("organic_moved", 0) == 0


def test_anticase_synthesized_disease_class_untouched(monkeypatch):
    """ON-armed, the native synthesized-row path (rehome + C′ rename +
    orphan-guard) is untouched — the organic lane is additive-separate.
    Papermark-shaped synthesized seed still rehomes AND renames."""
    _on(monkeypatch)
    UF._n = 0
    registry = {
        "route:faq": _anchor("route:faq", ["app/api/faqs"]),
        "route:dataroom": _anchor("route:dataroom", ["pages/datarooms"]),
    }
    pfs = [PF("faqs", "route:faq"), PF("datarooms", "route:dataroom")]
    fls = [
        Fl("f-l1", "pages/datarooms/index.tsx", "list-datarooms-flow"),
        Fl("f-l2", "pages/datarooms/d/index.tsx", "view-dataroom-flow"),
        Fl("f-fq", "app/api/faqs/route.ts", "retrieve-faqs-flow"),
        Fl("f-k1", "pages/datarooms/settings.tsx", "dataroom-settings-flow"),
    ]
    devs = [Dev("d1", fls)]
    sick = UF("View faqs", "faqs", ["f-l1", "f-l2"],
              synthesized=True, resource="datarooms")
    real = UF("Manage dataroom FAQs", "faqs", ["f-fq"])
    # DISJOINT members (member overlap would fold the seed instead of the
    # rehome+rename this anti-case pins).
    keeper = UF("Create and manage data rooms", "datarooms", ["f-k1"])
    tele = run_post_uf_rehome([sick, real, keeper], devs, pfs, registry)
    assert sick.product_feature_id == "datarooms"
    assert sick.name == "View datarooms"          # C′ rename intact
    assert tele["rehomed"] == 1 and tele["renamed"] == 1
    assert tele.get("organic_moved", 0) == 0


# ── Determinism ──────────────────────────────────────────────────────────


def _two_mover_scene():
    UF._n = 0
    registry = {
        "schema:space": _anchor(
            "schema:space", ["packages/schemas/features/space"]),
        "route:typebots": _anchor(
            "route:typebots", ["apps/builder/src/pages/typebots"]),
        "route:domains": _anchor(
            "route:domains", ["apps/builder/src/pages/domains"]),
    }
    pfs = [PF("space", "schema:space"), PF("typebots", "route:typebots"),
           PF("domains", "route:domains")]
    fls = [
        Fl("f-t1", "apps/builder/src/pages/typebots/index.tsx", "t-flow"),
        Fl("f-d1", "apps/builder/src/pages/domains/index.tsx", "d-flow"),
    ]
    devs = [Dev("d1", fls)]
    # Board order DELIBERATELY reversed vs id order (apply must sort by id).
    m2 = UF("Manage custom domains", "space", ["f-d1"], uid="UF-075")
    m1 = UF("Create typebots", "space", ["f-t1"], uid="UF-046")
    o1 = UF("Browse typebots", "typebots", ["f-t1"], uid="UF-800")
    o2 = UF("Browse domains", "domains", ["f-d1"], uid="UF-801")
    return registry, pfs, devs, [m2, m1, o1, o2]


def test_determinism_two_runs_and_sorted_apply(monkeypatch):
    _on(monkeypatch)
    a = _two_mover_scene()
    b = _two_mover_scene()
    tele_a = run_post_uf_rehome(a[3], a[2], a[1], a[0])
    tele_b = run_post_uf_rehome(b[3], b[2], b[1], b[0])
    assert copy.deepcopy(tele_a) == copy.deepcopy(tele_b)
    assert [(u.name, u.product_feature_id) for u in a[3]] == \
        [(u.name, u.product_feature_id) for u in b[3]]
    # sorted uf-id apply order (board order was reversed):
    assert [m["uf"] for m in tele_a["organic_moves"]] == ["UF-046", "UF-075"]
    assert tele_a["organic_moved"] == 2


# ── it2 guards (Soc0 UF-051 keyed panel refutation, mandate 2026-07-19) ──


def _soc0_uf051_scene():
    """The refutation shape, minimal: ALL member flows of the organic row
    enter ``backend/routers/admin.py`` — which is the home PF
    network-security's OWN role='anchor' member file (confidence 1.0,
    primary) yet sits OUTSIDE every ``route:network-security`` registry
    prefix, so the breadth ruler reads home_share=0.0 (FALSE ZERO); the
    rival 'chat' registry anchor covers backend/routers/ → rival=1.0."""
    UF._n = 0
    registry = {
        "route:network-security": _anchor(
            "route:network-security",
            ["frontend/src/pages/network-security"]),
        "route:chat": _anchor("route:chat", ["backend/routers"]),
    }
    pf_home = PF("network-security", "route:network-security")
    pf_home.member_files = [
        {"path": "backend/routers/admin.py", "role": "anchor",
         "confidence": 1.0, "primary": True},
        {"path": "frontend/src/api/admin.ts", "role": "closure",
         "confidence": 0.5, "primary": True},
    ]
    pf_chat = PF("chat", "route:chat")
    fls = [
        Fl("f-a1", "backend/routers/admin.py", "admin-mock-data-flow"),
        Fl("f-a2", "backend/routers/admin.py", "admin-migrate-flow"),
        Fl("f-a3", "backend/routers/admin.py", "admin-chats-flow"),
        Fl("f-ns", "frontend/src/pages/network-security/index.tsx",
           "browse-network-security-flow"),
    ]
    devs = [Dev("d1", fls)]
    sick = UF("Manage admin operations and mock data", "network-security",
              ["f-a1", "f-a2", "f-a3"], uid="UF-051")
    keeper = UF("Browse network security", "network-security", ["f-ns"],
                uid="UF-900")
    return registry, [pf_home, pf_chat], devs, sick, keeper


def test_it2_uf051_prior_hold_blocks(monkeypatch):
    """Guard 2 — PRIOR-HOLD: the B24 nav stage held UF-051
    (cross_app_target); the organic lane must NOT override the hold with
    its weaker breadth evidence. First-hit-wins: prior-hold fires before
    the anchor-overlap check."""
    _on(monkeypatch)
    registry, pfs, devs, sick, keeper = _soc0_uf051_scene()
    tele = run_post_uf_rehome(
        [sick, keeper], devs, pfs, registry,
        mega_holds={"UF-051": "cross_app_target"})
    assert sick.product_feature_id == "network-security"   # NOT moved
    assert tele.get("organic_moved", 0) == 0
    assert tele["organic_blocked_prior_hold"] == 1
    assert tele["organic_blocked_rows"] == [{
        "uf": "UF-051", "name": "Manage admin operations and mock data",
        "from": "network-security", "to": "chat",
        "reason": "prior-hold", "hold": "cross_app_target",
    }]


def test_it2_uf051_anchor_overlap_blocks(monkeypatch):
    """Guard 1 — ANCHOR-OVERLAP FALSE-ZERO: no hold, but the row's member
    entry IS the home PF's own role='anchor' member file → home_share=0 is
    invalid; block, never move (dev-demote territory of other cycles)."""
    _on(monkeypatch)
    registry, pfs, devs, sick, keeper = _soc0_uf051_scene()
    tele = run_post_uf_rehome([sick, keeper], devs, pfs, registry)
    assert sick.product_feature_id == "network-security"   # NOT moved
    assert tele.get("organic_moved", 0) == 0
    assert tele["organic_blocked_anchor_overlap"] == 1
    assert tele["organic_blocked_rows"] == [{
        "uf": "UF-051", "name": "Manage admin operations and mock data",
        "from": "network-security", "to": "chat",
        "reason": "anchor-overlap-false-zero",
    }]


def test_it2_hold_from_mega_moves_blocks(monkeypatch):
    """Guard 2 also covers rows the B24 stage MOVED itself (`оброблений`):
    a moves-derived hold (reason 'moved:<to>') blocks re-adjudication."""
    _on(monkeypatch)
    registry, pfs, devs, sick, keeper = _soc0_uf051_scene()
    tele = run_post_uf_rehome(
        [sick, keeper], devs, pfs, registry,
        mega_holds={"UF-051": "moved:chat"})
    assert sick.product_feature_id == "network-security"
    assert tele["organic_blocked_prior_hold"] == 1
    assert tele["organic_blocked_rows"][0]["hold"] == "moved:chat"


def test_it2_guards_spare_chrestomathic_movers(monkeypatch):
    """The guards MUST NOT kill the census movers: typebot UF-046 still
    moves when (a) its home PF carries a NON-overlapping role='anchor'
    member file, and (b) a hold exists for a DIFFERENT uf-id. (typebot's
    real board has ZERO mega stays/moves — probe-verified.)"""
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    pfs[0].member_files = [
        {"path": "packages/schemas/features/space/schema.ts",
         "role": "anchor", "confidence": 1.0, "primary": True},
    ]
    tele = run_post_uf_rehome(
        [mover, keeper], devs, pfs, registry,
        mega_holds={"UF-999": "cross_app_target"})
    assert mover.product_feature_id == "typebots"          # still moves
    assert tele["organic_moved"] == 1
    assert tele.get("organic_blocked_prior_hold", 0) == 0
    assert tele.get("organic_blocked_anchor_overlap", 0) == 0


def test_it2_direction_gate_survives_guards(monkeypatch):
    """UF-012 shape still direction-blocks (reason='direction') with the
    it2 guards armed and inert (no holds, no overlap)."""
    _on(monkeypatch)
    registry, pfs, devs, buried, other, teams_native = _burial_scene()
    tele = run_post_uf_rehome(
        [buried, other, teams_native], devs, pfs, registry,
        mega_holds={"UF-999": "cross_app_target"})
    assert buried.product_feature_id == "teams"
    assert tele["organic_blocked_direction"] == 1
    assert tele["organic_blocked_rows"][0]["reason"] == "direction"


# ── Arbiter-client contract (S3 ledger journals rung 'organic-move') ─────


def test_arbiter_ledger_journals_organic_move_rung(monkeypatch):
    """The move is an S3 ledger proposal (``propose_pf_now`` rung
    ``organic-move``) — with an active ledger the journal carries the rung
    and the write still lands (chokepoint-immediate semantics; the
    S5a/S5b client precedent)."""
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.overturn_ledger import (
        OverturnLedger,
        install_ledger,
        uninstall_ledger,
    )
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    # a REAL UserFlow so the ledger's _kind_of sees kind="uf".
    real_uf = UserFlow(
        id=mover.id, name=mover.name, product_feature_id="space",
        intent="manage", resource="typebot",
        member_flow_ids=list(mover.member_flow_ids),
        member_count=mover.member_count)
    real_uf.synthesized = False
    led = OverturnLedger()
    install_ledger(led)
    try:
        run_post_uf_rehome([real_uf, keeper], devs, pfs, registry)
    finally:
        uninstall_ledger()
    assert real_uf.product_feature_id == "typebots"
    organic = [e for e in led.entries if e.rung == "organic-move"]
    assert len(organic) == 1
    assert organic[0].kind == "uf"
    assert organic[0].old == "space" and organic[0].new == "typebots"


# ── Flag helper ──────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("FAULTLINE_ORGANIC_MOVE", raising=False)
    assert organic_move_enabled() is False
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "0")
    assert organic_move_enabled() is False
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    assert organic_move_enabled() is True
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "true")
    assert organic_move_enabled() is True
