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


def test_go_dsl_routes_mint_on_pageless_repo_and_fold_with_page_surface():
    """S4b it2 — the Go arm of the pageless-mint law (traefik/ollama shape).

    DSL-routed Go registrations (gorilla ``.Path("/api/...")``) enter
    ``routes_index`` as non-PAGE rows. On a PAGELESS Go server the API IS
    the product → the route anchor mints (ollama VERIFIED: PF 0→2 armed).
    The moment ANY page surface exists (SPA webui shell), the same
    api-only anchor is barred ``api_only_surface`` and the flowful api dev
    FOLDS into the page capability (traefik VERIFIED: route:debug /
    route:version barred; /api family reached PF only through the
    page+api certificates union). This is the PAGES-REPO ANTI-CASE: the
    bar STANDS — S4b changed route *delivery*, never the mint bars."""
    go_routes = [
        {"pattern": "/api/http/routers", "method": "GET",
         "file": "pkg/api/handler.go"},
        {"pattern": "/api/overview", "method": "GET",
         "file": "pkg/api/handler.go"},
        {"pattern": "/api/rawdata", "method": "GET", "file": "pkg/api/rest.go"},
    ]
    d = dev("api-http-routers", ["pkg/api/handler.go", "pkg/api/rest.go"],
            flows=[flow("inspect-routers-flow", "pkg/api/handler.go")])
    pfs, tele = mint([d], go_routes)
    assert tele["repo_has_pages"] is False
    assert [p.name for p in pfs] == ["http"]
    assert d.product_feature_id == "http"
    assert d.shared_reason is None

    # Pages-repo anti-case: same Go api surface + one SPA page row.
    page_route = {"pattern": "/dashboard", "method": "PAGE",
                  "file": "webui/src/App.tsx"}
    page_dev = dev("dashboard", ["webui/src/App.tsx"],
                   flows=[flow("view-dashboard-flow", "webui/src/App.tsx")])
    d2 = dev("api-http-routers", ["pkg/api/handler.go", "pkg/api/rest.go"],
             flows=[flow("inspect-routers-flow", "pkg/api/handler.go")])
    pfs2, tele2 = mint([d2, page_dev], go_routes + [page_route])
    assert tele2["repo_has_pages"] is True
    assert [p.name for p in pfs2] == ["dashboard"]
    assert d2.product_feature_id == "dashboard"  # folds, no standalone mint
    assert {(b["anchor"], b["bar"]) for b in tele2["bar_decisions"]} >= {
        ("route:http", "api_only_surface"),
    }


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
    # the Soc0 api-* family must not exist as product PFs). W2b.1 law:
    # the dev is FLOWFUL, so it never lanes — it binds to the plurality
    # real capability with a provenance note.
    pfs, tele = mint([api_dev, page_dev], [api_route, page_route])
    assert not any(p.name == "context-items" for p in pfs)
    assert api_dev.product_feature_id == "dash"
    assert (api_dev.anchor_id or "").startswith(("fold:span", "fold:walk"))
    assert api_dev.shared_reason is None
    assert build_platform_infrastructure_lane([api_dev, page_dev]) == []
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
    # W2b.1 law: gut is FLOWFUL → binds to the real studio capability
    # (never the lane); pq is flowless → honest lane resident.
    assert gut.product_feature_id == auth.product_feature_id
    assert (gut.anchor_id or "").startswith(("fold:", "mint:"))
    assert gut.shared_reason is None
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
    # W2b.1 law: wq + ctx_items are FLOWFUL → bound to a real capability
    # with a fold note (never the lane); flowless iot/trial stay honest
    # lane residents.
    assert wq.product_feature_id in names and wq.shared_reason is None
    assert (wq.anchor_id or "").startswith(("fold:", "mint:"))
    assert ctx_items.product_feature_id in names
    assert iot.product_feature_id is None and iot.shared_reason
    assert trial.product_feature_id is None and trial.shared_reason
    assert "detections" in names  # the real page capability minted


