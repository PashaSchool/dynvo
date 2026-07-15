"""W3.1 D6 — route-group journey recall seeds (validator I24's ruler)."""

from __future__ import annotations

from types import SimpleNamespace

from faultline.models.types import UserFlow
from faultline.pipeline_v2.route_group_recall import (
    route_group_seeds_enabled,
    seed_route_group_journeys,
)


def _flow(uuid: str, entry: str, paths=None):
    return SimpleNamespace(uuid=uuid, name=f"{uuid}-flow",
                           entry_point_file=entry, paths=paths or [entry])


def _dev(pfid: str, paths):
    return SimpleNamespace(layer="developer", product_feature_id=pfid,
                           paths=list(paths))


def _pf(name: str, paths=()):
    return SimpleNamespace(name=name, paths=list(paths))


def _uf(uid: str, name: str, member_flow_ids):
    return UserFlow(id=uid, name=name, resource="r", domain=None,
                    product_feature_id="documents", intent="manage",
                    member_flow_ids=member_flow_ids,
                    member_count=len(member_flow_ids))


_ROUTES = [
    # covered group — an existing UF touches its file
    {"pattern": "/documents", "method": "PAGE", "surface_scope": "product",
     "file": "app/documents/page.tsx"},
    {"pattern": "/documents/shared", "method": "PAGE",
     "surface_scope": "product", "file": "app/documents/shared.tsx"},
    # the HOLE — tracecat `tables` class: 2 pages, zero UF touch
    {"pattern": "/tables", "method": "PAGE", "surface_scope": "product",
     "file": "app/tables/page.tsx"},
    {"pattern": "/tables/[id]", "method": "PAGE", "surface_scope": "product",
     "file": "app/tables/[id]/page.tsx"},
    # marketing rows carry no journey obligation
    {"pattern": "/pricing", "method": "PAGE", "surface_scope": "marketing",
     "file": "www/pricing/a.tsx"},
    {"pattern": "/blog", "method": "PAGE", "surface_scope": "marketing",
     "file": "www/pricing/b.tsx"},
]


def test_seed_appended_for_uncovered_group():
    flows = [
        _flow("f-doc", "app/documents/page.tsx"),
        _flow("f-tab-1", "app/tables/page.tsx"),
        _flow("f-tab-2", "app/tables/[id]/page.tsx"),
    ]
    ufs = [_uf("UF-001", "Manage documents", ["f-doc"])]
    features = [
        _dev("documents", ["app/documents/page.tsx"]),
        _dev("tables", ["app/tables/page.tsx", "app/tables/[id]/page.tsx"]),
    ]
    pfs = [_pf("documents"), _pf("tables")]
    tele = seed_route_group_journeys(ufs, features, pfs, flows, _ROUTES)
    assert tele["holes"] == 1 and tele["seeded"] == 1
    seed = ufs[-1]
    assert seed.synthesized is True
    assert seed.synthesis_reason == "route_group_recall"
    assert seed.binding_confidence == "low"
    assert seed.name_confidence == "low"
    assert seed.product_feature_id == "tables"
    assert seed.id == "UF-002"  # numbering continues
    assert set(seed.member_flow_ids) == {"f-tab-1", "f-tab-2"}
    assert "tables" in (seed.name or "").lower()
    # verb-phrase seed name — never a PF==UF titleize twin
    assert (seed.name or "").strip().lower() != "tables"


def test_hole_without_flow_evidence_stays_honest():
    flows = [_flow("f-doc", "app/documents/page.tsx")]
    ufs = [_uf("UF-001", "Manage documents", ["f-doc"])]
    features = [_dev("documents", ["app/documents/page.tsx"])]
    pfs = [_pf("documents")]
    tele = seed_route_group_journeys(ufs, features, pfs, flows, _ROUTES)
    assert tele["holes"] == 1
    assert tele["seeded"] == 0
    assert tele["skipped_no_flows"] == 1
    assert len(ufs) == 1


