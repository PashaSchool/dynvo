"""Tests for the declared-workspace UNION gate (onyx shape, 2026-07-19).

The onyx repo declares peripheral packages in its root ``package.json``
(``widget/``, ``desktop/`` — ~1.6% of tracked files) while the product
bulk (``web/`` Next app, ``cli/`` Python) is *undeclared*. Pre-gate,
``run_stage_1_per_workspace`` short-circuited on the declared list and the
bulk dissolved into the js-generic ``__leftover__`` pass.

The UNION gate (env-flagged ``FAULTLINE_WORKSPACE_UNION``, default OFF)
detects that the declared workspaces span a strict MINORITY of tracked
files (scale-invariant ``covered * 2 < total`` — no magic number) and
unions them with the non-overlapping results of ``synthesise_workspaces``
(the infisical synthesis mechanism — no new extractors).

Anti-case: high-coverage declared monorepos (langfuse 93.9%, supabase
85.3%, typebot 99.0% declared coverage) must stay INERT even when the
flag is armed — the ratio gate returns False, so the union never fires
and the scan output is byte-identical to base.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    Workspace,
    stage_0_intake,
)
from faultline.pipeline_v2.stage_1_per_workspace import (
    _declared_covers_minority,
    _union_synthesised,
    run_stage_1_per_workspace,
    workspace_union_enabled,
)

WS_UNION_ENV = "FAULTLINE_WORKSPACE_UNION"


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_onyx_shape(root: Path) -> None:
    """Root declares peripheral widget/ + desktop/ (a minority); the
    product bulk lives in undeclared web/ (Next) + cli/ (Python)."""
    (root / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["widget", "desktop"]}),
    )
    widget = root / "widget"
    widget.mkdir()
    (widget / "package.json").write_text(
        json.dumps({"name": "widget", "dependencies": {"vite": "^5"}}),
    )
    (widget / "index.ts").write_text("export const x = 1")

    desktop = root / "desktop"
    desktop.mkdir()
    (desktop / "package.json").write_text(
        json.dumps({"name": "desktop", "dependencies": {"electron": "^30"}}),
    )
    (desktop / "main.js").write_text("const x = 1")

    web = root / "web"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"next": "^14"}}),
    )
    app = web / "app"
    app.mkdir()
    for i in range(12):
        page_dir = app / f"p{i}"
        page_dir.mkdir()
        (page_dir / "page.tsx").write_text("export default function P(){}")

    cli = root / "cli"
    cli.mkdir()
    (cli / "pyproject.toml").write_text("[project]\nname='cli'\n")
    src = cli / "src"
    src.mkdir()
    for i in range(6):
        (src / f"m{i}.py").write_text("def f():\n    return 1\n")


def _make_high_coverage_monorepo(root: Path) -> None:
    """The langfuse/supabase/typebot shape — declared workspaces span the
    MAJORITY of tracked files. The union gate must stay inert here."""
    (root / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": ["apps/web", "apps/api"]}),
    )
    apps = root / "apps"
    apps.mkdir()
    web = apps / "web"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"next": "^14"}}),
    )
    web_app = web / "app"
    web_app.mkdir()
    for i in range(20):
        page_dir = web_app / f"p{i}"
        page_dir.mkdir()
        (page_dir / "page.tsx").write_text("export default function P(){}")
    api = apps / "api"
    api.mkdir()
    (api / "package.json").write_text(
        json.dumps({"name": "api", "dependencies": {"fastify": "^4"}}),
    )
    api_src = api / "src"
    api_src.mkdir()
    for i in range(20):
        (api_src / f"route{i}.ts").write_text("export const r = 1")
    # A tiny undeclared conventional dir WITH a manifest — the only thing
    # synthesise_workspaces could grab. It must NOT be unioned because the
    # declared workspaces already cover the majority.
    docs = root / "docs"
    docs.mkdir()
    (docs / "package.json").write_text(json.dumps({"name": "docs"}))
    (docs / "index.md").write_text("# docs")


def _run(ctx: ScanContext) -> object:
    return run_stage_1_per_workspace(ctx)


# ── env flag / kill-switch ──────────────────────────────────────────────


def test_flag_defaults_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WS_UNION_ENV, raising=False)
    assert workspace_union_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_truthy_values_arm(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv(WS_UNION_ENV, value)
    assert workspace_union_enabled() is True


@pytest.mark.parametrize("value", ["0", "", "false", "no", "off", "nope"])
def test_flag_falsy_values_disarm(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv(WS_UNION_ENV, value)
    assert workspace_union_enabled() is False


# ── ratio gate (scale-invariant, no magic number) ───────────────────────


def test_declared_covers_minority_true_for_onyx_shape() -> None:
    declared = [Workspace(name="widget", path="widget", stack="vite")]
    tracked = ["widget/a.ts"] + [f"web/app/p{i}/page.tsx" for i in range(10)]
    # covered = 1, total = 11 → 2 < 11 → minority
    assert _declared_covers_minority(declared, tracked) is True


def test_declared_covers_minority_false_for_high_coverage() -> None:
    declared = [Workspace(name="web", path="apps/web", stack="next-app-router")]
    tracked = [f"apps/web/p{i}.tsx" for i in range(9)] + ["docs/x.md"]
    # covered = 9, total = 10 → 18 < 10 is False → majority
    assert _declared_covers_minority(declared, tracked) is False


def test_declared_covers_minority_exact_half_is_not_minority() -> None:
    # covered * 2 == total → NOT a strict minority (gate must be <, not <=).
    declared = [Workspace(name="a", path="a", stack="x")]
    tracked = ["a/1", "a/2", "b/1", "b/2"]
    assert _declared_covers_minority(declared, tracked) is False


def test_declared_covers_minority_empty_inputs_are_safe() -> None:
    assert _declared_covers_minority([], ["a", "b"]) is False
    decl = [Workspace(name="a", path="a", stack="x")]
    assert _declared_covers_minority(decl, []) is False


# ── union additions stay path-disjoint ──────────────────────────────────


def test_union_additions_drop_paths_nested_in_declared(tmp_path: Path) -> None:
    """A synthesised workspace that overlaps a declared one is dropped so
    the merged list stays path-disjoint."""
    # Real dirs on disk so synthesise_workspaces can walk them.
    (tmp_path / "package.json").write_text(json.dumps({"name": "root"}))
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"next": "^14"}}),
    )
    (web / "app").mkdir()
    (web / "app" / "page.tsx").write_text("x")
    ctx = stage_0_intake(tmp_path, skip_git=True)
    # Declare `web` explicitly — synthesise would also find it; the union
    # must NOT re-add the same path.
    declared = [Workspace(name="web", path="web", stack="next-app-router")]
    additions = _union_synthesised(declared, ctx)
    assert all(a.path != "web" for a in additions)


# ── onyx-shape end-to-end (armed vs base) ───────────────────────────────


def test_onyx_shape_armed_unions_undeclared_bulk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_onyx_shape(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    declared_names = {w.name for w in (ctx.workspaces or [])}
    assert declared_names == {"widget", "desktop"}

    monkeypatch.setenv(WS_UNION_ENV, "1")
    res = run_stage_1_per_workspace(ctx)
    used = {w.name for w in res.workspaces_used}
    # The undeclared product bulk is now scoped to its own stack.
    assert "web" in used
    assert "cli" in used
    assert {"widget", "desktop"} <= used
    assert res.synthesised_workspaces is True
    # web/ is scoped as its own workspace → not a next-app-router stub.
    web_ws = next(w for w in res.workspaces_used if w.name == "web")
    assert web_ws.stack == "next-app-router"


def test_onyx_shape_armed_drives_web_residual_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_onyx_shape(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)

    monkeypatch.delenv(WS_UNION_ENV, raising=False)
    base = run_stage_1_per_workspace(ctx)
    monkeypatch.setenv(WS_UNION_ENV, "1")
    armed = run_stage_1_per_workspace(ctx)

    # web/ files fall to the leftover pass in base; the union pulls them
    # into a scoped workspace → the leftover pool strictly shrinks and no
    # web/ file remains uncovered.
    assert armed.leftover_files_scanned < base.leftover_files_scanned
    ws_prefixes = tuple(
        w.path.rstrip("/") + "/" for w in armed.workspaces_used if w.path
    )
    web_residual = [
        f for f in ctx.tracked_files
        if (f == "web" or f.startswith("web/"))
        and not any(f == p[:-1] or f.startswith(p) for p in ws_prefixes)
    ]
    assert web_residual == []


def test_onyx_shape_flag_off_leaves_bulk_undeclared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF ⇒ pre-gate behaviour: only the declared workspaces are
    scoped, web/ + cli/ stay in the leftover pass (byte-identical base)."""
    _make_onyx_shape(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    monkeypatch.delenv(WS_UNION_ENV, raising=False)
    res = run_stage_1_per_workspace(ctx)
    used = {w.name for w in res.workspaces_used}
    assert used == {"widget", "desktop"}
    assert res.synthesised_workspaces is False
    assert "web" not in used
    assert "cli" not in used


# ── anti-case: high-coverage monorepo is INERT even when armed ──────────


def test_high_coverage_monorepo_inert_flag_off(tmp_path: Path) -> None:
    _make_high_coverage_monorepo(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)
    # Sanity: declared workspaces span the majority → not a minority.
    assert _declared_covers_minority(list(ctx.workspaces or []), ctx.tracked_files) is False


def test_high_coverage_monorepo_byte_identical_off_vs_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """langfuse/supabase/typebot stand-in: the ratio gate returns False,
    so arming the flag changes NOTHING — the workspace set and leftover
    count are identical OFF vs ON."""
    _make_high_coverage_monorepo(tmp_path)
    ctx = stage_0_intake(tmp_path, skip_git=True)

    monkeypatch.delenv(WS_UNION_ENV, raising=False)
    off = run_stage_1_per_workspace(ctx)
    monkeypatch.setenv(WS_UNION_ENV, "1")
    on = run_stage_1_per_workspace(ctx)

    assert [w.name for w in off.workspaces_used] == [
        w.name for w in on.workspaces_used
    ]
    assert off.synthesised_workspaces == on.synthesised_workspaces is False
    assert off.leftover_files_scanned == on.leftover_files_scanned
    # The undeclared docs/ package is NOT unioned in either mode.
    assert "docs" not in {w.name for w in on.workspaces_used}