# ── W2b.1 LAW: a flowful dev never lands in the lane ────────────────────


def test_law_flowful_dev_never_in_lane():
    """CONSTRUCTION INVARIANT (W2b.1 fix a, operator law): whenever ≥1
    capability mints, every dev with ≥1 flow terminates in a PF — via
    span-vote or ancestor-walk with a provenance note — and the
    platform_infrastructure lane holds ONLY flowless devs."""
    routes = [{"pattern": "/monitors", "method": "PAGE",
               "file": "apps/dashboard/src/app/(dashboard)/monitors/page.tsx"}]
    monitors = dev(
        "monitors",
        ["apps/dashboard/src/app/(dashboard)/monitors/page.tsx",
         "apps/dashboard/src/app/(dashboard)/monitors/list.tsx"],
        flows=[flow("browse-monitors-flow",
                    "apps/dashboard/src/app/(dashboard)/monitors/page.tsx")])
    # flowful dev with NO lineage, no route entries, no imports — the
    # ancestor-walk terminal rung must still bind it.
    trpc = dev("trpc", ["apps/dashboard/src/app/api/trpc/edge/route.ts"],
               flows=[flow("call-trpc-flow",
                           "apps/dashboard/src/app/api/trpc/edge/route.ts")])
    # flowless plumbing — the honest lane resident.
    plumbing = dev("importers", ["packages/importers/csv.ts"])
    pfs, tele = mint([monitors, trpc, plumbing], routes)
    assert {p.name for p in pfs} == {"monitors"}
    assert trpc.product_feature_id == "monitors"
    assert (trpc.anchor_id or "").startswith(("fold:span", "fold:walk"))
    assert trpc.shared_reason is None
    assert plumbing.product_feature_id is None
    rows = build_platform_infrastructure_lane([monitors, trpc, plumbing])
    assert [r["name"] for r in rows] == ["importers"]
    assert all(r["flows"] == 0 for r in rows)
    assert tele.get("law_flowful_in_lane", 0) == 0


def test_law_degenerate_scan_keeps_honest_lane():
    """Zero mintable anchors → there is no capability to bind to; the
    lane stays honest even for flowful devs (documented degenerate
    case — the law binds only when ≥1 PF exists)."""
    d = dev("worker", ["scripts/worker.ts"],
            flows=[flow("run-worker-flow", "scripts/worker.ts")])
    pfs, tele = mint([d])
    assert pfs == []
    assert d.product_feature_id is None
    assert tele.get("law_flowful_in_lane", 0) == 1


def test_entry_route_mint_on_demand_rescues_flowful_page_dev():
    """W2b.1 law rung L1: a flowful dev whose lineage was starved (owned
    set diluted below θ across two apps) still mints its PAGE route
    anchor on demand — the openstatus `llms/markdown` page-class rescue.
    An API-ONLY entry surface never mints this way (page-surface rule)."""
    ws = [SimpleNamespace(name="web", path="apps/web", stack="ts")]
    routes = [
        {"pattern": "/status", "method": "PAGE",
         "file": "apps/web/src/app/(landing)/status/page.tsx"},
        {"pattern": "/api/search", "method": "GET",
         "file": "apps/web/src/app/api/search/route.ts"},
        {"pattern": "/pricing", "method": "PAGE",
         "file": "apps/web/src/app/(landing)/pricing/page.tsx"},
    ]
    # dilution: 2/5 files inside the status route dir (< θ), the rest
    # spread over lib — the ws shell wins lineage but cannot mint.
    status = dev("status", [
        "apps/web/src/app/(landing)/status/page.tsx",
        "apps/web/src/app/(landing)/status/status-widget.tsx",
        "apps/web/src/lib/status/a.ts",
        "apps/web/src/lib/status/b.ts",
        "apps/web/src/lib/status/c.ts",
    ], flows=[flow("view-status-flow",
                   "apps/web/src/app/(landing)/status/page.tsx")])
    pricing = dev("pricing",
                  ["apps/web/src/app/(landing)/pricing/page.tsx"],
                  flows=[flow("view-pricing-flow",
                              "apps/web/src/app/(landing)/pricing/page.tsx")])
    search = dev("search", ["apps/web/src/app/api/search/route.ts"],
                 flows=[flow("search-flow",
                             "apps/web/src/app/api/search/route.ts")])
    pfs, tele = mint([status, pricing, search], routes, ctx_of(ws))
    names = {p.name for p in pfs}
    assert "status" in names, names          # minted ON DEMAND (rung L1)
    assert status.product_feature_id == "status"
    assert (status.anchor_id or "").startswith("mint:entry-route")
    assert tele.get("mint_entry_route", 0) >= 1
    # the api-only entry dev did NOT mint `search`; law bound it to a
    # real capability instead.
    assert "search" not in names
    assert search.product_feature_id in names
    assert search.shared_reason is None


