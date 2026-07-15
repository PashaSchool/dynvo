"""B66 — code-first server API-entry extractor unit pack.

Covers the mechanism (4 segments) + the SACRED anti-cases (spec §"SACRED
анти-кейси"):
  * flag default OFF (kill-switch) + byte-identical inert when unset, AND
    unregistered at the registry surface (extractor_hits key parity);
  * Seg A NestJS @Controller + HTTP verb decorators -> one entry, HTTP routes;
  * Seg B GraphQL code-first (decorator / pothos / nexus) -> QUERY/MUTATION;
  * Seg C tRPC procedures -> namespaced routes, verb by .query/.mutation;
  * Seg D koa-router (incl. rpc-name routes) + hono -> HTTP routes;
  * test / storybook / example files -> NOT entries (test-strip law);
  * import corroboration: koa/hono/trpc/graphql calls without the framework
    import -> honest skip (no false positives);
  * existing route sources untouched — the extractor only ADDS ``.routes``;
  * idempotency: a route already in routes_index by another source is not
    duplicated (build_routes_index (pattern, method, file) dedup);
  * ``.routes`` flow into ``build_routes_index``; determinism; per-workspace
    merge preserves the routes union when the flag is armed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.server_api_entries import (
    SERVER_API_ENTRIES_ENV,
    SERVER_API_ENTRY_SOURCE,
    ServerApiEntryExtractor,
    server_api_entries_enabled,
)
from faultline.pipeline_v2.indexes import build_routes_index
from faultline.pipeline_v2.stage_0_intake import ScanContext


def _ctx(repo: Path, files: list[str], **kw) -> ScanContext:
    return ScanContext(
        repo_path=repo,
        stack=kw.get("stack", "node"),
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        secondary_stacks=kw.get("secondary_stacks", ()),
        audited_stack=kw.get("audited_stack"),
    )


@pytest.fixture
def api_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")


def _write(tmp_path: Path, rel: str, body: str) -> str:
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return rel


def _extract(tmp_path: Path, rel: str, body: str, **kw):
    _write(tmp_path, rel, body)
    return ServerApiEntryExtractor().extract(_ctx(tmp_path, [rel], **kw))


# ── flag / kill-switch ───────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SERVER_API_ENTRIES_ENV, raising=False)
    assert server_api_entries_enabled() is False
    for falsy in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(SERVER_API_ENTRIES_ENV, falsy)
        assert server_api_entries_enabled() is False, falsy
    for truthy in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv(SERVER_API_ENTRIES_ENV, truthy)
        assert server_api_entries_enabled() is True, truthy


def test_off_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset flag -> zero candidates even with a real controller present."""
    monkeypatch.delenv(SERVER_API_ENTRIES_ENV, raising=False)
    rel = _write(
        tmp_path,
        "src/users/users.controller.ts",
        'import { Controller, Get } from "@nestjs/common";\n'
        '@Controller("users")\nexport class UsersController { @Get() all() {} }\n',
    )
    assert ServerApiEntryExtractor().extract(_ctx(tmp_path, [rel])) == []


