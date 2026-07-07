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
