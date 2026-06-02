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
    # singularized router resource
    assert r["user_flows"][0]["product_feature_id"] == "detector"


def test_router_init_is_skipped():
    scan = {
        "flows": [_flow("view-thing-flow",
                        paths=["pkg/routers/__init__.py", "app/things/svc.py"])],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    # __init__ skipped -> falls through to source folder "things"
    assert r["user_flows"][0]["product_feature_id"] == "thing"


def test_domain_from_product_feature_when_no_router():
    scan = {
        "flows": [_flow("view-thing-flow", primary_feature="dashboard-widgets",
                        paths=["frontend/components/Widget.tsx"])],
        "developer_features": [
            {"name": "dashboard-widgets", "product_feature_id": "analytics"},
        ],
    }
    r = cluster_user_flows(scan)
    assert r["user_flows"][0]["product_feature_id"] == "analytics"


def test_domain_none_when_ungroundable():
    scan = {"flows": [_flow("view-thing-flow", paths=["README.md"])],
            "developer_features": []}
    r = cluster_user_flows(scan)
    assert r["user_flows"][0]["product_feature_id"] is None


# ── Stage C — cluster by (domain, intent) ───────────────────────────────


def test_cluster_groups_by_domain_and_intent_not_resource():
    # two distinct resources, same domain + intent -> ONE cluster.
    scan = {
        "flows": [
            _flow("create-detector-flow", paths=["backend/routers/detectors.py"]),
            _flow("create-rule-flow", paths=["backend/routers/detectors.py"]),
            _flow("list-detector-flow", paths=["backend/routers/detectors.py"]),
        ],
        "developer_features": [],
    }
    r = cluster_user_flows(scan)
    intents = sorted(u["intent"] for u in r["user_flows"])
    assert intents == ["author", "browse"]  # 2 UF, not 3
    author = next(u for u in r["user_flows"] if u["intent"] == "author")
    assert author["member_count"] == 2


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
    assert r["user_flows"][0]["product_feature_id"] == "apple"


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
            _flow("create-detector-flow",
                  paths=["backend/routers/detectors.py", "backend/models/d.py"],
                  secondary_features=["billing-svc"], test_files=["t.py"],
                  coverage_pct=80.0),
            _flow("update-detector-flow",
                  paths=["backend/routers/detectors.py"],
                  secondary_features=["detector-core"],  # same domain -> excluded
                  coverage_pct=60.0),
        ],
        "developer_features": [
            {"name": "billing-svc", "product_feature_id": "billing"},
            {"name": "detector-core", "product_feature_id": "detector"},
        ],
    }
    r = cluster_user_flows(scan)
    uf = r["user_flows"][0]
    assert uf["routes"] == ["backend/routers/detectors.py"]  # deduped, router-only
    assert uf["cross_links"] == ["billing"]  # own domain excluded
    assert uf["ac_draft_count"] == 1  # one member with test_files
    assert uf["coverage_pct"] == 70.0  # mean of 80 and 60


def test_coverage_none_when_no_members_have_coverage():
    scan = {"flows": [_flow("view-x-flow", paths=["backend/routers/xs.py"])],
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
