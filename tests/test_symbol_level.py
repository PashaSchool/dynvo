"""Tests for symbol-level feature attribution (Phase 1 MVP)."""
import textwrap

import pytest

from faultline.analyzer.ast_extractor import (
    extract_symbol_ranges,
    extract_named_imports,
    FileSignature,
)
from faultline.analyzer.shared_files import (
    build_shared_attributions,
    _merge_line_ranges,
    _lines_in_ranges,
)
from faultline.models.types import SymbolRange


# ---------------------------------------------------------------------------
# extract_symbol_ranges
# ---------------------------------------------------------------------------

class TestExtractSymbolRanges:
    def test_single_const_export(self):
        source = 'export const FOO = "bar";\n'
        ranges = extract_symbol_ranges(source)
        assert len(ranges) == 1
        assert ranges[0].name == "FOO"
        assert ranges[0].kind == "const"
        assert ranges[0].start_line == 1
        # last export extends to EOF
        assert ranges[0].end_line == 2

    def test_multiple_exports_line_ranges(self):
        source = textwrap.dedent("""\
            export const A = 1;
            export const B = 2;
            export const C = 3;
        """)
        ranges = extract_symbol_ranges(source)
        assert len(ranges) == 3
        assert ranges[0].name == "A"
        assert ranges[0].start_line == 1
        assert ranges[0].end_line == 1  # ends before B starts
        assert ranges[1].name == "B"
        assert ranges[1].start_line == 2
        assert ranges[1].end_line == 2
        assert ranges[2].name == "C"
        assert ranges[2].start_line == 3
        # last export extends to EOF

    def test_function_export(self):
        source = textwrap.dedent("""\
            export function doSomething() {
              return 42;
            }

            export const VALUE = 10;
        """)
        ranges = extract_symbol_ranges(source)
        assert len(ranges) == 2
        assert ranges[0].name == "doSomething"
        assert ranges[0].kind == "function"
        assert ranges[0].start_line == 1
        assert ranges[0].end_line == 4  # before VALUE at line 5
        assert ranges[1].name == "VALUE"
        assert ranges[1].kind == "const"

    def test_class_export(self):
        source = "export class UserService {\n  constructor() {}\n}\n"
        ranges = extract_symbol_ranges(source)
        assert len(ranges) == 1
        assert ranges[0].name == "UserService"
        assert ranges[0].kind == "class"

    def test_type_and_enum_exports(self):
        source = textwrap.dedent("""\
            export type UserId = string;
            export interface Config {
              apiKey: string;
            }
            export enum Status {
              Active,
              Inactive,
            }
        """)
        ranges = extract_symbol_ranges(source)
        names = {r.name for r in ranges}
        assert "UserId" in names
        assert "Config" in names
        assert "Status" in names
        type_range = next(r for r in ranges if r.name == "UserId")
        assert type_range.kind == "type"
        enum_range = next(r for r in ranges if r.name == "Status")
        assert enum_range.kind == "enum"

    def test_reexport(self):
        source = 'export { Foo, Bar as Baz } from "./other";\n'
        ranges = extract_symbol_ranges(source)
        names = {r.name for r in ranges}
        assert "Foo" in names
        assert "Baz" in names  # re-exported as Baz
        for r in ranges:
            assert r.kind == "reexport"

    def test_empty_source(self):
        assert extract_symbol_ranges("") == []
        assert extract_symbol_ranges("const x = 1;\n") == []  # no exports

    def test_deduplication(self):
        source = textwrap.dedent("""\
            export const FOO = 1;
            export { FOO };
        """)
        ranges = extract_symbol_ranges(source)
        names = [r.name for r in ranges]
        assert names.count("FOO") == 1


# ---------------------------------------------------------------------------
# extract_named_imports
# ---------------------------------------------------------------------------

class TestExtractNamedImports:
    def test_basic_named_import(self):
        source = 'import { Foo, Bar } from "./utils";\n'
        result = extract_named_imports(source)
        assert "./utils" in result
        assert result["./utils"] == {"Foo", "Bar"}

    def test_aliased_import(self):
        source = 'import { Foo as MyFoo } from "./utils";\n'
        result = extract_named_imports(source)
        assert result["./utils"] == {"Foo"}  # original name

    def test_alias_path_import(self):
        source = 'import { Config } from "@/lib/config";\n'
        result = extract_named_imports(source)
        assert "@/lib/config" in result
        assert result["@/lib/config"] == {"Config"}

    def test_skips_external_imports(self):
        source = 'import { useState } from "react";\n'
        result = extract_named_imports(source)
        assert len(result) == 0

    def test_namespace_import(self):
        source = 'import * as Utils from "./utils";\n'
        result = extract_named_imports(source)
        assert result["./utils"] == {"*"}

    def test_multiple_imports_same_module(self):
        source = textwrap.dedent("""\
            import { Foo } from "./utils";
            import { Bar } from "./utils";
        """)
        result = extract_named_imports(source)
        assert result["./utils"] == {"Foo", "Bar"}


