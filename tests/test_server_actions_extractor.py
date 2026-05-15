"""Tests for the Server Actions extractor (Sprint 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.server_actions import (
    ServerActionsExtractor,
    collect_server_action_files,
)
from faultline.protocols import Extractor


def _w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_extractor_conforms_to_protocol():
    assert isinstance(ServerActionsExtractor(), Extractor)


def test_file_level_directive_collects_all_exported_async(tmp_path):
    _w(tmp_path, "src/billing/actions.ts", '''
"use server";

export async function createInvoice(input: any) {
  // ...
}

export async function cancelSubscription() {
  // ...
}

export const updateBilling = async (form: FormData) => { /* */ };

export function syncStripe() { /* not async — ignored */ }
''')
    out = collect_server_action_files(tmp_path)
    assert len(out) == 1
    assert out[0].file_level_directive is True
    assert set(out[0].action_names) == {
        "createInvoice", "cancelSubscription", "updateBilling",
    }


def test_inline_directive_collects_only_marked_function(tmp_path):
    _w(tmp_path, "src/lib/mixed.ts", '''
export async function publicAction() {
  "use server";
  return 1;
}

export async function regularUtil() {
  return "not a server action";
}

export const deleteUser = async (id: string) => {
  "use server";
  // ...
};
''')
    out = collect_server_action_files(tmp_path)
    assert len(out) == 1
    assert out[0].file_level_directive is False
    assert set(out[0].action_names) == {"publicAction", "deleteUser"}


def test_skips_files_without_directive(tmp_path):
    _w(tmp_path, "src/utils.ts", '''
export async function foo() { return 1; }
export async function bar() { return 2; }
''')
    assert collect_server_action_files(tmp_path) == []


def test_skips_test_dirs_and_node_modules(tmp_path):
    _w(tmp_path, "tests/actions.ts", '"use server";\nexport async function x() {}')
    _w(tmp_path, "node_modules/lib/actions.ts", '"use server";\nexport async function y() {}')
    assert collect_server_action_files(tmp_path) == []


def test_directive_with_imports_above(tmp_path):
    """Some codebases import first, then declare — still counts."""
    _w(tmp_path, "src/x.ts", '''
import { z } from "zod";
import { db } from "@/db";

"use server";

export async function action1() {}
''')
    out = collect_server_action_files(tmp_path)
    assert len(out) == 1
    assert out[0].file_level_directive is True


def test_handles_single_quotes_directive(tmp_path):
    _w(tmp_path, "src/x.ts", "'use server';\nexport async function a() {}")
    out = collect_server_action_files(tmp_path)
    assert len(out) == 1
    assert out[0].action_names == ("a",)


def test_extractor_emits_signal_with_sample_names(tmp_path):
    _w(tmp_path, "src/billing/actions.ts", '''
"use server";
export async function a1() {}
export async function a2() {}
export async function a3() {}
''')
    sigs = ServerActionsExtractor().extract(tmp_path, files=())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "server-actions-file"
    assert s.payload["action_count"] == 3
    assert set(s.payload["sample_names"]) == {"a1", "a2", "a3"}
    assert s.payload["file_level_directive"] is True


def test_extractor_applicable_false_on_repo_without_directive(tmp_path):
    _w(tmp_path, "src/utils.ts", "export const x = 1;")
    assert ServerActionsExtractor().applicable(tmp_path) is False


def test_extractor_applicable_true_when_directive_present(tmp_path):
    _w(tmp_path, "src/x.ts", '"use server";\nexport async function a() {}')
    assert ServerActionsExtractor().applicable(tmp_path) is True
