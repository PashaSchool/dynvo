"""Product-Spine §4.4 — hub/child connector relation.

Detection (both arms + guards), PF binding (sibling parity, shared
pull-out, minting), the 6.7 Filter-B consumption, and kill-switches.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2.hub_relation import (
    apply_hub_pf_binding,
    detect_hub_relations,
    vendor_of_segment,
)
from faultline.pipeline_v2.stage_8_9_7_vendor_connector_split import (
    split_vendor_connectors,
)


def _feat(name: str, paths: list[str], *, layer: str = "developer",
          pfid: str | None = None, role: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=list(paths), authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0, layer=layer, product_feature_id=pfid, role=role,
    )


_EDR_FILES = [
    "backend/services/edr/base.py",
    "backend/services/edr/factory.py",
    "backend/services/edr/claroty.py",
    "backend/services/edr/cortex.py",
    "backend/services/edr/crowdstrike.py",
]


# ── detection ──────────────────────────────────────────────────────────────


def test_vendor_of_segment() -> None:
    assert vendor_of_segment("crowdstrike.py") == "crowdstrike"
    assert vendor_of_segment("airtable_wrapper") == "airtable"
    assert vendor_of_segment("gocardless") == "gocardless"
    assert vendor_of_segment("base.py") is None
    assert vendor_of_segment("okta_auth0_bridge.py") is None  # 2 vendors


def test_detect_vendor_majority_arm() -> None:
    """Soc0 shape: backend/services/edr — the dir segment itself names the
    capability; 3 vendor children are the majority."""
    hub_dev = _feat("edr", _EDR_FILES)
    hubs = detect_hub_relations([hub_dev])
    assert len(hubs) == 1
    h = hubs[0]
    assert h.hub_dir == "backend/services/edr"
    assert h.arm == "vendor-majority"
    assert h.hub_key == "edr"
    assert set(h.vendor_children) == {"claroty", "cortex", "crowdstrike"}
    assert h.member_dev_names == ["edr"]


def test_detect_lexicon_arm_apps_at_depth() -> None:
    """midday shape: …/routers/apps/{fortnox,gmail,slack}.ts — 'apps' at
    depth >= 1 is a hub even when vendor files are NOT the dir majority."""
    files = [
        "apps/api/src/rest/routers/apps/fortnox.ts",
        "apps/api/src/rest/routers/apps/gmail.ts",
        "apps/api/src/rest/routers/apps/slack.ts",
        "apps/api/src/rest/routers/apps/helpers.ts",
        "apps/api/src/rest/routers/apps/types.ts",
        "apps/api/src/rest/routers/apps/schema.ts",
    ]
    dev = _feat("apps-routers", files)
    hubs = detect_hub_relations([dev])
    assert [h.hub_dir for h in hubs] == ["apps/api/src/rest/routers/apps"]
    assert hubs[0].arm == "lexicon"
    assert set(hubs[0].vendor_children) == {"fortnox", "gmail", "slack"}


def test_detect_apps_workspace_root_is_not_a_hub() -> None:
    """Top-level apps/ is a workspace root, never a hub — even with
    vendor-named children."""
    files = [
        "apps/stripe/index.ts",
        "apps/slack/index.ts",
        "apps/github/index.ts",
    ]
    hubs = detect_hub_relations([_feat("apps", files)])
    assert all(h.hub_dir != "apps" for h in hubs)


def test_detect_requires_three_vendors() -> None:
    files = [
        "src/edr/base.py",
        "src/edr/crowdstrike.py",
        "src/edr/sentinelone.py",
    ]
    assert detect_hub_relations([_feat("edr", files)]) == []


def test_detect_membership_requires_majority_inside() -> None:
    """A big dev touching ONE hub file is not pulled into the relation."""
    hub_dev = _feat("edr", _EDR_FILES)
    big = _feat("backend", [
        "backend/main.py", "backend/app.py", "backend/db.py",
        "backend/services/edr/claroty.py",
    ])
    hubs = detect_hub_relations([hub_dev, big])
    assert hubs[0].member_dev_names == ["edr"]


def test_detect_child_devs_are_members() -> None:
    """Per-vendor child devs (the Soc0 edr-claroty class) join the hub."""
    parent = _feat("edr", _EDR_FILES[:2])  # base + factory only
    children = [
        _feat("edr-claroty", ["backend/services/edr/claroty.py"]),
        _feat("edr-cortex", ["backend/services/edr/cortex.py"]),
        _feat("edr-crowdstrike", ["backend/services/edr/crowdstrike.py"]),
    ]
    hubs = detect_hub_relations([parent, *children])
    assert len(hubs) == 1
    assert hubs[0].member_dev_names == [
        "edr", "edr-claroty", "edr-cortex", "edr-crowdstrike",
    ]


def test_detect_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_HUBS", "0")
    assert detect_hub_relations([_feat("edr", _EDR_FILES)]) == []


# ── PF binding ─────────────────────────────────────────────────────────────


def _shared_pf() -> Feature:
    return _feat("shared-platform",
                 ["backend/main.py", *_EDR_FILES], layer="product")


def test_binding_pulls_hub_out_of_shared_and_mints_pf() -> None:
    """All members in Shared Platform → a hub PF is minted, every member
    rebound to it (sibling parity), shared PF paths recomputed."""
    parent = _feat("edr", _EDR_FILES[:2], pfid="shared-platform")
    children = [
        _feat("edr-claroty", ["backend/services/edr/claroty.py"],
              pfid="shared-platform"),
        _feat("edr-cortex", ["backend/services/edr/cortex.py"],
              pfid="shared-platform"),
        _feat("edr-crowdstrike", ["backend/services/edr/crowdstrike.py"],
              pfid="shared-platform"),
    ]
    other_shared = _feat("backend", ["backend/main.py"],
                         pfid="shared-platform")
    features = [parent, *children, other_shared]
    pfs = [_shared_pf()]
    tele = apply_hub_pf_binding(features, pfs)
    assert tele["hubs"] == 1
    assert tele["pfs_minted"] == 1
    assert tele["devs_rebound"] == 4
    # Sibling parity: hub + every child on ONE (new) PF, none in shared.
    assert {f.product_feature_id for f in [parent, *children]} == {"edr"}
    minted = next(pf for pf in pfs if pf.name == "edr")
    assert minted.layer == "product"
    assert "backend/services/edr/claroty.py" in minted.paths
    # Donor shared PF recomputed: hub files gone, non-hub member kept.
    shared = next(pf for pf in pfs if pf.name == "shared-platform")
    assert "backend/services/edr/claroty.py" not in shared.paths
    assert "backend/main.py" in shared.paths
    # Non-member shared dev untouched.
    assert other_shared.product_feature_id == "shared-platform"


def test_binding_majority_real_pf_wins_over_shared() -> None:
    """midday class: some children on a real PF, siblings in shared →
    ALL land on the real PF (never split shared/PF)."""
    parent = _feat("providers", ["packages/banking/src/providers/index.ts"],
                   pfid="bank-sync")
    c1 = _feat("providers-gocardless",
               ["packages/banking/src/providers/gocardless/api.ts"],
               pfid="bank-sync")
    c2 = _feat("providers-plaid",
               ["packages/banking/src/providers/plaid/api.ts"],
               pfid="shared-platform")
    c3 = _feat("providers-stripe",
               ["packages/banking/src/providers/stripe/api.ts"],
               pfid="shared-platform")
    pf_real = _feat("bank-sync", [], layer="product")
    pf_shared = _feat("shared-platform", [], layer="product")
    dev_map: dict[str, list[str]] = {}
    tele = apply_hub_pf_binding(
        [parent, c1, c2, c3], [pf_real, pf_shared], dev_map,
    )
    assert tele["pfs_minted"] == 0
    assert {f.product_feature_id for f in (parent, c1, c2, c3)} == {"bank-sync"}
    assert dev_map["providers-plaid"] == ["bank-sync"]
    # Target PF's union now carries every member's files.
    assert "packages/banking/src/providers/plaid/api.ts" in pf_real.paths


def test_binding_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_HUBS", "0")
    parent = _feat("edr", _EDR_FILES, pfid="shared-platform")
    tele = apply_hub_pf_binding([parent], [_shared_pf()])
    assert tele["enabled"] is False
    assert parent.product_feature_id == "shared-platform"


def test_binding_facet_never_joins_hub() -> None:
    facet = _feat("analytics", _EDR_FILES, role="facet")
    hubs = detect_hub_relations([facet])
    assert hubs == []  # only a facet claims the dir → no members → no hub


# ── 8.9.7 hub-dir child grouping (dir-per-vendor layouts) ─────────────────


def test_split_uses_hub_dir_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vendor DIRECTORY children under a hub dir group per vendor even
    though the file stems (api.ts/config.ts) carry no vendor token."""
    monkeypatch.delenv("FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT", raising=False)
    files = [
        "packages/app-store/zoom/api.ts",
        "packages/app-store/zoom/config.ts",
        "packages/app-store/stripe/api.ts",
        "packages/app-store/stripe/config.ts",
        "packages/app-store/slack/api.ts",
        "packages/app-store/slack/config.ts",
    ]
    hub = _feat("app-store", files, pfid="pf-1")
    feats = [hub]
    res = split_vendor_connectors(
        feats, hub_dirs=("packages/app-store",),
    )
    assert res.hubs_split == 1
    names = {f.name for f in feats}
    assert {"app-store-zoom", "app-store-stripe", "app-store-slack"} <= names
    zoom = next(f for f in feats if f.name == "app-store-zoom")
    assert sorted(zoom.paths) == [
        "packages/app-store/zoom/api.ts",
        "packages/app-store/zoom/config.ts",
    ]
    assert zoom.product_feature_id == "pf-1"


