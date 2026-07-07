"""Tests for Stage 8.9.7 — deterministic per-vendor connector split.

Covers: the EDR-shaped split (vendor stems + schema/<vendor>_* files split
per vendor, shared plumbing stays with the parent), every rail (OFF by
default, ≥2 vendors, majority share, anchor skip, multi-vendor file =
shared, name-collision skip), lineage/ownership contracts, and telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_8_9_7_vendor_connector_split import (
    split_vendor_connectors,
)

_ENV = "FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT"


def _feat(name, paths, description=None, uuid="u", layer="developer"):
    return Feature(
        name=name, display_name=name, description=description,
        paths=list(paths),
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0,
                                 primary=True) for p in paths],
        authors=["a"], total_commits=5, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        layer=layer, uuid=uuid, product_feature_id="pf-1",
    )


_EDR_PATHS = [
    "backend/services/edr/__init__.py",
    "backend/services/edr/base.py",
    "backend/services/edr/factory.py",
    "backend/services/edr/crowdstrike.py",
    "backend/services/edr/sentinelone.py",
    "backend/services/edr/defender.py",
    "backend/services/edr/schema/crowdstrike_baseline.py",
    "backend/services/edr/schema/sentinelone_schema.py",
    "backend/services/edr/schema/defender_schema.py",
]


def test_on_by_default_and_kill_switch(monkeypatch):
    """Default flipped ON by Product-Spine Wave 1 (§4.4, 2026-07-06);
    FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT=0 restores the historical no-op."""
    monkeypatch.delenv(_ENV, raising=False)
    res_on = split_vendor_connectors([_feat("edr", _EDR_PATHS)])
    assert res_on.enabled is True
    assert res_on.hubs_split == 1

    monkeypatch.setenv(_ENV, "0")
    hub = _feat("edr", _EDR_PATHS)
    feats = [hub]
    res_off = split_vendor_connectors(feats)
    assert res_off.enabled is False
    assert res_off.hubs_split == 0
    assert feats == [hub]


def test_edr_hub_splits_per_vendor(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    hub = _feat("edr", _EDR_PATHS, uuid="edr-uuid")
    feats = [hub]
    res = split_vendor_connectors(feats)
    assert res.hubs_split == 1
    assert res.connectors_created == 3
    assert res.files_moved == 6
    by_name = {f.name: f for f in feats}
    assert set(by_name) == {
        "edr", "edr-crowdstrike", "edr-sentinelone", "edr-defender",
    }
    cs = by_name["edr-crowdstrike"]
    assert sorted(cs.paths) == [
        "backend/services/edr/crowdstrike.py",
        "backend/services/edr/schema/crowdstrike_baseline.py",
    ]
    # lineage + product conservation
    assert cs.split_from == "edr-uuid"
    assert cs.product_feature_id == "pf-1"
    # ownership: children own their files (anchor/primary member rows)
    assert all(m.primary and m.role == "anchor" for m in cs.member_files)
    # parent keeps ONLY the shared plumbing, still owned
    assert sorted(by_name["edr"].paths) == [
        "backend/services/edr/__init__.py",
        "backend/services/edr/base.py",
        "backend/services/edr/factory.py",
    ]
    parent_member_paths = {m.path for m in by_name["edr"].member_files}
    assert "backend/services/edr/crowdstrike.py" not in parent_member_paths


def test_deterministic_output_order(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    feats1 = [_feat("edr", _EDR_PATHS)]
    feats2 = [_feat("edr", _EDR_PATHS)]
    split_vendor_connectors(feats1)
    split_vendor_connectors(feats2)
    assert [f.name for f in feats1] == [f.name for f in feats2]


def test_single_vendor_is_not_a_hub(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    feat = _feat("billing", [
        "src/billing/stripe.py", "src/billing/stripe_webhooks.py",
        "src/billing/invoice.py",
    ])
    feats = [feat]
    res = split_vendor_connectors(feats)
    assert res.hubs_split == 0
    assert len(feats) == 1


def test_minority_vendor_files_do_not_split(monkeypatch):
    """A product feature that merely TOUCHES two vendor SDKs keeps its
    grain — vendor files must be the majority of the footprint."""
    monkeypatch.setenv(_ENV, "1")
    feat = _feat("checkout", [
        "src/checkout/cart.py", "src/checkout/order.py",
        "src/checkout/payment.py", "src/checkout/shipping.py",
        "src/checkout/stripe.py", "src/checkout/paypal.py",
    ])
    feats = [feat]
    res = split_vendor_connectors(feats)
    assert res.hubs_split == 0


def test_workspace_anchor_never_splits(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat(
        "backend",
        ["backend/stripe.py", "backend/paypal.py"],
        description="workspace anchor 'backend' from monorepo package 'backend/'",
    )
    feats = [anchor]
    res = split_vendor_connectors(feats)
    assert res.hubs_split == 0


def test_multi_vendor_file_stays_with_parent(monkeypatch):
    """A file naming ≥2 vendors is shared plumbing, not a connector."""
    monkeypatch.setenv(_ENV, "1")
    feat = _feat("sso", [
        "src/sso/okta.py", "src/sso/auth0.py",
        "src/sso/okta_auth0_bridge.py",
    ])
    feats = [feat]
    split_vendor_connectors(feats)
    by_name = {f.name: f for f in feats}
    assert "src/sso/okta_auth0_bridge.py" in by_name["sso"].paths


def test_name_collision_skips_that_vendor(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    hub = _feat("edr", _EDR_PATHS)
    existing = _feat("edr-crowdstrike", ["other/place.py"], uuid="x")
    feats = [hub, existing]
    res = split_vendor_connectors(feats)
    assert res.collisions_skipped == 1
    minted = [f.name for f in feats if f.split_from]
    assert "edr-sentinelone" in minted and "edr-defender" in minted
    assert minted.count("edr-crowdstrike") == 0
    # the skipped vendor's files stay with the parent
    by_name = {f.name: f for f in feats if not f.split_from}
    assert "backend/services/edr/crowdstrike.py" in by_name["edr"].paths


def test_product_layer_untouched(monkeypatch):
    """Only developer-layer features are examined."""
    monkeypatch.setenv(_ENV, "1")
    pf = _feat("integrations", ["a/stripe.py", "a/paypal.py"], layer="product")
    feats = [pf]
    res = split_vendor_connectors(feats)
    assert res.features_examined == 0
    assert res.hubs_split == 0


def test_telemetry_sample_shape(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    feats = [_feat("edr", _EDR_PATHS)]
    res = split_vendor_connectors(feats)
    tele = res.as_telemetry()
    assert tele["enabled"] is True
    assert tele["hubs_split"] == 1
    assert tele["sample"][0]["source"] == "edr"
    assert tele["sample"][0]["connectors"] == [
        "edr-crowdstrike", "edr-defender", "edr-sentinelone",
    ]


def test_generic_container_feature_never_splits(monkeypatch):
    """`dialogs` holding per-vendor AwsDialog/TeamsDialog widgets is UI
    plumbing, not a connector hub (Soc0 replay 2026-07-05: splitting minted
    nine thin dialogs-<vendor> husks)."""
    monkeypatch.setenv(_ENV, "1")
    feat = _feat("dialogs", [
        "fe/src/features/integrations/dialogs/AwsDialog.tsx",
        "fe/src/features/integrations/dialogs/TeamsDialog.tsx",
        "fe/src/features/integrations/dialogs/GithubDialog.tsx",
    ])
    feats = [feat]
    res = split_vendor_connectors(feats)
    assert res.hubs_split == 0
    assert len(feats) == 1


# ── W1.1 — aggregate carve arm (member-less hubs) ───────────────────────

# The REAL midday layout (validation scan 2026-07-06): six vendor
# DIRECTORY children + one direct plumbing file under the hub, all inside
# the apps/api workspace-anchor aggregate whose footprint is dominated by
# non-hub code (the two historical rails both block the split for it).
_MIDDAY_HUB = "apps/api/src/rest/routers/apps"
_MIDDAY_HUB_PATHS = [
    f"{_MIDDAY_HUB}/fortnox/index.ts",
    f"{_MIDDAY_HUB}/fortnox/install-url.ts",
    f"{_MIDDAY_HUB}/fortnox/oauth-callback.ts",
    f"{_MIDDAY_HUB}/gmail/index.ts",
    f"{_MIDDAY_HUB}/gmail/install-url.ts",
    f"{_MIDDAY_HUB}/gmail/oauth-callback.ts",
    f"{_MIDDAY_HUB}/index.ts",
    f"{_MIDDAY_HUB}/outlook/index.ts",
    f"{_MIDDAY_HUB}/outlook/install-url.ts",
    f"{_MIDDAY_HUB}/outlook/oauth-callback.ts",
    f"{_MIDDAY_HUB}/quickbooks/index.ts",
    f"{_MIDDAY_HUB}/quickbooks/install-url.ts",
    f"{_MIDDAY_HUB}/quickbooks/oauth-callback.ts",
    f"{_MIDDAY_HUB}/slack/index.ts",
    f"{_MIDDAY_HUB}/slack/install-url.ts",
    f"{_MIDDAY_HUB}/slack/interactions.ts",
    f"{_MIDDAY_HUB}/slack/messages.ts",
    f"{_MIDDAY_HUB}/slack/oauth-callback.ts",
    f"{_MIDDAY_HUB}/slack/webhook.ts",
    f"{_MIDDAY_HUB}/xero/index.ts",
    f"{_MIDDAY_HUB}/xero/install-url.ts",
    f"{_MIDDAY_HUB}/xero/oauth-callback.ts",
]
_MIDDAY_EXTRA = [
    *[f"apps/api/src/trpc/routers/r{i}.ts" for i in range(30)],
    *[f"apps/api/src/rest/routers/d{i}.ts" for i in range(20)],
    "apps/api/Dockerfile",
    "apps/api/package.json",
]


def _midday_anchor():
    return _feat(
        "api", _MIDDAY_HUB_PATHS + _MIDDAY_EXTRA,
        description=(
            "[package] workspace anchor 'api' from monorepo package "
            "'apps/api' (package.json name='@midday/api')"
        ),
        uuid="api-uuid",
    )


def test_carve_arm_mints_per_vendor_children_from_anchor(monkeypatch):
    """midday defect (a): the carve arm pulls vendor DIRECTORY children of
    a member-less hub out of the covering workspace anchor — no footprint
    rail, anchors allowed. Direct files under the hub stay (plumbing)."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _midday_anchor()
    feats = [anchor]
    res = split_vendor_connectors(feats, carve_hub_dirs=(_MIDDAY_HUB,))
    assert res.aggregate_carves == 1
    assert res.carve_connectors_created == 6
    assert res.carve_files_moved == 21
    # The stem arm never acted (anchor) — historical counters untouched.
    assert res.hubs_split == 0
    assert res.connectors_created == 0
    by_name = {f.name: f for f in feats}
    assert set(by_name) == {
        "api", "api-fortnox", "api-gmail", "api-outlook",
        "api-quickbooks", "api-slack", "api-xero",
    }
    assert sorted(by_name["api-slack"].paths) == [
        f"{_MIDDAY_HUB}/slack/index.ts",
        f"{_MIDDAY_HUB}/slack/install-url.ts",
        f"{_MIDDAY_HUB}/slack/interactions.ts",
        f"{_MIDDAY_HUB}/slack/messages.ts",
        f"{_MIDDAY_HUB}/slack/oauth-callback.ts",
        f"{_MIDDAY_HUB}/slack/webhook.ts",
    ]
    # Files left the aggregate exactly once (no double-claim)…
    assert f"{_MIDDAY_HUB}/slack/index.ts" not in anchor.paths
    # …but the direct plumbing file and the non-hub footprint stayed.
    assert f"{_MIDDAY_HUB}/index.ts" in anchor.paths
    assert "apps/api/Dockerfile" in anchor.paths
    # Minted children inherit the aggregate's PF (binding refines later).
    assert by_name["api-fortnox"].product_feature_id == "pf-1"


