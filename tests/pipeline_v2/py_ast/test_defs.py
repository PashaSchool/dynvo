"""py_ast M1 defs — symbol definitions with exact line ranges."""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.py_ast import defs, parse


@pytest.fixture(autouse=True)
def _force_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_PY_AST", "1")
    monkeypatch.delenv("FAULTLINE_PY_AST_ENTRY", raising=False)
    parse.reset_state()


def _defs(src: str, path: str = "m.py"):
    fp = parse.parse_file(path, src.encode("utf-8"))
    assert fp is not None
    return defs.extract_defs(fp)


def _by_name(rows):
    return {(d.name, d.parent): d for d in rows}


def test_module_function_class_are_exported() -> None:
    rows = _defs(
        "def handler():\n    return 1\n\n\nclass Widget:\n    pass\n"
    )
    idx = _by_name(rows)
    assert idx[("handler", None)].kind == "function"
    assert idx[("handler", None)].exported is True
    assert idx[("handler", None)].start_line == 1
    assert idx[("handler", None)].end_line == 2
    assert idx[("Widget", None)].kind == "class"
    assert idx[("Widget", None)].exported is True


def test_methods_carry_parent_and_are_not_exported() -> None:
    rows = _defs(
        "class Repo:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "    def find(self, i):\n"
        "        return i\n"
    )
    idx = _by_name(rows)
    init = idx[("__init__", "Repo")]
    assert init.kind == "method"
    assert init.parent == "Repo"
    assert init.exported is False
    find = idx[("find", "Repo")]
    assert find.kind == "method"
    assert find.start_line == 4 and find.end_line == 5


def test_decorated_span_includes_decorators() -> None:
    rows = _defs(
        "@app.route('/x')\n"
        "@login_required\n"
        "def view(request):\n"
        "    return 1\n"
    )
    view = _by_name(rows)[("view", None)]
    # span begins at the FIRST decorator, not the ``def`` line.
    assert view.start_line == 1
    assert view.end_line == 4


def test_async_function() -> None:
    rows = _defs("async def fetch():\n    return await go()\n")
    assert _by_name(rows)[("fetch", None)].kind == "function"


def test_module_const_bindings() -> None:
    rows = _defs(
        "URL = '/x'\n"
        "router = Router()\n"
        "a = b = 2\n"
        "x, y = 1, 2\n"
        "obj.attr = 3\n"          # attribute target → NOT a module symbol
        "typed: int = 5\n"
        "bare: int\n"            # annotation only, no value → no binding
    )
    idx = _by_name(rows)
    for name in ("URL", "router", "a", "b", "x", "y", "typed"):
        assert (name, None) in idx, name
        assert idx[(name, None)].kind == "const"
        assert idx[(name, None)].exported is True
    assert ("attr", None) not in idx
    assert ("bare", None) not in idx


def test_nested_function_is_local_not_exported() -> None:
    rows = _defs(
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner\n"
    )
    idx = _by_name(rows)
    assert idx[("outer", None)].exported is True
    assert idx[("inner", None)].kind == "function"
    assert idx[("inner", None)].exported is False


def test_conditional_module_level_def_is_exported() -> None:
    rows = _defs(
        "try:\n"
        "    from fast import View\n"
        "except ImportError:\n"
        "    class View:\n"
        "        pass\n"
    )
    view = _by_name(rows).get(("View", None))
    assert view is not None and view.exported is True


def test_output_is_sorted_and_deterministic() -> None:
    src = (
        "class B:\n    def m(self): pass\n"
        "def a(): pass\n"
        "Z = 1\n"
    )
    r1 = _defs(src)
    r2 = _defs(src)
    keys = [(d.file, d.start_line, d.end_line, d.parent or "", d.name)
            for d in r1]
    assert keys == sorted(keys)
    assert [d.to_payload() for d in r1] == [d.to_payload() for d in r2]
