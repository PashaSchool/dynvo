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
    OssLibraryClassifier,
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