def test_carve_arm_enables_hub_binding_end_to_end(monkeypatch):
    """detect(member-less) → carve → re-detect finds the minted members →
    binding mints ONE hub PF and stamps every child (sibling parity)."""
    from faultline.pipeline_v2.hub_relation import (
        apply_hub_pf_binding,
        detect_hub_relations,
    )

    monkeypatch.setenv(_ENV, "1")
    anchor = _midday_anchor()
    anchor.product_feature_id = "shared-platform"
    feats = [anchor]
    relations = detect_hub_relations(feats, include_memberless=True)
    assert [h.hub_dir for h in relations] == [_MIDDAY_HUB]
    assert relations[0].member_dev_names == []
    split_vendor_connectors(
        feats,
        hub_dirs=tuple(h.hub_dir for h in relations if h.member_dev_names),
        carve_hub_dirs=tuple(
            h.hub_dir for h in relations if not h.member_dev_names
        ),
    )
    pfs = []
    tele = apply_hub_pf_binding(feats, pfs)
    assert tele["hubs"] == 1
    assert tele["pfs_minted"] == 1
    assert [pf.name for pf in pfs] == ["apps"]
    children = [f for f in feats if f.name.startswith("api-")]
    assert len(children) == 6
    assert {c.product_feature_id for c in children} == {"apps"}
    # The aggregate itself is NOT majority-inside — it keeps its binding.
    assert anchor.product_feature_id == "shared-platform"