# ── W2b.1 — reserved legacy slugs never mint bare ────────────────────────


def test_platform_named_anchor_mints_under_qualified_slug():
    """supabase smoke finding: a REAL route anchor named `platform`
    minted PF key `platform` — every legacy _SHARED_PF_KEYS consumer
    (validator old-I9, taxonomy shared-bucket exemptions) then treats it
    as the abolished shared bucket. The display keeps the author's word;
    the slug is qualified out of the reserved namespace."""
    routes = [{"pattern": "/platform", "method": "PAGE",
               "file": "apps/docs/app/platform/page.tsx"}]
    d = dev("platform", ["apps/docs/app/platform/page.tsx",
                         "apps/docs/app/platform/nav.tsx"],
            flows=[flow("browse-platform-flow",
                        "apps/docs/app/platform/page.tsx")])
    pfs, tele = mint([d], routes)
    assert len(pfs) == 1
    assert pfs[0].name not in ("platform", "shared-platform"), pfs[0].name
    assert d.product_feature_id == pfs[0].name


# ── W2b.1 fix (b) — pypkg domains mint (onyx class) ─────────────────────


def test_pypkg_domains_mint_and_thin_packages_lane():
    """onyx class: backend/onyx/<domain> packages give lineage to the
    python-monolith devs (79% no_anchor_lineage before the fix); a
    1-file flowless wrapper package (redis class) fails the thinness
    bar and stays an honest lane resident."""
    tracked = [
        "backend/onyx/__init__.py",
        "backend/onyx/chat/__init__.py",
        "backend/onyx/chat/service.py",
        "backend/onyx/chat/models.py",
        "backend/onyx/connectors/__init__.py",
        "backend/onyx/connectors/registry.py",
        "backend/onyx/connectors/factory.py",
        "backend/onyx/redis/__init__.py",
        "backend/onyx/redis/redis_pool.py",
        "backend/onyx/server/__init__.py",
        "backend/onyx/server/documents/__init__.py",
        "backend/onyx/server/documents/api.py",
        "backend/onyx/server/documents/models.py",
    ]
    chat = dev("chat", ["backend/onyx/chat/service.py",
                        "backend/onyx/chat/models.py"],
               flows=[flow("send-chat-flow", "backend/onyx/chat/service.py")])
    documents = dev("documents", ["backend/onyx/server/documents/api.py",
                                  "backend/onyx/server/documents/models.py"],
                    flows=[flow("manage-documents-flow",
                                "backend/onyx/server/documents/api.py")])
    connectors = dev("connectors", ["backend/onyx/connectors/registry.py",
                                    "backend/onyx/connectors/factory.py"])
    redis_wrap = dev("cache-management", ["backend/onyx/redis/redis_pool.py"])
    pfs, tele = mint([chat, documents, connectors, redis_wrap], [],
                     ctx_of(tracked=tracked))
    names = {p.name for p in pfs}
    assert "chat" in names and "documents" in names and "connectors" in names
    assert chat.product_feature_id == "chat"
    assert chat.anchor_id == "pypkg:backend/onyx/chat"
    assert documents.product_feature_id == "documents"
    # 1-file flowless wrapper package: thinness bar → honest lane.
    assert redis_wrap.product_feature_id is None
    assert redis_wrap.shared_reason == "sub_mint_bar_surface"
    rows = build_platform_infrastructure_lane(
        [chat, documents, connectors, redis_wrap])
    assert [r["name"] for r in rows] == ["cache-management"]


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


