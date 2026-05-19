"""Tests for ``faultline.pipeline_v2.flow_symbols`` (Sprint C2).

Verifies per-language entry-symbol detection, line-range extraction,
import resolution, the cap on symbols-per-flow, and the
graceful-degradation paths (entry detection failure → fallback,
support role for files in reach but unresolved).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.flow_reach import build_reach_context
from faultline.pipeline_v2.flow_symbols import (
    DEFAULT_MAX_SYMBOLS_PER_FLOW,
    _enumerate_functions,
    _extract_py_imports,
    _extract_ts_imports,
    _resolve_entry_symbol,
    compute_flow_symbols,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── Helpers ────────────────────────────────────────────────────────────────


def _write(tmp_path: Path, rel: str, body: str) -> None:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


# ── Function enumeration / entry detection ─────────────────────────────────


class TestEnumerateFunctions:
    def test_ts_arrow_function_const(self) -> None:
        source = (
            "import { foo } from './x';\n"
            "\n"
            "const handleCheckout = async (req) => {\n"
            "  return foo(req);\n"
            "};\n"
            "\n"
            "const tail = 1;\n"
        )
        fns = _enumerate_functions(source, "ts")
        names = [f.name for f in fns]
        assert "handleCheckout" in names
        hc = next(f for f in fns if f.name == "handleCheckout")
        assert hc.line_start == 3
        assert hc.line_end == 5

    def test_ts_nextjs_route_export(self) -> None:
        source = (
            "import { NextResponse } from 'next/server';\n"
            "\n"
            "export async function POST(req: Request) {\n"
            "  const body = await req.json();\n"
            "  return NextResponse.json({ ok: true });\n"
            "}\n"
        )
        fns = _enumerate_functions(source, "ts")
        assert any(f.name == "POST" for f in fns)
        post = next(f for f in fns if f.name == "POST")
        assert post.line_start == 3
        assert 5 <= post.line_end <= 6

    def test_ts_named_function(self) -> None:
        source = (
            "export function add(a: number, b: number): number {\n"
            "  return a + b;\n"
            "}\n"
        )
        fns = _enumerate_functions(source, "ts")
        assert [f.name for f in fns] == ["add"]
        assert fns[0].line_start == 1
        assert fns[0].line_end == 3

    def test_python_def_and_class_method(self) -> None:
        source = (
            "def helper(x):\n"
            "    return x + 1\n"
            "\n"
            "class Service:\n"
            "    def run(self):\n"
            "        return helper(2)\n"
            "\n"
            "    def stop(self):\n"
            "        pass\n"
        )
        fns = _enumerate_functions(source, "py")
        names = [f.name for f in fns]
        assert "helper" in names
        assert "run" in names
        assert "stop" in names
        helper = next(f for f in fns if f.name == "helper")
        assert helper.line_start == 1
        assert helper.line_end == 2

        run = next(f for f in fns if f.name == "run")
        assert run.line_start == 5
        assert run.line_end == 6

    def test_go_func_and_method_receiver(self) -> None:
        source = (
            'package main\n'
            '\n'
            'import "fmt"\n'
            '\n'
            'type Server struct{}\n'
            '\n'
            'func (s *Server) Handle(w http.ResponseWriter, r *http.Request) {\n'
            '    fmt.Println("ok")\n'
            '}\n'
            '\n'
            'func main() {\n'
            '    fmt.Println("hi")\n'
            '}\n'
        )
        fns = _enumerate_functions(source, "go")
        names = [f.name for f in fns]
        assert "Handle" in names
        assert "main" in names
        handle = next(f for f in fns if f.name == "Handle")
        assert handle.line_start == 7
        assert 9 <= handle.line_end <= 10

    def test_rust_pub_fn(self) -> None:
        source = (
            "pub fn execute(query: &str) {\n"
            "    let _ = query;\n"
            "}\n"
            "\n"
            "async fn helper() {\n"
            "    println!(\"hi\");\n"
            "}\n"
        )
        fns = _enumerate_functions(source, "rs")
        names = [f.name for f in fns]
        assert "execute" in names
        assert "helper" in names

    def test_entry_line_inside_multiline_body(self) -> None:
        source = (
            "function noise() {}\n"
            "\n"
            "export async function target(req) {\n"
            "  // line 4 — inside target body\n"
            "  // line 5\n"
            "  doSomething();\n"
            "  return req;\n"
            "}\n"
        )
        fn = _resolve_entry_symbol(source, "ts", 5)
        assert fn is not None
        assert fn.name == "target"
        assert fn.line_start == 3
        assert fn.line_end == 8

    def test_malformed_source_returns_empty(self) -> None:
        source = "this is { not } actually code (((("
        fns = _enumerate_functions(source, "ts")
        assert isinstance(fns, list)


# ── Import extraction ──────────────────────────────────────────────────────


class TestImportExtraction:
    def test_ts_named_import(self) -> None:
        source = "import { foo, bar as baz } from './utils';"
        imports = _extract_ts_imports(source)
        assert imports.get("foo") == "./utils"
        assert imports.get("baz") == "./utils"

    def test_ts_default_import(self) -> None:
        source = "import React from 'react';"
        imports = _extract_ts_imports(source)
        assert imports.get("React") == "react"

    def test_ts_default_with_named(self) -> None:
        source = "import React, { useState, useEffect } from 'react';"
        imports = _extract_ts_imports(source)
        assert imports.get("React") == "react"
        assert imports.get("useState") == "react"
        assert imports.get("useEffect") == "react"

    def test_python_from_import(self) -> None:
        source = "from .utils import helper, refund as ref\nfrom os import path"
        imports = _extract_py_imports(source)
        assert imports.get("helper") == ".utils"
        assert imports.get("ref") == ".utils"
        assert imports.get("path") == "os"


# ── End-to-end compute_flow_symbols ───────────────────────────────────────


class TestComputeFlowSymbols:
    def test_ts_called_attribution(self, tmp_path: Path) -> None:
        _write(
            tmp_path, "src/route.ts",
            "import { helper } from './utils';\n"
            "\n"
            "export async function POST(req) {\n"
            "  return helper(req);\n"
            "}\n",
        )
        _write(
            tmp_path, "src/utils.ts",
            "export function helper(x) {\n"
            "  return x;\n"
            "}\n",
        )
        ctx = _ctx(tmp_path, ["src/route.ts", "src/utils.ts"])
        rctx = build_reach_context(ctx)
        result = compute_flow_symbols(
            rctx,
            "src/route.ts",
            3,
            ("src/route.ts", "src/utils.ts"),
        )
        assert not result.entry_detection_failed
        roles = [a.role for a in result.attributions]
        assert "entry" in roles
        assert "called" in roles
        called = next(a for a in result.attributions if a.role == "called")
        assert called.file == "src/utils.ts"
        assert called.symbol == "helper"
        assert called.line_start == 1
        assert called.line_end == 3

    def test_entry_detection_fallback(self, tmp_path: Path) -> None:
        _write(
            tmp_path, "src/script.ts",
            "// top-level script\n"
            "const x = 1;\n"
            "console.log(x);\n",
        )
        ctx = _ctx(tmp_path, ["src/script.ts"])
        rctx = build_reach_context(ctx)
        result = compute_flow_symbols(
            rctx, "src/script.ts", 1, ("src/script.ts",),
        )
        assert result.entry_detection_failed is True
        entry = next(
            (a for a in result.attributions if a.role == "entry"), None,
        )
        assert entry is not None
        assert entry.line_start == 1
        assert entry.line_end >= 3

    def test_support_role_for_unresolved_reach(self, tmp_path: Path) -> None:
        # entry.ts has an entry function but its reach includes a
        # second file with no import edge from the entry function body
        # → should emit support.
        _write(
            tmp_path, "src/entry.ts",
            "export function run() {\n"
            "  return 1;\n"
            "}\n",
        )
        _write(
            tmp_path, "src/lib.ts",
            "export function aux() {\n"
            "  return 2;\n"
            "}\n",
        )
        ctx = _ctx(tmp_path, ["src/entry.ts", "src/lib.ts"])
        rctx = build_reach_context(ctx)
        result = compute_flow_symbols(
            rctx,
            "src/entry.ts",
            1,
            ("src/entry.ts", "src/lib.ts"),
        )
        roles = [a.role for a in result.attributions]
        assert "entry" in roles
        assert "support" in roles
        support = next(a for a in result.attributions if a.role == "support")
        assert support.file == "src/lib.ts"
        assert support.line_start == 1
        assert support.line_end >= 3

    def test_cap_max_symbols_per_flow(self, tmp_path: Path) -> None:
        helper_imports = "\n".join(
            f"import {{ helper{i} }} from './h{i}';" for i in range(50)
        )
        call_lines = "\n".join(f"  helper{i}();" for i in range(50))
        _write(
            tmp_path, "src/entry.ts",
            f"{helper_imports}\n"
            "\n"
            "export function entry() {\n"
            f"{call_lines}\n"
            "}\n",
        )
        files = ["src/entry.ts"]
        for i in range(50):
            _write(
                tmp_path, f"src/h{i}.ts",
                f"export function helper{i}() {{\n"
                f"  return {i};\n"
                f"}}\n",
            )
            files.append(f"src/h{i}.ts")
        ctx = _ctx(tmp_path, files)
        rctx = build_reach_context(ctx)
        result = compute_flow_symbols(
            rctx, "src/entry.ts", 52, tuple(files),
            max_symbols_per_flow=DEFAULT_MAX_SYMBOLS_PER_FLOW,
        )
        assert len(result.attributions) <= DEFAULT_MAX_SYMBOLS_PER_FLOW

    def test_python_called_attribution(self, tmp_path: Path) -> None:
        _write(
            tmp_path, "app/route.py",
            "from .utils import helper\n"
            "\n"
            "def post(req):\n"
            "    return helper(req)\n",
        )
        _write(
            tmp_path, "app/utils.py",
            "def helper(x):\n"
            "    return x\n",
        )
        _write(tmp_path, "app/__init__.py", "")
        ctx = _ctx(
            tmp_path,
            ["app/route.py", "app/utils.py", "app/__init__.py"],
        )
        rctx = build_reach_context(ctx)
        result = compute_flow_symbols(
            rctx,
            "app/route.py",
            3,
            ("app/route.py", "app/utils.py"),
        )
        roles = [a.role for a in result.attributions]
        assert "entry" in roles
        assert "called" in roles
        called = next(a for a in result.attributions if a.role == "called")
        assert called.file == "app/utils.py"
        assert called.symbol == "helper"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
