"""Stage 3 — per-flow symbol attribution (deterministic, NO LLM).

Extends Sprint C1's file-level call-graph reach with LINE-LEVEL
attribution: for each flow, walk from the entry symbol outward and
emit one ``FlowSymbolAttribution`` per called function with its exact
line range. Files in the C1 reach set that we couldn't resolve at the
symbol grain are emitted as ``role="support"`` with the whole-file
range so no information is lost.

Why this exists
===============

Per ``flow-feature-concept`` skill (authoritative spec): a flow does
NOT just list files — it lists, per file, the EXACT line ranges that
participate in this specific narrative. Sprint C1 added file-level
reach; this sprint adds the line-level attribution the model demands.

Algorithm
=========

  1. **Identify the entry symbol.** Open ``entry_file``, find the
     function whose body contains ``entry_line``. Language-aware regex
     over function/method/arrow definitions; balanced-brace counting
     for TS/JS/Go/Rust; indent tracking for Python.

  2. **Walk imports/calls from the entry-symbol body.** Slice the
     entry-symbol text (lines ``line_start..line_end``). For each
     identifier in that slice that matches a name imported into
     ``entry_file``, resolve the import to a target file (reuse the
     C1 ``flow_reach`` resolvers), find the exported symbol with that
     name in the target, emit a ``called`` attribution.

  3. **Mark file-level support.** For any file in ``reached_paths``
     not covered by step 2, emit ONE ``support`` attribution spanning
     the whole file (``line_start=1``, ``line_end=LOC``).

Caps + safety
=============

  - ``max_symbols_per_flow`` (default 12) — total attributions
    INCLUDING the entry. Bounded payload size for the landing JSON.
  - On any malformed input (regex no match, unbalanced braces, file
    unreadable) the function logs telemetry and continues; it never
    raises.
  - Scale-invariant: identical bounds for every language and every
    repo size (per ``rule-no-magic-tuning``).

Public shape
============

A flat ``tuple[FlowSymbolAttribution, ...]``. The orchestrator's
Stage 3 post-pass writes this onto ``FlowSpec.symbol_attributions``;
Stage 5 then bridges it onto the public :class:`Flow` model.

NO LLM. Pure structural parsing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from faultline.pipeline_v2.flow_reach import ReachContext

logger = logging.getLogger(__name__)


# ── Public result shape ──────────────────────────────────────────────────


SymbolRole = Literal["entry", "called", "support"]


@dataclass(frozen=True)
class FlowSymbolAttribution:
    """One line-range attribution for a flow.

    Attributes:
        file: repo-relative path.
        symbol: function/method/exported name; ``"<file>"`` when the
            attribution covers the whole file (``role="support"``).
        line_start: 1-indexed, inclusive.
        line_end: 1-indexed, inclusive.
        role: ``"entry"`` (the flow's entry function),
            ``"called"`` (function reached via import from the entry),
            ``"support"`` (file in C1 reach set with no symbol resolution).
    """

    file: str
    symbol: str
    line_start: int
    line_end: int
    role: SymbolRole


# ── Tunables (scale-invariant) ───────────────────────────────────────────

DEFAULT_MAX_SYMBOLS_PER_FLOW = 12

# Keywords that should NEVER be treated as function/method names.
# The regex patterns are intentionally permissive (a class method
# matcher would otherwise need to know about every control-flow
# keyword); we filter post-match. Identical set for every language —
# names like ``if``, ``for``, ``while``, ``switch`` never sensibly
# appear as function definitions and would be false positives.
_KEYWORD_BLOCKLIST: frozenset[str] = frozenset({
    "if", "else", "for", "while", "switch", "do", "try", "catch",
    "finally", "return", "throw", "yield", "await", "break",
    "continue", "case", "default", "new", "typeof", "delete", "void",
    "in", "of", "as", "with", "function", "class", "interface",
    "type", "enum", "import", "export", "from", "match", "where",
    "let", "var", "const", "use", "pub", "fn", "impl", "mod", "ref",
    "self", "super", "this", "true", "false", "null", "undefined",
    "and", "or", "not", "is",
})

# Maximum line range scan for a single function body. Pathological
# files (massive 10k-line minified bundles) shouldn't blow up the
# linear walk. 5000 is generous — real source files almost never have
# a single function over that size.
_MAX_BODY_LINE_SCAN = 5000


# ── Language detection ───────────────────────────────────────────────────


_TS_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_PY_EXTS = (".py",)
_GO_EXTS = (".go",)
_RUST_EXTS = (".rs",)


def _language_for(path: str) -> str | None:
    """Return ``"ts" | "py" | "go" | "rs"`` or None for unsupported."""
    lower = path.lower()
    if lower.endswith(_TS_JS_EXTS):
        return "ts"
    if lower.endswith(_PY_EXTS):
        return "py"
    if lower.endswith(_GO_EXTS):
        return "go"
    if lower.endswith(_RUST_EXTS):
        return "rs"
    return None


# ── Function-definition regexes per language ─────────────────────────────

# All patterns capture (group 1) the symbol NAME. They MUST match the
# start of the definition line so we can compute line_start from the
# match offset.

# TS/JS — function declarations, arrow functions, route-handler exports,
# default exports, and class methods.
#
# Named function: ``export async function foo(...)``, ``function foo(...)``.
_TS_NAMED_FN = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:async\s+)?"
    r"function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"(?:<[^>]*>)?\s*"
    r"\(",
    re.MULTILINE,
)
# Arrow / function-expression bound to const/let/var:
# ``export const handle = async (req) => { ... }`` / ``= function ()``
_TS_ARROW_OR_EXPR = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:export\s+)?"
    r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"(?::\s*[^=]+)?"
    r"=\s*"
    r"(?:async\s+)?"
    r"(?:\([^)]*\)\s*=>|function\b|\([^)]*\)\s*:)",
    re.MULTILINE,
)
# Generic ``export const NAME = expr(...)`` — catches schema / config
# declarations whose value is a factory call (Drizzle ``sqliteTable``,
# Zod schemas, tRPC routers, etc.). The body span is computed by
# balanced-paren counting at the call site so we capture multi-line
# table definitions correctly. Lower priority than the arrow/function
# pattern (added AFTER it) so explicit handlers win.
_TS_CONST_CALL = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"(?::\s*[^=]+)?"
    r"=\s*"
    r"(?:new\s+)?"
    r"[A-Za-z_$][\w$.]*\s*"
    r"[(<]",
    re.MULTILINE,
)
# Next.js / generic route-handler export: ``export async function GET(req)``.
_TS_ROUTE_EXPORT = re.compile(
    r"^(?P<indent>\s*)"
    r"export\s+(?:default\s+)?(?:async\s+)?function\s+"
    r"(?P<name>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*"
    r"\(",
    re.MULTILINE,
)
# Class method: ``  async foo(arg): RetType {`` — must be inside a class.
# We don't enforce that; we just look for the bare method-shape line.
_TS_CLASS_METHOD = re.compile(
    r"^(?P<indent>\s{2,})"
    r"(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|override\s+)*"
    r"(?:async\s+)?"
    r"\*?(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"(?:<[^>]*>)?\s*"
    r"\([^)]*\)\s*"
    r"(?::\s*[^{=]+)?\s*\{",
    re.MULTILINE,
)
# TS class / interface / type declarations: ``export class Foo``,
# ``export interface Foo``, ``export type Foo``. Used as fallback
# entry attributions when Stage 3 grounded a flow on a type symbol.
_TS_TYPE_DECL = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:abstract\s+)?"
    r"(?:class|interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)\b",
    re.MULTILINE,
)

_TS_PATTERNS: tuple[re.Pattern[str], ...] = (
    _TS_ROUTE_EXPORT,
    _TS_NAMED_FN,
    _TS_ARROW_OR_EXPR,
    _TS_CLASS_METHOD,
    _TS_TYPE_DECL,
    _TS_CONST_CALL,
)

# Python — ``def NAME`` / ``async def NAME``. Match indent so class
# methods are detected too; end-of-function is computed by indent
# tracking, not regex.
_PY_FN = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:async\s+)?def\s+(?P<name>[A-Za-z_][\w]*)\s*"
    r"\(",
    re.MULTILINE,
)
_PY_PATTERNS: tuple[re.Pattern[str], ...] = (_PY_FN,)

# Go — ``func (recv X) NAME(...)`` or ``func NAME(...)``.
_GO_FN = re.compile(
    r"^(?P<indent>\s*)"
    r"func\s*"
    r"(?:\([^)]*\)\s+)?"
    r"(?P<name>[A-Za-z_][\w]*)\s*"
    r"(?:<[^>]*>)?\s*"
    r"\(",
    re.MULTILINE,
)
# Go type declarations: ``type Foo struct { ... }`` / ``type Foo interface {``.
_GO_TYPE = re.compile(
    r"^(?P<indent>\s*)"
    r"type\s+(?P<name>[A-Za-z_][\w]*)\s+"
    r"(?:struct|interface|=\s*\S)",
    re.MULTILINE,
)
_GO_PATTERNS: tuple[re.Pattern[str], ...] = (_GO_FN, _GO_TYPE)

# Rust — ``pub fn NAME``, ``fn NAME``, ``pub async fn NAME``.
_RUST_FN = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:async\s+)?"
    r"(?:unsafe\s+)?"
    r"(?:const\s+)?"
    r"fn\s+(?P<name>[A-Za-z_][\w]*)\s*"
    r"(?:<[^>]*>)?\s*"
    r"\(",
    re.MULTILINE,
)
# Rust types: ``pub struct NAME``, ``pub enum NAME``, ``pub trait NAME``.
# Stage 3 often grounds flows on struct definitions in Rust (each
# struct is the "entry point" to its impl block). Capturing the struct
# definition gives us an entry attribution covering the struct body.
_RUST_TYPE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:struct|enum|trait|union)\s+(?P<name>[A-Za-z_][\w]*)\b",
    re.MULTILINE,
)
# Rust impl blocks: ``impl<'c, C> Chat<'c, C> { ... }`` — captures
# Chat as the symbol name; the body extends over the whole impl block.
_RUST_IMPL = re.compile(
    r"^(?P<indent>\s*)"
    r"impl\s*"
    r"(?:<[^>]*>)?\s*"
    r"(?:[A-Za-z_][\w:]*\s+for\s+)?"
    r"(?P<name>[A-Za-z_][\w]*)\b",
    re.MULTILINE,
)
_RUST_PATTERNS: tuple[re.Pattern[str], ...] = (
    _RUST_FN, _RUST_TYPE, _RUST_IMPL,
)


def _patterns_for(lang: str) -> tuple[re.Pattern[str], ...]:
    if lang == "ts":
        return _TS_PATTERNS
    if lang == "py":
        return _PY_PATTERNS
    if lang == "go":
        return _GO_PATTERNS
    if lang == "rs":
        return _RUST_PATTERNS
    return ()


# ── Function body extent ─────────────────────────────────────────────────


def _line_starts(source: str) -> list[int]:
    """Return list of character offsets where each (1-indexed) line begins.

    ``source[line_starts[i]]`` is the first char of line ``i + 1``.
    Used to translate match offsets → line numbers in O(log n).
    """
    starts = [0]
    for i, c in enumerate(source):
        if c == "\n":
            starts.append(i + 1)
    return starts


def _offset_to_line(offset: int, line_starts: list[int]) -> int:
    """Binary search ``offset`` → 1-indexed line."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1  # 1-indexed


def _find_brace_end(
    source: str, body_start_offset: int,
) -> int:
    """Return source offset of the closing ``}`` matching the first ``{``
    at or after ``body_start_offset``.

    Returns ``-1`` if no balanced match found within
    ``_MAX_BODY_LINE_SCAN`` newlines after the opening brace. Handles
    quoted strings ('', "", ``) and ``//`` + ``/* */`` comments so
    braces inside string literals or comments don't desync the count.

    Cheap enough — single linear pass.
    """
    n = len(source)
    # Locate the opening brace.
    i = body_start_offset
    while i < n and source[i] != "{":
        # Some declarations (arrow fns) might have ``=>`` then an
        # expression with no brace — caller should detect via "no {"
        # within a few chars and treat the whole one-liner accordingly.
        if source[i] == "\n":
            # Don't search across more than a few lines for the opening.
            # Bail if the open brace isn't near the def keyword.
            pass
        i += 1
    if i >= n:
        return -1

    depth = 0
    in_str: str | None = None  # quote char, or None
    in_line_comment = False
    in_block_comment = False
    newlines_seen = 0
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                newlines_seen += 1
                if newlines_seen > _MAX_BODY_LINE_SCAN:
                    return -1
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            if c == "\n":
                newlines_seen += 1
                if newlines_seen > _MAX_BODY_LINE_SCAN:
                    return -1
            i += 1
            continue
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            if c == "\n":
                newlines_seen += 1
                if newlines_seen > _MAX_BODY_LINE_SCAN:
                    return -1
            i += 1
            continue

        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if c in ("'", '"', "`"):
            in_str = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        if c == "\n":
            newlines_seen += 1
            if newlines_seen > _MAX_BODY_LINE_SCAN:
                return -1
        i += 1
    return -1


def _python_body_end_line(
    source_lines: list[str], def_line: int, def_indent: int,
) -> int:
    """Return the 1-indexed end line of a Python function whose ``def``
    starts at line ``def_line`` (1-indexed) with leading-whitespace count
    ``def_indent``.

    Walks forward until the first non-blank line whose indent is
    ``<= def_indent``. The function body's last line is the line BEFORE
    that. EOF terminates the function.
    """
    total = len(source_lines)
    last_content_line = def_line  # at minimum, just the def line
    # Start from the line AFTER def_line (1-indexed → list index def_line).
    i = def_line  # def_line is 1-indexed; list index def_line == line def_line+1
    scanned = 0
    while i < total and scanned < _MAX_BODY_LINE_SCAN:
        raw = source_lines[i]
        scanned += 1
        i += 1
        stripped = raw.strip()
        if not stripped:
            continue  # blank lines belong to whichever block; keep walking
        # Compute leading indent in spaces (treat tab as 1 — Python style
        # check is fine as a heuristic; production code is consistent).
        indent = len(raw) - len(raw.lstrip(" \t"))
        if indent <= def_indent:
            # Next sibling/parent — the function ended.
            return last_content_line
        last_content_line = i  # i is now AFTER the increment, 1-indexed
    return last_content_line


# ── Function table builder ───────────────────────────────────────────────


@dataclass(frozen=True)
class _FunctionEntry:
    name: str
    line_start: int
    line_end: int
    body_start_offset: int  # for downstream slicing


def _enumerate_functions(
    source: str, lang: str,
) -> list[_FunctionEntry]:
    """Return all top-level + method-level functions in ``source``.

    Stable order (first match wins). Best-effort: malformed source
    yields an empty list rather than raising.
    """
    patterns = _patterns_for(lang)
    if not patterns:
        return []
    line_starts = _line_starts(source)
    source_lines = source.splitlines()
    out: list[_FunctionEntry] = []
    seen_starts: set[int] = set()

    for pat in patterns:
        for m in pat.finditer(source):
            # Use the position of the NAME capture group to compute
            # line_start so regex patterns that include ``^\s*`` don't
            # report a leading-blank-line offset (the ``^`` after a
            # newline matches before the actual content). For Python
            # we use the def keyword position via ``indent`` group
            # length subtracted from the name offset.
            name_start = m.start("name")
            line_start = _offset_to_line(name_start, line_starts)
            if line_start in seen_starts:
                continue
            name = m.group("name")
            # Drop control-flow keywords matched by permissive patterns.
            if name in _KEYWORD_BLOCKLIST:
                continue
            seen_starts.add(line_start)
            indent_str = m.group("indent") or ""
            indent_n = len(indent_str)

            if lang == "py":
                # Python: indent-tracked end. The def line itself is
                # 1-indexed line_start; the body starts on the next line.
                end_line = _python_body_end_line(
                    source_lines, line_start, indent_n,
                )
                # body_start_offset: start of the line AFTER the def.
                next_line_idx = line_start  # 1-indexed line_start → next is line_start+1, list[line_start]
                body_offset = (
                    line_starts[next_line_idx]
                    if next_line_idx < len(line_starts) else len(source)
                )
                out.append(_FunctionEntry(
                    name=name,
                    line_start=line_start,
                    line_end=end_line,
                    body_start_offset=body_offset,
                ))
                continue

            # TS/JS/Go/Rust: balanced-brace end. Search for opening
            # brace after the match end. If no brace within a small
            # window, treat as single-line (e.g. arrow with no block:
            # ``const f = (x) => x + 1``).
            close = _find_brace_end(source, m.end())
            if close < 0:
                # Arrow without block — endpoint is end of statement
                # (next semicolon or newline). Cheap: scan to the next
                # newline since arrow one-liners are by definition
                # single-line.
                # Find end of current line.
                nl_idx = source.find("\n", m.end())
                if nl_idx < 0:
                    nl_idx = len(source)
                end_line = _offset_to_line(nl_idx, line_starts)
                body_offset = m.end()
            else:
                end_line = _offset_to_line(close, line_starts)
                body_offset = m.end()
            out.append(_FunctionEntry(
                name=name,
                line_start=line_start,
                line_end=end_line,
                body_start_offset=body_offset,
            ))

    # Sort by line_start for stable downstream lookup.
    out.sort(key=lambda e: e.line_start)
    return out


# ── Entry-symbol resolution ──────────────────────────────────────────────


def _resolve_entry_symbol(
    source: str, lang: str, entry_line: int,
) -> _FunctionEntry | None:
    """Return the function whose body contains ``entry_line``.

    When multiple functions contain the line (nested closures), prefers
    the INNERMOST one (smallest range). Returns None when no function
    matches — caller is responsible for the fallback role.
    """
    fns = _enumerate_functions(source, lang)
    if not fns:
        return None
    candidates = [
        f for f in fns
        if f.line_start <= entry_line <= f.line_end
    ]
    if not candidates:
        # Sometimes the LLM emits an entry_line that lands ON the def
        # line of an exported symbol the regex slightly missed due to
        # decorators. Try a small +/- window.
        candidates = [
            f for f in fns
            if abs(f.line_start - entry_line) <= 2
        ]
    if not candidates:
        return None
    # Innermost = smallest range.
    candidates.sort(key=lambda f: f.line_end - f.line_start)
    return candidates[0]


# ── Import name extraction (per language) ────────────────────────────────


# TS/JS: ``import { a, b as c } from "./x"`` / ``import x from "./y"`` /
# ``import * as ns from "./z"`` / ``const { a } = require("./z")``.
_TS_IMPORT_NAMED = re.compile(
    r"^\s*import\s+(?:type\s+)?\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_TS_IMPORT_DEFAULT = re.compile(
    r"^\s*import\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_TS_IMPORT_NAMESPACE = re.compile(
    r"^\s*import\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_TS_IMPORT_DEFAULT_NAMED = re.compile(
    r"^\s*import\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s*,\s*\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)

# Python: ``from x import a, b as c``.
_PY_IMPORT_FROM = re.compile(
    r"^\s*from\s+([.\w]+)\s+import\s+(.+)$",
    re.MULTILINE,
)


def _extract_ts_imports(source: str) -> dict[str, str]:
    """Return ``{local_name: import_specifier}`` for a TS/JS source.

    ``import { a, b as c } from "./x"`` produces ``{"a": "./x", "c": "./x"}``.
    ``import x from "./y"`` produces ``{"x": "./y"}``.
    """
    out: dict[str, str] = {}
    # Combined ``default, { named }`` first so it doesn't get consumed
    # by the simpler default pattern.
    for m in _TS_IMPORT_DEFAULT_NAMED.finditer(source):
        default_name = m.group(1).strip()
        names_blob = m.group(2)
        spec = m.group(3)
        out[default_name] = spec
        for piece in names_blob.split(","):
            local = _ts_alias_of(piece)
            if local:
                out[local] = spec
    for m in _TS_IMPORT_NAMED.finditer(source):
        names_blob = m.group(1)
        spec = m.group(2)
        for piece in names_blob.split(","):
            local = _ts_alias_of(piece)
            if local:
                out[local] = spec
    for m in _TS_IMPORT_DEFAULT.finditer(source):
        local = m.group(1).strip()
        spec = m.group(2)
        if local and local not in out:
            out[local] = spec
    for m in _TS_IMPORT_NAMESPACE.finditer(source):
        local = m.group(1).strip()
        spec = m.group(2)
        out[local] = spec
    return out


def _ts_alias_of(piece: str) -> str | None:
    """``"foo as bar"`` → ``"bar"``; ``"foo"`` → ``"foo"``. None on garbage."""
    s = piece.strip()
    if not s:
        return None
    # Strip ``type`` keyword for type-only named imports.
    if s.startswith("type "):
        s = s[5:].strip()
    if " as " in s:
        s = s.split(" as ", 1)[1].strip()
    if not s or not re.match(r"^[A-Za-z_$][\w$]*$", s):
        return None
    return s


def _extract_py_imports(source: str) -> dict[str, str]:
    """Return ``{local_name: dotted_module}`` for ``from x import a, b``.

    Doesn't handle bare ``import x`` (those create namespace bindings,
    not callable symbols — calls become ``x.foo()`` which we don't
    follow in this version).
    """
    out: dict[str, str] = {}
    for m in _PY_IMPORT_FROM.finditer(source):
        module = m.group(1).strip()
        names_blob = m.group(2)
        # Strip trailing comment.
        names_blob = names_blob.split("#", 1)[0]
        # Drop optional parens around multi-line imports.
        names_blob = names_blob.strip().strip("()")
        for piece in names_blob.split(","):
            s = piece.strip()
            if not s:
                continue
            if " as " in s:
                s = s.split(" as ", 1)[1].strip()
            if re.match(r"^[A-Za-z_][\w]*$", s):
                out[s] = module
    return out


# Go: identifiers used in a body that come from a dotted package call
# (e.g. ``pkg.Foo()``) — we don't follow these to the file level via
# regex (Go resolution is package-grained); calls within the same
# package look like ``Foo(...)`` and resolve via the same package's
# file set.
#
# Rust: ``use crate::foo::bar`` brings ``bar`` into local scope; ``mod``
# brings submodule scope. We don't fully follow Rust call graphs here;
# we rely on the C1 reach set + support attribution for these langs.


# ── Identifier extraction from a function body ───────────────────────────


_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
# Pulls every bare identifier (not preceded by a ``.``) — used to
# catch namespace access like ``prisma.user.findMany()`` where the
# CALL is on ``findMany`` but the import-resolution key is ``prisma``.
_BARE_IDENTIFIER_RE = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\b")


def _identifiers_called_in(
    body_text: str,
) -> list[str]:
    """Pull identifiers that look like CALLS (``foo(...)``).

    Returns in first-occurrence order without duplicates. Cheap regex
    — false positives (e.g. ``if (cond)`` matches ``if``) are filtered
    downstream by the keyword blocklist and by import-table lookup.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _IDENTIFIER_RE.finditer(body_text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _bare_identifiers_in(body_text: str) -> list[str]:
    """Pull every BARE identifier (not after a dot or word char).

    Catches namespace usage: ``prisma.user.findMany()`` → ``prisma``.
    Used as a secondary lookup against the import table to attribute
    files reached via namespace methods rather than direct calls.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _BARE_IDENTIFIER_RE.finditer(body_text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# ── Resolve import specifier → target file ───────────────────────────────


def _resolve_ts_specifier(
    rctx: "ReachContext",
    importer: str,
    specifier: str,
) -> str | None:
    """Use C1's TS resolver to map ``import ... from "spec"`` to a file."""
    from faultline.analyzer.import_graph import _resolve_import

    try:
        return _resolve_import(
            importer, specifier, rctx.file_set,
            alias_map=rctx.alias_map,
            monorepo_packages=rctx.monorepo_packages,
            workspace_package_map=getattr(rctx, "workspace_package_map", None),
            repo_root=str(rctx.repo_path),
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


def _resolve_py_module(
    rctx: "ReachContext",
    importer: str,
    module: str,
) -> str | None:
    """Reuse C1's Python module resolver."""
    from faultline.pipeline_v2.flow_reach import _resolve_python_module

    try:
        return _resolve_python_module(
            importer, module, rctx.file_set,
            source_roots=getattr(rctx, "python_source_roots", ("",)),
        )
    except Exception:  # noqa: BLE001
        return None


# ── File loading + LOC counting ──────────────────────────────────────────


def _read_text(repo_path: Path, rel: str) -> str | None:
    try:
        return (repo_path / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _file_loc(text: str) -> int:
    """Number of lines (1-indexed end line) for a file's content.

    Empty file → 1 (the line range ``(1, 1)`` is a stable placeholder).
    """
    if not text:
        return 1
    return text.count("\n") + (0 if text.endswith("\n") else 1) or 1


# ── Public entry point ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolAttributionResult:
    """Output bundle from :func:`compute_flow_symbols`.

    ``attributions`` is the per-file/symbol list; the boolean flag is
    surfaced as telemetry so the orchestrator can compute the
    repo-level failure rate.
    """

    attributions: tuple[FlowSymbolAttribution, ...]
    entry_detection_failed: bool


def compute_flow_symbols(
    rctx: "ReachContext",
    entry_file: str,
    entry_line: int,
    reached_paths: tuple[str, ...],
    *,
    max_symbols_per_flow: int = DEFAULT_MAX_SYMBOLS_PER_FLOW,
) -> SymbolAttributionResult:
    """Walk from the entry-symbol outward, attributing line ranges.

    Args:
        rctx: Reach context built once per scan (reused).
        entry_file: repo-relative path holding the flow's entry symbol.
        entry_line: 1-indexed line within ``entry_file``.
        reached_paths: C1 reach output. Used to populate ``support``
            attributions for files we don't reach at the symbol grain.
        max_symbols_per_flow: hard cap on emitted attributions.

    Returns:
        :class:`SymbolAttributionResult` — never raises.
    """
    if max_symbols_per_flow < 1:
        max_symbols_per_flow = 1

    out: list[FlowSymbolAttribution] = []
    seen_files_with_symbol: set[str] = set()
    entry_detection_failed = False

    # ── Step 1 — entry symbol ──────────────────────────────────────
    entry_text = _read_text(rctx.repo_path, entry_file)
    entry_lang = _language_for(entry_file)
    entry_fn: _FunctionEntry | None = None
    if entry_text is not None and entry_lang is not None:
        entry_fn = _resolve_entry_symbol(entry_text, entry_lang, entry_line)

    if entry_fn is not None:
        out.append(FlowSymbolAttribution(
            file=entry_file,
            symbol=entry_fn.name,
            line_start=entry_fn.line_start,
            line_end=entry_fn.line_end,
            role="entry",
        ))
        seen_files_with_symbol.add(entry_file)
    else:
        # Fallback: wrap the whole entry file. Mark telemetry.
        entry_detection_failed = True
        if entry_text is not None:
            loc = _file_loc(entry_text)
        else:
            loc = 1
        out.append(FlowSymbolAttribution(
            file=entry_file,
            symbol="<file>",
            line_start=1,
            line_end=max(loc, 1),
            role="entry",
        ))
        seen_files_with_symbol.add(entry_file)

    # ── Step 2 — imports/calls + reach-file symbol promotion ─────
    if entry_fn is not None and entry_text is not None:
        # Slice the entry symbol body text by character offsets for
        # accurate identifier extraction (avoids matching tokens
        # outside the function).
        line_starts = _line_starts(entry_text)
        body_text = entry_text[
            entry_fn.body_start_offset:
            line_starts[entry_fn.line_end]
            if entry_fn.line_end < len(line_starts) else len(entry_text)
        ]
        # Build the import name → specifier map ONCE per entry file.
        # Empty for Go/Rust (no TS/Py-style local-name imports);
        # the reach-file fallback below still promotes their calls.
        imports: dict[str, str] = {}
        if entry_lang == "ts":
            imports = _extract_ts_imports(entry_text)
        elif entry_lang == "py":
            imports = _extract_py_imports(entry_text)

        candidate_names = _identifiers_called_in(body_text)
        candidate_set = set(candidate_names)

        # 2a — Resolve via import specifier (TS/Py only).
        resolved_via_import: set[str] = set()
        for name in candidate_names:
            if len(out) >= max_symbols_per_flow:
                break
            if entry_lang not in ("ts", "py"):
                break
            specifier = imports.get(name)
            if specifier is None:
                continue
            # Resolve specifier → target file.
            if entry_lang == "ts":
                target = _resolve_ts_specifier(rctx, entry_file, specifier)
            else:
                target = _resolve_py_module(rctx, entry_file, specifier)
            if not target or target == entry_file:
                continue
            target_text = _read_text(rctx.repo_path, target)
            target_lang = _language_for(target)
            if target_text is None or target_lang is None:
                continue
            fns = _enumerate_functions(target_text, target_lang)
            match = next((f for f in fns if f.name == name), None)
            if match is None:
                continue
            out.append(FlowSymbolAttribution(
                file=target,
                symbol=name,
                line_start=match.line_start,
                line_end=match.line_end,
                role="called",
            ))
            seen_files_with_symbol.add(target)
            resolved_via_import.add(name)

        # 2b — Same-file calls (universal — applies to every language).
        # Identifiers in the body that match another function defined
        # IN THE SAME entry file get promoted to ``called`` so the
        # attribution covers helper functions co-located with the
        # entry handler (common in Go middleware, Rust handlers).
        same_file_fns = _enumerate_functions(entry_text, entry_lang) \
            if entry_lang else []
        for fn in same_file_fns:
            if len(out) >= max_symbols_per_flow:
                break
            if fn.name == entry_fn.name:
                continue
            if fn.name not in candidate_set:
                continue
            out.append(FlowSymbolAttribution(
                file=entry_file,
                symbol=fn.name,
                line_start=fn.line_start,
                line_end=fn.line_end,
                role="called",
            ))
            # Keep entry file in seen — already added by entry step.

        # 2c — Reach-file symbol promotion (universal). Any reach
        # file that contains a function definition matching a call
        # identifier in the body gets a ``called`` attribution
        # instead of the whole-file ``support`` fallback. This gives
        # Go/Rust flows symbol-grained attribution even though we
        # don't parse their imports.
        for p in reached_paths:
            if len(out) >= max_symbols_per_flow:
                break
            if p == entry_file or p in seen_files_with_symbol:
                continue
            text = _read_text(rctx.repo_path, p)
            lang = _language_for(p)
            if text is None or lang is None:
                continue
            fns = _enumerate_functions(text, lang)
            # Pick the first function in the file whose name appears
            # in the body's call set AND wasn't already resolved via
            # an import (TS/Py only — guards against double-attribution).
            match = next(
                (f for f in fns
                 if f.name in candidate_set
                 and f.name not in resolved_via_import),
                None,
            )
            if match is None:
                continue
            out.append(FlowSymbolAttribution(
                file=p,
                symbol=match.name,
                line_start=match.line_start,
                line_end=match.line_end,
                role="called",
            ))
            seen_files_with_symbol.add(p)

        # 2d — Namespace import resolution (TS/Py). A body reference
        # like ``prisma.user.findMany()`` won't appear in the call
        # candidate set (the call is on ``findMany``), but ``prisma``
        # is an imported NAMESPACE; the imported file is part of this
        # flow's narrative. Emit a whole-file ``support`` attribution
        # for the resolved target so namespace-style flows aren't
        # under-attributed.
        if entry_lang in ("ts", "py") and imports:
            bare_names = _bare_identifiers_in(body_text)
            for name in bare_names:
                if len(out) >= max_symbols_per_flow:
                    break
                if name in candidate_set:
                    # Already handled by step 2a (direct call) or 2b/2c.
                    continue
                specifier = imports.get(name)
                if specifier is None:
                    continue
                if entry_lang == "ts":
                    target = _resolve_ts_specifier(
                        rctx, entry_file, specifier,
                    )
                else:
                    target = _resolve_py_module(
                        rctx, entry_file, specifier,
                    )
                if not target or target == entry_file:
                    continue
                if target in seen_files_with_symbol:
                    continue
                text = _read_text(rctx.repo_path, target)
                if text is None:
                    continue
                loc = _file_loc(text)
                out.append(FlowSymbolAttribution(
                    file=target,
                    symbol=name,
                    line_start=1,
                    line_end=max(loc, 1),
                    role="support",
                ))
                seen_files_with_symbol.add(target)

    # ── Step 3 — support roles for unresolved reach files ─────────
    for p in reached_paths:
        if len(out) >= max_symbols_per_flow:
            break
        if p in seen_files_with_symbol:
            continue
        text = _read_text(rctx.repo_path, p)
        if text is None:
            continue
        loc = _file_loc(text)
        out.append(FlowSymbolAttribution(
            file=p,
            symbol="<file>",
            line_start=1,
            line_end=max(loc, 1),
            role="support",
        ))
        seen_files_with_symbol.add(p)

    return SymbolAttributionResult(
        attributions=tuple(out),
        entry_detection_failed=entry_detection_failed,
    )


__all__ = [
    "FlowSymbolAttribution",
    "SymbolAttributionResult",
    "compute_flow_symbols",
    "DEFAULT_MAX_SYMBOLS_PER_FLOW",
]
