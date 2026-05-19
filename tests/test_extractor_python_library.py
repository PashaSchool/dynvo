"""PythonLibraryExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.python_library import (
    PythonLibraryExtractor,
    _parse_init_exports,
)


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "python-library",
    stack: str | None = "python",
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack=audited_stack,
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=0.9,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── parser helpers ─────────────────────────────────────────────────────────


def test_parse_init_simple_relative_imports() -> None:
    src = """
from .routing import APIRouter, APIRoute
from .dependencies import Depends
from .openapi.docs import get_swagger_ui_html
""".strip()
    subs, syms = _parse_init_exports(src)
    assert "routing" in subs
    assert "dependencies" in subs
    assert "openapi" in subs
    assert {"APIRouter", "APIRoute", "Depends"}.issubset(syms)


def test_parse_init_dunder_all() -> None:
    src = """
__all__ = [
    "foo", "bar",
    'baz',
]
""".strip()
    _, syms = _parse_init_exports(src)
    assert {"foo", "bar", "baz"}.issubset(syms)


def test_parse_init_paren_form() -> None:
    src = """
from .routing import (
    APIRouter,
    APIRoute,
    Mount,
)
""".strip()
    subs, syms = _parse_init_exports(src)
    assert "routing" in subs
    assert {"APIRouter", "APIRoute", "Mount"}.issubset(syms)


# ── extractor ──────────────────────────────────────────────────────────────


def test_emits_submodule_anchor_for_fastapi_like_layout(tmp_path: Path) -> None:
    """Simulates fastapi/__init__.py re-exporting from submodules."""
    _write(
        tmp_path / "fastapi" / "__init__.py",
        """
from .routing import APIRouter, APIRoute
from .dependencies import Depends
""".strip(),
    )
    _write(tmp_path / "fastapi" / "routing" / "__init__.py", "")
    _write(tmp_path / "fastapi" / "routing" / "routes.py", "class APIRouter: pass")
    _write(tmp_path / "fastapi" / "dependencies" / "__init__.py", "")
    _write(tmp_path / "fastapi" / "dependencies" / "models.py", "def Depends(): ...")
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "fastapi"\n',
    )

    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "pyproject.toml",
            "fastapi/__init__.py",
            "fastapi/routing/__init__.py",
            "fastapi/routing/routes.py",
            "fastapi/dependencies/__init__.py",
            "fastapi/dependencies/models.py",
        ],
    )
    cands = PythonLibraryExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"routing", "dependencies"}.issubset(names)
    for c in cands:
        assert c.source == "python-library"


def test_emits_symbol_anchor_for_dunder_all_without_submodule(tmp_path: Path) -> None:
    _write(
        tmp_path / "mylib" / "__init__.py",
        '__all__ = ["public_helper"]\n',
    )
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mylib"\n')
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["mylib/__init__.py", "pyproject.toml"],
    )
    cands = PythonLibraryExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "public-helper" in names


def test_skips_when_app_marker_present(tmp_path: Path) -> None:
    """A repo with manage.py at the root is a Django APP, not a
    library; the extractor must stay silent."""
    _write(tmp_path / "manage.py", "import django\n")
    _write(tmp_path / "myapp" / "__init__.py", "")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["manage.py", "myapp/__init__.py"],
        audited_stack="django",
        stack="django",
    )
    cands = PythonLibraryExtractor().extract(ctx)
    assert cands == []


def test_skips_when_fastapi_app_call_present(tmp_path: Path) -> None:
    """A `main.py` that calls `app = FastAPI()` is an APP, not the
    framework. Extractor stays silent."""
    _write(tmp_path / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write(tmp_path / "myapp" / "__init__.py", "")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["main.py", "myapp/__init__.py"],
        audited_stack="fastapi",
        stack="fastapi",
    )
    cands = PythonLibraryExtractor().extract(ctx)
    assert cands == []


def test_skips_on_non_python_stack(tmp_path: Path) -> None:
    _write(tmp_path / "mylib" / "__init__.py", "from .x import Y")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["mylib/__init__.py"],
        audited_stack="rust-workspace",
        stack="rust",
    )
    cands = PythonLibraryExtractor().extract(ctx)
    assert cands == []
