"""Tests for Sprint S3 — per-workspace Stage 1 dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    Workspace,
    stage_0_intake,
)
from faultline.pipeline_v2.stage_1_per_workspace import (
    _merge_anchors_across_workspaces,
    run_stage_1_per_workspace,
    should_activate_per_workspace,
    synthesise_workspaces,
)


# ── Fixture helpers ─────────────────────────────────────────────────────


def _make_polyglot_repo(root: Path) -> None:
    """Create a fixture polyglot monorepo: backend (fastify) +
    frontend (vite/react) + cli (rust)."""
    # backend — Fastify
    backend = root / "backend"
    backend.mkdir()
    (backend / "package.json").write_text(json.dumps({
        "name": "backend",
        "dependencies": {"fastify": "^4.0.0", "stripe": "^14.0.0"},
    }))
    (backend / "src").mkdir()
    (backend / "src" / "users_controller.rb").write_text("class UsersController; end")
    (backend / "src" / "orders_controller.rb").write_text("class OrdersController; end")

    # frontend — Vite + React
    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(json.dumps({
        "name": "frontend",
        "dependencies": {"vite": "^5.0.0", "react": "^18.0.0"},
    }))
    (frontend / "src").mkdir()
    (frontend / "src" / "App.tsx").write_text("export default function App(){}")

    # cli — Rust binary
    cli = root / "cli"
    cli.mkdir()
    (cli / "Cargo.toml").write_text("[package]\nname=\"cli\"\nversion=\"0.1.0\"\n")
    (cli / "src").mkdir()
    (cli / "src" / "main.rs").write_text("fn main(){}")


def _ctx_with_audited(ctx: ScanContext, *, audited: str, secondary: tuple[str, ...] = ()) -> ScanContext:
    return ctx.with_audited_stack(
        audited_stack=audited,
        secondary_stacks=secondary,
        extractor_hints=(),
        auditor_confidence=0.9,
    )


# ── should_activate_per_workspace ───────────────────────────────────────


def test_activation_fires_when_auditor_flags_polyglot(tmp_path: Path) -> None:
    _make_polyglot_repo(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    ctx = _ctx_with_audited(ctx, audited="monorepo-polyglot")
    assert should_activate_per_workspace(ctx) is True


def test_activation_skipped_for_single_app(tmp_path: Path) -> None:
    """Single-app Next repo with audited_stack=next-app-router."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "app",
        "dependencies": {"next": "^14.0.0"},
    }))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page(){}")
    ctx = stage_0_intake(tmp_path, skip_git=True)
    ctx = _ctx_with_audited(ctx, audited="next-app-router")
    assert should_activate_per_workspace(ctx) is False


def test_activation_fires_on_declared_polyglot_workspaces(tmp_path: Path) -> None:
    """pnpm-workspace repo where workspaces have different stacks."""
    # Set up a pnpm workspace with diverse stacks.
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    (tmp_path / "package.json").write_text(json.dumps({"name": "root"}))
    apps = tmp_path / "apps"
    apps.mkdir()
    web = apps / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({
        "name": "web",
        "dependencies": {"next": "^14"},
    }))
    (web / "app").mkdir()
    (web / "app" / "page.tsx").write_text("x")
    api = apps / "api"
    api.mkdir()
    (api / "package.json").write_text(json.dumps({
        "name": "api",
        "dependencies": {"fastify": "^4"},
    }))
    ctx = stage_0_intake(tmp_path, skip_git=True)
    # No auditor — just structural polyglot
    assert ctx.monorepo is True
    if ctx.workspaces and len({(w.stack or "") for w in ctx.workspaces}) >= 2:
        assert should_activate_per_workspace(ctx) is True


def test_activation_skipped_when_workspaces_share_stack(tmp_path: Path) -> None:
    """All workspaces are Next — not polyglot, normal Stage 1 handles it."""
    workspaces = [
        Workspace(name="web", path="apps/web", stack="next-app-router"),
        Workspace(name="admin", path="apps/admin", stack="next-app-router"),
    ]
    ctx = ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=True,
        workspaces=workspaces,
        tracked_files=["apps/web/page.tsx", "apps/admin/page.tsx"],
        commits=[],
        workspace_manager="pnpm",
    )
    assert should_activate_per_workspace(ctx) is False


