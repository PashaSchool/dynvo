"""Workspace-granular filesystem-routing roots + React Router v7 detection.

Regression suite for the nested-monorepo route-root gap found during
PR #46 validation: documenso's main app lives at
``apps/remix/app/routes/**`` and produced ZERO route anchors because
the workspace was stack-tagged ``hono`` (its RRv7 server adapter dep)
— React Router v7 framework mode had no detection branch, so the
remix-style routing table was never consulted for that workspace.

Covers:
  - Stage 0: ``@react-router/*`` / ``react-router.config.*`` →
    ``react-router`` stack, beating backend-adapter deps (hono);
    plain ``react-router`` runtime dep alone (library-mode SPA) does
    NOT trigger framework mode.
  - RouteFileExtractor: nested workspace roots anchor; repo-root remix
    unchanged; sibling-prefix dirs don't false-match; non-monorepo
    behaviour unchanged; remix flat-route slug conventions
    (``folder+``, ``_pathless``, ``$param``, dot-delimited stems).
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.extractors.route import RouteFileExtractor
from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    Workspace,
    detect_stack,
)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2))


def _ctx(
    tmp_path: Path,
    *,
    stack: str | None,
    files: list[str],
    workspaces: list[Workspace] | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=stack,
        monorepo=workspaces is not None,
        workspaces=workspaces,
        tracked_files=files,
        commits=[],
    )


def _anchor_map(ctx: ScanContext) -> dict[str, tuple[str, ...]]:
    return {c.name: c.paths for c in RouteFileExtractor().extract(ctx)}


# ─────────────── Stage 0: React Router v7 framework-mode detection ──────────


def test_react_router_framework_mode_beats_hono_adapter(tmp_path: Path) -> None:
    """RRv7 app with a hono server adapter must NOT be tagged ``hono``."""
    _write_json(tmp_path / "package.json", {
        "name": "@acme/remix",
        "dependencies": {
            "@react-router/node": "^7.0.0",
            "react-router": "^7.0.0",
            "hono": "^4.0.0",
        },
        "devDependencies": {"@react-router/dev": "^7.0.0"},
    })
    _write(tmp_path / "react-router.config.ts", "export default {}")
    _write(tmp_path / "app" / "routes" / "_index.tsx")

    stack, signals = detect_stack(tmp_path, ["app/routes/_index.tsx"])
    assert stack == "react-router"
    assert any("React Router framework mode" in s for s in signals)


def test_react_router_config_file_alone_detects_framework_mode(
    tmp_path: Path,
) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "rr-app",
        "dependencies": {"react-router": "^7.0.0"},
    })
    _write(tmp_path / "react-router.config.mjs", "export default {}")

    stack, _ = detect_stack(tmp_path, [])
    assert stack == "react-router"


def test_react_router_dev_dependency_detects_framework_mode(
    tmp_path: Path,
) -> None:
    """``@react-router/dev`` is conventionally a devDependency."""
    _write_json(tmp_path / "package.json", {
        "name": "rr-app",
        "dependencies": {"react-router": "^7.0.0"},
        "devDependencies": {"@react-router/dev": "^7.0.0"},
    })

    stack, _ = detect_stack(tmp_path, [])
    assert stack == "react-router"


def test_plain_react_router_dep_is_not_framework_mode(tmp_path: Path) -> None:
    """Library-mode SPA routing (plain ``react-router`` dep, no scoped
    packages / config) must NOT claim a file-system routing stack."""
    _write_json(tmp_path / "package.json", {
        "name": "spa",
        "dependencies": {"react-router": "^7.0.0", "react": "^19.0.0"},
    })

    stack, _ = detect_stack(tmp_path, [])
    assert stack != "react-router"


def test_remix_classic_detection_unchanged(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "remix-app",
        "dependencies": {"@remix-run/node": "^2.0.0"},
    })

    stack, _ = detect_stack(tmp_path, [])
    assert stack == "remix"


# ─────────────── RouteFileExtractor: nested workspace roots ─────────────────


def test_nested_react_router_workspace_anchors(tmp_path: Path) -> None:
    """A RRv7 workspace nested at apps/remix must produce route anchors
    with the workspace prefix prepended to the ``app/routes/`` root."""
    files = [
        "apps/remix/app/routes/_authenticated+/documents._index.tsx",
        "apps/remix/app/routes/_authenticated+/documents.$id.tsx",
        "apps/remix/app/routes/_authenticated+/admin+/users.$id.tsx",
        "apps/remix/app/routes/_unauthenticated+/signin.tsx",
        "apps/remix/app/root.tsx",
    ]
    ws = Workspace(
        name="@acme/remix", path="apps/remix",
        stack="react-router", files=files,
    )
    ctx = _ctx(tmp_path, stack="next-app-router", files=files, workspaces=[ws])

    anchors = _anchor_map(ctx)
    assert anchors, "nested workspace produced zero route anchors"
    assert set(anchors) == {"documents", "admin", "signin"}
    assert anchors["documents"] == (
        "apps/remix/app/routes/_authenticated+/documents.$id.tsx",
        "apps/remix/app/routes/_authenticated+/documents._index.tsx",
    )


def test_nested_remix_workspace_inside_next_monorepo(tmp_path: Path) -> None:
    """A classic Remix workspace inside a Next-labelled monorepo must use
    the remix table, not the repo-level stack's."""
    remix_files = [
        "apps/web/app/routes/invoices.$id.tsx",
        "apps/web/app/routes/invoices._index.tsx",
    ]
    next_files = ["apps/docs/src/app/guides/page.tsx"]
    ws_remix = Workspace(
        name="web", path="apps/web", stack="remix", files=remix_files,
    )
    ws_next = Workspace(
        name="docs", path="apps/docs",
        stack="next-app-router", files=next_files,
    )
    ctx = _ctx(
        tmp_path, stack="next-app-router",
        files=remix_files + next_files, workspaces=[ws_remix, ws_next],
    )

    anchors = _anchor_map(ctx)
    assert "invoices" in anchors
    assert "guides" in anchors


