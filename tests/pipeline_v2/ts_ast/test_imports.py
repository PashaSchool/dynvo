"""W6-AST M2 unit tests — import/export edge extraction (spec §3.M2).

Uses a *private* minimal parse helper (tree-sitter directly) until M1's
shared ``parse.py`` lands; ``extract_imports`` itself takes a ready
tree + text, so these tests exercise the frozen contract exactly.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")
pytest.importorskip("tree_sitter_javascript")

import tree_sitter_javascript as _tsjs
import tree_sitter_typescript as _tsts
from tree_sitter import Language, Parser, Tree

from faultline.pipeline_v2.ts_ast.imports import (
    ExportEntry,
    ImportEdge,
    extract_imports,
)

_LANGUAGES = {
    "ts": Language(_tsts.language_typescript()),
    "tsx": Language(_tsts.language_tsx()),
    "js": Language(_tsjs.language()),
    "jsx": Language(_tsjs.language()),
}


def _parse(src: str, lang: str) -> tuple[Tree, bytes]:
    """Private M2 parse helper — replaced by M1's parse util at integration."""
    text = src.encode("utf-8")
    return Parser(_LANGUAGES[lang]).parse(text), text


def _extract(
    src: str, lang: str = "ts", path: str = "src/mod.ts"
) -> tuple[list[ImportEdge], list[ExportEntry]]:
    tree, text = _parse(src, lang)
    return extract_imports(path, lang, tree, text)


P = "src/mod.ts"


# ---------------------------------------------------------------------------
# import statements
# ---------------------------------------------------------------------------


class TestImportStatements:
    def test_named_import_uses_original_names(self) -> None:
        edges, exports = _extract("import { a, b as c } from './m';\n")
        assert edges == [ImportEdge(P, "named", ("a", "b"), "./m", 1)]
        assert exports == []

    def test_default_import_binds_local_name(self) -> None:
        edges, _ = _extract("import React from 'react';\n")
        assert edges == [ImportEdge(P, "default", ("React",), "react", 1)]

    def test_namespace_import(self) -> None:
        edges, _ = _extract("import * as path from 'node:path';\n")
        assert edges == [ImportEdge(P, "namespace", ("path",), "node:path", 1)]

    def test_side_effect_import(self) -> None:
        edges, _ = _extract("import './styles.css';\n")
        assert edges == [ImportEdge(P, "side_effect", (), "./styles.css", 1)]

    def test_combined_default_and_named_yields_two_edges(self) -> None:
        edges, _ = _extract("import Def, { n1, n2 as x } from './combo';\n")
        assert edges == [
            ImportEdge(P, "default", ("Def",), "./combo", 1),
            ImportEdge(P, "named", ("n1", "n2"), "./combo", 1),
        ]

    def test_combined_default_and_namespace(self) -> None:
        edges, _ = _extract("import Def, * as ns from './combo';\n")
        assert edges == [
            ImportEdge(P, "default", ("Def",), "./combo", 1),
            ImportEdge(P, "namespace", ("ns",), "./combo", 1),
        ]

    def test_multiline_import_with_inner_comment(self) -> None:
        src = (
            "import {\n"
            "  multi1,\n"
            "  // a trailing comment between specifiers\n"
            "  multi2 as m2,\n"
            "} from './multiline';\n"
        )
        edges, _ = _extract(src)
        assert edges == [ImportEdge(P, "named", ("multi1", "multi2"), "./multiline", 1)]

    def test_string_specifier_name_is_unquoted(self) -> None:
        edges, _ = _extract('import { "weird name" as w } from "./m";\n')
        assert edges == [ImportEdge(P, "named", ("weird name",), "./m", 1)]

    def test_line_numbers_are_one_based_statement_lines(self) -> None:
        src = "const pad = 1;\n\nimport { a } from './m';\n"
        edges, _ = _extract(src)
        assert edges == [ImportEdge(P, "named", ("a",), "./m", 3)]


# ---------------------------------------------------------------------------
# type-only imports / exports ('type:' prefix, spec §3.M2)
# ---------------------------------------------------------------------------