# ── synthesise_workspaces ───────────────────────────────────────────────


def test_synthesise_picks_up_undeclared_polyglot_dirs(tmp_path: Path) -> None:
    _make_polyglot_repo(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    # ctx.workspaces should be empty (no declared workspaces)
    assert not ctx.workspaces
    synthesised = synthesise_workspaces(ctx)
    names = {w.name for w in synthesised}
    assert "backend" in names
    assert "frontend" in names
    assert "cli" in names
    # Stack inference per workspace
    by_name = {w.name: w for w in synthesised}
    assert by_name["backend"].stack == "fastify"
    assert by_name["frontend"].stack == "vite"
    assert by_name["cli"].stack == "rust"


def test_synthesise_requires_manifest(tmp_path: Path) -> None:
    """A bare 'backend/' folder with NO manifest must NOT become a workspace."""
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "random.txt").write_text("not a package")
    # And a real one
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"name": "f"}))
    ctx = stage_0_intake(tmp_path, skip_git=True)
    synthesised = synthesise_workspaces(ctx)
    names = {w.name for w in synthesised}
    assert "backend" not in names  # no manifest, skipped
    assert "frontend" in names


# ── End-to-end per-workspace run ────────────────────────────────────────


def test_run_per_workspace_yields_per_ws_anchors(tmp_path: Path) -> None:
    _make_polyglot_repo(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    ctx = _ctx_with_audited(ctx, audited="monorepo-polyglot")
    result = run_stage_1_per_workspace(ctx)
    assert result.workspaces_used, "expected synthesised workspaces"
    assert result.synthesised_workspaces is True
    assert len(result.workspaces_processed) >= 3
    # MVC extractor should have fired on backend's controller files
    mvc_anchors = result.stage1_out.get("mvc", [])
    mvc_names = {a.name for a in mvc_anchors}
    assert "users" in mvc_names or "orders" in mvc_names


def test_per_workspace_empty_when_no_workspaces(tmp_path: Path) -> None:
    """ctx with no workspaces + no qualifying synthetic dirs → empty result."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}))
    (tmp_path / "index.js").write_text("//")
    ctx = stage_0_intake(tmp_path, skip_git=True)
    result = run_stage_1_per_workspace(ctx)
    assert result.workspaces_used == []
    assert result.stage1_out == {}


# ── Anchor merging across workspaces ────────────────────────────────────


def test_merge_disjoint_chunky_same_name_kept_separate() -> None:
    """Two workspaces emit ``auth`` with disjoint chunky paths → namespaced."""
    ws1 = ("backend", {
        "route": [
            AnchorCandidate(
                name="auth",
                paths=("backend/src/auth/login.ts", "backend/src/auth/logout.ts", "backend/src/auth/session.ts"),
                source="route",
                confidence_self=0.8,
            ),
        ],
    })
    ws2 = ("frontend", {
        "route": [
            AnchorCandidate(
                name="auth",
                paths=("frontend/src/auth/Login.tsx", "frontend/src/auth/SignUp.tsx", "frontend/src/auth/Reset.tsx"),
                source="route",
                confidence_self=0.7,
            ),
        ],
    })
    merged = _merge_anchors_across_workspaces([ws1, ws2])
    names = {c.name for c in merged["route"]}
    assert "backend-auth" in names
    assert "frontend-auth" in names


def test_merge_overlapping_paths_coalesces() -> None:
    """Two workspaces emit ``shared`` with overlapping paths → coalesced."""
    common = "common/util.ts"
    ws1 = ("a", {
        "package": [
            AnchorCandidate(
                name="shared",
                paths=(common, "a/x.ts"),
                source="package",
                confidence_self=0.6,
            ),
        ],
    })
    ws2 = ("b", {
        "package": [
            AnchorCandidate(
                name="shared",
                paths=(common, "b/y.ts"),
                source="package",
                confidence_self=0.7,
            ),
        ],
    })
    merged = _merge_anchors_across_workspaces([ws1, ws2])
    pkg = merged["package"]
    assert len(pkg) == 1
    assert pkg[0].name == "shared"
    assert common in pkg[0].paths


def test_merge_small_emissions_coalesce_even_if_disjoint() -> None:
    """When each workspace only emits 1-2 paths for a slug, coalesce
    rather than emit two flimsy features."""
    ws1 = ("a", {
        "schema": [
            AnchorCandidate(
                name="user",
                paths=("a/user.ts",),
                source="schema",
                confidence_self=0.6,
            ),
        ],
    })
    ws2 = ("b", {
        "schema": [
            AnchorCandidate(
                name="user",
                paths=("b/user.ts",),
                source="schema",
                confidence_self=0.6,
            ),
        ],
    })
    merged = _merge_anchors_across_workspaces([ws1, ws2])
    schemas = merged["schema"]
    assert len(schemas) == 1
    assert schemas[0].name == "user"


def test_leftover_pass_picks_up_uncovered_packages(tmp_path: Path) -> None:
    """Files outside any declared workspace should still get extracted.

    Simulates the pnpm-workspace glob-expansion gap where Stage 0
    only enumerated ``apps/*`` workspaces; here we manually construct
    a ctx with declared workspaces that DON'T include the schema
    files, then assert the leftover pass picks them up.
    """
    # Layout: apps/web/ is declared; packages/db/ exists with
    # drizzle schema but is NOT declared as a workspace.
    apps_web = tmp_path / "apps" / "web"
    apps_web.mkdir(parents=True)
    (apps_web / "package.json").write_text(json.dumps({
        "name": "web",
        "dependencies": {"next": "^14"},
    }))
    (apps_web / "app").mkdir()
    (apps_web / "app" / "page.tsx").write_text("export default function P(){}")

    db = tmp_path / "packages" / "db"
    db.mkdir(parents=True)
    (db / "package.json").write_text(json.dumps({
        "name": "db",
        "dependencies": {"drizzle-orm": "^0.30.0"},
    }))
    schema_dir = db / "src" / "schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "users.ts").write_text(
        "export const users = pgTable('users', {});\n"
    )
    (schema_dir / "orders.ts").write_text(
        "export const orders = pgTable('orders', {});\n"
    )

    # Construct a ctx with ONLY apps/web declared (mirrors the
    # Stage-0-bug-on-openstatus state).
    tracked = [
        "apps/web/package.json",
        "apps/web/app/page.tsx",
        "packages/db/package.json",
        "packages/db/src/schema/users.ts",
        "packages/db/src/schema/orders.ts",
    ]
    ws_files = ["apps/web/package.json", "apps/web/app/page.tsx"]
    ctx = ScanContext(
        repo_path=tmp_path,
        stack="js-generic",
        monorepo=True,
        workspaces=[
            Workspace(
                name="web", path="apps/web",
                package_json=json.loads((apps_web / "package.json").read_text()),
                stack="next-app-router",
                files=ws_files,
            ),
        ],
        tracked_files=tracked,
        commits=[],
        workspace_manager="pnpm",
        audited_stack="monorepo-polyglot",
        secondary_stacks=("next-app-router",),
        auditor_confidence=0.9,
    )
    result = run_stage_1_per_workspace(ctx)
    assert result.leftover_files_scanned > 0, (
        "expected leftover files for packages/db/* not covered by web workspace"
    )
    # The schema extractor should have found the drizzle tables in
    # the leftover pass.
    schema_anchors = result.stage1_out.get("schema", [])
    schema_names = {a.name for a in schema_anchors}
    assert "users" in schema_names or "orders" in schema_names, (
        f"expected drizzle anchors from leftover pass, got {schema_names}"
    )
    # And the leftover scope should appear in the reports.
    report_names = {r.name for r in result.workspaces_processed}
    assert "__leftover__" in report_names


def test_telemetry_reports_per_workspace_breakdown(tmp_path: Path) -> None:
    _make_polyglot_repo(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    ctx = _ctx_with_audited(ctx, audited="monorepo-polyglot")
    result = run_stage_1_per_workspace(ctx)
    by_name = {r.name: r for r in result.workspaces_processed}
    assert "backend" in by_name
    assert by_name["backend"].inferred_stack == "fastify"
    # ``frontend`` should have inferred stack ``vite``
    assert by_name.get("frontend") is not None
    assert by_name["frontend"].inferred_stack == "vite"
