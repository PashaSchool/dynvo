"""Go-stack extractors (Sprint 9c).

Three extractors that ground Go libraries the way ``trpc_router`` /
``python_subpackage`` ground TS / Python libraries:

  1. ``GoTopLevelFileExtractor`` — every ``<top>.go`` file at the repo
     root (or under the module root) is a candidate feature anchor.
     Stripped suffixes: ``_test``, ``_internal``. ``main.go`` is
     skipped (entry point, not a feature). Each file's basename is
     the slug.

  2. ``GoSubpackageExtractor`` — every directory containing one or
     more ``.go`` files becomes a candidate feature. Captures the
     directory name as slug (``middleware/logger`` → ``logger``).

  3. ``GoTestFileExtractor`` — every ``<X>_test.go`` file emits a
     test-anchor signal. Mirrors ``test_file.py`` for Go convention.

Generic per ``rule-no-repo-specific-paths`` — folder/file shape rules
only, no per-repo names.

Generic per ``rule-cold-scan`` — pure structural extractors with no
priors or seeds; derive everything from the current code.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from faultline.signals import Signal

logger = logging.getLogger(__name__)


_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".github", "vendor", "testdata",
    "_examples", "examples", ".venv", "venv", "dist", "build",
})

# Generic noise filenames at top level (not features, just scaffolding).
_NOISE_TOP_FILES = frozenset({
    "main", "doc", "init", "version", "constants", "errors",
})


def _walk(repo_root: Path):
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        yield root, dirs, files


def _has_go_files(directory: Path) -> bool:
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.endswith(".go"):
                return True
    except (OSError, PermissionError):
        return False
    return False


def _has_go_mod(repo_root: Path) -> bool:
    return (repo_root / "go.mod").is_file()


# ── 1. Top-level Go file extractor ──────────────────────────────────


class GoTopLevelFileExtractor:
    """Each ``<root>/<X>.go`` (or ``<module>/<X>.go``) is an anchor."""

    name = "go-top-level-file"

    def applicable(self, repo_root: Path) -> bool:
        return _has_go_mod(repo_root) or _has_go_files(repo_root)

    def extract(self, repo_root: Path, files=()) -> list[Signal]:
        _ = files
        out: list[Signal] = []
        seen: set[str] = set()
        try:
            for entry in repo_root.iterdir():
                if not entry.is_file() or not entry.name.endswith(".go"):
                    continue
                stem = entry.stem
                if stem.endswith("_test"):
                    continue
                if stem in _NOISE_TOP_FILES:
                    continue
                if stem in seen:
                    continue
                seen.add(stem)
                out.append(
                    Signal(
                        kind="go-module",
                        source=self.name,
                        payload={"file": entry.name, "slug": stem},
                    ),
                )
        except OSError:
            return []
        return out


# ── 2. Sub-package extractor ────────────────────────────────────────


class GoSubpackageExtractor:
    """Each subdirectory containing .go files is a candidate feature."""

    name = "go-subpackage"

    def applicable(self, repo_root: Path) -> bool:
        return _has_go_mod(repo_root)

    def extract(self, repo_root: Path, files=()) -> list[Signal]:
        _ = files
        out: list[Signal] = []
        seen: set[str] = set()
        for root, dirs, files_in_dir in _walk(repo_root):
            root_path = Path(root)
            if root_path == repo_root:
                continue
            if not any(f.endswith(".go") and not f.endswith("_test.go") for f in files_in_dir):
                continue
            try:
                rel = root_path.relative_to(repo_root)
            except ValueError:
                continue
            slug = rel.parts[-1]
            # Skip Go vendor-style directories. Sprint 9c P2 fix —
            # dedup key is the FULL relative path so ``pkg/auth/`` and
            # ``internal/auth/`` both emit (don't suppress one because
            # they share the leaf folder name).
            if slug.startswith("_"):
                continue
            key = str(rel)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Signal(
                    kind="go-subpackage",
                    source=self.name,
                    payload={
                        "directory": str(rel),
                        "slug": slug,
                        "file_count": sum(
                            1 for f in files_in_dir
                            if f.endswith(".go") and not f.endswith("_test.go")
                        ),
                    },
                ),
            )
        return out


# ── 3. Go test file extractor ───────────────────────────────────────


class GoTestFileExtractor:
    """``<X>_test.go`` → test-anchor signal with slug=<X>."""

    name = "go-test-file"

    def applicable(self, repo_root: Path) -> bool:
        return _has_go_mod(repo_root) or _has_go_files(repo_root)

    def extract(self, repo_root: Path, files=()) -> list[Signal]:
        _ = files
        out: list[Signal] = []
        seen: set[str] = set()
        for root, _dirs, files_in_dir in _walk(repo_root):
            for fn in files_in_dir:
                if not fn.endswith("_test.go"):
                    continue
                stem = fn[: -len("_test.go")]
                if not stem or stem in seen:
                    continue
                seen.add(stem)
                try:
                    rel = str((Path(root) / fn).relative_to(repo_root))
                except ValueError:
                    continue
                out.append(
                    Signal(
                        kind="test-anchor",
                        source=self.name,
                        payload={
                            "file": rel,
                            "slug": stem,
                            "match_kind": "go-test",
                        },
                    ),
                )
        return out


# ── 4. Inside-folder per-file extractor ─────────────────────────────


# Folders that follow "one .go file = one capability" convention in
# Go projects. Generic vocabulary, not repo-specific.
_PER_FILE_FOLDERS = frozenset({
    "middleware", "middlewares", "handlers", "handler",
    "transport", "transports", "adapters", "adapter",
    "providers", "provider", "encoders", "encoder",
    "decoders", "decoder", "drivers", "driver",
    "filters", "filter", "interceptors", "interceptor",
    "plugins", "plugin", "extensions", "extension",
})


class GoPerFileFolderExtractor:
    """Inside ``middleware/``, ``handlers/``, etc. each ``<X>.go`` is
    its own capability (chi: ``middleware/basic_auth.go`` is BasicAuth,
    a separate feature from Heartbeat in ``middleware/heartbeat.go``).

    The folder-level ``GoSubpackageExtractor`` captures the umbrella
    package as ONE feature; this extractor recurses one level deeper
    into the canonical capability folders to surface each file as a
    candidate feature.
    """

    name = "go-per-file-folder"

    def applicable(self, repo_root: Path) -> bool:
        return _has_go_mod(repo_root) or _has_go_files(repo_root)

    def extract(self, repo_root: Path, files=()) -> list[Signal]:
        _ = files
        out: list[Signal] = []
        seen: set[str] = set()
        for root, _dirs, files_in_dir in _walk(repo_root):
            root_path = Path(root)
            try:
                rel = root_path.relative_to(repo_root)
            except ValueError:
                continue
            # Folder name must be in the per-file-convention list
            folder_name = rel.parts[-1] if rel.parts else ""
            if folder_name not in _PER_FILE_FOLDERS:
                continue
            for fn in files_in_dir:
                if not fn.endswith(".go") or fn.endswith("_test.go"):
                    continue
                stem = fn[:-3]
                if not stem or stem in _NOISE_TOP_FILES:
                    continue
                key = f"{folder_name}/{stem}".lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    Signal(
                        kind="go-per-file",
                        source=self.name,
                        payload={
                            "file": str(rel / fn),
                            "slug": stem,
                            "folder": folder_name,
                        },
                    ),
                )
        return out


__all__ = [
    "GoPerFileFolderExtractor",
    "GoSubpackageExtractor",
    "GoTestFileExtractor",
    "GoTopLevelFileExtractor",
]