def test_hole_without_pf_home_stays_honest():
    flows = [
        _flow("f-doc", "app/documents/page.tsx"),
        _flow("f-tab-1", "app/tables/page.tsx"),
    ]
    ufs = [_uf("UF-001", "Manage documents", ["f-doc"])]
    # nobody owns the tables files and no PF path covers them
    features = [_dev("documents", ["app/documents/page.tsx"])]
    pfs = [_pf("documents")]
    tele = seed_route_group_journeys(ufs, features, pfs, flows, _ROUTES)
    assert tele["seeded"] == 0
    assert tele["skipped_no_pf"] == 1


def test_single_route_groups_and_marketing_never_seed():
    routes = [
        {"pattern": "/solo", "method": "PAGE", "surface_scope": "product",
         "file": "app/solo/page.tsx"},
        {"pattern": "/pricing", "method": "PAGE", "surface_scope": "marketing",
         "file": "www/pricing/a.tsx"},
        {"pattern": "/blog", "method": "PAGE", "surface_scope": "marketing",
         "file": "www/pricing/b.tsx"},
    ]
    flows = [_flow("f-solo", "app/solo/page.tsx")]
    features = [_dev("solo", ["app/solo/page.tsx"])]
    tele = seed_route_group_journeys([], features, [_pf("solo")], flows, routes)
    assert tele["seeded"] == 0 and tele["holes"] == 0


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("FAULTLINE_ROUTE_GROUP_SEED_UFS", "0")
    assert not route_group_seeds_enabled()
    monkeypatch.delenv("FAULTLINE_ROUTE_GROUP_SEED_UFS", raising=False)
    assert route_group_seeds_enabled()


# ── W4.2 Fix 2 — seed surface-guard (D6) ─────────────────────────────────


def _guard_clf(instrument_dirs=()):
    from faultline.pipeline_v2.surface_taxonomy import SurfaceScopeClassifier
    return SurfaceScopeClassifier(
        patterns={}, instrument_dirs=instrument_dirs)


def test_seed_never_homes_onto_an_instrument_pf():
    """The 'Run prisma' doctrine at D6 grain: a group whose only home is
    a dev_tooling (instrument) PF stays an honest hole."""
    flows = [
        _flow("f-tab-1", "app/tables/page.tsx"),
        _flow("f-tab-2", "app/tables/[id]/page.tsx"),
    ]
    devs = [_dev("toolkit", ["app/tables/page.tsx",
                             "app/tables/[id]/page.tsx",
                             "packages/toolkit/index.ts"])]
    pfs = [_pf("toolkit", ["packages/toolkit/index.ts"])]
    ufs: list = []
    tele = seed_route_group_journeys(
        ufs, devs, pfs, flows, list(_ROUTES),
        scope_classifier=_guard_clf(("packages/toolkit",)),
        route_by_file={},
    )
    assert tele["skipped_non_product_home"] == 1
    assert tele["seeded"] == 0 and not ufs


def test_seed_homes_onto_the_product_plurality_instead():
    """A product home exists alongside the non-product one — the seed
    homes onto the product PF (the guard only removes bad votes)."""
    flows = [
        _flow("f-tab-1", "app/tables/page.tsx"),
        _flow("f-tab-2", "app/tables/[id]/page.tsx"),
    ]
    devs = [
        _dev("toolkit", ["app/tables/page.tsx",
                         "packages/toolkit/index.ts"]),
        _dev("tables", ["app/tables/page.tsx",
                        "app/tables/[id]/page.tsx"]),
    ]
    pfs = [_pf("toolkit", ["packages/toolkit/index.ts"]),
           _pf("tables", ["app/tables/page.tsx"])]
    ufs: list = []
    tele = seed_route_group_journeys(
        ufs, devs, pfs, flows, list(_ROUTES),
        scope_classifier=_guard_clf(("packages/toolkit",)),
        route_by_file={},
    )
    assert tele["seeded"] == 1
    assert ufs and ufs[0].product_feature_id == "tables"