def test_hub_amendment_edr_five_vendor_pfs_plus_core(tmp_path):
    """Amendment case 5a: Soc0 edr → 5 vendor PFs + '<hub> Core'; the
    parent's per-vendor flows count as child evidence (entry files);
    sentinelone mints on own-code even with zero flows — its real
    Soc0 body is 1,258 LOC, above the W3.1 D4 husk floor (a husk-sized
    fake would now honestly fold)."""
    plumbing, kids = _edr_fixture()
    for rel in ("backend/services/edr/sentinelone.py",
                "backend/services/edr/schema/sentinelone_baseline.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"x{i} = {i}\n" for i in range(120)))
    pfs, tele = mint([plumbing, *kids], ctx=ctx_of(repo_path=str(tmp_path)))
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


# ── W4.2 Fix 1 — technology instruments never mint ───────────────────────


def _instrument_repo(tmp_path: Path):
    """typebot-Prisma-shaped mini monorepo ON DISK (the detector reads
    real manifests) + the matching dev/workspace fixtures."""
    import json as _json

    def w(rel: str, text: str = "") -> str:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return rel

    tracked = [
        w("package.json", _json.dumps({"name": "root", "private": True})),
        w("packages/ormkit/package.json", _json.dumps({
            "name": "@acme/ormkit",
            "dependencies": {"@prisma/client": "5.0.0"},
            "devDependencies": {"prisma": "5.0.0"},
        })),
        w("packages/ormkit/schema.prisma", "model User {}"),
        w("packages/ormkit/migrations/0001/migration.sql", "CREATE ..."),
        w("packages/ormkit/index.ts",
          'export { PrismaClient } from "@prisma/client";\n'),
        w("apps/web/package.json", _json.dumps({
            "name": "@acme/web", "private": True,
            "dependencies": {"react": "18.0.0"},
        })),
        w("apps/web/src/app/checkout/page.tsx",
          'import { db } from "@acme/ormkit";\n'),
    ]
    ws = [
        SimpleNamespace(path="packages/ormkit",
                        package_json={"name": "@acme/ormkit"}, files=None),
        SimpleNamespace(path="apps/web",
                        package_json={"name": "@acme/web"}, files=None),
    ]
    orm_dev = dev("ormkit", ["packages/ormkit/schema.prisma",
                             "packages/ormkit/index.ts",
                             "packages/ormkit/migrations/0001/migration.sql"])
    page = "apps/web/src/app/checkout/page.tsx"
    web_dev = dev("checkout", [page], flows=[flow("checkout-flow", page)])
    routes = [{"file": page, "pattern": "/checkout", "method": "PAGE"}]
    ctx = ctx_of(workspaces=ws, tracked=tracked, repo_path=tmp_path)
    return [orm_dev, web_dev], routes, ctx


def test_instrument_anchor_never_mints_devs_lane(tmp_path: Path) -> None:
    devs, routes, ctx = _instrument_repo(tmp_path)
    pfs, tele = mint(devs, routes=routes, ctx=ctx)
    names = {p.name for p in pfs}
    assert "ormkit" not in names, "instrument ws-pkg minted a PF"
    ti = tele.get("technology_instruments") or {}
    assert "packages/ormkit" in (ti.get("instruments") or {})
    orm_dev = devs[0]
    assert orm_dev.product_feature_id is None
    assert orm_dev.shared_reason == "technology_instrument"
    lane = build_platform_infrastructure_lane(devs)
    assert any(r["name"] == "ormkit"
               and r["shared_reason"] == "technology_instrument"
               for r in lane)
    # the product surface still mints
    assert "checkout" in names


def test_instrument_kill_switch_restores_the_mint(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_TECH_INSTRUMENTS", "0")
    devs, routes, ctx = _instrument_repo(tmp_path)
    pfs, tele = mint(devs, routes=routes, ctx=ctx)
    assert "ormkit" in {p.name for p in pfs}
    assert "technology_instruments" not in tele


# ── B78 Seg A — walk-evidence gate (FAULTLINE_FOLD_EVIDENCE_WEIGHT) ──────
#
# Fixtures distill the Soc0 probe canon (2026-07-21): a page dev hosts the
# walk-plurality vacuum; api-* router devs walk-fold into it with ZERO
# behavioral claim (the target anchor never touches their entry files).
# Armed, the void folds re-dispose by the dev's own entry evidence
# (rulings R1-R3); OFF stays byte-identical.


def _evw(monkeypatch):
    monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", "1")


def _cases_vacuum_repo():
    """The Soc0 shape: a 'cases' page capability + api routers whose walk
    folds are evidence-void (their entries live in backend routers the
    cases anchor never claims)."""
    routes = [
        {"pattern": "/cases", "method": "PAGE",
         "file": "frontend/src/pages/cases.tsx"},
        {"pattern": "/trial", "method": "GET",
         "file": "backend/routers/trial.py"},
        {"pattern": "/context-items", "method": "GET",
         "file": "backend/routers/context_items.py"},
        {"pattern": "/users/by-email", "method": "GET",
         "file": "backend/routers/users.py"},
    ]
    cases = dev("cases", ["frontend/src/pages/cases.tsx",
                          "frontend/src/pages/case-detail.tsx"],
                flows=[flow("browse-cases-flow",
                            "frontend/src/pages/cases.tsx")])
    trial = dev("api-trial-status", ["backend/routers/trial.py"],
                flows=[flow("check-trial-flow", "backend/routers/trial.py")])
    items = dev("api-context-items", ["backend/routers/context_items.py"],
                flows=[flow("browse-items-flow",
                            "backend/routers/context_items.py"),
                       flow("edit-items-flow",
                            "backend/routers/context_items.py")])
    users = dev("api-users-by-email", ["backend/routers/users.py",
                                       "backend/models/users_lookup.py"],
                flows=[flow("find-user-flow", "backend/routers/users.py"),
                       flow("sync-user-flow",
                            "backend/models/users_lookup.py")])
    return [cases, trial, items, users], routes


def test_evw_flag_default_off(monkeypatch):
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        fold_evidence_weight_enabled,
    )
    monkeypatch.delenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", raising=False)
    assert not fold_evidence_weight_enabled()
    monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", "0")
    assert not fold_evidence_weight_enabled()
    monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", "1")
    assert fold_evidence_weight_enabled()


def test_evw_off_keeps_the_void_walk_fold_byte_identical(monkeypatch):
    """Kill-switch law: unset == explicit 0 == the pre-B78 vacuum fold
    (api devs land in 'cases' — the disease, kept verbatim OFF)."""
    for env in (None, "0"):
        if env is None:
            monkeypatch.delenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT",
                               raising=False)
        else:
            monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", env)
        devs, routes = _cases_vacuum_repo()
        pfs, tele = mint(devs, routes)
        assert {p.name for p in pfs} == {"cases"}
        for d in devs[1:]:
            assert d.product_feature_id == "cases"
            assert (d.anchor_id or "").startswith(("fold:span", "fold:walk"))
        assert "walk_evidence_void" not in tele


