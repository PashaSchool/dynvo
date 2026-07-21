"""S5b Seg H — digest stratification (FAULTLINE_DIGEST_STRATIFICATION).

Class under test: digest-shadow starvation — the 6.7d Call-1 digest carries
MASS, not surface identity, so on any repo above the caps the ranked cuts
starve the whole page surface out of every channel at once (novu 'Sign in' /
'View usage analytics' forensics, spec SEG H).

Probe canon (2026-07-20 tune-first, s5bh-out/):
  * M1-ADDITIVE — page-anchored UFs beyond the mass cap are APPENDED, never
    displace (the fixed-cap stratified form starved 5/67 cached proposals:
    REFUTED — displacement must be 0).
  * M2-HYGIENE-QUOTA — under route pressure half the route budget is
    reserved for the HYGIENIC page stream (storybook/dev-artifact paths and
    filename-echo pseudo-routes demoted out of the quota only). M2b
    slug-dedup was REFUTED (flat page dirs).
  * Inertness — on a repo without pressure both mechanisms are byte-no-ops.

Captured-fixture units (novu / twenty run dirs) skip when the local capture
is absent — the synthetic units below hold the MECHANISM everywhere.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    MAX_ROUTES_DIGEST,
    MAX_USER_FLOWS_DIGEST,
    _build_digest,
    _hygienic_page_route,
    _page_anchored_uf_ids,
    digest_stratification_enabled,
)

_NOVU_RUN = Path("~/.faultline/logs/novu/20260719T113936Z-99ef8c41").expanduser()
_TWENTY_RUN = Path(
    "~/.faultline/logs/twenty/20260719T113937Z-983f03ad").expanduser()

_needs_novu = pytest.mark.skipif(
    not _NOVU_RUN.exists(), reason="local novu capture absent")
_needs_twenty = pytest.mark.skipif(
    not _TWENTY_RUN.exists(), reason="local twenty capture absent")


# ── Synthetic fixtures ──────────────────────────────────────────────────────

def _flow(uuid: str, entry: str | None) -> Flow:
    return Flow(
        name=f"{uuid}-flow", paths=[entry] if entry else [], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        entry_point_file=entry, uuid=uuid,
    )


def _feat(name: str, flows: list[Flow]) -> Feature:
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=[f"app/{name}/x.ts"], authors=["a"], total_commits=3,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        layer="developer", flows=flows,
    )


def _uf(uf_id: str, name: str, members: list[str], count: int,
        pf: str | None = "PF-1") -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="use", resource=name.lower(),
        member_flow_ids=members, member_count=count,
        product_feature_id=pf,
    )


def _api_routes(n: int) -> list[dict[str, Any]]:
    return [
        {"pattern": f"/api/things/{i}", "method": "GET", "trigger": "api",
         "file": f"src/api/things_{i}.ts"}
        for i in range(n)
    ]


def _digest_bytes(d: dict[str, Any]) -> bytes:
    return json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")


# ── Flag gate ───────────────────────────────────────────────────────────────

def test_gate_default_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("FAULTLINE_DIGEST_STRATIFICATION", raising=False)
    assert digest_stratification_enabled() is False
    monkeypatch.setenv("FAULTLINE_DIGEST_STRATIFICATION", "0")
    assert digest_stratification_enabled() is False
    monkeypatch.setenv("FAULTLINE_DIGEST_STRATIFICATION", "1")
    assert digest_stratification_enabled() is True


# ── M1-ADDITIVE mechanism (synthetic) ───────────────────────────────────────

def _pressured_uf_world() -> tuple[list[UserFlow], list[Feature], list[dict]]:
    """130 UFs (pressure): 125 heavy mass-ranked, 5 light page-anchored that
    the mass cap cuts. Page evidence rides the member flows' entry files."""
    heavy = [_uf(f"UF-{i:03d}", f"Manage thing {i}", [f"m{i}"], 50)
             for i in range(125)]
    light = [
        _uf("UF-200", "Sign in", ["fl-signin"], 1),
        _uf("UF-201", "Use SSO", ["fl-sso"], 1),
        _uf("UF-202", "View analytics", ["fl-analytics"], 1),
        # UF-104 class: pf=None must anchor the same (stratum independent
        # of product_feature_id).
        _uf("UF-203", "View usage", ["fl-usage"], 1, pf=None),
        # NOT page-anchored — must stay starved (helper entry).
        _uf("UF-204", "Run helper", ["fl-helper"], 1),
    ]
    devs = [
        _feat("auth", [_flow("fl-signin", "src/pages/sign-in.tsx"),
                       _flow("fl-sso", "src/pages/sso-sign-in.tsx")]),
        _feat("analytics", [_flow("fl-analytics", "src/pages/analytics.tsx")]),
        _feat("usage", [_flow("fl-usage", "src/pages/usage.tsx")]),
        _feat("helper", [_flow("fl-helper", "src/lib/helper.ts")]),
    ]
    routes = _api_routes(10)
    return heavy + light, devs, routes


