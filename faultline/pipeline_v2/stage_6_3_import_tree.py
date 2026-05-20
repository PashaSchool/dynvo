"""Stage 6.3 — Whole-import-tree enrichment (Sprint C3, deterministic).

Why this stage exists
=====================

After Sprint C1 (file-level flow reach) and Sprint C2 (per-flow
symbol attribution) the pipeline still has two gaps that the user
identified by inspecting real scan output:

  1. **Forward import tree** is too shallow / mis-resolves.
     Stage 3's ``flow_reach`` BFS uses
     ``analyzer.import_graph._resolve_import`` which reads exactly
     ONE ``tsconfig.json`` (root or ``src/``). Real Next.js monorepos
     keep their alias map in ``apps/web/tsconfig.json`` and that file
     ``extends`` a sibling base config (``packages/tsconfig/nextjs``).
     Imports like ``@/utils/foo`` never resolve, so the BFS terminates
     at depth 0 and Flow.paths stays at 1.

  2. **Reverse import tree** is missing entirely.
     ``PackageAnchorExtractor`` emits a Billing feature with
     ``paths=['apps/web']`` (the workspace root) because that's where
     ``stripe`` is in ``package.json``. No flow detection runs (the
     feature has no exported entry symbols at the workspace-root
     level), so the file-level BFS never seeds. Result: 47 stripe
     consumers in the codebase, 0 of them attributed to Billing.

This stage closes both gaps with ONE unified BFS:

  - Forward seeds: each flow's entry symbol (when present).
  - Reverse seeds: for package-anchor features, every file that
    imports the anchor module. For schema-source features, every
    file that imports a model symbol from the schema.
  - Structural fallback: features with neither flows nor anchor
    rationale get a seed per primary path's dominant symbol.

The BFS visits ``(file, symbol)`` pairs, hops along resolved
imports (using the new :mod:`faultline.analyzer.tsconfig_paths`
resolver that handles workspace + extends), and emits one
``FlowSymbolAttribution`` per reached symbol with role
``entry`` | ``anchor-consumer`` | ``schema-consumer`` |
``structural`` | ``called``.

The enriched paths surface in two places:

  - ``Feature.paths``   — expanded with every unique file reached
    from any of the feature's seeds.
  - ``Feature.shared_attributions``   — one entry per reached
    ``(file, symbol)`` pair, line-bounded (compatible with the
    pydantic :class:`SymbolAttribution` model).

For features that already have flows, the flow's own
``flow_symbol_attributions`` are extended with the called-symbol
chain reached from that flow's entry. ``Flow.paths`` becomes the
union of original + newly-reached files.

Caps + safety
=============

  - ``max_depth = 8``                   — page → component → hook →
                                          service → util → primitives
                                          chain + headroom (raised from
                                          6 in Sprint C3b after
                                          telemetry showed depth >= 4
                                          reached on real monorepos).
  - ``max_files_per_feature = 100``     — bounded payload size.
  - ``max_symbols_per_feature = 500``   — defensive ceiling.
  - Scale-invariant: identical bounds for every repo (per
    ``rule-no-magic-tuning``).
  - LRU caches on import extraction + symbol-body locate so repeated
    hops on shared infra don't re-parse files.

NO LLM. NO network. Pure structural parsing.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from faultline.analyzer.reverse_imports import (
    find_consumers_of_module,
    find_consumers_of_symbols,
    find_symbols_in_file_using_module,
    find_symbols_in_file_using_symbols,
)
from faultline.analyzer.tsconfig_paths import (
    AliasEntry,
    build_path_alias_map,
    resolve_ts_import,
)
from faultline.pipeline_v2.flow_symbols import (
    _enumerate_functions,
    _find_brace_end,
    _line_starts,
    _offset_to_line,
    _python_body_end_line,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Tunables (scale-invariant) ───────────────────────────────────────────

DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_FILES_PER_FEATURE = 100
DEFAULT_MAX_SYMBOLS_PER_FEATURE = 500

# Sprint F (2026-05-20) — large-repo concurrency + graceful degradation.
#
# Each feature's BFS is independent (its only shared state is the
# read-mostly ``_SourceCache``). A modest ``ThreadPoolExecutor`` keeps
# the GIL-bound regex / file-IO work busy without overwhelming a laptop.
# The wall-clock budget caps how long the stage may run; when exceeded
# we stop submitting new features, drain in-flight work, mark the
# remainder as ``budget_skipped`` in telemetry, and emit a warning so
# the operator can react. Scale-invariant: bounded by wall-time, not by
# feature count or repo size (per ``rule-no-magic-tuning``).
DEFAULT_MAX_WORKERS = 4
DEFAULT_WALL_BUDGET_SEC = 180.0

# Extensions we attempt to slice into symbol bodies. Files outside
# this set are still attributed as ``support`` (whole-file ranges)
# when reached.
_SLICEABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs",
    ".py",
})
_TS_EXTS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs",
})
_PY_EXTS: frozenset[str] = frozenset({".py"})

# Skip the same vendor / test markers flow_reach skips so attribution
# stays meaningful.
_VENDOR_PATH_MARKERS: tuple[str, ...] = (
    "/node_modules/", "/vendor/", "/venv/", "/.venv/",
    "/dist/", "/build/", "/out/", "/.next/", "/.turbo/",
    "/target/", "/__pycache__/", "/.pytest_cache/",
    "/.generated/", "/generated/",
)
_TEST_PATH_MARKERS: tuple[str, ...] = (
    "/tests/", "/test/", "/__tests__/", "/spec/",
    ".test.", ".spec.", "_test.go",
)

# Maximum file size we'll read into memory for slicing. Keeps the
# BFS bounded on pathological minified bundles.
_MAX_SLICE_BYTES = 2_000_000

# Roles emitted by this stage.
SeedRole = Literal[
    "entry", "anchor-consumer", "schema-consumer", "structural", "called",
]


# ── Public dataclasses ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportTreeSeed:
    """One starting point for the BFS over a single feature."""

    file: str
    symbol: str
    role: SeedRole


@dataclass(frozen=True)
class SymbolAttributionRecord:
    """In-memory analogue of ``FlowSymbolAttribution`` used during the
    BFS. Converted to the pydantic ``SymbolAttribution`` model at the
    end of the stage.
    """

    file: str
    symbol: str
    line_start: int
    line_end: int
    role: SeedRole


@dataclass
class FeatureEnrichment:
    """Per-feature enrichment summary, kept for the stage artifact."""

    feature_name: str
    source_kind: str  # "flow-based" | "package-anchor" | "schema-source" | "structural" | "config" | "none"
    anchor_deps: list[str] = field(default_factory=list)
    seeds_count: int = 0
    seeds_sample: list[dict[str, str]] = field(default_factory=list)
    paths_pre: int = 0
    paths_post: int = 0
    symbols_pre: int = 0
    symbols_post: int = 0
    depth_distribution: dict[int, int] = field(default_factory=dict)
    elapsed_ms: int = 0


@dataclass
class EnrichmentResult:
    """Top-level result of Stage 6.3."""

    enriched_features: list["Feature"]
    per_feature: list[FeatureEnrichment]
    total_seeds: int
    total_files_reached: int
    total_symbols_emitted: int
    cycles_detected: int
    depth_capped_events: int
    external_skipped: int
    cache_hits: int
    alias_map: list[AliasEntry]
    elapsed_sec: float
    # Sprint F (2026-05-20) — graceful degradation telemetry.
    budget_exceeded: bool = False
    budget_sec: float = 0.0
    features_budget_skipped: int = 0
    max_workers: int = 1


# ── Filter helpers ───────────────────────────────────────────────────────


def _is_vendor_or_test(path: str) -> bool:
    needle = "/" + path
    for marker in _VENDOR_PATH_MARKERS:
        if marker in needle:
            return True
    for marker in _TEST_PATH_MARKERS:
        if marker in needle:
            return True
    return False


def _suffix(path: str) -> str:
    idx = path.rfind(".")
    if idx < 0:
        return ""
    return path[idx:].lower()


def _language_for(path: str) -> str | None:
    suffix = _suffix(path)
    if suffix in _TS_EXTS:
        return "ts"
    if suffix in _PY_EXTS:
        return "py"
    return None


# ── Source caches ────────────────────────────────────────────────────────


class _SourceCache:
    """In-process per-scan cache for file contents + extracted metadata.

    All entries are bounded by file count rather than LRU eviction —
    a Layer 1 scan touches at most a few thousand source files, all of
    which fit in memory (median TS file < 5 KB).
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path
        self._text: dict[str, str | None] = {}
        self._functions: dict[str, list] = {}
        self._imports: dict[str, dict[str, str]] = {}
        self.hits = 0

    def text(self, rel: str) -> str | None:
        if rel in self._text:
            self.hits += 1
            return self._text[rel]
        try:
            abs_path = self._repo_path / rel
            stat = abs_path.stat()
            if stat.st_size > _MAX_SLICE_BYTES:
                self._text[rel] = None
                return None
            self._text[rel] = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            self._text[rel] = None
        return self._text[rel]

    def functions(self, rel: str) -> list:
        if rel in self._functions:
            self.hits += 1
            return self._functions[rel]
        text = self.text(rel)
        lang = _language_for(rel)
        if text is None or lang is None:
            self._functions[rel] = []
            return []
        try:
            entries = _enumerate_functions(text, lang)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("stage_6_3: function enumeration failed for %s: %s",
                         rel, exc)
            self._functions[rel] = []
            return []
        # TS only — extend any suspiciously short entry past the
        # parameter block (regex confuses ``(props: {...})`` for the
        # body brace). Python entries use indent tracking and are
        # accurate.
        if lang == "ts":
            entries = [_maybe_extend_entry(self, rel, e) for e in entries]
        self._functions[rel] = entries
        return self._functions[rel]

    def imports(self, rel: str) -> dict[str, str]:
        """Return ``{local_name: import_specifier_string}`` for ``rel``.

        We keep the SPECIFIER (not the resolved path) so resolution
        happens against the per-scan alias map only once, lazily.
        """
        if rel in self._imports:
            self.hits += 1
            return self._imports[rel]
        text = self.text(rel)
        lang = _language_for(rel)
        if text is None or lang is None:
            self._imports[rel] = {}
            return {}
        if lang == "ts":
            self._imports[rel] = _extract_ts_imports(text)
        else:
            self._imports[rel] = _extract_py_imports(text)
        return self._imports[rel]


