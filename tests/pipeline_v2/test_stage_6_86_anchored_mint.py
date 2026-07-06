"""Product-Spine Wave 2b — anchored mint + Call-2 retirement tests.

Operator acceptance fixtures are DISTILLED from the real 2026-07-06
wave2a-out scan JSONs (Soc0 widget-query family / supabase studio
mints / midday i-p-r-s / the hub-amendment vendor grain) — path shapes
and flow-entry layouts mirror the real repos.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    anchored_mint_enabled,
    build_platform_infrastructure_lane,
    enforce_hub_family_parity,
    run_anchored_mint,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        pfid: str | None = "old-pf", **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id=pfid,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ctx_of(workspaces=None, tracked=None, repo_path=".") -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path(repo_path), monorepo=bool(workspaces),
    )


def mint(devs, routes=None, ctx=None, signals=None, nav=frozenset()):
    return run_anchored_mint(
        devs, routes or [], ctx or ctx_of(),
        extractor_signals=signals, nav_keys=nav,
    )


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("FAULTLINE_SPINE_ANCHORED_MINT", raising=False)
    assert anchored_mint_enabled()
    monkeypatch.setenv("FAULTLINE_SPINE_ANCHORED_MINT", "0")
    assert not anchored_mint_enabled()


# ── Core lineage semantics ───────────────────────────────────────────────


def test_route_lineage_mints_pf_and_stamps_anchor_id():
    routes = [{"pattern": "/settings", "method": "PAGE",
               "file": "app/settings/page.tsx"}]
    d = dev("settings", ["app/settings/page.tsx",
                         "app/settings/form.tsx"],
            flows=[flow("edit-settings-flow", "app/settings/page.tsx")])
    pfs, tele = mint([d], routes)
    assert tele["applied"]
    assert [p.name for p in pfs] == ["settings"]
    assert pfs[0].anchor_id == "route:app/settings"
    assert pfs[0].layer == "product"
    assert d.product_feature_id == "settings"
    assert d.anchor_id == "route:app/settings"
    assert d.shared_reason is None


def test_specificity_route_beats_enclosing_workspace():
    ws = [SimpleNamespace(name="web", path="apps/web", stack="ts")]
    routes = [{"pattern": "/billing", "method": "PAGE",
               "file": "apps/web/app/billing/page.tsx"}]
    d = dev("billing", ["apps/web/app/billing/page.tsx"],
            flows=[flow("pay-flow", "apps/web/app/billing/page.tsx")])
    pfs, tele = mint([d], routes, ctx_of(ws))
    assert d.anchor_id == "route:apps/web/app/billing"


def test_cross_app_merged_route_beats_single_app_shell():
    """W2b.1 fix (a), openstatus `login`: a dev whose login surface spans
    TWO apps (6 files in apps/dashboard, 4 in apps/status-page) is won by
    the MERGED route:login anchor (share 1.0), NOT by the ws:apps/dashboard
    shell (share 0.6) — the shell's smaller matched set is not "more
    specific" because its subtree does not nest inside the route subtree."""
    ws = [SimpleNamespace(name="dashboard", path="apps/dashboard", stack="ts"),
          SimpleNamespace(name="status-page", path="apps/status-page", stack="ts")]
    dash = "apps/dashboard/src/app/login"
    sp = "apps/status-page/src/app/(status-page)/[domain]/[locale]/(auth)/login"
    routes = [
        {"pattern": "/login", "method": "PAGE", "file": f"{dash}/page.tsx"},
        {"pattern": "/:domain/:locale/login", "method": "PAGE",
         "file": f"{sp}/page.tsx"},
    ]
    d = dev("login", [
        f"{dash}/page.tsx", f"{dash}/layout.tsx", f"{dash}/search-params.ts",
        f"{dash}/_components/actions.ts", f"{dash}/_components/login-button.tsx",
        f"{dash}/_components/magic-link-form.tsx",
        f"{sp}/page.tsx", f"{sp}/_components/section-magic-link.tsx",
        f"{sp}/_components/section-sso.tsx", f"{sp}/actions.ts",
    ], flows=[flow("sign-in-with-magic-link-flow",
                   f"{dash}/_components/actions.ts")])
    pfs, tele = mint([d], routes, ctx_of(ws))
    assert d.product_feature_id == "login", (
        d.product_feature_id, d.shared_reason)
    assert d.anchor_id == "route:login" or (
        d.anchor_id or "").startswith("route:")
    assert d.shared_reason is None
    rows = build_platform_infrastructure_lane([d])
    assert rows == []