def test_sibling_prefix_directory_does_not_false_match(tmp_path: Path) -> None:
    """Files under ``apps/remix-docs/`` must NOT match the ``apps/remix``
    workspace's rooted prefix (trailing-slash guard)."""
    ws_files = ["apps/remix/app/routes/billing.tsx"]
    stray = ["apps/remix-docs/app/routes/marketing.tsx"]
    ws = Workspace(
        name="remix", path="apps/remix", stack="react-router", files=ws_files,
    )
    ctx = _ctx(tmp_path, stack=None, files=ws_files + stray, workspaces=[ws])

    anchors = _anchor_map(ctx)
    assert "billing" in anchors
    assert "marketing" not in anchors
    assert all(
        not p.startswith("apps/remix-docs/")
        for paths in anchors.values() for p in paths
    )


# ─────────────── Repo-root behaviour unchanged (regression guard) ───────────


def test_repo_root_remix_still_anchors(tmp_path: Path) -> None:
    files = [
        "app/routes/projects.$id.tsx",
        "app/routes/projects._index.tsx",
        "app/routes/login.tsx",
    ]
    ctx = _ctx(tmp_path, stack="remix", files=files)

    anchors = _anchor_map(ctx)
    assert set(anchors) == {"projects", "login"}


def test_non_monorepo_next_app_router_unchanged(tmp_path: Path) -> None:
    files = [
        "app/(marketing)/pricing/page.tsx",
        "app/dashboard/page.tsx",
        "app/dashboard/settings/page.tsx",
        "app/api/webhooks/route.ts",
    ]
    ctx = _ctx(tmp_path, stack="next-app-router", files=files)

    anchors = _anchor_map(ctx)
    assert anchors["pricing"] == ("app/(marketing)/pricing/page.tsx",)
    assert anchors["dashboard"] == (
        "app/dashboard/page.tsx",
        "app/dashboard/settings/page.tsx",
    )
    assert anchors["webhooks"] == ("app/api/webhooks/route.ts",)


# ─────────────── Remix flat-route slug conventions ──────────────────────────


def test_flat_route_conventions_produce_clean_slugs(tmp_path: Path) -> None:
    files = [
        # pathless layout folders are organisational; ``admin+`` is the
        # flat-route group that names the feature
        "app/routes/_authenticated+/admin+/stats.tsx",
        "app/routes/_authenticated+/admin+/_index.tsx",
        # dot-delimited stem: first meaningful sub-segment wins
        "app/routes/_unauthenticated+/reset-password.$token.tsx",
        # param-only tail under a pathless dir → stem segment ``ingest``
        "app/routes/_redirects+/ingest.$.tsx",
        # layout file of a pathless group → no anchor at all
        "app/routes/_unauthenticated+/_layout.tsx",
    ]
    ctx = _ctx(tmp_path, stack="react-router", files=files)

    anchors = _anchor_map(ctx)
    assert anchors["admin"] == (
        "app/routes/_authenticated+/admin+/_index.tsx",
        "app/routes/_authenticated+/admin+/stats.tsx",
    )
    assert anchors["reset-password"] == (
        "app/routes/_unauthenticated+/reset-password.$token.tsx",
    )
    assert anchors["ingest"] == ("app/routes/_redirects+/ingest.$.tsx",)
    # nothing named after pathless layouts / params
    assert not any(
        n.startswith(("authenticated", "unauthenticated")) for n in anchors
    )
