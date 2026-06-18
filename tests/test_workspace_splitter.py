"""Phase 1 — monorepo workspace-splitter.

Synthetic fixture trees (NOT real corpus paths) exercise the splitter's
contract:
  * single-package repo → ``[root]`` (non-monorepo path preserved),
  * pnpm / turbo / nx layouts → one workspace per declared package,
  * files scoped per workspace; root-level files stay on root.

Deterministic. No LLM, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.profiles import (
    ROOT_WORKSPACE_NAME,
    is_monorepo,
    split_workspaces,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# ── fixture helpers ─────────────────────────────────────────────────────────


def _write(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _pkg_json(name: str) -> str:
    return json.dumps({"name": name, "version": "0.0.0"})


def _ctx(
    repo: Path,
    *,
    tracked_files: list[str],
    monorepo: bool = False,
    workspaces: list[Workspace] | None = None,
    stack: str | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=repo,
        stack=stack,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=tracked_files,
        commits=[],
    )


# ── single-package repo (the byte-for-byte non-monorepo path) ────────────────


def test_single_package_returns_root_workspace(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", _pkg_json("solo-app"))
    _write(tmp_path / "src/index.ts")
    files = ["package.json", "src/index.ts"]

    ws = split_workspaces(_ctx(tmp_path, tracked_files=files, stack="next-app-router"))

    assert len(ws) == 1
    root = ws[0]
    assert root.name == ROOT_WORKSPACE_NAME
    assert root.path == ""
    assert root.files == files  # every tracked file on the single scope
    assert root.stack == "next-app-router"  # carries ctx stack
    assert not is_monorepo(ws)


def test_repo_with_no_manifest_still_returns_root(tmp_path: Path) -> None:
    _write(tmp_path / "main.py")
    ws = split_workspaces(_ctx(tmp_path, tracked_files=["main.py"]))
    assert len(ws) == 1 and ws[0].name == ROOT_WORKSPACE_NAME
    assert ws[0].package_json is None


# ── pnpm ─────────────────────────────────────────────────────────────────────


def test_pnpm_enumerates_workspaces(tmp_path: Path) -> None:
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    _write(tmp_path / "apps/web/package.json", _pkg_json("@acme/web"))
    _write(tmp_path / "apps/web/page.tsx")
    _write(tmp_path / "apps/api/package.json", _pkg_json("@acme/api"))
    _write(tmp_path / "apps/api/server.ts")
    _write(tmp_path / "packages/ui/package.json", _pkg_json("@acme/ui"))
    _write(tmp_path / "packages/ui/button.tsx")
    files = [
        "pnpm-workspace.yaml",
        "apps/web/package.json", "apps/web/page.tsx",
        "apps/api/package.json", "apps/api/server.ts",
        "packages/ui/package.json", "packages/ui/button.tsx",
    ]

    ws = split_workspaces(_ctx(tmp_path, tracked_files=files))

    names = sorted(w.name for w in ws)
    assert names == ["api", "ui", "web"]  # scope-stripped pkg names
    assert is_monorepo(ws)


def test_pnpm_scopes_files_per_workspace(tmp_path: Path) -> None:
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _write(tmp_path / "apps/web/package.json", _pkg_json("web"))
    _write(tmp_path / "apps/web/page.tsx")
    _write(tmp_path / "apps/admin/package.json", _pkg_json("admin"))
    _write(tmp_path / "apps/admin/dashboard.tsx")
    files = [
        "apps/web/package.json", "apps/web/page.tsx",
        "apps/admin/package.json", "apps/admin/dashboard.tsx",
    ]

    ws = {w.name: w for w in split_workspaces(_ctx(tmp_path, tracked_files=files))}

    assert sorted(ws["web"].files) == ["apps/web/package.json", "apps/web/page.tsx"]
    assert sorted(ws["admin"].files) == [
        "apps/admin/dashboard.tsx",
        "apps/admin/package.json",
    ]
    # No cross-contamination between scopes.
    assert all("admin" not in f for f in ws["web"].files)


def test_pnpm_reads_package_json_per_workspace(tmp_path: Path) -> None:
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _write(tmp_path / "apps/web/package.json", _pkg_json("@acme/web"))
    _write(tmp_path / "apps/web/page.tsx")
    files = ["apps/web/package.json", "apps/web/page.tsx"]

    ws = split_workspaces(_ctx(tmp_path, tracked_files=files))[0]
    assert ws.package_json is not None
    assert ws.package_json["name"] == "@acme/web"


# ── turbo (delegates to pnpm/npm workspaces) ─────────────────────────────────


def test_turbo_enumerates_workspaces(tmp_path: Path) -> None:
    _write(tmp_path / "turbo.json", json.dumps({"pipeline": {}}))
    _write(tmp_path / "package.json", json.dumps({
        "name": "root", "workspaces": ["apps/*", "packages/*"],
    }))
    _write(tmp_path / "apps/web/package.json", _pkg_json("web"))
    _write(tmp_path / "apps/web/index.ts")
    _write(tmp_path / "packages/db/package.json", _pkg_json("db"))
    _write(tmp_path / "packages/db/schema.ts")
    files = [
        "turbo.json", "package.json",
        "apps/web/package.json", "apps/web/index.ts",
        "packages/db/package.json", "packages/db/schema.ts",
    ]

    ws = split_workspaces(_ctx(tmp_path, tracked_files=files))
    assert sorted(w.name for w in ws) == ["db", "web"]
    assert is_monorepo(ws)


# ── nx ───────────────────────────────────────────────────────────────────────


def test_nx_enumerates_projects(tmp_path: Path) -> None:
    _write(tmp_path / "nx.json", json.dumps({"npmScope": "acme"}))
    _write(tmp_path / "apps/store/project.json", json.dumps({"name": "store"}))
    _write(tmp_path / "apps/store/main.ts")
    _write(tmp_path / "libs/auth/project.json", json.dumps({"name": "auth"}))
    _write(tmp_path / "libs/auth/auth.ts")
    files = [
        "nx.json",
        "apps/store/project.json", "apps/store/main.ts",
        "libs/auth/project.json", "libs/auth/auth.ts",
    ]

    ws = split_workspaces(_ctx(tmp_path, tracked_files=files))
    assert sorted(w.name for w in ws) == ["auth", "store"]
    assert is_monorepo(ws)


# ── stage-0-populated workspaces are trusted (no re-walk) ─────────────────────


def test_existing_workspaces_are_normalised_not_recomputed(tmp_path: Path) -> None:
    # No manifests on disk — proves the splitter trusts ctx.workspaces
    # rather than re-detecting.
    pre = [
        Workspace(name="web", path="apps/web", files=["apps/web/a.ts"]),
        Workspace(name="api", path="apps/api", files=["apps/api/b.ts"]),
    ]
    ctx = _ctx(
        tmp_path,
        tracked_files=["apps/web/a.ts", "apps/api/b.ts"],
        monorepo=True,
        workspaces=pre,
    )

    ws = split_workspaces(ctx)
    assert sorted(w.name for w in ws) == ["api", "web"]
    # Returned objects are copies — mutating them must not touch ctx.
    ws[0].files.append("MUTATED")
    assert "MUTATED" not in pre[0].files and "MUTATED" not in pre[1].files


def test_monorepo_flag_without_workspaces_falls_through_to_detection(
    tmp_path: Path,
) -> None:
    # monorepo=True but workspaces=None → splitter must still resolve via
    # detect_workspace, not return an empty / crashing result.
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    _write(tmp_path / "apps/web/package.json", _pkg_json("web"))
    _write(tmp_path / "apps/web/page.tsx")
    files = ["apps/web/package.json", "apps/web/page.tsx"]

    ctx = _ctx(tmp_path, tracked_files=files, monorepo=True, workspaces=None)
    ws = split_workspaces(ctx)
    assert [w.name for w in ws] == ["web"]


# ── is_monorepo helper ───────────────────────────────────────────────────────


def test_is_monorepo_false_for_root_only(tmp_path: Path) -> None:
    _write(tmp_path / "main.py")
    assert is_monorepo(split_workspaces(_ctx(tmp_path, tracked_files=["main.py"]))) is False
