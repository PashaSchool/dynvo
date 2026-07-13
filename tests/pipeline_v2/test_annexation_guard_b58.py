"""B58 — container-anchor annexation guard (FAULTLINE_ANNEXATION_GUARD).

Forensics (fresh keyed boards, 2026-07-13): a legit SMALL anchor core
annexes a HUGE foreign mass through the mint's rescue rungs —

  * plane ``Issue`` (svc anchor, 2,140 own LOC) ended up 174,254 LOC /
    53% of the board: the flowful ``i18n`` ws-package dev (125,566 LOC,
    552 files, ALL inside packages/i18n) walk-folded into it because the
    same-key MERGE made the host's evidence multi-unit ⇒ the B22a
    unit-unanimity test returned ``None`` ⇒ never foreign;
  * cal.com ``Bookings`` annexed apps/api/v2 (45K, 825 files) through
    the ENTRY rung, which B22a never wrapped;
  * novu ``Notifications`` is anchored at route:playground/nextjs/… —
    an example app the repo's own pnpm-workspace.yaml parks under
    ``playground/*`` ("# playground apps").

Seg A: with the guard armed, a unit-coherent flowful dev is never
force-bound (entry / span / walk) onto a target whose CANONICAL unit is
a different workspace unit — it lanes with its own unit (B22a's honest
refusal, ``cross_unit_isolation``).

Seg B: an anchor wholly inside a dev-artifact-rooted workspace unit
(unit-ROOT segments ∩ unit_root_artifact_tokens) never mints
(``dev_artifact_unit``). Unit-root grain: a product PAGE named
``playground`` inside apps/web (cal.com admin page) can never fire.

Default OFF: every test asserts the OFF world reproduces the annexation
byte-for-byte in behaviour (tele counters absent, dev annexed).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    _SHARED_REASON_CROSS_UNIT,
    build_platform_infrastructure_lane,
    run_anchored_mint,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id="old-pf",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ws(path: str) -> SimpleNamespace:
    return SimpleNamespace(name=path.rsplit("/", 1)[-1], path=path, files=[])


def ctx_of(workspaces=None, tracked=None, repo_path=".") -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path(repo_path), monorepo=bool(workspaces),
    )


_WORKSPACES = [ws("apps/web"), ws("packages/lib"), ws("packages/common")]


def _plane_fixture() -> tuple[list[Feature], list[dict]]:
    """The plane ``Issue`` shape: the ``board`` capability has dir-style
    page routes in TWO units (apps/web + packages/lib) — the same-KEY
    merge unions their evidence into ONE anchor whose evidence is
    multi-unit ⇒ the B22a unit-unanimity test yields ``None`` ⇒ the host
    is never foreign to anyone. Its canonical id keeps the head's
    embedded path (rung 1 of the B58 canonical-unit ladder).
    ``dash`` is a small same-unit sibling (2 files)."""
    routes = [
        {"pattern": "/board", "method": "PAGE",
         "file": "apps/web/src/pages/board/index.tsx"},
        {"pattern": "/board", "method": "PAGE",
         "file": "packages/lib/src/pages/board/panel.tsx"},
        {"pattern": "/dash", "method": "PAGE",
         "file": "apps/web/src/pages/dash.tsx"},
    ]
    board = dev(
        "board",
        ["apps/web/src/pages/board/index.tsx",
         "apps/web/src/pages/board/detail.tsx",
         "apps/web/src/pages/board/filters.tsx",
         "packages/lib/src/pages/board/panel.tsx"],
        flows=[flow("view-board-flow",
                    "apps/web/src/pages/board/index.tsx")])
    dash = dev(
        "dash",
        ["apps/web/src/pages/dash.tsx", "apps/web/src/pages/dash-two.tsx"],
        flows=[flow("view-dash-flow", "apps/web/src/pages/dash.tsx")])
    return [board, dash], routes


def _victim_i18n() -> Feature:
    """The plane ``i18n`` shape: a flowful dev wholly inside
    packages/common — a unit whose own ws anchor cannot mint (its base
    key is structural, the ws:packages/i18n instrument-bar analogue).
    Its flows live inside its own package (translation loading), so
    span votes resolve nowhere and the walk decides upstairs."""
    paths = [f"packages/common/src/locales/l{i}.ts" for i in range(6)]
    return dev("i18n", paths,
               flows=[flow("load-translations-flow", paths[0],
                           paths=paths[:3])])


# ── Seg A (a) — the multi-unit-host walk hole ────────────────────────────


def test_off_multi_unit_host_annexes_cross_unit_dev(monkeypatch) -> None:
    """OFF reproduces the plane hole EXACTLY: the host's merged
    evidence spans apps/web + packages/lib ⇒ B22a's unit-unanimity is
    ``None`` ⇒ the host is never foreign ⇒ the packages/i18n dev is
    annexed at root plurality. This test IS the diagnosis."""
    monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
    devs, routes = _plane_fixture()
    board, dash = devs
    victim = _victim_i18n()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert board.product_feature_id is not None
    assert victim.product_feature_id == board.product_feature_id, (
        "the OFF world must reproduce the pre-B58 annexation (the "
        "multi-unit host escapes B22a) — got {!r}".format(
            victim.product_feature_id))
    assert "annex_guard_span_blocked" not in tele
    assert "annex_guard_entry_blocked" not in tele
    assert "mint_bar_dev_artifact_unit" not in tele


def test_on_multi_unit_host_fenced_dev_lanes(monkeypatch) -> None:
    """ON closes the hole: the host's CANONICAL unit (route surface in
    apps/web) is foreign to the packages/i18n dev; every walk target is
    foreign ⇒ the dev lanes with its own unit (cross_unit_isolation) —
    the 125K i18n mass never lands on the Issue analogue."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    devs, routes = _plane_fixture()
    board, dash = devs
    victim = _victim_i18n()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert board.product_feature_id is not None
    assert victim.product_feature_id is None, (
        "B58 Seg A REGRESSION: the multi-unit host annexed the "
        "unit-coherent cross-unit dev — landed on {!r}".format(
            victim.product_feature_id))
    assert victim.shared_reason == _SHARED_REASON_CROSS_UNIT
    assert tele.get("fold_walk_crossunit_laned", 0) == 1
    assert tele.get("law_flowful_in_lane", 0) == 0
    lane = build_platform_infrastructure_lane(all_devs)
    assert "i18n" in [r["name"] for r in lane]


