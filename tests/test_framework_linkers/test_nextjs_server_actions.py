"""Tests for :mod:`faultline.framework_linkers.nextjs_server_actions` (Sprint D1, C5)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.framework_linkers.nextjs_server_actions import (
    NextjsServerActionsLinker,
    _has_use_server_directive,
    _is_app_api_route,
)
from faultline.models.types import Feature
from faultline.pipeline_v2.run_logger import StageLogger


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path, *, stack: str = "next-app-router", audited: str | None = "next-app-router") -> SimpleNamespace:
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            try:
                tracked.append(f.relative_to(repo).as_posix())
            except ValueError:
                continue
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        run_dir=None,
        stack=stack,
        audited_stack=audited,
        secondary_stacks=(),
        monorepo=False,
        workspaces=[],
    )


def _new_feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name, paths=list(paths), authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0, layer="developer",
    )


def _log(tmp_path: Path) -> StageLogger:
    return StageLogger(tmp_path, 6, "nextjs_server_actions_test")


# ── _has_use_server_directive ───────────────────────────────────────────


def test_directive_top_of_file() -> None:
    text = '"use server"\n\nexport async function createX() {}\n'
    assert _has_use_server_directive(text) is True


def test_directive_single_quotes() -> None:
    text = "'use server';\nexport const f = async () => {};\n"
    assert _has_use_server_directive(text) is True


def test_directive_after_comments_ok() -> None:
    text = '// header\n/* block */\n"use server"\nexport function f(){}\n'
    assert _has_use_server_directive(text) is True


def test_no_directive_returns_false() -> None:
    text = 'import { foo } from "./bar"\nexport function g(){}\n'
    assert _has_use_server_directive(text) is False


def test_directive_after_real_code_does_not_count() -> None:
    text = 'import x from "y"\n"use server"\nexport function f(){}\n'
    # The directive isn't the first non-trivia line — code precedes it.
    assert _has_use_server_directive(text) is False


# ── activation ──────────────────────────────────────────────────────────


def test_is_active_true_for_next_app_router(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, stack="next-app-router", audited="next-app-router")
    assert NextjsServerActionsLinker().is_active(ctx) is True


def test_is_active_false_for_django(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, stack="django", audited="django")
    assert NextjsServerActionsLinker().is_active(ctx) is False


# ── route exclusion ─────────────────────────────────────────────────────


def test_app_api_route_excluded() -> None:
    assert _is_app_api_route("apps/web/app/api/rules/route.ts") is True


def test_non_route_not_excluded() -> None:
    assert _is_app_api_route("apps/web/utils/actions/user.ts") is False


def test_pages_api_not_excluded_by_this_filter() -> None:
    # We deliberately only filter `app/api/**/route.{ext}` (the C4 surface).
    # pages/api files lack "use server" anyway so they won't match.
    assert _is_app_api_route("apps/web/pages/api/health.ts") is False


# ── module-level action: discovery + link emission ──────────────────────


def test_module_level_action_emits_link(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "apps/web/utils/actions/user.ts",
       '"use server"\n\nexport async function createUser(input: any) {\n'
       '  return await db.user.create({ data: input })\n'
       '}\n\nexport async function deleteUser(id: string) {\n'
       '  return await db.user.delete({ where: { id } })\n}\n')
    _w(repo / "apps/web/app/(app)/UserForm.tsx",
       'import { createUser } from "../../utils/actions/user"\n\n'
       'export function UserForm() {\n'
       '  return <form onSubmit={() => createUser({})}>x</form>\n}\n')

    ctx = _ctx(repo)
    feature = _new_feature("user", [
        "apps/web/app/(app)/UserForm.tsx",
        "apps/web/utils/actions/user.ts",
    ])
    linker = NextjsServerActionsLinker()
    assert linker.is_active(ctx) is True
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    assert len(links) >= 1
    server_links = [l for l in links if l.link_kind == "server-action"]
    assert server_links, "expected at least one server-action link"
    sample = next(
        l for l in server_links
        if l.target_symbol == "createUser"
    )
    assert sample.target_file == "apps/web/utils/actions/user.ts"
    assert sample.source_file == "apps/web/app/(app)/UserForm.tsx"
    assert sample.confidence == 1.0
    assert linker.telemetry.server_action_files_detected >= 1


def test_module_without_directive_not_indexed(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "apps/web/utils/helpers/user.ts",
       'export function helper(x: number) { return x + 1 }\n')
    _w(repo / "apps/web/app/Page.tsx",
       'import { helper } from "../utils/helpers/user"\n'
       'export function Page(){ return helper(1) }\n')
    ctx = _ctx(repo)
    feature = _new_feature("page", [
        "apps/web/app/Page.tsx",
        "apps/web/utils/helpers/user.ts",
    ])
    linker = NextjsServerActionsLinker()
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    assert links == []
    assert linker.telemetry.server_action_files_detected == 0


# ── route handlers skipped (no double-emit with C4) ─────────────────────


def test_route_handler_with_use_server_skipped(tmp_path: Path) -> None:
    # An app/api route file CAN legally contain "use server" — but we
    # leave it to the C4 linker. C5 must skip it.
    repo = tmp_path
    _w(repo / "apps/web/app/api/foo/route.ts",
       '"use server"\nexport async function POST() { return Response.json({}) }\n')
    _w(repo / "apps/web/app/Caller.tsx",
       'import { POST } from "./api/foo/route"\n'
       'export function C(){ return POST() }\n')
    ctx = _ctx(repo)
    feature = _new_feature("foo", [
        "apps/web/app/api/foo/route.ts",
        "apps/web/app/Caller.tsx",
    ])
    linker = NextjsServerActionsLinker()
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    assert links == []
    assert linker.telemetry.server_action_files_detected == 0


# ── inline JSX action ───────────────────────────────────────────────────


def test_inline_jsx_action_detected(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "apps/web/app/InlineForm.tsx",
       'export function InlineForm() {\n'
       '  return (\n'
       '    <form action={async (formData) => {\n'
       '      "use server"\n'
       '      await db.update(formData.get("x"))\n'
       '    }}>\n'
       '      <button>Go</button>\n'
       '    </form>\n'
       '  )\n}\n')
    ctx = _ctx(repo)
    feature = _new_feature("inline", ["apps/web/app/InlineForm.tsx"])
    linker = NextjsServerActionsLinker()
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    inline_links = [l for l in links if l.target_symbol == "<inline-action>"]
    assert len(inline_links) >= 1
    assert linker.telemetry.inline_action_sites >= 1


# ── path normalisation across .. and aliased relative ──────────────────


def test_relative_module_resolution_uses_extensions(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "src/actions.ts",
       '"use server"\nexport async function ping(){}\n')
    _w(repo / "src/page.tsx",
       'import { ping } from "./actions"\n'
       'export function P(){ return <button onClick={() => ping()}/> }\n')
    ctx = _ctx(repo)
    feature = _new_feature("p", ["src/page.tsx", "src/actions.ts"])
    linker = NextjsServerActionsLinker()
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    matched = [l for l in links if l.target_symbol == "ping"]
    assert matched, "extension resolution should find ./actions → ./actions.ts"
