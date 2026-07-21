"""B78 — home-fidelity homing pack (Seg B/C/D, ONE flag
``FAULTLINE_HOME_AFFINITY_GATE``, default OFF).

Seg B (conservation ``apply_home_affinity_gate``) — a ``tok0`` journey (name
shares zero content tokens with its home) that carries a deterministic
BETTER-HOME (bh_tok: another PF's name covers >=50% of the journey's content
tokens; bh_path: another PF's paths dominate the member entries >=0.5 AND
>=2x the home share) re-homes onto it via an S3 ``affinity-rehome`` proposal.
Named exhibits (B78 forensics): 'Manage cases' network-security→cases,
'Configure Slack' vacuum→slack. Anti-cases: 'Browse, filter, and manage
cases' shares 'case' with its 'cases' home ⇒ NOT tok0, never touched;
no-orphan (a PF's last journey never stripped); shared/container targets
refused.

Seg C (6.99b organic-move v3) — (a) prior-hold narrowed to SAME-TARGET (a
mega move to X no longer fences a move to a different better Y); (b)
RIVAL-SANITY: a rival with no name affinity to the journey ⇒ defer. Pin:
UF-051 'Manage admins'→'chat' — narrowing drops the wide hold, rival-sanity
re-catches it (chat shares zero content tokens with the journey).

Seg D (mega vacuum census) — a dominant vacuum's residual wrong-home
journeys are ranked by the same tok0/better-home lines (read-only forensic).

KILL-SWITCH (flag unset): every segment inert — the existing organic-move /
conservation behaviour is byte-identical (asserted at unit grain).
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from faultline.pipeline_v2.conservation import (
    apply_home_affinity_gate,
    home_affinity_gate_enabled,
)
from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import run_post_uf_rehome


# ── fixtures ─────────────────────────────────────────────────────────────


def _flow(uuid, ep, name=""):
    return NS(uuid=uuid, name=name, entry_point_file=ep, entry_point=None)


def _pf(key, paths, display=None, anchor_id=None, member_files=None):
    return NS(id=key, name=key, display_name=display or key.title(),
              paths=list(paths), anchor_id=anchor_id, layer="product",
              member_files=member_files or [])


def _uf(uid, name, home, members, *, synthesized=False, resource=None):
    return NS(id=uid, name=name, product_feature_id=home,
              member_flow_ids=list(members),
              member_count=len(members), synthesized=synthesized,
              resource=resource)


def _anchor(cid, prefixes):
    return SpineAnchor(
        canonical_id=cid, key=cid.split(":", 1)[-1], source="route",
        display=cid.split(":", 1)[-1].title(), prefixes=tuple(prefixes))


def _on(monkeypatch):
    monkeypatch.setenv("FAULTLINE_HOME_AFFINITY_GATE", "1")


# ══ Seg B — conservation home-affinity gate ═════════════════════════════


def _vacuum_scene():
    """A network-security vacuum that annexed 'Manage cases' + 'Configure
    Slack' (both tok0), plus a home-tied anti-case in 'cases' and two
    native network rows (keeps the vacuum non-orphan)."""
    f_slack = _flow("f-sl", "integrations/slack/config.ts", "configure slack")
    f_cases = _flow("f-ca", "app/cases/list.tsx", "browse cases")
    dev = NS(layer="developer", name="d", flows=[f_slack, f_cases], paths=[])
    pfs = [
        _pf("network-security", ["backend/routers/admin.py", "backend/net.py"],
            "Network Security"),
        _pf("cases", ["app/cases/list.tsx", "app/cases/detail.tsx"], "Cases"),
        _pf("slack", ["integrations/slack/config.ts"], "Slack"),
        _pf("chat", ["app/chat/x.tsx"], "Chat"),
    ]
    ufs = [
        _uf("UF-1", "Manage cases", "network-security", ["f-ca"]),
        _uf("UF-2", "Configure Slack", "network-security", ["f-sl"]),
        _uf("UF-3", "Browse, filter, and manage cases", "cases", ["f-ca"]),
        _uf("UF-4", "Investigate network security alerts", "network-security",
            []),
        _uf("UF-5", "Review network posture", "network-security", []),
    ]
    return [dev], pfs, ufs


def test_segb_exhibit_manage_cases_rehomes(monkeypatch):
    """'Manage cases' (tok0 in network-security) → cases via bh_tok."""
    _on(monkeypatch)
    devs, pfs, ufs = _vacuum_scene()
    tele = apply_home_affinity_gate(ufs, devs, pfs)
    assert ufs[0].product_feature_id == "cases"
    assert {"uf": "UF-1", "name": "Manage cases",
            "from": "network-security", "to": "cases"} in tele["moves"]


def test_segb_exhibit_configure_slack_rehomes(monkeypatch):
    """'Configure Slack' (tok0) → slack via bh_tok."""
    _on(monkeypatch)
    devs, pfs, ufs = _vacuum_scene()
    apply_home_affinity_gate(ufs, devs, pfs)
    assert ufs[1].product_feature_id == "slack"


def test_segb_anticase_home_tied_row_untouched(monkeypatch):
    """ANTI-CASE: 'Browse, filter, and manage cases' shares 'case' with its
    'cases' home ⇒ NOT tok0 ⇒ never proposed."""
    _on(monkeypatch)
    devs, pfs, ufs = _vacuum_scene()
    tele = apply_home_affinity_gate(ufs, devs, pfs)
    assert ufs[2].product_feature_id == "cases"
    assert all(m["uf"] != "UF-3" for m in tele["moves"])


def test_segb_anticase_correct_native_rows_untouched(monkeypatch):
    """Correct homes are named-untouched: the two network-native rows (their
    names carry network/security tokens) stay in network-security."""
    _on(monkeypatch)
    devs, pfs, ufs = _vacuum_scene()
    apply_home_affinity_gate(ufs, devs, pfs)
    assert ufs[3].product_feature_id == "network-security"
    assert ufs[4].product_feature_id == "network-security"


def test_segb_kill_switch_unset_is_noop(monkeypatch):
    """Flag unset ⇒ the gate is a no-op (no move, telemetry disabled) —
    byte-identical at unit grain."""
    monkeypatch.delenv("FAULTLINE_HOME_AFFINITY_GATE", raising=False)
    devs, pfs, ufs = _vacuum_scene()
    tele = apply_home_affinity_gate(ufs, devs, pfs)
    assert tele["enabled"] is False and tele["proposed"] == 0
    assert ufs[0].product_feature_id == "network-security"
    assert ufs[1].product_feature_id == "network-security"


def test_segb_explicit_zero_is_noop(monkeypatch):
    """Explicit ``=0`` stays a valid kill-switch forever."""
    monkeypatch.setenv("FAULTLINE_HOME_AFFINITY_GATE", "0")
    assert home_affinity_gate_enabled() is False
    devs, pfs, ufs = _vacuum_scene()
    tele = apply_home_affinity_gate(ufs, devs, pfs)
    assert tele["proposed"] == 0
    assert ufs[0].product_feature_id == "network-security"


def test_segb_orphan_guard_keeps_last_journey(monkeypatch):
    """no-orphan (B77): a vacuum whose ONLY journey is the tok0 mis-home is
    never emptied — the last journey stays put (guarded)."""
    _on(monkeypatch)
    f_ca = _flow("f-ca", "app/cases/list.tsx", "browse cases")
    dev = NS(layer="developer", name="d", flows=[f_ca], paths=[])
    pfs = [_pf("network-security", ["backend/net.py"], "Network Security"),
           _pf("cases", ["app/cases/list.tsx"], "Cases")]
    ufs = [_uf("UF-1", "Manage cases", "network-security", ["f-ca"])]
    tele = apply_home_affinity_gate(ufs, dev and [dev], pfs)
    assert ufs[0].product_feature_id == "network-security"  # kept
    assert tele["orphan_guarded"] == 1 and tele["proposed"] == 0


def test_segb_bh_path_rail(monkeypatch):
    """bh_path: a verb+generic journey ('Open records') with no name affinity
    to any PF still re-homes when a DIFFERENT PF's paths dominate its member
    entries >=0.5 AND >=2x the home's share."""
    _on(monkeypatch)
    f1 = _flow("f1", "app/records/a.tsx")
    f2 = _flow("f2", "app/records/b.tsx")
    dev = NS(layer="developer", name="d", flows=[f1, f2], paths=[])
    pfs = [
        _pf("dashboard", ["app/home.tsx"], "Dashboard"),
        _pf("records", ["app/records/a.tsx", "app/records/b.tsx"], "Records"),
    ]
    # 'Open widgets' — content token 'widget' matches no PF name (bh_tok
    # fails) but both entries live in records' paths, dashboard owns none.
    ufs = [_uf("UF-1", "Open widgets", "dashboard", ["f1", "f2"]),
           _uf("UF-2", "View dashboard home", "dashboard", [])]
    apply_home_affinity_gate(ufs, [dev], pfs)
    assert ufs[0].product_feature_id == "records"