class TestTypeOnly:
    def test_import_type_statement_prefixes_all_names(self) -> None:
        edges, _ = _extract("import type { Foo, Bar } from './types';\n")
        assert edges == [
            ImportEdge(P, "named", ("type:Bar", "type:Foo"), "./types", 1)
        ]

    def test_inline_type_specifier_mixed(self) -> None:
        edges, _ = _extract("import { type X, y } from './mixed';\n")
        assert edges == [ImportEdge(P, "named", ("type:X", "y"), "./mixed", 1)]

    def test_import_type_default(self) -> None:
        edges, _ = _extract("import type D from './m';\n")
        assert edges == [ImportEdge(P, "default", ("type:D",), "./m", 1)]

    def test_export_type_from(self) -> None:
        edges, exports = _extract("export type { T } from './m';\n")
        assert edges == [ImportEdge(P, "reexport_named", ("type:T",), "./m", 1)]
        assert exports == [ExportEntry(P, "type:T", "named", "./m")]

    def test_export_clause_mixed_inline_type(self) -> None:
        edges, exports = _extract("export { type T2, plain } from './d';\n")
        assert edges == [
            ImportEdge(P, "reexport_named", ("plain", "type:T2"), "./d", 1)
        ]
        assert exports == [
            ExportEntry(P, "plain", "named", "./d"),
            ExportEntry(P, "type:T2", "named", "./d"),
        ]

    def test_export_interface_and_type_alias_prefixed(self) -> None:
        _, exports = _extract(
            "export interface IFace { x: number }\nexport type Alias = string;\n"
        )
        assert exports == [
            ExportEntry(P, "type:Alias", "named", None),
            ExportEntry(P, "type:IFace", "named", None),
        ]


# ---------------------------------------------------------------------------
# dynamic import()
# ---------------------------------------------------------------------------


class TestDynamicImport:
    def test_dynamic_import_string_literal(self) -> None:
        edges, _ = _extract("const x = import('./dyn');\n")
        assert edges == [ImportEdge(P, "dynamic", (), "./dyn", 1)]

    def test_dynamic_import_template_without_substitution(self) -> None:
        edges, _ = _extract("const x = import(`./dyn-tpl`);\n")
        assert edges == [ImportEdge(P, "dynamic", (), "./dyn-tpl", 1)]

    def test_dynamic_import_template_with_substitution_is_skipped(self) -> None:
        edges, _ = _extract("const x = import(`./dyn-${name}`);\n")
        assert edges == []

    def test_dynamic_import_variable_argument_is_skipped(self) -> None:
        edges, _ = _extract("const x = import(modulePath);\n")
        assert edges == []

    def test_await_dynamic_import_inside_function(self) -> None:
        src = (
            "async function load() {\n"
            "  const mod = await import('./lazy');\n"
            "  return mod;\n"
            "}\n"
        )
        edges, _ = _extract(src)
        assert edges == [ImportEdge(P, "dynamic", (), "./lazy", 2)]


# ---------------------------------------------------------------------------
# CommonJS require()
# ---------------------------------------------------------------------------


class TestRequire:
    def test_require_identifier_binding(self) -> None:
        edges, _ = _extract(
            "const lib = require('./lib');\n", lang="js", path="src/mod.js"
        )
        assert edges == [ImportEdge("src/mod.js", "require", ("lib",), "./lib", 1)]

    def test_require_object_pattern_uses_original_keys(self) -> None:
        edges, _ = _extract(
            "const { m1, m2: alias } = require('./destructure');\n",
            lang="js",
            path="src/mod.js",
        )
        assert edges == [
            ImportEdge("src/mod.js", "require", ("m1", "m2"), "./destructure", 1)
        ]

    def test_bare_require_has_no_names(self) -> None:
        edges, _ = _extract(
            "require('./side-effect');\n", lang="js", path="src/mod.js"
        )
        assert edges == [
            ImportEdge("src/mod.js", "require", (), "./side-effect", 1)
        ]

    def test_nested_require_inside_block_is_found(self) -> None:
        src = "function f() {\n  if (cond) {\n    const inner = require('./inner');\n  }\n}\n"
        edges, _ = _extract(src, lang="js", path="src/mod.js")
        assert edges == [
            ImportEdge("src/mod.js", "require", ("inner",), "./inner", 3)
        ]

    def test_member_access_on_require_keeps_edge_without_names(self) -> None:
        edges, _ = _extract(
            "const d = require('./m').something;\n", lang="js", path="src/mod.js"
        )
        assert edges == [ImportEdge("src/mod.js", "require", (), "./m", 1)]

    def test_bare_require_ignored_in_ts_files(self) -> None:
        edges, _ = _extract("const lib = require('./lib');\n", lang="ts")
        assert edges == []

    def test_ts_import_require_clause_is_extracted(self) -> None:
        edges, _ = _extract("import x2 = require('./interop');\n", lang="ts")
        assert edges == [ImportEdge(P, "require", ("x2",), "./interop", 1)]

    def test_require_non_literal_argument_is_skipped(self) -> None:
        edges, _ = _extract(
            "const lib = require(pathVar);\n", lang="js", path="src/mod.js"
        )
        assert edges == []


