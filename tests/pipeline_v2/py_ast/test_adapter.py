"""py_ast M4 adapter — graph assembly, provenance view, kill-switch."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.py_ast import adapter, parse


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_PY_AST", "1")
    parse.reset_state()
    adapter.reset_py_ast_state()


def _write(tmp: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


_REPO = {
    "employee/__init__.py": "",
    "employee/models.py": "class Employee:\n    def save(self): pass\n",
    "employee/views.py": (
        "from employee.models import Employee\n"
        "import django\n"
        "def profile(request):\n"
        "    return Employee()\n"
    ),
    "payroll/models.py": "class Payslip: pass\n",
    "payroll/views.py": "from payroll.models import Payslip\n",
}


def _build(tmp: Path):
    fns = adapter._load_real_fns()
    assert fns is not None
    return adapter.build_symbol_graph(
        str(tmp), sorted(_REPO),
        parse_fn=fns.parse_fn, defs_fn=fns.defs_fn,
        imports_fn=fns.imports_fn, resolve_fn=fns.resolve_fn,
    )


def test_graph_telemetry_and_resolution(tmp_path: Path) -> None:
    _write(tmp_path, _REPO)
    g = _build(tmp_path)
    t = g.telemetry
    assert t["lang"] == "py"
    assert t["files_parsed"] == len(_REPO)
    assert t["parse_failures"] == 0
    # employee.models resolves in-repo; django is external.
    hist = t["resolution_histogram"]
    assert hist.get("workspace", 0) >= 2
    assert hist.get("package_external", 0) >= 1


def test_provenance_view_resolves_membership(tmp_path: Path) -> None:
    _write(tmp_path, _REPO)
    g = _build(tmp_path)
    view = adapter.provenance_view(g)
    # employee/views.py imports employee.models → resolves to the file.
    assert view.resolve("employee/views.py", "employee.models") \
        == "employee/models.py"
    # external import resolves to None (caller keeps legacy path).
    assert view.resolve("employee/views.py", "django") is None


def test_repo_provenance_and_current(tmp_path: Path) -> None:
    _write(tmp_path, _REPO)
    tracked = sorted(_REPO)
    view = adapter.repo_provenance(str(tmp_path), tracked)
    assert view is not None
    assert view.resolve("payroll/views.py", "payroll.models") \
        == "payroll/models.py"
    # current view is registered for THIS tracked set.
    assert adapter.current_provenance(frozenset(tracked)) is view
    # a different tracked set → None (no stale provenance).
    assert adapter.current_provenance(frozenset({"other.py"})) is None


def test_kill_switch_off_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, _REPO)
    monkeypatch.setenv("FAULTLINE_PY_AST", "0")
    adapter.reset_py_ast_state()
    assert adapter.repo_provenance(str(tmp_path), sorted(_REPO)) is None
    assert adapter.current_provenance(frozenset(_REPO)) is None
    assert adapter.ast_symbol_ranges(
        str(tmp_path), "employee/views.py", None, []) is None
    # parse_file also degrades when the master flag is off.
    assert parse.parse_file("x.py", b"def f(): pass\n") is None


def test_double_build_byte_identical(tmp_path: Path) -> None:
    _write(tmp_path, _REPO)
    g1 = _build(tmp_path)
    parse.reset_state()
    g2 = _build(tmp_path)
    assert g1.to_payload() == g2.to_payload()


def test_ast_symbol_ranges_upgrade(tmp_path: Path) -> None:
    _write(tmp_path, _REPO)
    ranges = adapter.ast_symbol_ranges(
        str(tmp_path), "employee/models.py", None, [])
    assert ranges is not None
    by = {(r.name, getattr(r, "parent", None)): r for r in ranges}
    assert by[("Employee", None)].kind == "class"
    # method carries parent + method kind.
    assert by[("save", "Employee")].kind == "method"


def test_parse_failure_degrades_to_regex(tmp_path: Path) -> None:
    files = dict(_REPO)
    files["broken.py"] = "def f(:\n"  # syntax error
    _write(tmp_path, files)
    fns = adapter._load_real_fns()
    g = adapter.build_symbol_graph(
        str(tmp_path), sorted(files),
        parse_fn=fns.parse_fn, defs_fn=fns.defs_fn,
        imports_fn=fns.imports_fn, resolve_fn=fns.resolve_fn,
    )
    assert "broken.py" in g.telemetry["failed_files"]
    assert g.telemetry["parse_failures"] == 1


def test_non_py_skipped(tmp_path: Path) -> None:
    fns = adapter._load_real_fns()
    g = adapter.build_symbol_graph(
        str(tmp_path), ["a.ts", "b.pyi", "c.md"],
        parse_fn=fns.parse_fn, defs_fn=fns.defs_fn,
        imports_fn=fns.imports_fn, resolve_fn=fns.resolve_fn,
    )
    assert g.telemetry["files_parsed"] == 0
    assert g.telemetry["stub_skipped"] == 1  # b.pyi
