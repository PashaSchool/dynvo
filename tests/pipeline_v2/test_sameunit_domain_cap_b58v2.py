"""B58-v2 — same-unit domain-dir cap (FAULTLINE_SAMEUNIT_DOMAIN_CAP).

Forensics (keyed boards 2026-07-14, B58-OFF; operator ruling D+narrowed-B):

  * Soc0 ``Network Security`` (fdir:frontend/src/modules/network-security)
    = 103,796 LOC = 38.6% of the board over 418 files. Own subtree = 148
    files; the annexed same-unit remainder splits into ``features/*``
    domain dirs (15 dirs / 55 files — 13 of the 15 names have an existing
    PF receiver on the same board) and ``components/*`` children (13 dirs
    / 121 files; ``components/api-keys`` carries a foreign journey but has
    NO PF on the annexed board — the nav-only class).
  * plane ``spaces`` (route:space) = 75,978 LOC = 39.1% of the board over
    575 paths — 451 files annexed from apps/api (db/app/api — the api's
    SHARED core) vs 124 own apps/space files; ZERO B53-container words in
    its paths, so the domain-dir cap is structurally inert there. Its page
    routes live wholly in apps/space — the Seg B page-surface rung.

Seg A (drain donor class 2): a container-anchor PF sheds member files in
SAME-unit foreign domain dirs whose domain name echo-matches exactly ONE
existing PF (NamespaceEcho v1 laws; nav-only match = telemetry, never a
move). Seg B (mint): a route anchor whose full-surface unit test is
non-unanimous resolves by PAGE-surface unanimity, feeding the B58 fence.

Default OFF: every trigger test has an OFF twin asserting the annexation
world stays byte-identical in behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile, UserFlow
from faultline.pipeline_v2 import ws_blob_domain_drain as WBD
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    SAMEUNIT_DOMAIN_CAP_ENV,
    _SHARED_REASON_CROSS_UNIT,
    run_anchored_mint,
    sameunit_domain_cap_enabled,
)
from faultline.pipeline_v2.ws_blob_domain_drain import (
    run_ws_blob_domain_drain,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── builders (mirror test_b53_ws_blob_drain / test_annexation_guard_b58) ──


def _mf(path: str, evidence: str = "orig") -> MemberFile:
    return MemberFile(path=path, role="anchor", confidence=1.0, primary=True,
                      evidence=evidence)


def _flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _dev(name, paths, *, pfid=None, flows=None):
    return Feature(
        name=name, paths=list(paths), flows=flows or [],
        product_feature_id=pfid, member_files=[_mf(p) for p in paths],
        shared_reason=None,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _pf(slug, paths, anchor_id):
    f = Feature(
        name=slug, paths=list(paths), flows=[],
        member_files=[_mf(p) for p in paths],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, loc=10,
    )
    f.layer = "product"
    f.anchor_id = anchor_id
    return f


def _uf(uid, name, pfid, member_ids):
    return UserFlow(
        id=uid, name=name, intent="manage", resource="record",
        product_feature_id=pfid, member_flow_ids=list(member_ids),
        member_count=len(member_ids),
    )


def _ws(path: str) -> SimpleNamespace:
    return SimpleNamespace(name=path.rsplit("/", 1)[-1], path=path, files=[])


def _ctx(workspaces=None, tracked=None):
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=list(tracked or []),
        repo_path=Path("."), monorepo=bool(workspaces),
    )


def _dirs_pf(tele):
    return {d: v["pf"] for d, v in tele["matched_dirs"].items()}


# ── the Soc0 exhibit topology (Seg A fixtures) ────────────────────────────

UNIT = "frontend"
ANCHOR_TAIL = f"{UNIT}/src/modules/network-security"

# own-subtree mass (must NEVER move — the anchor's identity).
OWN1 = f"{ANCHOR_TAIL}/panel.tsx"
OWN2 = f"{ANCHOR_TAIL}/hooks/useFlows.ts"
# whole-dir foreign dev (the Soc0 `teams` shape).
INT1 = f"{UNIT}/src/features/integrations/dialog.tsx"
INT2 = f"{UNIT}/src/features/integrations/list.tsx"
# mixed dev (the Soc0 `i18n` shape): one echo-matched features file, one
# nav-only components child, one unmatched components child, one generic.
CASE1 = f"{UNIT}/src/features/cases/detail.tsx"
APIKEY1 = f"{UNIT}/src/components/api-keys/table.tsx"
CHAT1 = f"{UNIT}/src/components/chat/window.tsx"
UI1 = f"{UNIT}/src/components/ui/button.tsx"
# receiver-PF anchor files (targets own code inside the unit — B22a law).
INT_ANCHOR = f"{UNIT}/src/anchors/integrations.ts"
CASE_ANCHOR = f"{UNIT}/src/anchors/cases.ts"


def _soc0_world():
    """The Soc0 Network-Security annexation, distilled: a container-anchor
    donor PF holding own-subtree mass + a whole-dir foreign dev + a mixed
    dev, with the `integrations` / `cases` receivers existing on-board."""
    core = _dev("netsec-core", [OWN1, OWN2], pfid="network-security",
                flows=[_flow("view-netsec-flow", OWN1)])
    teams = _dev("teams", [INT1, INT2], pfid="network-security",
                 flows=[_flow("manage-teams-flow", INT1, paths=[INT1, INT2])])
    mixed = _dev("i18n-mixed", [CASE1, APIKEY1, CHAT1, UI1],
                 pfid="network-security")
    int_owner = _dev("integrations-owner", [INT_ANCHOR], pfid="integrations")
    case_owner = _dev("cases-owner", [CASE_ANCHOR], pfid="cases")

    netsec_pf = _pf("network-security",
                    [OWN1, OWN2, INT1, INT2, CASE1, APIKEY1, CHAT1, UI1],
                    f"fdir:{ANCHOR_TAIL}")
    int_pf = _pf("integrations", [INT_ANCHOR], "route:integration")
    case_pf = _pf("cases", [CASE_ANCHOR], "route:case")

    devs = [core, teams, mixed, int_owner, case_owner]
    pfs = [netsec_pf, int_pf, case_pf]
    ufs = [_uf("UF-T", "Manage teams", "network-security", ["F-T"])]
    return devs, pfs, ufs


def _run_cap(devs, pfs, ufs, *, cap=True, nav=frozenset(), ws_blob=True):
    return run_ws_blob_domain_drain(
        devs, pfs, ufs, [], _ctx(workspaces=[_ws("frontend"), _ws("backend")]),
        ws_blob=ws_blob, sameunit_cap=cap, nav_keys=nav,
    )


# ── flag semantics ────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(SAMEUNIT_DOMAIN_CAP_ENV, raising=False)
    assert sameunit_domain_cap_enabled() is False
    monkeypatch.setenv(SAMEUNIT_DOMAIN_CAP_ENV, "1")
    assert sameunit_domain_cap_enabled() is True
    monkeypatch.setenv(SAMEUNIT_DOMAIN_CAP_ENV, "0")
    assert sameunit_domain_cap_enabled() is False


# ── Seg A — cap trigger on the exhibit topology ───────────────────────────


def test_cap_moves_echo_matched_dirs_to_owners():
    devs, pfs, ufs = _soc0_world()
    tele = _run_cap(devs, pfs, ufs, nav=frozenset({"api-key"}))

    # features/integrations (whole-dir dev) + features/cases (mixed dev
    # slice) move to their existing PFs; components/api-keys is nav-only
    # telemetry; components/chat unmatched; components/ui generic.
    assert _dirs_pf(tele) == {
        f"{UNIT}/src/features/integrations": "integrations",
        f"{UNIT}/src/features/cases": "cases",
    }
    assert tele["files_moved"] == 3  # INT1, INT2, CASE1
    assert tele["cap_donors"] == ["network-security"]
    assert tele["nav_only_dirs"] == {
        f"{UNIT}/src/components/api-keys": "api-key"}
    assert tele["skipped_generic"] >= 1  # components/ui

    int_pf = next(p for p in pfs if p.name == "integrations")
    case_pf = next(p for p in pfs if p.name == "cases")
    netsec_pf = next(p for p in pfs if p.name == "network-security")
    assert INT1 in int_pf.paths and INT2 in int_pf.paths
    assert CASE1 in case_pf.paths
    # donor keeps its own subtree + the unmoved components mass.
    assert OWN1 in netsec_pf.paths and OWN2 in netsec_pf.paths
    assert APIKEY1 in netsec_pf.paths and CHAT1 in netsec_pf.paths
    assert UI1 in netsec_pf.paths
    assert INT1 not in netsec_pf.paths and CASE1 not in netsec_pf.paths


def test_cap_dev_ledger_ownership_moves():
    """path_index is rebuilt from dev paths — the drained files must sit
    under a dev whose product_feature_id is the receiver (the Stage-6.99
    I16 journey re-home hook: journeys follow their flow mass via the
    EXISTING r1 machinery, no new journey mover)."""
    devs, pfs, ufs = _soc0_world()
    _run_cap(devs, pfs, ufs)

    def _owner(path):
        for d in devs:
            if d.layer == "developer" and path in [
                    m.path for m in d.member_files if m.primary]:
                return d.product_feature_id
        return None

    assert _owner(INT1) == "integrations"
    assert _owner(INT2) == "integrations"
    assert _owner(CASE1) == "cases"
    # unmoved mass keeps the donor home.
    assert _owner(OWN1) == "network-security"
    assert _owner(APIKEY1) == "network-security"
    # the mixed donor dev no longer claims the moved file.
    mixed = next(d for d in devs if d.name == "i18n-mixed")
    assert CASE1 not in mixed.paths and APIKEY1 in mixed.paths


def test_cap_conservation_files_neither_lost_nor_duplicated():
    devs, pfs, ufs = _soc0_world()
    before = sorted(
        p for d in devs for p in d.paths if d.layer == "developer")
    _run_cap(devs, pfs, ufs)
    after = sorted(
        p for d in devs for p in d.paths if d.layer == "developer")
    assert before == after, "cap must MOVE files between devs, never " \
        "drop or duplicate (journey conservation rides the dev ledger)"


def test_cap_journey_rehome_projection():
    """A donor-homed journey whose flow mass leaves projects an I16
    re-home (telemetry only — the authoritative move is Stage 6.99)."""
    devs, pfs, ufs = _soc0_world()
    teams = next(d for d in devs if d.name == "teams")
    teams.flows[0].uuid = "F-T"
    tele = _run_cap(devs, pfs, ufs)
    assert tele["ufs_rehomed"] == 1
    # projection never mutates the UF row itself (I8/B52 law: the mover
    # is the existing Stage-6.99 machinery, not the drain).
    assert ufs[0].product_feature_id == "network-security"


def test_cap_never_mints():
    devs, pfs, ufs = _soc0_world()
    before = len(pfs)
    _run_cap(devs, pfs, ufs, nav=frozenset({"api-key"}))
    assert len(pfs) == before


def test_nav_only_dir_stays_without_nav_keys():
    """No nav_keys ⇒ components/api-keys is not even telemetry — silent
    honest stay (the echo gate's 0-hit abstain)."""
    devs, pfs, ufs = _soc0_world()
    tele = _run_cap(devs, pfs, ufs, nav=frozenset())
    assert tele["nav_only_dirs"] == {}
    netsec_pf = next(p for p in pfs if p.name == "network-security")
    assert APIKEY1 in netsec_pf.paths


# ── Seg A — SACRED anti-cases ─────────────────────────────────────────────


def test_sacred_own_subtree_mass_is_invisible_to_the_cap():
    """twenty object-record: sub-dir mass AT/UNDER the anchor tail is the
    anchor's own identity — the cap structurally cannot see it, even
    when a same-named PF exists elsewhere."""
    pkg = "packages/twenty-front"
    tail = f"{pkg}/src/modules/object-record"
    m1 = f"{tail}/record-table/table.tsx"
    m2 = f"{tail}/record-board/board.tsx"
    core = _dev("or-core", [m1, m2], pfid="object-record")
    pf = _pf("object-record", [m1, m2], f"fdir:{tail}")
    other = _pf("record-table", ["packages/twenty-front/src/anchors/rt.ts"],
                "route:record-table")
    other_dev = _dev("rt-owner", ["packages/twenty-front/src/anchors/rt.ts"],
                     pfid="record-table")
    tele = run_ws_blob_domain_drain(
        [core, other_dev], [pf, other], [], [],
        _ctx(workspaces=[_ws(pkg)]),
        sameunit_cap=True,
    )
    assert tele["files_moved"] == 0
    assert tele["cap_donors"] == []
    assert m1 in pf.paths and m2 in pf.paths
    assert core.paths == [m1, m2]


def test_sacred_mono_domain_board_is_noop():
    """kan: no ≥1 foreign container child in the unit ⇒ the donor never
    becomes a cap candidate ⇒ full no-op."""
    core = _dev("board-core",
                ["web/src/board/list.tsx", "web/src/lib/util.ts"],
                pfid="board")
    pf = _pf("board", ["web/src/board/list.tsx", "web/src/lib/util.ts"],
             "fdir:web/src/board")
    tele = run_ws_blob_domain_drain(
        [core], [pf], [], [], _ctx(workspaces=[_ws("web")]),
        sameunit_cap=True,
    )
    assert tele["files_moved"] == 0
    assert tele["cap_donors"] == []


def test_sacred_centralized_shared_ui_components_full_noop():
    """A repo whose components/ tree is pure shared UI with ZERO PF
    matches (and no nav echo): the echo gate abstains everywhere — full
    no-op, byte-stable paths."""
    w1 = "app/src/components/widgets/chart.tsx"
    w2 = "app/src/components/inputs/field.tsx"
    core = _dev("dash-core", ["app/src/modules/dashboard/main.tsx", w1, w2],
                pfid="dashboard")
    pf = _pf("dashboard", ["app/src/modules/dashboard/main.tsx", w1, w2],
             "fdir:app/src/modules/dashboard")
    tele = run_ws_blob_domain_drain(
        [core], [pf], [], [], _ctx(workspaces=[_ws("app")]),
        sameunit_cap=True,
    )
    assert tele["files_moved"] == 0
    assert tele["nav_only_dirs"] == {}
    assert w1 in pf.paths and w2 in pf.paths
    assert core.paths == ["app/src/modules/dashboard/main.tsx", w1, w2]


def test_ambiguous_domain_echo_skips():
    """A domain whose key echoes >1 existing PF abstains (v1 law)."""
    devs, pfs, ufs = _soc0_world()
    # a second PF that also answers to 'integration'.
    twin = _pf("integration-hub",
               [f"{UNIT}/src/anchors/hub.ts"], "route:integration")
    twin_dev = _dev("hub-owner", [f"{UNIT}/src/anchors/hub.ts"],
                    pfid="integration-hub")
    tele = _run_cap(devs + [twin_dev], pfs + [twin], ufs)
    assert f"{UNIT}/src/features/integrations" not in _dirs_pf(tele)
    assert tele["skipped_ambig"] >= 1
    netsec_pf = next(p for p in pfs if p.name == "network-security")
    assert INT1 in netsec_pf.paths  # stays with the donor


def test_would_empty_donor_pulls_whole_move_back():
    """PF-survival invariance (I8-pullback shape at donor grain): a cap
    donor whose ENTIRE file set would leave keeps everything."""
    ghost_file = f"{UNIT}/src/features/cases/orphan.tsx"
    ghost_dev = _dev("ghost-dev", [ghost_file], pfid="ghost")
    ghost_pf = _pf("ghost", [ghost_file], f"fdir:{UNIT}/src/modules/ghost")
    case_owner = _dev("cases-owner", [CASE_ANCHOR], pfid="cases")
    case_pf = _pf("cases", [CASE_ANCHOR], "route:case")
    tele = run_ws_blob_domain_drain(
        [ghost_dev, case_owner], [ghost_pf, case_pf], [], [],
        _ctx(workspaces=[_ws("frontend")]),
        sameunit_cap=True,
    )
    assert tele["skipped_would_empty"] == 1
    assert tele["files_moved"] == 0
    assert ghost_pf.paths == [ghost_file]
    assert ghost_dev.paths == [ghost_file]


def test_cross_unit_dir_never_capped():
    """The cap judges SAME-unit dirs only: a foreign-unit domain dir in
    the donor's member set is out of scope (that class is B58 Seg A at
    the mint — cross-unit fencing, already shipped)."""
    foreign = "backend/src/features/cases/api.py"
    devs, pfs, ufs = _soc0_world()
    mixed = next(d for d in devs if d.name == "i18n-mixed")
    mixed.paths.append(foreign)
    mixed.member_files.append(_mf(foreign))
    netsec = next(p for p in pfs if p.name == "network-security")
    netsec.paths.append(foreign)
    tele = _run_cap(devs, pfs, ufs)
    assert foreign not in {f for fs in tele["matched_dirs"] for f in fs}
    assert foreign in mixed.paths  # untouched — wrong unit for this donor


# ── Seg A — class switches (independent kill-switches) ────────────────────


def test_cap_off_is_byte_inert_on_exhibit_topology():
    devs, pfs, ufs = _soc0_world()
    paths_before = {d.name: list(d.paths) for d in devs}
    tele = _run_cap(devs, pfs, ufs, cap=False, nav=frozenset({"api-key"}))
    assert tele["files_moved"] == 0
    assert "cap_donors" not in tele          # tele shape unchanged too
    assert "nav_only_dirs" not in tele
    assert {d.name: list(d.paths) for d in devs} == paths_before
    assert len(devs) == 5                     # no carve devs appended


def test_ws_blob_class_gated_independently():
    """sameunit_cap=True must NOT activate the ws-blob class: a classic
    B53 blob donor stays intact when ws_blob=False."""
    pkg = "twenty-front"
    b1 = f"{pkg}/src/modules/messaging/thread.tsx"
    blob_dev = _dev("blob", [b1], pfid="twenty-front")
    blob_pf = _pf("twenty-front", [b1], f"ws:{pkg}")
    msg_pf = _pf("messaging", [f"{pkg}/src/anchors/messaging.ts"],
                 "route:messaging")
    msg_dev = _dev("msg-owner", [f"{pkg}/src/anchors/messaging.ts"],
                   pfid="messaging")
    tele = run_ws_blob_domain_drain(
        [blob_dev, msg_dev], [blob_pf, msg_pf], [], [],
        _ctx(workspaces=[_ws(pkg)]),
        ws_blob=False, sameunit_cap=True,
    )
    assert tele["files_moved"] == 0           # ws: donor is class 1 only
    assert b1 in blob_pf.paths


# ── Seg B — page-surface canonical-unit rung (the plane shape) ────────────

_SEGB_WORKSPACES = [_ws("apps/space"), _ws("apps/api")]

# NOTE: no dir named ``portal`` anywhere — the anchor stays KEY-ONLY
# (``route:portal``, the plane ``route:space`` shape); a portal/ page dir
# would mint a path-embedded prefix anchor whose unit rung 1 already
# resolves (that class needs no Seg B).
PAGE1 = "apps/space/src/pages/index.tsx"
PAGE2 = "apps/space/src/pages/detail.tsx"
API1 = "apps/api/src/routes/portal.py"


def _segb_routes():
    """A full-stack KEY-ONLY anchor: PAGE surface wholly in apps/space,
    api surface in apps/api — rung 2 (page+api unanimity) fails, rung
    2.5 (page-only) resolves apps/space."""
    return [
        {"pattern": "/portal", "method": "PAGE", "file": PAGE1},
        {"pattern": "/portal", "method": "PAGE", "file": PAGE2},
        {"pattern": "/portal", "method": "GET", "file": API1},
    ]


def _segb_host():
    return _dev("portal", [PAGE1, PAGE2],
                flows=[_flow("view-portal-flow", PAGE1)])


def _segb_victim():
    """The plane apps/api shared-core shape: a flowful dev wholly inside
    apps/api whose flow ENTERS through the host's page (the entry-rung
    force-bind; span resolves nowhere else)."""
    paths = [f"apps/api/src/db/m{i}.py" for i in range(5)]
    return _dev("db-models", paths,
                flows=[_flow("persist-portal-flow", PAGE1, paths=[PAGE1])])


def _segb_lineage_backend():
    """The Soc0 route:detection anti-case: a dev whose OWN file IS the
    anchor's api evidence — a LINEAGE bind that must survive the cap."""
    return _dev("api-routers", [API1],
                flows=[_flow("serve-portal-api-flow", API1)])


def test_segb_off_multi_unit_route_anchor_annexes(monkeypatch):
    """B58 ON / cap OFF reproduces the plane hole: page+api units differ
    ⇒ canonical unit None ⇒ never foreign ⇒ apps/api dev annexed."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    monkeypatch.delenv(SAMEUNIT_DOMAIN_CAP_ENV, raising=False)
    host, victim = _segb_host(), _segb_victim()
    pfs, tele = run_anchored_mint(
        [host, victim], _segb_routes(), _ctx(_SEGB_WORKSPACES))
    assert host.product_feature_id is not None
    assert victim.product_feature_id == host.product_feature_id, (
        "cap-OFF must reproduce the plane annexation (diagnosis twin)")
    assert "cap_page_surface_unit" not in tele


def test_segb_on_page_surface_unit_fences_rescue_binds(monkeypatch):
    """Both flags ON: the anchor resolves to its PAGE unit (apps/space);
    the apps/api dev is fenced at the rescue rungs and lanes with its
    unit — the db/app shared core never rides the spaces PF."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    monkeypatch.setenv(SAMEUNIT_DOMAIN_CAP_ENV, "1")
    host, victim = _segb_host(), _segb_victim()
    pfs, tele = run_anchored_mint(
        [host, victim], _segb_routes(), _ctx(_SEGB_WORKSPACES))
    assert host.product_feature_id is not None
    assert victim.product_feature_id is None, (
        "B58-v2 Seg B REGRESSION: the multi-unit route anchor still "
        "annexes the api unit's shared core — landed on {!r}".format(
            victim.product_feature_id))
    assert victim.shared_reason == _SHARED_REASON_CROSS_UNIT
    assert tele.get("cap_page_surface_unit", 0) >= 1


def test_segb_lineage_bind_survives_the_cap(monkeypatch):
    """Full-stack LINEAGE binds are untouched (Soc0 route:detection: 28
    backend/routers files ride lineage): the dev owning the anchor's own
    api evidence stays bound with both flags ON."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    monkeypatch.setenv(SAMEUNIT_DOMAIN_CAP_ENV, "1")
    host, backend = _segb_host(), _segb_lineage_backend()
    pfs, tele = run_anchored_mint(
        [host, backend], _segb_routes(), _ctx(_SEGB_WORKSPACES))
    assert host.product_feature_id is not None
    assert backend.product_feature_id == host.product_feature_id, (
        "the cap may only fence RESCUE binds — lineage evidence is the "
        "anchor's own identity")


def test_segb_api_only_multi_unit_anchor_stays_unresolved(monkeypatch):
    """No page surface ⇒ rung 2.5 has nothing to vote with ⇒ canonical
    unit stays None ⇒ conservative never-foreign (no behaviour change)."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    monkeypatch.setenv(SAMEUNIT_DOMAIN_CAP_ENV, "1")
    routes = [
        {"pattern": "/portal", "method": "GET", "file": API1},
        {"pattern": "/portal", "method": "GET",
         "file": "apps/space/src/api/portal-proxy.ts"},
    ]
    host = _dev("portal", [API1, "apps/space/src/api/portal-proxy.ts"],
                flows=[_flow("serve-portal-flow", API1)])
    victim = _segb_victim()
    pfs, tele = run_anchored_mint(
        [host, victim], routes, _ctx(_SEGB_WORKSPACES))
    assert tele.get("cap_page_surface_unit", 0) == 0


def test_segb_inert_without_the_b58_fence(monkeypatch):
    """Cap ON / B58 guard OFF: rung 2.5 feeds a fence that is not armed —
    the mint stays byte-identical to the plain OFF world (the cap is a
    sub-mechanism of the annexation guard, mirroring the B22/B23
    lock-step precedent). Explicit ``=0`` (not delenv): the guard's
    default flipped ON at B62/ks29 — the test's semantics must be
    default-agnostic across both worlds."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "0")
    monkeypatch.setenv(SAMEUNIT_DOMAIN_CAP_ENV, "1")
    host, victim = _segb_host(), _segb_victim()
    pfs, tele = run_anchored_mint(
        [host, victim], _segb_routes(), _ctx(_SEGB_WORKSPACES))
    assert victim.product_feature_id == host.product_feature_id
    assert "cap_page_surface_unit" not in tele


# ── stage order + call-site law (cap at mint ← drain after husk-fold) ─────


def test_finalize_call_site_order_and_switches():
    """Source-order guard: Seg B lives in the 6.86 mint; Seg A rides the
    6.885b drain call AFTER the post-UF husk fold; the call site arms
    each donor class from its own flag (independent kill-switches)."""
    src = (Path(__file__).resolve().parents[2] / "faultline" / "pipeline_v2"
           / "phase_finalize.py").read_text(encoding="utf-8")
    i_mint = src.index("run_anchored_mint(")
    i_husk = src.index("husk_post_uf_fold")
    i_drain = src.index("wbd_tele = run_ws_blob_domain_drain(")
    assert i_mint < i_husk < i_drain
    call = src[i_drain:i_drain + 800]
    assert "ws_blob=_wbd_on" in call
    assert "sameunit_cap=_cap_on" in call
    assert "nav_keys=frozenset(_nav_keys)" in call


def test_cap_containers_extend_b53_yaml():
    """The container list is DATA (YAML single source of truth): the cap
    set == the B53 containers + the cap-only extension."""
    b53, _generic = WBD._containers()
    cap = WBD._cap_containers()
    assert b53 <= cap
    assert "components" in cap
    assert "components" not in b53  # the extension never leaks to class 1
