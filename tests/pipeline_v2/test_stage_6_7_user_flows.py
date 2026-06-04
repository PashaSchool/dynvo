"""Tests for Stage 6.7 — deterministic Flow → User-Flow rollup.

Covers each algorithm stage in isolation (dedup, intent table, domain
grounding, clustering, enrichment, emit) plus the typed engine adapter
and its additive-only guarantee. No LLM, no network.
"""

from __future__ import annotations

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.stage_6_7_user_flows import (
    INTENT,
    cluster_user_flows,
    run_user_flow_rollup,
)


def _flow(name, **kw):
    """Minimal flow dict for the dict-based clusterer."""
    base = {
        "name": name,
        "uuid": kw.pop("uuid", name + "-uuid"),
        "entry_point_file": None,
        "paths": [],
        "primary_feature": None,
        "secondary_features": [],
        "test_files": [],
        "coverage_pct": None,
    }
    base.update(kw)
    return base


# ── Stage A — dedup by canonical name ───────────────────────────────────


def test_dedup_by_name_first_seen_wins():
    scan = {
        "flows": [
            _flow("create-detector-flow", uuid="a", paths=["backend/routers/detectors.py"]),
            _flow("create-detector-flow", uuid="b", paths=["other.py"]),
            _flow("list-detector-flow", uuid="c", paths=["backend/routers/detectors.py"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    assert r["total_flows"] == 3
    assert r["unique_flows"] == 2
    assert r["dedup_dropped"] == 1
    # first-seen "a" wins; its uuid is the member, not "b".
    author = next(u for u in r["user_flows"] if u["intent"] == "author")
    assert author["member_flow_ids"] == ["a"]


def test_dedup_count_surfaced():
    scan = {"flows": [_flow("x-flow"), _flow("x-flow", uuid="x2")], "developer_features": []}
    r = cluster_user_flows(scan)
    assert r["dedup_dropped"] == 1
    assert r["unique_flows"] == 1


# ── Stage B — intent table ──────────────────────────────────────────────


def test_intent_table_is_fixed_semantic_map():
    # representative verbs from each class
    assert INTENT["create"] == "author"
    assert INTENT["update"] == "author"
    assert INTENT["list"] == "browse"
    assert INTENT["view"] == "browse"
    assert INTENT["approve"] == "lifecycle"
    assert INTENT["promote"] == "lifecycle"
    assert INTENT["run"] == "execute"
    assert INTENT["send"] == "execute"
    assert INTENT["delete"] == "manage"
    assert INTENT["reset"] == "manage"
    assert INTENT["bulk"] == "bulk"
    assert INTENT["export"] == "export"
    assert INTENT["download"] == "export"


def test_unknown_verb_falls_to_other():
    scan = {"flows": [_flow("frobnicate-widget-flow")], "developer_features": []}
    r = cluster_user_flows(scan)
    assert r["user_flows"][0]["intent"] == "other"


# ── Stage B — domain grounding (code, never spec) ───────────────────────


def test_domain_from_router_file_first():
    scan = {
        "flows": [_flow("create-detector-flow",
                        paths=["backend/routers/detectors.py", "backend/models/x.py"])],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    # singularized router resource → CODE-GRAIN domain (not product_feature_id).
    # product_feature_id is the Layer-2 marketing link, resolved separately
    # (None here — no dev-feature carries one).
    assert r["user_flows"][0]["domain"] == "detector"
    assert r["user_flows"][0]["product_feature_id"] is None


def test_router_init_is_skipped():
    scan = {
        "flows": [_flow("view-thing-flow",
                        paths=["pkg/routers/__init__.py", "app/things/svc.py"])],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    # __init__ skipped -> falls through to source folder "things" (domain)
    assert r["user_flows"][0]["domain"] == "thing"


def test_domain_from_product_feature_when_no_router():
    # When the only domain signal is product_feature_id, it is NORMALIZED to
    # a code token for the domain — and ALSO preserved verbatim as the
    # Layer-2 grouping link (product_feature_id field). The two are decoupled.
    scan = {
        "flows": [_flow("view-thing-flow", primary_feature="dashboard-widgets",
                        paths=["frontend/components/Widget.tsx"])],
        "developer_features": [
            {"name": "dashboard-widgets", "product_feature_id": "analytics"},
        ],
    }
    r = cluster_user_flows(scan)
    uf = r["user_flows"][0]
    assert uf["domain"] == "analytic"            # singularized code token
    assert uf["product_feature_id"] == "analytics"  # marketing link preserved


def test_marketing_string_pfid_does_not_become_domain():
    # REGRESSION GUARD (the core of this fix): a long Layer-2 MARKETING
    # label must NOT be used verbatim as the cluster-key domain. The domain
    # must collapse to a short code-grain head-noun token, while the
    # product_feature_id field keeps the full marketing label as the link.
    label = "organizations-&-multi-team-management"
    scan = {
        "flows": [_flow("create-org-flow", primary_feature="org-svc",
                        paths=["frontend/components/Org.tsx"])],
        "developer_features": [
            {"name": "org-svc", "product_feature_id": label},
        ],
    }
    r = cluster_user_flows(scan)
    uf = r["user_flows"][0]
    # domain is a SHORT code token, never the marketing string.
    assert uf["domain"] == "organization"
    assert uf["domain"] != label
    assert "&" not in (uf["domain"] or "")
    assert " " not in (uf["domain"] or "")
    # the marketing label survives as the grouping LINK.
    assert uf["product_feature_id"] == label


def test_domain_none_when_ungroundable():
    scan = {"flows": [_flow("view-thing-flow", paths=["README.md"])],
            "developer_features": []}
    r = cluster_user_flows(scan)
    assert r["user_flows"][0]["domain"] is None
    assert r["user_flows"][0]["product_feature_id"] is None


# ── Stage C — cluster by (domain, intent) ───────────────────────────────


def test_singleton_resources_fold_into_domain_intent_journey():
    # SINGLETON resource-clusters within the same (domain, intent) fold
    # into ONE journey UF: a resource that appears exactly once is grain
    # noise, not a recurring user journey. "create-detector" and
    # "create-rule" each appear once under the same (detector-domain,
    # author) → one "Create & edit" journey. "list-detector" (browse) is
    # a different intent → its own UF.
    #
    # This grain rule is corpus-validated (formbricks/infisical/documenso/
    # dub/openstatus) where per-resource-singleton grain over-split the
    # rollup 3-6x past product truth. It is NOT tuned to any spec count —
    # it is the structural "no recurring journey ⇒ fold" rule.
    scan = {
        "flows": [
            _flow("create-detector-flow", paths=["backend/routers/detectors.py"]),
            _flow("create-rule-flow", paths=["backend/routers/detectors.py"]),
            _flow("list-detector-flow", paths=["backend/routers/detectors.py"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    # 2 UF: (detector, author) folding both author singletons, and
    # (detector, browse).
    assert len(r["user_flows"]) == 2
    intents = sorted(u["intent"] for u in r["user_flows"])
    assert intents == ["author", "browse"]
    author = next(u for u in r["user_flows"] if u["intent"] == "author")
    assert author["member_count"] == 2  # both create-* folded


def test_distinct_domain_journeys_stay_distinct():
    # Distinct DOMAINS render distinct names ("Create & edit detectors" vs
    # "Create & edit rules") and so are never merged — the name-collision
    # merge only folds clusters that already render to the SAME name.
    scan = {
        "flows": [
            _flow("create-detector-flow", paths=["backend/routers/detectors.py"]),
            _flow("update-detector-flow", paths=["backend/routers/detectors.py"]),
            _flow("create-rule-flow", paths=["backend/routers/rules.py"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    by_dom = {u["domain"]: u for u in r["user_flows"]}
    assert by_dom["detector"]["member_count"] == 2  # create+update detector
    assert by_dom["rule"]["member_count"] == 1
    names = sorted(u["name"] for u in r["user_flows"])
    assert names == ["Create & edit detectors", "Create & edit rules"]


def test_same_name_multimember_clusters_merge_into_one_uf():
    # REGRESSION (cal.com duplicate-User-Flow bug): the UF NAME is derived
    # from (domain, intent) when domain is present, so multiple multi-member
    # (domain, resource, intent) clusters sharing the same (domain, intent)
    # emit the SAME human name ("Browse & filter organizations" ×11 on
    # cal.com). _merge_singleton_noise only folds 1-member clusters, leaving
    # the multi-member collisions. _merge_same_name_clusters closes that gap:
    # two UFs a user cannot tell apart (identical name + domain) are one
    # journey at product grain. Distinct INTENTS render distinct names and
    # are never merged.
    #
    # Fixture: one domain (organization, via pfid), three browse resources
    # each with 2 members (so none is a singleton — _merge_singleton_noise
    # cannot touch them) + one author resource. All three browse clusters
    # render to "Browse & filter organizations" and must collapse to ONE.
    devs = [{"name": "org-feat", "product_feature_id": "organizations-&-multi-team-management"}]
    scan = {
        "flows": [
            _flow("list-booking-flow", primary_feature="org-feat"),
            _flow("view-booking-flow", primary_feature="org-feat"),
            _flow("list-calendar-flow", primary_feature="org-feat"),
            _flow("view-calendar-flow", primary_feature="org-feat"),
            _flow("list-webhook-flow", primary_feature="org-feat"),
            _flow("view-webhook-flow", primary_feature="org-feat"),
            _flow("create-booking-flow", primary_feature="org-feat"),
            _flow("update-booking-flow", primary_feature="org-feat"),
        ],
        "developer_features": devs,
    }
    r = cluster_user_flows(scan)
    names = [u["name"] for u in r["user_flows"]]
    # exactly one browse UF and one author UF — no name appears twice
    assert names.count("Browse & filter organizations") == 1
    assert names.count("Create & edit organizations") == 1
    assert len(names) == len(set(names)), f"duplicate UF names: {names}"
    browse = next(u for u in r["user_flows"] if u["intent"] == "browse")
    # all 6 browse members (3 resources × 2) folded into the one browse UF
    assert browse["member_count"] == 6


def test_same_name_merge_is_recall_safe_members_conserved():
    # The merge only re-buckets members — it never drops a flow. Total member
    # count and the set of member_flow_ids are conserved (recall-safe).
    devs = [{"name": "org-feat", "product_feature_id": "organizations-&-multi-team-management"}]
    flows = [
        _flow("list-booking-flow", primary_feature="org-feat"),
        _flow("view-booking-flow", primary_feature="org-feat"),
        _flow("list-calendar-flow", primary_feature="org-feat"),
        _flow("view-calendar-flow", primary_feature="org-feat"),
    ]
    scan = {"flows": flows, "developer_features": devs}
    r = cluster_user_flows(scan)
    total_members = sum(u["member_count"] for u in r["user_flows"])
    assert total_members == 4
    all_ids = {mid for u in r["user_flows"] for mid in u["member_flow_ids"]}
    assert all_ids == {f["uuid"] for f in flows}
    # every flow got a UF assignment
    assert len(r["flow_to_uf"]) == 4


def test_same_name_merge_unions_cross_links():
    # When two same-name clusters merge, their cross_links union. Both browse
    # resources (booking, calendar) share the org domain via pfid (no router
    # path, so Signal 3 head-noun "organization" is the domain for both →
    # identical "Browse & filter organizations" name → collision). One cluster
    # carries a billing cross-link, the other an email cross-link; the merged
    # browse UF must carry BOTH (own pfid excluded).
    devs = [
        {"name": "org-feat", "product_feature_id": "organizations-&-multi-team-management"},
        {"name": "billing", "product_feature_id": "billing-&-subscriptions"},
        {"name": "email", "product_feature_id": "email-notifications"},
    ]
    scan = {
        "flows": [
            _flow("list-booking-flow", primary_feature="org-feat",
                  secondary_features=["billing"]),
            _flow("view-booking-flow", primary_feature="org-feat"),
            _flow("list-calendar-flow", primary_feature="org-feat",
                  secondary_features=["email"]),
            _flow("view-calendar-flow", primary_feature="org-feat"),
        ],
        "developer_features": devs,
    }
    r = cluster_user_flows(scan)
    browse = [u for u in r["user_flows"] if u["intent"] == "browse"]
    assert len(browse) == 1, f"expected 1 merged browse UF, got {len(browse)}"
    b = browse[0]
    assert b["member_count"] == 4
    assert "billing-&-subscriptions" in b["cross_links"]
    assert "email-notifications" in b["cross_links"]
    # own product feature is never a cross-link
    assert "organizations-&-multi-team-management" not in b["cross_links"]


def test_same_name_merge_tiny_medium_large_scale_invariant():
    # rule-no-magic-tuning: same structural behavior at 3 scales. N browse
    # resources (each 2 members) under one domain always collapse to ONE
    # browse UF, regardless of N (tiny=2, medium=10, large=60).
    def build(n):
        devs = [{"name": "org-feat",
                 "product_feature_id": "organizations-&-multi-team-management"}]
        flows = []
        for i in range(n):
            flows.append(_flow(f"list-res{i}-flow", uuid=f"l{i}",
                               primary_feature="org-feat"))
            flows.append(_flow(f"view-res{i}-flow", uuid=f"v{i}",
                               primary_feature="org-feat"))
        return {"flows": flows, "developer_features": devs}

    for n in (2, 10, 60):
        r = cluster_user_flows(build(n))
        browse = [u for u in r["user_flows"] if u["intent"] == "browse"]
        assert len(browse) == 1, f"n={n}: expected 1 browse UF, got {len(browse)}"
        assert browse[0]["member_count"] == 2 * n


def test_none_domain_clusters_not_blindly_merged():
    # When domain is None the name falls back to the per-cluster resource, so
    # two None-domain clusters only collide when their resource labels are
    # already identical. Distinct resources keep distinct names → not merged
    # (conservative, per finding-pathset-merge-refuted).
    scan = {
        "flows": [
            _flow("frobnicate-alpha-flow"),  # unknown verb → other intent
            _flow("frobnicate-beta-flow"),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    # distinct resources (alpha, beta), domain None → distinct names → 2 UFs
    assert len(r["user_flows"]) == 2
    names = sorted(u["name"] for u in r["user_flows"])
    assert names == ["alpha", "beta"]


def test_uf_ids_deterministic_and_ordered():
    scan = {
        "flows": [
            _flow("list-zebra-flow", paths=["backend/routers/zebras.py"]),
            _flow("create-apple-flow", paths=["backend/routers/apples.py"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    # sorted by (str(domain), intent): apple < zebra
    assert [u["id"] for u in r["user_flows"]] == ["UF-001", "UF-002"]
    assert r["user_flows"][0]["domain"] == "apple"


def test_name_template_per_intent():
    scan = {
        "flows": [
            _flow("create-detector-flow", paths=["backend/routers/detectors.py"]),
            _flow("list-detector-flow", paths=["backend/routers/detectors.py"]),
            _flow("run-detector-flow", paths=["backend/routers/detectors.py"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    names = {u["intent"]: u["name"] for u in r["user_flows"]}
    assert names["author"] == "Create & edit detectors"
    assert names["browse"] == "Browse & filter detectors"
    assert names["execute"] == "Run detectors"


# ── Stage E — enrichment ────────────────────────────────────────────────


def test_enrichment_routes_crosslinks_tests_coverage():
    scan = {
        "flows": [
            _flow("create-detector-flow", primary_feature="detector-core",
                  paths=["backend/routers/detectors.py", "backend/models/d.py"],
                  secondary_features=["billing-svc"], test_files=["t.py"],
                  coverage_pct=80.0),
            _flow("update-detector-flow", primary_feature="detector-core",
                  paths=["backend/routers/detectors.py"],
                  secondary_features=["detector-core"],  # own pf -> excluded
                  coverage_pct=60.0),
        ],
        "developer_features": [
            {"name": "billing-svc", "product_feature_id": "billing"},
            {"name": "detector-core", "product_feature_id": "detector"},
        ],
    }
    r = cluster_user_flows(scan)
    uf = r["user_flows"][0]
    # domain is the router code token; product_feature_id is the voted
    # marketing link ("detector"); cross_links exclude that own pf.
    assert uf["domain"] == "detector"
    assert uf["product_feature_id"] == "detector"
    assert uf["routes"] == ["backend/routers/detectors.py"]  # deduped, router-only
    assert uf["cross_links"] == ["billing"]  # own product feature excluded
    assert uf["ac_draft_count"] == 1  # one member with test_files
    assert uf["coverage_pct"] == 70.0  # mean of 80 and 60


def test_coverage_none_when_no_members_have_coverage():
    # Use a real product noun ("invoices") so the domain is not a single-char
    # / widget / infra artifact dropped by the non-journey filters.
    scan = {"flows": [_flow("view-invoice-flow", paths=["backend/routers/invoices.py"])],
            "developer_features": []}
    r = cluster_user_flows(scan)
    assert r["user_flows"][0]["coverage_pct"] is None


# ── Adapter — typed objects, additive only ──────────────────────────────


def _typed_flow(name, **kw):
    return Flow(
        name=name, paths=kw.pop("paths", []), authors=[], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified="2026-06-02T00:00:00Z",
        health_score=100.0, uuid=kw.pop("uuid", name + "-u"), **kw,
    )


def test_adapter_returns_userflow_models_and_stamps_ids():
    flows = [
        _typed_flow("create-detector-flow", paths=["backend/routers/detectors.py"]),
        _typed_flow("list-detector-flow", paths=["backend/routers/detectors.py"]),
    ]
    features: list[Feature] = []
    ufs, tel = run_user_flow_rollup(flows, features)
    assert all(isinstance(u, UserFlow) for u in ufs)
    assert tel["user_flows"] == 2
    # every flow stamped with a user_flow_id pointing at a real UF
    uf_ids = {u.id for u in ufs}
    assert all(f.user_flow_id in uf_ids for f in flows)


def test_adapter_stamps_duplicate_rows_via_name_fallback():
    flows = [
        _typed_flow("create-detector-flow", uuid="a", paths=["backend/routers/detectors.py"]),
        _typed_flow("create-detector-flow", uuid="b", paths=["backend/routers/detectors.py"]),
    ]
    ufs, tel = run_user_flow_rollup(flows, [])
    assert tel["dedup_dropped"] == 1
    # both rows (incl. the deduped duplicate) carry the same id by name.
    assert flows[0].user_flow_id == flows[1].user_flow_id
    assert flows[0].user_flow_id is not None


# ── Non-journey UF filters (Filter A/B/C) ───────────────────────────────
#
# A User Flow is a product-grain JOURNEY. Shared UI primitives, per-connector
# plugin packages, and infra/DI/version artifacts are NOT journeys and must
# not seed a UF — while flows[] / developer_features[] stay fully intact.
# All fixtures are synthetic with neutral structural names (no real-repo
# paths) per rule-no-repo-specific-paths.


# Filter A — UI-primitive / design-system rendering infra is excluded.


def test_filter_a_ui_primitive_package_barrel_excluded():
    """A flow whose entry point is a design-system package barrel
    (``<pkg>/ui/components/form/index.ts``) does not seed a UF."""
    scan = {
        "flows": [
            _flow("input-text-flow", paths=["packages/ui/components/form/index.ts"],
                  entry_point_file="packages/ui/components/form/index.ts"),
            _flow("pick-color-flow", paths=["packages/ui/components/form/index.ts"],
                  entry_point_file="packages/ui/components/form/index.ts"),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_ui_primitive"] == 2
    assert r["user_flows"] == []
    # Layer 1 untouched.
    assert r["total_flows"] == 2


def test_filter_a_primitive_package_root_even_with_nonprimitive_file():
    """An ``atoms`` design-system PACKAGE is excluded even when the specific
    file is a hook (``platform/atoms/hooks/useAtomsContext.ts``) — the package
    root segment is the structural signal."""
    scan = {
        "flows": [
            _flow("use-atoms-context-flow",
                  entry_point_file="packages/platform/atoms/hooks/useAtomsContext.ts",
                  paths=["packages/platform/atoms/hooks/useAtomsContext.ts"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_ui_primitive"] == 1
    assert r["user_flows"] == []


def test_filter_a_does_not_drop_feature_with_nested_components_subfolder():
    """A real feature whose code lives under ``features/<domain>/components/``
    (primitive segment is NESTED, not the package root) MUST survive."""
    scan = {
        "flows": [
            _flow("pick-available-date-flow",
                  entry_point_file="packages/features/calendars/components/DatePicker.tsx",
                  paths=["packages/features/calendars/components/DatePicker.tsx"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_ui_primitive"] == 0
    assert len(r["user_flows"]) == 1


def test_filter_a_secondary_primitive_path_does_not_exclude():
    """A journey that merely IMPORTS a primitive as a SECONDARY path (its own
    entry point is a feature file) is not UI-primitive infra and survives."""
    scan = {
        "flows": [
            _flow("configure-event-payment-flow",
                  entry_point_file="packages/features/bookings/lib/payment.ts",
                  paths=["packages/features/bookings/lib/payment.ts",
                         "packages/ui/components/icon/index.ts"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_ui_primitive"] == 0
    assert len(r["user_flows"]) == 1


def test_filter_a_primitive_domain_token_excluded():
    """A flow whose resolved domain token is built only from widget words
    (``data_table`` → {data, table}) is excluded as a widget package."""
    scan = {
        "flows": [
            _flow("paginate-table-flow",
                  entry_point_file="packages/features/data-table/lib/parsers.ts",
                  paths=["packages/features/data-table/lib/parsers.ts"],
                  primary_feature="data-table"),
        ],
        "developer_features": [
            {"name": "data-table", "product_feature_id": "data-table-widget"},
        ],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_ui_primitive"] == 1
    assert r["user_flows"] == []


# Filter B — per-connector plugin sibling collapse.


def test_filter_b_collapses_many_sibling_connectors_into_integration():
    """Many sibling child dirs under a plugin root (``app-store/<connector>``)
    each contributing flows collapse into ONE integration journey domain."""
    flows = []
    for conn in ["alpha", "bravo", "charlie", "delta", "echo"]:
        flows.append(_flow(f"add-{conn}-integration-flow",
                           entry_point_file=f"packages/app-store/{conn}/api/index.ts",
                           paths=[f"packages/app-store/{conn}/api/index.ts"]))
    scan = {"flows": flows, "developer_features": []}
    r = cluster_user_flows(scan)
    assert "app-store" in r["uf_plugin_roots"]
    assert r["uf_plugin_collapsed"] == 5
    # All five connectors share the single "integration" domain.
    domains = {uf["domain"] for uf in r["user_flows"]}
    assert domains == {"integration"}


def test_filter_b_does_not_collapse_few_large_sibling_apps():
    """A monorepo ``apps/`` with only a couple of LARGE children (web, api),
    each owning many flows, is NOT a plugin root and is not collapsed."""
    flows = []
    for i in range(8):
        flows.append(_flow(f"web-action-{i}-flow",
                           entry_point_file="apps/web/src/page.tsx",
                           paths=["apps/web/src/page.tsx"]))
    for i in range(8):
        flows.append(_flow(f"api-action-{i}-flow",
                           entry_point_file="apps/api/src/route.ts",
                           paths=["apps/api/src/route.ts"]))
    scan = {"flows": flows, "developer_features": []}
    r = cluster_user_flows(scan)
    assert "apps" not in r["uf_plugin_roots"]
    assert r["uf_plugin_collapsed"] == 0


def test_filter_b_shared_plugin_root_helper_also_collapses():
    """Flows anchored in a plugin-root shared helper dir (``app-store/_utils``)
    fold to the integration domain once the root is detected."""
    flows = []
    for conn in ["alpha", "bravo", "charlie"]:
        flows.append(_flow(f"add-{conn}-integration-flow",
                           entry_point_file=f"packages/app-store/{conn}/api/index.ts",
                           paths=[f"packages/app-store/{conn}/api/index.ts"]))
    flows.append(_flow("install-app-helper-flow",
                       entry_point_file="packages/app-store/_utils/installation.ts",
                       paths=["packages/app-store/_utils/installation.ts"]))
    scan = {"flows": flows, "developer_features": []}
    r = cluster_user_flows(scan)
    assert r["uf_plugin_collapsed"] == 4
    assert {uf["domain"] for uf in r["user_flows"]} == {"integration"}


# Filter C — infra / DI / version / artifact domain tokens dropped.


def test_filter_c_infra_di_domain_dropped():
    scan = {
        "flows": [
            _flow("initialize-dependency-container-flow",
                  entry_point_file="packages/features/di/di.ts",
                  paths=["packages/features/di/di.ts"], primary_feature="di"),
        ],
        "developer_features": [{"name": "di", "product_feature_id": "di"}],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_infra_domain"] == 1
    assert r["user_flows"] == []


def test_filter_c_version_and_numeric_and_single_char_dropped():
    scan = {
        "flows": [
            _flow("create-team-flow",
                  entry_point_file="apps/api/v1/teams.ts",
                  paths=["apps/api/v1/teams.ts"], primary_feature="v1"),
            _flow("calc-billing-flow",
                  entry_point_file="packages/ee/billing/x.ts",
                  paths=["packages/ee/billing/x.ts"], primary_feature="0"),
        ],
        "developer_features": [
            {"name": "v1", "product_feature_id": "v1"},
            {"name": "0", "product_feature_id": "0"},
        ],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_infra_domain"] == 2
    assert r["user_flows"] == []


def test_filter_c_compound_infra_head_dropped():
    """A compound domain whose HEAD word is infra (``platform_util`` → head
    ``util``, resolved via the source-folder heuristic) is dropped; a compound
    whose head is a real noun (``team_setting``) survives."""
    scan = {
        "flows": [
            _flow("read-permissions-flow",
                  entry_point_file="src/platform_util/permissions.ts",
                  paths=["src/platform_util/permissions.ts"]),
            _flow("manage-team-setting-flow",
                  entry_point_file="src/team_setting/lib.ts",
                  paths=["src/team_setting/lib.ts"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_infra_domain"] == 1
    # team_setting (head "setting") survives.
    assert len(r["user_flows"]) == 1
    assert r["user_flows"][0]["domain"] == "team_setting"


# Real-journey protection guard — a true journey is NEVER filtered.


def test_real_journey_never_filtered_by_any_filter():
    """A real booking journey grounded in a backend router is untouched by all
    three filters (the load-bearing safety property)."""
    scan = {
        "flows": [
            _flow("create-booking-flow",
                  entry_point_file="apps/web/app/booking/route.ts",
                  paths=["apps/web/app/booking/route.ts",
                         "packages/ui/components/button/index.ts"],
                  primary_feature="booking"),
            _flow("cancel-booking-flow",
                  entry_point_file="backend/routers/bookings.py",
                  paths=["backend/routers/bookings.py"],
                  primary_feature="booking"),
        ],
        "developer_features": [
            {"name": "booking", "product_feature_id": "booking-management"},
        ],
    }
    r = cluster_user_flows(scan)
    assert r["uf_filtered_ui_primitive"] == 0
    assert r["uf_filtered_infra_domain"] == 0
    assert r["uf_plugin_collapsed"] == 0
    domains = {uf["domain"] for uf in r["user_flows"]}
    assert "booking" in domains


def test_filters_do_not_change_total_flows_layer1_intact():
    """Across a mixed scan, total_flows == input flow count: Layer 1 intact."""
    flows = [
        _flow("input-text-flow", entry_point_file="packages/ui/components/form/index.ts",
              paths=["packages/ui/components/form/index.ts"]),
        _flow("add-alpha-integration-flow", entry_point_file="packages/app-store/alpha/api/index.ts",
              paths=["packages/app-store/alpha/api/index.ts"]),
        _flow("add-bravo-integration-flow", entry_point_file="packages/app-store/bravo/api/index.ts",
              paths=["packages/app-store/bravo/api/index.ts"]),
        _flow("add-charlie-integration-flow", entry_point_file="packages/app-store/charlie/api/index.ts",
              paths=["packages/app-store/charlie/api/index.ts"]),
        _flow("init-di-flow", entry_point_file="packages/features/di/di.ts",
              paths=["packages/features/di/di.ts"], primary_feature="di"),
        _flow("create-booking-flow", entry_point_file="backend/routers/bookings.py",
              paths=["backend/routers/bookings.py"], primary_feature="booking"),
    ]
    scan = {"flows": flows,
            "developer_features": [{"name": "di", "product_feature_id": "di"},
                                   {"name": "booking", "product_feature_id": "booking"}]}
    r = cluster_user_flows(scan)
    assert r["total_flows"] == 6
    assert r["unique_flows"] == 6
