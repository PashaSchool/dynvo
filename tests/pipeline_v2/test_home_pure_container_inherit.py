"""B74 Seg C — home-pure: ws-pkg container ≠ foreignness (default OFF).

``FAULTLINE_HOME_PURE_CONTAINER_INHERIT``: a journey member whose HOME
PF is a monorepo ws-pkg CONTAINER (anchored-mint ``anchor_id`` "ws:"
marker — Form A, the only detector; mass/ratio forms refuted by the
2026-07-19 probe) is inheritable like lane/unowned — on the CITED
channels only (Pass-1 from_flows + Pass-2a cited devs). The whole-pool
2b token rescue and the route backfill stay home-STRICT (claim-greed
law). Foreignness is unchanged when home is a SIBLING capability.

Named exhibits pinned here (the spec's survivors):
  - twenty  'Sign in and authenticate'   — rescue shape 0 -> 11 members
  - twenty  'Manage email blocklist'     — ANTI-CASE: stays filtered
    (home=settings is a route-anchored sibling — the rule holds)
  - twenty  'Submit partner application' — NOT killed (2b stays strict)
  - novu    'Authenticate CLI device session' — cited-dev 2a rescue
  - sibling-leak == 0 invariant across all channels
  - unset => byte-behavior; armed w/o ws-containers => inert
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _build_user_flows,
    _container_pf_keys,
    _flow_home_map,
    run_journey_abstraction,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ENV = "FAULTLINE_HOME_PURE_CONTAINER_INHERIT"


def _flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, uuid=f"uuid-{name}", paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, entry_point_file=entry,
    )


def _dev(name: str, pfid: str | None, paths: list[str],
         flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=paths, authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", product_feature_id=pfid,
        flows=flows or [],
    )


def _pf(name: str, display: str, anchor: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=display, paths=[], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="product", anchor_id=anchor,
    )


def _uf(uid: str, name: str, pfid: str, member_ids: list[str]) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource="auth", domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=member_ids, member_count=len(member_ids),
    )


_CONTAINERS = frozenset({"twenty-front"})


def _twenty_signin_world():
    """The twenty exhibit: dev ``auth`` stamped to the ws-pkg container
    ``twenty-front`` — every sign-in flow's HOME is the container, so
    strict home-pure filtered the ENTIRE journey (0 members, dropped)."""
    flows = [
        _flow(f"sign-in-step-{i}-flow",
              f"packages/twenty-front/src/auth/step{i}.tsx")
        for i in range(11)
    ]
    d_auth = _dev("auth", "twenty-front",
                  [f.entry_point_file for f in flows], flows)
    old = _uf("UF-001", "Sign in", "twenty-front", [f.uuid for f in flows])
    spec = {
        "name": "Sign in and authenticate", "resource": "auth",
        "product_feature": "Auth",
        "from_flows": ["UF-001"], "from_dev_features": [],
    }
    return d_auth, old, spec, flows


# ── twenty 'Sign in and authenticate' — rescue shape 0 -> 11 ────────────


def test_twenty_sign_in_rescue_shape_0_to_11(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    d_auth, old, spec, flows = _twenty_signin_world()
    ufs, tele = _build_user_flows(
        [spec], [old], [d_auth], [], home_pure=True,
        container_pf_keys=_CONTAINERS)
    (uf,) = ufs
    assert uf.name == "Sign in and authenticate"
    assert uf.member_flow_ids == [f.uuid for f in flows]
    assert uf.member_count == 11
    assert tele["uf_home_container_inherited"] == 11
    assert tele["uf_home_filtered"] == 0
    assert tele["uf_dropped_names"] == []


def test_unset_keeps_strict_filter_byte_behavior(monkeypatch) -> None:
    """unset => byte-behavior: the container set is dead weight — the
    journey is filtered/dropped exactly like a run given NO set, and no
    new telemetry key appears."""
    monkeypatch.delenv(_ENV, raising=False)
    d_auth, old, spec, _flows_ = _twenty_signin_world()
    ufs, tele = _build_user_flows(
        [spec], [old], [d_auth], [], home_pure=True,
        container_pf_keys=_CONTAINERS)
    assert ufs == []
    assert "Sign in and authenticate" in tele["uf_dropped_names"]
    assert "uf_home_container_inherited" not in tele
    # Identical to the no-set world (the exact pre-B74 behavior).
    d2, old2, spec2, _f2 = _twenty_signin_world()
    ufs_ref, tele_ref = _build_user_flows(
        [spec2], [old2], [d2], [], home_pure=True, container_pf_keys=None)
    assert ufs == ufs_ref
    assert tele == tele_ref


def test_flag_off_value_zero_matches_unset(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "0")
    d_auth, old, spec, _flows_ = _twenty_signin_world()
    ufs, tele = _build_user_flows(
        [spec], [old], [d_auth], [], home_pure=True,
        container_pf_keys=_CONTAINERS)
    assert ufs == []
    assert "uf_home_container_inherited" not in tele


def test_armed_without_ws_containers_is_inert(monkeypatch) -> None:
    """openstatus analog: flag armed on a repo with NO ws-pkg containers
    — empty container set, strict behavior, no new telemetry key."""
    monkeypatch.setenv(_ENV, "1")
    d_auth, old, spec, _flows_ = _twenty_signin_world()
    ufs, tele = _build_user_flows(
        [spec], [old], [d_auth], [], home_pure=True,
        container_pf_keys=frozenset())
    assert ufs == []
    assert "uf_home_container_inherited" not in tele
    monkeypatch.delenv(_ENV, raising=False)
    d2, old2, spec2, _f2 = _twenty_signin_world()
    ufs_ref, tele_ref = _build_user_flows(
        [spec2], [old2], [d2], [], home_pure=True, container_pf_keys=None)
    assert ufs == ufs_ref
    assert tele == tele_ref


# ── ANTI-CASE: 'Manage email blocklist' stays filtered ──────────────────


def test_manage_email_blocklist_stays_filtered(monkeypatch) -> None:
    """home=settings is a route-anchored SIBLING capability (anchor_id
    'route:/settings', not 'ws:') — foreignness law holds under the flag."""
    monkeypatch.setenv(_ENV, "1")
    fl = _flow("email-blocklist-table-flow",
               "packages/twenty-front/src/settings/blocklist.tsx")
    d_settings = _dev("settings-email", "settings",
                      [fl.entry_point_file], [fl])
    old = _uf("UF-002", "Manage settings", "settings", [fl.uuid])
    spec = {
        "name": "Manage email blocklist", "resource": "blocklist",
        "product_feature": "Email Blocklist",
        "from_flows": ["UF-002"], "from_dev_features": [],
    }
    ufs, tele = _build_user_flows(
        [spec], [old], [d_settings], [], home_pure=True,
        container_pf_keys=_CONTAINERS)
    assert ufs == []
    assert "Manage email blocklist" in tele["uf_dropped_names"]
    assert tele["uf_home_container_inherited"] == 0
    assert tele["uf_home_filtered"] > 0


# ── 'Submit partner application' is NOT killed (2b stays strict) ────────


def test_submit_partner_application_not_killed_by_2b_greed(monkeypatch) -> None:
    """An earlier journey may NOT whole-pool-rescue container-homed flows
    (2b stays home-STRICT under the flag) — the rightful journey still
    claims them via its cited-dev channel and survives."""
    monkeypatch.setenv(_ENV, "1")
    fl = _flow("partner-application-form-flow",
               "packages/twenty-front/src/partner/form.tsx")
    d_partner = _dev("partner", "twenty-front", [fl.entry_point_file], [fl])
    greedy = {
        # Emission-order FIRST; 'partner' resource token-matches the flow,
        # so a container-inheriting 2b would claim-greed it here.
        "name": "Manage partners", "resource": "partner",
        "product_feature": "Partners Hub",
        "from_flows": [], "from_dev_features": [],
    }
    rightful = {
        "name": "Submit partner application", "resource": "application",
        "product_feature": "Partner Applications",
        "from_flows": [], "from_dev_features": ["partner"],
    }
    ufs, tele = _build_user_flows(
        [greedy, rightful], [], [d_partner], [], home_pure=True,
        container_pf_keys=_CONTAINERS)
    assert [u.name for u in ufs] == ["Submit partner application"]
    assert ufs[0].member_flow_ids == [fl.uuid]
    assert "Manage partners" in tele["uf_dropped_names"]
    assert tele["uf_dev_grounded"] == 1
    assert tele["uf_rescued_flows"] == 0  # 2b never fired a rescue


# ── novu 'Authenticate CLI device session' — cited-dev 2a rescue ────────


def test_novu_cli_device_session_cited_dev_rescue(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    fl = _flow("authenticate-cli-device-flow", "apps/web/src/cli/auth.tsx")
    d_cli = _dev("cli-auth", "novu-web", [fl.entry_point_file], [fl])
    spec = {
        "name": "Authenticate CLI device session", "resource": "device",
        "product_feature": "CLI Auth",
        "from_flows": [], "from_dev_features": ["cli-auth"],
    }
    ufs, tele = _build_user_flows(
        [spec], [], [d_cli], [], home_pure=True,
        container_pf_keys=frozenset({"novu-web"}))
    (uf,) = ufs
    assert uf.member_flow_ids == [fl.uuid]
    assert tele["uf_dev_grounded"] == 1
    assert tele["uf_home_container_inherited"] == 1


# ── sibling-leak == 0 invariant ─────────────────────────────────────────


def test_sibling_leak_zero_invariant(monkeypatch) -> None:
    """With the flag armed, NO emitted journey holds a member whose HOME
    is a sibling capability (non-container, != the journey's own PF) —
    the inherit widens membership to container homes ONLY."""
    monkeypatch.setenv(_ENV, "1")
    a1 = _flow("sign-in-form-flow", "packages/twenty-front/src/auth/a.tsx")
    a2 = _flow("verify-mfa-flow", "packages/twenty-front/src/auth/b.tsx")
    s1 = _flow("email-blocklist-table-flow",
               "packages/twenty-front/src/settings/bl.tsx")
    d_auth = _dev("auth", "twenty-front",
                  [a1.entry_point_file, a2.entry_point_file], [a1, a2])
    d_settings = _dev("settings-email", "settings",
                      [s1.entry_point_file], [s1])
    devs = [d_auth, d_settings]
    # One mixed deterministic UF — the classic swap shape.
    old = _uf("UF-001", "Do auth things", "twenty-front",
              [a1.uuid, a2.uuid, s1.uuid])
    specs = [
        {"name": "Sign in and authenticate", "resource": "auth",
         "product_feature": "Auth",
         "from_flows": ["UF-001"], "from_dev_features": []},
        {"name": "Manage email blocklist", "resource": "blocklist",
         "product_feature": "Email Blocklist",
         "from_flows": ["UF-001"], "from_dev_features": []},
    ]
    ufs, _tele = _build_user_flows(
        specs, [old], devs, [], home_pure=True,
        container_pf_keys=_CONTAINERS)
    home = _flow_home_map(devs)
    leaks = [
        (u.name, mid, home.get(mid))
        for u in ufs for mid in u.member_flow_ids
        if home.get(mid) is not None
        and home.get(mid) != u.product_feature_id
        and home.get(mid) not in _CONTAINERS
    ]
    assert leaks == []
    # And the container inherit actually fired (the invariant is not
    # vacuous): the auth journey holds the two container-homed flows.
    by_name = {u.name: u for u in ufs}
    assert set(by_name["Sign in and authenticate"].member_flow_ids) == {
        a1.uuid, a2.uuid}
    # The settings-homed flow never leaked into the auth journey.
    assert s1.uuid not in by_name["Sign in and authenticate"].member_flow_ids


# ── Form A detector ─────────────────────────────────────────────────────


def test_container_detector_is_ws_marker_only() -> None:
    pfs = [
        _pf("twenty-front", "Twenty Front", anchor="ws:packages/twenty-front"),
        _pf("settings", "Settings", anchor="route:/settings"),
        _pf("stripe", "Stripe", anchor="dep:stripe"),
        _pf("plain", "Plain", anchor=None),
    ]
    assert _container_pf_keys(pfs) == frozenset({"twenty-front"})


def test_container_detector_tolerates_missing_attr() -> None:
    assert _container_pf_keys(
        [SimpleNamespace(name="x")]) == frozenset()  # type: ignore[list-item]


# ── Plumbing: run_journey_abstraction threads the mint-universe set ─────


class _Client:
    """Abstraction payload for Call 1; a dev->PF map for the Call-2-era
    re-attribution prompt (un-anchored path only)."""

    def __init__(self, payload: str, reattrib: str = '{"map":{}}') -> None:
        self._payload = payload
        self._reattrib = reattrib
        self.messages = self

    def create(self, **kw):  # noqa: ANN003
        sysp = kw.get("system", "")
        text = (self._reattrib
                if "assign each developer feature" in sysp else self._payload)
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


_PAYLOAD = (
    '{"product_features":[{"name":"Auth","description":"auth"}],'
    '"user_flows":[{"name":"Sign in and authenticate","resource":"auth",'
    '"product_feature":"Auth","from_flows":["UF-001"],'
    '"from_dev_features":["auth"]}]}'
)


def test_run_journey_abstraction_threads_container_keys(monkeypatch) -> None:
    """The container set is derived from the INPUT product_features (the
    phase_finalize:1092 mint universe) and handed to _build_user_flows."""
    import faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction as s67d

    captured: list[frozenset[str] | None] = []
    real = s67d._build_user_flows

    def _spy(*args, **kw):
        captured.append(kw.get("container_pf_keys"))
        return real(*args, **kw)

    monkeypatch.setattr(s67d, "_build_user_flows", _spy)
    d_auth, old, _spec, _flows_ = _twenty_signin_world()
    pfs = [
        _pf("auth", "Auth", anchor="route:/welcome"),
        _pf("twenty-front", "Twenty Front", anchor="ws:packages/twenty-front"),
    ]
    run_journey_abstraction(
        [old], pfs, [d_auth], [], client=_Client(_PAYLOAD), model="m",
        anchored=True,
    )
    assert captured == [frozenset({"twenty-front"})]


def test_run_journey_abstraction_unanchored_passes_no_keys(monkeypatch) -> None:
    import faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction as s67d

    captured: list[frozenset[str] | None] = []
    real = s67d._build_user_flows

    def _spy(*args, **kw):
        captured.append(kw.get("container_pf_keys"))
        return real(*args, **kw)

    monkeypatch.setattr(s67d, "_build_user_flows", _spy)
    d_auth, old, _spec, _flows_ = _twenty_signin_world()
    pfs = [_pf("auth", "Auth", anchor="route:/welcome")]
    run_journey_abstraction(
        [old], pfs, [d_auth], [],
        client=_Client(_PAYLOAD, reattrib='{"map":{"auth":"Auth"}}'),
        model="m", anchored=False,
    )
    assert captured == [None]
