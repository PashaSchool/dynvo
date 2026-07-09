"""Stage 6.98 journey-worthiness floor — B10 UI-chrome demotion.

A UI-chrome affordance (Toggle sidebar, loc=3) is a micro-interaction, never a
user journey. The floor demotes such UFs I8-SAFELY (only when the PF keeps other
cover). Anti-cases: a small REAL journey survives (smallness never triggers), a
grey-zone capability that shares a chrome verb but persists product state
(Toggle theme → setThemeCookie) survives, a wide organic journey is untouched.

The production transform uses universal _get/_set accessors, so these dict
fixtures reproduce the pydantic path exactly.
"""
from __future__ import annotations

from faultline.pipeline_v2.synth_quality import (
    UF_WORTHINESS_ENV,
    _is_ui_chrome_uf,
    demote_ui_chrome_ufs,
    uf_worthiness_enabled,
)


def _flow(uuid, name, symbols):
    return {"uuid": uuid, "name": name, "user_flow_id": None,
            "loc_nodes": [{"symbol": s} for s in symbols]}


def _uf(uid, name, pfid, resource, members):
    return {"id": uid, "name": name, "product_feature_id": pfid,
            "resource": resource, "member_flow_ids": members}


def _fbi(flows):
    return {f["uuid"]: f for f in flows}


# ── the classifier ───────────────────────────────────────────────────────

def test_toggle_sidebar_is_chrome() -> None:
    flows = [_flow("m1", "toggle-sidebar-flow", ["getDashboardSidebarState"])]
    uf = _uf("UF-1", "Toggle sidebar", "dashboard", "components", ["m1"])
    assert _is_ui_chrome_uf(uf, _fbi(flows)) is True


def test_toggle_theme_persistence_survives() -> None:
    """Grey-zone: shares the chrome verb 'toggle' but persists product state
    (setThemeCookie) → touches a domain resource → NOT chrome."""
    flows = [_flow("m1", "toggle-theme-flow",
                   ["ThemeProvider", "setThemeCookie", "useTheme"])]
    uf = _uf("UF-1", "Toggle light and dark theme", "sections", "theme", ["m1"])
    assert _is_ui_chrome_uf(uf, _fbi(flows)) is False


def test_theme_noun_not_chrome_even_without_persistence() -> None:
    """A domain object (theme) is not a chrome noun, so it survives even if the
    persistence signal is absent — the noun gate is a second protection."""
    flows = [_flow("m1", "toggle-theme-flow", ["useTheme"])]
    uf = _uf("UF-1", "Toggle theme", "sections", "theme", ["m1"])
    assert _is_ui_chrome_uf(uf, _fbi(flows)) is False


def test_delete_account_survives_smallness_never_triggers() -> None:
    """A real journey with a CRUD verb — smallness alone NEVER triggers the
    floor; only the chrome-verb + chrome-noun + no-domain conjunction does."""
    flows = [_flow("m1", "delete-account-flow", ["deleteAccount"])]
    uf = _uf("UF-1", "Delete account", "settings", "account", ["m1"])
    assert _is_ui_chrome_uf(uf, _fbi(flows)) is False


def test_mixed_member_with_one_domain_flow_survives() -> None:
    """ALL members must be chrome; a single domain member disqualifies."""
    flows = [_flow("m1", "toggle-panel-flow", ["togglePanel"]),
             _flow("m2", "create-widget-flow", ["createWidget"])]
    uf = _uf("UF-1", "Manage panel", "dashboard", "panel", ["m1", "m2"])
    assert _is_ui_chrome_uf(uf, _fbi(flows)) is False


# ── demotion + I8 safety ───────────────────────────────────────────────────

def test_chrome_demoted_when_pf_keeps_other_cover() -> None:
    flows = [_flow("m1", "toggle-sidebar-flow", ["getSidebarState"]),
             _flow("m2", "manage-account-flow", ["saveAccount"])]
    flows[0]["user_flow_id"] = "UF-1"
    ufs = [_uf("UF-1", "Toggle sidebar", "dashboard", "sidebar", ["m1"]),
           _uf("UF-2", "Manage account", "dashboard", "account", ["m2"])]
    sm: dict = {}
    tele = demote_ui_chrome_ufs(ufs, flows, sm)
    assert tele["demoted"] == 1
    assert [u["name"] for u in ufs] == ["Manage account"]  # sidebar removed
    assert flows[0]["user_flow_id"] is None  # backpointer nulled (I14)
    assert sm["ui_chrome_demoted"][0]["name"] == "Toggle sidebar"


def test_chrome_sole_cover_is_kept_i8_safe() -> None:
    """A chrome UF that is its PF's ONLY cover is KEPT — never an I8 re-fire."""
    flows = [_flow("m1", "toggle-sidebar-flow", ["getSidebarState"])]
    ufs = [_uf("UF-1", "Toggle sidebar", "lonely-pf", "sidebar", ["m1"])]
    sm: dict = {}
    tele = demote_ui_chrome_ufs(ufs, flows, sm)
    assert tele["demoted"] == 0
    assert tele["kept_sole_cover_chrome"] == 1
    assert [u["name"] for u in ufs] == ["Toggle sidebar"]  # kept as I8 cover


def test_wide_organic_journey_untouched() -> None:
    flows = [_flow("m1", "create-resume-flow", ["createResume"]),
             _flow("m2", "browse-resumes-flow", ["listResumes"])]
    ufs = [_uf("UF-1", "Manage resumes", "resumes", "resume", ["m1", "m2"])]
    sm: dict = {}
    tele = demote_ui_chrome_ufs(ufs, flows, sm)
    assert tele["demoted"] == 0
    assert [u["name"] for u in ufs] == ["Manage resumes"]


def test_flag_default_on(monkeypatch) -> None:
    monkeypatch.delenv(UF_WORTHINESS_ENV, raising=False)
    assert uf_worthiness_enabled() is True
    monkeypatch.setenv(UF_WORTHINESS_ENV, "0")
    assert uf_worthiness_enabled() is False
