"""Tests for the universal package-anchor extractor (Phase 3c)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.extractors.package_anchor import (
    PackageAnchorExtractor,
    collect_anchors,
    find_manifests,
    load_anchor_map,
)
from faultline.protocols import Extractor


# ── Helpers ──────────────────────────────────────────────────────────


def _write_pkg(tmp: Path, deps: dict, peer: dict | None = None,
               dev: dict | None = None) -> None:
    pkg = {"name": "tiny", "version": "0.1.0", "dependencies": deps}
    if peer:
        pkg["peerDependencies"] = peer
    if dev:
        pkg["devDependencies"] = dev
    (tmp / "package.json").write_text(json.dumps(pkg))


@pytest.fixture
def tiny_anchors(tmp_path):
    """Minimal anchor map fixture."""
    yaml_text = """
billing:
  must:
    - stripe
authentication:
  must:
    - next-auth
testing_infra:
  ignore:
    - vitest
"""
    p = tmp_path / "anchors.yaml"
    p.write_text(yaml_text)
    return load_anchor_map(p)


# ── Anchor map loading ───────────────────────────────────────────────


def test_load_anchor_map_returns_dep_to_category(tiny_anchors):
    assert "stripe" in tiny_anchors
    assert tiny_anchors["stripe"]["category"] == "billing"
    assert tiny_anchors["stripe"]["severity"] == "must"
    assert "next-auth" in tiny_anchors


def test_load_anchor_map_drops_ignored_deps(tiny_anchors):
    assert "vitest" not in tiny_anchors


def test_load_anchor_map_missing_file_returns_empty(tmp_path):
    assert load_anchor_map(tmp_path / "missing.yaml") == {}


# ── Manifest discovery ──────────────────────────────────────────────


def test_find_manifests_includes_package_json(tmp_path):
    _write_pkg(tmp_path, {"stripe": "14.0.0"})
    manifests = find_manifests(tmp_path)
    names = {m.name for m in manifests}
    assert "package.json" in names


def test_find_manifests_skips_node_modules(tmp_path):
    _write_pkg(tmp_path, {"x": "1.0.0"})
    nm = tmp_path / "node_modules" / "stripe"
    nm.mkdir(parents=True)
    _write_pkg(nm, {"buried": "1.0.0"})
    manifests = find_manifests(tmp_path)
    assert all("node_modules" not in str(m) for m in manifests)


def test_find_manifests_supports_workspace_dirs(tmp_path):
    _write_pkg(tmp_path, {})
    apps_web = tmp_path / "apps" / "web"
    apps_web.mkdir(parents=True)
    _write_pkg(apps_web, {"stripe": "14.0.0"})
    manifests = find_manifests(tmp_path)
    assert any("apps/web" in str(m) for m in manifests)


# ── Anchor collection ───────────────────────────────────────────────


def test_collect_anchors_emits_billing_for_stripe(tmp_path, tiny_anchors):
    _write_pkg(tmp_path, {"stripe": "14.0.0"})
    anchors = collect_anchors(tmp_path, anchor_map=tiny_anchors)
    assert len(anchors) == 1
    assert anchors[0].feature_category == "billing"
    assert anchors[0].severity == "must"
    assert anchors[0].dep_name == "stripe"


def test_collect_anchors_skips_dev_deps(tmp_path, tiny_anchors):
    _write_pkg(tmp_path, {}, dev={"stripe": "14.0.0"})
    anchors = collect_anchors(tmp_path, anchor_map=tiny_anchors)
    assert anchors == []


def test_collect_anchors_includes_peer_deps(tmp_path, tiny_anchors):
    _write_pkg(tmp_path, {}, peer={"next-auth": "5.0.0"})
    anchors = collect_anchors(tmp_path, anchor_map=tiny_anchors)
    assert len(anchors) == 1
    assert anchors[0].feature_category == "authentication"


def test_collect_anchors_dedupes_dep_per_manifest(tmp_path, tiny_anchors):
    _write_pkg(tmp_path, {"stripe": "14.0.0", "next-auth": "5.0.0"})
    anchors = collect_anchors(tmp_path, anchor_map=tiny_anchors)
    assert len(anchors) == 2
    cats = {a.feature_category for a in anchors}
    assert cats == {"billing", "authentication"}


def test_collect_anchors_walks_workspaces(tmp_path, tiny_anchors):
    _write_pkg(tmp_path, {})
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    _write_pkg(web, {"stripe": "14.0.0"})
    anchors = collect_anchors(tmp_path, anchor_map=tiny_anchors)
    assert len(anchors) == 1
    assert anchors[0].manifest.endswith("apps/web/package.json")


# ── Manifest format coverage ─────────────────────────────────────────


def test_pyproject_toml_parsing(tmp_path, tiny_anchors):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["stripe>=14.0.0"]\n'
    )
    # tiny_anchors only has stripe→billing for "stripe" dep
    anchors = collect_anchors(tmp_path, anchor_map=tiny_anchors)
    assert any(a.feature_category == "billing" for a in anchors)


def test_gemfile_parsing(tmp_path):
    (tmp_path / "Gemfile").write_text(
        'source "https://rubygems.org"\ngem "sidekiq"\ngem "stripe"\n'
    )
    yaml_text = (
        "background_jobs:\n  must:\n    - sidekiq\n"
        "billing:\n  must:\n    - stripe\n"
    )
    amap_path = tmp_path / "anchors.yaml"
    amap_path.write_text(yaml_text)
    anchors = collect_anchors(tmp_path, anchor_map=load_anchor_map(amap_path))
    cats = {a.feature_category for a in anchors}
    assert "background_jobs" in cats
    assert "billing" in cats


def test_cargo_toml_parsing(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"x\"\n[dependencies]\nstripe-rust = \"0.1\"\n"
    )
    yaml_text = "billing:\n  must:\n    - stripe-rust\n"
    amap_path = tmp_path / "anchors.yaml"
    amap_path.write_text(yaml_text)
    anchors = collect_anchors(tmp_path, anchor_map=load_anchor_map(amap_path))
    assert len(anchors) == 1
    assert anchors[0].dep_name == "stripe-rust"


# ── Extractor wrapper ────────────────────────────────────────────────


def test_extractor_satisfies_protocol():
    e = PackageAnchorExtractor()
    assert isinstance(e, Extractor)


def test_extractor_emits_signals(tmp_path, tiny_anchors):
    _write_pkg(tmp_path, {"stripe": "14.0.0"})
    e = PackageAnchorExtractor(anchor_map=tiny_anchors)
    signals = e.extract(tmp_path, files=[])
    assert len(signals) == 1
    s = signals[0]
    assert s.kind == "expected-feature"
    assert s.payload["feature_category"] == "billing"
    assert s.payload["severity"] == "must"
    assert "dep:stripe" in s.payload["evidence"]
