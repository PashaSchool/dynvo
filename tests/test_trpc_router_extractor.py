"""Tests for the tRPC router extractor (Sprint 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.trpc_router import (
    TrpcRouterExtractor,
    collect_trpc_routers,
)
from faultline.protocols import Extractor


def _w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_extractor_conforms_to_protocol():
    assert isinstance(TrpcRouterExtractor(), Extractor)


def test_basic_router_with_query_and_mutation(tmp_path):
    _w(tmp_path, "src/server/api/routers/billing.ts", '''
import { z } from "zod";
import { createTRPCRouter, publicProcedure } from "@/server/api/trpc";

export const billingRouter = createTRPCRouter({
  getInvoices: publicProcedure
    .input(z.object({ from: z.string() }))
    .query(({ input }) => { /* */ }),

  createInvoice: publicProcedure
    .input(z.object({ amount: z.number() }))
    .mutation(({ input }) => { /* */ }),

  cancelSubscription: publicProcedure
    .mutation(() => { /* */ }),
});
''')
    out = collect_trpc_routers(tmp_path)
    assert len(out) == 1
    proc_names = {p.name for p in out[0].procedures}
    assert proc_names == {"getInvoices", "createInvoice", "cancelSubscription"}


def test_router_with_protected_procedure(tmp_path):
    _w(tmp_path, "src/routers/users.ts", '''
import { protectedProcedure, router } from "@/trpc";

export const usersRouter = router({
  me: protectedProcedure.query(() => { /* */ }),
  updateProfile: protectedProcedure.mutation(() => { /* */ }),
});
''')
    out = collect_trpc_routers(tmp_path)
    assert len(out) == 1
    assert {p.name for p in out[0].procedures} == {"me", "updateProfile"}


def test_skips_files_without_trpc_hints(tmp_path):
    _w(tmp_path, "src/lib/utils.ts", '''
export const handler = {
  getUsers: someProc.query(() => 1),
  createUser: someProc.mutation(() => 2),
};
''')
    # No @trpc/server, no createTRPCRouter, no procedure-style imports
    # → not recognised as tRPC.
    assert collect_trpc_routers(tmp_path) == []


def test_skips_test_dirs(tmp_path):
    _w(tmp_path, "tests/billing.test.ts", '''
import { createTRPCRouter, publicProcedure } from "@trpc/server";
const r = createTRPCRouter({ x: publicProcedure.query(() => 1) });
''')
    assert collect_trpc_routers(tmp_path) == []


def test_subscription_kind_recognised(tmp_path):
    _w(tmp_path, "src/api/notifications.ts", '''
import { publicProcedure, router } from "@/trpc";
export const notificationsRouter = router({
  onNew: publicProcedure.subscription(() => { /* */ }),
});
''')
    out = collect_trpc_routers(tmp_path)
    assert len(out) == 1
    assert out[0].procedures[0].kind == "subscription"


def test_dedupes_same_procedure_name(tmp_path):
    """If a regex would match the same name twice (rare), de-dupe."""
    _w(tmp_path, "src/api/x.ts", '''
import { publicProcedure, router } from "@trpc/server";
export const r = router({
  list: publicProcedure.query(() => 1),
});
''')
    out = collect_trpc_routers(tmp_path)
    names = [p.name for p in out[0].procedures]
    assert names.count("list") == 1


def test_extractor_emits_signal_with_router_basename(tmp_path):
    _w(tmp_path, "src/server/routers/billing.ts", '''
import { publicProcedure, createTRPCRouter } from "@trpc/server";
export const billingRouter = createTRPCRouter({
  getInvoices: publicProcedure.query(() => 1),
  createInvoice: publicProcedure.mutation(() => 2),
});
''')
    sigs = TrpcRouterExtractor().extract(tmp_path, files=())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "trpc-router-file"
    assert s.payload["router_basename"] == "billing"
    assert s.payload["procedure_count"] == 2


def test_extractor_applicable_false_without_trpc(tmp_path):
    _w(tmp_path, "src/x.ts", "export const x = 1;")
    assert TrpcRouterExtractor().applicable(tmp_path) is False


def test_extractor_applicable_true_with_trpc_import(tmp_path):
    _w(tmp_path, "src/api.ts", "import { x } from '@trpc/server';")
    assert TrpcRouterExtractor().applicable(tmp_path) is True