# ── Seg A (b) — the entry rung fence (cal.com apps/api/v2 shape) ─────────


def _victim_api_v2() -> Feature:
    """The cal.com ``api-v2`` shape: a flowful dev wholly inside
    packages/common whose flow ENTERS through the host's page in
    apps/web — the un-guarded entry rung annexes it wholesale."""
    paths = [f"packages/common/src/api/e{i}.ts" for i in range(4)]
    return dev("api-v2", paths,
               flows=[flow("serve-api-flow", "apps/web/src/pages/dash.tsx",
                           paths=["apps/web/src/pages/dash.tsx"])])


def test_off_entry_rung_annexes_cross_unit_dev(monkeypatch) -> None:
    monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
    devs, routes = _plane_fixture()
    board, dash = devs
    victim = _victim_api_v2()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert victim.product_feature_id == dash.product_feature_id, (
        "OFF must reproduce the cal.com entry-rung annexation")
    assert (victim.anchor_id or "").startswith("fold:entry->")


def test_on_entry_rung_fenced(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    devs, routes = _plane_fixture()
    board, dash = devs
    victim = _victim_api_v2()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert victim.product_feature_id != dash.product_feature_id, (
        "B58 Seg A REGRESSION: entry rung still annexes across units")
    assert tele.get("annex_guard_entry_blocked", 0) >= 1
    # the dev lanes (its own unit mints nothing; every target foreign)
    assert victim.product_feature_id is None
    assert victim.shared_reason == _SHARED_REASON_CROSS_UNIT


# ── Seg A (c) — SACRED anti-cases: same-unit mass NEVER touched ──────────


def test_same_unit_dense_module_untouched_on_and_off(monkeypatch) -> None:
    """The twenty object-record / Soc0 network-security law: a flowful
    dev whose own-subtree mass lives in the SAME unit as its host binds
    identically ON and OFF — the guard keys on FOREIGN cross-unit mass
    only, never on density."""
    def _run(on: bool):
        if on:
            monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
        else:
            monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
        devs, routes = _plane_fixture()
        board, dash = devs
        paths = [f"apps/web/src/records/obj{i}.ts" for i in range(12)]
        dense = dev("object-record", paths,
                    flows=[flow("manage-record-flow", paths[0],
                                paths=paths[:4])])
        all_devs = devs + [dense]
        pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
        return dense, tele

    d_on, tele_on = _run(True)
    d_off, tele_off = _run(False)
    assert d_on.product_feature_id is not None
    assert d_on.product_feature_id == d_off.product_feature_id
    assert d_on.anchor_id == d_off.anchor_id
    assert tele_on.get("annex_guard_entry_blocked", 0) == 0
    assert tele_on.get("annex_guard_span_blocked", 0) == 0


def test_non_coherent_dev_never_fenced(monkeypatch) -> None:
    """A dev whose own files SPAN units (the documenso embed shape) is
    not unit-coherent — B58 never fences it; the B22a rehome law keeps
    ruling it (identical ON and OFF)."""
    def _run(on: bool):
        if on:
            monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
        else:
            monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
        devs, routes = _plane_fixture()
        paths = ["apps/web/src/embed/frame.ts",
                 "packages/lib/src/embed-kit.ts"]
        embed = dev("embed", paths,
                    flows=[flow("embed-flow", paths[0], paths=list(paths))])
        all_devs = devs + [embed]
        pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
        return embed

    e_on = _run(True)
    e_off = _run(False)
    assert e_on.product_feature_id == e_off.product_feature_id
    assert e_on.anchor_id == e_off.anchor_id


# ── Seg B — dev-artifact-unit mint bar (novu playground shape) ───────────


_PLAYGROUND_WORKSPACES = [ws("apps/web"), ws("apps/dashboard"),
                          ws("playground/nextjs")]


def _playground_fixture() -> tuple[list[Feature], list[dict]]:
    routes = [
        {"pattern": "/notifications", "method": "PAGE",
         "file": "playground/nextjs/src/pages/notifications.tsx"},
        {"pattern": "/workflows", "method": "PAGE",
         "file": "apps/dashboard/src/pages/workflows.tsx"},
    ]
    notif = dev(
        "notifications",
        ["playground/nextjs/src/pages/notifications.tsx",
         "playground/nextjs/src/pages/notif-detail.tsx"],
        flows=[flow("view-notifications-flow",
                    "playground/nextjs/src/pages/notifications.tsx")])
    workflows = dev(
        "workflows",
        ["apps/dashboard/src/pages/workflows.tsx"],
        flows=[flow("manage-workflows-flow",
                    "apps/dashboard/src/pages/workflows.tsx")])
    return [notif, workflows], routes


def test_off_playground_anchor_mints(monkeypatch) -> None:
    """OFF reproduces the novu hole: the playground example app mints a
    product PF."""
    monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
    devs, routes = _playground_fixture()
    notif, workflows = devs
    pfs, tele = run_anchored_mint(devs, routes,
                                  ctx_of(_PLAYGROUND_WORKSPACES))
    assert notif.product_feature_id is not None
    assert notif.product_feature_id != workflows.product_feature_id
    assert "mint_bar_dev_artifact_unit" not in tele


def test_on_playground_anchor_never_mints(monkeypatch) -> None:
    """ON: the playground-unit anchor is barred (dev_artifact_unit);
    the example-app dev lanes with its unit instead of minting a fake
    product tile — the novu Notifications 52% exhibit dissolves."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    devs, routes = _playground_fixture()
    notif, workflows = devs
    pfs, tele = run_anchored_mint(devs, routes,
                                  ctx_of(_PLAYGROUND_WORKSPACES))
    assert workflows.product_feature_id is not None, (
        "the REAL product surface must keep minting")
    assert tele.get("mint_bar_dev_artifact_unit", 0) >= 1
    assert (notif.product_feature_id is None
            or notif.product_feature_id == workflows.product_feature_id), (
        "B58 Seg B REGRESSION: the playground anchor still minted its "
        "own PF — got {!r}".format(notif.product_feature_id))
    for pf in pfs:
        assert "playground" not in (pf.anchor_id or ""), (
            "a playground-unit anchor minted: {!r}".format(pf.anchor_id))


def test_on_contaminated_playground_anchor_still_barred(monkeypatch) -> None:
    """Seg B v2 regression (novu kill-switch census, 2026-07-13): the
    ``notifications`` playground anchor escaped the v1 all-or-nothing
    evidence rule because a same-KEY merge unioned real product
    evidence into it (libs/notifications) — the surviving tile then
    absorbed all 16 barred playground siblings (611→17,631 LOC). The
    anchor's CANONICAL id-path is playground — merged evidence must
    never rescue it."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    routes = [
        # dir-style playground page → path-bearing route anchor
        {"pattern": "/notifications", "method": "PAGE",
         "file": "playground/nextjs/src/pages/notifications/index.tsx"},
        {"pattern": "/notifications", "method": "PAGE",
         "file": "playground/nextjs/src/pages/notifications/detail.tsx"},
        # real product surface elsewhere (keeps minting)
        {"pattern": "/workflows", "method": "PAGE",
         "file": "apps/dashboard/src/pages/workflows.tsx"},
    ]
    notif = dev(
        "notifications",
        ["playground/nextjs/src/pages/notifications/index.tsx",
         "playground/nextjs/src/pages/notifications/detail.tsx",
         # CONTAMINATION: same dev also owns real notification lib files
         # (the same-key merge analogue — evidence is mixed)
         "libs/notifications/src/store.ts",
         "libs/notifications/src/badge.ts"],
        flows=[flow("view-notifications-flow",
                    "playground/nextjs/src/pages/notifications/index.tsx")])
    workflows = dev(
        "workflows",
        ["apps/dashboard/src/pages/workflows.tsx"],
        flows=[flow("manage-workflows-flow",
                    "apps/dashboard/src/pages/workflows.tsx")])
    wss = _PLAYGROUND_WORKSPACES + [ws("libs/notifications")]
    pfs, tele = run_anchored_mint([notif, workflows], routes, ctx_of(wss))
    assert workflows.product_feature_id is not None
    for pf in pfs:
        assert "playground" not in (pf.anchor_id or ""), (
            "Seg B v2 REGRESSION: contaminated playground anchor "
            "minted: {!r}".format(pf.anchor_id))


