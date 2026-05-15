"""Tests for the Sprint 6 TS library exports extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.ts_library_exports import (
    TsLibraryExportsExtractor,
    _parse_direct_exports,
    _parse_named_reexports,
    collect_ts_library_indices,
)
from faultline.protocols import Extractor


def _w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_extractor_conforms_to_protocol():
    assert isinstance(TsLibraryExportsExtractor(), Extractor)


# ── parsers ───────────────────────────────────────────────────────────


def test_parse_named_reexports_basic():
    text = '''export { foo, bar } from "./things";'''
    assert _parse_named_reexports(text) == ["foo", "bar"]


def test_parse_named_reexports_with_alias():
    text = '''export { foo as renamed, bar } from "./things";'''
    assert _parse_named_reexports(text) == ["renamed", "bar"]


def test_parse_named_reexports_multiline():
    text = '''
export {
  twoFactor,
  oauth,
  magicLink,
  emailOtp,
} from "./plugins";
'''
    assert _parse_named_reexports(text) == [
        "twoFactor", "oauth", "magicLink", "emailOtp",
    ]


def test_parse_named_reexports_strips_type_keyword():
    text = '''export { type FooType, bar } from "./x";'''
    assert _parse_named_reexports(text) == ["FooType", "bar"]


def test_parse_direct_exports_const_function_class():
    text = '''
export const billingConfig = { /* */ };
export function createInvoice() {}
export async function processPayment() {}
export class Subscription {}
export interface User {}
'''
    out = _parse_direct_exports(text)
    assert "billingConfig" in out
    assert "createInvoice" in out
    assert "processPayment" in out
    assert "Subscription" in out
    assert "User" in out


# ── full collection ──────────────────────────────────────────────────


def test_collect_indices_from_index_ts(tmp_path):
    _w(tmp_path, "packages/lib/src/index.ts", '''
export { betterAuth } from "./core";
export { createAuthClient } from "./client";
''')
    out = collect_ts_library_indices(tmp_path)
    assert len(out) == 1
    assert set(out[0].exports) == {"betterAuth", "createAuthClient"}


def test_collect_indices_groups_by_subdir(tmp_path):
    _w(tmp_path, "src/index.ts", '''export { core } from "./core";''')
    _w(tmp_path, "src/plugins/index.ts", '''
export { twoFactor } from "./two-factor";
export { oauth } from "./oauth";
export { magicLink } from "./magic-link";
''')
    out = collect_ts_library_indices(tmp_path)
    files = sorted(idx.file for idx in out)
    assert files == ["src/index.ts", "src/plugins/index.ts"]


def test_collect_indices_skips_node_modules_and_tests(tmp_path):
    _w(tmp_path, "node_modules/somelib/index.ts", '''export { x } from "./y";''')
    _w(tmp_path, "tests/index.ts", '''export { x } from "./y";''')
    _w(tmp_path, "src/index.ts", '''export { betterAuth } from "./core";''')

    out = collect_ts_library_indices(tmp_path)
    files = [idx.file for idx in out]
    assert files == ["src/index.ts"]


def test_collect_indices_skips_files_with_no_exports(tmp_path):
    _w(tmp_path, "src/index.ts", '''
import { foo } from "./bar";
const x = 1;
''')
    assert collect_ts_library_indices(tmp_path) == []


def test_collect_indices_supports_jsx_and_mjs(tmp_path):
    _w(tmp_path, "src/index.tsx", '''export { Component } from "./component";''')
    _w(tmp_path, "esm/index.mjs", '''export { fn } from "./fn";''')
    out = collect_ts_library_indices(tmp_path)
    files = sorted(idx.file for idx in out)
    assert "esm/index.mjs" in files
    assert "src/index.tsx" in files


def test_collect_indices_dedupes_repeated_export_names(tmp_path):
    _w(tmp_path, "src/index.ts", '''
export { foo } from "./a";
export { foo } from "./b";
''')
    out = collect_ts_library_indices(tmp_path)
    assert out[0].exports == ("foo",)


# ── extractor wrapper ────────────────────────────────────────────────


def test_extractor_emits_signal_with_export_sample(tmp_path):
    _w(tmp_path, "src/plugins/index.ts", '''
export { twoFactor } from "./two-factor";
export { oauth } from "./oauth";
export { magicLink } from "./magic-link";
''')
    sigs = TsLibraryExportsExtractor().extract(tmp_path, files=())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.kind == "ts-library-index"
    assert s.payload["export_count"] == 3
    assert "twoFactor" in s.payload["sample_exports"]


def test_extractor_applicable_false_when_no_index_files(tmp_path):
    _w(tmp_path, "src/main.ts", "console.log('hi')")
    assert TsLibraryExportsExtractor().applicable(tmp_path) is False


def test_extractor_applicable_true_with_any_index_file(tmp_path):
    _w(tmp_path, "src/index.ts", "export const x = 1;")
    assert TsLibraryExportsExtractor().applicable(tmp_path) is True