def test_guardless_call_is_byte_identical_to_pre_w42():
    """No classifier (kill-switch path) → the original behavior."""
    flows = [
        _flow("f-tab-1", "app/tables/page.tsx"),
        _flow("f-tab-2", "app/tables/[id]/page.tsx"),
    ]
    devs = [_dev("toolkit", ["app/tables/page.tsx",
                             "packages/toolkit/index.ts"])]
    pfs = [_pf("toolkit", ["packages/toolkit/index.ts"])]
    ufs: list = []
    tele = seed_route_group_journeys(ufs, devs, pfs, flows, list(_ROUTES))
    assert tele["seeded"] == 1 and ufs[0].product_feature_id == "toolkit"


# ── B69-v2 — same-(pf,resource) seed coalescence ─────────────────────────────


_TWIN_ROUTES = [
    # API-side conversations group (2 files) …
    {"pattern": "/api/teams/[teamId]/datarooms/[id]/conversations",
     "method": "GET", "surface_scope": "product",
     "file": "pages/api/teams/t/datarooms/d/conversations/index.ts"},
    {"pattern": "/api/teams/[teamId]/datarooms/[id]/conversations/[cId]",
     "method": "POST", "surface_scope": "product",
     "file": "pages/api/teams/t/datarooms/d/conversations/one.ts"},
    # … and the page-side conversations group (2 files), same noun.
    {"pattern": "/datarooms/[id]/conversations", "method": "PAGE",
     "surface_scope": "product",
     "file": "pages/datarooms/d/conversations/index.tsx"},
    {"pattern": "/datarooms/[id]/conversations/[cId]", "method": "PAGE",
     "surface_scope": "product",
     "file": "pages/datarooms/d/conversations/one.tsx"},
]


def _twin_scene(page_pf="datarooms"):
    flows = [
        _flow("f-api-1", "pages/api/teams/t/datarooms/d/conversations/index.ts"),
        _flow("f-api-2", "pages/api/teams/t/datarooms/d/conversations/one.ts"),
        _flow("f-pg-1", "pages/datarooms/d/conversations/index.tsx"),
        _flow("f-pg-2", "pages/datarooms/d/conversations/one.tsx"),
    ]
    ufs = []
    features = [
        _dev("datarooms", ["pages/api/teams/t/datarooms/d/conversations/index.ts",
                           "pages/api/teams/t/datarooms/d/conversations/one.ts"]),
        _dev(page_pf, ["pages/datarooms/d/conversations/index.tsx",
                       "pages/datarooms/d/conversations/one.tsx"]),
    ]
    pfs = [_pf("datarooms"), _pf("conversations")]
    return ufs, features, pfs, flows


def test_b69v2_same_pf_resource_seeds_coalesce():
    """The papermark-ON twin class: API-side + page-side groups of the SAME
    noun under the SAME PF are ONE journey — no twin, no parenthetical.
    (Armed via the explicit kwarg — finalize passes the family flag.)"""
    ufs, features, pfs, flows = _twin_scene()
    tele = seed_route_group_journeys(ufs, features, pfs, flows, _TWIN_ROUTES,
                                     coalesce_same_pf_resource=True)
    assert tele["holes"] == 2
    assert tele.get("coalesced") == 1
    assert tele["seeded"] == 1
    seed = ufs[-1]
    assert set(seed.member_flow_ids) == {
        "f-api-1", "f-api-2", "f-pg-1", "f-pg-2"}
    assert seed.member_count == 4
    assert "(" not in (seed.name or "")


def test_b69v2_cross_pf_same_noun_not_coalesced():
    """Anti-case: same noun, DIFFERENT homes — honest separate journeys."""
    ufs, features, pfs, flows = _twin_scene(page_pf="conversations")
    tele = seed_route_group_journeys(ufs, features, pfs, flows, _TWIN_ROUTES,
                                     coalesce_same_pf_resource=True)
    assert tele.get("coalesced", 0) == 0
    assert tele["seeded"] == 2


def test_b69v2_coalesce_off_byte_identical():
    """Kill-switch: kwarg unset/None ⇒ both twins seed exactly as
    pre-B69-v2 (second wears the dir-segment parenthetical at birth)."""
    ufs, features, pfs, flows = _twin_scene()
    tele = seed_route_group_journeys(ufs, features, pfs, flows, _TWIN_ROUTES)
    assert "coalesced" not in tele
    assert tele["seeded"] == 2
    names = sorted(str(u.name) for u in ufs[-2:])
    assert any("(" in n for n in names)  # the pre-B69v2 birth parenthetical