def test_no_shared_platform_pf_ever_minted():
    """Operator amendment (final): the Shared Platform PF is abolished —
    residuals go to the platform_infrastructure lane."""
    d1 = dev("utils", ["lib/utils/dates.ts", "lib/utils/text.ts"])
    d2 = dev("ui-kit", ["lib/ui/button.tsx"])
    pfs, tele = mint([d1, d2])
    assert not any(p.name in ("shared-platform", "platform") for p in pfs)
    assert d1.product_feature_id is None
    assert d1.shared_reason == "no_anchor_lineage"
    rows = build_platform_infrastructure_lane([d1, d2])
    assert {r["name"] for r in rows} == {"utils", "ui-kit"}
    assert all(r["shared_reason"] for r in rows)


def test_facets_are_out_of_scope():
    d = dev("auth", ["app/anything/x.ts"])
    d.role = "facet"
    pfs, tele = mint([d])
    assert tele["devs_in_scope"] == 0
    assert d.product_feature_id == "old-pf"  # untouched


# ── Mint bar ─────────────────────────────────────────────────────────────


def test_api_only_anchor_folds_in_page_repo_but_mints_in_pure_api_repo():
    api_route = {"pattern": "/context-items", "method": "GET",
                 "file": "backend/routers/context_items.py"}
    page_route = {"pattern": "/dash", "method": "PAGE",
                  "file": "frontend/src/pages/dash.tsx"}
    api_dev = dev("api-context-items", ["backend/routers/context_items.py"],
                  flows=[flow("browse-items-flow",
                              "backend/routers/context_items.py")])
    page_dev = dev("dash", ["frontend/src/pages/dash.tsx"],
                   flows=[flow("view-dash-flow",
                               "frontend/src/pages/dash.tsx")])
    # Repo WITH a page surface: the api-only router folds (operator case:
    # the Soc0 api-* family must not exist as product PFs).
    pfs, tele = mint([api_dev, page_dev], [api_route, page_route])
    assert not any(p.name == "context-items" for p in pfs)
    assert api_dev.product_feature_id is None
    assert api_dev.shared_reason == "sub_mint_bar_surface"
    # Pure-API repo: the API surface IS the product → it mints.
    api_dev2 = dev("api-context-items", ["backend/routers/context_items.py"],
                   flows=[flow("browse-items-flow",
                               "backend/routers/context_items.py")])
    pfs2, _ = mint([api_dev2], [api_route])
    assert [p.name for p in pfs2] == ["context-items"]