# ══ Seg C — organic-move v3 (rival-sanity + same-target narrowing) ══════


def _uf051_scene():
    """Soc0 UF-051: all member flows enter backend/routers/admin.py (home
    network-security's own anchor file, outside its route: prefix →
    home_share=0), rival 'chat' registry covers backend/routers/ → rival=1.0
    — but 'chat' shares zero content tokens with the journey name."""
    registry = {
        "route:network-security": _anchor(
            "route:network-security", ["frontend/src/pages/network-security"]),
        "route:chat": _anchor("route:chat", ["backend/routers"]),
    }
    pf_home = _pf("network-security", [], "Network Security",
                  anchor_id="route:network-security",
                  member_files=[
                      {"path": "backend/routers/admin.py", "role": "anchor",
                       "confidence": 1.0, "primary": True}])
    pf_chat = _pf("chat", [], "Chat", anchor_id="route:chat")
    fls = [
        _flow("f-a1", "backend/routers/admin.py", "admin-mock-data-flow"),
        _flow("f-a2", "backend/routers/admin.py", "admin-migrate-flow"),
        _flow("f-a3", "backend/routers/admin.py", "admin-chats-flow"),
        _flow("f-ns", "frontend/src/pages/network-security/index.tsx",
              "browse-network-security-flow"),
    ]
    devs = [NS(layer="developer", name="d1", flows=fls, paths=[])]
    sick = _uf("UF-051", "Manage admin operations and mock data",
               "network-security", ["f-a1", "f-a2", "f-a3"])
    keeper = _uf("UF-900", "Browse network security", "network-security",
                 ["f-ns"])
    return registry, [pf_home, pf_chat], devs, sick, keeper