def test_b69v2_coalesce_deterministic():
    a = _twin_scene()
    b = _twin_scene()
    ta = seed_route_group_journeys(a[0], a[1], a[2], a[3], _TWIN_ROUTES,
                                   coalesce_same_pf_resource=True)
    tb = seed_route_group_journeys(b[0], b[1], b[2], b[3], _TWIN_ROUTES,
                                   coalesce_same_pf_resource=True)
    assert ta == tb
    assert [(u.name, u.member_flow_ids) for u in a[0]] == \
        [(u.name, u.member_flow_ids) for u in b[0]]


# ── B69-v2 commit 8 — seed intent from the group's own route methods ────────


_CRUD_ROUTES = [
    # the papermark faqs shape: a CRUD API group (GET list + write verbs)
    {"pattern": "/api/teams/[teamId]/datarooms/[id]/faqs", "method": "GET",
     "surface_scope": "product",
     "file": "pages/api/teams/t/datarooms/d/faqs/index.ts"},
    {"pattern": "/api/teams/[teamId]/datarooms/[id]/faqs", "method": "POST",
     "surface_scope": "product",
     "file": "pages/api/teams/t/datarooms/d/faqs/one.ts"},
    # a pure page group — the TRUE browse seed (anti-case)
    {"pattern": "/tables", "method": "PAGE", "surface_scope": "product",
     "file": "app/tables/page.tsx"},
    {"pattern": "/tables/[id]", "method": "PAGE", "surface_scope": "product",
     "file": "app/tables/[id]/page.tsx"},
]


def _intent_scene():
    flows = [
        _flow("f-faq-1", "pages/api/teams/t/datarooms/d/faqs/index.ts"),
        _flow("f-faq-2", "pages/api/teams/t/datarooms/d/faqs/one.ts"),
        _flow("f-tab-1", "app/tables/page.tsx"),
        _flow("f-tab-2", "app/tables/[id]/page.tsx"),
    ]
    features = [
        _dev("datarooms", ["pages/api/teams/t/datarooms/d/faqs/index.ts",
                           "pages/api/teams/t/datarooms/d/faqs/one.ts"]),
        _dev("tables", ["app/tables/page.tsx", "app/tables/[id]/page.tsx"]),
    ]
    pfs = [_pf("datarooms"), _pf("tables")]
    return [], features, pfs, flows


def test_b69v2_write_group_seed_intent_manage():
    """The ratified commit-8 unit: a manage-only (write-verb) group seeds
    intent='manage' while keeping the 'Browse & manage {r}' birth name —
    B31's recompose ladder can no longer mint the 'Browse & filter faqs'
    verb-lie from a hardcoded browse intent."""
    ufs, features, pfs, flows = _intent_scene()
    seed_route_group_journeys(ufs, features, pfs, flows, _CRUD_ROUTES,
                              derive_seed_intent=True)
    by_res = {str(u.resource): u for u in ufs}
    faqs_seed = by_res["faqs"]
    assert faqs_seed.intent == "manage"
    assert faqs_seed.name == "Browse & manage faqs"


def test_b69v2_true_browse_seed_stays_browse():
    """Anti-case: a read-class-only (PAGE) group is a genuine browse
    journey — intent stays 'browse'."""
    ufs, features, pfs, flows = _intent_scene()
    seed_route_group_journeys(ufs, features, pfs, flows, _CRUD_ROUTES,
                              derive_seed_intent=True)
    by_res = {str(u.resource): u for u in ufs}
    assert by_res["tables"].intent == "browse"


def test_b69v2_seed_intent_off_byte_identical():
    """Kill-switch: kwarg unset ⇒ every seed keeps the pre-B69-v2
    hardcoded 'browse' intent."""
    ufs, features, pfs, flows = _intent_scene()
    seed_route_group_journeys(ufs, features, pfs, flows, _CRUD_ROUTES)
    assert {str(u.intent) for u in ufs} == {"browse"}
