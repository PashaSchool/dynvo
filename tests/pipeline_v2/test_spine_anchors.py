"""Product-Spine Wave 2b — anchor-candidate builder tests.

Covers the FIVE calibration traps (each shipped as a live bug in some
prior engine iteration — see calib-report F-traps, 2026-07-06) plus the
per-source construction rules and the hub-family amendment grain.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.spine_anchors import (
    SpineAnchor,
    build_spine_anchors,
    load_spine_vocab,
    normalize_anchor_key,
    owned_paths_of,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def dev(name: str, paths: list[str], **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ctx_of(workspaces=None, tracked=None) -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path("."), monorepo=bool(workspaces),
    )


def by_id(anchors: list[SpineAnchor]) -> dict[str, SpineAnchor]:
    return {a.canonical_id: a for a in anchors}


# ── Trap 2 — singularizer guards (js/us/is/os/ss) ────────────────────────


def test_trap_singularizer_guards():
    assert normalize_anchor_key("nextjs") == "nextjs"      # js guard
    assert normalize_anchor_key("status") == "status"      # us guard
    assert normalize_anchor_key("analysis") == "analysis"  # is guard
    assert normalize_anchor_key("macos") == "macos"        # os guard
    assert normalize_anchor_key("address") == "address"    # ss guard
    assert normalize_anchor_key("cases") == "case"
    assert normalize_anchor_key("WidgetQuery") == "widget-query"
    assert normalize_anchor_key("context_items") == "context-item"


# ── Trap 1 — structural-vocabulary stoplist for schema matching ──────────


def test_trap_schema_structural_stoplist():
    """A schema model named ``apps``/``page`` must NEVER become a
    name-match anchor — it would claim the framework tree (measured:
    drizzle table `apps` swallowed midday at share 1.0)."""
    devs = [
        dev("d1", ["apps/dashboard/src/app/(public)/i/[token]/page.tsx"]),
        dev("d2", ["pages/settings.tsx"]),
    ]
    schema_cands = [
        SimpleNamespace(name="apps"),   # framework vocabulary → banned
        SimpleNamespace(name="page"),   # framework vocabulary → banned
        SimpleNamespace(name="invoice"),  # real domain noun → allowed
    ]
    anchors = build_spine_anchors(
        devs, [], ctx_of(), extractor_signals={"schema": schema_cands})
    keys = {a.canonical_id for a in anchors}
    assert not any(c.startswith("schema:app") for c in keys)
    assert not any(c.startswith("schema:page") for c in keys)


# ── Trap 3 — workspace classes: only example excluded; tool included ─────


def test_trap_workspace_tool_included_example_excluded():
    ws = [
        SimpleNamespace(name="cli", path="packages/cli", stack="ts"),
        SimpleNamespace(name="demo", path="examples/demo", stack="ts"),
        SimpleNamespace(name="web", path="apps/web", stack="ts"),
    ]
    devs = [dev("cli", ["packages/cli/src/index.ts"])]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of(ws)))
    # published-CLI doctrine: a tool-class package is a REAL product
    # surface — present and mint-eligible (ws-pkg).
    assert anchors["ws:packages/cli"].source == "ws-pkg"
    # examples are the ONLY excluded class.
    assert "ws:examples/demo" not in anchors
    # apps/* are shells (never mint) but exist for lineage.
    assert anchors["ws:apps/web"].source == "ws-app"
    assert anchors["ws:apps/web"].shell


def test_workspace_structural_unit_is_shell():
    """A top-level ``frontend`` unit is a deployment shell even when the
    workspace list calls it a package (fastapi-template smoke)."""
    ws = [SimpleNamespace(name="frontend", path="frontend", stack="ts")]
    devs = [dev("f", ["frontend/src/routes/login.tsx"])]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of(ws)))
    assert anchors["ws:frontend"].source == "ws-app"


# ── Trap 4 — basename collisions keep full-path identity ─────────────────


def test_trap_basename_collision_no_merge():
    ws = [
        SimpleNamespace(name="studio", path="apps/studio", stack="ts"),
        SimpleNamespace(name="studio-e2e", path="e2e/studio", stack="ts"),
    ]
    devs = [
        dev("a", ["apps/studio/pages/x.tsx"]),
        dev("b", ["e2e/studio/spec.ts"]),
    ]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of(ws)))
    assert "ws:apps/studio" in anchors
    assert "ws:e2e/studio" in anchors  # distinct identity, no merge
    a, b = anchors["ws:apps/studio"], anchors["ws:e2e/studio"]
    assert not a.matches("e2e/studio/spec.ts")
    assert not b.matches("apps/studio/pages/x.tsx")


# ── Trap 5 — route mega-segments need collection-descend ─────────────────


def test_trap_collection_descend_carves_mega_segment():
    """papermark /api/teams/[teamId]/* = 168 routes — without descend the
    route anchor goes coarse. The chain must carve ``documents``."""
    routes = [
        {"pattern": "/api/teams/:teamId/documents/:docId", "method": "GET",
         "file": "pages/api/teams/[teamId]/documents/[docId]/index.ts"},
        {"pattern": "/api/teams/:teamId/billing", "method": "GET",
         "file": "pages/api/teams/[teamId]/billing/index.ts"},
    ]
    devs = [dev("d", [r["file"] for r in routes])]
    anchors = build_spine_anchors(devs, routes, ctx_of())
    ids = {a.canonical_id for a in anchors}
    assert "route:pages/api/teams" in ids
    assert "route:pages/api/teams/[teamId]/documents" in ids  # descend d1
    assert "route:pages/api/teams/[teamId]/billing" in ids


# ── Version-dir + single-letter key classes ──────────────────────────────


def test_version_dir_transparent_when_deeper_and_barred_when_leaf():
    routes = [
        {"pattern": "/api/v1/items", "method": "GET",
         "file": "backend/app/api/routes/items.py"},
        {"pattern": "/v1", "method": "PAGE",
         "file": "apps/docs/app/guides/functions/v1/page.tsx"},
    ]
    devs = [dev("d", [r["file"] for r in routes])]
    anchors = by_id(build_spine_anchors(devs, routes, ctx_of()))
    # v1 transparent when deeper segments exist → key is `item`.
    assert "route:item" in anchors
    assert anchors["route:item"].barred is None
    # a trailing version segment IS the key → version_dir bar.
    v1 = [a for a in anchors.values() if a.key == "v1"]
    assert v1 and v1[0].barred == "version_dir"


def test_single_letter_route_keys_are_barred():
    routes = [
        {"pattern": "/i/:token", "method": "PAGE",
         "file": "apps/dashboard/src/app/[locale]/(public)/i/[token]/page.tsx"},
    ]
    devs = [dev("i", [routes[0]["file"]])]
    anchors = build_spine_anchors(devs, routes, ctx_of())
    i_anchor = [a for a in anchors if a.key == "i"]
    assert i_anchor and i_anchor[0].barred == "single_letter"
    # …and the prefix sits at the ROUTE-TREE (public)/i dir, never the
    # workspace root (the phantom mega-anchor trap, route side).
    assert i_anchor[0].prefixes == (
        "apps/dashboard/src/app/[locale]/(public)/i",)


def test_group_and_param_dirs_never_locate_the_key():
    """midday: the ``(app)`` group dir normalized equal to the ``/apps``
    key and truncated the prefix — groups/params are URL-invisible."""
    routes = [
        {"pattern": "/apps", "method": "PAGE",
         "file": "apps/dashboard/src/app/[locale]/(app)/apps/page.tsx"},
    ]
    devs = [dev("apps", [routes[0]["file"]])]
    anchors = build_spine_anchors(devs, routes, ctx_of())
    apps = [a for a in anchors if a.source == "route" and a.key == "app"]
    assert apps
    assert apps[0].prefixes == (
        "apps/dashboard/src/app/[locale]/(app)/apps",)


def test_param_segments_transparent_in_all_router_dialects():
    """W2b.1 fix (c2), typebot `$slug` x30: TanStack/Remix `$param`,
    Django/Flask `<int:id>` and `*splat` segments are URL machinery —
    never a chain key, never an anchor. The dir key is the collection
    segment (`blog`), and a param-keyed leaf can never mint."""
    routes = [
        {"pattern": "/_layout/blog/$slug", "method": "PAGE",
         "file": "apps/landing-page/src/routes/_layout/blog/$slug.tsx"},
        {"pattern": "/_layout/blog", "method": "PAGE",
         "file": "apps/landing-page/src/routes/_layout/blog/index.tsx"},
        {"pattern": "/articles/<int:pk>", "method": "GET",
         "file": "backend/articles/urls.py"},
        {"pattern": "/files/*splat", "method": "PAGE",
         "file": "app/routes/files.tsx"},
    ]
    devs = [dev("d", sorted({r["file"] for r in routes}))]
    anchors = build_spine_anchors(devs, routes, ctx_of())
    keys = {a.key for a in anchors if a.source == "route"}
    assert "slug" not in keys, keys          # $slug never a key
    assert not any(k.startswith(("pk", "int")) for k in keys), keys
    assert "splat" not in keys, keys
    blog = [a for a in anchors if a.source == "route" and a.key == "blog"]
    assert blog and blog[0].prefixes == (
        "apps/landing-page/src/routes/_layout/blog",)


def test_param_named_raw_segment_is_barred():
    """Defense-in-depth: a dialect the transparency regex misses still
    hits the param_leaf mint bar via the RAW segment."""
    import re as _re

    from faultline.pipeline_v2.spine_anchors import _bar_reason

    vre = _re.compile(r"^v\d+$")
    assert _bar_reason("slug", vre, raw_seg="$slug") == "param_leaf"
    assert _bar_reason("id", vre, raw_seg="[id]") == "param_leaf"
    assert _bar_reason("id", vre, raw_seg="{id}") == "param_leaf"
    assert _bar_reason("id", vre, raw_seg=":id") == "param_leaf"
    assert _bar_reason("pk", vre, raw_seg="<int:pk>") == "param_leaf"
    assert _bar_reason("blog", vre, raw_seg="blog") is None


def test_pages_api_routes_classify_api_despite_page_method():
    """W2b.1 fix (d1), supabase get-utc-time class: the Pages-Router
    extractor stamps method=PAGE on pages/api/* files; the URL pattern
    (/api/...) is the structural truth — such routes are API surface and
    carry NO page evidence."""
    routes = [
        {"pattern": "/api/get-utc-time", "method": "PAGE",
         "file": "apps/studio/pages/api/get-utc-time.ts"},
        {"pattern": "/api/trpc/edge/:trpc", "method": "PAGE",
         "file": "apps/web/src/app/api/trpc/edge/[trpc]/route.ts"},
        {"pattern": "/maintenance", "method": "PAGE",
         "file": "apps/studio/pages/maintenance.tsx"},
    ]
    devs = [dev("d", sorted({r["file"] for r in routes}))]
    anchors = build_spine_anchors(devs, routes, ctx_of())
    gut = [a for a in anchors if a.key == "get-utc-time"]
    assert gut, [a.canonical_id for a in anchors]
    assert not gut[0].page_route_files
    assert gut[0].api_route_files == frozenset(
        {"apps/studio/pages/api/get-utc-time.ts"})
    # a REAL page keeps page evidence
    mt = [a for a in anchors if a.key == "maintenance"]
    assert mt and mt[0].page_route_files


def test_central_router_files_anchor_the_handler_file():
    """FastAPI class: no routing-root run in the path → the handler FILE
    is the subtree (never a phantom ``routers``-dir anchor)."""
    routes = [
        {"pattern": "/context-items", "method": "GET",
         "file": "backend/routers/context_items.py"},
        {"pattern": "/context-items/:id", "method": "PUT",
         "file": "backend/routers/context_items.py"},
    ]
    devs = [dev("api-context-items", ["backend/routers/context_items.py"])]
    anchors = by_id(build_spine_anchors(devs, routes, ctx_of()))
    assert "route:context-item" in anchors
    assert anchors["route:context-item"].files == frozenset(
        {"backend/routers/context_items.py"})
    assert not any(a.key == "router" for a in anchors.values())


# ── W2b.1 fix (b) — python-package domain source ─────────────────────────


def _onyx_shape_tracked() -> list[str]:
    return [
        "backend/onyx/__init__.py",
        "backend/onyx/chat/__init__.py",
        "backend/onyx/chat/service.py",
        "backend/onyx/chat/models.py",
        "backend/onyx/connectors/__init__.py",
        "backend/onyx/connectors/registry.py",
        "backend/onyx/indexing/__init__.py",
        "backend/onyx/indexing/pipeline.py",
        "backend/onyx/utils/__init__.py",
        "backend/onyx/utils/text.py",
        "backend/onyx/server/__init__.py",
        "backend/onyx/server/documents/__init__.py",
        "backend/onyx/server/documents/api.py",
        "backend/onyx/server/manage/__init__.py",
        "backend/onyx/server/manage/api.py",
        "backend/onyx/server/settings/__init__.py",
        "backend/onyx/server/settings/api.py",
        "backend/ee/__init__.py",
        "backend/ee/onyx/__init__.py",
        "backend/ee/onyx/analytics/__init__.py",
        "backend/ee/onyx/analytics/api.py",
        "backend/ee/onyx/hooks/__init__.py",
        "backend/ee/onyx/hooks/api.py",
        "backend/ee/onyx/reporting/__init__.py",
        "backend/ee/onyx/reporting/api.py",
        "backend/tests/__init__.py",
        "backend/tests/unit/__init__.py",
        "backend/tests/unit/test_chat.py",
    ]


def test_pypkg_domain_source_python_monolith():
    """The onyx class: backend/onyx/<domain> python packages are domain
    anchors; stoplisted children (server/utils) DESCEND to their own
    domains; namespace echoes (backend/ee/onyx mirroring backend/onyx)
    descend; test trees never participate."""
    tracked = _onyx_shape_tracked()
    owned = [p for p in tracked if not p.startswith("backend/tests/")]
    devs = [dev("d", owned)]
    anchors = build_spine_anchors(
        devs, [], ctx_of(tracked=tracked))
    ids = by_id(anchors)
    assert "pypkg:backend/onyx/chat" in ids
    assert ids["pypkg:backend/onyx/chat"].source == "pypkg"
    assert "pypkg:backend/onyx/connectors" in ids
    # stoplisted `server` descends → its children are the domains
    assert "pypkg:backend/onyx/server/documents" in ids
    assert not any(c.endswith(":backend/onyx/server") for c in ids)
    # stoplisted `utils` never a domain (and has no pkg children)
    assert not any("onyx/utils" in c for c in ids if c.startswith("pypkg:"))
    # namespace echo backend/ee/onyx descends to ITS domains
    assert "pypkg:backend/ee/onyx/analytics" in ids
    assert "pypkg:backend/ee/onyx" not in ids
    # guard segments: no anchor under tests/
    assert not any("tests" in c for c in ids if c.startswith("pypkg:"))


def test_pypkg_services_container_children_stay_svc_class():
    """Soc0 protection: a `services/` container discovered through the
    package walk emits svc-class (LINEAGE-ONLY) children — widget_query
    must not become mint-eligible through the pypkg source."""
    tracked = [
        "backend/services/__init__.py",
        "backend/services/edr/__init__.py",
        "backend/services/edr/cortex.py",
        "backend/services/widget_query/__init__.py",
        "backend/services/widget_query/base.py",
        "backend/services/mitre_packs/__init__.py",
        "backend/services/mitre_packs/adopt.py",
    ]
    devs = [dev("d", [p for p in tracked if p.endswith(".py")])]
    anchors = build_spine_anchors(devs, [], ctx_of(tracked=tracked))
    wq = [a for a in anchors if "widget_query" in a.canonical_id]
    assert wq and all(a.source == "svc" for a in wq), [
        (a.canonical_id, a.source) for a in wq]
    assert not any(a.source == "pypkg" and "widget_query" in a.canonical_id
                   for a in anchors)


# ── Cross-source key merge ───────────────────────────────────────────────


def test_key_merge_route_plus_schema():
    routes = [{"pattern": "/cases", "method": "PAGE",
               "file": "app/cases/page.tsx"}]
    devs = [
        dev("cases", ["app/cases/page.tsx", "components/case/list.tsx"]),
    ]
    anchors = build_spine_anchors(
        devs, routes, ctx_of(),
        extractor_signals={"schema": [SimpleNamespace(name="case")]})
    merged = [a for a in anchors if a.key == "case"]
    assert len(merged) == 1
    assert merged[0].sources == frozenset({"route", "schema"})
    # schema matched the components/case dir into the same anchor
    assert merged[0].matches("components/case/list.tsx")
    assert merged[0].matches("app/cases/page.tsx")


# ── Hub families (operator amendment: per-vendor grain) ──────────────────


def _edr_devs() -> list[Feature]:
    plumbing = dev("edr", [
        "backend/services/edr/__init__.py",
        "backend/services/edr/base.py",
        "backend/services/edr/factory.py",
        "backend/services/edr/normalizer.py",
    ])
    kids = [
        dev("edr-claroty", ["backend/services/edr/claroty_xdome.py"]),
        dev("edr-cortex", ["backend/services/edr/cortex.py",
                           "backend/services/edr/schema/cortex_baseline.py"]),
        dev("edr-crowdstrike", ["backend/services/edr/crowdstrike.py"]),
        dev("edr-defender", ["backend/services/edr/defender.py"]),
        dev("edr-sentinelone", ["backend/services/edr/sentinelone.py"]),
    ]
    return [plumbing, *kids]


def test_hub_family_dev_arm_edr():
    """Soc0 edr (amendment case 5a): 5 vendor children + the core."""
    anchors = by_id(build_spine_anchors(_edr_devs(), [], ctx_of()))
    for v in ("claroty", "cortex", "crowdstrike", "defender", "sentinelone"):
        cid = f"hub:backend/services/edr/{v}"
        assert cid in anchors, f"missing vendor child {v}"
        assert anchors[cid].source == "hub-vendor"
    core = anchors["hub:backend/services/edr"]
    assert core.source == "hub-core"
    # core excludes every child file → plumbing only
    assert core.matches("backend/services/edr/base.py")
    assert not core.matches("backend/services/edr/cortex.py")


def test_hub_family_lexicon_arm_banking_providers_incl_nonvocab_vendor():
    """midday banking (amendment case 5b): 4 provider dirs mint as
    children INCLUDING ``teller`` (not in the public vendor vocabulary);
    the lexicon parent never mints a '<providers> Core'."""
    devs = [
        dev("gocardless", [f"packages/banking/src/providers/gocardless/{f}"
                           for f in ("gocardless-api.ts", "transform.ts")]),
        dev("plaid", [f"packages/banking/src/providers/plaid/{f}"
                      for f in ("plaid-api.ts", "transform.ts")]),
        dev("enablebanking", [
            "packages/banking/src/providers/enablebanking/enablebanking-api.ts"]),
        dev("teller", ["packages/banking/src/providers/teller/teller-api.ts"]),
        dev("banking", ["packages/banking/src/index.ts"]),
    ]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of()))
    for v in ("gocardless", "plaid", "enablebanking", "teller"):
        assert f"hub:packages/banking/src/providers/{v}" in anchors, v
    assert "hub:packages/banking/src/providers" not in anchors  # no core


def test_hub_family_generic_parent_children_only():
    """backend/routers holding slack/github/teams routers: vendor
    children exist (flow-gated at mint time) but NO 'Routers Core'."""
    devs = [
        dev("api-slack", ["backend/routers/slack.py"]),
        dev("api-github", ["backend/routers/github.py"]),
        dev("api-teams", ["backend/routers/teams.py"]),
        dev("api-context-items", ["backend/routers/context_items.py"]),
    ]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of()))
    assert "hub:backend/routers/slack" in anchors
    assert anchors["hub:backend/routers/slack"].hub_parent_generic
    assert "hub:backend/routers" not in anchors  # core suppressed


def test_hub_family_needs_three_vendor_pure_devs():
    """widget-query (operator case 1): ONE dev owning base + 5 vendor
    adapters is NOT a family — no vendor-pure sibling devs exist, so no
    hub anchors mint; the dir stays a service-dir (lineage-only)."""
    devs = [dev("widget-query", [
        "backend/services/widget_query/__init__.py",
        "backend/services/widget_query/base.py",
        "backend/services/widget_query/elasticsearch_adapter.py",
        "backend/services/widget_query/zscaler_adapter.py",
        "backend/services/widget_query/entra_adapter.py",
        "backend/services/widget_query/sentinel_adapter.py",
    ])]
    anchors = build_spine_anchors(devs, [], ctx_of())
    assert not any(a.source.startswith("hub") for a in anchors)
    svc = [a for a in anchors if a.source == "svc"]
    assert svc and svc[0].canonical_id == "svc:backend/services/widget_query"


def test_hub_family_typebot_blocks_src_passthrough():
    """typebot blocks (amendment case 5c): the family dir is
    ``…/integrations/src`` — ``src`` is a passthrough; ``integrations``
    (lexicon) classes it: children mint on code, core suppressed."""
    devs = [
        dev("chatwoot", [
            "packages/blocks/integrations/src/chatwoot/constants.ts",
            "packages/blocks/integrations/src/chatwoot/schema.ts"]),
        dev("google-analytics", [
            "packages/blocks/integrations/src/googleAnalytics/schema.ts"]),
        dev("google-sheets", [
            "packages/blocks/integrations/src/googleSheets/schema.ts"]),
        dev("zapier", [
            "packages/blocks/integrations/src/zapier/schema.ts"]),
    ]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of()))
    kid = "hub:packages/blocks/integrations/src/chatwoot"
    assert kid in anchors
    assert not anchors[kid].hub_parent_generic  # lexicon, not generic
    assert "hub:packages/blocks/integrations/src" not in anchors


# ── Feature-dirs + service-dirs (rider R1 sources) ───────────────────────


def test_feature_dir_and_service_dir_sources():
    devs = [
        dev("anomalies", ["frontend/src/features/anomalies/Page.tsx",
                          "frontend/src/features/anomalies/api.ts"]),
        dev("billing-ee", ["ee/features/billing/index.ts"]),
        dev("iot", ["backend/services/iot_ot/base.py"]),
    ]
    anchors = by_id(build_spine_anchors(devs, [], ctx_of()))
    assert anchors["fdir:frontend/src/features/anomalies"].source == "fdir"
    assert anchors["fdir:ee/features/billing"].source == "fdir"
    assert anchors["svc:backend/services/iot_ot"].source == "svc"


# ── Vocab drift guard (house pattern) ────────────────────────────────────


def test_vocab_yaml_authoring_copy_is_byte_identical():
    packaged = (
        Path(__file__).resolve().parents[2]
        / "faultline" / "pipeline_v2" / "data" / "spine-anchor-vocab.yaml"
    )
    authoring = (
        Path(__file__).resolve().parents[2] / "eval" / "spine-anchor-vocab.yaml"
    )
    assert packaged.read_bytes() == authoring.read_bytes(), (
        "data/spine-anchor-vocab.yaml drifted from eval/spine-anchor-vocab.yaml"
    )


def test_vocab_has_required_sections():
    v = load_spine_vocab()
    for key in ("structural_stoplist", "route_transparent_segments",
                "version_segment_pattern", "feature_dir_containers",
                "service_dir_containers", "workspace_shell_roots",
                "workspace_excluded_segments", "unit_manifest_files",
                "hub_plumbing_segments", "code_extensions"):
        assert v.get(key), key


def test_owned_paths_prefers_primary_member_files():
    f = dev("x", ["a.py"])
    f.paths = ["a.py", "reach/expanded.py"]  # expansion noise
    assert owned_paths_of(f) == ["a.py"]
