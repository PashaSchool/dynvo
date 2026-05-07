"""Sprint 19 Day 1 — stack_detector unit tests with synthetic fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.llm.stack_detector import (
    StackProfile,
    VALID_STACKS,
    detect_stack,
)


def _mk(repo: Path, *files: str) -> None:
    """Create empty files (with parent dirs) under ``repo``."""
    for f in files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_detects_next_monorepo_with_pnpm_workspace(tmp_path):
    _mk(tmp_path, "pnpm-workspace.yaml")
    _mk(tmp_path, "apps/web/next.config.js")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "next-monorepo"
    assert profile.confidence >= 0.9
    assert profile.via == "static"


def test_detects_node_monorepo_when_no_next_app(tmp_path):
    _mk(tmp_path, "turbo.json")
    _mk(tmp_path, "apps/api/server.js")
    _write_json(tmp_path / "apps" / "api" / "package.json", {"name": "api"})
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "node-monorepo"


def test_detects_next_app_router_single_app(tmp_path):
    _mk(tmp_path, "next.config.js", "app/page.tsx")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "next-app-router"
    assert "next.config.js" in profile.signals


def test_detects_vue_spa(tmp_path):
    _mk(tmp_path, "vue.config.js", "src/components/Foo.vue")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "vue-spa"


def test_detects_vue_nuxt_monorepo(tmp_path):
    _mk(tmp_path, "pnpm-workspace.yaml", "nuxt.config.ts")
    _mk(tmp_path, "packages/lib/index.ts")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "vue-nuxt-monorepo"


def test_detects_rails(tmp_path):
    _mk(tmp_path, "Gemfile", "config/application.rb",
        "app/controllers/application_controller.rb")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "rails-app"


def test_detects_go_modular(tmp_path):
    _mk(tmp_path, "go.mod", "internal/auth/auth.go")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "go-modular"


def test_detects_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "foo"\n')
    _mk(tmp_path, "src/lib.rs")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "rust-modular"


def test_detects_python_flat(tmp_path):
    _mk(tmp_path, "pyproject.toml", "mypkg/__init__.py", "mypkg/core.py")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "python-flat"
    assert "single pkg: mypkg" in profile.signals


def test_detects_python_modules_django(tmp_path):
    _mk(tmp_path, "manage.py", "pyproject.toml", "users/__init__.py")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "python-modules"
    assert "manage.py" in profile.signals


def test_detects_js_library(tmp_path):
    _write_json(tmp_path / "package.json", {
        "name": "mylib",
        "main": "dist/index.js",
        "exports": {".": "./dist/index.js"},
    })
    _mk(tmp_path, "lib/index.ts")
    profile = detect_stack(tmp_path, [])
    assert profile.kind == "js-library"


def test_override_short_circuits_detection(tmp_path):
    _mk(tmp_path, "Gemfile")  # would normally be rails-app
    profile = detect_stack(tmp_path, [], override="next-monorepo")
    assert profile.kind == "next-monorepo"
    assert profile.via == "override"


def test_invalid_override_falls_back_to_mixed(tmp_path):
    _mk(tmp_path, "Gemfile")
    profile = detect_stack(tmp_path, [], override="not-a-real-stack")
    assert profile.kind == "mixed"


def test_empty_repo_falls_back_to_mixed_when_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = detect_stack(tmp_path, ["random.file"], api_key=None)
    assert profile.kind == "mixed"
    assert profile.via == "fallback"


def test_all_returned_kinds_are_valid(tmp_path):
    """Sanity: detector never returns a tag outside VALID_STACKS."""
    profile = detect_stack(tmp_path, [])
    assert profile.kind in VALID_STACKS