# ── 6.7 Filter-B consumption ───────────────────────────────────────────────


def test_uf_clustering_per_vendor_under_hub() -> None:
    from faultline.pipeline_v2.stage_6_7_user_flows import cluster_user_flows

    def _flow(name: str, entry: str) -> dict:
        return {"name": name, "uuid": name, "entry_point_file": entry,
                "paths": [entry], "primary_feature": "edr",
                "secondary_features": [], "test_files": [],
                "coverage_pct": None}

    scan = {
        "flows": [
            # crowdstrike has TWO author-intent flows → its own journey.
            _flow("create-crowdstrike-alert-flow",
                  "backend/services/edr/crowdstrike.py"),
            _flow("update-crowdstrike-config-flow",
                  "backend/services/edr/crowdstrike.py"),
            # single-flow vendors → folded into one hub journey.
            _flow("create-claroty-alert-flow",
                  "backend/services/edr/claroty.py"),
            _flow("create-cortex-alert-flow",
                  "backend/services/edr/cortex.py"),
        ],
        "developer_features": [
            {"name": "edr", "product_feature_id": "edr", "paths": _EDR_FILES,
             "role": None},
        ],
    }
    result = cluster_user_flows(
        scan, hub_dirs=[("backend/services/edr", "edr")],
    )
    assert result["uf_hub_clustered"] == 4
    ufs = result["user_flows"]
    # Per-vendor journey for the recurring vendor (vendor-qualified
    # domain); the two singleton vendors folded into ONE hub journey.
    domains = {uf["domain"] for uf in ufs}
    assert domains == {"edr", "edr_crowdstrike"}
    crowd = next(uf for uf in ufs if uf["domain"] == "edr_crowdstrike")
    assert crowd["resource"] == "crowdstrike"
    assert crowd["member_count"] == 2
    assert len(ufs) == 2
