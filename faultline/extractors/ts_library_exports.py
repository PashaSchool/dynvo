"""TypeScript library exports extractor (Sprint 6 / Phase 5 Layer C).

For TS LIBRARIES (better-auth, hono, drizzle-orm), the
maintainer-stated public API lives in ``index.ts`` files — what
the package re-exports IS what they pitch as features.

  better-auth/packages/better-auth/src/index.ts
    → re-exports betterAuth, ...

  better-auth/packages/better-auth/src/plugins/index.ts
    → re-exports twoFactor, oauth, magicLink, ...

Each named re-export is a public capability. Grouping by source
file gives reasonable feature granularity (a "plugins index"
re-exporting 18 plugin functions is one "Plugin System" feature).

Detection signature (repo-agnostic per
``memory/rule-no-repo-specific-paths``):

  - Files matching ``index.ts`` / ``index.tsx`` (also ``.js`` /
    ``.mjs`` variants).
  - Skip files inside ``node_modules`` / ``tests`` / ``stories``.
  - Parse ``export { foo, bar } from "./...";`` and
    ``export * from "./...";`` and ``export const foo = ...;``
    patterns.

Each file emits ONE signal listing its exports — recall critique
decides whether to surface them as feature(s) based on count and
naming.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


_INDEX_FILE_NAMES = frozenset({
    "index.ts", "index.tsx", "index.js", "index.mjs", "index.cjs",
})

_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", ".turbo", ".venv", "venv", "env",
    "tests", "test", "spec", "specs", "fixtures", "stories",
    "storybook-static", ".storybook", "e2e", "__tests__",
    "examples", "example", "demo", "demos",
})


# Named re-export: export { a, b as c } from "./foo";
_REEXPORT_NAMED_RE = re.compile(
    r"""\bexport\s*\{\s*([^}]+)\s*\}\s*(?:from\s+["'][^"']+["'])?\s*;?""",
    re.VERBOSE | re.DOTALL,
)

# Direct export const/let/var/function/class declaration:
#   export const foo = ...
#   export function foo()
#   export class Foo
#   export async function foo()
#   export default function foo()
_DIRECT_EXPORT_RE = re.compile(
    r"""
    \bexport\s+
    (?:default\s+)?
    (?:async\s+)?
    (?:const|let|var|function|class|interface|type|enum)\s+
    (?P<name>[A-Za-z_$][A-Za-z0-9_$]*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TsLibraryIndex:
    file: str                       # repo-relative
    package_dir: str                # closest dir holding package.json or src/
    exports: tuple[str, ...]


def _walk_index_files(repo_root: Path) -> Iterable[Path]:
    for p in repo_root.rglob("*"):
        if not p.is_file() or p.name not in _INDEX_FILE_NAMES:
            continue
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _parse_named_reexports(text: str) -> list[str]:
    out: list[str] = []
    for m in _REEXPORT_NAMED_RE.finditer(text):
        body = m.group(1)
        # Each comma-separated entry can be "name" or "name as alias".
        for raw in body.split(","):
            raw = raw.strip().split("//")[0].strip()  # drop trailing comments
            if not raw:
                continue
            # Strip type-only marker if present.
            if raw.startswith("type "):
                raw = raw[5:]
            parts = raw.split(" as ")
            name = parts[-1].strip()
            if re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", name):
                out.append(name)
    return out


def _parse_direct_exports(text: str) -> list[str]:
    return [m.group("name") for m in _DIRECT_EXPORT_RE.finditer(text)]


def _package_dir_for(p: Path, repo_root: Path) -> str:
    """Walk up until we find package.json or src/, otherwise the
    parent directory.
    """
    cur = p.parent
    while cur != repo_root and cur != cur.parent:
        if (cur / "package.json").is_file():
            return str(cur.relative_to(repo_root))
        if cur.name == "src":
            return str(cur.parent.relative_to(repo_root)) if cur.parent != repo_root else ""
        cur = cur.parent
    return str(p.parent.relative_to(repo_root))


def collect_ts_library_indices(repo_root: Path) -> list[TsLibraryIndex]:
    out: list[TsLibraryIndex] = []
    seen: set[str] = set()
    for p in _walk_index_files(repo_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names = _parse_named_reexports(text) + _parse_direct_exports(text)
        # De-duplicate while preserving order.
        deduped: list[str] = []
        seen_local: set[str] = set()
        for n in names:
            if n in seen_local:
                continue
            seen_local.add(n)
            deduped.append(n)
        if not deduped:
            continue
        rel = str(p.relative_to(repo_root))
        if rel in seen:
            continue
        seen.add(rel)
        out.append(TsLibraryIndex(
            file=rel,
            package_dir=_package_dir_for(p, repo_root),
            exports=tuple(deduped),
        ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class TsLibraryExportsExtractor:
    """Universal TypeScript library index-exports extractor."""

    name: str = "ts-library-exports-extractor"

    def applicable(self, repo_root: Path) -> bool:
        for _ in _walk_index_files(repo_root):
            return True
        return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        return [
            Signal(
                kind="ts-library-index",
                source=self.name,
                payload={
                    "file": idx.file,
                    "package_dir": idx.package_dir,
                    "export_count": len(idx.exports),
                    "sample_exports": idx.exports[:8],
                    "all_exports": idx.exports,
                },
            )
            for idx in collect_ts_library_indices(repo_root)
        ]


__all__ = [
    "TsLibraryExportsExtractor",
    "TsLibraryIndex",
    "collect_ts_library_indices",
]