def test_on_product_page_named_playground_still_mints(monkeypatch) -> None:
    """The cal.com counterfactual (Lane-C 'measured out' verdict): a
    REAL product admin page named ``playground`` inside apps/web is a
    route LEAF — unit-root grain cannot fire; the page keeps minting."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    routes = [
        {"pattern": "/settings/admin/playground", "method": "PAGE",
         "file": "apps/web/src/pages/settings/admin/playground.tsx"},
    ]
    page = dev(
        "playground",
        ["apps/web/src/pages/settings/admin/playground.tsx",
         "apps/web/src/pages/settings/admin/playground-run.tsx"],
        flows=[flow("run-playground-flow",
                    "apps/web/src/pages/settings/admin/playground.tsx")])
    pfs, tele = run_anchored_mint([page], routes, ctx_of(_WORKSPACES))
    assert page.product_feature_id is not None, (
        "B58 Seg B REGRESSION: a product page merely NAMED playground "
        "was barred — the Lane-C carve-out is broken")
    assert tele.get("mint_bar_dev_artifact_unit", 0) == 0


def test_no_workspace_units_top_level_artifact_dir_barred(
        monkeypatch) -> None:
    """A non-workspace repo with a TOP-LEVEL examples/ dir: the
    top-level-dir grain fires (examples app ≠ product); a src/ page
    named examples-gallery does not."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    routes = [
        {"pattern": "/gallery", "method": "PAGE",
         "file": "examples/webapp/pages/gallery.tsx"},
        {"pattern": "/home", "method": "PAGE",
         "file": "src/pages/home.tsx"},
    ]
    gallery = dev(
        "gallery",
        ["examples/webapp/pages/gallery.tsx"],
        flows=[flow("view-gallery-flow",
                    "examples/webapp/pages/gallery.tsx")])
    home = dev(
        "home", ["src/pages/home.tsx"],
        flows=[flow("view-home-flow", "src/pages/home.tsx")])
    pfs, tele = run_anchored_mint([gallery, home], routes, ctx_of(None))
    assert home.product_feature_id is not None
    assert tele.get("mint_bar_dev_artifact_unit", 0) >= 1
    for pf in pfs:
        assert not (pf.anchor_id or "").startswith("route:examples"), (
            "top-level examples/ anchor minted: {!r}".format(pf.anchor_id))


