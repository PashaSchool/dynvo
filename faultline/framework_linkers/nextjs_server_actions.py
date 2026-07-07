"""Next.js Server Actions linker — Stage 6.4 (Sprint D1, C5).

What it links
=============

Next.js 13+ exposes server-side mutations to client code via the
``"use server"`` directive. There are two canonical patterns:

1. **Module-level**: a TS/JS file with ``"use server"`` as its very
   first non-comment line. Every export from that file becomes a
   server-callable RPC. Clients import named symbols like normal
   (``import { createUser } from "@/utils/actions/user"``) but the
   call crosses the network boundary at runtime.

2. **Inline action**: a function literal carrying ``"use server"`` as
   its first statement, typically passed to a JSX ``<form action={...}>``
   prop. The action body lives in a CLIENT file but executes on the
   server.

Pipeline gap closed
===================

C3 (whole-import-tree) already records the IMPORTS of action files as
plain symbol attributions — but it cannot distinguish a "real" import
(client-side helper) from a server-action import (network boundary).
This linker:

  * Tags every call site to a server-action file as
    ``link_kind="server-action"`` so dashboards can render the
    network-crossing boundary explicitly.
  * Counts inline actions inside JSX props as first-class call sites.
  * Skips ``app/api/**/route.{ts,tsx,...}`` files entirely — those are
    HTTP routes already handled by :mod:`nextjs_http_route` (C4) and
    are NOT Server Actions in the Next sense.

Determinism
===========

Pure file IO + regex. NO LLM. NO network. Same code → same links.

Failure modes
=============

* No ``"use server"`` directives anywhere → ``is_active`` still returns
  True for Next stacks (cheap), but the route map is empty and
  ``link_for_feature`` returns ``[]`` for every feature.
* Caller file is unreadable → counted in telemetry ``files_unreadable``;
  remaining files still scanned.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.analyzer.tsconfig_paths import (
    AliasEntry,
    build_path_alias_map,
    resolve_ts_import,
)
from faultline.framework_linkers.base import (
    FrameworkLink,
    canonical_sample,
    merge_linker_telemetry,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Activation gates ────────────────────────────────────────────────────────

_NEXT_AUDITED_PREFIXES: tuple[str, ...] = ("next-", "next")
_NEXT_DEPENDENCY_NAMES: tuple[str, ...] = ("next",)


# ── Regex library ───────────────────────────────────────────────────────────

# "use server" directive at the top of a file. We allow any number of
# blank lines, single-line comments (// ...) or block comments at the
# start; the first non-trivia line must be the directive.
_DIRECTIVE_PATTERN = re.compile(r"""^\s*["']use server["']\s*;?\s*$""")
_COMMENT_OR_BLANK_LINE = re.compile(r"""^\s*(?://.*|/\*.*?\*/\s*)?$""")

# Inline action: arrow function whose first non-trivia statement is
# "use server". Captures the source line of the directive itself.
# Conservative: requires the directive to appear within ~5 lines of an
# opening brace following a JSX action= prop OR an inline arrow.
_JSX_ACTION_PROP = re.compile(
    r"""\baction\s*=\s*\{[^{}]*?(?:async\s+)?\(\s*[^)]*\)\s*=>\s*\{""",
)
_INLINE_DIRECTIVE = re.compile(r"""["']use server["']""")

# Re-exports: `export * from "./foo"` and `export { x } from "./foo"`
# inside an action module re-export the symbols at module level too.
_RE_EXPORT = re.compile(
    r"""^\s*export\s*(?:\*|\{[^}]*\})\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Exported function names in an action module — we use these as the
# canonical target symbol when we can match a caller's import.
# Match: export function|const|let|var|class NAME
_EXPORT_DECL = re.compile(
    r"""^\s*export\s+(?:async\s+)?(?:default\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)""",
    re.MULTILINE,
)
# Also `export { foo, bar as baz }` (bare re-exports)
_EXPORT_NAMED = re.compile(
    r"""^\s*export\s*\{([^}]+)\}""",
    re.MULTILINE,
)

# Import statements — used to discover which symbols a caller pulled
# from an action module. Captures (symbols-block, module-specifier).
_IMPORT_NAMED = re.compile(
    r"""^\s*import\s*\{([^}]+)\}\s*from\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_IMPORT_DEFAULT = re.compile(
    r"""^\s*import\s+([A-Za-z_$][\w$]*)\s+from\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
# Bare side-effect import (rare for actions but possible)
_IMPORT_NAMESPACE = re.compile(
    r"""^\s*import\s*\*\s*as\s+([A-Za-z_$][\w$]*)\s*from\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


# ── Internal types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ActionExport:
    """One named export of a server-actions module."""

    module_file: str  # repo-relative POSIX
    symbol: str       # exported name (or "<default>" for default export)
    line_start: int
    line_end: int


@dataclass
class _LinkerTelemetry:
    """Per-scan counters surfaced into the Stage 6.4 artifact."""

    server_action_files_detected: int = 0
    inline_action_sites: int = 0
    action_call_sites_found: int = 0
    links_emitted: int = 0
    features_processed: int = 0
    files_scanned: int = 0
    files_unreadable: int = 0
    sample_links: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "server_action_files_detected": self.server_action_files_detected,
            "inline_action_sites": self.inline_action_sites,
            "action_call_sites_found": self.action_call_sites_found,
            "links_emitted": self.links_emitted,
            "features_processed": self.features_processed,
            "files_scanned": self.files_scanned,
            "files_unreadable": self.files_unreadable,
            "sample_links": canonical_sample(self.sample_links, 5),
        }