def test_evw_vacuum_devs_escape_cases_named(monkeypatch):
    """Probe canon: api-trial-status / api-context-items /
    api-users-by-email are NOT in 'cases' when armed."""
    _evw(monkeypatch)
    devs, routes = _cases_vacuum_repo()
    _cases, trial, items, users = devs
    pfs, tele = mint(devs, routes)
    assert _cases.product_feature_id == "cases"
    for d in (trial, items, users):
        assert d.product_feature_id != "cases", d.name
        assert d.shared_reason is None, d.name  # R3: never laned
    assert tele["walk_evidence_void"] == 3
    assert tele["walk_evidence_redisposed"] == 3


def test_evw_r2_all_three_dispositions(monkeypatch):
    """R2 branches on the same fixture: >=2 distinct entry files =>
    standalone mint (users); >=2 flows on one entry => standalone mint
    (context-items); 1 flow => fold into the nearest evidence-claiming
    PF — none exists for trial.py, so the single-flow fallback mints
    (the probe's 'trial' standalone, tele-tracked)."""
    _evw(monkeypatch)
    devs, routes = _cases_vacuum_repo()
    _cases, trial, items, users = devs
    pfs, tele = mint(devs, routes)
    by_name = {p.name: p for p in pfs}
    # (i) >=2 distinct entry files — standalone mint.
    assert users.product_feature_id == "users"
    assert (users.anchor_id or "").startswith("mint:entry-evidence")
    # (ii) >=2 flows, single entry — standalone mint.
    assert items.product_feature_id == "context-items"
    assert (items.anchor_id or "").startswith("mint:entry-evidence")
    # (iii) 1 flow — no evidence-claiming PF exists => fallback mint,
    # counted under the single-flow tele (the R2 brake's honest residue).
    assert trial.product_feature_id == "trial"
    assert tele.get("walk_evidence_single_flow_mint") == 1
    assert {"users", "context-items", "trial"} <= set(by_name)