def test_m1_appends_page_anchored_beyond_cap_no_displacement() -> None:
    ufs, devs, routes = _pressured_uf_world()
    pa = _page_anchored_uf_ids(ufs, devs, routes)
    assert {"UF-200", "UF-201", "UF-202", "UF-203"} <= pa
    assert "UF-204" not in pa
    off = _build_digest(devs, [], ufs, routes)
    on = _build_digest(devs, [], ufs, routes, stratified=True,
                       page_anchored=pa)
    # displacement=0: the capped prefix is byte-identical, nothing dropped.
    assert on["current_user_flows"][:MAX_USER_FLOWS_DIGEST] == \
        off["current_user_flows"]
    added = {u["id"] for u in on["current_user_flows"][MAX_USER_FLOWS_DIGEST:]}
    assert added == {"UF-200", "UF-201", "UF-202", "UF-203"}


def test_m1_stratum_independent_of_pf() -> None:
    """UF-104 class: a pf=None journey with a page entry anchors."""
    ufs, devs, routes = _pressured_uf_world()
    pa = _page_anchored_uf_ids(ufs, devs, routes)
    assert "UF-203" in pa  # product_feature_id=None


def test_m1_p1_exclusions_never_anchor() -> None:
    """_app/_document/_error + pages/api entries are NOT product pages."""
    ufs = [
        _uf("UF-300", "App shell", ["fl-app"], 1),
        _uf("UF-301", "Api call", ["fl-api"], 1),
    ]
    devs = [_feat("shell", [_flow("fl-app", "src/pages/_app.tsx"),
                            _flow("fl-api", "src/pages/api/hello.ts")])]
    assert _page_anchored_uf_ids(ufs, devs, []) == frozenset()


# ── M2-HYGIENE-QUOTA mechanism (synthetic) ──────────────────────────────────

def test_m2_hygiene_stories_and_component_paths_excluded() -> None:
    """Twenty exhibits: SignInUp.stories (artifact path) and
    RecordShowPageHeader (filename-echo pattern) never consume the quota;
    real kebab pages do."""
    junk_stories = {
        "pattern": "/auth/__stories__/SignInUp.stories", "method": "PAGE",
        "trigger": "interactive",
        "file": "src/pages/auth/__stories__/SignInUp.stories.tsx",
    }
    junk_component = {
        "pattern": "/object-record/RecordShowPageHeader", "method": "PAGE",
        "trigger": "interactive",
        "file": "src/pages/object-record/RecordShowPageHeader.tsx",
    }
    real_page = {
        "pattern": "/sign-in", "method": "PAGE", "trigger": "interactive",
        "file": "src/pages/sign-in.tsx",
    }
    assert _hygienic_page_route(junk_stories) is False
    assert _hygienic_page_route(junk_component) is False
    assert _hygienic_page_route(real_page) is True
    # Under pressure the real page is quota-reserved; the junk rows (queued
    # after the APIs) are not.
    routes = _api_routes(200) + [junk_stories, junk_component, real_page]
    on = _build_digest([], [], [], routes, stratified=True)
    pats = [r["p"] for r in on["routes"]]
    assert "/sign-in" in pats
    assert "/auth/__stories__/SignInUp.stories" not in pats
    assert "/object-record/RecordShowPageHeader" not in pats
    assert len(on["routes"]) == MAX_ROUTES_DIGEST


def test_m2_dynamic_params_are_not_echo() -> None:
    """camelCase route PARAMS are authored routing, not filename echo."""
    assert _hygienic_page_route({
        "pattern": "/accounts/configuration/:connectedAccountId",
        "method": "PAGE", "file": "src/pages/settings/roads.tsx",
    }) is True
    assert _hygienic_page_route({
        "pattern": "/${ROUTES.SIGN_IN}/*", "method": "PAGE",
        "file": "src/pages/index.ts",
    }) is True


def test_m2_spa_page_rows_ride_page_quota() -> None:
    """BONUS integration: B74 route-table rows (method=PAGE, kind=spa-page)
    legally ride the M2 page stream — novu /auth/sign-in + /auth/sso reach
    the digest under both flags ON."""
    spa = [
        {"pattern": "/auth/sign-in", "method": "PAGE", "kind": "spa-page",
         "trigger": "interactive",
         "file": "apps/dashboard/src/utils/routes.ts"},
        {"pattern": "/auth/sso", "method": "PAGE", "kind": "spa-page",
         "trigger": "interactive",
         "file": "apps/dashboard/src/utils/routes.ts"},
    ]
    routes = _api_routes(200) + spa
    off = _build_digest([], [], [], routes)
    on = _build_digest([], [], [], routes, stratified=True)
    off_pats = [r["p"] for r in off["routes"]]
    pats = [r["p"] for r in on["routes"]]
    assert "/auth/sign-in" not in off_pats and "/auth/sso" not in off_pats
    assert "/auth/sign-in" in pats and "/auth/sso" in pats


# ── Inertness + kill-switch laws ────────────────────────────────────────────