def test_off_not_registered_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF byte-identity at the REGISTRY surface: scan_meta.extractor_hits
    serializes every registered source key, so with the flag unset the
    extractor must not even REGISTER (B67 kill-switch lesson). With the flag
    set it must appear, and nothing else changes."""
    from faultline.pipeline_v2.stage_1_extractors import (
        _load_default_extractors,
    )

    monkeypatch.delenv(SERVER_API_ENTRIES_ENV, raising=False)
    names_off = {e.name for e in _load_default_extractors()}
    assert SERVER_API_ENTRY_SOURCE not in names_off

    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    names_on = {e.name for e in _load_default_extractors()}
    assert SERVER_API_ENTRY_SOURCE in names_on
    assert names_on - {SERVER_API_ENTRY_SOURCE} == names_off


# ── Seg A — NestJS REST ──────────────────────────────────────────────────────


def test_nestjs_controller_prefix_and_verbs(tmp_path: Path, api_on) -> None:
    anchors = _extract(
        tmp_path,
        "src/users/users.controller.ts",
        'import { Controller, Get, Post } from "@nestjs/common";\n'
        '@Controller("users")\n'
        "export class UsersController {\n"
        "  @Get() findAll() {}\n"
        '  @Get(":id") findOne() {}\n'
        "  @Post() create() {}\n"
        "}\n",
    )
    (a,) = anchors
    assert a.name == "users"
    assert a.source == SERVER_API_ENTRY_SOURCE
    assert a.paths == ("src/users/users.controller.ts",)
    assert set(a.routes) == {
        ("/users", "GET", "src/users/users.controller.ts"),
        ("/users/:id", "GET", "src/users/users.controller.ts"),
        ("/users", "POST", "src/users/users.controller.ts"),
    }


def test_nestjs_controller_obj_path_and_nested_prefix(tmp_path: Path, api_on) -> None:
    """@Controller({ path: 'v2/bookings' }) with a @Patch(':id/cancel')."""
    (a,) = _extract(
        tmp_path,
        "apps/api/v2/bookings/bookings.controller.ts",
        'import { Controller, Patch } from "@nestjs/common";\n'
        "@Controller({ path: 'v2/bookings', version: '2' })\n"
        "export class BookingsController {\n"
        "  @Patch(':id/cancel') cancel() {}\n"
        "}\n",
    )
    assert a.name == "v2"  # first meaningful prefix segment
    assert ("/v2/bookings/:id/cancel", "PATCH",
            "apps/api/v2/bookings/bookings.controller.ts") in a.routes


def test_nestjs_bare_controller_uses_class_name(tmp_path: Path, api_on) -> None:
    """@Controller() (no prefix) -> slug from the class name (Controller peeled)."""
    (a,) = _extract(
        tmp_path,
        "src/health/health.controller.ts",
        'import { Controller, Get } from "@nestjs/common";\n'
        "@Controller()\nexport class HealthController { @Get() check() {} }\n",
    )
    assert a.name == "health"
    assert a.routes == (("/health", "GET", "src/health/health.controller.ts"),)


def test_non_controller_file_is_not_an_entry(tmp_path: Path, api_on) -> None:
    """ANTI-CASE: a plain service/util with a ``.get(...)`` and no @Controller /
    no framework import -> no entry (existing route sources untouched)."""
    assert _extract(
        tmp_path,
        "src/util/cache.ts",
        "export class Cache { get(key: string) { return this.map.get(key); } }\n",
    ) == []


# ── Seg B — GraphQL code-first ───────────────────────────────────────────────


def test_graphql_decorator_resolver(tmp_path: Path, api_on) -> None:
    """type-graphql @Resolver + @Query/@Mutation on separate lines."""
    (a,) = _extract(
        tmp_path,
        "src/user/user.resolver.ts",
        'import { Resolver, Query, Mutation, Arg } from "type-graphql";\n'
        "@Resolver(() => User)\n"
        "export class UserResolver {\n"
        "  @Query(() => [User])\n"
        "  async users() {}\n"
        "  @Mutation(() => User)\n"
        "  async createUser(@Arg('input') input: NewUser) {}\n"
        "}\n",
    )
    assert a.name == "user"
    assert set(a.routes) == {
        ("users", "QUERY", "src/user/user.resolver.ts"),
        ("createUser", "MUTATION", "src/user/user.resolver.ts"),
    }


def test_graphql_decorator_stacked_and_name_option(tmp_path: Path, api_on) -> None:
    """@Query({ name: 'allTeams' }) explicit name wins; a stacked @UseGuards
    before the method never becomes the operation."""
    (a,) = _extract(
        tmp_path,
        "src/team/team.resolver.ts",
        'import { Resolver, Query } from "@nestjs/graphql";\n'
        "@Resolver(() => Team)\n"
        "export class TeamResolver {\n"
        "  @Query(() => [Team], { name: 'allTeams' })\n"
        "  @UseGuards(JwtGuard)\n"
        "  teams() {}\n"
        "}\n",
    )
    assert a.routes == (("allTeams", "QUERY", "src/team/team.resolver.ts"),)


def test_graphql_pothos_builder_fields(tmp_path: Path, api_on) -> None:
    (a,) = _extract(
        tmp_path,
        "src/schema/organization.ts",
        'import { builder } from "@pothos/core";\n'
        'builder.queryField("organizations", (t) => t.field({}));\n'
        'builder.mutationField("createOrganization", (t) => t.field({}));\n',
    )
    assert a.name == "organization"
    assert set(a.routes) == {
        ("organizations", "QUERY", "src/schema/organization.ts"),
        ("createOrganization", "MUTATION", "src/schema/organization.ts"),
    }


def test_graphql_nexus_query_field_and_extend_type(tmp_path: Path, api_on) -> None:
    (a,) = _extract(
        tmp_path,
        "src/graphql/post.ts",
        'import { queryField, extendType } from "nexus";\n'
        'export const posts = queryField("posts", {});\n'
        'extendType({\n'
        '  type: "Mutation",\n'
        '  definition(t) {\n'
        '    t.field("createPost", {});\n'
        '    t.nonNull.field("deletePost", {});\n'
        "  },\n"
        "});\n",
    )
    assert a.name == "post"
    assert set(a.routes) == {
        ("posts", "QUERY", "src/graphql/post.ts"),
        ("createPost", "MUTATION", "src/graphql/post.ts"),
        ("deletePost", "MUTATION", "src/graphql/post.ts"),
    }


def test_graphql_without_import_is_skipped(tmp_path: Path, api_on) -> None:
    """ANTI-CASE: a class with @Query-looking text but no graphql import -> no
    entry (decorator names are generic; import corroboration required)."""
    assert _extract(
        tmp_path,
        "src/x.ts",
        "// @Query is just a comment here\nexport const q = 1;\n",
    ) == []


# ── Seg C — tRPC ─────────────────────────────────────────────────────────────


def test_trpc_router_procedures(tmp_path: Path, api_on) -> None:
    (a,) = _extract(
        tmp_path,
        "server/api/routers/organization.ts",
        'import { publicProcedure, protectedProcedure, router } from "@trpc/server";\n'
        "export const organizationRouter = router({\n"
        "  list: publicProcedure.query(() => {}),\n"
        "  create: protectedProcedure.input(schema).mutation(() => {}),\n"
        "  onEvent: publicProcedure.subscription(() => {}),\n"
        "});\n",
    )
    assert a.name == "organization"
    assert set(a.routes) == {
        ("organization.list", "QUERY", "server/api/routers/organization.ts"),
        ("organization.create", "MUTATION", "server/api/routers/organization.ts"),
        ("organization.onEvent", "SUBSCRIPTION", "server/api/routers/organization.ts"),
    }


def test_trpc_appRouter_composition_not_procedures(tmp_path: Path, api_on) -> None:
    """ANTI-CASE: appRouter composing sub-routers (``user: userRouter``) has NO
    ``Procedure`` values, so its namespace keys never become routes."""
    assert _extract(
        tmp_path,
        "server/api/root.ts",
        'import { createTRPCRouter } from "@trpc/server";\n'
        "export const appRouter = createTRPCRouter({\n"
        "  user: userRouter,\n"
        "  post: postRouter,\n"
        "});\n",
    ) == []


def test_trpc_without_import_is_skipped(tmp_path: Path, api_on) -> None:
    """ANTI-CASE: a ``router({...})`` with no @trpc import -> honest skip."""
    assert _extract(
        tmp_path,
        "src/y.ts",
        "export const r = router({ list: somethingProcedure.query(() => {}) });\n",
    ) == []


# ── Seg D — koa + hono ───────────────────────────────────────────────────────


def test_koa_router_rpc_name_routes(tmp_path: Path, api_on) -> None:
    """outline: ``router.post("documents.list", ...)`` — rpc-name path kept as-is,
    slug from the file stem."""
    (a,) = _extract(
        tmp_path,
        "server/routes/api/documents.ts",
        'import Router from "@koa/router";\n'
        "const router = new Router();\n"
        'router.post("documents.list", auth(), async (ctx) => {});\n'
        'router.post("documents.info", async (ctx) => {});\n'
        "export default router;\n",
    )
    assert a.name == "documents"
    assert set(a.routes) == {
        ("documents.list", "POST", "server/routes/api/documents.ts"),
        ("documents.info", "POST", "server/routes/api/documents.ts"),
    }


def test_koa_url_route_with_prefix(tmp_path: Path, api_on) -> None:
    (a,) = _extract(
        tmp_path,
        "server/routes/webhooks.ts",
        'import Router from "koa-router";\n'
        'const api = new Router({ prefix: "/api" });\n'
        'api.get("/webhooks/:id", async (ctx) => {});\n',
    )
    assert ("/api/webhooks/:id", "GET", "server/routes/webhooks.ts") in a.routes


def test_koa_without_import_is_skipped(tmp_path: Path, api_on) -> None:
    """ANTI-CASE: ``router.get(...)`` with no koa import -> skip (Express and
    others own their own extractors)."""
    assert _extract(
        tmp_path,
        "src/z.ts",
        'const router = makeRouter();\nrouter.get("/x", h);\n',
    ) == []


def test_hono_app_routes(tmp_path: Path, api_on) -> None:
    (a,) = _extract(
        tmp_path,
        "apps/api/src/rest/transactions.ts",
        'import { Hono } from "hono";\n'
        "const app = new Hono();\n"
        'app.get("/", (c) => {});\n'
        'app.post("/:id", (c) => {});\n'
        "export default app;\n",
    )
    assert a.name == "transactions"
    assert set(a.routes) == {
        ("/", "GET", "apps/api/src/rest/transactions.ts"),
        ("/:id", "POST", "apps/api/src/rest/transactions.ts"),
    }


def test_hono_without_import_is_skipped(tmp_path: Path, api_on) -> None:
    """ANTI-CASE: ``app.get(...)`` with no hono import -> skip."""
    assert _extract(
        tmp_path,
        "src/w.ts",
        'const app = express();\napp.get("/x", h);\n',
    ) == []


# ── SACRED: test / storybook / example files are not entries ──────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "src/users/__tests__/users.controller.ts",
        "src/users/users.controller.spec.ts",
        "src/users/users.controller.test.ts",
        "src/user/user.resolver.e2e.ts",
        "packages/x/__stories__/probe.controller.ts",
        "packages/x/examples/demo.controller.ts",
        "packages/x/playground/scratch.resolver.ts",
    ],
)
def test_test_and_artifact_files_are_not_entries(
    tmp_path: Path, api_on, rel: str
) -> None:
    assert _extract(
        tmp_path,
        rel,
        'import { Controller, Get } from "@nestjs/common";\n'
        '@Controller("x")\nexport class XController { @Get() a() {} }\n',
    ) == []


# ── routes_index integration + idempotency + determinism ─────────────────────


def test_routes_flow_into_routes_index(tmp_path: Path, api_on) -> None:
    rel = _write(
        tmp_path,
        "src/users/users.controller.ts",
        'import { Controller, Post } from "@nestjs/common";\n'
        '@Controller("users")\nexport class UsersController { @Post() create() {} }\n',
    )
    signals = {SERVER_API_ENTRY_SOURCE: ServerApiEntryExtractor().extract(
        _ctx(tmp_path, [rel])
    )}
    routes_index = build_routes_index([], signals)
    rows = [r for r in routes_index if r.get("file") == rel]
    assert len(rows) == 1
    assert rows[0]["pattern"] == "/users"
    assert rows[0]["method"] == "POST"


def test_routes_index_dedup_across_sources(tmp_path: Path, api_on) -> None:
    """IDEMPOTENCY anti-case: a (pattern, method, file) already emitted by
    another source is not duplicated when our extractor emits the same."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    rel = _write(
        tmp_path,
        "src/users/users.controller.ts",
        'import { Controller, Get } from "@nestjs/common";\n'
        '@Controller("users")\nexport class UsersController { @Get() all() {} }\n',
    )
    mine = ServerApiEntryExtractor().extract(_ctx(tmp_path, [rel]))
    other = [AnchorCandidate(
        name="users", paths=(rel,), source="route", confidence_self=0.9,
        routes=(("/users", "GET", rel),),
    )]
    routes_index = build_routes_index(
        [], {SERVER_API_ENTRY_SOURCE: mine, "route": other}
    )
    users_get = [r for r in routes_index
                 if r["pattern"] == "/users" and r["method"] == "GET"]
    assert len(users_get) == 1