def test_single_letter_route_folds_via_imports(tmp_path):
    """midday i/p/r/s (operator case 3): the (public)/i share page folds
    under Invoices via its own imports; zero single-letter PFs."""
    repo = tmp_path / "repo"
    inv_dir = repo / "apps/dashboard/src/components"
    i_dir = repo / "apps/dashboard/src/app/[locale]/(public)/i/[token]"
    inv_dir.mkdir(parents=True)
    i_dir.mkdir(parents=True)
    (inv_dir / "invoice-view.tsx").write_text("export const V = 1;\n")
    (i_dir / "page.tsx").write_text(
        "import { V } from '../../../../../components/invoice-view';\n"
        "export default function P() { return V; }\n"
    )
    inv_page = repo / "apps/dashboard/src/app/[locale]/(app)/invoices"
    inv_page.mkdir(parents=True)
    (inv_page / "page.tsx").write_text("export default () => null;\n")

    routes = [
        {"pattern": "/i/:token", "method": "PAGE",
         "file": "apps/dashboard/src/app/[locale]/(public)/i/[token]/page.tsx"},
        {"pattern": "/invoices", "method": "PAGE",
         "file": "apps/dashboard/src/app/[locale]/(app)/invoices/page.tsx"},
    ]
    invoices = dev(
        "invoices",
        ["apps/dashboard/src/app/[locale]/(app)/invoices/page.tsx",
         "apps/dashboard/src/components/invoice-view.tsx"],
        flows=[flow("manage-invoices-flow",
                    "apps/dashboard/src/app/[locale]/(app)/invoices/page.tsx")])
    i_dev = dev(
        "i",
        ["apps/dashboard/src/app/[locale]/(public)/i/[token]/page.tsx"],
        flows=[flow("view-shared-invoice-flow",
                    "apps/dashboard/src/app/[locale]/(public)/i/[token]/page.tsx")])
    tracked = [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file()]
    pfs, tele = mint([invoices, i_dev], routes,
                     ctx_of(tracked=tracked, repo_path=str(repo)))
    names = [p.name for p in pfs]
    assert "i" not in names and not any(len(n) == 1 for n in names)
    assert i_dev.product_feature_id == "invoices"
    assert i_dev.anchor_id and i_dev.anchor_id.startswith("fold:import->")
    assert tele["fold_import"] == 1


def test_supabase_studio_micro_route_mints_die():
    """Operator case 2: get-utc-time / parse-query — api micro-routes in
    a page-bearing repo never mint; they fold or lane with a reason."""
    routes = [
        {"pattern": "/api/get-utc-time", "method": "GET",
         "file": "apps/studio/pages/api/get-utc-time.ts"},
        {"pattern": "/api/parse-query", "method": "POST",
         "file": "apps/studio/pages/api/parse-query.ts"},
        {"pattern": "/project/:ref/auth", "method": "PAGE",
         "file": "apps/studio/pages/project/[ref]/auth/users.tsx"},
    ]
    ws = [SimpleNamespace(name="studio", path="apps/studio", stack="ts")]
    gut = dev("get-utc-time", ["apps/studio/pages/api/get-utc-time.ts"],
              flows=[flow("get-utc-time-flow",
                          "apps/studio/pages/api/get-utc-time.ts")])
    pq = dev("parse-query", ["apps/studio/pages/api/parse-query.ts"])
    auth = dev("auth-users", ["apps/studio/pages/project/[ref]/auth/users.tsx"],
               flows=[flow("browse-users-flow",
                           "apps/studio/pages/project/[ref]/auth/users.tsx")])
    pfs, tele = mint([gut, pq, auth], routes, ctx_of(ws))
    names = {p.name for p in pfs}
    assert "get-utc-time" not in names
    assert "parse-query" not in names
    assert gut.product_feature_id is None and gut.shared_reason
    assert pq.product_feature_id is None and pq.shared_reason
    # the real capability (route-deep under project/[ref]) survives
    assert auth.product_feature_id is not None


