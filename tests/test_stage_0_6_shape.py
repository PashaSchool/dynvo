"""Tests for ``faultline.pipeline_v2.stage_0_6_shape``.

Covers:
  - ShapeSignals.collect (structural read, no .md reads)
  - Each of the 7 classifiers (table-driven, 5+ cases each)
  - The dispatcher (priority order, fallback, error degrade,
    artifact write toggle, pure-function contract)
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from faultline.pipeline_v2.stage_0_6_shape import (
    FALLBACK_CONFIDENCE,
    MIN_CONFIDENCE,
    BackendMonolithClassifier,
    ClassificationResult,
    CliToolClassifier,
    FrameworkRepoClassifier,
    GoLibraryClassifier,
    GoServerClassifier,
    OssLibraryClassifier,
    RustWorkspaceClassifier,
    ShapeSignals,
    SingleSaasRoutedClassifier,
    TurborepoMonorepoClassifier,
    UniversalResidualClassifier,
    classify_repo_shape,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_signals(**overrides) -> ShapeSignals:
    """Build a ShapeSignals with sensible defaults; override per test."""
    defaults = dict(
        audited_stack=None,
        stage_0_stack=None,
        secondary_stacks=(),
        monorepo=False,
        workspace_count=0,
        workspace_names=(),
        extractor_hints=(),
        has_package_json=False,
        has_pnpm_workspace=False,
        has_turbo_json=False,
        has_pyproject=False,
        has_cargo_toml=False,
        has_cargo_workspace=False,
        has_go_mod=False,
        has_gemfile=False,
        has_composer_json=False,
        has_app_router_dir=False,
        has_pages_router_dir=False,
        has_rails_app_dir=False,
        has_django_manage_py=False,
        has_fastapi_app_factory=False,
        has_remix_routes_dir=False,
        has_laravel_controllers_dir=False,
        has_bin_dir=False,
        has_cmd_dir=False,
        has_cli_py_entry=False,
        has_main_rs_bin=False,
        package_json_main_or_exports=False,
        package_json_no_app_entry=True,
        pyproject_has_project_section=False,
        cargo_is_single_crate=False,
        workspace_has_apps_dir=False,
        workspace_has_packages_dir=False,
        is_framework_self_repo=False,
        # Sprint S6.2 — Extension 1/2/3/4 fields.
        has_go_top_level_files=False,
        has_go_cmd_with_main=False,
        has_go_server_dir=False,
        cargo_workspace_member_count=0,
        has_split_fullstack_frontend_backend=False,
        has_packages_only_workspace=False,
        packages_only_count=0,
        is_subdir_scan=False,
        parent_git_root=None,
        package_json_has_react_dep=False,
        package_json_has_vue_dep=False,
        package_json_has_vite_dep=False,
        has_src_pages_or_routes_dir=False,
        parent_shape="",  # Sprint S10
    )
    defaults.update(overrides)
    return ShapeSignals(**defaults)


def _make_ctx(
    tmp_path: Path | None = None,
    *,
    stack: str | None = None,
    audited_stack: str | None = None,
    monorepo: bool = False,
    workspaces: list[Workspace] | None = None,
    extractor_hints: tuple[str, ...] = (),
    secondary_stacks: tuple[str, ...] = (),
) -> ScanContext:
    return ScanContext(
        repo_path=Path(tmp_path) if tmp_path else Path("/tmp/__nonexistent_test__"),
        stack=stack,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=[],
        commits=[],
        audited_stack=audited_stack,
        secondary_stacks=secondary_stacks,
        extractor_hints=extractor_hints,
        run_dir=None,
    )


@pytest.fixture
def make_tmp_repo(tmp_path):
    def _make(files: dict[str, str]) -> Path:
        for rel, content in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return tmp_path
    return _make


# ── TurborepoMonorepoClassifier ──────────────────────────────────────


class TestTurborepoMonorepoClassifier:
    clf = TurborepoMonorepoClassifier()

    def test_canonical_apps_and_packages_conf_095(self):
        sig = _make_signals(
            monorepo=True,
            has_turbo_json=True,
            workspace_count=4,
            workspace_has_apps_dir=True,
            workspace_has_packages_dir=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None
        assert r.shape == "turborepo-monorepo"
        assert r.confidence == 0.95
        assert "4 workspaces" in r.rationale

    def test_pnpm_only_with_canonical_layout(self):
        sig = _make_signals(
            monorepo=True,
            has_pnpm_workspace=True,
            workspace_count=3,
            workspace_has_apps_dir=True,
            workspace_has_packages_dir=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.95

    def test_apps_only_layout_conf_085(self):
        sig = _make_signals(
            monorepo=True,
            has_turbo_json=True,
            workspace_count=2,
            workspace_has_apps_dir=True,
            workspace_has_packages_dir=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.85

    def test_flat_workspaces_no_apps_no_packages_conf_070(self):
        sig = _make_signals(
            monorepo=True,
            has_pnpm_workspace=True,
            workspace_count=5,
            workspace_has_apps_dir=False,
            workspace_has_packages_dir=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.70

    def test_single_workspace_does_not_fire(self):
        sig = _make_signals(
            monorepo=True,
            has_turbo_json=True,
            workspace_count=1,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_non_monorepo_does_not_fire(self):
        sig = _make_signals(monorepo=False, has_package_json=True)
        assert self.clf.classify(_make_ctx(), sig) is None


# ── OssLibraryClassifier ─────────────────────────────────────────────


class TestOssLibraryClassifier:
    clf = OssLibraryClassifier()

    def test_js_library_canonical_conf_090(self):
        sig = _make_signals(
            has_package_json=True,
            package_json_main_or_exports=True,
            package_json_no_app_entry=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None
        assert r.shape == "oss-library"
        assert r.confidence == 0.90
        assert "JS library" in r.rationale

    def test_python_library_canonical(self):
        sig = _make_signals(
            has_pyproject=True,
            pyproject_has_project_section=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "oss-library"
        assert "Python library" in r.rationale

    def test_rust_library_canonical(self):
        sig = _make_signals(
            has_cargo_toml=True,
            cargo_is_single_crate=True,
            has_main_rs_bin=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "oss-library"
        assert "Rust library" in r.rationale

    def test_js_library_with_bin_dir_borderline_conf_075(self):
        sig = _make_signals(
            has_package_json=True,
            package_json_main_or_exports=True,
            package_json_no_app_entry=True,
            has_bin_dir=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.75
        assert "CLI binary" in r.rationale

    def test_django_app_does_not_fire(self):
        sig = _make_signals(
            has_pyproject=True,
            pyproject_has_project_section=True,
            has_django_manage_py=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_monorepo_does_not_fire(self):
        sig = _make_signals(
            monorepo=True,
            has_package_json=True,
            package_json_main_or_exports=True,
            package_json_no_app_entry=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None


# ── BackendMonolithClassifier ────────────────────────────────────────


class TestBackendMonolithClassifier:
    clf = BackendMonolithClassifier()

    def test_rails_canonical(self):
        sig = _make_signals(has_gemfile=True, has_rails_app_dir=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "backend-monolith"
        assert "Rails" in r.rationale

    def test_django_canonical(self):
        sig = _make_signals(has_django_manage_py=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and "Django" in r.rationale

    def test_laravel_canonical(self):
        sig = _make_signals(
            has_composer_json=True,
            has_laravel_controllers_dir=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and "Laravel" in r.rationale

    def test_rails_without_gemfile_does_not_fire(self):
        sig = _make_signals(has_rails_app_dir=True, has_gemfile=False)
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_monorepo_does_not_fire(self):
        sig = _make_signals(monorepo=True, has_django_manage_py=True)
        assert self.clf.classify(_make_ctx(), sig) is None


# ── CliToolClassifier ────────────────────────────────────────────────


class TestCliToolClassifier:
    clf = CliToolClassifier()

    def test_go_cmd_canonical(self):
        sig = _make_signals(
            has_cmd_dir=True,
            has_go_mod=True,
            audited_stack="go-cli",
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "cli-tool"
        assert "cmd/" in r.rationale

    def test_python_cli_canonical(self):
        sig = _make_signals(
            has_cli_py_entry=True,
            has_pyproject=True,
            pyproject_has_project_section=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "cli-tool"

    def test_rust_binary_canonical(self):
        sig = _make_signals(
            has_main_rs_bin=True,
            cargo_is_single_crate=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "cli-tool"

    def test_js_bin_only_canonical(self):
        sig = _make_signals(
            has_package_json=True,
            has_bin_dir=True,
            package_json_no_app_entry=True,
            package_json_main_or_exports=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "cli-tool"

    def test_mixed_bin_and_library_does_not_fire(self):
        # Has library exports AND bin/ — OssLibrary will take precedence
        # in the dispatcher; CliToolClassifier itself returns None here
        # because the JS branch requires NOT package_json_main_or_exports.
        sig = _make_signals(
            has_package_json=True,
            has_bin_dir=True,
            package_json_no_app_entry=True,
            package_json_main_or_exports=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None


# ── FrameworkRepoClassifier ──────────────────────────────────────────


class TestFrameworkRepoClassifier:
    clf = FrameworkRepoClassifier()

    def test_fires_when_hint_present(self):
        sig = _make_signals(is_framework_self_repo=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "framework-repo"
        assert r.confidence == 0.85

    def test_does_not_fire_without_hint(self):
        sig = _make_signals(is_framework_self_repo=False)
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_does_not_fire_when_only_pyproject_present(self):
        sig = _make_signals(
            has_pyproject=True,
            pyproject_has_project_section=True,
            is_framework_self_repo=False,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_rationale_mentions_framework(self):
        sig = _make_signals(is_framework_self_repo=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert "framework" in r.rationale.lower()

    def test_matched_signals_includes_framework_flag(self):
        sig = _make_signals(is_framework_self_repo=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert "is_framework_self_repo" in r.matched_signals


# ── SingleSaasRoutedClassifier ───────────────────────────────────────


class TestSingleSaasRoutedClassifier:
    clf = SingleSaasRoutedClassifier()

    def test_next_app_router_single(self):
        sig = _make_signals(has_app_router_dir=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "single-saas-routed"

    def test_next_pages_single(self):
        sig = _make_signals(has_pages_router_dir=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "single-saas-routed"

    def test_remix_single(self):
        sig = _make_signals(has_remix_routes_dir=True)
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "single-saas-routed"

    def test_fastapi_app_single(self):
        sig = _make_signals(
            has_fastapi_app_factory=True,
            pyproject_has_project_section=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "single-saas-routed"

    def test_next_app_in_monorepo_does_not_fire(self):
        sig = _make_signals(monorepo=True, has_app_router_dir=True)
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_fastapi_library_does_not_fire(self):
        sig = _make_signals(
            has_fastapi_app_factory=True,
            pyproject_has_project_section=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None


# ── UniversalResidualClassifier ──────────────────────────────────────


class TestUniversalResidualClassifier:
    clf = UniversalResidualClassifier()

    def test_always_returns(self):
        r = self.clf.classify(_make_ctx(), _make_signals())
        assert r is not None
        assert r.shape == "universal-residual"

    def test_returns_fallback_confidence(self):
        r = self.clf.classify(_make_ctx(), _make_signals())
        assert r.confidence == FALLBACK_CONFIDENCE

    def test_rationale_mentions_threshold(self):
        r = self.clf.classify(_make_ctx(), _make_signals())
        assert "threshold" in r.rationale.lower()


# ── GoServerClassifier (Sprint S6.2 Ext 1) ───────────────────────────


class TestGoServerClassifier:
    clf = GoServerClassifier()

    def test_canonical_go_server_with_cmd_main(self):
        """ollama-shape: go.mod + cmd/<name>/main.go + server/ dir."""
        sig = _make_signals(
            has_go_mod=True,
            has_go_cmd_with_main=True,
            has_go_server_dir=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "go-server"
        assert r.confidence == 0.90
        assert "server/api/internal/server" in r.rationale

    def test_go_cmd_only_no_server_dir(self):
        """A Go cmd binary without server/ dir still classifies."""
        sig = _make_signals(
            has_go_mod=True,
            has_go_cmd_with_main=True,
            has_go_server_dir=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "go-server"
        assert "cmd/<name>/main.go present" in r.rationale

    def test_go_library_without_cmd_does_not_fire(self):
        """chi-shape: go.mod + top-level go files, no cmd — GoLibrary wins."""
        sig = _make_signals(
            has_go_mod=True,
            has_go_top_level_files=True,
            has_go_cmd_with_main=False,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_no_go_mod_does_not_fire(self):
        sig = _make_signals(has_go_mod=False, has_go_cmd_with_main=True)
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_monorepo_does_not_fire(self):
        sig = _make_signals(
            monorepo=True,
            has_go_mod=True,
            has_go_cmd_with_main=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None


# ── GoLibraryClassifier (Sprint S6.2 Ext 1) ──────────────────────────


class TestGoLibraryClassifier:
    clf = GoLibraryClassifier()

    def test_canonical_go_library_chi_shape(self):
        """chi-shape: go.mod + top-level go files, no cmd/ dir."""
        sig = _make_signals(
            has_go_mod=True,
            has_go_top_level_files=True,
            has_go_cmd_with_main=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "go-library"
        assert r.confidence == 0.90
        assert "library shape" in r.rationale

    def test_go_with_cmd_does_not_fire(self):
        """A Go module with cmd/ classifies as go-server, not go-library."""
        sig = _make_signals(
            has_go_mod=True,
            has_go_top_level_files=True,
            has_go_cmd_with_main=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_go_without_top_level_files_does_not_fire(self):
        """An "internal-only" Go module without top-level entries doesn't fire."""
        sig = _make_signals(
            has_go_mod=True,
            has_go_top_level_files=False,
            has_go_cmd_with_main=False,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_no_go_mod_does_not_fire(self):
        sig = _make_signals(has_go_top_level_files=True)
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_monorepo_does_not_fire(self):
        sig = _make_signals(
            monorepo=True,
            has_go_mod=True,
            has_go_top_level_files=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None


# ── RustWorkspaceClassifier (Sprint S6.2 Ext 2) ──────────────────────


class TestRustWorkspaceClassifier:
    clf = RustWorkspaceClassifier()

    def test_canonical_meilisearch_shape_conf_095(self):
        """meilisearch-shape: Cargo workspace with ≥3 member crates."""
        sig = _make_signals(
            has_cargo_toml=True,
            has_cargo_workspace=True,
            cargo_workspace_member_count=17,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "rust-workspace"
        assert r.confidence == 0.95
        assert "17 member crates" in r.rationale

    def test_two_member_workspace_conf_085(self):
        """A 2-crate workspace is still a workspace, but lower confidence."""
        sig = _make_signals(
            has_cargo_toml=True,
            has_cargo_workspace=True,
            cargo_workspace_member_count=2,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.85

    def test_extractor_hint_boost_to_090(self):
        """rust-workspace hint from auditor boosts 0.85 → 0.90."""
        sig = _make_signals(
            has_cargo_toml=True,
            has_cargo_workspace=True,
            cargo_workspace_member_count=2,
            extractor_hints=("rust-workspace",),
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.90

    def test_single_crate_does_not_fire(self):
        sig = _make_signals(
            has_cargo_toml=True,
            has_cargo_workspace=False,
            cargo_is_single_crate=True,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_one_member_does_not_fire(self):
        """A workspace declared but with <2 resolved members doesn't fire."""
        sig = _make_signals(
            has_cargo_toml=True,
            has_cargo_workspace=True,
            cargo_workspace_member_count=1,
        )
        assert self.clf.classify(_make_ctx(), sig) is None


# ── Extension 3: TurborepoMonorepoClassifier non-canonical variants ──


class TestTurborepoMonorepoNonCanonical:
    clf = TurborepoMonorepoClassifier()

    def test_split_fullstack_frontend_backend_conf_080(self):
        """infisical/soc0-shape: /frontend + /backend, NOT canonical monorepo."""
        sig = _make_signals(
            monorepo=False,
            has_split_fullstack_frontend_backend=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "turborepo-monorepo"
        assert r.confidence == 0.80
        assert "Split-fullstack" in r.rationale

    def test_packages_only_workspace_conf_080(self):
        """strapi-shape: /packages without /apps, multi-package."""
        sig = _make_signals(
            monorepo=False,
            has_packages_only_workspace=True,
            packages_only_count=7,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "turborepo-monorepo"
        assert r.confidence == 0.80
        assert "7 sub-packages" in r.rationale

    def test_canonical_still_wins_over_extension_3(self):
        """When both signatures match, canonical (0.95) wins."""
        sig = _make_signals(
            monorepo=True,
            has_turbo_json=True,
            workspace_count=4,
            workspace_has_apps_dir=True,
            workspace_has_packages_dir=True,
            has_split_fullstack_frontend_backend=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.95

    def test_neither_canonical_nor_extension_3_does_not_fire(self):
        sig = _make_signals(
            monorepo=False,
            has_split_fullstack_frontend_backend=False,
            has_packages_only_workspace=False,
        )
        assert self.clf.classify(_make_ctx(), sig) is None

    def test_lerna_nx_monorepo_classifies_at_080(self):
        """strapi-shape: monorepo enumerated by Stage 0 via lerna/nx,
        no turbo.json / pnpm-workspace.yaml.
        """
        sig = _make_signals(
            monorepo=True,
            workspace_count=17,
            workspace_has_packages_dir=True,
            has_turbo_json=False,
            has_pnpm_workspace=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "turborepo-monorepo"
        assert r.confidence == 0.80
        assert "Lerna/Nx" in r.rationale


# ── Extension 4: SingleSaasRoutedClassifier subdir + SPA variants ────


class TestSingleSaasRoutedExtension4:
    clf = SingleSaasRoutedClassifier()

    def test_vite_react_spa_subdir_classifies_conf_070(self):
        """Soc0/frontend-shape: Vite + React SPA with src/."""
        sig = _make_signals(
            has_package_json=True,
            package_json_has_react_dep=True,
            package_json_has_vite_dep=True,
            has_src_pages_or_routes_dir=True,
            is_subdir_scan=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.shape == "single-saas-routed"
        assert r.confidence == 0.70
        assert "React SPA" in r.rationale
        assert "subdir scan" in r.rationale

    def test_vite_vue_spa_standalone_repo(self):
        """Vite + Vue SPA at repo root (not subdir) — still classifies."""
        sig = _make_signals(
            has_package_json=True,
            package_json_has_vue_dep=True,
            package_json_has_vite_dep=True,
            has_src_pages_or_routes_dir=True,
            is_subdir_scan=False,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and "Vue SPA" in r.rationale

    def test_fastapi_subdir_scan_classifies(self):
        """Soc0/backend-shape: FastAPI at subdir of parent git repo."""
        sig = _make_signals(
            is_subdir_scan=True,
            has_fastapi_app_factory=True,
            # NOTE: pyproject_has_project_section may be True here
            # since Soc0/backend has a pyproject — Ext 4 fires anyway.
            pyproject_has_project_section=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.70
        assert "Subdir scan" in r.rationale and "FastAPI" in r.rationale

    def test_react_subdir_without_vite_loose_match(self):
        """React subdir scan without Vite — loose match at 0.65."""
        sig = _make_signals(
            is_subdir_scan=True,
            has_package_json=True,
            package_json_has_react_dep=True,
            package_json_has_vite_dep=False,
            has_src_pages_or_routes_dir=True,
        )
        r = self.clf.classify(_make_ctx(), sig)
        assert r is not None and r.confidence == 0.65

    def test_vite_without_routing_folder_does_not_fire(self):
        """A Vite + React package.json without src/pages or src/routes doesn't fire."""
        sig = _make_signals(
            has_package_json=True,
            package_json_has_react_dep=True,
            package_json_has_vite_dep=True,
            has_src_pages_or_routes_dir=False,
        )
        # Falls through to None (Ext 4 SPA path needs routing dir).
        assert self.clf.classify(_make_ctx(), sig) is None


# ── Dispatcher ───────────────────────────────────────────────────────


class TestDispatcher:
    def test_runs_in_priority_order_and_first_match_wins(self):
        calls: list[str] = []

        class FakeHi:
            name = "hi"
            priority = 10
            def classify(self, ctx, sig):
                calls.append("hi")
                return ClassificationResult("hi", 0.95, "ok", ())

        class FakeLo:
            name = "lo"
            priority = 60
            def classify(self, ctx, sig):
                calls.append("lo")
                return ClassificationResult("lo", 0.95, "ok", ())

        r = classify_repo_shape(_make_ctx(), classifiers=[FakeLo(), FakeHi()])
        assert r.shape == "hi"
        assert calls == ["hi"]

    def test_below_threshold_falls_through(self):
        class WeakClf:
            name = "weak"
            priority = 10
            def classify(self, ctx, sig):
                return ClassificationResult("weak", 0.30, "low", ())

        r = classify_repo_shape(
            _make_ctx(),
            classifiers=[WeakClf(), UniversalResidualClassifier()],
        )
        assert r.shape == "universal-residual"
        assert r.confidence == FALLBACK_CONFIDENCE

    def test_classifier_exception_degrades_to_none(self, caplog):
        class BoomClf:
            name = "boom"
            priority = 10
            def classify(self, ctx, sig):
                raise RuntimeError("kaboom")

        import logging
        caplog.set_level(logging.WARNING, logger="faultline.pipeline_v2.stage_0_6_shape")
        r = classify_repo_shape(
            _make_ctx(),
            classifiers=[BoomClf(), UniversalResidualClassifier()],
        )
        assert r.shape == "universal-residual"
        assert "kaboom" in caplog.text

    def test_writes_artifact_when_run_dir_set(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        ctx.run_dir = tmp_path
        classify_repo_shape(ctx)
        artifact = tmp_path / "06-stage-shape.json"
        assert artifact.exists()
        data = json.loads(artifact.read_text())
        assert "winner" in data
        assert "evaluations" in data
        assert "signals" in data
        assert data["min_confidence"] == MIN_CONFIDENCE

    def test_does_not_write_artifact_when_run_dir_none(self, tmp_path):
        ctx = _make_ctx(tmp_path)
        ctx.run_dir = None
        classify_repo_shape(ctx)
        # tmp_path should remain empty of stage artifacts
        assert not (tmp_path / "06-stage-shape.json").exists()

    def test_min_confidence_is_universal(self):
        assert MIN_CONFIDENCE == 0.60
        assert FALLBACK_CONFIDENCE == 0.40

    def test_with_only_residual_fires_residual(self):
        r = classify_repo_shape(
            _make_ctx(), classifiers=[UniversalResidualClassifier()],
        )
        assert r.shape == "universal-residual"


# ── ShapeSignals.collect() — structural integration ─────────────────


class TestShapeSignalsCollect:
    def test_turborepo_repo_signals_detected(self, make_tmp_repo):
        repo = make_tmp_repo({
            "package.json": json.dumps({
                "name": "root",
                "workspaces": ["apps/*", "packages/*"],
            }),
            "turbo.json": "{}",
            "apps/web/package.json": '{"name":"web"}',
            "packages/ui/package.json": '{"name":"ui"}',
        })
        ctx = ScanContext(
            repo_path=repo, stack=None, monorepo=True,
            workspaces=[
                Workspace(name="web", path="apps/web"),
                Workspace(name="ui", path="packages/ui"),
            ],
            tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_turbo_json is True
        assert sig.has_package_json is True
        assert sig.workspace_has_apps_dir is True
        assert sig.workspace_has_packages_dir is True
        assert sig.workspace_count == 2

    def test_js_library_signals(self, make_tmp_repo):
        repo = make_tmp_repo({
            "package.json": json.dumps({
                "name": "axios", "main": "index.js",
                "exports": {".": "./index.js"},
            }),
            "index.js": "module.exports = {};",
        })
        ctx = ScanContext(
            repo_path=repo, stack="js-generic", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.package_json_main_or_exports is True
        assert sig.package_json_no_app_entry is True

    def test_python_library_signals(self, make_tmp_repo):
        repo = make_tmp_repo({
            "pyproject.toml": "[project]\nname = \"libx\"\n",
            "libx/__init__.py": "",
        })
        ctx = ScanContext(
            repo_path=repo, stack="python-lib", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_pyproject is True
        assert sig.pyproject_has_project_section is True

    def test_rails_signals(self, make_tmp_repo):
        repo = make_tmp_repo({
            "Gemfile": "source 'https://rubygems.org'\n",
            "app/controllers/.keep": "",
            "app/models/.keep": "",
        })
        ctx = ScanContext(
            repo_path=repo, stack="rails", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_gemfile is True
        assert sig.has_rails_app_dir is True

    def test_go_library_signals_chi_shape(self, make_tmp_repo):
        """chi-like: go.mod + top-level chi.go, no cmd/."""
        repo = make_tmp_repo({
            "go.mod": "module github.com/example/chi\n\ngo 1.21\n",
            "chi.go": "package chi\n",
            "mux.go": "package chi\n",
        })
        ctx = ScanContext(
            repo_path=repo, stack="go", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_go_mod is True
        assert sig.has_go_top_level_files is True
        assert sig.has_go_cmd_with_main is False

    def test_go_server_signals_ollama_shape(self, make_tmp_repo):
        """ollama-like: go.mod + cmd/<name>/main.go + server/ dir."""
        repo = make_tmp_repo({
            "go.mod": "module github.com/example/ollama\n\ngo 1.21\n",
            "cmd/serve/main.go": "package main\nfunc main(){}\n",
            "server/server.go": "package server\n",
        })
        ctx = ScanContext(
            repo_path=repo, stack="go", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_go_mod is True
        assert sig.has_go_cmd_with_main is True
        assert sig.has_go_server_dir is True

    def test_rust_workspace_signals_meilisearch_shape(self, make_tmp_repo):
        """meilisearch-like: Cargo workspace with members glob."""
        repo = make_tmp_repo({
            "Cargo.toml": (
                "[workspace]\nresolver = \"2\"\n"
                "members = [\"crates/a\", \"crates/b\", \"crates/c\"]\n"
            ),
            "crates/a/Cargo.toml": "[package]\nname=\"a\"\n",
            "crates/b/Cargo.toml": "[package]\nname=\"b\"\n",
            "crates/c/Cargo.toml": "[package]\nname=\"c\"\n",
        })
        ctx = ScanContext(
            repo_path=repo, stack="rust", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_cargo_workspace is True
        assert sig.cargo_workspace_member_count == 3

    def test_rust_workspace_signals_glob_pattern(self, make_tmp_repo):
        """Glob members like ``crates/*`` resolve via filesystem."""
        repo = make_tmp_repo({
            "Cargo.toml": (
                "[workspace]\nmembers = [\"crates/*\"]\n"
            ),
            "crates/x/Cargo.toml": "[package]\nname=\"x\"\n",
            "crates/y/Cargo.toml": "[package]\nname=\"y\"\n",
        })
        ctx = ScanContext(
            repo_path=repo, stack="rust", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.cargo_workspace_member_count == 2

    def test_split_fullstack_signals_infisical_shape(self, make_tmp_repo):
        """infisical-like: /frontend + /backend siblings with manifests."""
        repo = make_tmp_repo({
            "frontend/package.json": '{"name":"fe"}',
            "backend/package.json": '{"name":"be"}',
        })
        ctx = ScanContext(
            repo_path=repo, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_split_fullstack_frontend_backend is True

    def test_packages_only_signals_strapi_shape(self, make_tmp_repo):
        """strapi-like: /packages with N sub-packages, NO /apps."""
        repo = make_tmp_repo({
            "package.json": '{"name":"strapi-root"}',
            "packages/core/package.json": '{"name":"core"}',
            "packages/cli/package.json": '{"name":"cli"}',
            "packages/utils/package.json": '{"name":"utils"}',
        })
        ctx = ScanContext(
            repo_path=repo, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_packages_only_workspace is True
        assert sig.packages_only_count == 3

    def test_packages_only_does_not_fire_when_apps_present(self, make_tmp_repo):
        """When /apps coexists with /packages, packages-only signature is OFF."""
        repo = make_tmp_repo({
            "packages/a/package.json": "{}",
            "packages/b/package.json": "{}",
            "apps/web/package.json": "{}",
        })
        ctx = ScanContext(
            repo_path=repo, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.has_packages_only_workspace is False

    def test_subdir_scan_detection(self, tmp_path):
        """Scanning a subdir of a git repo sets is_subdir_scan + parent_git_root."""
        # Build a parent dir with .git, and a subdir with package.json.
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "frontend"
        sub.mkdir()
        (sub / "package.json").write_text('{"name":"fe"}')
        ctx = ScanContext(
            repo_path=sub, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.is_subdir_scan is True
        assert sig.parent_git_root is not None

    def test_subdir_scan_false_for_repo_root(self, tmp_path):
        """A repo root with its own .git is NOT a subdir scan."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "package.json").write_text("{}")
        ctx = ScanContext(
            repo_path=tmp_path, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.is_subdir_scan is False

    def test_framework_dep_detection(self, make_tmp_repo):
        """package.json with react+vite deps reports both flags."""
        repo = make_tmp_repo({
            "package.json": json.dumps({
                "name": "x",
                "dependencies": {"react": "18", "react-dom": "18"},
                "devDependencies": {"vite": "5", "@vitejs/plugin-react": "4"},
            }),
        })
        ctx = ScanContext(
            repo_path=repo, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.package_json_has_react_dep is True
        assert sig.package_json_has_vite_dep is True
        assert sig.package_json_has_vue_dep is False

    def test_no_md_files_are_read_during_collection(self, make_tmp_repo, monkeypatch):
        """Hard rule from CLAUDE.md: README parsing is forbidden."""
        repo = make_tmp_repo({
            "package.json": "{}",
            "README.md": "this should never be read by signal collection",
            "CHANGELOG.md": "...",
        })
        read_paths: list[str] = []
        original = Path.read_text

        def spy(self, *a, **kw):
            read_paths.append(str(self))
            return original(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", spy)

        ctx = ScanContext(
            repo_path=repo, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        ShapeSignals.collect(ctx)
        offenders = [p for p in read_paths if p.lower().endswith(".md")]
        assert offenders == [], f"signal collection read forbidden .md: {offenders}"


# ── Pure-function contract ───────────────────────────────────────────


class TestClassifyRepoShapePure:
    def test_deterministic_same_inputs_same_output(self, make_tmp_repo):
        repo = make_tmp_repo({
            "package.json": json.dumps({"name": "x", "main": "index.js"}),
            "index.js": "module.exports = {};",
        })
        ctx = ScanContext(
            repo_path=repo, stack="js-generic", monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        r1 = classify_repo_shape(ctx)
        r2 = classify_repo_shape(ctx)
        assert r1.shape == r2.shape
        assert r1.confidence == r2.confidence
        assert r1.rationale == r2.rationale

    def test_with_shape_returns_new_ctx_with_fields(self):
        ctx = _make_ctx()
        r = ClassificationResult("oss-library", 0.9, "rationale", ("a",))
        ctx2 = ctx.with_shape(r)
        assert ctx2.repo_shape == "oss-library"
        assert ctx2.shape_confidence == 0.9
        assert ctx2.shape_rationale == "rationale"
        assert ctx.repo_shape is None  # original untouched


# ── Sprint S10 — SubdirOfMonorepoClassifier + parent_shape ───────────


class TestS10DetectParentShape:
    """Cheap structural probes against parent git roots."""

    def test_detects_turborepo_parent(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        (tmp_path / "turbo.json").write_text("{}")
        assert _detect_parent_shape(tmp_path) == "turborepo-monorepo"

    def test_detects_pnpm_workspace_as_turborepo(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        (tmp_path / "pnpm-workspace.yaml").write_text("packages: ['apps/*']")
        assert _detect_parent_shape(tmp_path) == "turborepo-monorepo"

    def test_detects_split_fullstack(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        (tmp_path / "frontend").mkdir()
        (tmp_path / "backend").mkdir()
        assert _detect_parent_shape(tmp_path) == "split-fullstack"

    def test_detects_cargo_workspace(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        (tmp_path / "Cargo.toml").write_text(
            "[workspace]\nmembers = [\"crates/*\"]\n",
        )
        assert _detect_parent_shape(tmp_path) == "cargo-workspace"

    def test_detects_go_multi_module(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        (tmp_path / "go.work").write_text("go 1.21\nuse (./a ./b)\n")
        assert _detect_parent_shape(tmp_path) == "go-multi-module"

    def test_detects_rails_monolith(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "routes.rb").write_text("Rails.routes.draw {}")
        (tmp_path / "app" / "controllers").mkdir(parents=True)
        assert _detect_parent_shape(tmp_path) == "rails-monolith"

    def test_returns_empty_when_no_recognisable_shape(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        # Just a normal repo, nothing structural at root
        (tmp_path / "README.md").write_text("hi")
        assert _detect_parent_shape(tmp_path) == ""

    def test_returns_empty_for_none_root(self):
        from faultline.pipeline_v2.stage_0_6_shape import _detect_parent_shape
        assert _detect_parent_shape(None) == ""


class TestS10ShapeSignalsParentShape:
    """ShapeSignals.collect populates parent_shape only for subdir scans."""

    def test_parent_shape_empty_for_root_scan(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "package.json").write_text("{}")
        ctx = ScanContext(
            repo_path=tmp_path, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.parent_shape == ""

    def test_parent_shape_populated_for_subdir_scan(self, tmp_path):
        # Build a parent with split-fullstack and scan the subdir.
        (tmp_path / ".git").mkdir()
        (tmp_path / "frontend").mkdir()
        (tmp_path / "backend").mkdir()
        # Need a tracked-files marker on the subdir so collection runs.
        sub = tmp_path / "frontend"
        (sub / "package.json").write_text(json.dumps({"name": "fe"}))
        ctx = ScanContext(
            repo_path=sub, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        sig = ShapeSignals.collect(ctx)
        assert sig.is_subdir_scan is True
        assert sig.parent_shape == "split-fullstack"


class TestSubdirOfMonorepoClassifier:
    """Sprint S10 — parent-inherited fallback classifier."""

    def test_does_not_fire_on_root_scan(self):
        from faultline.pipeline_v2.stage_0_6_shape import SubdirOfMonorepoClassifier
        clf = SubdirOfMonorepoClassifier()
        sig = _make_signals(is_subdir_scan=False, parent_shape="turborepo-monorepo")
        ctx = _make_ctx()
        assert clf.classify(ctx, sig) is None

    def test_does_not_fire_when_parent_unknown(self):
        from faultline.pipeline_v2.stage_0_6_shape import SubdirOfMonorepoClassifier
        clf = SubdirOfMonorepoClassifier()
        sig = _make_signals(is_subdir_scan=True, parent_shape="")
        ctx = _make_ctx()
        assert clf.classify(ctx, sig) is None

    def test_fires_with_child_of_prefix_at_070(self):
        from faultline.pipeline_v2.stage_0_6_shape import SubdirOfMonorepoClassifier
        clf = SubdirOfMonorepoClassifier()
        sig = _make_signals(
            is_subdir_scan=True,
            parent_shape="split-fullstack",
            parent_git_root="/repos/parent",
        )
        ctx = _make_ctx()
        r = clf.classify(ctx, sig)
        assert r is not None
        assert r.shape == "child-of-split-fullstack"
        assert r.confidence == 0.70
        assert "is_subdir_scan" in r.matched_signals
        assert "parent_shape" in r.matched_signals

    def test_priority_below_single_saas(self):
        """The S10 classifier must run AFTER SingleSaasRouted so a
        recognisable SPA still wins; but BEFORE residual fallback."""
        from faultline.pipeline_v2.stage_0_6_shape import (
            SingleSaasRoutedClassifier,
            SubdirOfMonorepoClassifier,
            UniversalResidualClassifier,
        )
        sub = SubdirOfMonorepoClassifier()
        saas = SingleSaasRoutedClassifier()
        resid = UniversalResidualClassifier()
        assert saas.priority < sub.priority < resid.priority

    def test_dispatcher_picks_child_when_no_specific_match(self, tmp_path):
        """End-to-end: subdir with no SPA markers but a turborepo parent
        → ``child-of-turborepo-monorepo``."""
        parent = tmp_path / "monorepo"
        parent.mkdir()
        (parent / ".git").mkdir()
        (parent / "turbo.json").write_text("{}")
        # Subdir is empty — nothing else can fire.
        sub = parent / "apps" / "obscure"
        sub.mkdir(parents=True)
        ctx = ScanContext(
            repo_path=sub, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        r = classify_repo_shape(ctx)
        assert r.shape == "child-of-turborepo-monorepo"
        assert r.confidence == 0.70

    def test_dispatcher_prefers_single_saas_when_specific(self, tmp_path):
        """A subdir Vite SPA with all canonical markers should win as
        single-saas-routed, NOT child-of-split-fullstack."""
        parent = tmp_path / "monorepo"
        parent.mkdir()
        (parent / ".git").mkdir()
        (parent / "frontend").mkdir()
        (parent / "backend").mkdir()
        sub = parent / "frontend"
        (sub / "package.json").write_text(json.dumps({
            "name": "fe",
            "dependencies": {"react": "18", "react-dom": "18"},
            "devDependencies": {"vite": "5"},
        }))
        (sub / "src" / "pages").mkdir(parents=True)
        ctx = ScanContext(
            repo_path=sub, stack=None, monorepo=False,
            workspaces=None, tracked_files=[], commits=[],
        )
        r = classify_repo_shape(ctx)
        # SPA matcher fires first (priority 60 vs 65).
        assert r.shape == "single-saas-routed"