def test_deterministic_sorted_emission(tmp_path: Path, api_on) -> None:
    files = []
    for stem in ("zeta", "alpha", "mid"):
        files.append(_write(
            tmp_path,
            f"src/{stem}/{stem}.controller.ts",
            'import { Controller, Get } from "@nestjs/common";\n'
            f'@Controller("{stem}")\nexport class {stem.title()}Controller '
            "{ @Get() a() {} }\n",
        ))
    ctx = _ctx(tmp_path, files)
    out1 = [a.name for a in ServerApiEntryExtractor().extract(ctx)]
    out2 = [a.name for a in ServerApiEntryExtractor().extract(ctx)]
    assert out1 == out2 == sorted(out1)


# ── per-workspace merge: monorepo twin-slug routes preservation ──────────────


def _twin_candidates():
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    a = AnchorCandidate(
        name="user",
        paths=("packages/twenty-server/src/user/user.resolver.ts",),
        source=SERVER_API_ENTRY_SOURCE,
        confidence_self=0.85,
        routes=(("users", "QUERY",
                 "packages/twenty-server/src/user/user.resolver.ts"),),
    )
    b = AnchorCandidate(
        name="user",
        paths=("apps/api/src/user/user.controller.ts",),
        source=SERVER_API_ENTRY_SOURCE,
        confidence_self=0.85,
        routes=(("/user", "GET",
                 "apps/api/src/user/user.controller.ts"),),
    )
    return a, b


