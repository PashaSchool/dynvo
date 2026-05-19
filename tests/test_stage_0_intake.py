"""Tests for ``faultline.pipeline_v2.stage_0_intake``.

Each test builds a synthetic repo under ``tmp_path`` with just enough
shape to trigger one detection path, then asserts that
``stage_0_intake`` returns the expected stack / monorepo / workspaces.

We pass ``skip_git=True`` everywhere so we don't have to ``git init``
each fixture. The git-history loading path is exercised end-to-end by
the legacy pipeline tests already; Stage 0 only delegates to existing
helpers there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2 import ScanContext, Workspace, stage_0_intake
from faultline.pipeline_v2.stage_0_intake import detect_stack


# ─────────────── Helpers ────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2))


# ─────────────── Single-app fixtures ────────────────


def test_next_app_router_single_app(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "my-app",
        "dependencies": {"next": "^15.0.0", "react": "^19.0.0"},
    })
    _write(tmp_path / "next.config.ts", "export default {}")
    _write(tmp_path / "app" / "page.tsx", "export default function Page() {}")
    _write(tmp_path / "app" / "layout.tsx", "export default function L() {}")

    ctx = stage_0_intake(tmp_path, skip_git=True)

    assert isinstance(ctx, ScanContext)
    assert ctx.monorepo is False
    assert ctx.workspaces is None
    assert ctx.stack == "next-app-router"
    assert ctx.workspace_manager is None
    # signals should mention both the dep and the config file
    joined = " | ".join(ctx.stack_signals)
    assert "next" in joined.lower()
    assert "app/" in joined or "app router" in joined.lower()


def test_next_pages_router_single_app(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "my-pages-app",
        "dependencies": {"next": "^14.0.0"},
    })
    _write(tmp_path / "next.config.js", "module.exports = {}")
    _write(tmp_path / "pages" / "index.tsx", "export default function I() {}")
    _write(tmp_path / "pages" / "api" / "hello.ts", "export default () => {}")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.stack == "next-pages"
    assert ctx.monorepo is False


def test_python_lib_pyproject_only(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[project]\nname = "tinylib"\nversion = "0.1"\n')
    _write(tmp_path / "tinylib" / "__init__.py", "")
    _write(tmp_path / "tinylib" / "core.py", "def hello(): return 'hi'\n")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.stack == "python-lib"
    assert ctx.monorepo is False
    assert any("pyproject" in s.lower() for s in ctx.stack_signals)


def test_fastapi_single_app(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", '[project]\nname = "api"\ndependencies = ["fastapi", "uvicorn"]\n')
    _write(tmp_path / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.stack == "fastapi"
    assert ctx.monorepo is False


def test_django_single_app(tmp_path: Path) -> None:
    _write(tmp_path / "requirements.txt", "Django>=5\n")
    _write(tmp_path / "manage.py", "#!/usr/bin/env python\n")
    _write(tmp_path / "myproj" / "settings.py", "")
    _write(tmp_path / "myproj" / "urls.py", "")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.stack == "django"


def test_unknown_stack_returns_none(tmp_path: Path) -> None:
    # Just a bunch of plain text files — nothing recognisable.
    _write(tmp_path / "README" / "readme.txt", "hello")
    _write(tmp_path / "data.csv", "a,b,c\n1,2,3\n")
    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.stack is None
    assert ctx.monorepo is False


# ─────────────── Monorepo fixtures ────────────────


def test_pnpm_monorepo_two_next_apps(tmp_path: Path) -> None:
    _write(tmp_path / "pnpm-workspace.yaml", 'packages:\n  - "apps/*"\n')
    _write_json(tmp_path / "package.json", {"name": "root", "private": True})

    # apps/web — Next App Router
    _write_json(tmp_path / "apps" / "web" / "package.json", {
        "name": "web",
        "dependencies": {"next": "^15.0.0"},
    })
    _write(tmp_path / "apps" / "web" / "next.config.ts", "export default {}")
    _write(tmp_path / "apps" / "web" / "app" / "page.tsx", "export default () => {}")

    # apps/dashboard — Next App Router
    _write_json(tmp_path / "apps" / "dashboard" / "package.json", {
        "name": "dashboard",
        "dependencies": {"next": "^15.0.0", "@stripe/stripe-js": "^4.0.0"},
    })
    _write(tmp_path / "apps" / "dashboard" / "next.config.ts", "export default {}")
    _write(tmp_path / "apps" / "dashboard" / "app" / "page.tsx", "export default () => {}")

    ctx = stage_0_intake(tmp_path, skip_git=True)

    assert ctx.monorepo is True
    assert ctx.workspaces is not None
    assert ctx.workspace_manager == "pnpm"
    by_name = {w.name: w for w in ctx.workspaces}
    assert set(by_name) == {"web", "dashboard"}
    assert by_name["web"].stack == "next-app-router"
    assert by_name["dashboard"].stack == "next-app-router"
    assert by_name["web"].path == "apps/web"
    assert by_name["dashboard"].path == "apps/dashboard"
    # package_json gets surfaced for downstream extractors
    assert by_name["dashboard"].package_json is not None
    deps = by_name["dashboard"].package_json.get("dependencies")  # type: ignore[union-attr]
    assert isinstance(deps, dict) and "@stripe/stripe-js" in deps
    # Files were assigned per package.
    assert any(f.endswith("page.tsx") for f in by_name["web"].files)
    assert any(f.endswith("page.tsx") for f in by_name["dashboard"].files)


def test_npm_workspaces_mixed_stacks(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "root",
        "private": True,
        "workspaces": ["packages/*"],
    })

    # packages/api — express
    _write_json(tmp_path / "packages" / "api" / "package.json", {
        "name": "api",
        "dependencies": {"express": "^4.0.0"},
    })
    _write(tmp_path / "packages" / "api" / "src" / "server.ts", "import express from 'express';")

    # packages/ui — js-generic (no framework)
    _write_json(tmp_path / "packages" / "ui" / "package.json", {
        "name": "ui",
        "dependencies": {"react": "^19.0.0"},
    })
    _write(tmp_path / "packages" / "ui" / "src" / "Button.tsx", "export const Button = () => null;")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.monorepo is True
    assert ctx.workspace_manager in ("npm", "yarn")
    assert ctx.workspaces is not None
    by_name = {w.name: w for w in ctx.workspaces}
    assert by_name["api"].stack == "express"
    assert by_name["ui"].stack == "js-generic"


def test_cargo_workspace(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["crates/core", "crates/cli"]\n')
    _write(tmp_path / "crates" / "core" / "Cargo.toml", '[package]\nname = "core"\n')
    _write(tmp_path / "crates" / "core" / "src" / "lib.rs", "pub fn hi() {}")
    _write(tmp_path / "crates" / "cli" / "Cargo.toml", '[package]\nname = "cli"\n')
    _write(tmp_path / "crates" / "cli" / "src" / "main.rs", "fn main() {}")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.monorepo is True
    assert ctx.workspace_manager == "cargo"
    assert ctx.workspaces is not None
    names = {w.name for w in ctx.workspaces}
    assert names == {"core", "cli"}
    for w in ctx.workspaces:
        assert w.stack == "rust"


# ─────────────── detect_stack pure-function tests ────────────────


def test_detect_stack_next_app_router(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {"dependencies": {"next": "^15"}})
    _write(tmp_path / "next.config.mjs", "export default {}")
    (tmp_path / "app").mkdir()
    stack, signals = detect_stack(tmp_path, ["app/page.tsx"])
    assert stack == "next-app-router"
    assert signals  # non-empty


def test_detect_stack_empty_dir_returns_none(tmp_path: Path) -> None:
    stack, signals = detect_stack(tmp_path, [])
    assert stack is None


# ─────────────── Sprint S3.1 — recursive glob expansion ────────────────


def test_pnpm_workspace_double_star_recurses(tmp_path: Path) -> None:
    """pnpm-workspace.yaml `packages/**` must enumerate nested packages.

    Regression for S3.1 fix (a): the previous ``iterdir()``-only impl
    silently dropped nested packages like ``packages/db/src/schema``.
    """
    _write(tmp_path / "pnpm-workspace.yaml", 'packages:\n  - "packages/**"\n')
    _write_json(tmp_path / "package.json", {"name": "root", "private": True})

    # Three levels of nesting under packages/ — only dirs WITH manifests
    # should become workspaces; bare source dirs should not.
    _write_json(tmp_path / "packages" / "auth" / "package.json", {"name": "auth"})
    _write(tmp_path / "packages" / "auth" / "src" / "index.ts", "export {};")

    _write_json(
        tmp_path / "packages" / "db" / "schema" / "package.json",
        {"name": "db-schema"},
    )
    _write(
        tmp_path / "packages" / "db" / "schema" / "users.ts",
        "export type User = {};",
    )

    _write_json(
        tmp_path / "packages" / "ui" / "primitives" / "buttons" / "package.json",
        {"name": "buttons"},
    )
    _write(
        tmp_path / "packages" / "ui" / "primitives" / "buttons" / "Button.tsx",
        "export const Button = () => null;",
    )

    # A bare source dir without a manifest must NOT be a workspace.
    _write(
        tmp_path / "packages" / "ui" / "primitives" / "icons" / "Icon.tsx",
        "export const Icon = () => null;",
    )

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.monorepo is True
    assert ctx.workspaces is not None
    names = {w.name for w in ctx.workspaces}
    # All three nested manifest dirs should appear; the manifest-less
    # ``icons`` source dir should not.
    assert "auth" in names
    assert "db-schema" in names
    assert "buttons" in names
    paths = {w.path for w in ctx.workspaces}
    assert "packages/ui/primitives/icons" not in paths


def test_pnpm_workspace_single_star_unchanged(tmp_path: Path) -> None:
    """``packages/*`` (no ``**``) only enumerates immediate children.

    Preserves back-compat for the existing single-level glob shape.
    """
    _write(tmp_path / "pnpm-workspace.yaml", 'packages:\n  - "packages/*"\n')
    _write_json(tmp_path / "package.json", {"name": "root", "private": True})

    _write_json(tmp_path / "packages" / "auth" / "package.json", {"name": "auth"})
    _write(tmp_path / "packages" / "auth" / "src" / "index.ts", "export {};")

    # Nested package — must NOT be picked up by single-level glob.
    _write_json(
        tmp_path / "packages" / "db" / "schema" / "package.json",
        {"name": "db-schema"},
    )
    _write(tmp_path / "packages" / "db" / "schema" / "x.ts", "export {};")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.workspaces is not None
    names = {w.name for w in ctx.workspaces}
    assert "auth" in names
    # ``packages/db`` has no manifest at the immediate level so it does
    # not become a workspace; the nested ``packages/db/schema`` must be
    # ignored under the single-star pattern.
    assert "db-schema" not in names


def test_glob_expansion_skips_node_modules(tmp_path: Path) -> None:
    """Recursive ``**`` expansion must not descend into ``node_modules/``.

    A package.json in a vendored sub-tree must never fabricate a
    workspace; the noise-dir skiplist is enforced at every level.
    """
    _write(tmp_path / "pnpm-workspace.yaml", 'packages:\n  - "packages/**"\n')
    _write_json(tmp_path / "package.json", {"name": "root", "private": True})

    # Real package
    _write_json(tmp_path / "packages" / "auth" / "package.json", {"name": "auth"})
    _write(tmp_path / "packages" / "auth" / "src" / "index.ts", "export {};")

    # Vendored package — must be ignored
    _write_json(
        tmp_path / "packages" / "auth" / "node_modules" / "lodash"
        / "package.json",
        {"name": "lodash"},
    )

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.workspaces is not None
    names = {w.name for w in ctx.workspaces}
    assert "auth" in names
    assert "lodash" not in names


# ─────────────── Error handling ────────────────


def test_stage_0_intake_rejects_nonexistent_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        stage_0_intake(tmp_path / "does-not-exist", skip_git=True)
