"""tRPC router extractor (Sprint 3 / Phase 5 Layer C).

Detects tRPC router files and extracts the procedure names declared
in each router. tRPC is the dominant typesafe-RPC layer in modern
React/Next SaaS — inbox-zero, dub, formbricks, etc. all use it.
Each procedure (``query`` / ``mutation`` / ``subscription``) is a
customer-facing operation.

A tRPC router is a TS file that:
  1. Imports something from ``@trpc/server`` or matches a
     ``createTRPCRouter`` / ``router`` callsite, AND
  2. Contains procedure declarations of the shape
     ``NAME: someProcedure.<query|mutation|subscription>(...)``.

The extractor groups procedures per FILE (not per procedure) and
emits one signal per router file with sample procedure names —
ground-truth feature lists describe domains, not individual RPC
calls.

Generic per ``memory/rule-no-repo-specific-paths`` — works on any
TypeScript codebase using @trpc/server, no hardcoded directory
names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


_SOURCE_EXTS = frozenset({".ts", ".tsx"})

_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", ".turbo", ".venv", "venv", "env",
    "tests", "test", "spec", "specs", "fixtures", "stories",
    "storybook-static", ".storybook", "e2e", "__tests__",
})

# Indicators that a file is a tRPC router. Any one is enough.
_TRPC_HINTS = (
    "@trpc/server",
    "createTRPCRouter",
    "createCallerFactory",
    "router({",
    "router( {",
    "publicProcedure",
    "protectedProcedure",
)

# Procedure declarations come in two parts that are too unpredictable
# to match in one regex (the chain ``.input(z.object({...})).query(...)``
# contains nested parens that break naive ``[^)]*`` matching).
#
# Strategy: 2-pass:
#   1. Find every ``NAME: <identifier>`` left-hand side that looks
#      like the start of a procedure declaration.
#   2. For each, scan the text chunk up to the next NAME (or EOF) for
#      a terminal ``.query(`` / ``.mutation(`` / ``.subscription(``.
#
# Robust to arbitrary chained method calls between the name and the
# terminal call, and to multiline declarations.
_PROC_NAME_RE = re.compile(
    r"""
    (?:^|[\{,;\n])\s*
    (?P<name>[A-Za-z_$][A-Za-z0-9_$]*)
    \s*:\s*
    [A-Za-z_$]            # value side starts with an identifier (the procedure builder)
    """,
    re.VERBOSE | re.MULTILINE,
)

_TERMINAL_RE = re.compile(
    r"""\.\s*(?P<kind>query|mutation|subscription)\s*\(""",
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TrpcProcedure:
    name: str
    kind: str   # "query" / "mutation" / "subscription"


@dataclass(frozen=True, slots=True, kw_only=True)
class TrpcRouterFile:
    file: str
    procedures: tuple[TrpcProcedure, ...]


def _walkable_files(repo_root: Path) -> Iterable[Path]:
    for p in repo_root.rglob("*"):
        if not p.is_file() or p.suffix not in _SOURCE_EXTS:
            continue
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _looks_like_trpc_router(text: str) -> bool:
    return any(hint in text for hint in _TRPC_HINTS)


def _depth_at(text: str, pos: int) -> int:
    """Brace/paren/bracket nesting depth at character offset ``pos``.

    Used to filter out NAME matches that aren't at the top level of
    a router's object literal — e.g. ``from:`` and ``amount:`` keys
    inside a ``z.object({...})`` schema argument are at depth > 1
    and must not be treated as procedure declarations.
    """
    depth = 0
    for c in text[:pos]:
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
    return depth


def _extract_procedures(text: str) -> list[TrpcProcedure]:
    """Two-pass with depth filter:
      1. Find every ``NAME: <identifier>`` left-hand side at the
         top level of a router object (depth == 1).
      2. For each, scan the chunk up to the next top-level name (or
         EOF) for a terminal ``.query(`` / ``.mutation(`` /
         ``.subscription(``.
    """
    # Filter names by structural depth so we ignore properties
    # nested inside zod schemas or method-call argument literals.
    # Use m.end() (just past the value-side first char) so the
    # measurement is consistent regardless of which prefix char the
    # regex consumed (newline vs comma vs brace).
    # Procedure declarations sit at depth 2: one level for
    # ``createTRPCRouter(`` and one for the object literal ``{``.
    top_level_matches = [
        m for m in _PROC_NAME_RE.finditer(text)
        if _depth_at(text, m.end()) == 2
    ]
    seen: set[str] = set()
    out: list[TrpcProcedure] = []
    for i, m in enumerate(top_level_matches):
        name = m.group("name")
        if name in seen:
            continue
        start = m.end()
        end = (
            top_level_matches[i + 1].start()
            if i + 1 < len(top_level_matches)
            else len(text)
        )
        chunk = text[start:end]
        terminal = _TERMINAL_RE.search(chunk)
        if not terminal:
            continue
        seen.add(name)
        out.append(TrpcProcedure(name=name, kind=terminal.group("kind")))
    return out


def collect_trpc_routers(repo_root: Path) -> list[TrpcRouterFile]:
    out: list[TrpcRouterFile] = []
    for p in _walkable_files(repo_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not _looks_like_trpc_router(text):
            continue
        procs = _extract_procedures(text)
        if not procs:
            continue
        out.append(TrpcRouterFile(
            file=str(p.relative_to(repo_root)),
            procedures=tuple(procs),
        ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class TrpcRouterExtractor:
    """Universal tRPC router extractor."""

    name: str = "trpc-router-extractor"

    def applicable(self, repo_root: Path) -> bool:
        for p in _walkable_files(repo_root):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "@trpc/server" in text or "createTRPCRouter" in text:
                return True
        return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        return [
            Signal(
                kind="trpc-router-file",
                source=self.name,
                payload={
                    "file": r.file,
                    "procedure_count": len(r.procedures),
                    "sample_procedures": tuple(
                        f"{p.name}({p.kind})" for p in r.procedures[:8]
                    ),
                    "router_basename": Path(r.file).stem,
                },
            )
            for r in collect_trpc_routers(repo_root)
        ]


__all__ = [
    "TrpcProcedure",
    "TrpcRouterFile",
    "TrpcRouterExtractor",
    "collect_trpc_routers",
]