# ---------------------------------------------------------------------------
# export statements → edges + export index
# ---------------------------------------------------------------------------


class TestExportStatements:
    def test_export_star_from(self) -> None:
        edges, exports = _extract("export * from './star';\n")
        assert edges == [ImportEdge(P, "reexport_star", (), "./star", 1)]
        assert exports == [ExportEntry(P, "*", "star_from", "./star")]

    def test_export_star_as_namespace(self) -> None:
        edges, exports = _extract("export * as nsx from './star-ns';\n")
        assert edges == [ImportEdge(P, "reexport_star", ("nsx",), "./star-ns", 1)]
        assert exports == [ExportEntry(P, "nsx", "star_from", "./star-ns")]

    def test_reexport_named_from(self) -> None:
        edges, exports = _extract("export { a, b as c } from './re';\n")
        assert edges == [ImportEdge(P, "reexport_named", ("a", "b"), "./re", 1)]
        assert exports == [
            ExportEntry(P, "a", "named", "./re"),
            ExportEntry(P, "c", "named", "./re"),
        ]

    def test_reexport_as_default(self) -> None:
        edges, exports = _extract("export { x as default } from './m';\n")
        assert edges == [ImportEdge(P, "reexport_named", ("x",), "./m", 1)]
        assert exports == [ExportEntry(P, "default", "default", "./m")]

    def test_local_export_clause_no_edge(self) -> None:
        src = "const l1 = 1;\nconst l2 = 2;\nexport { l1, l2 as pub };\n"
        edges, exports = _extract(src)
        assert edges == []
        assert exports == [
            ExportEntry(P, "l1", "named", None),
            ExportEntry(P, "pub", "named", None),
        ]

    def test_local_export_as_default(self) -> None:
        _, exports = _extract("const thing = 1;\nexport { thing as default };\n")
        assert exports == [ExportEntry(P, "default", "default", None)]

    def test_export_default_function_declaration(self) -> None:
        _, exports = _extract("export default function main() {}\n")
        assert exports == [ExportEntry(P, "default", "default", None)]

    def test_export_default_anonymous_arrow(self) -> None:
        _, exports = _extract("export default () => {};\n")
        assert exports == [ExportEntry(P, "default", "default", None)]

    def test_export_default_expression(self) -> None:
        _, exports = _extract("export default 42;\n")
        assert exports == [ExportEntry(P, "default", "default", None)]

    def test_export_const_multiple_declarators(self) -> None:
        _, exports = _extract("export const cx = 1, cy = 2;\n")
        assert exports == [
            ExportEntry(P, "cx", "named", None),
            ExportEntry(P, "cy", "named", None),
        ]

    def test_export_destructured_const_uses_bindings(self) -> None:
        _, exports = _extract("export const { a, b: c } = obj;\n")
        assert exports == [
            ExportEntry(P, "a", "named", None),
            ExportEntry(P, "c", "named", None),
        ]

    def test_export_array_pattern_bindings(self) -> None:
        _, exports = _extract("export const [x, y = 2, ...rest] = arr;\n")
        assert exports == [
            ExportEntry(P, "rest", "named", None),
            ExportEntry(P, "x", "named", None),
            ExportEntry(P, "y", "named", None),
        ]

    def test_export_function_and_class(self) -> None:
        _, exports = _extract("export function fn() {}\nexport class Klass {}\n")
        assert exports == [
            ExportEntry(P, "Klass", "named", None),
            ExportEntry(P, "fn", "named", None),
        ]

    def test_export_enum_is_runtime_named(self) -> None:
        _, exports = _extract("export enum Enu { A }\n")
        assert exports == [ExportEntry(P, "Enu", "named", None)]

    def test_export_namespace_indexes_namespace_only(self) -> None:
        _, exports = _extract("export namespace N { export const q = 1 }\n")
        assert exports == [ExportEntry(P, "N", "named", None)]

    def test_export_declare_const(self) -> None:
        _, exports = _extract("export declare const dc: number;\n")
        assert exports == [ExportEntry(P, "dc", "named", None)]

    def test_export_equals_legacy_is_skipped(self) -> None:
        edges, exports = _extract("const x = 1;\nexport = x;\n")
        assert edges == []
        assert exports == []


# ---------------------------------------------------------------------------
# noise immunity: strings, templates, comments, jsx
# ---------------------------------------------------------------------------