def test_carve_arm_ignores_direct_vendor_files(monkeypatch):
    """Dir-per-vendor evidence only: vendor-named FILES directly under a
    member-less hub are not carved (they stay stem-arm territory, where
    the footprint rail still guards SDK users)."""
    monkeypatch.setenv(_ENV, "1")
    paths = [
        "pkg/src/integrations/stripe.ts",
        "pkg/src/integrations/paypal.ts",
        "pkg/src/integrations/shopify.ts",
        *[f"pkg/src/other/f{i}.ts" for i in range(20)],
    ]
    dev = _feat(
        "pkg", paths,
        description="[package] workspace anchor 'pkg' from monorepo package 'pkg'",
    )
    feats = [dev]
    res = split_vendor_connectors(
        feats, carve_hub_dirs=("pkg/src/integrations",),
    )
    assert res.aggregate_carves == 0
    assert res.carve_connectors_created == 0
    assert len(feats) == 1


def test_carve_arm_facet_never_carved(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    dev = _midday_anchor()
    dev.role = "facet"
    feats = [dev]
    res = split_vendor_connectors(feats, carve_hub_dirs=(_MIDDAY_HUB,))
    assert res.aggregate_carves == 0
    assert len(feats) == 1


def test_carve_arm_respects_kill_switch(monkeypatch):
    monkeypatch.setenv(_ENV, "0")
    feats = [_midday_anchor()]
    res = split_vendor_connectors(feats, carve_hub_dirs=(_MIDDAY_HUB,))
    assert res.enabled is False
    assert res.aggregate_carves == 0
    assert len(feats) == 1


def test_carve_arm_deterministic(monkeypatch):
    """Two runs over equal inputs mint identical names in identical order."""
    monkeypatch.setenv(_ENV, "1")

    def run():
        feats = [_midday_anchor()]
        split_vendor_connectors(feats, carve_hub_dirs=(_MIDDAY_HUB,))
        return [(f.name, tuple(sorted(f.paths))) for f in feats]

    assert run() == run()


def test_carve_arm_no_op_without_carve_dirs(monkeypatch):
    """No carve dirs → byte-identical to the historical behavior: the
    anchor is skipped outright."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _midday_anchor()
    feats = [anchor]
    res = split_vendor_connectors(feats)
    assert res.aggregate_carves == 0
    assert res.hubs_split == 0
    assert len(feats) == 1
    assert len(anchor.paths) == len(_MIDDAY_HUB_PATHS) + len(_MIDDAY_EXTRA)


def test_carve_telemetry_omitted_when_inactive(monkeypatch):
    """No member-less hubs → the telemetry dict stays byte-identical to
    pre-W1.1 (no carve keys)."""
    monkeypatch.setenv(_ENV, "1")
    res = split_vendor_connectors([_feat("edr", _EDR_PATHS)])
    tele = res.as_telemetry()
    assert "aggregate_carves" not in tele
    assert "carve_sample" not in tele


# ── D4 keyed husk floor (debt-pack) ──────────────────────────────────
# w31x honest-anomaly 3: comp's `aws-/azure-/gcp-(integration)` twins
# (logo.tsx + config.ts, 0 owned LOC, 0 UFs) minted through the keyed
# 8.9.x channel straight past the 6.86 anchored-mint floor. With
# repo_root passed, a flowless vendor group under
# _HUB_HUSK_LOC_FLOOR (150) folds into the parent instead of minting.

from datetime import datetime as _dt, timezone as _tz

from faultline.models.types import Flow as _Flow

_HUB = "packages/integrations/src"


def _write(root, rel, lines):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"const x{i} = {i};\n" for i in range(lines)))


def _comp_hub(tmp_path, vendors=("aws", "azure", "gcp"), lines=15):
    """comp-shaped integrations hub: per-vendor logo.tsx + config.ts."""
    root = tmp_path / "repo"
    paths = []
    for v in vendors:
        for fn in ("logo.tsx", "config.ts"):
            rel = f"{_HUB}/{v}/{fn}"
            _write(root, rel, lines)
            paths.append(rel)
    return root, paths


def test_husk_floor_folds_comp_shaped_twins(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV, "1")
    root, paths = _comp_hub(tmp_path)  # 30 LOC per vendor — under 150
    hub = _feat("integrations", paths, uuid="hub-uuid")
    feats = [hub]
    res = split_vendor_connectors(
        feats, hub_dirs=(_HUB,), repo_root=root)
    assert res.connectors_created == 0
    assert res.husk_folds == 3
    assert [s["vendor"] for s in res.husk_fold_sample] == ["aws", "azure", "gcp"]
    assert feats == [hub]                      # nothing minted
    assert sorted(hub.paths) == sorted(paths)  # files merged back (kept)
    tele = res.as_telemetry()
    assert tele["husk_folds"] == 3


def test_husk_floor_spares_real_code_connectors(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV, "1")
    root, paths = _comp_hub(tmp_path, vendors=("aws", "azure"))
    big = f"{_HUB}/slack/client.ts"
    _write(root, big, 200)  # 200 LOC — above the floor
    slack_logo = f"{_HUB}/slack/logo.tsx"
    _write(root, slack_logo, 5)
    hub = _feat("integrations", paths + [big, slack_logo], uuid="hub-uuid")
    feats = [hub]
    res = split_vendor_connectors(feats, hub_dirs=(_HUB,), repo_root=root)
    assert res.husk_folds == 2                # aws + azure folded
    assert res.connectors_created == 1        # slack minted
    minted = [f for f in feats if f is not hub]
    assert [f.name for f in minted] == ["integrations-slack"]
    assert sorted(minted[0].paths) == sorted([big, slack_logo])


def test_husk_floor_flow_evidence_rescues_thin_group(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV, "1")
    root, paths = _comp_hub(tmp_path)
    hub = _feat("integrations", paths, uuid="hub-uuid")
    hub.flows = [_Flow(
        name="connect-aws", entry_point_file=f"{_HUB}/aws/config.ts",
        paths=[f"{_HUB}/aws/config.ts"], authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_dt(2026, 1, 1, tzinfo=_tz.utc), health_score=100.0,
    )]
    feats = [hub]
    res = split_vendor_connectors(feats, hub_dirs=(_HUB,), repo_root=root)
    # aws carries flow evidence → mints despite 30 LOC; azure/gcp fold
    assert res.connectors_created == 1
    assert res.husk_folds == 2
    assert any(f.name == "integrations-aws" for f in feats)


def test_husk_floor_off_without_repo_root(monkeypatch, tmp_path):
    """repo_root=None (replay / old callers) keeps historical minting."""
    monkeypatch.setenv(_ENV, "1")
    _root, paths = _comp_hub(tmp_path)
    hub = _feat("integrations", paths, uuid="hub-uuid")
    feats = [hub]
    res = split_vendor_connectors(feats, hub_dirs=(_HUB,))
    assert res.connectors_created == 3
    assert res.husk_folds == 0
    assert "husk_folds" not in res.as_telemetry()


def test_husk_floor_applies_to_carve_arm(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV, "1")
    root, paths = _comp_hub(tmp_path)
    plumbing = f"{_HUB}/index.ts"
    _write(root, plumbing, 5)
    cover = _feat("api", paths + [plumbing], uuid="cover-uuid")
    feats = [cover]
    res = split_vendor_connectors(
        feats, carve_hub_dirs=(_HUB,), repo_root=root)
    assert res.carve_connectors_created == 0
    assert res.husk_folds == 3
    assert feats == [cover]