def test_evw_r2_single_flow_folds_into_claiming_pf(monkeypatch):
    """R2 branch (iii) proper — the Soc0 chat→conversations shape: an
    earlier evidence-void dev mints 'conversations' on demand; the LATER
    single-flow dev whose only journey enters the same router file FOLDS
    into that fresh evidence-claiming PF instead of minting its own."""
    _evw(monkeypatch)
    routes = [
        {"pattern": "/cases", "method": "PAGE",
         "file": "frontend/src/pages/cases.tsx"},
        {"pattern": "/conversations", "method": "GET",
         "file": "backend/routers/convo.py"},
        {"pattern": "/conversations/archive", "method": "GET",
         "file": "backend/routers/convo.py"},
    ]
    cases = dev("cases", ["frontend/src/pages/cases.tsx",
                          "frontend/src/pages/case-detail.tsx"],
                flows=[flow("browse-cases-flow",
                            "frontend/src/pages/cases.tsx")])
    convo = dev("api-conversations", ["backend/routers/convo.py"],
                flows=[flow("chat-flow", "backend/routers/convo.py"),
                       flow("archive-chat-flow", "backend/routers/convo.py")])
    tail_flow = flow("archive-old-chats-flow", "backend/routers/convo.py")
    # span mass mostly unresolvable => the span-vote coherence floor
    # fails and the dev reaches the walk rung (the Soc0 chat shape).
    tail_flow.paths = ["backend/routers/convo.py", "tools/convo_archiver.py",
                       "tools/retention_policy.py", "tools/cron_glue.py"]
    tail = dev("chat-tail", ["tools/convo_archiver.py"],
               flows=[tail_flow])
    pfs, tele = mint([cases, convo, tail], routes)
    assert convo.product_feature_id == "conversations"
    assert (convo.anchor_id or "").startswith("mint:entry-evidence")
    assert tail.product_feature_id == "conversations"
    assert (tail.anchor_id or "").startswith("fold:entry-evidence")
    assert not any(p.name in {"chat-tail", "convo-archiver"} for p in pfs)


