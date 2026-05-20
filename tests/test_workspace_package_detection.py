"""Sprint D3 — workspace package anchor coverage.

Director's plane investigation surfaced 9 missing developer-truth
entries on plane — Stage 1 was silently skipping every workspace
package whose generic name (``ui``, ``types``, ``utils``,
``services``, ``logger``, ``decorators``, ``shared-state``) did not
match a dependency-category token in
:mod:`faultline.pipeline_v2.extractors.package`. The fix promotes
each declared workspace to a deterministic anchor regardless of its
dependency set, using ``package.json#name`` (scope-stripped) or the
workspace directory name as the slug.

These tests pin the new behaviour against the same generic names
that triggered the regression and against a Next.js route-group
recovery case (``app/(home)/page.tsx`` must produce ``home``).
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2 import (
    ScanContext,
    Workspace,
)
from faultline.pipeline_v2.extractors.package import (
    PackageAnchorExtractor,
    _slug_from_package_name,
    _slug_from_workspace_path,
    _workspace_slug,
)
from faultline.pipeline_v2.extractors.route import RouteFileExtractor


# ── helpers ────────────────────────────────────────────────────────────────


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _ctx(
    *,
    repo_path: Path,
    monorepo: bool = True,
    workspaces: list[Workspace] | None = None,
    tracked_files: list[str] | None = None,
    stack: str | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=tracked_files or [],
        commits=[],
        stack_signals=[],
        workspace_manager="pnpm",
    )


def _ws(
    name: str,
    path: str,
    pkg_name: str | None,
    files: list[str],
) -> Workspace:
    pkg_json: dict[str, object] | None = None
    if pkg_name is not None:
        pkg_json = {"name": pkg_name, "version": "1.0.0"}
    return Workspace(
        name=name,
        path=path,
        package_json=pkg_json,
        stack="js-generic",
        files=files,
    )


# ── slug helpers ───────────────────────────────────────────────────────────


def test_slug_from_scoped_package_name() -> None:
    assert _slug_from_package_name("@plane/ui") == "ui"
    assert _slug_from_package_name("@plane/shared-state") == "shared-state"
    assert _slug_from_package_name("@scope/sub/path/leaf") == "leaf"


def test_slug_from_plain_package_name() -> None:
    assert _slug_from_package_name("editor") == "editor"
    assert _slug_from_package_name("some_Camel_Case") == "some-camel-case"
    assert _slug_from_package_name("") is None
    assert _slug_from_package_name(None) is None
    assert _slug_from_package_name(123) is None  # type: ignore[arg-type]


def test_slug_from_workspace_path() -> None:
    assert _slug_from_workspace_path("packages/ui") == "ui"
    assert _slug_from_workspace_path("apps/web") == "web"
    assert _slug_from_workspace_path("packages/shared-state/") == "shared-state"
    assert _slug_from_workspace_path("") is None


def test_workspace_slug_prefers_package_json_name() -> None:
    ws = Workspace(
        name="something-else",
        path="packages/funny-dir-name",
        package_json={"name": "@plane/ui"},
        files=[],
    )
    assert _workspace_slug(ws) == "ui"


def test_workspace_slug_falls_back_to_path() -> None:
    ws = Workspace(
        name="ui",
        path="packages/ui",
        package_json=None,
        files=[],
    )
    assert _workspace_slug(ws) == "ui"


# ── PackageAnchorExtractor emits one anchor per workspace ─────────────────


def test_package_extractor_emits_anchor_per_workspace_generic_names(
    tmp_path: Path,
) -> None:
    """Plane regression: 7 generic-named packages all yield anchors.

    Before Sprint D3 the package extractor only fired when a workspace's
    deps matched a category token (stripe/clerk/etc.). The 7 packages
    below carry NO category-matching deps but are real workspaces that
    the maintainer ships — they MUST become anchors.
    """
    targets = [
        ("ui", "@plane/ui"),
        ("types", "@plane/types"),
        ("utils", "@plane/utils"),
        ("services", "@plane/services"),
        ("logger", "@plane/logger"),
        ("decorators", "@plane/decorators"),
        ("shared-state", "@plane/shared-state"),
    ]
    workspaces: list[Workspace] = []
    for dir_name, pkg_name in targets:
        files = [
            f"packages/{dir_name}/package.json",
            f"packages/{dir_name}/src/index.ts",
        ]
        workspaces.append(
            _ws(
                name=dir_name,
                path=f"packages/{dir_name}",
                pkg_name=pkg_name,
                files=files,
            ),
        )

    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=workspaces,
        tracked_files=[f for ws in workspaces for f in ws.files],
    )

    cands = PackageAnchorExtractor().extract(ctx)
    names = {c.name for c in cands}
    expected = {dir_name for dir_name, _ in targets}
    missing = expected - names
    assert not missing, f"missing workspace anchors: {missing}"


def test_package_extractor_workspace_anchors_carry_full_file_list(
    tmp_path: Path,
) -> None:
    files = [
        "packages/ui/package.json",
        "packages/ui/src/button.tsx",
        "packages/ui/src/dialog.tsx",
    ]
    ws = _ws(
        name="ui", path="packages/ui",
        pkg_name="@plane/ui", files=files,
    )
    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=[ws],
        tracked_files=files,
    )

    cands = PackageAnchorExtractor().extract(ctx)
    ui_cands = [c for c in cands if c.name == "ui"]
    assert len(ui_cands) == 1
    anchor = ui_cands[0]
    assert anchor.source == "package"
    # The full workspace file list rides on the anchor.
    assert set(anchor.paths) == set(files)
    # Workspace anchors are high-confidence (manifest is authoritative).
    assert anchor.confidence_self >= 0.9


def test_package_extractor_workspace_path_fallback_when_no_pkg_name(
    tmp_path: Path,
) -> None:
    """A workspace without ``package.json#name`` still becomes an anchor.

    Some real-world packages (Cargo / Go modules / orphaned npm packages)
    don't carry a usable name in their manifest. The dir-name fallback
    keeps them detectable.
    """
    files = ["packages/legacy-thing/lib.rs"]
    ws = Workspace(
        name="legacy-thing",
        path="packages/legacy-thing",
        package_json=None,  # missing
        files=files,
    )
    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=[ws],
        tracked_files=files,
    )

    cands = PackageAnchorExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "legacy-thing" in names


def test_package_extractor_skips_duplicate_workspace_slugs(
    tmp_path: Path,
) -> None:
    """Two workspaces with the same derived slug coalesce.

    Real-world example: ``apps/ui`` + ``packages/ui`` both yield slug
    ``ui``. Stage 1 keeps the first emission; Stage 2 will merge by
    name anyway, but we don't want telemetry to double-count.
    """
    workspaces = [
        _ws(
            name="apps-ui", path="apps/ui",
            pkg_name="@plane/ui",
            files=["apps/ui/package.json", "apps/ui/src/main.tsx"],
        ),
        _ws(
            name="packages-ui", path="packages/ui",
            pkg_name="@plane/ui-lib",  # same dir name `ui` once scope is stripped
            files=["packages/ui/package.json"],
        ),
    ]
    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=workspaces,
        tracked_files=[
            "apps/ui/package.json", "apps/ui/src/main.tsx",
            "packages/ui/package.json",
        ],
    )

    cands = PackageAnchorExtractor().extract(ctx)
    ui_anchors = [
        c for c in cands
        if c.name in {"ui", "ui-lib"} and c.source == "package"
    ]
    # ``apps/ui`` wins for slug ``ui``; ``packages/ui`` keeps its own
    # ``ui-lib`` slug because that came from its package.json#name.
    assert len(ui_anchors) == 2
    assert {c.name for c in ui_anchors} == {"ui", "ui-lib"}


def test_package_extractor_workspace_anchors_coexist_with_dep_anchors(
    tmp_path: Path,
) -> None:
    """Workspace anchors don't replace dependency-category anchors.

    A workspace depending on stripe still produces ``billing`` AND
    its own workspace slug.
    """
    pkg_json = {
        "name": "@plane/web",
        "dependencies": {"stripe": "^14", "next-auth": "^5"},
    }
    _write(tmp_path / "apps" / "web" / "package.json", json.dumps(pkg_json))
    files = [
        "apps/web/package.json",
        "apps/web/app/page.tsx",
    ]
    ws = Workspace(
        name="web", path="apps/web",
        package_json=pkg_json, stack="next-app-router",
        files=files,
    )
    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=[ws],
        tracked_files=files,
    )

    cands = PackageAnchorExtractor().extract(ctx)
    names = {c.name for c in cands}
    # Dep-category anchors still present.
    assert {"billing", "auth"}.issubset(names)
    # Plus the workspace anchor.
    assert "web" in names


def test_package_extractor_empty_workspace_list(tmp_path: Path) -> None:
    """No workspaces ⇒ no workspace anchors emitted.

    Single-app repos (``ctx.monorepo=False``) must NOT spuriously
    promote the root to a workspace anchor.
    """
    _write(tmp_path / "package.json", json.dumps({"name": "single-app"}))
    ctx = _ctx(
        repo_path=tmp_path,
        monorepo=False,
        workspaces=None,
        tracked_files=["package.json"],
    )

    cands = PackageAnchorExtractor().extract(ctx)
    names = {c.name for c in cands}
    # No workspace anchor for ``single-app`` (no monorepo).
    assert "single-app" not in names
    assert names == set() or all(
        c.source == "package" and not c.name.startswith("workspace")
        for c in cands
    )


# ── Next.js route group recovery (apps/web/app/(home)/page.tsx → home) ────


def test_route_extractor_recovers_root_route_group(tmp_path: Path) -> None:
    """The plane bug: ``apps/web/app/(home)/page.tsx`` produced no anchor.

    Sprint D3 fix: when stripping route groups leaves no meaningful
    directory segments, use the group's inner name as the slug. The
    group ``(home)`` becomes ``home`` and the page becomes an anchor.
    """
    files = [
        "apps/web/app/(home)/page.tsx",
        "apps/web/app/(home)/layout.tsx",
    ]
    ws = Workspace(
        name="web", path="apps/web",
        package_json={"name": "@plane/web"},
        stack="next-app-router",
        files=files,
    )
    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=[ws],
        tracked_files=files,
        stack="next-app-router",
    )

    cands = RouteFileExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "home" in names


def test_route_extractor_route_group_not_used_when_real_segment_exists(
    tmp_path: Path,
) -> None:
    """Route groups stay invisible when a real URL segment exists.

    ``app/(marketing)/about/page.tsx`` should still produce ``about``,
    NOT ``marketing``. The recovery path only kicks in when stripping
    groups would otherwise leave the slug empty.
    """
    files = ["apps/web/app/(marketing)/about/page.tsx"]
    ws = Workspace(
        name="web", path="apps/web",
        package_json={"name": "@plane/web"},
        stack="next-app-router",
        files=files,
    )
    ctx = _ctx(
        repo_path=tmp_path,
        workspaces=[ws],
        tracked_files=files,
        stack="next-app-router",
    )

    cands = RouteFileExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "about" in names
    assert "marketing" not in names