def test_widget_query_family_never_mints_service_pfs():
    """Operator case 1 (Soc0): the widget-query service dir + api-* router
    siblings — zero service-dir-named PFs whose members are that dir."""
    routes = [
        {"pattern": "/context-items", "method": "GET",
         "file": "backend/routers/context_items.py"},
        {"pattern": "/trial", "method": "GET",
         "file": "backend/routers/trial.py"},
        {"pattern": "/detections", "method": "PAGE",
         "file": "frontend/src/pages/DetectionsPage.tsx"},
    ]
    wq = dev("widget-query", [
        "backend/services/widget_query/__init__.py",
        "backend/services/widget_query/base.py",
        "backend/services/widget_query/elasticsearch_adapter.py",
        "backend/services/widget_query/zscaler_adapter.py",
        "backend/services/widget_query/entra_adapter.py",
    ], flows=[flow("query-data-flow",
                   "backend/services/widget_query/__init__.py")])
    iot = dev("iot-ot", ["backend/services/iot_ot/base.py",
                         "backend/services/iot_ot/claroty.py"])
    ctx_items = dev("api-context-items", ["backend/routers/context_items.py"],
                    flows=[flow("browse-flow",
                                "backend/routers/context_items.py")])
    trial = dev("api-trial-status", ["backend/routers/trial.py"])
    det = dev("detections", ["frontend/src/pages/DetectionsPage.tsx"],
              flows=[flow("browse-detections-flow",
                          "frontend/src/pages/DetectionsPage.tsx")])
    pfs, tele = mint([wq, iot, ctx_items, trial, det], routes)
    names = {p.name for p in pfs}
    for banned in ("widget-query", "iot-ot", "context-items",
                   "api-context-items", "trial", "api-trial-status"):
        assert banned not in names, banned
    assert wq.product_feature_id is None and wq.shared_reason
    assert "detections" in names  # the real page capability minted


# ── Hub amendment unit cases ─────────────────────────────────────────────


def _edr_fixture():
    plumbing = dev("edr", [
        "backend/services/edr/__init__.py",
        "backend/services/edr/base.py",
        "backend/services/edr/factory.py",
        "backend/services/edr/normalizer.py",
        "backend/services/edr/query_rewrite.py",
    ], flows=[
        flow("query-edr-alerts-flow", "backend/services/edr/base.py"),
        flow("build-cortex-filters-flow", "backend/services/edr/cortex.py"),
        flow("build-crowdstrike-filters-flow",
             "backend/services/edr/crowdstrike.py"),
        flow("build-defender-filters-flow",
             "backend/services/edr/defender.py"),
        flow("parse-alert-identifiers-flow",
             "backend/services/edr/claroty_xdome.py"),
    ])
    kids = [
        dev("edr-claroty", ["backend/services/edr/claroty_xdome.py"]),
        dev("edr-cortex", ["backend/services/edr/cortex.py",
                           "backend/services/edr/schema/cortex_baseline.py"]),
        dev("edr-crowdstrike", ["backend/services/edr/crowdstrike.py"]),
        dev("edr-defender", ["backend/services/edr/defender.py"]),
        dev("edr-sentinelone", ["backend/services/edr/sentinelone.py",
                                "backend/services/edr/schema/sentinelone_baseline.py"]),
    ]
    return plumbing, kids


def test_hub_amendment_edr_five_vendor_pfs_plus_core():
    """Amendment case 5a: Soc0 edr → 5 vendor PFs + '<hub> Core'; the
    parent's per-vendor flows count as child evidence (entry files);
    sentinelone mints on own-code even with zero flows."""
    plumbing, kids = _edr_fixture()
    pfs, tele = mint([plumbing, *kids])
    by_name = {p.name: p for p in pfs}
    for v in ("claroty", "cortex", "crowdstrike", "defender", "sentinelone"):
        assert v in by_name, f"vendor PF {v} missing"
        assert by_name[v].anchor_id == f"hub:backend/services/edr/{v}"
    assert "edr-core" in by_name
    assert plumbing.product_feature_id == "edr-core"
    for k in kids:
        assert k.product_feature_id in {p.name for p in pfs}
        assert k.shared_reason is None  # sibling parity: never shared
    fams = tele["hub_families"]
    assert fams and fams[0]["hub_dir"] == "backend/services/edr"
    assert fams[0]["core"] == "edr-core"


