"""py_ast M3 resolve — dotted/relative import edges → repo files."""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.py_ast import imports, parse
from faultline.pipeline_v2.py_ast.resolve import PyResolver, resolve_edges


@pytest.fixture(autouse=True)
def _force_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_PY_AST", "1")
    parse.reset_state()


def _graph_edges(files: dict[str, str]):
    """Parse every file → the combined edge list (order: sorted paths)."""
    edges = []
    for path in sorted(files):
        fp = parse.parse_file(path, files[path].encode("utf-8"))
        assert fp is not None, path
        e, _ = imports.extract_imports(fp)
        edges.extend(e)
    return edges


def _resolve(files: dict[str, str], repo_root: str = "/repo"):
    edges = _graph_edges(files)
    resolved, tele = resolve_edges(edges, None, repo_root, list(files))
    return resolved, tele


def _target(resolved, src, raw):
    hits = [r for r in resolved if r.src_file == src and r.raw_target == raw]
    return hits[0].target_file if hits else "<<missing>>"


def _res(resolved, src, raw):
    hits = [r for r in resolved if r.src_file == src and r.raw_target == raw]
    return hits[0].resolution if hits else "<<missing>>"


def test_flat_absolute_import() -> None:
    files = {
        "employee/models.py": "class Employee: pass\n",
        "employee/views.py": "from employee.models import Employee\n",
    }
    resolved, _ = _resolve(files)
    assert _target(resolved, "employee/views.py", "employee.models") \
        == "employee/models.py"
    assert _res(resolved, "employee/views.py", "employee.models") == "workspace"


def test_relative_sibling_and_parent() -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/a.py": "from .b import X\nfrom ..util import Y\n",
        "pkg/sub/b.py": "X = 1\n",
        "pkg/util.py": "Y = 2\n",
    }
    resolved, _ = _resolve(files)
    assert _target(resolved, "pkg/sub/a.py", ".b") == "pkg/sub/b.py"
    assert _res(resolved, "pkg/sub/a.py", ".b") == "relative"
    assert _target(resolved, "pkg/sub/a.py", "..util") == "pkg/util.py"


def test_package_init_and_submodule_split() -> None:
    files = {
        "app/__init__.py": "",
        "app/models.py": "class M: pass\n",
        "app/consumer.py": "from app import models, helper\n",
        "app/helper.py": "def helper(): pass\n",  # submodule named like attr
    }
    resolved, _ = _resolve(files)
    # ``models`` is a submodule → app/models.py; ``helper`` is submodule too
    rows = [r for r in resolved
            if r.src_file == "app/consumer.py" and r.raw_target == "app"]
    targets = {r.target_file for r in rows}
    assert "app/models.py" in targets
    assert "app/helper.py" in targets


def test_namespace_package_pep420() -> None:
    # No __init__.py anywhere — still resolvable (PEP-420).
    files = {
        "ns/mod.py": "VALUE = 1\n",
        "consumer.py": "from ns.mod import VALUE\n",
    }
    resolved, _ = _resolve(files)
    assert _target(resolved, "consumer.py", "ns.mod") == "ns/mod.py"


def test_src_layout_root() -> None:
    files = {
        "src/proj/__init__.py": "",
        "src/proj/core.py": "def run(): pass\n",
        "src/proj/app.py": "from proj.core import run\n",
    }
    resolved, _ = _resolve(files)
    assert _target(resolved, "src/proj/app.py", "proj.core") \
        == "src/proj/core.py"


def test_data_derived_backend_root() -> None:
    # ``onyx``/``shared`` live under backend/ ; imports use the bare name.
    files = {
        "backend/onyx/__init__.py": "",
        "backend/onyx/db.py": "class DB: pass\n",
        "backend/shared/__init__.py": "",
        "backend/shared/util.py": "def u(): pass\n",
        "backend/main.py": "from onyx.db import DB\nfrom shared.util import u\n",
    }
    resolved, _ = _resolve(files)
    assert _target(resolved, "backend/main.py", "onyx.db") \
        == "backend/onyx/db.py"
    assert _target(resolved, "backend/main.py", "shared.util") \
        == "backend/shared/util.py"


def test_external_is_package_external() -> None:
    files = {
        "app/v.py": "import django\nfrom rest_framework import serializers\n",
    }
    resolved, _ = _resolve(files)
    assert _res(resolved, "app/v.py", "django") == "package_external"
    assert _res(resolved, "app/v.py", "rest_framework") == "package_external"
    assert _target(resolved, "app/v.py", "django") is None


def test_barrel_descent_through_init() -> None:
    files = {
        "lib/__init__.py": "from .core import Engine\n",
        "lib/core.py": "class Engine: pass\n",
        "consumer.py": "from lib import Engine\n",
    }
    resolved, _ = _resolve(files)
    # ``from lib import Engine`` descends the __init__ re-export → core.py
    assert _target(resolved, "consumer.py", "lib") == "lib/core.py"


def test_conservative_root_discovery_no_false_root() -> None:
    # ``base`` is an importable package, never a source root; an external
    # collision must NOT mint it as a root.
    files = {
        "base/__init__.py": "",
        "base/models.py": "class M: pass\n",
        "base/views.py": "from base.models import M\nimport requests\n",
    }
    r = PyResolver("/repo", list(files), _graph_edges(files))
    assert r.roots == [""]


def test_deterministic_resolution() -> None:
    files = {
        "a/__init__.py": "",
        "a/x.py": "from a.y import Z\nfrom .w import Q\n",
        "a/y.py": "Z = 1\n",
        "a/w.py": "Q = 2\n",
    }
    r1, _ = _resolve(files)
    r2, _ = _resolve(files)
    assert [x.to_payload() for x in r1] == [x.to_payload() for x in r2]
