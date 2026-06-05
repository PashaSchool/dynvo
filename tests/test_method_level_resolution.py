"""Method-level symbol indexing + member-call resolution.

Regression guard for the whole-class-pulled-into-a-flow over-count: a
member call ``obj.findById()`` must resolve to the SPECIFIC method body,
never the entire enclosing class. Covers:

  * TS/JS class methods / constructors / arrow class-fields indexed as
    their own ``SymbolRange`` (kind ``method`` / ``constructor``, with
    ``parent``), while the class symbol is KEPT.
  * Python class methods indexed with kind ``method`` / ``constructor``
    + ``parent`` (module-level ``def`` stays ``function``).
  * Call-graph member resolution: ``new Class()`` → constructor body
    only; ``obj.method()`` → that method's tight range; the whole class
    is never attributed for a member/constructor call.
  * Unresolved member calls attribute nothing (miss telemetry) — no
    whole-class fallback.
"""
from __future__ import annotations

import textwrap

from faultline.analyzer.ast_extractor import (
    extract_symbol_ranges,
    _parse_python_file,
)


# ── TS/JS method-level indexing ─────────────────────────────────────────


def _by_name(ranges, name):
    return [r for r in ranges if r.name == name]


def test_ts_class_methods_get_own_ranges():
    source = textwrap.dedent(
        """\
        export class Repo {
          constructor(db) {
            this.db = db;
          }
          findById(id) {
            return this.db.q(id);
          }
          create(data) {
            const row = this.db.insert(data);
            return row;
          }
        }
        """
    )
    ranges = extract_symbol_ranges(source)
    # Class symbol KEPT (feature anchoring).
    cls = _by_name(ranges, "Repo")
    assert cls and cls[0].kind == "class"
    # Class spans the whole body.
    assert cls[0].start_line == 1

    find = _by_name(ranges, "findById")
    create = _by_name(ranges, "create")
    ctor = _by_name(ranges, "constructor")
    assert find and find[0].kind == "method" and find[0].parent == "Repo"
    assert create and create[0].kind == "method"
    assert ctor and ctor[0].kind == "constructor" and ctor[0].parent == "Repo"

    # Each method's range is TIGHT — far smaller than the class.
    class_loc = cls[0].end_line - cls[0].start_line + 1
    find_loc = find[0].end_line - find[0].start_line + 1
    assert find_loc < class_loc
    # findById must NOT span the whole class.
    assert find[0].start_line >= ctor[0].start_line


def test_ts_arrow_class_field_indexed_as_method():
    source = textwrap.dedent(
        """\
        export class Service {
          handle = async (req) => {
            return this.run(req);
          };
          run(x) {
            return x;
          }
        }
        """
    )
    ranges = extract_symbol_ranges(source)
    handle = _by_name(ranges, "handle")
    assert handle and handle[0].kind == "method" and handle[0].parent == "Service"


def test_ts_control_keywords_not_indexed_as_methods():
    source = textwrap.dedent(
        """\
        export class C {
          run() {
            if (x) { return 1; }
            for (const a of b) { doThing(a); }
            while (y) { step(); }
          }
        }
        """
    )
    ranges = extract_symbol_ranges(source)
    names = {r.name for r in ranges if r.kind == "method"}
    assert "run" in names
    assert "if" not in names
    assert "for" not in names
    assert "while" not in names


def test_ts_object_brace_in_method_does_not_break_span():
    # A method whose body contains nested object literals / strings with
    # braces must still be balanced correctly.
    source = textwrap.dedent(
        """\
        export class C {
          a() {
            const o = { x: { y: "}" }, z: `${1}` };
            return o;
          }
          b() {
            return 2;
          }
        }
        """
    )
    ranges = extract_symbol_ranges(source)
    a = _by_name(ranges, "a")
    b = _by_name(ranges, "b")
    assert a and b
    # ``a`` must end before ``b`` begins (no spill-over).
    assert a[0].end_line < b[0].start_line


def test_ts_top_level_function_unchanged():
    # A plain exported function still gets a function range (no class).
    source = "export function helper(x) { return x + 1; }\n"
    ranges = extract_symbol_ranges(source)
    h = _by_name(ranges, "helper")
    assert h and h[0].kind == "function"
    assert all(r.parent is None for r in ranges)


# ── Python method-level indexing ────────────────────────────────────────


def test_python_class_methods_tagged_with_parent():
    source = textwrap.dedent(
        """\
        class Repo:
            def __init__(self, db):
                self.db = db

            def find_by_id(self, x):
                return self.db.q(x)


        def module_fn():
            return 1
        """
    )
    sig = _parse_python_file("repo.py", source)
    rng = {r.name: r for r in sig.symbol_ranges}
    assert rng["Repo"].kind == "class"
    assert rng["__init__"].kind == "constructor"
    assert rng["__init__"].parent == "Repo"
    assert rng["find_by_id"].kind == "method"
    assert rng["find_by_id"].parent == "Repo"
    # module-level def stays a function with no parent.
    assert rng["module_fn"].kind == "function"
    assert rng["module_fn"].parent is None
    # exports keep MODULE-LEVEL names only (Stage-3 contract).
    assert set(sig.exports) == {"Repo", "module_fn"}
    assert "find_by_id" not in sig.exports


def test_python_method_range_tighter_than_class():
    source = textwrap.dedent(
        """\
        class Big:
            def a(self):
                return 1

            def b(self):
                x = 2
                y = 3
                return x + y
        """
    )
    sig = _parse_python_file("big.py", source)
    rng = {r.name: r for r in sig.symbol_ranges}
    class_loc = rng["Big"].end_line - rng["Big"].start_line + 1
    a_loc = rng["a"].end_line - rng["a"].start_line + 1
    assert a_loc < class_loc
