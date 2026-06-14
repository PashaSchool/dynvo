"""Stage 6.8b — system/background-flow classification + UF inheritance."""
from __future__ import annotations

import json

from faultline.pipeline_v2.stage_6_7_user_flows import cluster_user_flows
from faultline.pipeline_v2.system_flows import (
    SystemFlowClassifier,
    _file_to_url,
    classify_routes,
)

_PATTERNS = {
    "path_globs": {
        "scheduled": [r"(?:^|/)api/cron(?:/|$)"],
        "queue": [r"(?:^|/)jobs?(?:/|$)"],
        "webhook": [r"(?:^|/)webhooks?(?:/|$)"],
    },
    "content_markers": {
        "queue": ["qstash"],
        "webhook": ["constructevent"],
        "scheduled": [],
    },
    "cron_manifests": {},
}


def test_path_glob_classification() -> None:
    c = SystemFlowClassifier(patterns=_PATTERNS)
    assert c.classify("app/api/cron/x/route.ts") == "scheduled"
    assert c.classify("app/api/jobs/y/route.ts") == "queue"
    assert c.classify("app/api/webhook/z/route.ts") == "webhook"
    assert c.classify("app/dashboard/page.tsx") == "interactive"
    assert not c.is_system("app/dashboard/page.tsx")


def test_no_patterns_is_noop() -> None:
    c = SystemFlowClassifier(patterns={})
    assert c.classify("app/api/cron/x/route.ts") == "interactive"


def test_content_marker(tmp_path) -> None:
    (tmp_path / "h.ts").write_text("import { Receiver } from '@upstash/qstash'\n")
    c = SystemFlowClassifier(repo_path=tmp_path, patterns=_PATTERNS)
    assert c.classify("h.ts") == "queue"


def test_cron_manifest_is_authoritative(tmp_path) -> None:
    # A cron handler can live OUTSIDE any cron/ dir and carry no marker — the
    # vercel.json crons[] declaration is the authoritative signal.
    (tmp_path / "vercel.json").write_text(
        json.dumps({"crons": [{"path": "/api/watch/all", "schedule": "0 * * * *"}]})
    )
    pats = dict(
        _PATTERNS,
        cron_manifests={
            "vercel": {"file_glob": "**/vercel.json", "crons_key": "crons", "path_key": "path"}
        },
    )
    c = SystemFlowClassifier(repo_path=tmp_path, patterns=pats)
    assert c.classify("apps/web/app/api/watch/all/route.ts", "/api/watch/all") == "scheduled"


def test_file_to_url() -> None:
    assert _file_to_url("apps/web/app/api/watch/all/route.ts") == "/api/watch/all"
    assert _file_to_url("app/(app)/settings/page.tsx") == "/settings"
    assert _file_to_url("lib/util.ts") == ""


def test_classify_routes_stamps_trigger_and_counts() -> None:
    # Uses the committed runtime patterns (load_patterns()).
    ri = [
        {"file": "app/api/cron/a/route.ts", "pattern": "/api/cron/a"},
        {"file": "app/x/page.tsx", "pattern": "/x"},
    ]
    counts = classify_routes(ri, repo_path=None)
    assert ri[0]["trigger"] == "scheduled"
    assert ri[1]["trigger"] == "interactive"
    assert counts.get("scheduled") == 1


def _flow(name, uuid, f, feat):
    return {
        "name": name, "uuid": uuid, "entry_point_file": f,
        "paths": [f], "primary_feature": feat,
    }


def test_uf_system_labeling_and_infra_exemption() -> None:
    # `process-digest-flow` is anchored on app/api/cron/digest — its path domain
    # resolves to an infra token, so WITHOUT the exemption it would be filtered
    # out. The scheduled trigger rescues it as a system UF.
    scan = {
        "flows": [
            _flow("manage-clean-flow", "f1", "apps/web/app/api/clean/route.ts", "clean"),
            _flow("process-digest-flow", "f3", "apps/web/app/api/cron/digest/route.ts", "digest"),
            _flow("browse-settings-flow", "f2", "apps/web/app/(app)/settings/page.tsx", "settings"),
        ],
        "developer_features": [{"name": "clean"}, {"name": "digest"}, {"name": "settings"}],
    }
    ridx = [
        {"file": "apps/web/app/api/clean/route.ts", "pattern": "/api/clean", "trigger": "queue"},
        {"file": "apps/web/app/api/cron/digest/route.ts", "pattern": "/api/cron/digest", "trigger": "scheduled"},
        {"file": "apps/web/app/(app)/settings/page.tsx", "pattern": "/settings", "trigger": "interactive"},
    ]
    res = cluster_user_flows(scan, routes_index=ridx)
    by_cat: dict[str, list] = {}
    for u in res["user_flows"]:
        by_cat.setdefault(u["category"], []).append(u)
    assert len(by_cat.get("system", [])) == 2
    assert {u["trigger"] for u in by_cat["system"]} == {"queue", "scheduled"}
    assert len(by_cat.get("interactive", [])) >= 1