def test_evw_r1_tiebreak_prefers_the_files_own_slug_family(monkeypatch):
    """R1 (a): backend/routers/admin.py carries several route families
    (admins + webhook-dispatcher); the exact-file tie resolves to the
    admins family — the file's OWN slug — never 'webhook-dispatcher'
    (the api-admin probe misfire)."""
    _evw(monkeypatch)
    routes = [
        {"pattern": "/cases", "method": "PAGE",
         "file": "frontend/src/pages/cases.tsx"},
        {"pattern": "/admins", "method": "GET",
         "file": "backend/routers/admin.py"},
        {"pattern": "/admins/roles", "method": "GET",
         "file": "backend/routers/admin.py"},
        {"pattern": "/webhook-dispatcher", "method": "POST",
         "file": "backend/routers/admin.py"},
    ]
    cases = dev("cases", ["frontend/src/pages/cases.tsx",
                          "frontend/src/pages/case-detail.tsx"],
                flows=[flow("browse-cases-flow",
                            "frontend/src/pages/cases.tsx")])
    admin = dev("api-admin", ["backend/routers/admin.py"],
                flows=[flow("manage-admins-flow", "backend/routers/admin.py"),
                       flow("dispatch-hook-flow",
                            "backend/routers/admin.py")])
    pfs, tele = mint([cases, admin], routes)
    assert admin.product_feature_id == "admins"
    assert not any(p.name == "webhook-dispatcher" for p in pfs)
    assert tele.get("walk_evidence_r1_stem", 0) >= 1


def test_evw_r1_tiebreak_falls_to_route_share(monkeypatch):
    """R1 (b): when no anchor is named by the file's own stem, the
    exact-file tie resolves to the anchor holding the larger share of
    the file's routes."""
    _evw(monkeypatch)
    routes = [
        {"pattern": "/cases", "method": "PAGE",
         "file": "frontend/src/pages/cases.tsx"},
        {"pattern": "/invoices", "method": "GET",
         "file": "backend/routers/handlers.py"},
        {"pattern": "/invoices/export", "method": "GET",
         "file": "backend/routers/handlers.py"},
        {"pattern": "/invoices/archive", "method": "POST",
         "file": "backend/routers/handlers.py"},
        {"pattern": "/webhook-dispatcher", "method": "POST",
         "file": "backend/routers/handlers.py"},
    ]
    cases = dev("cases", ["frontend/src/pages/cases.tsx",
                          "frontend/src/pages/case-detail.tsx"],
                flows=[flow("browse-cases-flow",
                            "frontend/src/pages/cases.tsx")])
    api = dev("api-billing", ["backend/routers/handlers.py"],
              flows=[flow("send-invoice-flow", "backend/routers/handlers.py"),
                     flow("archive-invoice-flow",
                          "backend/routers/handlers.py")])
    pfs, tele = mint([cases, api], routes)
    assert api.product_feature_id == "invoices"
    assert not any(p.name == "webhook-dispatcher" for p in pfs)
    assert tele.get("walk_evidence_r1_route_share", 0) >= 1


def test_evw_r3_dev_standalone_mint_never_lane(monkeypatch):
    """R3: a flowful dev whose entry files NO anchor claims (unextracted
    router — the Soc0 'api'/'network-mock' shape) stands alone as its
    own PF; it is never laned and never left in the vacuum."""
    _evw(monkeypatch)
    routes = [
        {"pattern": "/cases", "method": "PAGE",
         "file": "frontend/src/pages/cases.tsx"},
    ]
    cases = dev("cases", ["frontend/src/pages/cases.tsx",
                          "frontend/src/pages/case-detail.tsx"],
                flows=[flow("browse-cases-flow",
                            "frontend/src/pages/cases.tsx")])
    mock = dev("network-mock", ["backend/routers/network_mock.py"],
               flows=[flow("serve-mock-flow",
                           "backend/routers/network_mock.py"),
                      flow("reset-mock-flow",
                           "backend/routers/network_mock.py")])
    pfs, tele = mint([cases, mock], routes)
    assert mock.product_feature_id == "network-mock"
    assert (mock.anchor_id or "").startswith("mint:dev-standalone")
    assert mock.shared_reason is None
    assert tele.get("walk_evidence_dev_mint") == 1
    assert build_platform_infrastructure_lane([cases, mock]) == []
    # flag OFF: same dev stays the vacuum fold (proves the gate did it)
    monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", "0")
    devs2 = [dev("cases", ["frontend/src/pages/cases.tsx",
                           "frontend/src/pages/case-detail.tsx"],
                 flows=[flow("browse-cases-flow",
                             "frontend/src/pages/cases.tsx")]),
             dev("network-mock", ["backend/routers/network_mock.py"],
                 flows=[flow("serve-mock-flow",
                             "backend/routers/network_mock.py")])]
    mint(devs2, routes)
    assert devs2[1].product_feature_id == "cases"