def test_segc_uf051_rival_sanity_blocks(monkeypatch):
    """PIN: flag ON, a cross_app_target STAYS hold — same-target narrowing
    drops the wide hold (a stays hold has no move-target), rival-sanity
    then blocks (chat has no name affinity to the journey). UF-051 does NOT
    move; the block reason is rival-sanity, NOT prior-hold."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    _on(monkeypatch)
    registry, pfs, devs, sick, keeper = _uf051_scene()
    tele = run_post_uf_rehome([sick, keeper], devs, pfs, registry,
                              mega_holds={"UF-051": "cross_app_target"})
    assert sick.product_feature_id == "network-security"
    assert tele.get("organic_moved", 0) == 0
    assert tele.get("organic_blocked_prior_hold", 0) == 0
    assert tele["organic_blocked_rival_sanity"] == 1
    assert tele["organic_prior_hold_narrowed"] == 1


def test_segc_uf051_rival_sanity_blocks_no_hold(monkeypatch):
    """flag ON, no hold at all: rival-sanity fires before the anchor-overlap
    guard — either way UF-051 stays home."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    _on(monkeypatch)
    registry, pfs, devs, sick, keeper = _uf051_scene()
    tele = run_post_uf_rehome([sick, keeper], devs, pfs, registry)
    assert sick.product_feature_id == "network-security"
    assert tele["organic_blocked_rival_sanity"] == 1


def test_segc_kill_switch_uf051_prior_hold_unchanged(monkeypatch):
    """Flag unset ⇒ Seg C inert: the wide prior-hold blocks UF-051 exactly
    as main (byte-identical organic-move behaviour)."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    monkeypatch.delenv("FAULTLINE_HOME_AFFINITY_GATE", raising=False)
    registry, pfs, devs, sick, keeper = _uf051_scene()
    tele = run_post_uf_rehome([sick, keeper], devs, pfs, registry,
                              mega_holds={"UF-051": "cross_app_target"})
    assert sick.product_feature_id == "network-security"
    assert tele["organic_blocked_prior_hold"] == 1
    assert "organic_blocked_rival_sanity" not in tele
    assert "organic_prior_hold_narrowed" not in tele


def _typebot_scene():
    """typebot UF-046 on 'space' (schema:, zero member match); 'typebots'
    (route:) matches every member — 0.0/1.0, rival name affinity present."""
    registry = {
        "schema:space": _anchor(
            "schema:space", ["packages/schemas/features/space"]),
        "route:typebots": _anchor(
            "route:typebots", ["apps/builder/src/pages/typebots"]),
        "route:domains": _anchor(
            "route:domains", ["apps/builder/src/pages/domains"]),
    }
    pf_space = _pf("space", [], "Space", anchor_id="schema:space")
    pf_typebots = _pf("typebots", [], "Typebots", anchor_id="route:typebots")
    pf_domains = _pf("domains", [], "Domains", anchor_id="route:domains")
    fls = [
        _flow("f-t1", "apps/builder/src/pages/typebots/index.tsx",
              "list-typebots-flow"),
        _flow("f-t2", "apps/builder/src/pages/typebots/create.tsx",
              "create-typebot-flow"),
    ]
    devs = [NS(layer="developer", name="d1", flows=fls, paths=[])]
    mover = _uf("UF-046", "Create, duplicate, and delete typebots", "space",
                ["f-t1", "f-t2"])
    keeper = _uf("UF-100", "Browse typebot gallery", "typebots", ["f-t1"])
    return registry, [pf_space, pf_typebots, pf_domains], devs, mover, keeper


def test_segc_rival_sanity_spares_affinity_mover(monkeypatch):
    """Anti-case: rival-sanity must NOT kill a legit mover — typebot UF-046
    (rival 'typebots' shares 'typebot' with the journey) still moves ON."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    tele = run_post_uf_rehome([mover, keeper], devs, pfs, registry)
    assert mover.product_feature_id == "typebots"
    assert tele["organic_moved"] == 1
    assert tele.get("organic_blocked_rival_sanity", 0) == 0


