"""Python framework top-level module extractor (Sprint Phase 2).

Sister to ``python_package_subdir`` for the specific case where the
repo IS the framework (``fastapi/`` repo ships the ``fastapi/``
package; same for flask, starlette, django, sanic, tornado).

The sub-package extractor already covers ``fastapi/dependencies``,
``fastapi/openapi``, etc. — but real frameworks ALSO ship important
capabilities as top-level FILES, not sub-packages:

  fastapi/routing.py        → Routing / Path Operation Decorators
  fastapi/websockets.py     → WebSocket Support
  fastapi/background.py     → Background Tasks
  fastapi/responses.py      → Custom Response Classes
  fastapi/exceptions.py     → Error Handling
  fastapi/staticfiles.py    → Static Files
  fastapi/security.py (if file) or fastapi/security/ (if dir, covered)

  flask/blueprints.py       → Blueprints
  flask/views.py            → Class-based views
  flask/cli.py              → Flask CLI
  flask/sessions.py         → Sessions

Each meaningful (≥ MIN_MEANINGFUL_LOC) top-level ``.py`` module under
the framework's own package dir becomes a candidate feature. Private
(underscore-prefixed) modules are skipped.

Activation:
  Only emit signals when the repo looks like a "self-shipped"
  Python web framework — pyproject ``[project].name`` matches a
  top-level dir name that itself contains an ``__init__.py``. We
  don't auto-detect "is this a framework" here; the calling code
  (pipeline / classifier) decides whether to consume these signals.
  The extractor stays universal — it emits the candidates whenever
  the structural pattern exists, callers filter by classification.

Per ``memory/rule-no-repo-specific-paths``: works on ANY Python repo
whose layout matches the structural pattern, never hardcodes fastapi
/ flask file names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


# Threshold below which a top-level .py module is treated as a
# helper/glue file rather than a feature surface. Scale-invariant
# (counts non-blank, non-comment, non-docstring lines).
MIN_MEANINGFUL_LOC = 20

# Module names that are universally scaffolding/utility regardless of
# package — never product features.
#
# NB: ``exceptions`` and ``errors`` are NOT in the tooling list when
# we're scanning a framework repo. In FastAPI/Flask/Django, the
# ``exceptions.py`` module IS the Error Handling feature surface
# (defines public exception classes the framework user catches /
# raises). Treating it as scaffolding would lose a real capability.
_TOOLING_MODULE_NAMES = frozenset({
    "utils", "util", "helpers", "helper",
    "constants", "_constants", "consts",
    "version", "_version", "__version__",
    "types", "_types", "typing",
    "compat", "_compat",
    "_internal",
    "py", "asyncio_imports",
})


def _is_python_package_dir(d: Path) -> bool:
    return d.is_dir() and (d / "__init__.py").is_file()


def _meaningful_loc(path: Path) -> int:
    """Count lines that aren't blank, comment, or pure-docstring fences.

    Cheap heuristic; not AST-perfect but good enough to separate a
    300-LOC ``routing.py`` from a 4-line ``__about__.py``.
    """
    count = 0
    in_docstring = False
    docstring_quote = None
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            if in_docstring:
                if docstring_quote in line:
                    in_docstring = False
                continue
            if line.startswith("#"):
                continue
            if line.startswith('"""') or line.startswith("'''"):
                quote = line[:3]
                rest = line[3:]
                # Single-line docstring closes on same line
                if quote in rest:
                    continue
                in_docstring = True
                docstring_quote = quote
                continue
            count += 1
    except Exception:
        return 0
    return count


def _find_self_framework_package(repo_root: Path) -> Path | None:
    """Locate the package dir whose name matches the pyproject project name.

    Supports both ``<repo>/<pkg>/`` and src-layout ``<repo>/src/<pkg>/``.
    Returns None if no pyproject ``project.name`` or no matching package
    dir. Case-insensitive on the package name (flask vs Flask).
    """
    py = repo_root / "pyproject.toml"
    if not py.is_file():
        return None
    text = py.read_text(encoding="utf-8", errors="ignore")
    # Naive but works for the canonical TOML format used by hatch/setuptools.
    m = re.search(r'\[project\][^\[]*?name\s*=\s*"([^"]+)"', text, re.S)
    if not m:
        return None
    name = m.group(1).strip().lower()
    for candidate in (repo_root / name, repo_root / "src" / name):
        if _is_python_package_dir(candidate):
            return candidate
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class PythonModuleCandidate:
    file: str            # repo-relative, e.g. "fastapi/routing.py"
    module: str          # stem, e.g. "routing"
    parent_package: str  # e.g. "fastapi"
    loc: int


def collect_python_framework_modules(
    repo_root: Path,
) -> list[PythonModuleCandidate]:
    pkg_dir = _find_self_framework_package(repo_root)
    if pkg_dir is None:
        return []
    parent = str(pkg_dir.relative_to(repo_root))
    out: list[PythonModuleCandidate] = []
    for child in pkg_dir.iterdir():
        if not child.is_file():
            continue
        if child.suffix != ".py":
            continue
        stem = child.stem
        if stem == "__init__":
            continue
        if stem.startswith("_"):
            continue
        if stem.lower() in _TOOLING_MODULE_NAMES:
            continue
        loc = _meaningful_loc(child)
        if loc < MIN_MEANINGFUL_LOC:
            continue
        rel = str(child.relative_to(repo_root))
        out.append(PythonModuleCandidate(
            file=rel, module=stem, parent_package=parent, loc=loc,
        ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class PythonFrameworkModuleExtractor:
    """Top-level modules of a self-shipped Python web framework."""

    name: str = "python-framework-module-extractor"

    def applicable(self, repo_root: Path) -> bool:
        return _find_self_framework_package(repo_root) is not None

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        return [
            Signal(
                kind="python-framework-module",
                source=self.name,
                payload={
                    "file": c.file,
                    "module": c.module,
                    "parent_package": c.parent_package,
                    "loc": c.loc,
                },
            )
            for c in collect_python_framework_modules(repo_root)
        ]


__all__ = [
    "PythonFrameworkModuleExtractor",
    "PythonModuleCandidate",
    "collect_python_framework_modules",
]
