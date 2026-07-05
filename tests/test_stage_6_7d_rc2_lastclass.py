"""RC2 fix-3 — all-shared-UF resolution ladder + draw-native flowless-shell
absorption (2026-07-06).

(A) An ALL-SHARED user flow (every member flow owned by an infra/anchor dev)
    is resolved by the ladder: (i) token-family NAME match against a flowful
    non-shared capability (verb-folded: "…navigate" → "Navigation"),
    (ii) entry-file carve minting a domain-dedicated owning dev, (iii) honest
    ``uf_shared_unresolved`` residual.
(B) A draw-native flowless shell (a PF whose devs own >= 1k LOC but ZERO flows)
    is resettled: absorb a footprint-matched residual dev, JOIN a token-family
    flowful PF, or DEMOTE its devs to the shared bucket and DROP the shell.

Both run inside ``_finish`` → identical on the live and cache-hit replay paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Feature, Flow, MemberFile, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _SHARED_PF_SLUGS,
    _reassign_shared_ufs,
    _shell_absorb_enabled,
    _uf_family_capability,
    _verb_fold,
    resolve_flowless_shells,
)
SHARED = "shared-platform"


# ── Fixtures ────────────────────────────────────────────────────────────────

def _flow(uuid: str, entry: str = "") -> Flow:
    return Flow(
        name=uuid, uuid=uuid, paths=[entry or f"backend/{uuid}.py"],
        entry_point_file=entry or f"backend/{uuid}.py", authors=["a"],
        total_commits=2, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=95.0,
    )


def _dev(name: str, flow_specs: list[Any], *, paths: list[str] | None = None,
         loc: int | None = None, pfid: str | None = None) -> Feature:
    pths = paths or [f"backend/{name}/x.py"]
    flows = [_flow(f) if isinstance(f, str) else _flow(*f) for f in flow_specs]
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=pths, authors=["a"], total_commits=3, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer", loc=loc,
        product_feature_id=pfid,
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0)
                      for p in pths],
        flows=flows,
    )


def _uf(name: str, pf_slug: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id="UF-000", name=name, intent="browse", resource=name.lower(),
        product_feature_id=pf_slug, member_flow_ids=members,
        member_count=len(members), routes=[],
    )


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, description=display, paths=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        layer="product", member_files=[], flows=[])


# ── Ladder step (i): token-family NAME match ────────────────────────────────

def test_verb_fold_folds_verb_endings_but_guards_short_stems() -> None:
    assert _verb_fold("navigate") == "navig"          # == navigation stem
    assert _verb_fold("authorize") == "author"        # == authorization stem
    assert _verb_fold("integrate") == "integr"
    assert _verb_fold("create") == "create"           # 3-char stem guarded
    assert _verb_fold("state") == "state"
    assert _verb_fold("navig") == "navig"             # idempotent


def test_uf_family_capability_verb_folded_match() -> None:
    ctx = {
        "Navigation": {"slug": "navigation", "stems": {"navig"},
                       "flows": 5, "members": 1, "paths": 2},
        "Billing": {"slug": "billing", "stems": {"billing"},
                    "flows": 3, "members": 1, "paths": 1},
    }
    assert _uf_family_capability("Open command menu and navigate", ctx) \
        == "navigation"
    assert _uf_family_capability("Pay an invoice", ctx) is None


def test_ladder_family_resolves_all_shared_uf() -> None:
    """rallly-shaped: an all-shared journey whose NAME family-matches a
    flowful capability is moved off the residual (ladder i)."""
    ui = _dev("ui", ["f-nav"], paths=["packages/ui/src/breadcrumb.tsx"])
    nav = _dev("nav-feature", ["n1", "n2"], paths=["src/nav/menu.tsx"])
    devs = [ui, nav]
    d2p = {"ui": (SHARED,), "nav-feature": ("navigation",)}
    pfs = [_pf(SHARED, "Shared Platform"), _pf("navigation", "Navigation")]
    uf = _uf("Open command menu and navigate", SHARED, ["f-nav"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert uf.product_feature_id == "navigation"
    assert tele["uf_shared_family_resolved"] == 1
    assert tele["uf_shared_unresolved"] == 0
    assert not any((u.product_feature_id or "") in _SHARED_PF_SLUGS
                   for u in [uf])


def test_ladder_family_prefers_only_flowful_caps() -> None:
    """A flowless cap with a matching name is NOT a journey home; the ladder
    ignores it and (here) leaves the UF unresolved."""
    ui = _dev("ui", ["f-nav"], paths=["packages/ui/src/x.tsx"])
    nav = _dev("nav-feature", [], paths=["src/nav/menu.tsx"])  # 0 flows
    devs = [ui, nav]
    d2p = {"ui": (SHARED,), "nav-feature": ("navigation",)}
    pfs = [_pf(SHARED, "Shared Platform"), _pf("navigation", "Navigation")]
    uf = _uf("Open command menu and navigate", SHARED, ["f-nav"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert uf.product_feature_id == SHARED
    assert tele["uf_shared_family_resolved"] == 0
    assert tele["uf_shared_unresolved"] == 1


# ── Ladder step (ii): entry-file carve ──────────────────────────────────────

def test_ladder_carve_mints_dedicated_domain_dev() -> None:
    """An all-shared UF whose member flows are owned by a domain-DEDICATED dev
    (features/<domain>/) that was mis-sunk to the residual mints that dev its
    own capability (ladder ii)."""
    cmd = _dev("command-menu", [("c1", "features/command-menu/palette.tsx")],
               paths=["features/command-menu/palette.tsx"])
    devs = [cmd]
    d2p = {"command-menu": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Use command palette", SHARED, ["c1"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert uf.product_feature_id == "command-menu"
    assert tele["uf_shared_carved"] == 1
    assert d2p["command-menu"] == ("command-menu",)  # dev re-homed off shared
    assert any(p.name == "command-menu" for p in pfs)  # PF minted


def test_ladder_anchor_dev_not_carved_stays_unresolved() -> None:
    """A workspace-anchor / app-shell mega-dev (apps/…) never carves — the UF
    stays on the residual and is flagged (ladder iii)."""
    studio = _dev("studio", [("s1", "apps/studio/data/actions/q.ts")],
                  paths=["apps/studio/data/actions/q.ts"])
    devs = [studio]
    d2p = {"studio": (SHARED,)}
    pfs = [_pf(SHARED, "Shared Platform")]
    uf = _uf("Monitor scheduled action runs", SHARED, ["s1"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert uf.product_feature_id == SHARED
    assert tele["uf_shared_carved"] == 0
    assert tele["uf_shared_unresolved"] == 1


def test_ladder_order_family_before_carve() -> None:
    """Family match (i) wins over carve (ii) when both could apply."""
    cmd = _dev("command-menu", [("c1", "features/command-menu/palette.tsx")],
               paths=["features/command-menu/palette.tsx"])
    nav = _dev("nav-feature", ["n1", "n2"], paths=["src/nav/menu.tsx"])
    devs = [cmd, nav]
    d2p = {"command-menu": (SHARED,), "nav-feature": ("navigation",)}
    pfs = [_pf(SHARED, "Shared Platform"), _pf("navigation", "Navigation")]
    uf = _uf("Open command menu and navigate", SHARED, ["c1"])
    tele = _reassign_shared_ufs([uf], devs, d2p, new_pfs=pfs)
    assert uf.product_feature_id == "navigation"       # family, not carve
    assert tele["uf_shared_family_resolved"] == 1
    assert tele["uf_shared_carved"] == 0


def test_ladder_deterministic() -> None:
    def _once() -> tuple[str | None, dict[str, Any]]:
        ui = _dev("ui", ["f-nav"], paths=["packages/ui/src/x.tsx"])
        nav = _dev("nav-feature", ["n1", "n2"], paths=["src/nav/menu.tsx"])
        d2p = {"ui": (SHARED,), "nav-feature": ("navigation",)}
        pfs = [_pf(SHARED, "Shared Platform"), _pf("navigation", "Navigation")]
        uf = _uf("Open command menu and navigate", SHARED, ["f-nav"])
        tele = _reassign_shared_ufs([uf], [ui, nav], d2p, new_pfs=pfs)
        return uf.product_feature_id, tele
    a, b = _once(), _once()
    assert a[0] == b[0] == "navigation"
    assert a[1] == b[1]


def test_backward_compat_unresolved_record_unchanged() -> None:
    """The legacy all-shared unresolved path (no new_pfs, no family/carve
    target) still records the exact 2026-07-05 shape."""
    devs = [_dev("backend", ["s1", "s2"])]
    d2p = {"backend": (SHARED,)}
    uf = _uf("Manage playbooks", SHARED, ["s1", "s2"])
    tele = _reassign_shared_ufs([uf], devs, d2p)
    assert uf.product_feature_id == SHARED
    assert tele["uf_shared_unresolved"] == 1
    assert tele["uf_shared_reassignments"] == [
        {"uf": "Manage playbooks", "from": SHARED, "to": None,
         "basis": "all_members_shared_owned"}]


# ── Part B: draw-native flowless-shell resolution (post-feature_loc) ────────

def test_shell_demoted_and_dropped_when_no_family() -> None:
    """linkwarden-shaped: a >= 1k-LOC flowless shell with no token-family home
    demotes its dev to the shared bucket and the shell is dropped."""
    ai = _dev("ai", [], paths=["apps/worker/highlight.ts"], loc=1087,
              pfid="highlights")
    link = _dev("links", ["l1", "l2"], paths=["src/links/x.ts"],
                pfid="link-mgmt")
    devs = [ai, link]
    pfs = [_pf("highlights", "Highlights"), _pf("link-mgmt", "Link Management")]
    out, tele = resolve_flowless_shells(devs, pfs)
    assert tele["shell_demoted"] == 1
    assert ai.product_feature_id == SHARED
    assert not any(p.name == "highlights" for p in out)   # shell dropped
    assert any(p.name == "link-mgmt" for p in out)


def test_shell_joins_token_family_flowful_pf() -> None:
    """A flowless shell whose NAME shares a family stem with a FLOWFUL PF folds
    into it and is dropped (LOC-independent)."""
    shell = _dev("highlight-store", [], paths=["apps/x/store.ts"], loc=1500,
                 pfid="highlights-shell")
    home = _dev("highlighter", ["h1", "h2"], paths=["src/highlights/x.ts"],
                pfid="highlights")
    devs = [shell, home]
    pfs = [_pf("highlights-shell", "Highlights Store"),
           _pf("highlights", "Highlights")]
    out, tele = resolve_flowless_shells(devs, pfs)
    assert tele["shell_joined"] == 1
    assert shell.product_feature_id == "highlights"
    assert not any(p.name == "highlights-shell" for p in out)


def test_shell_below_loc_floor_kept() -> None:
    """A flowless PF below the 1k-LOC journeys-worthy floor with no family/
    absorb target is tolerated — never demoted/churned."""
    small = _dev("mini", [], paths=["apps/x/mini.ts"], loc=200, pfid="mini-cap")
    pfs = [_pf("mini-cap", "Mini Cap")]
    out, tele = resolve_flowless_shells([small], pfs)
    assert tele["shell_demoted"] == 0 and tele["shell_joined"] == 0
    assert any(p.name == "mini-cap" for p in out)
    assert small.product_feature_id == "mini-cap"


def test_shell_never_absorbs_unrelated_dev_under_anchor_root() -> None:
    """Regression (linkwarden Localization): a flowless shell whose claimed
    paths are an anchor ROOT (apps/web) must NOT sweep in an unrelated flowful
    dev by prefix — that manufactured a has-flows I8. The shell demotes+drops;
    the unrelated dev is untouched."""
    shell = _dev("i18n", [], paths=["apps/web", "packages/router"], loc=1297,
                 pfid="localization")
    other = _dev("settings", [("s1", "apps/web/settings/page.tsx")],
                 paths=["apps/web/settings/page.tsx"], pfid=SHARED)
    devs = [shell, other]
    pfs = [_pf("localization", "Localization")]
    out, tele = resolve_flowless_shells(devs, pfs)
    assert tele["shell_demoted"] == 1
    assert tele["shell_absorbed"] == 0
    assert other.product_feature_id == SHARED   # NOT swept into the shell
    assert shell.product_feature_id == SHARED   # demoted
    assert not any(p.name == "localization" for p in out)  # shell dropped


def test_flowful_pf_never_absorbed() -> None:
    """A PF whose devs own flows is never touched by Part B."""
    ok = _dev("ok", ["k1"], paths=["src/ok.ts"], loc=5000, pfid="okay")
    pfs = [_pf("okay", "Okay")]
    out, tele = resolve_flowless_shells([ok], pfs)
    assert tele == {"shell_absorbed": 0, "shell_joined": 0, "shell_demoted": 0,
                    "shell_resolutions": []}
    assert any(p.name == "okay" for p in out)


def test_shell_resolve_deterministic() -> None:
    def _once() -> tuple[dict[str, Any], str | None]:
        ai = _dev("ai", [], paths=["apps/worker/h.ts"], loc=1087,
                  pfid="highlights")
        link = _dev("links", ["l1"], paths=["src/links/x.ts"], pfid="link-mgmt")
        pfs = [_pf("highlights", "Highlights"), _pf("link-mgmt", "Links")]
        _out, tele = resolve_flowless_shells([ai, link], pfs)
        return tele, ai.product_feature_id
    a, b = _once(), _once()
    assert a[0] == b[0]
    assert a[1] == b[1] == SHARED


def test_shell_kill_switch(monkeypatch: Any) -> None:
    monkeypatch.delenv("FAULTLINE_STAGE_6_7D_SHELL_ABSORB", raising=False)
    assert _shell_absorb_enabled() is True
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_SHELL_ABSORB", "0")
    assert _shell_absorb_enabled() is False