def test_segc_same_target_narrowing_allows_different_target(monkeypatch):
    """Seg C(a): a mega hold ABOUT a move to 'domains' no longer fences a
    move to the different better target 'typebots' (mega-hold про move-до-X
    не вбиває move-до-Y)."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    tele = run_post_uf_rehome([mover, keeper], devs, pfs, registry,
                              mega_holds={"UF-046": "moved:domains"})
    assert mover.product_feature_id == "typebots"       # moved to Y != X
    assert tele["organic_moved"] == 1
    assert tele["organic_prior_hold_narrowed"] == 1


def test_segc_same_target_still_blocks_same(monkeypatch):
    """Seg C(a): a mega move to the SAME target 'typebots' still blocks the
    re-move (prior-hold, same-target)."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    _on(monkeypatch)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    tele = run_post_uf_rehome([mover, keeper], devs, pfs, registry,
                              mega_holds={"UF-046": "moved:typebots"})
    assert mover.product_feature_id == "space"          # blocked
    assert tele["organic_blocked_prior_hold"] == 1
    assert tele.get("organic_moved", 0) == 0


def test_segc_same_target_narrowing_off_when_flag_unset(monkeypatch):
    """Kill-switch: with the flag unset the wide hold blocks the move to
    'typebots' even though the hold was about 'domains' (main behaviour)."""
    monkeypatch.setenv("FAULTLINE_ORGANIC_MOVE", "1")
    monkeypatch.delenv("FAULTLINE_HOME_AFFINITY_GATE", raising=False)
    registry, pfs, devs, mover, keeper = _typebot_scene()
    tele = run_post_uf_rehome([mover, keeper], devs, pfs, registry,
                              mega_holds={"UF-046": "moved:domains"})
    assert mover.product_feature_id == "space"          # blocked (wide hold)
    assert tele["organic_blocked_prior_hold"] == 1
    assert "organic_prior_hold_narrowed" not in tele


# ══ Seg D — mega vacuum census ══════════════════════════════════════════


def test_segd_vacuum_census_ranks_wrong_home(monkeypatch):
    """The read-only vacuum census reports the tok0 + better-home residual
    of the dominant vacuum, ranked by member mass."""
    _on(monkeypatch)
    from faultline.pipeline_v2.mega_pf_nav_rehome import _b78_vacuum_census
    flows = {"f1": _flow("f1", "app/cases/a.tsx"),
             "f2": _flow("f2", "integrations/slack/c.ts")}
    pfs = [_pf("network-security", ["backend/net.py"], "Network Security"),
           _pf("cases", ["app/cases/a.tsx"], "Cases"),
           _pf("slack", ["integrations/slack/c.ts"], "Slack")]
    ufs = [_uf("U1", "Manage cases", "network-security", ["f1", "f2"]),
           _uf("U2", "Configure Slack", "network-security", ["f2"]),
           _uf("U3", "Review network security", "network-security", [])]
    vc = _b78_vacuum_census("network-security", ufs, pfs,
                            {p.id: p for p in pfs}, flows)
    assert vc["vacuum"] == "network-security"
    assert vc["tok0"] == 2 and vc["better_home_residual"] == 2
    assert vc["exhibits"][0]["uf"] == "U1"      # ranked by member mass desc
