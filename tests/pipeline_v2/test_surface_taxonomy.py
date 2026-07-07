"""Product-Spine Wave 2a — product-surface taxonomy (spec §4.2).

Classifier signals (route-groups / workspace class / URL lexicon / system
triggers / container identity), the Layer-1 tagging pass, the emission
lane (non-product PFs leave the product list, journeys ride along), the
info-page dissolution, the shared-dev re-bind, the I22 reason stamping,
the kill-switches, and the YAML drift guard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.surface_taxonomy import (
    SurfaceScopeClassifier,
    apply_emission_taxonomy,
    is_non_product_dev,
    tag_layer1,
)


def _feature(name: str, paths: list[str], pfid: str | None = None,
             *, layer: str = "developer", flows: list[Flow] | None = None,
             display_name: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=display_name, paths=paths, authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0, layer=layer, product_feature_id=pfid,
        flows=flows or [],
    )


def _flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, uuid=name, entry_point_file=entry,
        paths=paths or [entry], authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
    )


def _uf(uf_id: str, name: str, pfid: str | None, members: list[str],
        routes: list[str] | None = None,
        category: str = "interactive") -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="browse", resource="page",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), routes=routes or [], category=category,
    )


# ── classifier signals ─────────────────────────────────────────────────


def test_classify_path_signals() -> None:
    c = SurfaceScopeClassifier()
    assert c.classify_path("apps/www/pages/blog/x.tsx") == "marketing"
    assert c.classify_path("apps/docs/content/api.mdx") == "docs"
    assert c.classify_path("packages/cli/src/main.ts") == "dev_tooling"
    assert c.classify_path("app/(marketing)/pricing/page.tsx") == "marketing"
    assert c.classify_path("app/legal/terms/page.tsx") == "legal"
    # The author's (dashboard) declaration is a decisive PRODUCT vote.
    assert c.classify_path("app/(dashboard)/settings/page.tsx") == "product"
    # No signal → None (never guesses); source dirs never match the
    # URL lexicon (blog-model is not a blog page).
    assert c.classify_path("app/checkout/page.tsx") is None
    assert c.classify_path("src/features/blog-model/x.ts") is None


def test_shell_group_never_paints_a_subtree() -> None:
    """Regression (polar, 2026-07-06): ``(main)`` wraps the WHOLE product
    app — a shell-NAMED route group must not vote shell on its subtree
    (that falsely demoted polar's Analytics/Usage PFs to the lane). Shell
    comes only from the root route pattern and the container name."""
    c = SurfaceScopeClassifier()
    assert c.classify_path(
        "clients/apps/web/src/app/(main)/dashboard/[org]/(header)/analytics/page.tsx"
    ) is None
    assert c.classify_path(
        "apps/web/app/(ee)/x/(dashboard)/programs/customers/(index)/page.tsx"
    ) == "product"  # (dashboard) product vote survives; (index) abstains
    pf = _feature("analytics", [
        "clients/apps/web/src/app/(main)/dashboard/[org]/analytics/page.tsx",
        "clients/apps/web/src/utils/organization.ts",
    ], layer="product", display_name="Analytics")
    assert c.classify_feature(pf) == "product"


def _typebot_landing_routes() -> list[dict]:
    lp = "apps/landing-page/src/routes"
    return [
        {"pattern": "/healthz", "method": "PAGE", "file": f"{lp}/healthz.ts"},
        {"pattern": "/sitemap[.]xml", "method": "PAGE",
         "file": f"{lp}/sitemap[.]xml.ts"},
        {"pattern": "/_layout/about", "method": "PAGE",
         "file": f"{lp}/_layout/about.tsx"},
        {"pattern": "/_layout/oss-friends", "method": "PAGE",
         "file": f"{lp}/_layout/oss-friends.tsx"},
        {"pattern": "/_layout/pricing", "method": "PAGE",
         "file": f"{lp}/_layout/pricing.tsx"},
        {"pattern": "/_layout/blog/$slug", "method": "PAGE",
         "file": f"{lp}/_layout/blog/$slug.tsx"},
        {"pattern": "/_layout/blog", "method": "PAGE",
         "file": f"{lp}/_layout/blog/index.tsx"},
        {"pattern": "/_layout/faq/$slug", "method": "PAGE",
         "file": f"{lp}/_layout/faq/$slug.tsx"},
        {"pattern": "/_layout/faq", "method": "PAGE",
         "file": f"{lp}/_layout/faq/index.tsx"},
        {"pattern": "/_layout/templates/$slug", "method": "PAGE",
         "file": f"{lp}/_layout/templates/$slug.tsx"},
        {"pattern": "/_layout/templates", "method": "PAGE",
         "file": f"{lp}/_layout/templates/index.tsx"},
    ]


def test_marketing_workspace_class_override_typebot_shape() -> None:
    """W2b.1 fix (c1): a workspace whose routes are ALL public marketing/
    info pages (typebot apps/landing-page — TanStack, no route groups) is
    a MARKETING surface: every path under it classifies marketing, so the
    pricing/blog/about PFs reach the non-product lane at emission."""
    # typebot's actual name rides the workspace VOCAB (landing-page was
    # added to the lexicon); the structural override covers any name.
    c = SurfaceScopeClassifier(routes_index=_typebot_landing_routes())
    assert c.classify_path(
        "apps/landing-page/src/features/pricing/PlanComparisonsTables.tsx"
    ) == "marketing"
    # STRUCTURAL case — same route profile under a lexicon-free name.
    promo = [dict(e, file=e["file"].replace("apps/landing-page/",
                                            "apps/promo-site/"))
             for e in _typebot_landing_routes()]
    cs = SurfaceScopeClassifier(routes_index=promo)
    assert cs._ws_scope_overrides == {"apps/promo-site": "marketing"}
    assert cs.classify_path(
        "apps/promo-site/src/features/pricing/PlanComparisonsTables.tsx"
    ) == "marketing"
    pricing = _feature("pricing", [
        "apps/landing-page/src/routes/_layout/pricing.tsx",
        "apps/landing-page/src/features/pricing/PlanComparisonsTables.tsx",
        "apps/landing-page/src/features/pricing/PricingHeading.tsx",
        "apps/landing-page/src/features/pricing/Faq.tsx",
    ], layer="product", display_name="Pricing")
    assert c.classify_feature(pricing) == "marketing"


def test_marketing_workspace_override_never_fires_with_api_routes() -> None:
    """openstatus apps/web keeps its product surface: ANY api-method
    route (webhook/search/upload) disqualifies the workspace override,
    and a product-declared route group does too."""
    web = [
        {"pattern": "/blog/why", "method": "PAGE",
         "file": "apps/web/src/app/(landing)/blog/why/page.tsx"},
        {"pattern": "/pricing", "method": "PAGE",
         "file": "apps/web/src/app/(landing)/pricing/page.tsx"},
        {"pattern": "/api/webhook/stripe", "method": "POST",
         "file": "apps/web/src/app/api/webhook/stripe/route.ts"},
    ]
    c = SurfaceScopeClassifier(routes_index=web)
    assert c._ws_scope_overrides == {}
    # product-group case: all PAGE but one route declares (dashboard)
    dash = [
        {"pattern": "/pricing", "method": "PAGE",
         "file": "apps/x/src/app/pricing/page.tsx"},
        {"pattern": "/blog", "method": "PAGE",
         "file": "apps/x/src/app/blog/page.tsx"},
        {"pattern": "/home", "method": "PAGE",
         "file": "apps/x/src/app/(dashboard)/home/page.tsx"},
    ]
    c2 = SurfaceScopeClassifier(routes_index=dash)
    assert c2._ws_scope_overrides == {}
    # vocab-classified workspaces are skipped (vocab stays authoritative)
    www = [
        {"pattern": "/blog", "method": "PAGE",
         "file": "apps/www/pages/blog.tsx"},
        {"pattern": "/pricing", "method": "PAGE",
         "file": "apps/www/pages/pricing.tsx"},
    ]
    c3 = SurfaceScopeClassifier(routes_index=www)
    assert "apps/www" not in c3._ws_scope_overrides


def test_classify_route_entry_trigger_and_shell() -> None:
    c = SurfaceScopeClassifier()
    assert c.classify_route_entry(
        {"pattern": "/api/cron/sync", "file": "app/api/cron/sync/route.ts",
         "trigger": "scheduled"},
    ) == "system"
    assert c.classify_route_entry(
        {"pattern": "/blog/why", "file": "apps/www/pages/blog/why.tsx"},
    ) == "marketing"
    # Bare root page with no other signal = the app shell.
    assert c.classify_route_entry(
        {"pattern": "/", "file": "app/page.tsx"},
    ) == "shell"
    # Unmatched real route = product surface (conservative).
    assert c.classify_route_entry(
        {"pattern": "/checkout", "file": "app/checkout/page.tsx"},
    ) == "product"


def test_classify_feature_majority_and_shell_identity() -> None:
    c = SurfaceScopeClassifier()
    www = _feature("www", ["apps/www/pages/blog/a.tsx",
                           "apps/www/pages/pricing.tsx",
                           "apps/www/lib/util.ts"])
    assert c.classify_feature(www) == "marketing"
    # Product evidence ties-or-beats non-product → product (conservative).
    mixed = _feature("surveys", ["app/(dashboard)/surveys/page.tsx",
                                 "apps/www/pages/blog/a.tsx"])
    assert c.classify_feature(mixed) == "product"
    shell = _feature("home-page", ["app/page.tsx"])
    assert c.classify_feature(shell) == "shell"
    # No signal at all → product.
    plain = _feature("checkout", ["src/checkout/api.py"])
    assert c.classify_feature(plain) == "product"


def test_is_non_product_dev_reads_stamped_tag_only() -> None:
    dev = _feature("blog", ["apps/www/pages/blog/a.tsx"])
    assert is_non_product_dev(dev) is False  # not stamped yet
    dev.surface_scope = "marketing"
    assert is_non_product_dev(dev) is True
    dev.surface_scope = "shell"  # shell is the container guard's job
    assert is_non_product_dev(dev) is False
    dev.surface_scope = "system"
    assert is_non_product_dev(dev) is False


def test_published_cli_workspace_is_product_not_dev_tooling(
    tmp_path: Path,
) -> None:
    """Operator doctrine (midday review, 2026-07-06): a cli workspace
    whose package.json ships a bin (and is not private) is a PRODUCT
    surface — customers install and drive it; its journeys are real."""
    import json as _json

    shipped = tmp_path / "packages" / "cli"
    shipped.mkdir(parents=True)
    (shipped / "package.json").write_text(_json.dumps({
        "name": "@midday/cli", "bin": {"midday": "./dist/index.js"},
    }))
    internal = tmp_path / "tools" / "cli"
    internal.mkdir(parents=True)
    (internal / "package.json").write_text(_json.dumps({
        "name": "@internal/cli", "private": True,
        "bin": {"x": "./x.js"},
    }))
    bare = tmp_path / "apps" / "cli"
    bare.mkdir(parents=True)
    (bare / "package.json").write_text(_json.dumps({"name": "cli-helpers"}))

    c = SurfaceScopeClassifier(repo_path=tmp_path)
    # Published bin + not private → product override.
    assert c.classify_path("packages/cli/src/auth.ts") is None or \
        c.classify_path("packages/cli/src/auth.ts") == "product"
    assert c.classify_path("packages/cli/src/auth.ts") == "product"
    # private:true → stays dev_tooling despite the bin.
    assert c.classify_path("tools/cli/src/main.ts") == "dev_tooling"
    # No bin at all → stays dev_tooling.
    assert c.classify_path("apps/cli/src/main.ts") == "dev_tooling"
    # Feature grain: the shipped CLI dev stays a product feature.
    dev = _feature("cli", ["packages/cli/src/auth.ts",
                           "packages/cli/src/deploy.ts"])
    assert c.classify_feature(dev) == "product"
    # Without repo_path (no reads) the lexicon verdict stands.
    c2 = SurfaceScopeClassifier()
    assert c2.classify_path("packages/cli/src/auth.ts") == "dev_tooling"


# ── Layer-1 tagging ─────────────────────────────────────────────────────


def test_tag_layer1_stamps_routes_and_devs() -> None:
    devs = [
        _feature("blog", ["apps/www/pages/blog/a.tsx"]),
        _feature("surveys", ["app/(dashboard)/surveys/page.tsx"]),
    ]
    routes = [
        {"pattern": "/blog/a", "file": "apps/www/pages/blog/a.tsx",
         "trigger": "interactive"},
        {"pattern": "/surveys", "file": "app/(dashboard)/surveys/page.tsx",
         "trigger": "interactive"},
    ]
    tele = tag_layer1(devs, routes)
    assert all("surface_scope" in r for r in routes)
    assert routes[0]["surface_scope"] == "marketing"
    assert routes[1]["surface_scope"] == "product"
    assert devs[0].surface_scope == "marketing"
    assert devs[1].surface_scope == "product"
    assert tele["dev_scopes"] == {"marketing": 1, "product": 1}


def test_tag_layer1_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SURFACE_TAXONOMY", "0")
    devs = [_feature("blog", ["apps/www/pages/blog/a.tsx"])]
    routes = [{"pattern": "/blog/a", "file": "apps/www/pages/blog/a.tsx"}]
    tele = tag_layer1(devs, routes)
    assert tele == {"enabled": False}
    assert devs[0].surface_scope is None
    assert "surface_scope" not in routes[0]
    # Omit-unset serialization: untagged features dump without the key.
    assert "surface_scope" not in devs[0].model_dump()


# ── emission lane ───────────────────────────────────────────────────────


def _emission_fixture():
    """supabase-in-miniature: a marketing-site PF + a product PF."""
    blog_flow = _flow("browse-blog-flow", "apps/www/pages/blog/index.tsx")
    surveys_flow = _flow("create-survey-flow",
                         "app/(dashboard)/surveys/new/page.tsx")
    www_dev = _feature("www-blog", ["apps/www/pages/blog/index.tsx"],
                       "marketing-site-pages", flows=[blog_flow])
    surveys_dev = _feature("surveys", ["app/(dashboard)/surveys/new/page.tsx"],
                           "surveys", flows=[surveys_flow])
    pf_www = _feature("marketing-site-pages",
                      ["apps/www/pages/blog/index.tsx"],
                      layer="product", display_name="Marketing Site Pages")
    pf_surveys = _feature("surveys", ["app/(dashboard)/surveys/new/page.tsx"],
                          layer="product", display_name="Surveys")
    uf_blog = _uf("UF-001", "Browse marketing pages", "marketing-site-pages",
                  ["browse-blog-flow"])
    uf_surveys = _uf("UF-002", "Create a survey", "surveys",
                     ["create-survey-flow"])
    devs = [www_dev, surveys_dev]
    pfs = [pf_www, pf_surveys]
    ufs = [uf_blog, uf_surveys]
    flows = [blog_flow, surveys_flow]
    routes = [
        {"pattern": "/blog", "file": "apps/www/pages/blog/index.tsx",
         "trigger": "interactive"},
        {"pattern": "/surveys/new",
         "file": "app/(dashboard)/surveys/new/page.tsx",
         "trigger": "interactive"},
    ]
    return devs, pfs, ufs, flows, routes


def test_lane_split_moves_marketing_pf_with_its_journeys() -> None:
    devs, pfs, ufs, flows, routes = _emission_fixture()
    tele, lane, product = apply_emission_taxonomy(devs, pfs, ufs, flows, routes)
    # The marketing PF left the product list into the lane…
    assert [p.name for p in product] == ["surveys"]
    assert [e["name"] for e in lane] == ["marketing-site-pages"]
    assert lane[0]["surface_scope"] == "marketing"
    assert lane[0]["member_devs"] == ["www-blog"]
    # …taking its journey along (removed from user_flows[]).
    assert [u.id for u in ufs] == ["UF-002"]
    assert [u["name"] for u in lane[0]["user_flows"]] == [
        "Browse marketing pages",
    ]
    # Surviving rows are tagged; the product PF stays product-scope.
    assert product[0].surface_scope == "product"
    assert ufs[0].surface_scope == "product"
    assert tele["pfs_moved_to_lane"] == 1
    assert tele["ufs_moved_to_lane"] == 1


def test_lane_split_leaves_system_pf_in_product_list() -> None:
    cron_flow = _flow("sync-cron-flow", "app/api/cron/sync/route.ts")
    dev = _feature("cron", ["app/api/cron/sync/route.ts"],
                   "background-jobs", flows=[cron_flow])
    pf = _feature("background-jobs", ["app/api/cron/sync/route.ts"],
                  layer="product", display_name="Background Jobs")
    uf = _uf("UF-001", "Run scheduled syncs", "background-jobs",
             ["sync-cron-flow"], category="system")
    routes = [{"pattern": "/api/cron/sync",
               "file": "app/api/cron/sync/route.ts",
               "trigger": "scheduled"}]
    tele, lane, product = apply_emission_taxonomy(
        [dev], [pf], [uf], [cron_flow], routes,
    )
    assert lane == []
    assert product == [pf]
    assert pf.surface_scope == "system"  # I20 allows system in the list
    assert uf.surface_scope == "system"


def test_shared_bucket_row_never_moves_and_stays_product_tagged() -> None:
    shared = _feature("shared-platform", ["apps/www/lib/util.ts"],
                      layer="product", display_name="Shared Platform")
    tele, lane, product = apply_emission_taxonomy([], [shared], [], [], [])
    assert lane == [] and product == [shared]
    assert shared.surface_scope == "product"


def test_info_page_uf_dissolves_into_hosting_uf() -> None:
    contact_flow = _flow("view-contact-flow",
                         "app/(marketing)/contact/page.tsx")
    checkout_flow = _flow("checkout-flow", "app/checkout/page.tsx")
    dev = _feature("shop", ["app/checkout/page.tsx",
                            "app/(marketing)/contact/page.tsx"],
                   "shop", flows=[checkout_flow, contact_flow])
    pf = _feature("shop", ["app/checkout/page.tsx",
                           "app/(marketing)/contact/page.tsx"],
                  layer="product", display_name="Shop")
    host = _uf("UF-001", "Check out", "shop", ["checkout-flow"])
    info = _uf("UF-002", "View contact page", "shop", ["view-contact-flow"])
    ufs = [host, info]
    tele, lane, product = apply_emission_taxonomy(
        [dev], [pf], ufs, [contact_flow, checkout_flow], [],
    )
    # The info journey dissolved: its flow became a plain dev-flow of the
    # hosting UF; the info UF row is gone; the PF still has its journey.
    assert [u.id for u in ufs] == ["UF-001"]
    assert set(host.member_flow_ids) == {"checkout-flow", "view-contact-flow"}
    assert host.member_count == 2
    assert tele["info_ufs_dissolved"] == 1
    assert product == [pf]  # a lone info page never re-scopes the PF


def test_info_page_uf_without_host_stays_tagged_only() -> None:
    contact_flow = _flow("view-contact-flow",
                         "app/(marketing)/contact/page.tsx")
    dev = _feature("contact", ["app/(marketing)/contact/page.tsx"],
                   "contact-pf", flows=[contact_flow])
    pf = _feature("contact-pf", ["app/(marketing)/contact/page.tsx"],
                  layer="product", display_name="Contact")
    only = _uf("UF-001", "View contact page", "contact-pf",
               ["view-contact-flow"])
    ufs = [only]
    tele, lane, product = apply_emission_taxonomy(
        [dev], [pf], ufs, [contact_flow], [],
    )
    # The whole PF is marketing-scope → it moved to the lane WITH its UF
    # (the lane arm wins before dissolution for pure info surfaces).
    assert product == [] and len(lane) == 1
    assert ufs == []
    assert tele["info_ufs_dissolved"] == 0


def test_non_product_shared_dev_rebinds_to_lane_surface() -> None:
    blog_flow = _flow("browse-blog-flow", "apps/www/pages/blog/index.tsx")
    www_dev = _feature("www-blog", ["apps/www/pages/blog/index.tsx"],
                       "marketing-site-pages", flows=[blog_flow])
    stray = _feature("www-brand", ["apps/www/pages/brand.tsx"],
                     "shared-platform")
    pf_www = _feature("marketing-site-pages",
                      ["apps/www/pages/blog/index.tsx",
                       "apps/www/pages/brand.tsx"],
                      layer="product")
    uf = _uf("UF-001", "Browse marketing pages", "marketing-site-pages",
             ["browse-blog-flow"])
    tele, lane, product = apply_emission_taxonomy(
        [www_dev, stray], [pf_www], [uf], [blog_flow], [],
    )
    assert len(lane) == 1
    # The stray marketing dev left the shared bucket for the lane surface.
    assert stray.product_feature_id == "marketing-site-pages"
    assert tele["devs_rebound_to_lane"] == 1
    # It is NOT a shared resident anymore → no shared_reason.
    assert stray.shared_reason is None


def test_shared_reasons_stamped_on_every_shared_resident() -> None:
    anchor_flow = _flow("do-edr-flow", "src/edr/api.py")
    residents = [
        _feature("web", ["packages/web/index.ts"], "shared-platform"),
        _feature("email", ["src/email/送信.py"], "shared-platform"),
        _feature("edr", ["src/edr/api.py"], "shared-platform",
                 flows=[anchor_flow]),
        _feature("mystery", ["src/misc/x.py"], "shared-platform"),
    ]
    pf = _feature("shared-platform", [], layer="product")
    tele, lane, product = apply_emission_taxonomy(
        residents, [pf], [], [anchor_flow], [],
    )
    reasons = {d.name: d.shared_reason for d in residents}
    assert reasons["web"] == "genuinely_shared_infra"   # structure-leak slug
    assert reasons["email"] == "facet_view"             # concern-named
    assert reasons["edr"] == "awaiting_wave2_mint"      # flowful, product-named
    assert reasons["mystery"] == "no_anchor_lineage"
    assert tele["shared_reasons"]["genuinely_shared_infra"] >= 1
    # Serialization: reasons ride the dump; unset stays omitted.
    assert residents[0].model_dump()["shared_reason"] == "genuinely_shared_infra"
    assert "shared_reason" not in pf.model_dump()


def test_lane_kill_switch_keeps_tags_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_SURFACE_LANE", "0")
    devs, pfs, ufs, flows, routes = _emission_fixture()
    tele, lane, product = apply_emission_taxonomy(devs, pfs, ufs, flows, routes)
    assert lane == []
    assert len(product) == 2 and len(ufs) == 2
    # Tags still stamped (I20 arms and FAILS on such a scan — that is the
    # point of the switch split: taxonomy visible, consequences bisectable).
    assert pfs[0].surface_scope == "marketing"
    # Shared reasons ride their own switch — still stamped here (none shared).
    assert tele.get("pfs_moved_to_lane") is None


def test_taxonomy_master_kill_switch_is_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_SURFACE_TAXONOMY", "0")
    devs, pfs, ufs, flows, routes = _emission_fixture()
    tele, lane, product = apply_emission_taxonomy(devs, pfs, ufs, flows, routes)
    assert tele == {"enabled": False}
    assert lane == [] and product is pfs
    assert all(p.surface_scope is None for p in pfs)
    assert all(u.surface_scope is None for u in ufs)
    # Byte-identity contract: nothing tagged → dumps carry no new keys.
    assert "surface_scope" not in pfs[0].model_dump()
    assert "surface_scope" not in ufs[0].model_dump()
    assert "binding_confidence" not in ufs[0].model_dump()


# ── data drift guard (house pattern: eval/ authoring == packaged copy) ──


def test_surface_scope_yaml_matches_eval_authoring_copy() -> None:
    from faultline.pipeline_v2.data import load_data_text

    repo_root = Path(__file__).resolve().parents[2]
    authoring = (repo_root / "eval" / "surface-scope-patterns.yaml").read_text(
        encoding="utf-8",
    )
    load_data_text.cache_clear()
    packaged = load_data_text("surface-scope-patterns.yaml")
    assert packaged == authoring, (
        "DRIFT: faultline/pipeline_v2/data/surface-scope-patterns.yaml "
        "differs from eval/surface-scope-patterns.yaml. Re-sync the "
        "in-package copy."
    )


# ── W4.2 Fix 1 — instrument dirs at the emission classifier ──────────────


def test_instrument_dirs_classify_devs_but_never_flip_anchored_pfs() -> None:
    """The midday `banking` shape: a PRODUCT capability whose paths
    straddle instrument dirs (post-fold shared members) stays product at
    PF grain; a dev living inside an instrument dir still tags
    dev_tooling; a PF ANCHORED inside an instrument dir still leaves for
    the lane."""
    kit_dev = _feature("uikit", ["packages/uikit/src/button.tsx",
                                 "packages/uikit/src/dialog.tsx"],
                       pfid=None)
    bank_dev = _feature("banking-core", ["packages/banking/src/index.ts"],
                        pfid="banking")
    banking = _feature(
        "banking",
        # 1 own file + 2 instrument-dir files (fold/shared riders) —
        # an instrument-path MAJORITY without the exemption.
        ["packages/banking/src/index.ts", "packages/uikit/src/button.tsx",
         "packages/uikit/src/dialog.tsx"],
        layer="product")
    banking.anchor_id = "ws:packages/banking"
    kit_pf = _feature("uikit", ["packages/uikit/src/button.tsx"],
                      layer="product")
    kit_pf.anchor_id = "ws:packages/uikit"
    tele, lane, product = apply_emission_taxonomy(
        [kit_dev, bank_dev], [banking, kit_pf], [], [], [],
        instrument_dirs=["packages/uikit"],
    )
    assert [p.name for p in product] == ["banking"], (
        "anchored product PF flipped by instrument paths")
    assert banking.surface_scope == "product"
    assert [e["name"] for e in lane] == ["uikit"]
    assert lane[0]["surface_scope"] == "dev_tooling"
    assert kit_dev.surface_scope == "dev_tooling"


# ── W5.1 — adjudicator product-route guard (midday `transactions`) ──────


def test_adjudicator_route_guard_protects_product_route_pf() -> None:
    """A PF that owns a real PRODUCT route file is never sent to the Surface
    Adjudicator for a non-product flip — even when a stray marketing sibling
    (same-key merge) makes it ambiguous. The midday `transactions` shape: a
    dashboard route + a marketing-site `.../transactions` page. A control PF
    whose product signal is mere lexicon (no route) is still flippable."""
    def _flip_all(items):
        return {i["id"]: "marketing" for i in items}

    routes = [{"file": "apps/dashboard/src/app/transactions/page.tsx",
               "pattern": "/transactions"}]
    # transactions: OWNS a product route + a marketing-site page → ambiguous
    transactions = _feature(
        "transactions",
        ["apps/dashboard/src/app/transactions/page.tsx",
         "apps/website/src/app/transactions/page.tsx"],
        layer="product")
    # control: product signal is LEXICON only (no route) + marketing → flips
    promo = _feature(
        "promo",
        ["app/(dashboard)/x/page.tsx", "app/(marketing)/y/page.tsx"],
        layer="product")

    tele, lane, product = apply_emission_taxonomy(
        [], [transactions, promo], [], [], routes,
        adjudicator=_flip_all,
    )
    names = {p.name for p in product}
    assert "transactions" in names, "product-route PF flipped into the lane"
    assert transactions.surface_scope == "product"
    assert tele["adjudicator"]["route_guarded"] == 1
    assert tele["adjudicator"]["journey_guarded"] == 0
    # Control with no product route was NOT guarded — it flipped to the lane.
    assert "promo" not in names
    assert [e["name"] for e in lane] == ["promo"]