def test_hub_amendment_stub_children_stay_under_parent():
    """Amendment case 5d (supabase FDW class): vendor-named STATIC files
    with no flows are stubs — 0 new PFs; the owning dev keeps its
    ordinary lineage under the hosting surface."""
    routes = [
        {"pattern": "/project/:ref/integrations", "method": "PAGE",
         "file": "apps/studio/pages/project/[ref]/integrations/index.tsx"},
        {"pattern": "/project/:ref/integrations/wrappers", "method": "PAGE",
         "file": "apps/studio/pages/project/[ref]/integrations/wrappers.tsx"},
    ]
    integrations = dev("integrations", [
        "apps/studio/pages/project/[ref]/integrations/index.tsx",
    ], flows=[flow("browse-integrations-flow",
                   "apps/studio/pages/project/[ref]/integrations/index.tsx")])
    wrappers = dev("wrappers", [
        "apps/studio/pages/project/[ref]/integrations/wrappers.tsx",
        "apps/studio/components/interfaces/Integrations/Wrappers/airtable.sql",
        "apps/studio/components/interfaces/Integrations/Wrappers/auth0.sql",
        "apps/studio/components/interfaces/Integrations/Wrappers/stripe.sql",
    ], flows=[flow("manage-wrappers-flow",
                   "apps/studio/pages/project/[ref]/integrations/wrappers.tsx")])
    pfs, tele = mint([integrations, wrappers], routes)
    names = {p.name for p in pfs}
    assert not any(n in names for n in ("airtable", "auth0", "stripe"))
    # the stub-holding dev folds UNDER the hosting integrations surface
    # via its flow entry (the amendment's "stay under the parent" rule).
    assert wrappers.product_feature_id == "integrations"
    assert wrappers.anchor_id and wrappers.anchor_id.startswith("fold:entry")


def test_cross_family_same_vendor_stays_separate_with_unique_display():
    """Soc0 claroty under BOTH edr and iot_ot: two separate PFs (different
    capability families) with UNIQUE slugs AND displays — a shared display
    would silently merge them in the 6.7d rebuild (dev_map keys by
    display)."""
    plumbing, kids = _edr_fixture()
    iot = [
        dev("iot-ot", ["backend/services/iot_ot/base.py",
                       "backend/services/iot_ot/factory.py"],
            flows=[flow("poll-iot-flow", "backend/services/iot_ot/claroty.py")]),
        dev("iot-claroty", ["backend/services/iot_ot/claroty.py"]),
        dev("iot-zscaler", ["backend/services/iot_ot/zscaler.py"]),
        dev("iot-defender", ["backend/services/iot_ot/defender.py"]),
    ]
    pfs, tele = mint([plumbing, *kids, *iot])
    claroty_pfs = [p for p in pfs if (p.name or "").startswith("claroty")]
    assert len(claroty_pfs) == 2, [p.name for p in pfs]
    names = {p.name for p in claroty_pfs}
    displays = {p.display_name for p in claroty_pfs}
    assert len(names) == 2 and len(displays) == 2, (names, displays)


def test_hub_family_parity_restamps_moved_children():
    plumbing, kids = _edr_fixture()
    pfs, tele = mint([plumbing, *kids])
    stamps = tele["hub_family_stamps"]
    # simulate a later ladder scattering a child into another PF
    kids[1].product_feature_id = "somewhere-else"
    par = enforce_hub_family_parity([plumbing, *kids], pfs, stamps)
    assert par["restamped"] == 1
    assert kids[1].product_feature_id == stamps[kids[1].name]


# ── Shared-consumer pass (amendment §2) ──────────────────────────────────


def test_infra_files_surface_as_shared_members_on_consumers(tmp_path):
    repo = tmp_path / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "app" / "billing").mkdir(parents=True)
    (repo / "lib" / "money.ts").write_text("export const fmt = 1;\n")
    (repo / "app" / "billing" / "page.tsx").write_text(
        "import { fmt } from '../../lib/money';\nexport default fmt;\n")
    routes = [{"pattern": "/billing", "method": "PAGE",
               "file": "app/billing/page.tsx"}]
    billing = dev("billing", ["app/billing/page.tsx"],
                  flows=[flow("pay-flow", "app/billing/page.tsx")])
    money = dev("money-utils", ["lib/money.ts"])
    tracked = [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file()]
    pfs, tele = mint([billing, money], routes,
                     ctx_of(tracked=tracked, repo_path=str(repo)))
    assert money.product_feature_id is None
    shared = [m for m in billing.member_files
              if m.role == "shared" and m.path == "lib/money.ts"]
    assert shared, "consumer did not gain the role=shared member"
    assert tele["shared_consumers"]["edges"] == 1