# ---------------------------------------------------------------------------
# _merge_line_ranges / _lines_in_ranges
# ---------------------------------------------------------------------------

class TestMergeLineRanges:
    def test_no_overlap(self):
        assert _merge_line_ranges([(1, 5), (10, 15)]) == [(1, 5), (10, 15)]

    def test_overlapping(self):
        assert _merge_line_ranges([(1, 10), (5, 15)]) == [(1, 15)]

    def test_adjacent(self):
        assert _merge_line_ranges([(1, 5), (6, 10)]) == [(1, 10)]

    def test_contained(self):
        assert _merge_line_ranges([(1, 20), (5, 10)]) == [(1, 20)]

    def test_unsorted_input(self):
        assert _merge_line_ranges([(10, 15), (1, 5)]) == [(1, 5), (10, 15)]

    def test_empty(self):
        assert _merge_line_ranges([]) == []

    def test_lines_in_ranges(self):
        assert _lines_in_ranges([(1, 5), (10, 15)]) == 11
        assert _lines_in_ranges([(1, 1)]) == 1
        assert _lines_in_ranges([]) == 0


# ---------------------------------------------------------------------------
# build_shared_attributions
# ---------------------------------------------------------------------------

class TestBuildSharedAttributions:
    def _make_sig(self, path: str, source: str) -> FileSignature:
        sig = FileSignature(path=path, source=source)
        sig.symbol_ranges = extract_symbol_ranges(source)
        # Parse exports for is_empty check
        if sig.symbol_ranges:
            sig.exports = [sr.name for sr in sig.symbol_ranges]
        return sig

    def test_no_shared_files(self):
        feature_paths = {
            "auth": ["auth.ts"],
            "dashboard": ["dashboard.ts"],
        }
        result = build_shared_attributions(feature_paths, {}, {})
        assert result == {}

    def test_shared_file_with_imports(self):
        constants_source = textwrap.dedent("""\
            export const ROUTES = { api: "/api" };
            export const FEATURE_FLAGS = {
              enableAuth: true,
              enableDashboard: false,
            };
            export const CONFIG = { timeout: 5000 };
        """)
        sig = self._make_sig("lib/constants.ts", constants_source)

        feature_paths = {
            "auth": ["auth.ts", "lib/constants.ts"],
            "dashboard": ["dashboard.ts", "lib/constants.ts"],
        }
        symbol_imports = {
            "auth.ts": {"lib/constants.ts": {"FEATURE_FLAGS"}},
            "dashboard.ts": {"lib/constants.ts": {"ROUTES", "CONFIG"}},
        }
        signatures = {"lib/constants.ts": sig}

        result = build_shared_attributions(feature_paths, symbol_imports, signatures)

        assert "auth" in result
        assert "dashboard" in result

        auth_attr = result["auth"][0]
        assert auth_attr.file_path == "lib/constants.ts"
        assert "FEATURE_FLAGS" in auth_attr.symbols
        assert auth_attr.attributed_lines > 0
        assert auth_attr.attributed_lines < auth_attr.total_file_lines

        dash_attr = result["dashboard"][0]
        assert "ROUTES" in dash_attr.symbols
        assert "CONFIG" in dash_attr.symbols

    def test_namespace_import_attributes_all(self):
        source = textwrap.dedent("""\
            export const A = 1;
            export const B = 2;
        """)
        sig = self._make_sig("utils.ts", source)

        feature_paths = {
            "feat-a": ["a.ts", "utils.ts"],
            "feat-b": ["b.ts", "utils.ts"],
        }
        symbol_imports = {
            "a.ts": {"utils.ts": {"*"}},  # namespace import
            "b.ts": {"utils.ts": {"A"}},
        }
        signatures = {"utils.ts": sig}

        result = build_shared_attributions(feature_paths, symbol_imports, signatures)

        feat_a_attr = result["feat-a"][0]
        assert set(feat_a_attr.symbols) == {"A", "B"}  # all symbols

    def test_no_imports_attributes_all(self):
        """When no file in the feature imports from the shared file, attribute all symbols."""
        source = "export const X = 1;\nexport const Y = 2;\n"
        sig = self._make_sig("shared.ts", source)

        feature_paths = {
            "feat-a": ["a.ts", "shared.ts"],
            "feat-b": ["b.ts", "shared.ts"],
        }
        symbol_imports = {}  # no imports at all
        signatures = {"shared.ts": sig}

        result = build_shared_attributions(feature_paths, symbol_imports, signatures)
        # Both features get all symbols since neither imports explicitly
        for feat in ("feat-a", "feat-b"):
            attr = result[feat][0]
            assert set(attr.symbols) == {"X", "Y"}

    def test_skips_files_without_symbol_ranges(self):
        sig = FileSignature(path="data.json", source="{}")
        # No symbol_ranges
        feature_paths = {
            "feat-a": ["a.ts", "data.json"],
            "feat-b": ["b.ts", "data.json"],
        }
        result = build_shared_attributions(feature_paths, {}, {"data.json": sig})
        assert result == {}