# ── TS / JS import-line parsers ──────────────────────────────────────────

# These mirror flow_symbols's helpers but live here so Stage 6.3 has
# its own contract; the upstream module may evolve independently.

_RE_TS_IMPORT_LINE = re.compile(
    r"""
    ^\s*import\s+
    (?P<head>
        (?:[A-Za-z_$][\w$]*\s*,?\s*)?
        (?:\*\s+as\s+[A-Za-z_$][\w$]*|\{[^}]*\})?
    )
    \s*from\s+['"](?P<mod>[^'"]+)['"]
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_TS_SIDE_EFFECT_IMPORT = re.compile(
    r"""^\s*import\s+['"](?P<mod>[^'"]+)['"]""",
    re.VERBOSE | re.MULTILINE,
)
_RE_TS_REQUIRE = re.compile(
    r"""
    (?:const|let|var)\s+
    (?P<bind>[A-Za-z_$][\w$]*|\{[^}]*\})
    \s*=\s*require\s*\(\s*['"](?P<mod>[^'"]+)['"]\s*\)
    """,
    re.VERBOSE,
)


def _extract_ts_imports(source: str) -> dict[str, str]:
    """Return ``{local_name: import_specifier}`` for a TS/JS file."""
    out: dict[str, str] = {}
    for m in _RE_TS_IMPORT_LINE.finditer(source):
        head = (m.group("head") or "").strip()
        mod = m.group("mod")
        if not mod:
            continue
        for local in _ts_locals_of(head):
            out[local] = mod
    for m in _RE_TS_REQUIRE.finditer(source):
        mod = m.group("mod")
        bind = (m.group("bind") or "").strip()
        if not mod:
            continue
        if bind.startswith("{"):
            for piece in bind.strip("{}").split(","):
                piece = piece.strip().split(":")[-1].strip()
                if piece:
                    out[piece] = mod
        elif bind:
            out[bind] = mod
    # Side-effect imports — register the module but no local name.
    # Not useful for symbol-level traversal so we don't add them.
    return out


def _ts_locals_of(head: str) -> list[str]:
    """Pull local names out of an import head.

    Accepts the contents BETWEEN ``import`` and ``from``. Returns
    every name a downstream identifier scan would match.
    """
    head = head.strip()
    locals_: list[str] = []
    if not head:
        return locals_

    # Split default + named: ``Foo, { Bar, Baz as B2 }``
    # 1. namespace import — strip it from the rest.
    ns_match = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", head)
    if ns_match:
        locals_.append(ns_match.group(1))
        head = head[: ns_match.start()] + head[ns_match.end():]

    # 2. named imports — anything inside { ... }.
    brace_match = re.search(r"\{([^}]*)\}", head)
    if brace_match:
        names_section = brace_match.group(1)
        head = head[: brace_match.start()] + head[brace_match.end():]
        for piece in names_section.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if " as " in piece:
                piece = piece.split(" as ", 1)[1].strip()
            if piece.startswith("type "):
                piece = piece[5:].strip()
            if piece:
                locals_.append(piece)

    # 3. default import — the remaining bare identifier.
    head = head.strip().rstrip(",").strip()
    if head and re.fullmatch(r"[A-Za-z_$][\w$]*", head):
        locals_.append(head)

    return locals_


# ── Python import-line parser ────────────────────────────────────────────

_RE_PY_FROM = re.compile(
    r"^\s*from\s+(?P<mod>[\w.]+)\s+import\s+(?P<names>[^\n]+)",
    re.MULTILINE,
)
_RE_PY_IMPORT = re.compile(
    r"^\s*import\s+(?P<mod>[\w.]+)(?:\s+as\s+(?P<alias>\w+))?\s*$",
    re.MULTILINE,
)


def _extract_py_imports(source: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _RE_PY_FROM.finditer(source):
        mod = m.group("mod")
        for piece in m.group("names").split(","):
            piece = piece.strip().strip("()")
            if not piece or piece == "*":
                continue
            if " as " in piece:
                piece = piece.split(" as ", 1)[1].strip()
            if piece:
                out[piece] = mod
    for m in _RE_PY_IMPORT.finditer(source):
        mod = m.group("mod")
        alias = m.group("alias") or mod.split(".", 1)[0]
        out[alias] = mod
    return out


# ── Symbol locate helpers ────────────────────────────────────────────────


def _locate_symbol_at_line(
    cache: _SourceCache, file: str, line: int,
) -> tuple[str, int, int] | None:
    """Return ``(name, line_start, line_end)`` for the function whose
    body contains ``line`` in ``file``, or ``None``.

    Body-end correction for the inline-param-type case (``(props: {...})``)
    happens inside :meth:`_SourceCache.functions` at enumeration time
    so every consumer (seed phase, BFS traversal, flow-attribution)
    sees the same fixed-up span.
    """
    fns = cache.functions(file)
    best = None
    for fn in fns:
        if fn.line_start <= line <= fn.line_end:
            if best is None or (fn.line_end - fn.line_start) < (best.line_end - best.line_start):
                best = fn
    if best is None:
        return None
    return (best.name, best.line_start, best.line_end)


def _maybe_extend_entry(cache: "_SourceCache", rel: str, entry: Any) -> Any:
    """Return a copy of ``entry`` with ``line_end`` extended via
    :func:`_better_body_end` when the original span looks truncated.
    """
    if (entry.line_end - entry.line_start) > 6:
        return entry
    better = _better_body_end(cache, rel, entry.line_start)
    if better is None or better <= entry.line_end:
        return entry
    # _FunctionEntry is a frozen dataclass — use dataclasses.replace.
    import dataclasses as _dc
    try:
        return _dc.replace(entry, line_end=better)
    except Exception:
        return entry


def _better_body_end(
    cache: _SourceCache, file: str, line_start: int,
) -> int | None:
    """Re-derive a function's body end by walking PAST the parameter
    block first. Returns ``None`` when the heuristic can't apply.

    Only fires for TS/JS files. Python functions are indent-based and
    don't suffer the same brace-confusion.
    """
    if _suffix(file) not in _TS_EXTS:
        return None
    text = cache.text(file)
    if text is None:
        return None
    lines = text.splitlines()
    if line_start - 1 >= len(lines):
        return None
    # Walk the source from line_start, skip past first ( ... ) param
    # block (balanced), then find first { and balance from there.
    # Compute absolute offset of line_start.
    line_offset = sum(len(lines[i]) + 1 for i in range(line_start - 1))
    n = len(text)
    i = line_offset
    # Find first '(' on or after line_start.
    paren_open = text.find("(", i, min(n, i + 4000))
    if paren_open < 0:
        return None
    # Walk balanced parens.
    depth = 1
    i = paren_open + 1
    in_str: str | None = None
    while i < n and depth > 0:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch in {'"', "'", "`"}:
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    if depth != 0:
        return None
    paren_end = i
    # Find next '{' that opens the function body.
    brace_open = text.find("{", paren_end, min(n, paren_end + 200))
    if brace_open < 0:
        # Arrow body with no block (one-liner) — leave end as is.
        return None
    # Walk balanced braces.
    depth = 1
    i = brace_open + 1
    in_str = None
    in_line_comment = False
    in_block_comment = False
    while i < n and depth > 0:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
        elif in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in {'"', "'", "`"}:
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    # Compute line for offset i.
                    return 1 + text.count("\n", 0, i + 1)
        i += 1
    return None


def _locate_symbol_by_name(
    cache: _SourceCache, file: str, symbol: str,
) -> tuple[int, int] | None:
    """Return ``(line_start, line_end)`` for the named function in
    ``file``. Falls back to the first match when multiple share the
    name (rare — duplicate top-level exports are syntax errors in TS).
    """
    fns = cache.functions(file)
    for fn in fns:
        if fn.name == symbol:
            return (fn.line_start, fn.line_end)
    return None


def _slice_symbol_body(
    cache: _SourceCache, file: str, line_start: int, line_end: int,
) -> str:
    """Return the source text spanning ``[line_start, line_end]``."""
    text = cache.text(file)
    if text is None:
        return ""
    lines = text.splitlines()
    s = max(0, line_start - 1)
    e = min(len(lines), line_end)
    return "\n".join(lines[s:e])


def _file_loc(cache: _SourceCache, file: str) -> int:
    text = cache.text(file)
    if text is None:
        return 0
    return max(1, text.count("\n") + 1)


# ── Identifier extraction (for forward-walking) ──────────────────────────

_RE_IDENT = re.compile(r"\b([A-Za-z_$][\w$]*)\b")


def _identifiers_in(body: str) -> set[str]:
    return set(_RE_IDENT.findall(body))


# ── Seed phase ───────────────────────────────────────────────────────────


_RE_DEPS_RATIONALE = re.compile(
    r"package anchor\s+'?[\w-]+'?\s+from deps\s+\[([^\]]+)\]",
    re.IGNORECASE,
)


def _parse_anchor_deps(description: str | None) -> list[str]:
    """Extract dep names from a ``PackageAnchorExtractor`` rationale.

    Stage 2 stores the rationale as ``"package anchor 'billing' from
    deps ['stripe', '@stripe/stripe-js']"``. We don't have access to
    the original AnchorCandidate by Stage 6.3, so we parse it back
    from the description string. Returns an empty list on any other
    description shape.
    """
    if not description:
        return []
    m = _RE_DEPS_RATIONALE.search(description)
    if not m:
        return []
    raw = m.group(1)
    out: list[str] = []
    for piece in raw.split(","):
        piece = piece.strip().strip("'\"")
        if piece:
            out.append(piece)
    return out


def _is_package_anchor(feature: "Feature") -> bool:
    desc = (feature.description or "").lower()
    return "package anchor" in desc


def _is_schema_source(feature: "Feature") -> bool:
    desc = (feature.description or "").lower()
    # Schema-domain extractor uses rationale starting with
    # ``"schema domain"`` / ``"schema model"``. Be permissive.
    return "schema " in desc and (
        "domain" in desc or "model" in desc or "table" in desc
    )


def _is_config_anchor(feature: "Feature") -> bool:
    desc = (feature.description or "").lower()
    return "config-as-product" in desc or "manifest" in desc[:80]


def _extract_model_names_from_schema(
    file: str, cache: _SourceCache,
) -> list[str]:
    """Pull model / table names out of a schema file.

    Handles Prisma (``model Foo {``), Drizzle (``export const foo =
    sqliteTable("...")``), Django (``class Foo(models.Model):``),
    Rails (``create_table :foo do``). Returns at most 30 names.
    """
    text = cache.text(file)
    if text is None:
        return []
    out: list[str] = []
    for pat in (
        r"^\s*model\s+([A-Z][\w]*)\s*\{",                # Prisma
        r"^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*\w*[Tt]able\s*\(",  # Drizzle
        r"^\s*class\s+([A-Z][\w]*)\s*\(\s*models\.Model",  # Django
        r"^\s*create_table\s+:(\w+)",                     # Rails
    ):
        for m in re.finditer(pat, text, flags=re.MULTILINE):
            name = m.group(1)
            if name and name not in out:
                out.append(name)
                if len(out) >= 30:
                    break
    return out


def _find_dominant_symbol(
    file: str, cache: _SourceCache,
) -> tuple[str, int, int] | None:
    """Return the FIRST exported / top-level function in ``file``.

    Heuristic — Stage 3 already uses ``_enumerate_functions``; we
    pick the first one whose span is > 3 lines (skips trivial
    re-exports). Returns ``None`` for files with no detectable
    function.
    """
    fns = cache.functions(file)
    for fn in fns:
        if (fn.line_end - fn.line_start) >= 2:
            return (fn.name, fn.line_start, fn.line_end)
    if fns:
        first = fns[0]
        return (first.name, first.line_start, first.line_end)
    return None


def _determine_seeds(
    feature: "Feature",
    ctx: "ScanContext",
    cache: _SourceCache,
    tracked_files: frozenset[str],
    *,
    log: "StageLogger | None",
) -> tuple[list[ImportTreeSeed], str, list[str]]:
    """Decide which seeds to BFS from for ``feature``.

    Returns ``(seeds, source_kind, anchor_deps)`` where:
      * ``source_kind`` is one of
        ``"flow-based" | "package-anchor" | "schema-source"
          | "structural" | "config" | "none"``.
      * ``anchor_deps`` is populated only for ``package-anchor``.
    """
    seeds: list[ImportTreeSeed] = []

    # CASE D — config-as-product: NO expansion; the manifest IS the feature.
    if _is_config_anchor(feature):
        return ([], "config", [])

    # CASE A — flow-based forward seeds.
    if feature.flows:
        for flow in feature.flows:
            entry_file = flow.entry_point_file
            entry_line = flow.entry_point_line or 1
            if not entry_file:
                continue
            located = _locate_symbol_at_line(cache, entry_file, entry_line)
            if located is None:
                continue
            seeds.append(ImportTreeSeed(
                file=entry_file, symbol=located[0], role="entry",
            ))
        if seeds:
            return (seeds, "flow-based", [])

    # CASE B — package-anchor reverse seeds.
    if _is_package_anchor(feature):
        deps = _parse_anchor_deps(feature.description)
        if deps:
            scope_prefix = _scope_prefix_for(feature)
            consumers = find_consumers_of_module(
                deps,
                ctx.repo_path,
                tracked_files,
                scope_prefix=scope_prefix,
            )
            for file in consumers:
                syms = find_symbols_in_file_using_module(
                    file, deps, ctx.repo_path,
                )
                if not syms:
                    # Whole-file fallback — emit a single seed at
                    # line 1 with a synthetic symbol name; the BFS
                    # records it as ``anchor-consumer`` then expands.
                    seeds.append(ImportTreeSeed(
                        file=file, symbol="<file>", role="anchor-consumer",
                    ))
                    continue
                for sym, _ls, _le in syms:
                    seeds.append(ImportTreeSeed(
                        file=file, symbol=sym, role="anchor-consumer",
                    ))
            if log:
                log.info(
                    f"seed:anchor-reverse feature={feature.name} "
                    f"deps={deps} consumers={len(consumers)} seeds={len(seeds)}",
                )
            return (seeds, "package-anchor", deps)

    # CASE C — schema-source reverse seeds.
    if _is_schema_source(feature) and not seeds:
        schema_files = [p for p in feature.paths if _suffix(p) in {
            ".prisma", ".ts", ".tsx", ".py", ".rb",
        }]
        models: list[str] = []
        for sf in schema_files:
            models.extend(_extract_model_names_from_schema(sf, cache))
        models = list(dict.fromkeys(models))  # dedup, preserve order
        if models:
            # Schema reverse-find uses SYMBOL names (not module names) —
            # the import line ``from "...prisma/client" { User }`` is
            # what we're looking for, regardless of which generated
            # client path bound the symbol.
            consumers = find_consumers_of_symbols(
                models, ctx.repo_path, tracked_files,
            )
            for file in consumers:
                syms = find_symbols_in_file_using_symbols(
                    file, models, ctx.repo_path,
                )
                if not syms:
                    seeds.append(ImportTreeSeed(
                        file=file, symbol="<file>", role="schema-consumer",
                    ))
                    continue
                for sym, _ls, _le in syms:
                    seeds.append(ImportTreeSeed(
                        file=file, symbol=sym, role="schema-consumer",
                    ))
            if log:
                log.info(
                    f"seed:schema-reverse feature={feature.name} "
                    f"models={models[:5]} consumers={len(consumers)} "
                    f"seeds={len(seeds)}",
                )
            return (seeds, "schema-source", [])

    # CASE E — structural fallback for everything else.
    for path in feature.paths[:3]:
        if not path or _suffix(path) not in _SLICEABLE_EXTENSIONS:
            continue
        if _is_vendor_or_test(path):
            continue
        dominant = _find_dominant_symbol(path, cache)
        if dominant is None:
            continue
        seeds.append(ImportTreeSeed(
            file=path, symbol=dominant[0], role="structural",
        ))
    if seeds:
        return (seeds, "structural", [])

    return ([], "none", [])


def _scope_prefix_for(feature: "Feature") -> str | None:
    """For a package-anchor feature whose primary path is a workspace
    root (``apps/web``), return that path as the search scope so the
    reverse-import lookup doesn't drag in cross-workspace consumers.

    Returns ``None`` when ``paths`` is empty or the first path is
    ``"."`` (single-workspace repo — scan everything).
    """
    if not feature.paths:
        return None
    first = feature.paths[0]
    if not first or first in {".", ""}:
        return None
    # If first looks like a single file, use its parent dir.
    if "." in Path(first).name and "/" in first:
        return str(Path(first).parent) + "/"
    if not first.endswith("/"):
        return first + "/"
    return first


# ── BFS traversal ────────────────────────────────────────────────────────


def _traverse(
    seeds: list[ImportTreeSeed],
    *,
    cache: _SourceCache,
    alias_map: list[AliasEntry],
    tracked_files: frozenset[str],
    max_depth: int,
    max_files: int,
    max_symbols: int,
    log: "StageLogger | None",
    feature_name: str,
) -> tuple[list[SymbolAttributionRecord], dict[str, int], int, int, int]:
    """Run the BFS from ``seeds``.

    Returns ``(attributions, depth_distribution, cycles, depth_capped,
    external_skipped)``. ``attributions`` preserves emission order.
    """
    visited: set[tuple[str, str]] = set()
    attributions: list[SymbolAttributionRecord] = []
    depth_dist: dict[int, int] = defaultdict(int)
    queue: deque[tuple[str, str, int, SeedRole]] = deque()
    for seed in seeds:
        queue.append((seed.file, seed.symbol, 0, seed.role))

    cycles = 0
    depth_capped = 0
    external_skipped = 0

    files_seen: set[str] = set()

    while queue:
        if len(attributions) >= max_symbols:
            break
        if len(files_seen) >= max_files:
            break
        file, symbol, depth, role = queue.popleft()
        key = (file, symbol)
        if key in visited:
            cycles += 1
            continue
        if depth > max_depth:
            depth_capped += 1
            continue
        visited.add(key)

        if _is_vendor_or_test(file) or file not in tracked_files:
            external_skipped += 1
            continue

        # Resolve symbol body.
        line_start = 1
        line_end = 1
        if symbol == "<file>":
            line_end = _file_loc(cache, file)
        else:
            located = _locate_symbol_by_name(cache, file, symbol)
            if located is None:
                # Symbol not found — record as whole-file support so
                # the file still surfaces in feature.paths.
                line_end = _file_loc(cache, file)
                symbol = "<file>"
            else:
                line_start, line_end = located

        attributions.append(SymbolAttributionRecord(
            file=file, symbol=symbol,
            line_start=line_start, line_end=line_end,
            role=role,
        ))
        files_seen.add(file)
        depth_dist[depth] += 1
        if log:
            log.emit(
                feature_name,
                f"role={role} file={file} sym={symbol} "
                f"L{line_start}-L{line_end} d={depth}",
            )

        # Expand — only TS/JS/Python files can be sliced for identifier
        # walking. Other languages (Rust / Go) attributed but not
        # expanded — flow_reach already covers their forward case.
        suffix = _suffix(file)
        if suffix not in _SLICEABLE_EXTENSIONS:
            continue

        body = _slice_symbol_body(cache, file, line_start, line_end)
        if not body:
            continue
        imports = cache.imports(file)
        if not imports:
            continue
        ids_in_body = _identifiers_in(body)
        for local_name in ids_in_body:
            if local_name not in imports:
                continue
            import_spec = imports[local_name]
            target_file: str | None = None
            if suffix in _TS_EXTS:
                target_file = resolve_ts_import(
                    file, import_spec,
                    alias_map=alias_map,
                    tracked_files=tracked_files,
                )
                if target_file is None:
                    # Try simple relative ./../ fallback for cases the
                    # alias map didn't cover.
                    target_file = _fallback_relative_resolve(
                        file, import_spec, tracked_files,
                    )
            else:  # py
                target_file = _resolve_py_module_simple(
                    file, import_spec, tracked_files,
                )
            if not target_file:
                external_skipped += 1
                continue
            queue.append((target_file, local_name, depth + 1, "called"))

    return (
        attributions,
        dict(depth_dist),
        cycles,
        depth_capped,
        external_skipped,
    )


def _fallback_relative_resolve(
    importer: str, import_spec: str, tracked_files: frozenset[str],
) -> str | None:
    """A last-ditch relative resolver used when alias_map has no match.

    The legacy ``_resolve_import`` is itself path-alias-aware so we
    don't need it here, but if ``import_spec`` is purely relative
    we can resolve without alias_map.
    """
    if not (import_spec.startswith("./") or import_spec.startswith("../")):
        return None
    import os as _os
    importer_dir = str(Path(importer).parent)
    raw = _os.path.normpath(_os.path.join(importer_dir, import_spec))
    base = raw.replace("\\", "/").lstrip("/")
    if base.startswith(".."):
        return None
    # Try same extension candidates as tsconfig_paths resolver.
    candidates = [
        base, base + ".ts", base + ".tsx", base + ".js", base + ".jsx",
        base + ".mts", base + ".mjs",
        base + "/index.ts", base + "/index.tsx", base + "/index.js",
    ]
    for c in candidates:
        if c in tracked_files:
            return c
    return None


def _resolve_py_module_simple(
    importer: str, module: str, tracked_files: frozenset[str],
) -> str | None:
    """Resolve a Python dotted module path against tracked files.

    Handles relative (``.sibling``) and absolute (``foo.bar.baz``)
    forms. Returns None for stdlib / third-party.
    """
    if not module:
        return None
    leading_dots = 0
    while leading_dots < len(module) and module[leading_dots] == ".":
        leading_dots += 1
    rest = module[leading_dots:]
    if leading_dots > 0:
        importer_dir = Path(importer).parent
        parts = importer_dir.parts
        up = leading_dots - 1
        if up > len(parts):
            return None
        base_parts = parts[: len(parts) - up] if up > 0 else parts
        base = "/".join(base_parts)
    else:
        base = ""
    if rest:
        rest_path = rest.replace(".", "/")
        candidate_stem = f"{base}/{rest_path}" if base else rest_path
    else:
        candidate_stem = base
    candidate_stem = candidate_stem.lstrip("/")
    if not candidate_stem:
        return None
    for c in (f"{candidate_stem}.py", f"{candidate_stem}/__init__.py"):
        if c in tracked_files:
            return c
    return None


# ── Feature mutation helpers ─────────────────────────────────────────────


def _apply_to_feature(
    feature: "Feature",
    attributions: list[SymbolAttributionRecord],
    *,
    seeds: list[ImportTreeSeed],
) -> tuple[int, int]:
    """Mutate ``feature`` to include the enrichment results.

    Returns ``(paths_post, symbols_post)``. ``paths_post`` is the new
    length of ``feature.paths`` AFTER union with reached files.
    """
    from faultline.models.types import (
        FlowSymbolAttribution as PydanticFlowSymbolAttribution,
        SymbolAttribution as PydanticSymbolAttribution,
    )

    # Union the reached files into feature.paths (preserve order:
    # original first, new appended).
    reached_files = []
    seen_paths: set[str] = set(feature.paths)
    for attr in attributions:
        if attr.file not in seen_paths:
            seen_paths.add(attr.file)
            reached_files.append(attr.file)
    feature.paths = list(feature.paths) + reached_files

    # Build SymbolAttribution entries (one per file, aggregating
    # symbols + line ranges). This matches the existing pydantic
    # model shape consumed by the landing app.
    by_file: dict[str, list[SymbolAttributionRecord]] = defaultdict(list)
    for attr in attributions:
        by_file[attr.file].append(attr)

    new_attrs: list[PydanticSymbolAttribution] = []
    for file, recs in by_file.items():
        symbols = [r.symbol for r in recs]
        line_ranges = [(r.line_start, r.line_end) for r in recs]
        attributed = sum(max(r.line_end - r.line_start + 1, 1) for r in recs)
        # Use whole-file LOC when we have it; defensive 0 fallback.
        total = max(line_ranges, key=lambda x: x[1])[1] if line_ranges else 0
        roles = {r.symbol: r.role for r in recs}
        new_attrs.append(PydanticSymbolAttribution(
            file_path=file,
            symbols=symbols,
            line_ranges=line_ranges,
            attributed_lines=attributed,
            total_file_lines=total,
            roles=roles,
        ))

    # Extend, do not replace, so coverage / blame data added by other
    # stages survives.
    existing_files = {a.file_path for a in feature.shared_attributions}
    for a in new_attrs:
        if a.file_path not in existing_files:
            feature.shared_attributions.append(a)
            existing_files.add(a.file_path)

    # Sprint C3b — feature-level per-symbol attributions.
    # The legacy ``shared_attributions`` is a per-file aggregate; the
    # landing app needs per-symbol records (file / symbol / line span /
    # role) at the FEATURE level too, not just per-flow. We emit one
    # ``FlowSymbolAttribution`` per BFS record. For flow-bearing
    # features we ALSO union flow-level called-records (added by
    # :func:`_extend_flow_attributions` below) so a single read of
    # ``feature.symbol_attributions`` yields the complete narrative.
    existing_keys = {
        (a.file, a.symbol) for a in feature.symbol_attributions
    }
    for rec in attributions:
        key = (rec.file, rec.symbol)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        feature.symbol_attributions.append(PydanticFlowSymbolAttribution(
            file=rec.file,
            symbol=rec.symbol,
            line_start=rec.line_start,
            line_end=rec.line_end,
            role=rec.role,
        ))

    # Flow-level extension — when a flow's entry seeded this BFS,
    # extend that flow's paths / flow_symbol_attributions with the
    # called-symbol chain that touches its entry file's subtree.
    if feature.flows:
        _extend_flow_attributions(feature, seeds, attributions)
        # Union flow-level entries back up to the feature surface so
        # downstream consumers don't have to walk both surfaces.
        for flow in feature.flows:
            for fattr in flow.flow_symbol_attributions:
                key = (fattr.file, fattr.symbol)
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                feature.symbol_attributions.append(fattr)

    return (len(feature.paths), len(attributions))


def _extend_flow_attributions(
    feature: "Feature",
    seeds: list[ImportTreeSeed],
    attributions: list[SymbolAttributionRecord],
) -> None:
    """For each seed of role ``entry``, extend its flow with the chain
    of called-symbol attributions reached after it in the BFS.

    The BFS interleaves seeds, so we use entry-file matching as the
    join key. This is approximate when two flows share an entry file
    (rare) but keeps the algorithm linear.
    """
    from faultline.models.types import (
        FlowSymbolAttribution as PydanticFlowSymbolAttribution,
    )

    entry_files = {s.file for s in seeds if s.role == "entry"}
    if not entry_files:
        return

    # Collect ALL called-attributions reachable from any entry file.
    # (We don't track per-seed parentage in the BFS; expanding the
    # full set to each entry-flow is a small over-attribution but
    # honours the user spec — "whole import tree per flow".)
    called = [a for a in attributions if a.role == "called"]
    if not called:
        return

    for flow in feature.flows:
        if flow.entry_point_file not in entry_files:
            continue
        existing_keys = {
            (a.file, a.symbol)
            for a in flow.flow_symbol_attributions
        }
        # Union flow.paths.
        flow_path_set = set(flow.paths or [])
        for attr in called:
            key = (attr.file, attr.symbol)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            flow.flow_symbol_attributions.append(
                PydanticFlowSymbolAttribution(
                    file=attr.file,
                    symbol=attr.symbol,
                    line_start=attr.line_start,
                    line_end=attr.line_end,
                    role="called",
                ),
            )
            if attr.file not in flow_path_set:
                flow_path_set.add(attr.file)
        flow.paths = list(flow.paths or []) + [
            p for p in (a.file for a in called)
            if p not in (flow.paths or [])
        ]


# ── Public entry point ───────────────────────────────────────────────────


# ── Sprint F: per-feature worker (parallelism unit) ─────────────────────


@dataclass
class _PerFeatureResult:
    """Computed enrichment for ONE feature. Apply step still runs on the
    main thread so all feature-list and shared-cache mutations stay
    serial (the BFS itself is the heavy part).
    """

    feature_index: int
    feature_name: str
    source_kind: str
    anchor_deps: list[str]
    seeds: list[ImportTreeSeed]
    attributions: list[SymbolAttributionRecord]
    depth_dist: dict[int, int]
    cycles: int
    depth_capped: int
    external_skipped: int
    paths_pre: int
    symbols_pre: int
    elapsed_ms: int


def _compute_one_feature(
    feature: "Feature",
    feature_index: int,
    *,
    ctx: "ScanContext",
    cache: _SourceCache,
    alias_map: list[AliasEntry],
    tracked_files: frozenset[str],
    max_depth: int,
    max_files_per_feature: int,
    max_symbols_per_feature: int,
    log: "StageLogger | None",
) -> _PerFeatureResult:
    """Pure-compute step: determine seeds + traverse for ONE feature.

    Does NOT mutate the feature (mutation is deferred to the main
    thread via :func:`_apply_to_feature`). Safe to call from a worker
    thread because the only shared state is the read-mostly
    :class:`_SourceCache` (dict get-or-fill — racy writes are fine,
    the second writer just overwrites with identical content) and the
    immutable ``alias_map`` / ``tracked_files``.
    """
    feat_t0 = time.monotonic()
    paths_pre = len(feature.paths)
    symbols_pre = sum(
        len(a.symbols) for a in feature.shared_attributions
    )
    seeds, source_kind, anchor_deps = _determine_seeds(
        feature, ctx, cache, tracked_files, log=log,
    )
    if not seeds:
        return _PerFeatureResult(
            feature_index=feature_index,
            feature_name=feature.name,
            source_kind=source_kind,
            anchor_deps=anchor_deps,
            seeds=[],
            attributions=[],
            depth_dist={},
            cycles=0,
            depth_capped=0,
            external_skipped=0,
            paths_pre=paths_pre,
            symbols_pre=symbols_pre,
            elapsed_ms=int((time.monotonic() - feat_t0) * 1000),
        )

    attributions, depth_dist, fc, dc, es = _traverse(
        seeds,
        cache=cache, alias_map=alias_map,
        tracked_files=tracked_files,
        max_depth=max_depth,
        max_files=max_files_per_feature,
        max_symbols=max_symbols_per_feature,
        log=log,
        feature_name=feature.name,
    )
    return _PerFeatureResult(
        feature_index=feature_index,
        feature_name=feature.name,
        source_kind=source_kind,
        anchor_deps=anchor_deps,
        seeds=seeds,
        attributions=attributions,
        depth_dist=depth_dist,
        cycles=fc,
        depth_capped=dc,
        external_skipped=es,
        paths_pre=paths_pre,
        symbols_pre=symbols_pre,
        elapsed_ms=int((time.monotonic() - feat_t0) * 1000),
    )


def enrich_with_import_tree(
    ctx: "ScanContext",
    features: list["Feature"],
    *,
    log: "StageLogger | None" = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files_per_feature: int = DEFAULT_MAX_FILES_PER_FEATURE,
    max_symbols_per_feature: int = DEFAULT_MAX_SYMBOLS_PER_FEATURE,
    max_workers: int | None = None,
    wall_budget_sec: float | None = None,
) -> EnrichmentResult:
    """Run Stage 6.3 over ``features``.

    Mutates each feature in place; also returns the enriched list for
    callers that prefer pure-functional pipelines. Telemetry is
    captured in :class:`EnrichmentResult` for the artifact writer.

    Sprint F (2026-05-20) parallelism + budget:
      - ``max_workers``: thread-pool size for per-feature BFS. ``None``
        resolves to :data:`DEFAULT_MAX_WORKERS` (=4). Set to 1 to force
        the legacy serial path (used by unit tests that assert ordering).
      - ``wall_budget_sec``: graceful-degradation budget. When the
        stage exceeds this wall time, in-flight features finish but no
        new features are submitted; remaining features are recorded
        with ``source_kind='budget_skipped'`` so the artifact captures
        what was deferred. ``None`` resolves to
        :data:`DEFAULT_WALL_BUDGET_SEC`. Set to 0 to disable the budget
        (legacy behaviour — useful when an external runner already
        enforces a higher ceiling).
    """
    t0 = time.monotonic()
    repo_path = Path(ctx.repo_path)
    tracked_files = frozenset(ctx.tracked_files)
    cache = _SourceCache(repo_path)
    alias_map = build_path_alias_map(repo_path)
    if log:
        log.info(
            f"alias-map-built: {len(alias_map)} entries "
            f"sample={[a.prefix for a in alias_map[:5]]}",
        )

    if max_workers is None:
        # Honour FAULTLINE_STAGE_6_3_WORKERS for ad-hoc overrides.
        env_workers = os.environ.get("FAULTLINE_STAGE_6_3_WORKERS")
        try:
            max_workers = (
                max(1, int(env_workers)) if env_workers
                else DEFAULT_MAX_WORKERS
            )
        except ValueError:
            max_workers = DEFAULT_MAX_WORKERS
    if wall_budget_sec is None:
        env_budget = os.environ.get("FAULTLINE_STAGE_6_3_BUDGET_SEC")
        try:
            wall_budget_sec = (
                float(env_budget) if env_budget else DEFAULT_WALL_BUDGET_SEC
            )
        except ValueError:
            wall_budget_sec = DEFAULT_WALL_BUDGET_SEC

    if log:
        log.info(
            f"concurrency: max_workers={max_workers} "
            f"wall_budget_sec={wall_budget_sec}",
        )

    # Result map keyed by feature index so we can reassemble per_feature
    # in the original order, regardless of thread completion order.
    result_by_index: dict[int, _PerFeatureResult] = {}
    budget_skipped_indices: list[int] = []
    budget_exceeded = False

    def _budget_exhausted() -> bool:
        if wall_budget_sec is None or wall_budget_sec <= 0:
            return False
        return (time.monotonic() - t0) >= wall_budget_sec

    if max_workers <= 1:
        # Legacy serial path — preserves exact behaviour for tests that
        # rely on deterministic ordering of log emit events.
        for index, feature in enumerate(features):
            if _budget_exhausted():
                budget_exceeded = True
                budget_skipped_indices.append(index)
                continue
            result_by_index[index] = _compute_one_feature(
                feature, index,
                ctx=ctx, cache=cache, alias_map=alias_map,
                tracked_files=tracked_files,
                max_depth=max_depth,
                max_files_per_feature=max_files_per_feature,
                max_symbols_per_feature=max_symbols_per_feature,
                log=log,
            )
    else:
        # Parallel path — submit all features up front; check the
        # budget on each completion. Once exhausted, mark every still-
        # pending feature as ``budget_skipped`` (we cannot cancel an
        # in-flight thread but the ``Future.cancel`` call will succeed
        # for any task the executor has not yet started).
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="stage6_3",
        ) as pool:
            future_to_index = {
                pool.submit(
                    _compute_one_feature,
                    feature, index,
                    ctx=ctx, cache=cache, alias_map=alias_map,
                    tracked_files=tracked_files,
                    max_depth=max_depth,
                    max_files_per_feature=max_files_per_feature,
                    max_symbols_per_feature=max_symbols_per_feature,
                    log=log,
                ): index
                for index, feature in enumerate(features)
            }
            from concurrent.futures import CancelledError

            for fut in as_completed(future_to_index):
                index = future_to_index[fut]
                try:
                    result_by_index[index] = fut.result()
                except CancelledError:
                    # Future was cancelled by the budget-exhausted
                    # branch below; treat as a budget skip, NOT an
                    # error. The apply phase recognises a None entry
                    # in result_by_index as ``budget_skipped`` so we
                    # leave this index unset and continue.
                    continue
                except Exception as exc:  # noqa: BLE001 — defensive
                    # A worker exception should never break Stage 6.3.
                    # Record a no-op enrichment for the feature so the
                    # rest of the pipeline keeps moving.
                    feature = features[index]
                    result_by_index[index] = _PerFeatureResult(
                        feature_index=index,
                        feature_name=feature.name,
                        source_kind="error",
                        anchor_deps=[],
                        seeds=[],
                        attributions=[],
                        depth_dist={},
                        cycles=0,
                        depth_capped=0,
                        external_skipped=0,
                        paths_pre=len(feature.paths),
                        symbols_pre=sum(
                            len(a.symbols) for a in feature.shared_attributions
                        ),
                        elapsed_ms=0,
                    )
                    if log:
                        log.warn(
                            f"feature-worker-error name={feature.name} "
                            f"{type(exc).__name__}: {exc}",
                        )
                    logger.warning(
                        "stage_6_3 worker raised", exc_info=True,
                    )
                if not budget_exceeded and _budget_exhausted():
                    budget_exceeded = True
                    # Cancel everything not yet running.
                    for pending_fut, pending_idx in future_to_index.items():
                        if (
                            pending_idx not in result_by_index
                            and not pending_fut.done()
                        ):
                            if pending_fut.cancel():
                                budget_skipped_indices.append(pending_idx)
            # After the with-block exits, any remaining future MUST be
            # complete; if it isn't (e.g. cancel returned False), we
            # already have a result. Index gaps remain only for
            # cancelled ones, which were appended above.

    # ── Apply phase (single-threaded) ────────────────────────────────
    # Iterate in original feature order so per_feature telemetry stays
    # stable + log emits stay ordered.
    per_feature: list[FeatureEnrichment] = []
    total_seeds = 0
    total_files_reached = 0
    total_symbols_emitted = 0
    cycles = 0
    depth_capped = 0
    external_skipped = 0

    for index, feature in enumerate(features):
        result = result_by_index.get(index)
        if result is None:
            # Budget-skipped feature — record a placeholder.
            per_feature.append(FeatureEnrichment(
                feature_name=feature.name,
                source_kind="budget_skipped",
                anchor_deps=[],
                seeds_count=0,
                paths_pre=len(feature.paths),
                paths_post=len(feature.paths),
                symbols_pre=sum(
                    len(a.symbols) for a in feature.shared_attributions
                ),
                symbols_post=sum(
                    len(a.symbols) for a in feature.shared_attributions
                ),
                elapsed_ms=0,
            ))
            if log:
                log.warn(
                    f"feature-budget-skipped name={feature.name} "
                    f"budget_sec={wall_budget_sec}",
                )
            continue

        if not result.seeds:
            per_feature.append(FeatureEnrichment(
                feature_name=feature.name,
                source_kind=result.source_kind,
                anchor_deps=result.anchor_deps,
                seeds_count=0,
                paths_pre=result.paths_pre,
                paths_post=result.paths_pre,
                symbols_pre=result.symbols_pre,
                symbols_post=result.symbols_pre,
                elapsed_ms=result.elapsed_ms,
            ))
            if log:
                log.info(
                    f"feature-skip name={feature.name} "
                    f"source_kind={result.source_kind} no-seeds",
                )
            continue

        cycles += result.cycles
        depth_capped += result.depth_capped
        external_skipped += result.external_skipped

        paths_post, symbols_post = _apply_to_feature(
            feature, result.attributions, seeds=result.seeds,
        )

        seeds_sample = [
            {"file": s.file, "symbol": s.symbol, "role": s.role}
            for s in result.seeds[:5]
        ]
        per_feature.append(FeatureEnrichment(
            feature_name=feature.name,
            source_kind=result.source_kind,
            anchor_deps=result.anchor_deps,
            seeds_count=len(result.seeds),
            seeds_sample=seeds_sample,
            paths_pre=result.paths_pre,
            paths_post=paths_post,
            symbols_pre=result.symbols_pre,
            symbols_post=symbols_post,
            depth_distribution=result.depth_dist,
            elapsed_ms=result.elapsed_ms,
        ))
        total_seeds += len(result.seeds)
        total_files_reached += paths_post - result.paths_pre
        total_symbols_emitted += len(result.attributions)
        if log:
            log.info(
                f"feature-end name={feature.name} "
                f"source_kind={result.source_kind} "
                f"seeds={len(result.seeds)} "
                f"symbols_emitted={len(result.attributions)} "
                f"paths={result.paths_pre}→{paths_post}",
            )

    elapsed = round(time.monotonic() - t0, 3)
    if budget_exceeded and log:
        log.warn(
            f"stage_6_3_budget_exceeded budget_sec={wall_budget_sec} "
            f"elapsed_sec={elapsed} features_skipped="
            f"{len(budget_skipped_indices)}",
        )
    return EnrichmentResult(
        enriched_features=features,
        per_feature=per_feature,
        total_seeds=total_seeds,
        total_files_reached=total_files_reached,
        total_symbols_emitted=total_symbols_emitted,
        cycles_detected=cycles,
        depth_capped_events=depth_capped,
        external_skipped=external_skipped,
        cache_hits=cache.hits,
        alias_map=alias_map,
        elapsed_sec=elapsed,
        budget_exceeded=budget_exceeded,
        budget_sec=wall_budget_sec or 0.0,
        features_budget_skipped=len(budget_skipped_indices),
        max_workers=max_workers,
    )


def build_artifact_payload(
    result: EnrichmentResult,
    *,
    max_depth: int,
    max_files_per_feature: int,
    max_symbols_per_feature: int,
) -> dict[str, Any]:
    """Build the JSON payload written to
    ``<run_dir>/06.3-stage-import-tree.json``.
    """
    return {
        "stage": "6.3-import-tree",
        "elapsed_sec": result.elapsed_sec,
        "config": {
            "max_depth": max_depth,
            "max_files_per_feature": max_files_per_feature,
            "max_symbols_per_feature": max_symbols_per_feature,
        },
        "alias_map_size": len(result.alias_map),
        "alias_map_sample": [
            {
                "alias_prefix": a.prefix,
                "workspace": a.workspace_root,
                "target": a.target_prefix,
            }
            for a in result.alias_map[:10]
        ],
        "features": [
            {
                "name": fe.feature_name,
                "source_kind": fe.source_kind,
                "anchor_deps": fe.anchor_deps,
                "seeds_count": fe.seeds_count,
                "seeds_sample": fe.seeds_sample,
                "paths_pre": fe.paths_pre,
                "paths_post": fe.paths_post,
                "symbols_pre": fe.symbols_pre,
                "symbols_post": fe.symbols_post,
                "depth_distribution": {
                    str(k): v for k, v in fe.depth_distribution.items()
                },
                "elapsed_ms": fe.elapsed_ms,
            }
            for fe in result.per_feature
        ],
        "aggregate": {
            "total_seeds": result.total_seeds,
            "total_files_reached": result.total_files_reached,
            "total_symbols_emitted": result.total_symbols_emitted,
            "cycles_detected": result.cycles_detected,
            "depth_capped_events": result.depth_capped_events,
            "external_skipped": result.external_skipped,
            "cache_hits": result.cache_hits,
        },
        # Sprint F (2026-05-20) — concurrency + budget telemetry.
        "concurrency": {
            "max_workers": result.max_workers,
            "budget_sec": result.budget_sec,
            "budget_exceeded": result.budget_exceeded,
            "features_budget_skipped": result.features_budget_skipped,
        },
    }


__all__ = [
    "ImportTreeSeed",
    "SymbolAttributionRecord",
    "FeatureEnrichment",
    "EnrichmentResult",
    "enrich_with_import_tree",
    "build_artifact_payload",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FILES_PER_FEATURE",
    "DEFAULT_MAX_SYMBOLS_PER_FEATURE",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_WALL_BUDGET_SEC",
]