def test_ws_merge_preserves_twin_routes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-slug entries across workspaces coalesce; ON -> the coalesced
    candidate carries the routes union (monorepo NestJS/tRPC survival)."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    a, b = _twin_candidates()
    merged = _merge_anchors_across_workspaces(
        [("srv", {SERVER_API_ENTRY_SOURCE: [a, b]})]
    )
    (cand,) = merged[SERVER_API_ENTRY_SOURCE]
    assert set(cand.routes) == set(a.routes) | set(b.routes)


def test_ws_merge_legacy_drop_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both flags OFF -> byte-identity: the coalesce keeps the LEGACY behavior
    (routes dropped), so OFF-world boards are unchanged."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.delenv(SERVER_API_ENTRIES_ENV, raising=False)
    monkeypatch.delenv("FAULTLINE_JOBS_ENTRIES", raising=False)
    a, b = _twin_candidates()
    merged = _merge_anchors_across_workspaces(
        [("srv", {SERVER_API_ENTRY_SOURCE: [a, b]})]
    )
    (cand,) = merged[SERVER_API_ENTRY_SOURCE]
    assert cand.routes == ()


# ── ORIGIN-GATE: an armed flag preserves ONLY its own source's routes ────────
#
# Regression (VERIFIED, control boards onyx-off.json vs onyx.json): the merge
# armed preservation as a blanket ``jobs OR server_api`` boolean, so on python
# repos the ``route`` extractor's internal FastAPI candidates — which OFF-world
# DROP at coalesce — survived whenever B66 (or B67) was on. onyx unique-routes
# 56 -> 208 (+487 raw, all backend/**/*.py), re-partitioning UF/PF on a stack
# entirely outside this flag's scope. Fix: preservation is keyed to the
# candidate's BIRTH source (``cand.source`` == the extractor's registration
# name); each flag arms ONLY its own source key.


