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


def test_off_by_default_noop(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    hub = _feat("edr", _EDR_PATHS)
    feats = [hub]
    res = split_vendor_connectors(feats)
    assert res.enabled is False
    assert res.hubs_split == 0
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
