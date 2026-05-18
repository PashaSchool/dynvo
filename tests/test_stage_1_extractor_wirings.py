"""Fixture tests for the five built-in Stage 1 extractors.

Each test builds a tiny synthetic repo under ``tmp_path``, constructs
a ``ScanContext`` by hand (no git), and asserts the extractor emits
the expected anchor names. Orchestrator-level behaviour is tested
separately in ``test_stage_1_extractors.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2 import (
    ScanContext,
    Workspace,
)
from faultline.pipeline_v2.extractors.config import ConfigAsProductExtractor
from faultline.pipeline_v2.extractors.mvc import MVCControllerExtractor
from faultline.pipeline_v2.extractors.package import PackageAnchorExtractor
from faultline.pipeline_v2.extractors.route import RouteFileExtractor
from faultline.pipeline_v2.extractors.schema import SchemaDomainExtractor


# ── helpers ────────────────────────────────────────────────────────────────


def _ctx(
    *,
    repo_path: Path,
    stack: str | None = None,
    tracked_files: list[str] | None = None,
    monorepo: bool = False,
    workspaces: list[Workspace] | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=tracked_files or [],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── route ──────────────────────────────────────────────────────────────────


def test_route_extractor_next_app_router(tmp_path: Path) -> None:
    files = [
        "app/page.tsx",
        "app/(marketing)/about/page.tsx",
        "app/dashboard/page.tsx",
        "app/dashboard/settings/page.tsx",
        "app/api/users/route.ts",
        "app/api/billing/route.ts",
        "app/[locale]/page.tsx",   # dynamic root — skipped
    ]
    for f in files:
        _write(tmp_path / f, "export default function P() { return null }")
    ctx = _ctx(
        repo_path=tmp_path,
        stack="next-app-router",
        tracked_files=files,
    )

    cands = RouteFileExtractor().extract(ctx)
    names = {c.name for c in cands}
    # ``api`` is a noise token — under it we surface the first
    # meaningful child (``users``, ``billing``).
    assert {"about", "dashboard", "users", "billing"}.issubset(names)
    # route group ``(marketing)`` should NOT produce an anchor of its own
    assert "marketing" not in names
    # Dynamic root ``[locale]`` must not produce ``locale``.
    assert "locale" not in names
    # All candidates declare source=route
    for c in cands:
        assert c.source == "route"
        assert c.paths


def test_route_extractor_next_pages_router(tmp_path: Path) -> None:
    files = [
        "pages/index.tsx",
        "pages/dashboard.tsx",
        "pages/api/billing.ts",
        "pages/users/[id].tsx",
    ]
    for f in files:
        _write(tmp_path / f, "export default function P() {}")
    ctx = _ctx(
        repo_path=tmp_path,
        stack="next-pages",
        tracked_files=files,
    )

    cands = RouteFileExtractor().extract(ctx)
    names = {c.name for c in cands}
    # ``dashboard`` (top-level page), ``users`` (folder), and ``billing``
    # (folder under api) all become anchors. index.tsx is filtered.
    assert "dashboard" in names
    assert "users" in names


def test_route_extractor_monorepo_workspace_prefix(tmp_path: Path) -> None:
    """The route extractor must scope its routing roots to each
    workspace path when running on a monorepo."""
    files = [
        "apps/web/app/dashboard/page.tsx",
        "apps/web/app/settings/page.tsx",
        "apps/api/src/index.ts",
    ]
    for f in files:
        _write(tmp_path / f, "export default function P() {}")
    workspaces = [
        Workspace(
            name="web", path="apps/web", package_json=None,
            stack="next-app-router",
            files=["apps/web/app/dashboard/page.tsx",
                   "apps/web/app/settings/page.tsx"],
        ),
        Workspace(
            name="api", path="apps/api", package_json=None,
            stack="fastify",
            files=["apps/api/src/index.ts"],
        ),
    ]
    ctx = _ctx(
        repo_path=tmp_path,
        stack=None,
        monorepo=True,
        workspaces=workspaces,
        tracked_files=files,
    )

    cands = RouteFileExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"dashboard", "settings"} == names


# ── mvc ────────────────────────────────────────────────────────────────────


def test_mvc_extractor_rails_and_laravel(tmp_path: Path) -> None:
    files = [
        "app/controllers/users_controller.rb",
        "app/controllers/billing_controller.rb",
        "app/Http/Controllers/InvoiceController.php",
        "app/Http/Controllers/Api/WebhookController.php",
        # noise:
        "app/models/user.rb",
        "config/routes.rb",
    ]
    for f in files:
        _write(tmp_path / f, "# rb\n")
    ctx = _ctx(repo_path=tmp_path, tracked_files=files)

    cands = MVCControllerExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "users" in names
    assert "billing" in names
    assert "invoice" in names
    assert "webhook" in names
    for c in cands:
        assert c.source == "mvc"


# ── schema ─────────────────────────────────────────────────────────────────


def test_schema_extractor_prisma(tmp_path: Path) -> None:
    _write(
        tmp_path / "prisma" / "schema.prisma",
        """
        model User {
            id String @id
        }
        model BillingAccount {
            id String @id
        }
        enum Role {
            ADMIN
            USER
        }
        """.strip(),
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["prisma/schema.prisma"],
    )

    cands = SchemaDomainExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"user", "billing-account", "role"}.issubset(names)


def test_schema_extractor_drizzle_single_file(tmp_path: Path) -> None:
    _write(
        tmp_path / "db" / "schema.ts",
        """
        import { pgTable } from "drizzle-orm/pg-core";
        export const users = pgTable("users", {});
        export const subscriptions = pgTable("subscriptions", {});
        """.strip(),
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["db/schema.ts"],
    )

    cands = SchemaDomainExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "users" in names
    assert "subscriptions" in names


def test_schema_extractor_drizzle_split_by_table(tmp_path: Path) -> None:
    """Modern Drizzle pattern: per-domain .ts files inside a /schema/
    directory (openstatus / cal.com)."""
    _write(
        tmp_path / "packages" / "db" / "src" / "schema" / "monitors" / "monitor.ts",
        """
        import { pgTable } from "drizzle-orm/pg-core";
        export const monitor = pgTable("monitor", {});
        export const monitorRun = pgTable("monitor_run", {});
        """.strip(),
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["packages/db/src/schema/monitors/monitor.ts"],
    )
    cands = SchemaDomainExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "monitor" in names
    assert "monitor-run" in names


# ── package ────────────────────────────────────────────────────────────────


def test_package_extractor_js_billing_and_auth(tmp_path: Path) -> None:
    pkg = {
        "name": "my-app",
        "dependencies": {
            "stripe": "^14",
            "next": "^15",
            "next-auth": "^5",
            "resend": "^4",
        },
    }
    _write(tmp_path / "package.json", json.dumps(pkg))
    ctx = _ctx(
        repo_path=tmp_path,
        stack="next-app-router",
        tracked_files=["package.json"],
    )

    cands = PackageAnchorExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"billing", "auth", "email"}.issubset(names)
    for c in cands:
        assert c.source == "package"


def test_package_extractor_monorepo(tmp_path: Path) -> None:
    apps_web_pkg = {
        "name": "web",
        "dependencies": {"stripe": "^14", "@clerk/nextjs": "^5"},
    }
    apps_api_pkg = {
        "name": "api",
        "dependencies": {"inngest": "^3"},
    }
    _write(tmp_path / "apps" / "web" / "package.json", json.dumps(apps_web_pkg))
    _write(tmp_path / "apps" / "api" / "package.json", json.dumps(apps_api_pkg))

    workspaces = [
        Workspace(
            name="web", path="apps/web",
            package_json=apps_web_pkg, stack="next-app-router",
            files=["apps/web/package.json"],
        ),
        Workspace(
            name="api", path="apps/api",
            package_json=apps_api_pkg, stack="fastify",
            files=["apps/api/package.json"],
        ),
    ]
    ctx = _ctx(
        repo_path=tmp_path,
        monorepo=True,
        workspaces=workspaces,
        tracked_files=[
            "apps/web/package.json", "apps/api/package.json",
        ],
    )

    cands = PackageAnchorExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"billing", "auth", "background-jobs"}.issubset(names)


# ── config-as-product ──────────────────────────────────────────────────────


def test_config_extractor_vscode_extension(tmp_path: Path) -> None:
    pkg = {
        "name": "my-vscode-ext",
        "engines": {"vscode": "^1.80.0"},
        "contributes": {
            "commands": [
                {"command": "myExt.openSettings", "title": "Open Settings"},
                {"command": "myExt.refresh", "title": "Refresh"},
            ],
            "views": {
                "explorer": [{"id": "myExt.treeView", "name": "Tree"}],
            },
        },
    }
    _write(tmp_path / "package.json", json.dumps(pkg))
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["package.json"],
    )

    cands = ConfigAsProductExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "open-settings" in names
    assert "refresh" in names
    assert "tree-view" in names


def test_config_extractor_chrome_mv3(tmp_path: Path) -> None:
    manifest = {
        "manifest_version": 3,
        "name": "Demo",
        "action": {"default_popup": "popup.html"},
        "permissions": ["storage", "tabs", "scripting"],
    }
    _write(tmp_path / "manifest.json", json.dumps(manifest))
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["manifest.json"],
    )

    cands = ConfigAsProductExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"storage", "tabs", "scripting", "popup"}.issubset(names)


def test_config_extractor_skips_non_mv3_manifest(tmp_path: Path) -> None:
    manifest = {"manifest_version": 2, "permissions": ["storage"]}
    _write(tmp_path / "manifest.json", json.dumps(manifest))
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["manifest.json"],
    )
    cands = ConfigAsProductExtractor().extract(ctx)
    assert cands == []