class TestNoiseImmunity:
    def test_import_text_inside_string_literal_is_ignored(self) -> None:
        src = "const s = \"import { fake } from './not-real'\";\n"
        edges, exports = _extract(src)
        assert edges == []
        assert exports == []

    def test_export_text_inside_template_literal_is_ignored(self) -> None:
        src = "const t = `export * from './also-fake'`;\n"
        edges, exports = _extract(src)
        assert edges == []
        assert exports == []

    def test_commented_out_imports_are_ignored(self) -> None:
        src = (
            "// import { c1 } from './commented'\n"
            "/* export { c2 } from './blocked' */\n"
        )
        edges, exports = _extract(src)
        assert edges == []
        assert exports == []

    def test_jsx_file_imports_and_require(self) -> None:
        src = (
            "import Button from './Button';\n"
            "const helpers = require('./helpers');\n"
            "export default function App() {\n"
            "  return <Button label={'import nothing'} />;\n"
            "}\n"
        )
        edges, exports = _extract(src, lang="jsx", path="src/App.jsx")
        assert edges == [
            ImportEdge("src/App.jsx", "default", ("Button",), "./Button", 1),
            ImportEdge("src/App.jsx", "require", ("helpers",), "./helpers", 2),
        ]
        assert exports == [ExportEntry("src/App.jsx", "default", "default", None)]

    def test_tsx_component_file(self) -> None:
        src = (
            "import * as React from 'react';\n"
            "import type { Props } from './types';\n"
            "export const Card = (p: Props) => <div>{p.title}</div>;\n"
        )
        edges, exports = _extract(src, lang="tsx", path="src/Card.tsx")
        assert edges == [
            ImportEdge("src/Card.tsx", "namespace", ("React",), "react", 1),
            ImportEdge("src/Card.tsx", "named", ("type:Props",), "./types", 2),
        ]
        assert exports == [ExportEntry("src/Card.tsx", "Card", "named", None)]

    def test_empty_file(self) -> None:
        assert _extract("") == ([], [])


# ---------------------------------------------------------------------------
# determinism + canonical ordering + contract
# ---------------------------------------------------------------------------


class TestDeterminismAndContract:
    def test_edges_sorted_by_line_then_kind(self) -> None:
        src = (
            "import { z } from './z';\n"
            "import Def, { a } from './a';\n"
            "export * from './s';\n"
        )
        edges, _ = _extract(src)
        keys = [(e.line, e.kind, e.raw_target, e.names) for e in edges]
        assert keys == sorted(keys)
        assert [e.kind for e in edges] == ["named", "default", "named", "reexport_star"]

    def test_exports_sorted_by_name_regardless_of_source_order(self) -> None:
        src = "export const zz = 1;\nexport const aa = 2;\nexport * from './mid';\n"
        _, exports = _extract(src)
        assert [x.name for x in exports] == ["*", "aa", "zz"]

    def test_names_tuples_are_sorted(self) -> None:
        edges, _ = _extract("import { zeta, alpha, mid } from './m';\n")
        assert edges[0].names == ("alpha", "mid", "zeta")

    def test_identical_entries_are_deduplicated(self) -> None:
        src = "const a = 1;\nexport { a };\nexport { a };\n"
        _, exports = _extract(src)
        assert exports == [ExportEntry(P, "a", "named", None)]

    def test_same_line_identical_edges_deduplicated(self) -> None:
        edges, _ = _extract("import './x'; import './x';\n")
        assert edges == [ImportEdge(P, "side_effect", (), "./x", 1)]

    def test_distinct_star_reexports_both_kept(self) -> None:
        _, exports = _extract("export * from './a';\nexport * from './b';\n")
        assert exports == [
            ExportEntry(P, "*", "star_from", "./a"),
            ExportEntry(P, "*", "star_from", "./b"),
        ]

    def test_double_run_is_identical(self) -> None:
        src = (
            "import D, { a as b } from './m';\n"
            "export { c } from './n';\n"
            "const l = import('./dyn');\n"
        )
        assert _extract(src) == _extract(src)

    def test_invalid_lang_raises(self) -> None:
        tree, text = _parse("import x from './m';\n", "ts")
        with pytest.raises(ValueError, match="unsupported lang"):
            extract_imports(P, "rb", tree, text)

    def test_edges_and_entries_are_frozen_value_objects(self) -> None:
        edges, exports = _extract("export { a } from './m';\n")
        with pytest.raises(AttributeError):
            edges[0].line = 99  # type: ignore[misc]
        assert hash(exports[0]) == hash(ExportEntry(P, "a", "named", "./m"))