# ── Seg B v3 — journey-layer dissolution (warden RESURRECTION fix) ───────


def test_artifact_laned_dev_gets_distinct_reason(monkeypatch) -> None:
    """v3: a playground dev laned by the walk guard carries
    ``dev_artifact_unit`` (not the generic B22a reason) — the UF
    rollup's seeding guard keys on it."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    devs, routes = _playground_fixture()
    notif, workflows = devs
    pfs, tele = run_anchored_mint(devs, routes,
                                  ctx_of(_PLAYGROUND_WORKSPACES))
    assert notif.product_feature_id is None
    assert notif.shared_reason == "dev_artifact_unit", (
        "v3 REGRESSION: artifact-homed laned dev carries {!r}".format(
            notif.shared_reason))


def test_generic_cross_unit_dev_keeps_legacy_reason(monkeypatch) -> None:
    """v3 anti-case: a NON-artifact cross-unit dev (packages/common)
    keeps ``cross_unit_isolation`` — the distinct reason is scoped to
    artifact homes only."""
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    devs, routes = _plane_fixture()
    victim = _victim_i18n()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert victim.product_feature_id is None
    assert victim.shared_reason == _SHARED_REASON_CROSS_UNIT


def test_uf_rollup_skips_dev_artifact_flows(monkeypatch) -> None:
    """v3 core: flows owned by a ``dev_artifact_unit``-laned dev never
    seed a journey (the novu agent-toolkit B24-resurrection door);
    flows of real devs still do. Flag OFF: both seed (pre-B58 world)."""
    from faultline.pipeline_v2.stage_6_7_user_flows import cluster_user_flows

    def _scan():
        return {
            "flows": [
                {"name": "view-billing-flow", "primary_feature": "billing",
                 "entry_point_file": "apps/web/src/pages/billing.tsx",
                 "paths": ["apps/web/src/pages/billing.tsx"]},
                {"name": "render-bells-flow", "primary_feature": "bells",
                 "entry_point_file":
                     "playground/nextjs/src/pages/bells.tsx",
                 "paths": ["playground/nextjs/src/pages/bells.tsx"]},
            ],
            "developer_features": [
                {"name": "billing", "product_feature_id": "billing",
                 "shared_reason": None},
                {"name": "bells", "product_feature_id": None,
                 "shared_reason": "dev_artifact_unit"},
            ],
        }

    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    on = cluster_user_flows(_scan())
    on_names = [u["name"] for u in on["user_flows"]]
    assert on["uf_filtered_dev_artifact"] == 1, on
    assert not any("bell" in n.lower() for n in on_names), on_names
    assert any("billing" in n.lower() for n in on_names), on_names

    monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
    off = cluster_user_flows(_scan())
    assert off["uf_filtered_dev_artifact"] == 0
    assert len(off["user_flows"]) >= len(on["user_flows"])


def test_route_group_recall_skips_artifact_toplevel(monkeypatch) -> None:
    """v3: playground/* route rows are not PRODUCT route groups (the
    novu I24 hole); apps/web/**/playground pages keep counting."""
    from faultline.pipeline_v2.route_group_recall import (
        seed_route_group_journeys,
    )
    routes = [
        {"file": "playground/nextjs/src/pages/api/a.ts", "pattern": "/a"},
        {"file": "playground/nextjs/src/pages/api/b.ts", "pattern": "/b"},
        {"file": "apps/web/src/pages/settings/admin/playground/run.tsx",
         "pattern": "/settings/admin/playground/run"},
        {"file": "apps/web/src/pages/settings/admin/playground/edit.tsx",
         "pattern": "/settings/admin/playground/edit"},
    ]
    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    tele_on = seed_route_group_journeys([], [], [], [], list(routes))
    assert tele_on.get("skipped_dev_artifact", 0) == 2, tele_on
    assert tele_on["groups"] == 1, tele_on  # only the apps/web group

    monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
    tele_off = seed_route_group_journeys([], [], [], [], list(routes))
    assert tele_off.get("skipped_dev_artifact", 0) == 0
    assert tele_off["groups"] == 2, tele_off


def test_rollup_adapter_carries_lane_reason(monkeypatch) -> None:
    """v4 regression (the v3 no-op): run_user_flow_rollup's dev snapshot
    MUST carry shared_reason — v3's guard read None for every dev and
    novu agent-toolkit kept its 20 playground journeys. Adapter-level
    test with typed objects (the core-only test missed this)."""
    from faultline.pipeline_v2.stage_6_7_user_flows import (
        run_user_flow_rollup,
    )

    def _mk():
        billing_fl = flow("view-billing-flow",
                          "apps/web/src/pages/billing.tsx")
        bells_fl = flow("render-bells-flow",
                        "playground/nextjs/src/pages/bells.tsx")
        billing = dev("billing", ["apps/web/src/pages/billing.tsx"],
                      flows=[billing_fl])
        billing.product_feature_id = "billing"
        bells = dev("bells", ["playground/nextjs/src/pages/bells.tsx"],
                    flows=[bells_fl])
        bells.product_feature_id = None
        bells.shared_reason = "dev_artifact_unit"
        # primary_feature stamps (bipartite store contract)
        billing_fl.primary_feature = "billing"
        bells_fl.primary_feature = "bells"
        return [billing_fl, bells_fl], [billing, bells]

    monkeypatch.setenv("FAULTLINE_ANNEXATION_GUARD", "1")
    flows_on, feats_on = _mk()
    ufs_on, tele_on = run_user_flow_rollup(flows_on, feats_on)
    names_on = [u.name for u in ufs_on]
    assert not any("bell" in n.lower() for n in names_on), names_on
    assert any("billing" in n.lower() for n in names_on), names_on

    monkeypatch.delenv("FAULTLINE_ANNEXATION_GUARD", raising=False)
    flows_off, feats_off = _mk()
    ufs_off, tele_off = run_user_flow_rollup(flows_off, feats_off)
    assert len(ufs_off) >= len(ufs_on)