# ── 6.7d anchored mode: Call-2 retired, Call-1 constrained ───────────────


class _OneDrawClient:
    """Counts calls; returns a fixed Call-1 JSON (never a Call-2 map)."""

    def __init__(self, payload: str) -> None:
        self.calls = 0
        self._payload = payload
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(text=self._payload)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


def test_67d_anchored_retires_call2_and_drops_invented_pfs(monkeypatch):
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        run_journey_abstraction,
    )

    routes = [{"pattern": "/settings", "method": "PAGE",
               "file": "app/settings/page.tsx"}]
    d = dev("settings", ["app/settings/page.tsx"],
            flows=[flow("edit-settings-flow", "app/settings/page.tsx")])
    lane = dev("utils", ["lib/utils/x.ts"], pfid=None)
    lane.shared_reason = "no_anchor_lineage"
    pfs, _tele = mint([d, lane], routes)
    d_uf = UserFlow(id="UF-001", name="Manage settings", resource="setting",
                    domain="settings", product_feature_id="settings",
                    intent="manage",
                    member_flow_ids=[d.flows[0].name], member_count=1)
    payload = (
        '{"product_features":[{"name":"Settings","description":"tune"},'
        '{"name":"Invented Capability","description":"nope"}],'
        '"user_flows":[{"name":"Manage settings","resource":"setting",'
        '"product_feature":"Settings","from_flows":["UF-001"],'
        '"from_dev_features":["settings"]}]}'
    )
    cli = _OneDrawClient(payload)
    ufs2, pfs2, dev_map, tele = run_journey_abstraction(
        [d_uf], pfs, [d, lane], routes,
        client=cli, model="m", anchored=True,
    )
    assert tele["applied"], tele.get("fallback")
    assert cli.calls == 1, "Call-2 must NOT fire in anchored mode"
    assert tele["anchored"] is True
    names = {p.display_name for p in pfs2}
    assert "Invented Capability" not in names
    assert "Settings" in names
    assert tele["anchored_pf_invented_dropped"] == 1
    # lane resident stays off the product map
    assert dev_map is not None and "utils" not in dict(dev_map)
    assert not any(p.name in ("shared-platform", "platform") for p in pfs2)
    # the anchored PF kept its lineage id through the rebuild
    settings_pf = next(p for p in pfs2 if p.name == "settings")
    assert settings_pf.anchor_id == "route:app/settings"


def test_67d_anchored_entry_owner_override_telemetry():
    """Hub grain end-to-end: the parent-held per-vendor flow's journey
    follows the vendor PF (entry-owner override feeds the ladders)."""
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        run_journey_abstraction,
    )

    plumbing, kids = _edr_fixture()
    pfs, _t = mint([plumbing, *kids])
    uf = UserFlow(id="UF-001", name="Build cortex filters",
                  resource="filter", domain="edr",
                  product_feature_id="edr-core", intent="author",
                  member_flow_ids=["build-cortex-filters-flow"],
                  member_count=1)
    payload = (
        '{"product_features":[{"name":"Cortex","description":"x"}],'
        '"user_flows":[{"name":"Build cortex filters","resource":"filter",'
        '"product_feature":"Cortex","from_flows":["UF-001"],'
        '"from_dev_features":["edr-cortex"]}]}'
    )
    cli = _OneDrawClient(payload)
    ufs2, pfs2, dev_map, tele = run_journey_abstraction(
        [uf], pfs, [plumbing, *kids], [],
        client=cli, model="m", anchored=True,
    )
    assert tele["applied"]
    assert tele.get("entry_owner_overrides", 0) >= 1
    cortex_ufs = [u for u in ufs2 if u.product_feature_id == "cortex"]
    assert cortex_ufs, [
        (u.name, u.product_feature_id) for u in ufs2]
