"""Tests for the Layer 1 / Layer 2 schema split on FeatureMap.

Introduced 2026-05-18 on agent/layer1-dev-features-v1 as part of the
two-layer output rebuild. Covers:

  - Legacy ``features=[...]`` construction still works and defaults
    everything to ``layer="developer"``.
  - New ``developer_features=`` / ``product_features=`` construction
    folds into ``features`` and stamps the ``layer`` field.
  - JSON dump emits the layered top-level arrays AND the legacy
    ``features`` array.
  - Round-trip through ``model_dump`` → ``model_validate`` preserves
    both shapes.
  - ``scan_meta`` defaults to an empty dict and survives round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, FeatureMap


def _make_feature(name: str, layer: str = "developer") -> Feature:
    return Feature(
        name=name,
        paths=[f"src/{name}.py"],
        authors=["alice"],
        total_commits=3,
        bug_fixes=1,
        bug_fix_ratio=0.33,
        last_modified=datetime(2026, 5, 1, tzinfo=timezone.utc),
        health_score=80.0,
        layer=layer,  # type: ignore[arg-type]
    )


def _now() -> datetime:
    return datetime(2026, 5, 18, tzinfo=timezone.utc)


# ─────────────── Legacy construction ────────────────


def test_legacy_features_field_still_works() -> None:
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=_now(),
        total_commits=10,
        date_range_days=30,
        features=[_make_feature("auth"), _make_feature("billing")],
    )
    assert len(fm.features) == 2
    assert all(f.layer == "developer" for f in fm.features)
    assert fm.get_developer_features() == fm.features
    assert fm.get_product_features() == []
    assert fm.scan_meta == {}


def test_legacy_dump_emits_developer_features() -> None:
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=_now(),
        total_commits=10,
        date_range_days=30,
        features=[_make_feature("auth")],
    )
    data = fm.model_dump(mode="json")
    # Legacy ``features`` array stays for back-compat.
    assert "features" in data
    assert len(data["features"]) == 1
    # New layered arrays are present.
    assert "developer_features" in data
    assert "product_features" in data
    assert len(data["developer_features"]) == 1
    assert data["developer_features"][0]["name"] == "auth"
    assert data["product_features"] == []
    # scan_meta is always emitted.
    assert data["scan_meta"] == {}


# ─────────────── Layered construction ────────────────


def test_developer_features_input_stamps_layer() -> None:
    dev = [_make_feature("auth"), _make_feature("billing")]
    prod = [_make_feature("Account & Billing", layer="developer")]  # wrong on input
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=_now(),
        total_commits=10,
        date_range_days=30,
        developer_features=dev,
        product_features=prod,
    )
    # All three are merged into ``features``.
    assert len(fm.features) == 3
    # Layer field is stamped from the input bucket regardless of input value.
    by_name = {f.name: f for f in fm.features}
    assert by_name["auth"].layer == "developer"
    assert by_name["billing"].layer == "developer"
    assert by_name["Account & Billing"].layer == "product"


def test_layered_dump_round_trip() -> None:
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=_now(),
        total_commits=10,
        date_range_days=30,
        developer_features=[_make_feature("auth"), _make_feature("billing")],
        product_features=[_make_feature("Onboarding")],
        scan_meta={"stack": "next-app-router", "monorepo": False},
    )
    dumped = fm.model_dump(mode="json")
    assert len(dumped["developer_features"]) == 2
    assert len(dumped["product_features"]) == 1
    assert dumped["product_features"][0]["name"] == "Onboarding"
    assert dumped["scan_meta"]["stack"] == "next-app-router"
    # Round trip through JSON.
    blob = json.dumps(dumped)
    re_loaded = FeatureMap.model_validate(json.loads(blob))
    assert len(re_loaded.get_developer_features()) == 2
    assert len(re_loaded.get_product_features()) == 1
    assert re_loaded.scan_meta["stack"] == "next-app-router"


def test_input_aliases_dropped_from_dump() -> None:
    """The input-side ``developer_features=`` / ``product_features=``
    aliases must NOT round-trip as raw fields — only the computed
    arrays (re-derived from ``features``) survive.
    """
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=_now(),
        total_commits=10,
        date_range_days=30,
        developer_features=[_make_feature("auth")],
    )
    # Internal aliases were normalised away.
    assert fm.developer_features is None
    assert fm.product_features is None
    # Dump still emits the derived arrays — from ``features``.
    data = fm.model_dump(mode="json")
    assert len(data["developer_features"]) == 1
    assert data["developer_features"][0]["layer"] == "developer"


# ─────────────── Old-shape JSON reads back as developer features ────────────────


def test_old_shape_json_reads_as_developer_features() -> None:
    """Simulates an artifact written before the Layer 1/2 split:
    only ``features`` at the top level, no ``layer`` field on
    each entry. Reader must default everything to developer.
    """
    old_shape = {
        "repo_path": "/tmp/old",
        "remote_url": "",
        "analyzed_at": "2026-05-01T00:00:00+00:00",
        "total_commits": 5,
        "date_range_days": 30,
        "features": [
            {
                "name": "legacy_feature",
                "paths": ["src/foo.py"],
                "authors": ["alice"],
                "total_commits": 1,
                "bug_fixes": 0,
                "bug_fix_ratio": 0.0,
                "last_modified": "2026-05-01T00:00:00+00:00",
                "health_score": 90.0,
            },
        ],
    }
    fm = FeatureMap.model_validate(old_shape)
    assert len(fm.features) == 1
    assert fm.features[0].layer == "developer"
    assert fm.features[0].product_feature_id is None
    assert fm.get_developer_features() == fm.features
    assert fm.get_product_features() == []
    assert fm.scan_meta == {}


def test_product_feature_id_round_trip() -> None:
    dev = _make_feature("auth-jwt")
    dev.product_feature_id = "Account & Billing"
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=_now(),
        total_commits=1,
        date_range_days=30,
        developer_features=[dev],
    )
    data = fm.model_dump(mode="json")
    assert data["developer_features"][0]["product_feature_id"] == "Account & Billing"
    re_loaded = FeatureMap.model_validate(data)
    assert re_loaded.features[0].product_feature_id == "Account & Billing"


def test_invalid_layer_value_rejected() -> None:
    with pytest.raises(Exception):  # noqa: BLE001 — pydantic ValidationError shape varies
        Feature(
            name="x",
            paths=[],
            authors=[],
            total_commits=0,
            bug_fixes=0,
            bug_fix_ratio=0.0,
            last_modified=_now(),
            health_score=0.0,
            layer="middleware",  # type: ignore[arg-type]
        )