def test_inertness_no_pressure_byte_identical() -> None:
    """Cut-change-only law: UFs <= 120 and routes <= 160 → ON == OFF
    byte-identical (M1 appends nothing, M2 cuts nothing)."""
    ufs = [_uf(f"UF-{i:03d}", f"Do thing {i}", [f"m{i}"], 5)
           for i in range(40)]
    devs = [_feat("auth", [_flow("m1", "src/pages/sign-in.tsx")])]
    routes = _api_routes(50) + [
        {"pattern": "/sign-in", "method": "PAGE", "trigger": "interactive",
         "file": "src/pages/sign-in.tsx"},
    ]
    pa = _page_anchored_uf_ids(ufs, devs, routes)
    off = _build_digest(devs, [], ufs, routes)
    on = _build_digest(devs, [], ufs, routes, stratified=True,
                       page_anchored=pa)
    assert _digest_bytes(on) == _digest_bytes(off)


def test_killswitch_unset_equals_zero(monkeypatch: Any) -> None:
    """unset == "0" == "false": all three read as OFF (the 4-way A/B/C/D
    law at the flag-helper level)."""
    for val in (None, "0", "false", "off", ""):
        if val is None:
            monkeypatch.delenv("FAULTLINE_DIGEST_STRATIFICATION",
                               raising=False)
        else:
            monkeypatch.setenv("FAULTLINE_DIGEST_STRATIFICATION", val)
        assert digest_stratification_enabled() is False


# ── Captured-fixture units (novu / twenty probe canon) ─────────────────────

def _load(run_dir: Path) -> dict[str, Any]:
    from faultline.replay.capture import load_stage_input

    return load_stage_input(run_dir, 6, "journey_abstraction")


def _digests(state: dict[str, Any]) -> tuple[dict, dict, frozenset[str]]:
    from faultline.pipeline_v2.spine_hygiene import is_facet
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _rollup_split_view,
    )

    feats = state["features"]
    non_facet = [f for f in feats if not is_facet(f)]
    dev_view, _ = _rollup_split_view(non_facet)
    ufs, pfs, ri = (state["user_flows"], state["product_features"],
                    state["routes_index"])
    pa = _page_anchored_uf_ids(ufs, feats, ri)
    off = _build_digest(dev_view, pfs, ufs, ri)
    on = _build_digest(dev_view, pfs, ufs, ri, stratified=True,
                       page_anchored=pa)
    return off, on, pa


def _token_count(digest: dict[str, Any], token: str) -> int:
    txt = re.sub(r"[^a-z0-9]+", " ",
                 json.dumps(digest, ensure_ascii=False).lower())
    return len(re.findall(r"\b" + re.escape(token) + r"\b", txt))


@_needs_novu
def test_novu_capture_sign_in_sso_tokens_recovered() -> None:
    """SEG H forensics: baseline digest carries 'sign in'=0 / 'sso'=0 →
    0/12 strict draws propose 'Sign in'. Stratified digest carries both."""
    off, on, _pa = _digests(_load(_NOVU_RUN))
    assert _token_count(off, "sign in") == 0
    assert _token_count(off, "sso") == 0
    assert _token_count(on, "sign in") > 0
    assert _token_count(on, "sso") > 0
    assert _token_count(on, "analytics") >= _token_count(off, "analytics")


@_needs_novu
def test_novu_capture_displacement_zero() -> None:
    """M1-ADDITIVE: every baseline-120 UF row survives byte-identically
    (the fixed-cap form starved 5/67 cached proposals — refuted)."""
    off, on, pa = _digests(_load(_NOVU_RUN))
    assert len(pa) == 46  # probe canon
    assert on["current_user_flows"][:MAX_USER_FLOWS_DIGEST] == \
        off["current_user_flows"]
    appended = on["current_user_flows"][MAX_USER_FLOWS_DIGEST:]
    assert len(appended) > 0
    assert {u["id"] for u in appended} <= pa


@_needs_novu
def test_novu_capture_quota_admits_targets() -> None:
    """The hygienic page stream targets sit at idx 2/11/53/55 (< quota 80):
    /sign-in, /sso-sign-in, /analytics all reach the digest routes."""
    _off, on, _pa = _digests(_load(_NOVU_RUN))
    pats = [str(r["p"]) for r in on["routes"]]
    assert "/sign-in" in pats
    assert "/sso-sign-in" in pats
    assert "/analytics" in pats
    n_pages = sum(1 for r in on["routes"]
                  if str(r["m"]).upper() == "PAGE")
    assert 0 < n_pages <= MAX_ROUTES_DIGEST // 2 + 1


@_needs_twenty
def test_twenty_capture_uf_channel_noop() -> None:
    """twenty: 111 UFs < 120 cap → M1 appends nothing (no UF pressure)."""
    off, on, _pa = _digests(_load(_TWENTY_RUN))
    assert on["current_user_flows"] == off["current_user_flows"]


@_needs_twenty
def test_twenty_capture_hygiene_excludes_exhibits() -> None:
    """The captured twenty PAGE stream carries the two exhibit classes —
    neither reaches the stratified digest routes."""
    _off, on, _pa = _digests(_load(_TWENTY_RUN))
    pats = [str(r["p"]) for r in on["routes"]]
    assert "/auth/__stories__/SignInUp.stories" not in pats
    assert "/object-record/RecordShowPageHeader" not in pats
