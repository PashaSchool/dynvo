"""py_ast M2 imports — import/re-export edges (raw, pre-resolution)."""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.py_ast import imports, parse


@pytest.fixture(autouse=True)
def _force_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_PY_AST", "1")
    parse.reset_state()


def _edges(src: str, path: str = "m.py"):
    fp = parse.parse_file(path, src.encode("utf-8"))
    assert fp is not None
    return imports.extract_imports(fp)


def _find(edges, raw):
    return [e for e in edges if e.raw_target == raw]


def test_plain_and_dotted_import() -> None:
    edges, _ = _edges("import os\nimport a.b.c\nimport a.b as x\n")
    assert _find(edges, "os")[0].names == ("os",)
    # ``import a.b.c`` binds top component ``a``; target keeps full path.
    assert _find(edges, "a.b.c")[0].names == ("a",)
    assert _find(edges, "a.b")[0].names == ("x",)
    assert all(e.kind == "named" for e in edges)


def test_multi_import_splits_per_alias() -> None:
    edges, _ = _edges("import a, b\n")
    assert {e.raw_target for e in edges} == {"a", "b"}


def test_from_import_named_sorted() -> None:
    edges, _ = _edges("from pkg.mod import Zeta, Alpha, Beta\n")
    e = _find(edges, "pkg.mod")[0]
    assert e.names == ("Alpha", "Beta", "Zeta")
    assert e.kind == "named"


def test_from_import_rename_kept_as_pair() -> None:
    edges, _ = _edges("from m import Original as Local\n")
    assert _find(edges, "m")[0].names == ("Original as Local",)


def test_relative_levels_encoded_as_dots() -> None:
    edges, _ = _edges(
        "from . import a\n"
        "from .mod import b\n"
        "from ..pkg import c\n"
        "from ...deep.mod import d\n",
        path="pkg/sub/f.py",
    )
    assert _find(edges, ".")[0].names == ("a",)
    assert _find(edges, ".mod")[0].names == ("b",)
    assert _find(edges, "..pkg")[0].names == ("c",)
    assert _find(edges, "...deep.mod")[0].names == ("d",)


def test_star_import_is_reexport_star() -> None:
    edges, _ = _edges("from m import *\n")
    e = _find(edges, "m")[0]
    assert e.kind == "reexport_star"
    assert e.names == ("*",)


def test_init_reexports_become_export_entries() -> None:
    edges, exports = _edges(
        "from .views import Home, About as Info\n"
        "from .models import *\n",
        path="pkg/__init__.py",
    )
    names = {(x.name, x.kind) for x in exports}
    assert ("Home", "named") in names
    assert ("Info", "named") in names  # published name is the local (asname)
    assert ("*", "star_from") in names


def test_non_init_file_has_no_export_entries() -> None:
    _edges_out, exports = _edges("from .views import Home\n", path="pkg/x.py")
    assert exports == []


def test_nested_import_captured() -> None:
    edges, _ = _edges(
        "def f():\n"
        "    from lazy import thing\n"
        "    return thing\n"
    )
    assert _find(edges, "lazy")


def test_deterministic_sorted() -> None:
    src = "import b\nimport a\nfrom z import Y, X\n"
    e1, _ = _edges(src)
    e2, _ = _edges(src)
    assert [x.to_payload() for x in e1] == [x.to_payload() for x in e2]
    keys = [(e.src_file, e.line, e.kind, e.raw_target, e.names) for e in e1]
    assert keys == sorted(keys)
