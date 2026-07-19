"""S5b — Stage 6.987 leaf-route dissolution + platform promotion (ONE wave).

Canon (spec fixs5b §ПРОБИ-ФОРМИ; experimenter /private/tmp/s5b-probe):

Seg B predicate = route:-anchor PF with leaf-ness (dir_grain==0 in product
routes + a flat route file stem==slug) ∧ own<=3 ∧ annexed/own>=12; anti-
protection is the LEAF gate, not the ratio.
Seg C = P1 (page evidence in a lane resident; a-lite exclusions _app/
_document/_error + pages/api; unit = PAGE-COHORT) ∪ P2 (lane token ↔ freed/
mis-homed product-PAGE, len>3, intersection>=1); birth (S5a birth-law) or
merge-into-sibling (notifications class).

NAMED units + anti-cases:
  * dup-workflow shape — leaf fires, foreign devs dissolve to siblings,
    the workflow PAGE freed by dissolution promotes workflow-editor via P2.
  * typebot past-due shape — leaf fires, dissolution to feature siblings.
  * Soc0 policy-page — leaf fires (38 %); an unhomed dev lanes (NOT forced).
  * ANTI cal bookings — dir_grain>0 → leaf gate fails → 0 moves (unit-lock).
  * ANTI twenty workflows — deep real domain, no flat-leaf → 0 moves.
  * ANTI supabase/docs — a 979-file shell with 2 pages: the page-cohort
    does NOT drag the whole row.
  * ANTI langfuse — _app + pages/api are NOT page evidence.
  * Seg C merge — a notifications lane resident merges into its sibling PF.
  * Flag default OFF (no-op) + determinism (double run identical).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.leafroute_promotion import (
    LEAFROUTE_PROMOTION_ENV,
    _leaf_firing,
    _page_files,
    _product_route_seg_index,
    leafroute_promotion_enabled,
    run_leafroute_promotion,
)

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


# ── real-model builders (births need real Feature rows) ─────────────────────


def dev(name, pfid, paths, *, shared_reason=None, anchor_id=None):
    f = Feature(
        name=name, display_name=name, description="",
        paths=list(paths), authors=[], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_EPOCH, health_score=1.0,
        layer="developer",
    )
    f.product_feature_id = pfid
    f.shared_reason = shared_reason
    if anchor_id:
        f.anchor_id = anchor_id
    return f


def pf(name, anchor_id, paths=(), surface_scope="product"):
    f = Feature(
        name=name, display_name=name, description="",
        paths=list(paths), authors=[], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_EPOCH, health_score=1.0,
        layer="product",
    )
    f.anchor_id = anchor_id
    f.surface_scope = surface_scope
    f.member_files = [
        MemberFile(path=str(p), role="anchor", confidence=1.0,
                   evidence="seed", primary=True) for p in paths
    ]
    return f


def ri(files):
    return [{"file": f, "surface_scope": "product", "method": "PAGE",
             "pattern": "/" + f} for f in files]


class Ctx:
    repo_path = "."


def _run(devs, pfs, routes_index):
    return run_leafroute_promotion(devs, pfs, [], [], routes_index, Ctx())


def _homed(devs, key):
    return [d for d in devs if getattr(d, "product_feature_id", None) == key]


# ── the dup-workflow scene (Seg B fires + P2 promotes workflow-editor) ──────


def _dup_workflow_scene():
    # leaf PF: route:duplicate-workflow — own dev tiny, annexed subtree huge.
    own_paths = ["apps/dashboard/src/pages/duplicate-workflow.tsx",
                 "apps/dashboard/src/hooks/use-duplicate-workflow.ts"]
    sub_paths = [f"apps/dashboard/src/components/subscribers/sub-{i}.tsx"
                 for i in range(14)]
    ana_paths = [f"apps/dashboard/src/api/analytics-{i}.ts" for i in range(10)]
    dom_paths = [f"apps/dashboard/src/pages/domains-{i}.tsx" for i in range(6)]
    # a foreign dev with NO sibling → unhomed → lane (freed); it carries a
    # workflow PAGE so P2 can bridge it to the workflow-editor lane resident.
    freed_paths = ["apps/dashboard/src/pages/new-workflow.tsx",
                   "apps/dashboard/src/api/connect-claim.ts"]

    leaf = pf("duplicate-workflow", "route:duplicate-workflow",
              paths=own_paths + sub_paths + ana_paths + dom_paths + freed_paths)
    subs = pf("subscribers", "route:subscribers",
              paths=["apps/dashboard/src/pages/subscribers.tsx"])
    ana = pf("analytics", "route:analytics",
             paths=["apps/dashboard/src/pages/analytics.tsx"])
    dom = pf("domains", "route:domains",
             paths=["apps/dashboard/src/pages/domains.tsx"])
    pfs = [leaf, subs, ana, dom]

    devs = [
        dev("duplicate-workflow", "duplicate-workflow", own_paths),
        dev("subscribers", "duplicate-workflow", sub_paths),
        dev("analytics", "duplicate-workflow", ana_paths),
        dev("domains", "duplicate-workflow", dom_paths),
        dev("new-workflow", "duplicate-workflow", freed_paths),
        # workflow-editor: already a lane resident, 0 pages of its own.
        dev("workflow-editor", None,
            [f"apps/dashboard/src/components/workflow-editor/node-{i}.tsx"
             for i in range(12)], shared_reason="shell_lineage_only"),
    ]
    routes = ri([
        "apps/dashboard/src/pages/duplicate-workflow.tsx",
        "apps/dashboard/src/pages/subscribers.tsx",
        "apps/dashboard/src/pages/analytics.tsx",
        "apps/dashboard/src/pages/domains.tsx",
        "apps/dashboard/src/pages/new-workflow.tsx",
    ])
    return devs, pfs, routes


def test_seg_b_leaf_fires_and_dissolves():
    devs, pfs, routes = _dup_workflow_scene()
    tele = _run(devs, pfs, routes)
    assert any(r["pf"] == "duplicate-workflow"
               for r in tele.get("leaf_fired", [])), tele
    # the foreign capability devs re-homed to their siblings.
    assert _homed(devs, "subscribers"), "subscribers dev should re-home"
    assert _homed(devs, "analytics"), "analytics dev should re-home"
    assert _homed(devs, "domains"), "domains dev should re-home"
    # the leaf's OWN dev (name==slug) stays.
    own = [d for d in devs if d.name == "duplicate-workflow"]
    assert own and own[0].product_feature_id == "duplicate-workflow"
    # the leaf PF shed the re-homed subtree from its paths.
    leaf = [p for p in pfs if p.name == "duplicate-workflow"][0]
    assert not any("subscribers/" in p for p in leaf.paths)


def test_p2_bridges_freed_page_to_workflow_editor():
    devs, pfs, routes = _dup_workflow_scene()
    tele = _run(devs, pfs, routes)
    # new-workflow had no sibling → laned (freed); its page bridges to the
    # workflow-editor lane resident (token 'workflow') → promotion.
    born = tele.get("births", [])
    assert any(b["dev"] == "workflow-editor" and b["kind"] == "P2"
               for b in born), f"workflow-editor P2 promotion expected: {tele}"
    assert tele.get("p2_pages_bridged", 0) >= 1


# ── typebot past-due scene ──────────────────────────────────────────────────


def test_typebot_past_due_dissolves():
    own = ["apps/builder/src/features/billing/past-due.tsx"]
    tb = [f"apps/builder/src/features/typebot/api/handle-{i}.ts"
          for i in range(6)]
    res = [f"apps/builder/src/features/results/api/handle-{i}.ts"
           for i in range(6)]
    leaf = pf("past-due", "route:past-due", paths=own + tb + res)
    typebots = pf("typebots", "route:typebots",
                  paths=["apps/builder/src/features/typebot/index.tsx"])
    results = pf("results", "route:results",
                 paths=["apps/builder/src/features/results/index.tsx"])
    pfs = [leaf, typebots, results]
    devs = [
        dev("past-due", "past-due", own),
        dev("typebots", "past-due", tb),
        dev("results", "past-due", res),
    ]
    routes = ri(["apps/builder/src/pages/past-due.tsx",
                 "apps/builder/src/features/typebot/index.tsx",
                 "apps/builder/src/features/results/index.tsx"])
    tele = _run(devs, pfs, routes)
    assert any(r["pf"] == "past-due" for r in tele.get("leaf_fired", []))
    assert _homed(devs, "typebots") and _homed(devs, "results")


# ── Soc0 policy-page — an unhomed dev lanes (NOT forced) ────────────────────


def test_soc0_policy_page_unhomed_lanes():
    own = ["frontend/src/pages/policy-page.tsx"]
    lab = [f"frontend/src/features/labels/svc-{i}.ts" for i in range(8)]
    # a dev with no matching sibling — must lane, not be force-homed.
    orphan = [f"frontend/src/components/ui/scroll-area-{i}.tsx" for i in range(8)]
    leaf = pf("policy-page", "route:policy-page", paths=own + lab + orphan)
    labels = pf("labels", "route:labels",
                paths=["frontend/src/pages/labels.tsx"])
    pfs = [leaf, labels]
    devs = [
        dev("policy-page", "policy-page", own),
        dev("labels", "policy-page", lab),
        dev("ui-kit", "policy-page", orphan),   # no sibling → lane
    ]
    routes = ri(["frontend/src/pages/policy-page.tsx",
                 "frontend/src/pages/labels.tsx"])
    tele = _run(devs, pfs, routes)
    assert any(r["pf"] == "policy-page" for r in tele.get("leaf_fired", []))
    assert _homed(devs, "labels"), "labels dev homes"
    # unhomed paths are NOT forced — the ui-kit dev keeps them on the leaf.
    orphan_dev = [d for d in devs if d.name == "ui-kit"][0]
    assert orphan_dev.product_feature_id == "policy-page", \
        "unhomed dev must stay (never force-homed)"
    assert len(orphan_dev.paths) == 8, "unhomed paths retained"


# ── ANTI-CASES (unit-locks) ─────────────────────────────────────────────────


def test_anti_cal_bookings_deep_domain_never_fires():
    # bookings has a real route DIRECTORY (dir_grain>0) → leaf gate fails
    # even though it owns a big subtree — the leaf gate, not the ratio.
    booking_dir = [f"apps/web/app/bookings/{n}/page.tsx"
                   for n in ("upcoming", "past", "cancelled", "recurring")]
    sub = [f"apps/web/app/bookings/_components/comp-{i}.tsx" for i in range(20)]
    bookings = pf("bookings", "route:bookings", paths=booking_dir + sub)
    pfs = [bookings, pf("event-types", "route:event-types",
                        paths=["apps/web/app/event-types/page.tsx"])]
    devs = [dev("bookings", "bookings", booking_dir + sub)]
    routes = ri(booking_dir + ["apps/web/app/event-types/page.tsx"])
    dir_sets, stems = _product_route_seg_index(routes)
    assert _leaf_firing(bookings, dir_sets, stems) is None, "dir_grain>0 → no fire"
    tele = _run(devs, pfs, routes)
    assert not tele.get("leaf_fired"), "cal bookings must be 0-move"
    assert bookings.paths, "bookings untouched"


def test_anti_twenty_workflows_no_flat_leaf():
    # workflows lives under a directory with no flat leaf file stem==slug.
    wf = [f"packages/twenty-front/src/modules/workflow/comp-{i}.tsx"
          for i in range(30)]
    workflows = pf("workflow", "route:workflow", paths=wf)
    pfs = [workflows, pf("settings", "route:settings",
                         paths=["packages/twenty-front/src/pages/settings.tsx"])]
    devs = [dev("workflow", "workflow", wf)]
    # routes_index has a DIR 'workflow' but NO flat file stem 'workflow'.
    routes = ri([f"packages/twenty-front/src/modules/workflow/page-{i}.tsx"
                 for i in range(3)])
    tele = _run(devs, pfs, routes)
    assert not tele.get("leaf_fired"), "twenty workflows must be 0-move"


def test_anti_supabase_docs_pagecohort_does_not_drag_979():
    # a huge lane shell (docs): 979-ish files, only 2 real pages. The
    # page-cohort promotes the 2 pages' own dirs, never the whole row.
    docs_files = [f"apps/docs/src/content/guide-{i}.mdx" for i in range(200)]
    docs_files += [f"apps/docs/src/lib/util-{i}.ts" for i in range(200)]
    pages = ["apps/docs/pages/index.tsx", "apps/docs/pages/pricing.tsx"]
    docs = dev("docs", None, docs_files + pages,
               shared_reason="shell_lineage_only")
    pfs = [pf("home", "route:home", paths=["apps/web/app/page.tsx"])]
    devs = [docs]
    routes = ri(pages + ["apps/web/app/page.tsx"])
    tele = _run(devs, pfs, routes)
    born = tele.get("births", [])
    # if it promotes at all, the born PF holds only the tiny page cohort.
    for b in born:
        if b["dev"] == "docs":
            assert b["paths"] <= 10, f"page-cohort dragged {b['paths']} files"
    # the docs lane resident keeps the bulk of its 400 files.
    assert len(docs.paths) >= 390, "docs shell must retain its bulk"


def test_anti_langfuse_app_and_pagesapi_not_page_evidence():
    # _app / _document / _error + a pages/api file are NOT page evidence.
    paths = [
        "web/src/pages/_app.tsx",
        "web/src/pages/_document.tsx",
        "web/src/pages/api/public/scim/users.ts",
        "web/src/pages/project/settings.tsx",   # a REAL page
    ]
    got = _page_files(paths, page_ri_files=set())
    assert "web/src/pages/_app.tsx" not in got
    assert "web/src/pages/_document.tsx" not in got
    assert "web/src/pages/api/public/scim/users.ts" not in got
    assert "web/src/pages/project/settings.tsx" in got


# ── Seg C merge-into-sibling (notifications class) ──────────────────────────


def test_seg_c_notifications_merges_into_sibling():
    # a lane resident 'notifications' with a page, and an existing
    # 'notifications' PF sibling → MERGE, not birth.
    notif = dev("notifications", None,
                ["playground/nextjs/src/pages/notifications/index.tsx"],
                shared_reason="technology_instrument")
    sibling = pf("notifications", "route:notifications",
                 paths=["apps/dashboard/src/pages/inbox.tsx"])
    pfs = [sibling]
    devs = [notif]
    routes = ri(["playground/nextjs/src/pages/notifications/index.tsx",
                 "apps/dashboard/src/pages/inbox.tsx"])
    tele = _run(devs, pfs, routes)
    assert any(m["into"] == "notifications"
               for m in tele.get("merged", [])), f"expected merge: {tele}"
    assert notif.product_feature_id == "notifications"
    assert tele.get("pfs_born", 0) == 0 or all(
        b["dev"] != "notifications" for b in tele.get("births", []))


# ── flag OFF byte-no-op + determinism ───────────────────────────────────────


def test_flag_default_off():
    assert leafroute_promotion_enabled() is False


def test_off_scene_is_noop(monkeypatch):
    # NOTE: run_leafroute_promotion is only CALLED when the flag is on (the
    # caller guards it); here we assert the guard itself.
    monkeypatch.delenv(LEAFROUTE_PROMOTION_ENV, raising=False)
    assert leafroute_promotion_enabled() is False
    monkeypatch.setenv(LEAFROUTE_PROMOTION_ENV, "0")
    assert leafroute_promotion_enabled() is False
    monkeypatch.setenv(LEAFROUTE_PROMOTION_ENV, "1")
    assert leafroute_promotion_enabled() is True


def test_determinism_double_run_identical():
    d1, p1, r1 = _dup_workflow_scene()
    d2, p2, r2 = _dup_workflow_scene()
    t1 = _run(d1, p1, r1)
    t2 = _run(d2, p2, r2)
    # telemetry counters + move ledgers identical across two fresh runs.
    for k in ("devs_rehomed", "paths_dissolved", "pfs_born", "devs_merged",
              "p2_pages_bridged"):
        assert t1[k] == t2[k], f"{k} not deterministic: {t1[k]} vs {t2[k]}"
    assert ([b["pf"] for b in t1["births"]]
            == [b["pf"] for b in t2["births"]])
    assert ([(m["from"], m["to"]) for m in t1["dissolve_moves"]]
            == [(m["from"], m["to"]) for m in t2["dissolve_moves"]])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
