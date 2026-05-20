"""Tests for :mod:`faultline.framework_linkers.trpc_procedure` (Sprint D1, C7)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.framework_linkers.trpc_procedure import (
    TrpcProcedureLinker,
    _find_matching_brace,
    _split_top_level_object,
)
from faultline.models.types import Feature
from faultline.pipeline_v2.run_logger import StageLogger


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(
    repo: Path,
    *,
    stack: str = "next-app-router",
    audited: str | None = "next-app-router",
) -> SimpleNamespace:
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
    return StageLogger(tmp_path, 6, "trpc_procedure_test")


# ── helpers ────────────────────────────────────────────────────────────


def test_find_matching_brace_simple() -> None:
    s = "{ a: 1, b: 2 }"
    assert _find_matching_brace(s, 0) == len(s)


def test_find_matching_brace_skips_strings() -> None:
    s = '{ a: "{}", b: 1 }'
    assert _find_matching_brace(s, 0) == len(s)


def test_split_top_level_object_shorthand() -> None:
    items = _split_top_level_object(" userRouter, monitorRouter ")
    keys = [k for k, _ in items]
    assert "userRouter" in keys and "monitorRouter" in keys


# ── activation ─────────────────────────────────────────────────────────


def test_inactive_when_no_trpc_dep(tmp_path: Path) -> None:
    _w(tmp_path / "package.json", json.dumps({"dependencies": {"next": "15"}}))
    ctx = _ctx(tmp_path)
    assert TrpcProcedureLinker().is_active(ctx) is False


def test_active_via_workspace_dep(tmp_path: Path) -> None:
    pkg = {"dependencies": {"@trpc/server": "11.0.0"}}
    ws = SimpleNamespace(name="api", path="packages/api", package_json=pkg)
    ctx = _ctx(tmp_path)
    ctx.workspaces = [ws]
    assert TrpcProcedureLinker().is_active(ctx) is True


def test_active_via_tracked_package_json(tmp_path: Path) -> None:
    _w(tmp_path / "package.json", json.dumps({
        "dependencies": {"@trpc/client": "11.0.0"},
    }))
    ctx = _ctx(tmp_path)
    assert TrpcProcedureLinker().is_active(ctx) is True


# ── router map building ────────────────────────────────────────────────


def test_router_map_flat_router(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/server": "11"}}))
    _w(repo / "server/router.ts",
       'import { createTRPCRouter, publicProcedure } from "./trpc"\n'
       'export const appRouter = createTRPCRouter({\n'
       '  hello: publicProcedure.query(() => "world"),\n'
       '  goodbye: publicProcedure.mutation(() => "bye"),\n'
       '})\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    assert linker.is_active(ctx) is True
    proc = linker._ensure_router_map(ctx)
    assert "hello" in proc
    assert "goodbye" in proc
    assert proc["hello"].file == "server/router.ts"


def test_router_map_nested(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/server": "11"}}))
    _w(repo / "server/userRouter.ts",
       'import { createTRPCRouter, publicProcedure } from "./trpc"\n'
       'export const userRouter = createTRPCRouter({\n'
       '  create: publicProcedure.input(z.object({})).mutation(async ()=>{}),\n'
       '  getById: publicProcedure.query(() => null),\n'
       '})\n')
    _w(repo / "server/root.ts",
       'import { userRouter } from "./userRouter"\n'
       'import { createTRPCRouter } from "./trpc"\n'
       'export const appRouter = createTRPCRouter({\n'
       '  user: userRouter,\n'
       '})\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    assert linker.is_active(ctx) is True
    proc = linker._ensure_router_map(ctx)
    assert "user.create" in proc
    assert "user.getById" in proc
    assert proc["user.create"].file == "server/userRouter.ts"


def test_router_map_three_levels_deep(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/server": "11"}}))
    _w(repo / "server/list.ts",
       'export const listRouter = createTRPCRouter({\n'
       '  all: publicProcedure.query(()=>[]),\n'
       '})\n')
    _w(repo / "server/users.ts",
       'import { listRouter } from "./list"\n'
       'export const usersRouter = createTRPCRouter({\n'
       '  list: listRouter,\n'
       '})\n')
    _w(repo / "server/admin.ts",
       'import { usersRouter } from "./users"\n'
       'export const adminRouter = createTRPCRouter({\n'
       '  users: usersRouter,\n'
       '})\n')
    _w(repo / "server/root.ts",
       'import { adminRouter } from "./admin"\n'
       'export const appRouter = createTRPCRouter({\n'
       '  admin: adminRouter,\n'
       '})\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    proc = linker._ensure_router_map(ctx)
    assert "admin.users.list.all" in proc


# ── call-site detection ────────────────────────────────────────────────


def test_call_site_mutate(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/server": "11"}}))
    _w(repo / "server/router.ts",
       'export const appRouter = createTRPCRouter({\n'
       '  user: createTRPCRouter({\n'
       '    create: publicProcedure.mutation(async ()=>{}),\n'
       '  }),\n'
       '})\n')
    _w(repo / "src/Page.tsx",
       'import { trpc } from "@/trpc/client"\n'
       'export function P() {\n'
       '  const { mutate } = trpc.user.create.useMutation()\n'
       '  return null\n'
       '}\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    assert linker.is_active(ctx) is True
    feature = _new_feature("user", ["src/Page.tsx", "server/router.ts"])
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    assert any(l.target_symbol == "user.create" for l in links)
    assert linker.telemetry.procedure_call_sites_found >= 1


def test_call_site_useQuery_via_api_alias(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/client": "11"}}))
    _w(repo / "server/router.ts",
       'export const appRouter = createTRPCRouter({\n'
       '  user: createTRPCRouter({\n'
       '    getById: publicProcedure.query(()=>null),\n'
       '  }),\n'
       '})\n')
    _w(repo / "src/UserView.tsx",
       'import { api } from "@/trpc/client"\n'
       'export function UV() {\n'
       '  const x = api.user.getById.useQuery({id: 1})\n'
       '  return null\n'
       '}\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    feature = _new_feature("uv", ["src/UserView.tsx", "server/router.ts"])
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    assert any(l.target_symbol == "user.getById" for l in links)


def test_unknown_call_site_unmatched(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/server": "11"}}))
    _w(repo / "server/router.ts",
       'export const appRouter = createTRPCRouter({\n'
       '  user: createTRPCRouter({\n'
       '    create: publicProcedure.mutation(async ()=>{}),\n'
       '  }),\n'
       '})\n')
    _w(repo / "src/P.tsx",
       'import { trpc } from "@/trpc/client"\n'
       'export function P(){ return trpc.foo.bar.mutate({}) }\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    feature = _new_feature("p", ["src/P.tsx"])
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))
    # foo.bar isn't in the router map.
    assert all(l.target_symbol != "foo.bar" for l in links)
    assert linker.telemetry.unmatched_call_sites >= 1
    assert "foo.bar" in linker.telemetry.unmatched_paths_sample


def test_telemetry_router_map_size(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "package.json", json.dumps({"dependencies": {"@trpc/server": "11"}}))
    _w(repo / "server/router.ts",
       'export const appRouter = createTRPCRouter({\n'
       '  a: publicProcedure.query(()=>1),\n'
       '  b: publicProcedure.query(()=>2),\n'
       '  c: publicProcedure.mutation(()=>3),\n'
       '})\n')
    ctx = _ctx(repo)
    linker = TrpcProcedureLinker()
    linker._ensure_router_map(ctx)
    assert linker.telemetry.router_map_size >= 3
    assert linker.telemetry.router_files_parsed >= 1
