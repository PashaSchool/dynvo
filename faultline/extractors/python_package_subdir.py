"""Python package sub-directory extractor (Sprint 6 / Phase 5 Layer C).

For Python LIBRARIES (apprise, structlog, click-style framework
libs), the dominant feature-map shape is "the main package contains
a handful of named sub-packages, each one a horizontal capability".

  apprise/
    plugins/        ← Plugin Extensibility
    config/         ← Configuration Files
    attachment/     ← File Attachments
    i18n/           ← Internationalization
    utils/          ← (tooling, skipped)
    assets/         ← (tooling, skipped)

The primary scan, optimised for SaaS apps with file-system routing,
under-detects this shape — apprise baseline finds 3 features for
11 ground-truth concepts. This extractor emits one signal per
named sub-package so the recall critique can re-surface them.

Detection signature (repo-agnostic per
``memory/rule-no-repo-specific-paths``):

  - Find every ``__init__.py``. Its parent dir is a Python package.
  - For each package, find direct child dirs that ALSO contain
    ``__init__.py`` (sub-packages).
  - Skip well-known tooling subdirs (``utils``, ``helpers``,
    ``assets``, ``vendor``, ``internal``, ``_internal``, ``tests``).
  - Emit one ``python-subpackage`` signal per surviving sub-package
    with its parent package and a sample of contained module names.

The extractor is structural — works on any Python package layout,
not just apprise.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


# Sub-package names that are universally tooling/internal across
# Python libraries. Never product features.
_TOOLING_SUBPKG_NAMES = frozenset({
    "utils", "util", "helpers", "helper",
    "internal", "_internal", "core_utils",
    "assets", "vendor", "_vendor", "third_party", "_third_party",
    "tests", "test", "spec", "specs", "fixtures",
    "examples", "example", "demo", "demos",
    "scripts", "_scripts",
    "_compat", "compat",
    "types", "_types", "typing",
    "constants", "_constants",
    "errors", "exceptions", "_errors",
    "logging", "_logging", "logger",
})

_SKIP_PARENT_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", ".turbo", ".venv", "venv", "env", "site-packages",
    "tests", "test", "spec", "specs", "fixtures",
    "examples", "example", "demo", "demos",
    # Docs-as-tested-code conventions: FastAPI/Pydantic/SQLAlchemy
    # all maintain runnable code samples under these paths. Each
    # snippet is its own throwaway sub-package — never product
    # features. Universal across the Python docs-toolchain ecosystem.
    "docs_src", "samples", "cookbook", "tutorial", "tutorials",
    "snippets", "code_samples",
})


@dataclass(frozen=True, slots=True, kw_only=True)
class PythonSubpackage:
    parent_package: str   # repo-relative, e.g. "apprise"
    name: str             # sub-package name, e.g. "config"
    file: str             # repo-relative dir, e.g. "apprise/config"
    module_count: int     # how many .py files inside (excluding __init__)
    sample_modules: tuple[str, ...]


def _is_python_package(d: Path) -> bool:
    return (d / "__init__.py").is_file()


def _walk_dirs(repo_root: Path) -> Iterable[Path]:
    for p in repo_root.rglob("*"):
        if not p.is_dir():
            continue
        if any(part in _SKIP_PARENT_DIR_NAMES for part in p.parts):
            continue
        yield p


def _module_summary(d: Path) -> tuple[int, list[str]]:
    """Return (count of .py modules excluding __init__, sample names)."""
    names = []
    for child in d.iterdir():
        if child.is_file() and child.suffix == ".py" and child.stem != "__init__":
            names.append(child.stem)
    names.sort()
    return len(names), names[:8]


def collect_python_subpackages(repo_root: Path) -> list[PythonSubpackage]:
    out: list[PythonSubpackage] = []
    for d in _walk_dirs(repo_root):
        if not _is_python_package(d):
            continue
        # Look for direct child sub-packages.
        for child in d.iterdir():
            if not child.is_dir():
                continue
            if not _is_python_package(child):
                continue
            if child.name.lower() in _TOOLING_SUBPKG_NAMES:
                continue
            if child.name.startswith("_"):
                # Underscore-prefixed sub-packages are private/internal.
                continue
            count, sample = _module_summary(child)
            rel = str(child.relative_to(repo_root))
            parent_rel = str(d.relative_to(repo_root))
            out.append(PythonSubpackage(
                parent_package=parent_rel,
                name=child.name,
                file=rel,
                module_count=count,
                sample_modules=tuple(sample),
            ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class PythonPackageSubdirExtractor:
    """Universal Python sub-package extractor."""

    name: str = "python-package-subdir-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # At least one Python package in the repo.
        for d in _walk_dirs(repo_root):
            if _is_python_package(d):
                return True
        return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        return [
            Signal(
                kind="python-subpackage",
                source=self.name,
                payload={
                    "parent_package": s.parent_package,
                    "name": s.name,
                    "file": s.file,
                    "module_count": s.module_count,
                    "sample_modules": s.sample_modules,
                },
            )
            for s in collect_python_subpackages(repo_root)
        ]


__all__ = [
    "PythonPackageSubdirExtractor",
    "PythonSubpackage",
    "collect_python_subpackages",
]