# ── Helper functions ────────────────────────────────────────────────────────


@lru_cache(maxsize=4096)
def _read_text_cached(abs_path: str) -> str | None:
    try:
        with open(abs_path, "r", encoding="utf-8") as fp:
            return fp.read()
    except (OSError, UnicodeDecodeError):
        return None


def _has_use_server_directive(text: str) -> bool:
    """True iff the very first non-trivia line is the ``"use server"`` directive."""
    if not text:
        return False
    for line in text.splitlines():
        if _COMMENT_OR_BLANK_LINE.match(line):
            continue
        return _DIRECTIVE_PATTERN.match(line) is not None
    return False


def _is_app_api_route(rel_path: str) -> bool:
    """C4 owns app/api/**/route.{ext} — skip them here to avoid double-emit."""
    posix = rel_path.replace("\\", "/")
    if "/app/api/" not in "/" + posix:
        return False
    return (
        posix.endswith("/route.ts") or posix.endswith("/route.tsx")
        or posix.endswith("/route.js") or posix.endswith("/route.jsx")
        or posix.endswith("/route.mts") or posix.endswith("/route.mjs")
    )


def _extract_exports(text: str) -> list[tuple[str, int]]:
    """Return ``[(symbol, line_no), ...]`` for every export decl + bare re-export."""
    out: list[tuple[str, int]] = []
    for m in _EXPORT_DECL.finditer(text):
        sym = m.group(1)
        line_no = text.count("\n", 0, m.start()) + 1
        out.append((sym, line_no))
    for m in _EXPORT_NAMED.finditer(text):
        block = m.group(1)
        line_no = text.count("\n", 0, m.start()) + 1
        for raw in block.split(","):
            name = raw.strip().split(" as ")[-1].strip()
            if name and re.match(r"^[A-Za-z_$][\w$]*$", name):
                out.append((name, line_no))
    return out


def _file_loc(text: str) -> int:
    return max(1, len(text.splitlines()))


def _module_specifier_to_repo_path(
    specifier: str,
    caller_file: str,
    alias_map: list[AliasEntry],
    tracked: frozenset[str],
) -> str | None:
    """Resolve a JS/TS import specifier to a repo-relative file path.

    Delegates to the shared :func:`resolve_ts_import` (used by C3) so
    BOTH relative imports (``./foo``) AND path-aliased imports
    (``@/utils/actions/x``) resolve consistently. Bare imports (npm
    packages) return ``None``.
    """
    return resolve_ts_import(
        importer_file=caller_file,
        import_spec=specifier,
        alias_map=alias_map,
        tracked_files=tracked,
    )


# ── Linker class ────────────────────────────────────────────────────────────