def test_evw_fold_entry_anti_case_untouched(monkeypatch):
    """Anti-case (probe: 25 Soc0 fold:entry survivors): a dev resolved
    by the ENTRY rung — its journeys enter a minted capability's own
    surface — keeps the identical disposition with the flag armed."""
    devs_off, routes_off = None, None
    results = {}
    for env in ("0", "1"):
        monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", env)
        routes = [
            {"pattern": "/reports", "method": "PAGE",
             "file": "frontend/src/pages/reports/index.tsx"},
        ]
        reports = dev(
            "reports",
            ["frontend/src/pages/reports/index.tsx",
             "frontend/src/pages/reports/detail.tsx"],
            flows=[flow("browse-reports-flow",
                        "frontend/src/pages/reports/index.tsx")])
        helper = dev(
            "report-widgets",
            ["frontend/src/widgets/report_grid.tsx"],
            flows=[flow("render-grid-flow",
                        "frontend/src/pages/reports/detail.tsx")])
        pfs, tele = mint([reports, helper], routes)
        results[env] = (helper.product_feature_id, helper.anchor_id,
                        sorted(p.name for p in pfs))
    assert results["0"] == results["1"]
    assert results["1"][1].startswith("fold:entry->")


def test_evw_lineage_world_is_inert(monkeypatch):
    """Unit-pin (twenty): a lineage-shaped world — every dev wins its
    own minting anchor, no walk folds — is byte-inert under the armed
    flag (the probe's twenty 0-movement expectation)."""
    _evw(monkeypatch)
    routes = [
        {"pattern": "/people", "method": "PAGE",
         "file": "packages/front/src/pages/people/index.tsx"},
        {"pattern": "/companies", "method": "PAGE",
         "file": "packages/front/src/pages/companies/index.tsx"},
    ]
    people = dev("people", ["packages/front/src/pages/people/index.tsx"],
                 flows=[flow("browse-people-flow",
                             "packages/front/src/pages/people/index.tsx")])
    companies = dev(
        "companies", ["packages/front/src/pages/companies/index.tsx"],
        flows=[flow("browse-companies-flow",
                    "packages/front/src/pages/companies/index.tsx")])
    pfs, tele = mint([people, companies], routes)
    assert {p.name for p in pfs} == {"people", "companies"}
    assert people.anchor_id.startswith("route:")
    assert companies.anchor_id.startswith("route:")
    assert not any(k.startswith("walk_evidence") for k in tele)


def test_evw_conservation_of_files(monkeypatch):
    """Conservation: the gate re-homes devs — it never drops or invents
    files/devs. OFF vs ON: same dev set assigned, same file universe."""
    def _world(env):
        monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", env)
        devs, routes = _cases_vacuum_repo()
        pfs, _tele = mint(devs, routes)
        dev_files = {d.name: sorted(d.paths) for d in devs}
        assigned = {d.name for d in devs if d.product_feature_id}
        pf_members = set()
        for p in pfs:
            for mf in (getattr(p, "member_files", None) or []):
                pf_members.add(mf.path if hasattr(mf, "path")
                               else mf.get("path"))
        return dev_files, assigned, pf_members
    files_off, assigned_off, members_off = _world("0")
    files_on, assigned_on, members_on = _world("1")
    assert files_off == files_on
    assert assigned_off == assigned_on
    assert members_off == members_on