def _py_route_twins():
    """onyx-class: two same-slug FastAPI ``route`` candidates (1 path each ->
    they coalesce) that DID NOT come from an armed source."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    a = AnchorCandidate(
        name="users",
        paths=("backend/onyx/server/users/api.py",),
        source="route",
        confidence_self=0.9,
        routes=(("/users", "GET", "backend/onyx/server/users/api.py"),),
    )
    b = AnchorCandidate(
        name="users",
        paths=("backend/onyx/server/manage/users.py",),
        source="route",
        confidence_self=0.9,
        routes=(("/manage/users", "GET",
                 "backend/onyx/server/manage/users.py"),),
    )
    return a, b


def test_ws_merge_unarmed_route_source_dropped_even_when_b66_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORE anti-case (onyx): python FastAPI ``route`` candidates that coalesce
    must NOT keep their routes just because B66 is armed — B66 arms only
    ``server-api-entry``. routes_index ON == OFF byte-for-byte for the unarmed
    source (the candidate still coalesces on paths; only its routes drop)."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    a, b = _py_route_twins()
    # Extension is a TEST assert only (per fix mandate: the PRODUCTION
    # mechanism keys on cand.source, never on the file extension).
    assert all(p.endswith(".py") for c in (a, b) for p in c.paths)

    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    monkeypatch.delenv("FAULTLINE_JOBS_ENTRIES", raising=False)
    on = _merge_anchors_across_workspaces([("srv", {"route": [a, b]})])
    (cand_on,) = on["route"]
    assert cand_on.routes == ()
    assert set(cand_on.paths) == set(a.paths) | set(b.paths)

    monkeypatch.delenv(SERVER_API_ENTRIES_ENV, raising=False)
    off = _merge_anchors_across_workspaces([("srv", {"route": [a, b]})])
    (cand_off,) = off["route"]
    # ON == OFF for the unarmed source (byte-for-byte on this group).
    assert cand_on.routes == cand_off.routes == ()
    assert cand_on.paths == cand_off.paths


def test_ws_merge_armed_preserves_only_new_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TS-monorepo contract: with B66 armed the union preserves EXACTLY the new
    source's emission — a co-present unarmed ``route`` group in the SAME merge
    still drops its routes (no blanket preservation leaks across sources)."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    api_a, api_b = _twin_candidates()        # source == server-api-entry (armed)
    route_a, route_b = _py_route_twins()     # source == route (unarmed)

    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    monkeypatch.delenv("FAULTLINE_JOBS_ENTRIES", raising=False)
    merged = _merge_anchors_across_workspaces(
        [(
            "srv",
            {
                SERVER_API_ENTRY_SOURCE: [api_a, api_b],
                "route": [route_a, route_b],
            },
        )]
    )
    (api_cand,) = merged[SERVER_API_ENTRY_SOURCE]
    (route_cand,) = merged["route"]
    assert set(api_cand.routes) == set(api_a.routes) | set(api_b.routes)
    assert route_cand.routes == ()


def test_ws_merge_jobs_armed_gates_by_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BOTH-FLAGS coverage: the same merge path is armed by B67
    FAULTLINE_JOBS_ENTRIES. Under jobs-armed (B66 off) the ``jobs-entry`` twins
    keep their routes, but a co-present unarmed ``route`` group still drops
    them — origin-gating holds for the jobs flag too."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    ja = AnchorCandidate(
        name="sync", paths=("pkg/a/jobs/sync.cron.job.ts",),
        source="jobs-entry", confidence_self=0.85,
        routes=(("/sync", "CRON", "pkg/a/jobs/sync.cron.job.ts"),),
    )
    jb = AnchorCandidate(
        name="sync", paths=("pkg/b/jobs/sync.job.ts",),
        source="jobs-entry", confidence_self=0.85,
        routes=(("/sync", "JOB", "pkg/b/jobs/sync.job.ts"),),
    )
    route_a, route_b = _py_route_twins()

    monkeypatch.setenv("FAULTLINE_JOBS_ENTRIES", "1")
    monkeypatch.delenv(SERVER_API_ENTRIES_ENV, raising=False)
    merged = _merge_anchors_across_workspaces(
        [("srv", {"jobs-entry": [ja, jb], "route": [route_a, route_b]})]
    )
    (jobs_cand,) = merged["jobs-entry"]
    (route_cand,) = merged["route"]
    assert set(jobs_cand.routes) == set(ja.routes) | set(jb.routes)
    assert route_cand.routes == ()
