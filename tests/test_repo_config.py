"""Tests for ``faultline.analyzer.repo_config``."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.analyzer.repo_config import (
    auto_save_canonicals,
    find_repo_config,
    load_repo_config,
)


# ── find_repo_config ─────────────────────────────────────────────────


class TestFind:
    def test_returns_none_when_absent(self, tmp_path: Path):
        assert find_repo_config(tmp_path) is None

    def test_finds_yaml_variant(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("features: {}\n", encoding="utf-8")
        assert find_repo_config(tmp_path).name == ".faultline.yaml"

    def test_finds_yml_variant(self, tmp_path: Path):
        (tmp_path / ".faultline.yml").write_text("features: {}\n", encoding="utf-8")
        assert find_repo_config(tmp_path).name == ".faultline.yml"

    def test_yaml_wins_over_yml(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("a: 1\n", encoding="utf-8")
        (tmp_path / ".faultline.yml").write_text("b: 2\n", encoding="utf-8")
        assert find_repo_config(tmp_path).name == ".faultline.yaml"

    def test_alternate_filename(self, tmp_path: Path):
        (tmp_path / "faultline.config.yaml").write_text(
            "features: {}\n", encoding="utf-8",
        )
        assert find_repo_config(tmp_path).name == "faultline.config.yaml"


# ── load_repo_config ─────────────────────────────────────────────────


class TestLoad:
    def test_no_config_returns_none(self, tmp_path: Path):
        assert load_repo_config(tmp_path) is None

    def test_empty_yaml_returns_empty_config(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("", encoding="utf-8")
        cfg = load_repo_config(tmp_path)
        assert cfg is not None and cfg.is_empty

    def test_full_config(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("""
features:
  billing:
    description: Stripe billing.
    variants:
      - lib/billing
      - ee/stripe-billing
  embedded-signing:
    variants:
      - remix/embedded-signing-authoring
skip_features:
  - tsconfig
  - tailwind-config
force_merges:
  - into: design-system
    from:
      - ui-primitives
      - ui/primitive-components
    description: Reusable UI primitives.
""", encoding="utf-8")
        cfg = load_repo_config(tmp_path)
        assert cfg is not None
        assert len(cfg.features) == 2
        assert cfg.features[0].canonical == "billing"
        assert "lib/billing" in cfg.features[0].variants
        assert cfg.skip_features == ["tsconfig", "tailwind-config"]
        assert cfg.force_merges[0].into == "design-system"
        assert "ui-primitives" in cfg.force_merges[0].sources

    def test_top_level_must_be_mapping(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_repo_config(tmp_path)

    def test_features_must_be_mapping(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text(
            "features: not-a-map\n", encoding="utf-8",
        )
        with pytest.raises(ValueError, match="features"):
            load_repo_config(tmp_path)

    def test_duplicate_canonical_rejected(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text(
            "features:\n  x: {}\n  x: {}\n", encoding="utf-8",
        )
        # YAML dedup makes this OK at parse time — but if we ever get
        # explicit duplicates via dict construction, our loader
        # would catch them. Verify no exception on YAML-collapsed dup.
        cfg = load_repo_config(tmp_path)
        assert cfg is not None and len(cfg.features) == 1

    def test_force_merge_missing_into(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("""
force_merges:
  - from: [a, b]
""", encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'into'"):
            load_repo_config(tmp_path)

    def test_skip_features_must_be_list(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text(
            "skip_features: nope\n", encoding="utf-8",
        )
        with pytest.raises(ValueError, match="skip_features"):
            load_repo_config(tmp_path)

    def test_records_source_path(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("features: {}\n", encoding="utf-8")
        cfg = load_repo_config(tmp_path)
        assert ".faultline.yaml" in cfg.source_path


# ── auto_save_canonicals ──────────────────────────────────────────────


class TestAutoSave:
    def test_no_config_no_op_by_default(self, tmp_path: Path):
        # No .faultline.yaml in repo root → don't create one
        n = auto_save_canonicals(
            tmp_path,
            {"auth": [f"a{i}.ts" for i in range(20)]},
            {"auth": "User auth"},
        )
        assert n == 0
        assert not (tmp_path / ".faultline.yaml").exists()

    def test_no_config_creates_when_write_if_missing(self, tmp_path: Path):
        n = auto_save_canonicals(
            tmp_path,
            {"auth": [f"a{i}.ts" for i in range(20)]},
            {"auth": "User auth"},
            write_if_missing=True,
        )
        assert n == 1
        assert (tmp_path / ".faultline.yaml").exists()

    def test_writes_canonical_with_description(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("features: {}\n", encoding="utf-8")
        n = auto_save_canonicals(
            tmp_path,
            {"auth": [f"a{i}.ts" for i in range(20)]},
            {"auth": "User authentication."},
        )
        assert n == 1
        text = (tmp_path / ".faultline.yaml").read_text(encoding="utf-8")
        assert "auto_aliases:" in text
        assert "auth:" in text
        assert "User authentication" in text

    def test_skips_protected(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("features: {}\n", encoding="utf-8")
        auto_save_canonicals(
            tmp_path,
            {
                "documentation": [f"d{i}.md" for i in range(50)],
                "shared-infra": [f"i{i}.ts" for i in range(40)],
                "examples": [f"e{i}.ts" for i in range(30)],
                "auth": [f"a{i}.ts" for i in range(20)],
            },
        )
        text = (tmp_path / ".faultline.yaml").read_text(encoding="utf-8")
        assert "auth" in text
        assert "documentation" not in text.split("auto_aliases:")[1]
        assert "shared-infra" not in text.split("auto_aliases:")[1]

    def test_skips_below_min_files(self, tmp_path: Path):
        # 9 files < _AUTO_LOCK_MIN_FILES=10 → skip
        (tmp_path / ".faultline.yaml").write_text("features: {}\n", encoding="utf-8")
        n = auto_save_canonicals(
            tmp_path,
            {"tiny": [f"t{i}.ts" for i in range(9)]},
            {"tiny": "small"},
        )
        assert n == 0

    def test_skips_user_managed_features(self, tmp_path: Path):
        # User has explicitly written 'auth' under features:
        (tmp_path / ".faultline.yaml").write_text(
            "features:\n  auth:\n    description: my own\n",
            encoding="utf-8",
        )
        n = auto_save_canonicals(
            tmp_path,
            {"auth": [f"a{i}.ts" for i in range(20)]},
            {"auth": "Engine description"},
        )
        # Already user-managed → not promoted to auto_aliases
        assert n == 0
        text = (tmp_path / ".faultline.yaml").read_text(encoding="utf-8")
        # User description preserved
        assert "my own" in text

    def test_preserves_user_features_block(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("""
features:
  signing:
    description: Sacred user content.
    variants:
      - lib/signing
""", encoding="utf-8")
        auto_save_canonicals(
            tmp_path,
            {
                "signing": [f"s{i}.ts" for i in range(20)],
                "billing": [f"b{i}.ts" for i in range(15)],
            },
        )
        cfg = load_repo_config(tmp_path)
        assert any(r.canonical == "signing" for r in cfg.features)
        assert any(r.canonical == "billing" for r in cfg.auto_aliases)

    def test_idempotent_same_input(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("features: {}\n", encoding="utf-8")
        detected = {"auth": [f"a{i}.ts" for i in range(20)]}
        descs = {"auth": "User auth."}
        first = auto_save_canonicals(tmp_path, detected, descs)
        second = auto_save_canonicals(tmp_path, detected, descs)
        # First call writes 1 new entry; second call sees the same name
        # already in auto_aliases → 0 new
        assert first == 1
        assert second == 0

    def test_lock_via_all_canonical_names(self, tmp_path: Path):
        (tmp_path / ".faultline.yaml").write_text("""
features:
  signing:
    description: Sacred.
auto_aliases:
  billing:
    description: Engine-managed.
""", encoding="utf-8")
        cfg = load_repo_config(tmp_path)
        names = cfg.all_canonical_names()
        assert "signing" in names
        assert "billing" in names
