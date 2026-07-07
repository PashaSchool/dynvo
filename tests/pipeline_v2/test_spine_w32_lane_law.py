"""Product-Spine W3.2 — the flowful-never-lane law holds for EVERY
flowful dev, regardless of anchor family.

wave31 (keyed, 2026-07-07) shipped exactly one core I1-14 regression:
typebot dev `user` — 8 account-management flows, 20 files split between
``packages/user`` (7) and ``apps/builder/src/features/user`` (13) —
carries the PackageAnchorExtractor workspace marker, so the W3.1 D1
carve-out swept it into the platform lane. Validator I9 does NOT exempt
it (its shape is product code, not a shell) → the one wave31 I9 breach.
The same class fired on tracecat (w31x): the ``frontend/`` workspace is
manifest-named ``tracecat`` (the product's own name, = the root
pyproject project), so the whole frontend shell dev surfaced under a
product alias no ruler can recognize as a shell.

Two fixes under test:
  (1) the carve-out now requires the SHELL SHAPE the ruler itself
      exempts (``_lane_shell_exempt``); ws-marker devs that fail it
      ride the guarded rescue ladder (span-vote → walk) — lane only if
      truly flowless;
  (2) the workspace-anchor slug demotes a manifest name that equals a
      ROOT project name (the product alias) to the workspace directory
      basename — tracecat's frontend dev is named ``frontend`` again,
      which both rulers and PMs read correctly as the app shell.

Fixtures are distilled from wave31-out/typebot.json and
wave31-extra/tracecat.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.extractors.package import (
    PackageAnchorExtractor,
    _root_project_slugs,
    _workspace_slug,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    _lane_shell_exempt,
    build_platform_infrastructure_lane,
    run_anchored_mint,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_WS_MARKER = (
    "[package] workspace anchor {name!r} from monorepo package "
    "{path!r} (package.json name={pkg!r})"
)


def flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id="old-pf",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ctx_of(workspaces=None, tracked=None, repo_path=".") -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path(repo_path), monorepo=bool(workspaces),
    )


# ── (1) carve-out narrowing — the typebot `user` class ───────────────────


def _user_class_fixture() -> tuple[list[Feature], list[dict]]:
    """typebot shape: a settings page capability (mints) + the flowful
    `user` dev whose ws package holds only a minority of its files and
    whose flow spans concentrate on the settings capability."""
    routes = [
        {"pattern": "/settings", "method": "PAGE",
         "file": "apps/builder/src/pages/settings.tsx"},
    ]
    settings = dev(
        "settings-page", ["apps/builder/src/pages/settings.tsx"],
        flows=[flow("edit-settings-flow",
                    "apps/builder/src/pages/settings.tsx")])
    # the real wave31 ratio: 7/20 files in the ws package — well under
    # the ruler's "at least half" bar (here 2/7).
    user_paths = (
        [f"packages/user/src/{n}.ts" for n in ("schemas", "rpc")]
        + [f"apps/builder/src/features/user/components/{n}.tsx"
           for n in ("MyAccountForm", "ChangeEmailDialog", "ApiTokensList",
                     "AppearanceRadioGroup", "UserPreferencesForm")]
    )
    user = dev(
        "user", user_paths,
        flows=[
            flow("manage-account-flow",
                 "apps/builder/src/features/user/components/MyAccountForm.tsx",
                 paths=[
                     "apps/builder/src/features/user/components/MyAccountForm.tsx",
                     "apps/builder/src/pages/settings.tsx",
                 ]),
        ],
        description=_WS_MARKER.format(
            name="user", path="packages/user", pkg="@typebot.io/user"),
    )
    return [settings, user], routes


def test_flowful_ws_marker_dev_without_shell_shape_is_rescued() -> None:
    """THE wave31 I9 regression: dev `user` must NOT lane — the marker
    alone is not a lane ticket; the span rung grounds it in the real
    capability its journeys touch."""
    devs, routes = _user_class_fixture()
    settings, user = devs
    pfs, tele = run_anchored_mint(devs, routes, ctx_of())
    assert user.product_feature_id is not None, (
        "W3.2 REGRESSION: flowful non-shell ws-marker dev swept into "
        "the lane (validator I9 breach — the typebot `user` class)")
    assert user.product_feature_id == settings.product_feature_id
    assert (user.anchor_id or "").startswith("fold:span")
    assert tele.get("law_ws_anchor_released", 0) == 1
    assert tele.get("law_ws_anchor_laned", 0) == 0
    assert tele.get("law_flowful_in_lane", 0) == 0
    assert build_platform_infrastructure_lane(devs) == []


def test_flowful_shell_shaped_ws_dev_still_lanes() -> None:
    """The tracecat frontend-shell class (renamed `frontend` by fix 2):
    a flowful ws-marker dev with a structural shell NAME and whole-app
    span dispersion keeps the honest lane — no capability bind."""
    routes = [
        {"pattern": "/workflows", "method": "PAGE",
         "file": "frontend/src/app/workflows/page.tsx"},
    ]
    wf = dev("workflows", ["frontend/src/app/workflows/page.tsx"],
             flows=[flow("edit-workflow-flow",
                         "frontend/src/app/workflows/page.tsx")])
    shell_files = [f"frontend/src/components/{d}/{n}.tsx"
                   for d in ("organization", "cases", "tables")
                   for n in ("a", "b", "c")]
    shell = dev(
        "frontend", shell_files,
        flows=[flow("browse-cases-flow",
                    "frontend/src/components/cases/a.tsx",
                    paths=shell_files)],
        description=_WS_MARKER.format(
            name="frontend", path="frontend", pkg="tracecat"),
    )
    pfs, tele = run_anchored_mint([wf, shell], routes, ctx_of())
    assert shell.product_feature_id is None, (
        "shell-shaped ws dev must lane honestly (D1 protection)")
    assert tele.get("law_ws_anchor_laned", 0) == 1
    assert tele.get("law_ws_anchor_released", 0) == 0


def test_lane_shell_exempt_shapes() -> None:
    """The exemption mirror: name hints / shared-package majority /
    own-app shell pass; split feature-code shapes fail."""
    # typebot `user`: 2/7 under packages/ → NOT exempt
    devs, _ = _user_class_fixture()
    assert not _lane_shell_exempt(devs[1])
    # supabase `studio`: all paths under apps/studio/ → exempt
    studio = dev("studio", [f"apps/studio/components/{n}.tsx"
                            for n in ("a", "b", "c")])
    assert _lane_shell_exempt(studio)
    # name hint ("frontend") → exempt regardless of layout
    fe = dev("frontend", ["frontend/src/components/x.tsx"])
    assert _lane_shell_exempt(fe)
    # shared-package majority → exempt
    pkg = dev("schemas", [f"packages/schemas/{n}.ts" for n in ("a", "b")])
    assert _lane_shell_exempt(pkg)
    # pypkg-root shape (product-named python package) → NOT exempt:
    # binding is the ladder's job, not the lane's
    py = dev("acme", [f"acme/{n}.py" for n in ("app", "api", "jobs")])
    assert not _lane_shell_exempt(py)


# ── (2) product-alias workspace slug ─────────────────────────────────────


def test_workspace_slug_demotes_product_alias_to_dir() -> None:
    ws = SimpleNamespace(
        path="frontend", package_json={"name": "tracecat"}, files=None)
    assert _workspace_slug(ws, frozenset({"tracecat"})) == "frontend"
    # no collision → manifest name wins as before
    assert _workspace_slug(ws, frozenset({"acme"})) == "tracecat"
    assert _workspace_slug(ws) == "tracecat"
    # scoped manifest names keep their tail (no collision with root)
    ws2 = SimpleNamespace(
        path="packages/user", package_json={"name": "@typebot.io/user"},
        files=None)
    assert _workspace_slug(ws2, frozenset({"typebot"})) == "user"
    # ROOT workspace (path '.') keeps the manifest name — there is no
    # meaningful dir basename to demote to
    root = SimpleNamespace(path=".", package_json={"name": "acme"}, files=None)
    assert _workspace_slug(root, frozenset({"acme"})) == "acme"


def test_root_project_slugs_reads_both_manifests(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "acme"}')
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "tracecat"\nversion = "0"\n')
    assert _root_project_slugs(tmp_path) == frozenset({"acme", "tracecat"})
    assert _root_project_slugs(tmp_path / "missing") == frozenset()


def test_package_extractor_emits_dir_named_anchor_for_alias(
    tmp_path: Path,
) -> None:
    """tracecat shape end-to-end through the extractor: the frontend
    workspace manifest carries the product's own name; the emitted
    workspace anchor must be dir-named (``frontend``)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "tracecat"\nversion = "0"\n')
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text('{"name": "tracecat"}')
    ws = SimpleNamespace(
        path="frontend", package_json={"name": "tracecat"},
        files=["frontend/src/app/page.tsx"])
    ctx = SimpleNamespace(
        repo_path=tmp_path, monorepo=True, workspaces=[ws])
    out = PackageAnchorExtractor().extract(ctx)
    ws_anchors = [a for a in out if "workspace anchor" in a.rationale]
    assert len(ws_anchors) == 1
    a = ws_anchors[0]
    assert a.name == "frontend", (
        "product-alias workspace kept the alias name — every ruler "
        "will misread the frontend shell as product code")
    assert "'frontend'" in a.rationale
    assert "name='tracecat'" in a.rationale.replace('"', "'")
