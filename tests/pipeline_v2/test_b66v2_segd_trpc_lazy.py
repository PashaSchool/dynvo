"""B66-v2 Seg D — cal.com lazy handler-cache tRPC routers.

EXHIBIT (cal.com residual-debt: 38 unseen ``_router.ts``): a router file that
imports ``router`` RELATIVELY (not ``@trpc/server``) and dispatches every
procedure through a lazily-imported ``*.handler`` module
(``UNSTABLE_HANDLER_CACHE`` + ``await import("./x.handler")``) fails the
canonical ``require_import_re`` gate, so all its procedures stay unseen. Seg D
adds a flag-gated lazy handler-cache gate (router constructor AND lazy handler
BOTH required). Kill-switch: OFF -> byte-identical to the B66-merged world.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.server_api_entries import (
    SERVER_API_ENTRIES_ENV,
    ServerApiEntryExtractor,
)
from faultline.pipeline_v2.ownership_v2 import OWNERSHIP_V2_ENV, ownership_v2_enabled
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


def _extract(tmp_path: Path, rel: str, body: str, **kw):
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return ServerApiEntryExtractor().extract(_ctx(tmp_path, [rel], **kw))


# The cal.com UNSTABLE_HANDLER_CACHE shape: relative ``router`` import (fails the
# @trpc/server gate), custom ``*Procedure`` builders, lazy ``import("./x.handler")``.
_CAL_WEBHOOK_ROUTER = (
    'import { router } from "../../../trpc";\n'
    'import { createWebhookPbacProcedure } from "./util";\n'
    "type WebhookRouterHandlerCache = {\n"
    '  list?: typeof import("./list.handler").listHandler;\n'
    '  create?: typeof import("./create.handler").createHandler;\n'
    "};\n"
    "const UNSTABLE_HANDLER_CACHE: WebhookRouterHandlerCache = {};\n"
    "export const webhookRouter = router({\n"
    '  list: createWebhookPbacProcedure("webhook.read")\n'
    "    .input(ZListInputSchema)\n"
    "    .query(async ({ ctx, input }) => {\n"
    "      if (!UNSTABLE_HANDLER_CACHE.list) {\n"
    '        UNSTABLE_HANDLER_CACHE.list = await import("./list.handler").then((m) => m.listHandler);\n'
    "      }\n"
    "      return UNSTABLE_HANDLER_CACHE.list({ ctx, input });\n"
    "    }),\n"
    '  create: createWebhookPbacProcedure("webhook.write")\n'
    "    .input(ZCreateInputSchema)\n"
    "    .mutation(async ({ ctx, input }) => {\n"
    '        UNSTABLE_HANDLER_CACHE.create = await import("./create.handler").then((m) => m.createHandler);\n'
    "      return UNSTABLE_HANDLER_CACHE.create({ ctx, input });\n"
    "    }),\n"
    "});\n"
)

_CAL_PATH = "packages/trpc/server/routers/viewer/webhook/_router.tsx"


def test_segd_flag_reader_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OWNERSHIP_V2_ENV, raising=False)
    assert ownership_v2_enabled() is False
    for falsy in ("", "0", "false", "no", "off", "OFF"):
        monkeypatch.setenv(OWNERSHIP_V2_ENV, falsy)
        assert ownership_v2_enabled() is False
    for truthy in ("1", "true", "on", "yes"):
        monkeypatch.setenv(OWNERSHIP_V2_ENV, truthy)
        assert ownership_v2_enabled() is True


def test_segd_off_lazy_router_skipped_killswitch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KILL-SWITCH: SERVER_API on but OWNERSHIP_V2 off -> cal getHandler router
    stays unseen exactly as in the B66-merged world (byte-identical)."""
    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    monkeypatch.delenv(OWNERSHIP_V2_ENV, raising=False)
    assert _extract(tmp_path, _CAL_PATH, _CAL_WEBHOOK_ROUTER) == []


def test_segd_cal_gethandler_router_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXHIBIT: the lazy handler-cache router resolves its procedures once
    OWNERSHIP_V2 is on (``webhook.list`` QUERY + ``webhook.create`` MUTATION)."""
    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    (a,) = _extract(tmp_path, _CAL_PATH, _CAL_WEBHOOK_ROUTER)
    assert a.name == "webhook"
    assert set(a.routes) == {
        ("webhook.list", "QUERY", _CAL_PATH),
        ("webhook.create", "MUTATION", _CAL_PATH),
    }


def test_segd_lazy_handler_without_router_ctor_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-CASE: a file that lazily imports a ``*.handler`` but constructs NO
    router is not a tRPC router -> honest skip even with OWNERSHIP_V2 on (the
    double-signal gate: router constructor AND lazy handler both required)."""
    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    body = (
        'import { thing } from "./util";\n'
        "export async function run(input) {\n"
        '  const h = await import("./do.handler").then((m) => m.doHandler);\n'
        "  return h(input);\n"
        "}\n"
    )
    assert _extract(tmp_path, "packages/svc/run.ts", body) == []


def test_segd_router_ctor_without_lazy_handler_still_needs_trpc_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-CASE: a bare ``router({...})`` with neither a @trpc import nor a
    lazy handler import stays an honest skip (Seg D does not loosen the gate for
    ordinary router-shaped objects)."""
    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    body = "export const r = router({ list: somethingProcedure.query(() => {}) });\n"
    assert _extract(tmp_path, "src/y.ts", body) == []


def test_segd_canonical_trpc_router_unaffected_by_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standard @trpc/server router resolves identically whether OWNERSHIP_V2
    is on or off (Seg D only ADDS the lazy-cache gate; canonical path untouched)."""
    monkeypatch.setenv(SERVER_API_ENTRIES_ENV, "1")
    body = (
        'import { publicProcedure, router } from "@trpc/server";\n'
        "export const orgRouter = router({\n"
        "  list: publicProcedure.query(() => {}),\n"
        "});\n"
    )
    monkeypatch.delenv(OWNERSHIP_V2_ENV, raising=False)
    (off,) = _extract(tmp_path, "server/routers/org.ts", body)
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    (on,) = _extract(tmp_path, "server/routers/org.ts", body)
    assert set(off.routes) == set(on.routes) == {
        ("org.list", "QUERY", "server/routers/org.ts"),
    }