def test_uf_system_splits_from_same_domain_interactive() -> None:
    # A system flow (stripe webhook) and an interactive flow (billing page) share
    # the `billing` domain. They must produce TWO UFs — the webhook journey is
    # NOT folded into the interactive billing UF (the dilution fix).
    scan = {
        "flows": [
            _flow("manage-billing-flow", "b1", "apps/web/app/(app)/billing/page.tsx", "billing"),
            _flow("process-billing-flow", "b2", "apps/web/app/api/stripe/webhook/route.ts", "billing"),
        ],
        "developer_features": [{"name": "billing"}],
    }
    ridx = [
        {"file": "apps/web/app/(app)/billing/page.tsx", "pattern": "/billing", "trigger": "interactive"},
        {"file": "apps/web/app/api/stripe/webhook/route.ts", "pattern": "/api/stripe/webhook", "trigger": "webhook"},
    ]
    res = cluster_user_flows(scan, routes_index=ridx)
    cats = [u["category"] for u in res["user_flows"]]
    assert cats.count("system") == 1 and cats.count("interactive") == 1


def test_synthesised_system_uf_for_flowless_route() -> None:
    # A system route with NO flow → a thin synthetic system UF (member_count=0).
    # Sibling routes of one journey dedup; the flow graph is never touched.
    scan = {
        "flows": [_flow("browse-settings-flow", "s1", "apps/web/app/(app)/settings/page.tsx", "settings")],
        "developer_features": [{"name": "settings"}],
    }
    ridx = [
        {"file": "apps/web/app/(app)/settings/page.tsx", "pattern": "/settings", "trigger": "interactive"},
        {"file": "apps/web/app/api/google/webhook/route.ts", "pattern": "/api/google/webhook", "trigger": "webhook"},
        {"file": "apps/web/app/api/cron/automation-jobs/route.ts", "pattern": "/api/cron/automation-jobs", "trigger": "scheduled"},
        {"file": "apps/web/app/api/automation-jobs/execute/route.ts", "pattern": "/api/automation-jobs/execute", "trigger": "queue"},
    ]
    res = cluster_user_flows(scan, routes_index=ridx)
    syn = [u for u in res["user_flows"] if u["category"] == "system" and u["member_count"] == 0]
    assert len(syn) == 2  # google-webhook + automation-jobs (cron + execute deduped)
    autojobs = [u for u in syn if u["resource"] == "automation-jobs"]
    assert len(autojobs) == 1 and len(autojobs[0]["routes"]) == 2


def test_synthesis_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_SEED_SYSTEM_UFS", "0")
    scan = {"flows": [], "developer_features": []}
    ridx = [{"file": "apps/web/app/api/google/webhook/route.ts", "pattern": "/api/google/webhook", "trigger": "webhook"}]
    res = cluster_user_flows(scan, routes_index=ridx)
    assert not [u for u in res["user_flows"] if u["category"] == "system" and u["member_count"] == 0]


def test_synthesis_skips_routes_that_already_have_a_flow() -> None:
    # A system route that DOES have a flow must not also be synthesised (no dup).
    scan = {
        "flows": [_flow("process-webhook-flow", "w1", "apps/web/app/api/google/webhook/route.ts", "google")],
        "developer_features": [{"name": "google"}],
    }
    ridx = [{"file": "apps/web/app/api/google/webhook/route.ts", "pattern": "/api/google/webhook", "trigger": "webhook"}]
    res = cluster_user_flows(scan, routes_index=ridx)
    assert not [u for u in res["user_flows"] if u["member_count"] == 0]  # covered by the real flow


def test_uf_no_routes_index_all_interactive() -> None:
    scan = {
        "flows": [_flow("browse-settings-flow", "f2", "apps/web/app/(app)/settings/page.tsx", "settings")],
        "developer_features": [{"name": "settings"}],
    }
    res = cluster_user_flows(scan, routes_index=None)
    assert all(u["category"] == "interactive" for u in res["user_flows"])