class NextjsServerActionsLinker:
    """Resolves Next.js Server-Action call sites to ``"use server"`` files."""

    name: str = "nextjs-server-actions"
    activation_keys: tuple[str, ...] = (
        "next-app-router", "next-pages", "next-monorepo", "next",
    )

    def __init__(self) -> None:
        # action_file -> list of exported symbols with line ranges
        self._action_modules: dict[str, list[_ActionExport]] | None = None
        self._tracked_set: frozenset[str] | None = None
        self._alias_map: list[AliasEntry] | None = None
        self._repo_root: Path | None = None
        self.telemetry: _LinkerTelemetry = _LinkerTelemetry()
        # Perf wave R3: per-file outcomes are feature-independent —
        # computed once per file, replayed per (feature × file).
        self._file_outcomes: dict[
            str,
            tuple[
                list[FrameworkLink],
                _LinkerTelemetry,
                list[tuple[str, dict]],
            ],
        ] = {}

    # ── Activation ──────────────────────────────────────────────────────

    def is_active(self, ctx: "ScanContext") -> bool:
        return self._is_next_stack(ctx)

    @staticmethod
    def _is_next_stack(ctx: "ScanContext") -> bool:
        audited = (getattr(ctx, "audited_stack", None) or "").lower()
        if any(audited.startswith(p) for p in _NEXT_AUDITED_PREFIXES):
            return True
        stage0 = (getattr(ctx, "stack", None) or "").lower()
        if any(stage0.startswith(p) for p in _NEXT_AUDITED_PREFIXES):
            return True
        for sec in (getattr(ctx, "secondary_stacks", None) or ()):
            if any(str(sec).lower().startswith(p) for p in _NEXT_AUDITED_PREFIXES):
                return True
        for ws in (getattr(ctx, "workspaces", None) or []):
            pkg = getattr(ws, "package_json", None)
            if not pkg:
                continue
            deps: dict[str, object] = {}
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                d = pkg.get(key)
                if isinstance(d, dict):
                    deps.update(d)
            for dep in deps:
                if any(str(dep) == n for n in _NEXT_DEPENDENCY_NAMES):
                    return True
        return False

    # ── Action-module precomputation ────────────────────────────────────

    def _ensure_action_map(self, ctx: "ScanContext") -> dict[str, list[_ActionExport]]:
        if self._action_modules is not None:
            return self._action_modules

        self._repo_root = ctx.repo_path
        self._tracked_set = frozenset(ctx.tracked_files)
        self._alias_map = build_path_alias_map(ctx.repo_path)

        action_modules: dict[str, list[_ActionExport]] = {}
        for rel in ctx.tracked_files:
            posix = rel.replace("\\", "/")
            # Cheap pre-filter: .ts/.tsx/.js/.jsx only.
            if not posix.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs")):
                continue
            if _is_app_api_route(posix):
                # C4 owns route handlers; not a Server Action surface.
                continue
            abs_path = str(ctx.repo_path / posix)
            text = _read_text_cached(abs_path)
            if text is None:
                continue
            if not _has_use_server_directive(text):
                continue

            exports = _extract_exports(text)
            loc = _file_loc(text)
            if not exports:
                # No named exports but directive present — treat the whole
                # file as one anonymous server-action entry.
                action_modules[posix] = [
                    _ActionExport(
                        module_file=posix, symbol="<module>",
                        line_start=1, line_end=loc,
                    ),
                ]
                continue
            # Sort by line so the next-export gives us an upper bound.
            exports.sort(key=lambda x: x[1])
            entries: list[_ActionExport] = []
            for i, (sym, start_line) in enumerate(exports):
                end_line = exports[i + 1][1] - 1 if i + 1 < len(exports) else loc
                entries.append(_ActionExport(
                    module_file=posix, symbol=sym,
                    line_start=start_line, line_end=max(start_line, end_line),
                ))
            action_modules[posix] = entries

        self._action_modules = action_modules
        self.telemetry.server_action_files_detected = len(action_modules)
        return action_modules

    # ── Public surface ─────────────────────────────────────────────────

    def link_for_feature(
        self,
        feature: "Feature",
        ctx: "ScanContext",
        log: "StageLogger",
    ) -> list[FrameworkLink]:
        # We ALWAYS build the action-map (even if empty) because inline
        # JSX actions live in CLIENT files that don't themselves carry
        # the top-level directive — we still need to scan them.
        action_modules = self._ensure_action_map(ctx)
        self.telemetry.features_processed += 1
        tracked = self._tracked_set or frozenset(ctx.tracked_files)
        alias_map = self._alias_map or []

        caller_files = self._caller_files(feature)
        if not caller_files:
            return []

        links: list[FrameworkLink] = []
        for rel in caller_files:
            # Caller files that ARE action modules themselves don't need
            # to be scanned for cross-file imports of themselves; we DO
            # still want to detect inline-action JSX patterns there.
            abs_path = str(ctx.repo_path / rel)
            text = _read_text_cached(abs_path)
            if text is None:
                self.telemetry.files_unreadable += 1
                continue
            self.telemetry.files_scanned += 1
            file_links = self._scan_file(
                rel, text, action_modules, tracked, alias_map, ctx, log,
                feature_name=feature.name,
            )
            links.extend(file_links)

        self.telemetry.links_emitted += len(links)
        return links

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _caller_files(feature: "Feature") -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for attr in (feature.symbol_attributions or []):
            f = (attr.file or "").replace("\\", "/")
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        for f in (feature.paths or []):
            f = (f or "").replace("\\", "/")
            if not f:
                continue
            if "." not in f.rsplit("/", 1)[-1]:
                continue
            if f in seen:
                continue
            seen.add(f)
            out.append(f)
        return out

    def _scan_file(
        self,
        rel: str,
        text: str,
        action_modules: dict[str, list[_ActionExport]],
        tracked: frozenset[str],
        alias_map: list[AliasEntry],
        ctx: "ScanContext",
        log: "StageLogger",
        *,
        feature_name: str,
    ) -> list[FrameworkLink]:
        """Per-feature entry: cached per-file compute + per-occurrence replay
        (perf wave R3 — see nextjs_http_route for the pattern rationale)."""
        outcome = self._file_outcomes.get(rel)
        if outcome is None:
            outcome = self._compute_file_outcome(
                rel, text, action_modules, tracked, alias_map,
            )
            self._file_outcomes[rel] = outcome
        links, tel_delta, events = outcome
        merge_linker_telemetry(self.telemetry, tel_delta)
        for msg, kwargs in events:
            log.emit(feature_name, msg, **kwargs)
        return list(links)

    def _compute_file_outcome(
        self,
        rel: str,
        text: str,
        action_modules: dict[str, list[_ActionExport]],
        tracked: frozenset[str],
        alias_map: list[AliasEntry],
    ) -> tuple[
        list[FrameworkLink], "_LinkerTelemetry", list[tuple[str, dict]],
    ]:
        """Feature-independent scan of ONE file (pure given its inputs)."""
        tel = _LinkerTelemetry()
        events: list[tuple[str, dict]] = []
        results: list[FrameworkLink] = []
        assert self._repo_root is not None

        # 1. Imported server actions — resolve each `import { x } from "./foo"`
        #    where ./foo is an action module; for each imported symbol,
        #    emit one link per call site in this file.
        imported: dict[str, _ActionExport] = {}  # local-name -> export entry
        for m in _IMPORT_NAMED.finditer(text):
            block = m.group(1)
            spec = m.group(2)
            target = _module_specifier_to_repo_path(
                spec, rel, alias_map, tracked,
            )
            if target is None or target not in action_modules:
                continue
            module_exports = {e.symbol: e for e in action_modules[target]}
            for raw in block.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                # `foo` or `foo as bar` or default `default as foo`
                if " as " in raw:
                    orig, alias = (p.strip() for p in raw.split(" as ", 1))
                else:
                    orig = alias = raw
                # Skip type-only imports
                if orig.startswith("type "):
                    continue
                exp = module_exports.get(orig)
                if exp is None and "<module>" in module_exports:
                    exp = module_exports["<module>"]
                if exp is not None:
                    imported[alias] = exp

        for m in _IMPORT_DEFAULT.finditer(text):
            local = m.group(1)
            spec = m.group(2)
            target = _module_specifier_to_repo_path(
                spec, rel, alias_map, tracked,
            )
            if target is None or target not in action_modules:
                continue
            # Pick the module's <default> export if present, else <module>.
            module_exports = {e.symbol: e for e in action_modules[target]}
            exp = (
                module_exports.get("<default>")
                or module_exports.get("<module>")
                or (action_modules[target][0] if action_modules[target] else None)
            )
            if exp is not None:
                imported[local] = exp

        # 2. Find each call site of any imported local name.
        if imported:
            # Build one regex matching `\b(name1|name2|...)\s*\(`
            names_alt = "|".join(re.escape(n) for n in imported)
            call_re = re.compile(rf"\b({names_alt})\s*\(")
            for cm in call_re.finditer(text):
                local = cm.group(1)
                exp = imported[local]
                tel.action_call_sites_found += 1
                line_no = text.count("\n", 0, cm.start()) + 1
                source_symbol = _enclosing_symbol(text.splitlines(), line_no)
                link = FrameworkLink(
                    source_file=rel,
                    source_symbol=source_symbol,
                    source_line=line_no,
                    target_file=exp.module_file,
                    target_symbol=exp.symbol,
                    target_line_start=exp.line_start,
                    target_line_end=exp.line_end,
                    linker=self.name,
                    link_kind="server-action",
                    confidence=1.0,
                    reason=(
                        f"call to imported server action "
                        f"{exp.symbol} from {exp.module_file}"
                    ),
                )
                results.append(link)
                tel.sample_links.append({
                    "source": f"{rel}:{line_no}",
                    "target": f"{exp.module_file}:{exp.symbol}",
                    "kind": "imported-action",
                })

        # 3. Inline JSX actions: <form action={async (...) => { "use server"; ... }}>
        for am in _JSX_ACTION_PROP.finditer(text):
            # Look at the next ~400 chars for the directive.
            window = text[am.start(): am.start() + 400]
            if not _INLINE_DIRECTIVE.search(window):
                continue
            # Verify it's actually the FIRST statement (cheap heuristic):
            # find the directive's offset and ensure no `;` precedes it
            # in the body window between the opening brace and the
            # directive's match.
            body_start = am.end()  # right after opening brace
            dm = _INLINE_DIRECTIVE.search(text, body_start, body_start + 400)
            if dm is None:
                continue
            preamble = text[body_start: dm.start()]
            # Allow whitespace + comments only.
            stripped = re.sub(r"//.*", "", preamble)
            stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
            if stripped.strip() not in ("", ";"):
                continue
            tel.inline_action_sites += 1
            tel.action_call_sites_found += 1
            line_no = text.count("\n", 0, am.start()) + 1
            source_symbol = _enclosing_symbol(text.splitlines(), line_no)
            link = FrameworkLink(
                source_file=rel,
                source_symbol=source_symbol,
                source_line=line_no,
                # Inline actions live in the same file — the "target" is
                # the inline body itself.
                target_file=rel,
                target_symbol="<inline-action>",
                target_line_start=line_no,
                target_line_end=line_no,
                linker=self.name,
                link_kind="server-action",
                confidence=1.0,
                reason="inline `use server` action in JSX action prop",
            )
            results.append(link)
            tel.sample_links.append({
                "source": f"{rel}:{line_no}",
                "target": f"{rel}:<inline-action>",
                "kind": "inline-jsx",
            })
            events.append((
                f"inline server-action at {rel}:{line_no}",
                {"linker": self.name, "kind": "inline-jsx"},
            ))

        return results, tel, events


# ── Enclosing-symbol heuristic (copied from C4 — same idiom) ────────────────


_FN_DECL = re.compile(
    r"""^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?
        (?:function\s+([A-Za-z_$][\w$]*)
         |const\s+([A-Za-z_$][\w$]*)\s*=
         |let\s+([A-Za-z_$][\w$]*)\s*=
        )""",
    re.VERBOSE,
)


def _enclosing_symbol(lines: list[str], call_line: int) -> str:
    start = max(1, call_line - 200)
    for ln in range(call_line - 1, start - 1, -1):
        if ln <= 0 or ln > len(lines):
            continue
        line = lines[ln - 1]
        m = _FN_DECL.match(line)
        if m:
            for grp in m.groups():
                if grp:
                    return grp
    return "<module>"


__all__ = ["NextjsServerActionsLinker"]
