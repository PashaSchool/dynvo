"""Server Actions extractor (Sprint 3 / Phase 5 Layer C).

Detects Next.js / React Server Components Server Actions — async
functions tagged with the ``"use server"`` directive that the
Next runtime exposes as RPC endpoints. Each one is a server-side
mutation = a customer-facing operation.

Two declaration shapes are recognised:

  1. **File-level directive**: file starts with ``"use server"`` (or
     ``'use server'``). EVERY exported async function in the file is
     a Server Action.

  2. **Inline directive**: an exported async function whose body
     starts with ``"use server"``. Just that function is a Server
     Action.

The extractor groups actions per FILE and emits one signal per file
(not per action) — ground-truth feature lists describe domains, not
individual operations. Signal payload carries up to 8 sample action
names so the recall critique has enough context.

Generic per ``memory/rule-no-repo-specific-paths`` — no per-repo
filenames hardcoded; works on any Next.js / React Server Components
codebase.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


_SOURCE_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs"})

_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", ".turbo", ".venv", "venv", "env",
    "tests", "test", "spec", "specs", "fixtures", "stories",
    "storybook-static", ".storybook", "e2e", "__tests__",
})

# File-level directive: must be at the top of the file (after
# optional comments / whitespace) for Next.js to honour it. We're
# looser — any of the first 5 non-blank lines counts — because some
# codebases prefix with imports then add the directive.
_FILE_DIRECTIVE_RE = re.compile(
    r"""^\s*(?:["']use\s+server["']\s*;?)\s*$""",
    re.MULTILINE,
)

# Exported async function declaration:
#   export async function NAME(...)
#   export const NAME = async (...) => ...
#   export const NAME = async function (...) {...}
#   export const NAME : Type = async ...
_EXPORT_FN_RE = re.compile(
    r"""
    \bexport\s+
    (?:
        async\s+function\s+(?P<n1>[A-Za-z_$][A-Za-z0-9_$]*)
      | (?:const|let|var)\s+(?P<n2>[A-Za-z_$][A-Za-z0-9_$]*)
        (?:\s*:\s*[^=]+?)?      # optional type annotation
        \s*=\s*async\b
    )
    """,
    re.VERBOSE,
)

# Inline directive: function body starts with the directive.
_INLINE_FN_RE = re.compile(
    r"""
    \bexport\s+(?:async\s+function\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)
                 | (?:const|let|var)\s+(?P<name2>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*async)
    [^{]*?\{\s*
    (?:["']use\s+server["']\s*;?)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerActionFile:
    file: str                  # repo-relative
    file_level_directive: bool # True iff the directive is at file scope
    action_names: tuple[str, ...]  # exported async function names (deduped)


def _walkable_files(repo_root: Path) -> Iterable[Path]:
    for p in repo_root.rglob("*"):
        if not p.is_file() or p.suffix not in _SOURCE_EXTS:
            continue
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _has_file_directive(text: str) -> bool:
    """True iff the very first non-blank, non-comment, non-import
    line is ``"use server"`` (or single-quoted variant). Next.js
    requires the directive at file scope; anything inside a
    function body is a function-level directive instead.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("//", "/*", "*")):
            continue
        # Imports are allowed before the directive.
        if line.startswith(("import ", "import{", 'import"', "import'")):
            continue
        # Bare directive line (with optional semicolon).
        if line in ('"use server"', "'use server'",
                    '"use server";', "'use server';"):
            return True
        return False
    return False


def _extract_exported_names(text: str) -> list[str]:
    """All exported async function names in the file."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _EXPORT_FN_RE.finditer(text):
        name = m.group("n1") or m.group("n2")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _extract_inline_action_names(text: str) -> list[str]:
    """Names of exported async functions whose body starts with the
    directive (function-level marker, not file-level).
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _INLINE_FN_RE.finditer(text):
        name = m.group("name") or m.group("name2")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def collect_server_action_files(repo_root: Path) -> list[ServerActionFile]:
    out: list[ServerActionFile] = []
    for p in _walkable_files(repo_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Cheap pre-filter: the directive string must appear somewhere.
        if "use server" not in text:
            continue

        if _has_file_directive(text):
            names = _extract_exported_names(text)
            if not names:
                continue
            out.append(ServerActionFile(
                file=str(p.relative_to(repo_root)),
                file_level_directive=True,
                action_names=tuple(names),
            ))
        else:
            names = _extract_inline_action_names(text)
            if not names:
                continue
            out.append(ServerActionFile(
                file=str(p.relative_to(repo_root)),
                file_level_directive=False,
                action_names=tuple(names),
            ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerActionsExtractor:
    """Universal Server Actions extractor for Next.js / RSC repos."""

    name: str = "server-actions-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # Cheap check — walk a few files looking for the marker.
        # We don't run the full collector here; if even one file
        # has "use server" string, the extractor's worth running.
        for p in _walkable_files(repo_root):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "use server" in text:
                return True
        return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        return [
            Signal(
                kind="server-actions-file",
                source=self.name,
                payload={
                    "file": f.file,
                    "file_level_directive": f.file_level_directive,
                    "action_count": len(f.action_names),
                    "sample_names": f.action_names[:8],
                },
            )
            for f in collect_server_action_files(repo_root)
        ]


__all__ = [
    "ServerActionFile",
    "ServerActionsExtractor",
    "collect_server_action_files",
]
