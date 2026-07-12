"""B37-ph2 — dispatch-mint homing to the target-file owner PF + the Stage
6.987 devgrain I9 rider.

Covers the spec's Метод anti-cases:
  * a mint whose target-owner != first-attribution owner is re-homed;
  * a PF/UF without dispatch mints is untouched;
  * homing never steals a mint from the true owner (no-op byte-identity);
  * a single dispatch mint inside a rich journey never moves it (minority);
  * determinism (board-state, not synthesis order);
  * flowful-demote → owner-homing vs flowless-demote → the lane (I9 rider),
    and the flag-OFF byte-identity of both passes.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from faultline.pipeline_v2.devgrain_demote import (
    FDIR_DEVGRAIN_GATE_ENV,
    _SHARED_REASON_BAR,
    run_devgrain_demote,
)
from faultline.pipeline_v2.dispatch_homing import (
    DISPATCH_HOMING_ENV,
    build_anchor_owner_resolver,
    dispatch_homing_enabled,
    home_dispatch_mints,
)


def _ns(**kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


def _pf(name: str, anchor: str | None) -> types.SimpleNamespace:
    return _ns(name=name, id=None, anchor_id=anchor, display_name=name,
               paths=[], member_files=[])


def _mint(uuid: str, target: str, name: str = "run-x-flow") -> types.SimpleNamespace:
    return _ns(uuid=uuid, name=name, entry_point_file=target,
               description=f"dispatch registry some/registry.ts ['{uuid}']")


def _plain(uuid: str, target: str) -> types.SimpleNamespace:
    return _ns(uuid=uuid, name="do-thing-flow", entry_point_file=target,
               description="")


def _dev(name: str, pfid: str | None, flows: list[Any]) -> types.SimpleNamespace:
    return _ns(name=name, layer="developer", product_feature_id=pfid,
               flows=flows, anchor_id=None, shared_reason=None,
               paths=[], member_files=[], display_name=name)


def _uf(name: str, pfid: str | None, member_ids: list[str]) -> types.SimpleNamespace:
    return _ns(name=name, product_feature_id=pfid,
               member_flow_ids=list(member_ids), member_count=len(member_ids))


# ── flag ─────────────────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DISPATCH_HOMING_ENV, raising=False)
    assert dispatch_homing_enabled() is False
    for v in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(DISPATCH_HOMING_ENV, v)
        assert dispatch_homing_enabled() is False
    for v in ("1", "true", "on", "yes"):
        monkeypatch.setenv(DISPATCH_HOMING_ENV, v)
        assert dispatch_homing_enabled() is True


# ── resolver: the anchor-chain walk ──────────────────────────────────────────


def test_resolver_longest_prefix_and_url_slug() -> None:
    pfs = [
        _pf("integrations", "fdir:apps/studio/components/interfaces/Integrations"),
        _pf("functions", "fdir:apps/studio/components/interfaces/Functions"),
        _pf("logout", "route:logout"),  # bare URL slug — never a fs prefix
    ]
    r = build_anchor_owner_resolver(pfs)
    # nested target → the enclosing (longest-prefix) PF
    assert r("apps/studio/components/interfaces/Integrations/CronJobs/Page.tsx") \
        == "integrations"
    assert r("apps/studio/components/interfaces/Functions/FnPage.tsx") \
        == "functions"
    # a URL-slug anchor never matches a real file path
    assert r("apps/studio/pages/other.tsx") is None
    assert r("logout") == "logout"  # exact single-segment match still resolves
    assert r(None) is None


def test_resolver_longer_prefix_wins_over_ancestor() -> None:
    pfs = [
        _pf("app-store", "fdir:packages/app-store"),
        _pf("cal-video", "fdir:packages/app-store/dailyvideo"),
    ]
    r = build_anchor_owner_resolver(pfs)
    # the mint's target lives under BOTH anchors — the MORE SPECIFIC one wins
    # (the cal.com Cal Video exhibit: dailyvideo, not the App Store parent).
    assert r("packages/app-store/dailyvideo/api/VideoApiAdapter.ts") == "cal-video"
    assert r("packages/app-store/other/index.ts") == "app-store"


# ── homing: moves a mint to its target owner ─────────────────────────────────


def test_home_moves_mint_to_target_owner() -> None:
    """The cal.com class: mint homed to the first-attribution PF (App Store)
    re-homes to the anchor-chain owner (Cal Video / dailyvideo)."""
    pfs = [
        _pf("app-store", "fdir:packages/app-store"),
        _pf("cal-video", "fdir:packages/app-store/dailyvideo"),
    ]
    mint = _mint("m1", "packages/app-store/dailyvideo/api/VideoApiAdapter.ts",
                 name="run-dailyvideo-video-api-adapter-flow")
    dev = _dev("app-store-dev", "app-store", [mint])
    uf = _uf("run-dailyvideo-video-api-adapter-flow", "app-store", ["m1"])
    keeper = _uf("browse-app-store-flow", "app-store", ["k1"])  # source cover
    tele = home_dispatch_mints([uf, keeper], [dev], pfs)
    assert tele["rehomed"] == 1
    assert uf.product_feature_id == "cal-video"
    assert keeper.product_feature_id == "app-store"  # untouched
    assert tele["moves"][0]["from"] == "app-store"
    assert tele["moves"][0]["to"] == "cal-video"


def test_orphan_guard_never_strips_source_last_journey() -> None:
    """The midday 'Run accounting' class: a dispatch UF that is its source
    PF's ONLY member-ful journey is NOT moved (stripping it silently re-arms
    I8 — the Stage 6.99 i16-rehome orphan-guard precedent)."""
    pfs = [_pf("app-store", "fdir:packages/app-store"),
           _pf("cal-video", "fdir:packages/app-store/dailyvideo")]
    mint = _mint("m1", "packages/app-store/dailyvideo/api/VideoApiAdapter.ts")
    dev = _dev("app-store-dev", "app-store", [mint])
    uf = _uf("run-adapter-flow", "app-store", ["m1"])  # app-store's ONLY UF
    tele = home_dispatch_mints([uf], [dev], pfs)
    assert tele["rehomed"] == 0
    assert tele["skipped_orphan_guard"] == 1
    assert uf.product_feature_id == "app-store"  # kept — source not starved


def test_no_op_when_already_owns_target() -> None:
    """No-op guard — a mint already homed to its target owner is untouched
    (byte-identity for correctly-homed mints)."""
    pfs = [_pf("cal-video", "fdir:packages/app-store/dailyvideo")]
    mint = _mint("m1", "packages/app-store/dailyvideo/api/VideoApiAdapter.ts")
    dev = _dev("cal-video-dev", "cal-video", [mint])
    uf = _uf("run-adapter-flow", "cal-video", ["m1"])
    tele = home_dispatch_mints([uf], [dev], pfs)
    assert tele["rehomed"] == 0
    assert tele["skipped_noop"] == 1
    assert uf.product_feature_id == "cal-video"  # unchanged


def test_pf_without_dispatch_mints_untouched() -> None:
    pfs = [_pf("billing", "fdir:apps/web/billing"),
           _pf("other", "fdir:apps/web/other")]
    plain = _plain("p1", "apps/web/other/foo.ts")
    dev = _dev("billing-dev", "billing", [plain])
    uf = _uf("manage-billing-flow", "billing", ["p1"])
    tele = home_dispatch_mints([uf], [dev], pfs)
    assert tele["rehomed"] == 0
    assert tele["candidates"] == 0  # no dispatch mint members at all
    assert uf.product_feature_id == "billing"


def test_minority_dispatch_mint_not_moved() -> None:
    """A single dispatch mint inside a rich journey (minority of members)
    never hijacks the whole journey."""
    pfs = [_pf("connectors", "fdir:apps/api/connectors"),
           _pf("dashboard", "fdir:apps/web/dashboard")]
    mint = _mint("m1", "apps/api/connectors/callback.ts")
    plains = [_plain(f"p{i}", "apps/web/dashboard/x.ts") for i in range(3)]
    dev = _dev("dash-dev", "dashboard", [mint, *plains])
    uf = _uf("browse-dashboard-flow", "dashboard", ["m1", "p0", "p1", "p2"])
    tele = home_dispatch_mints([uf], [dev], pfs)
    assert tele["rehomed"] == 0
    assert tele["skipped_not_majority"] == 1
    assert uf.product_feature_id == "dashboard"  # rich journey stays put


def test_owner_not_a_real_pf_key_skipped() -> None:
    """The target file resolves to no emitted PF anchor → no move."""
    pfs = [_pf("billing", "fdir:apps/web/billing")]
    mint = _mint("m1", "apps/api/unowned/handler.ts")
    dev = _dev("billing-dev", "billing", [mint])
    uf = _uf("run-handler-flow", "billing", ["m1"])
    tele = home_dispatch_mints([uf], [dev], pfs)
    assert tele["rehomed"] == 0
    assert tele["skipped_no_owner"] == 1
    assert uf.product_feature_id == "billing"


# ── path_index-first resolution (i16-consistency / regression guard) ─────────


def _owned_dev(name: str, uuid: str, pfid: str | None,
               flows: list[Any]) -> types.SimpleNamespace:
    return _ns(name=name, layer="developer", uuid=uuid,
               product_feature_id=pfid, flows=flows, anchor_id=None,
               shared_reason=None, paths=[], member_files=[], display_name=name)


def test_path_index_owner_prevents_cross_package_mismove() -> None:
    """The midday fortnox regression: the target lives under the
    packages/accounting ANCHOR but is DEV-OWNED (path_index) by app-store.
    Leading with path_index keeps it on app-store (== current) — a NO-OP —
    instead of the anchor-chain's wrong move to accounting (which i16 would
    revert, leaving a phantom app-store gap)."""
    pfs = [_pf("app-store", "ws:packages/app-store"),
           _pf("accounting", "ws:packages/accounting")]
    entry = "packages/accounting/src/providers/fortnox.ts"
    dev = _owned_dev("d", "dev1", "app-store", [_mint("m1", entry)])
    uf = _uf("run-fortnox-flow", "app-store", ["m1"])
    path_index = {entry: {"feature_uuid": "dev1"}}  # dev1 → app-store
    tele = home_dispatch_mints([uf], [dev], pfs, path_index=path_index)
    assert tele["rehomed"] == 0 and tele["skipped_noop"] == 1
    assert uf.product_feature_id == "app-store"  # NOT accounting


def test_path_index_owner_beats_anchor_chain() -> None:
    """path_index dev→PF wins over the file-path anchor: the target sits
    under apps/b (anchor → pf-b) but its dev is owned by pf-a; a UF frozen
    on pf-c re-homes to the path_index owner pf-a, not the anchor pf-b."""
    pfs = [_pf("pf-a", "fdir:apps/a"), _pf("pf-b", "fdir:apps/b"),
           _pf("pf-c", "fdir:apps/c")]
    entry = "apps/b/handler.ts"
    dev = _owned_dev("d", "dev1", "pf-a", [_mint("m1", entry)])
    uf = _uf("run-handler-flow", "pf-c", ["m1"])
    keeper = _uf("browse-c-flow", "pf-c", ["k1"])  # pf-c retains cover
    path_index = {entry: {"feature_uuid": "dev1"}}  # dev1 → pf-a
    tele = home_dispatch_mints([uf, keeper], [dev], pfs, path_index=path_index)
    assert tele["rehomed"] == 1
    assert uf.product_feature_id == "pf-a"  # path_index owner, NOT anchor pf-b


def test_unowned_target_falls_back_to_anchor_chain() -> None:
    """When the target file is UNOWNED in path_index (a pf=None dev — the
    supabase studio-mint shape), fall back to the anchor-chain enclosing PF
    (i16 sees the entry as unowned, so it never reverts)."""
    pfs = [_pf("integrations", "fdir:apps/studio/interfaces/Integrations"),
           _pf("projects", "fdir:apps/studio")]
    entry = "apps/studio/interfaces/Integrations/CronJobs/Page.tsx"
    dev = _owned_dev("d", "dev1", None, [_mint("m1", entry)])  # unowned
    uf = _uf("run-cron-flow", "projects", ["m1"])
    keeper = _uf("browse-projects-flow", "projects", ["k1"])  # projects cover
    path_index = {entry: {"feature_uuid": "dev1"}}  # dev1 → None
    tele = home_dispatch_mints([uf, keeper], [dev], pfs, path_index=path_index)
    assert tele["rehomed"] == 1
    assert uf.product_feature_id == "integrations"  # anchor-chain fallback


def test_homing_deterministic_board_state_not_order() -> None:
    """Same board state ⇒ same homing outcome regardless of UF order."""
    pfs = [
        _pf("app-store", "fdir:packages/app-store"),
        _pf("cal-video", "fdir:packages/app-store/dailyvideo"),
        _pf("zoom", "fdir:packages/app-store/zoomvideo"),
    ]
    devs = [
        _dev("d1", "app-store",
             [_mint("m1", "packages/app-store/dailyvideo/api/A.ts")]),
        _dev("d2", "app-store",
             [_mint("m2", "packages/app-store/zoomvideo/api/B.ts")]),
    ]

    def run(order: list[int]) -> list[tuple[str, str | None]]:
        ufs = [
            _uf("uf-a", "app-store", ["m1"]),
            _uf("uf-b", "app-store", ["m2"]),
            _uf("uf-keep", "app-store", ["k1"]),  # source keeps cover
        ]
        ufs = [ufs[i] for i in order]
        home_dispatch_mints(ufs, devs, pfs)
        return sorted((u.name, u.product_feature_id) for u in ufs)

    assert run([0, 1, 2]) == run([2, 1, 0]) == [
        ("uf-a", "cal-video"), ("uf-b", "zoom"), ("uf-keep", "app-store")]


# ── devgrain I9 rider (Stage 6.987) ──────────────────────────────────────────


def _devgrain_scene() -> tuple[list, list, list]:
    """A 'welcome' journey-step PF demotes (route:welcome, micro journey,
    not nav-declared). Its FLOWFUL dev's flow entry lives under a surviving
    'account' PF anchored at apps/web/modules/account (a non-journey-step
    leaf, so it is never itself eligible to demote)."""
    demoted = _pf("welcome", "route:welcome")
    account = _pf("account", "fdir:apps/web/modules/account")
    real = _pf("dashboard", "fdir:apps/web/dashboard")
    pfs = [demoted, account, real]
    ufs = [_uf("Welcome", "welcome", ["w1"])]  # micro (1 member) → demotes
    return pfs, ufs, [account, real]


def test_devgrain_i9_flowful_homes_to_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    monkeypatch.setenv(DISPATCH_HOMING_ENV, "1")
    pfs, ufs, _survivors = _devgrain_scene()
    dev = _dev("welcome-dev", "welcome", [
        _ns(entry_point_file="apps/web/modules/account/welcome/Welcome.tsx")])
    tele = run_devgrain_demote([dev], pfs, ufs,
                               nav_keys=frozenset({"dashboard"}))
    assert tele["devs_i9_homed"] == 1
    assert tele["devs_unowned"] == 0
    # flowful dev homed to the target-owner PF (NOT the platform lane)
    assert dev.product_feature_id == "account"
    assert dev.shared_reason is None
    assert dev.shared_reason != _SHARED_REASON_BAR


def test_devgrain_i9_flowless_keeps_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    monkeypatch.setenv(DISPATCH_HOMING_ENV, "1")
    pfs, ufs, _survivors = _devgrain_scene()
    dev = _dev("welcome-dev", "welcome", [])  # FLOWLESS
    tele = run_devgrain_demote([dev], pfs, ufs,
                               nav_keys=frozenset({"dashboard"}))
    assert tele["devs_i9_homed"] == 0
    assert tele["devs_unowned"] == 1  # flowless → the platform lane
    assert dev.product_feature_id is None
    assert dev.shared_reason == _SHARED_REASON_BAR


def test_devgrain_i9_flowful_no_owner_not_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    monkeypatch.setenv(DISPATCH_HOMING_ENV, "1")
    pfs, ufs, _survivors = _devgrain_scene()
    # flow entry resolves to NO surviving PF anchor
    dev = _dev("welcome-dev", "welcome", [
        _ns(entry_point_file="apps/web/unmapped/Welcome.tsx")])
    tele = run_devgrain_demote([dev], pfs, ufs,
                               nav_keys=frozenset({"dashboard"}))
    assert tele["devs_flowful_unowned"] == 1
    assert tele["devs_unowned"] == 0  # a flowful dev is NEVER a lane resident
    assert dev.product_feature_id is None
    assert dev.shared_reason is None  # NOT lane-bound
    assert dev.shared_reason != _SHARED_REASON_BAR


def test_devgrain_i9_off_flowful_goes_to_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Homing flag OFF ⇒ byte-identical to pre-B37-ph2: a flowful demoted
    dev with no ancestor falls to the lane exactly as before."""
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    monkeypatch.setenv(DISPATCH_HOMING_ENV, "0")
    pfs, ufs, _survivors = _devgrain_scene()
    dev = _dev("welcome-dev", "welcome", [
        _ns(entry_point_file="apps/web/modules/account/welcome/Welcome.tsx")])
    tele = run_devgrain_demote([dev], pfs, ufs,
                               nav_keys=frozenset({"dashboard"}))
    # I9 counters are ABSENT when OFF — the devgrain telemetry is byte-identical
    # to pre-B37-ph2 (the flag adds no keys unless it acts).
    assert "devs_i9_homed" not in tele
    assert "devs_flowful_unowned" not in tele
    assert tele["devs_unowned"] == 1
    assert dev.product_feature_id is None
    assert dev.shared_reason == _SHARED_REASON_BAR  # lane, pre-B37-ph2 behavior
