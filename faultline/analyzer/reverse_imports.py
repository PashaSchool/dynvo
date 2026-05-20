"""Reverse-import lookup for Stage 6.3 anchor / schema seeding (deterministic).

Forward import resolution answers "which file does this import point
at?". Stage 6.3 also needs the inverse question: "which files in the
repo import this module name?" That's the seed-generation mechanism
for package-anchor and schema-source features that don't have flows.

Example
=======

The ``billing`` feature on inbox-zero is emitted by
``PackageAnchorExtractor`` because ``stripe`` and
``@stripe/stripe-js`` appear in ``apps/web/package.json``. The
feature's primary ``paths`` list is ``['apps/web']`` — the workspace
ROOT, not the consumer files. Stage 6.3's reverse-import seed expands
that to the 14 files under ``apps/web`` that actually
``from "stripe"`` / ``from "@stripe/stripe-js"``.

The same mechanism applies to schema features: a Prisma schema with
``model User`` and ``model Account`` is consumed by every file that
imports ``User`` / ``Account`` from the generated client — those
files become the schema-feature's seed set.

Why regex not AST
=================

Per :data:`flow_reach._extract_*` precedent: we cannot assume a
TS/JS/Python compiler is available on the user's machine. A
well-bounded regex over import statements is fast (O(file bytes)),
language-portable, and handles the variations TS allows:

  - ``import x from "stripe"``
  - ``import * as Stripe from "stripe"``
  - ``import { Foo } from "stripe"``
  - ``const stripe = require("stripe")``
  - dynamic ``import("stripe")``
  - Python ``import stripe`` / ``from stripe import Foo``

The regex does NOT understand re-exports or tag-template require
calls — those are rare and would catch us in false-positive territory
without commensurate true-positive gain.

NO LLM. Pure file scan.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────────

# Source-file extensions we scan. Strictly a subset of the languages
# Stage 6.3 traverses. Adding more later is safe (the regex is
# language-agnostic for the JS/Python-style import syntax).
_SCANNED_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs", ".cts", ".cjs",
    ".py",
})

# Skip dirs identical to those used by flow_reach's filter so the
# behaviour is consistent across stages.
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

# Reasonable upper bound on file size we'll grep. A 5 MB minified
# bundle isn't a real consumer; skip it to keep total scan time flat.
_MAX_FILE_BYTES = 2_000_000


# ── Regex compilation per module name ────────────────────────────────────


def _build_import_regex(module_names: tuple[str, ...]) -> re.Pattern[str]:
    r"""Compile one regex that matches ANY of the supplied module names.

    Escapes each name and joins them with ``|`` inside a non-capturing
    group. Matches:

      * ES module imports:
        ``from "MOD"`` / ``from 'MOD'`` (any whitespace between)
      * dynamic + require:
        ``import("MOD")`` / ``require("MOD")`` (single or double
        quotes)
      * Python:
        ``import MOD`` / ``from MOD import ...``
        (only for module_names matching ``^[A-Za-z_][\w.]*$`` — the
        regex is still safe for scoped npm names, those just won't
        match Python lines).
    """
    if not module_names:
        raise ValueError("module_names must be non-empty")
    escaped = [re.escape(m) for m in module_names]
    alt = "|".join(escaped)
    # JS-flavour patterns. Python ones too; the Python alternatives
    # only fire when the module name is a valid Python identifier
    # path (no @ scope, no /).
    patterns = [
        # from "mod" / from 'mod'
        rf"\bfrom\s+['\"](?:{alt})['\"]",
        # import "mod" — bare side-effect import
        rf"^\s*import\s+['\"](?:{alt})['\"]",
        # require("mod") / require('mod')
        rf"\brequire\s*\(\s*['\"](?:{alt})['\"]\s*\)",
        # import("mod") — dynamic
        rf"\bimport\s*\(\s*['\"](?:{alt})['\"]\s*\)",
    ]
    py_safe = [m for m in module_names if re.fullmatch(r"[A-Za-z_][\w.]*", m)]
    if py_safe:
        py_alt = "|".join(re.escape(m) for m in py_safe)
        patterns.extend([
            rf"^\s*from\s+(?:{py_alt})(?:\.\w+)*\s+import\s",
            rf"^\s*import\s+(?:{py_alt})(?:\s*,\s*[\w.]+)*(?:\s+as\s+\w+)?\s*$",
        ])
    return re.compile("|".join(patterns), re.MULTILINE)


# ── Public API ───────────────────────────────────────────────────────────


def find_consumers_of_module(
    module_names: list[str] | tuple[str, ...],
    repo_path: Path,
    tracked_files: frozenset[str],
    *,
    scope_prefix: str | None = None,
) -> list[str]:
    """Return tracked files that import ANY of ``module_names``.

    Args:
        module_names: package / module identifiers to look for. May
            include scoped npm names (``@stripe/stripe-js``), bare
            packages (``stripe``), or Python identifiers (``stripe``,
            ``some.dotted.module``).
        repo_path: absolute repo root used to read each candidate.
        tracked_files: the orchestrator's tracked-file set. Only files
            in this set are considered (matches the scan-context view).
        scope_prefix: when set, restrict the search to files whose
            path starts with this prefix. Useful when an anchor lives
            in one workspace and we don't want to seed across the
            whole monorepo. ``None`` means whole repo.

    Returns:
        Sorted list of repo-relative file paths that match. Empty
        list when no matches or when ``module_names`` is empty.

    NO LLM. NO network. Pure regex scan over file contents.
    """
    if not module_names:
        return []
    name_tuple = tuple(sorted(set(module_names)))
    regex = _build_import_regex(name_tuple)

    matches: list[str] = []
    for rel in tracked_files:
        if not rel:
            continue
        if scope_prefix and not rel.startswith(scope_prefix):
            continue
        suffix = Path(rel).suffix.lower()
        if suffix not in _SCANNED_EXTENSIONS:
            continue
        if _is_vendor_or_test(rel):
            continue
        abs_path = repo_path / rel
        try:
            stat = abs_path.stat()
        except OSError:
            continue
        if stat.st_size == 0 or stat.st_size > _MAX_FILE_BYTES:
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if regex.search(text):
            matches.append(rel)

    matches.sort()
    return matches


def find_symbols_in_file_using_module(
    file_rel: str,
    module_names: list[str] | tuple[str, ...],
    repo_path: Path,
) -> list[tuple[str, int, int]]:
    """Within a consumer file, find symbol bodies that reference any
    imported name from the target module(s).

    The algorithm:

      1. Read the file.
      2. Identify which LOCAL names are bound to each target module
         via the import line (default + named + namespace + require).
      3. Walk the file's exported / top-level function bodies (regex
         pattern set similar to ``flow_symbols._enumerate_functions``
         but simpler — we only need name + line span here).
      4. Emit ``(symbol, line_start, line_end)`` for each function
         whose body region contains a reference to one of the local
         names from step 2.

    When NO local-name binding is detected (rare — wildcards / dynamic
    require), we fall back to scanning the raw module names themselves
    inside function bodies, which still catches direct usage.

    Returns up to 25 entries (defensive cap); empty on parse failure.
    """
    if not module_names:
        return []
    abs_path = repo_path / file_rel
    try:
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    name_set = set(module_names)
    local_names = _extract_local_names(text, name_set)
    # Fallback — if no locals detected, scan for the bare module
    # names themselves (covers ``require("stripe").charges.create``).
    search_tokens = local_names if local_names else name_set

    if not search_tokens:
        return []

    function_spans = _enumerate_top_level_functions(text, file_rel)
    matches: list[tuple[str, int, int]] = []
    for name, line_start, line_end, body in function_spans:
        if _body_references_any(body, search_tokens):
            matches.append((name, line_start, line_end))
            if len(matches) >= 25:
                break
    return matches


# ── Helpers ──────────────────────────────────────────────────────────────


def _is_vendor_or_test(path: str) -> bool:
    needle = "/" + path
    for marker in _VENDOR_PATH_MARKERS:
        if marker in needle:
            return True
    for marker in _TEST_PATH_MARKERS:
        if marker in needle:
            return True
    return False


# Captures the IMPORT LINE's local bindings.
#
# Pattern semantics:
#   ``import Stripe from "stripe"``  → ``Stripe``
#   ``import { Foo, Bar as Baz } from "stripe"``  → ``Foo`` + ``Baz``
#   ``import * as Stripe from "stripe"`` → ``Stripe``
#   ``const stripe = require("stripe")`` → ``stripe``
_RE_TS_IMPORT_LINE = re.compile(
    r"""
    ^\s*import\s+
    (?:
        (?P<default>[A-Za-z_$][\w$]*)\s*(?:,\s*)?
    )?
    (?:
        \*\s+as\s+(?P<ns>[A-Za-z_$][\w$]*)
        |
        \{\s*(?P<named>[^}]+?)\s*\}
    )?
    \s*from\s+['"](?P<mod>[^'"]+)['"]
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_TS_REQUIRE_LINE = re.compile(
    r"""
    (?:const|let|var)\s+
    (?:
        (?P<bind>[A-Za-z_$][\w$]*)
        |
        \{\s*(?P<named>[^}]+?)\s*\}
    )
    \s*=\s*require\s*\(\s*['"](?P<mod>[^'"]+)['"]\s*\)
    """,
    re.VERBOSE,
)
_RE_PY_FROM_IMPORT_LINE = re.compile(
    r"""
    ^\s*from\s+(?P<mod>[\w.]+)\s+import\s+(?P<names>[\w.,\s*()]+?)$
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_PY_IMPORT_LINE = re.compile(
    r"""
    ^\s*import\s+(?P<mod>[\w.]+)(?:\s+as\s+(?P<alias>\w+))?\s*$
    """,
    re.VERBOSE | re.MULTILINE,
)


def _extract_local_names(text: str, target_modules: set[str]) -> set[str]:
    """Bind LOCAL names to any of ``target_modules`` from import lines."""
    locals_: set[str] = set()

    # TS / JS — ES module form.
    for m in _RE_TS_IMPORT_LINE.finditer(text):
        if m.group("mod") not in target_modules:
            continue
        default = m.group("default")
        if default:
            locals_.add(default)
        ns = m.group("ns")
        if ns:
            locals_.add(ns)
        named = m.group("named")
        if named:
            for piece in named.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                # ``Foo as Bar`` → use Bar
                if " as " in piece:
                    piece = piece.split(" as ", 1)[1].strip()
                # Strip type prefix (``type Foo``).
                if piece.startswith("type "):
                    piece = piece[5:].strip()
                if piece:
                    locals_.add(piece)

    # TS / JS — CommonJS require.
    for m in _RE_TS_REQUIRE_LINE.finditer(text):
        if m.group("mod") not in target_modules:
            continue
        if m.group("bind"):
            locals_.add(m.group("bind"))
        named = m.group("named")
        if named:
            for piece in named.split(","):
                piece = piece.strip().split(":")[-1].strip()
                if piece:
                    locals_.add(piece)

    # Python — from X import a, b
    for m in _RE_PY_FROM_IMPORT_LINE.finditer(text):
        if m.group("mod") not in target_modules:
            continue
        names = m.group("names")
        for piece in names.split(","):
            piece = piece.strip().strip("()")
            if not piece:
                continue
            if " as " in piece:
                piece = piece.split(" as ", 1)[1].strip()
            if piece and piece != "*":
                locals_.add(piece)

    # Python — import X / import X as Y
    for m in _RE_PY_IMPORT_LINE.finditer(text):
        mod = m.group("mod")
        if mod not in target_modules:
            # Allow ``import dotted.x`` when the prefix matches.
            top = mod.split(".", 1)[0]
            if top not in target_modules:
                continue
        alias = m.group("alias")
        locals_.add(alias or mod.split(".", 1)[0])

    return locals_


# Top-level function / arrow / const detection — coarse but adequate
# for "does this exported symbol's body touch a Stripe API?".
_RE_TS_TOPLEVEL_FN = re.compile(
    r"""
    ^(?P<head>(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*[\(<])
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_TS_TOPLEVEL_CONST = re.compile(
    r"""
    ^(?P<head>(?:export\s+(?:default\s+)?)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?\s*=)
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_TS_TOPLEVEL_CLASS = re.compile(
    r"""
    ^(?P<head>(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*))
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_PY_TOPLEVEL_DEF = re.compile(
    r"""
    ^(?P<head>(?:async\s+)?def\s+(?P<name>[A-Za-z_][\w]*)\s*\()
    """,
    re.VERBOSE | re.MULTILINE,
)
_RE_PY_TOPLEVEL_CLASS = re.compile(
    r"""^(?P<head>class\s+(?P<name>[A-Za-z_][\w]*)\b)""",
    re.MULTILINE,
)


def _enumerate_top_level_functions(
    text: str, file_rel: str,
) -> list[tuple[str, int, int, str]]:
    """Return ``(name, line_start, line_end, body_text)`` for each
    top-level function / const-bound expression / class in ``text``.

    For TS/JS we estimate body end via balanced-brace counting from
    the first ``{`` after the head. For Python we walk forward until
    we encounter a non-indented non-blank line (the next top-level
    statement) — same heuristic as ``flow_symbols._python_body_end_line``.
    """
    suffix = Path(file_rel).suffix.lower()
    line_starts = _line_starts(text)

    def _offset_to_line(offset: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1  # 1-indexed

    results: list[tuple[str, int, int, str]] = []
    if suffix in {".py"}:
        for pattern in (_RE_PY_TOPLEVEL_DEF, _RE_PY_TOPLEVEL_CLASS):
            for m in pattern.finditer(text):
                # Only TOP-LEVEL — no leading whitespace.
                if m.start() > 0 and text[m.start() - 1] != "\n":
                    continue
                name = m.group("name")
                line_start = _offset_to_line(m.start())
                line_end = _python_body_end(text, line_start)
                body = "\n".join(text.splitlines()[line_start - 1 : line_end])
                results.append((name, line_start, line_end, body))
    else:
        for pattern in (_RE_TS_TOPLEVEL_FN, _RE_TS_TOPLEVEL_CONST, _RE_TS_TOPLEVEL_CLASS):
            for m in pattern.finditer(text):
                # Only TOP-LEVEL — no leading whitespace on the line.
                head_offset = m.start()
                if head_offset > 0 and text[head_offset - 1] != "\n":
                    continue
                name = m.group("name")
                line_start = _offset_to_line(head_offset)
                line_end = _brace_balance_end(text, head_offset, line_start)
                body = "\n".join(text.splitlines()[line_start - 1 : line_end])
                results.append((name, line_start, line_end, body))

    # Dedup by (name, line_start) — const + class can collide on same line.
    seen: set[tuple[str, int]] = set()
    deduped = []
    for entry in results:
        key = (entry[0], entry[1])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    deduped.sort(key=lambda x: x[1])
    return deduped


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def _brace_balance_end(text: str, head_offset: int, line_start: int) -> int:
    """Return the 1-indexed line containing the matching ``}`` for the
    first ``{`` after ``head_offset``.

    Falls back to ``line_start`` (single-line) when no opening brace
    is found within 200 characters of the head (true for ``export const
    X = 42``).
    """
    search_window = text[head_offset : head_offset + 4000]
    try:
        rel_open = search_window.index("{")
    except ValueError:
        # No brace → likely a one-liner. Use line_start to line_start.
        return line_start
    open_abs = head_offset + rel_open
    depth = 0
    n = len(text)
    in_str: str | None = None
    in_line_comment = False
    in_block_comment = False
    i = open_abs
    while i < n:
        ch = text[i]
        next_ch = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and next_ch == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1  # skip escaped char
            elif ch == in_str:
                in_str = None
        else:
            if ch == "/" and next_ch == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and next_ch == "*":
                in_block_comment = True
                i += 1
            elif ch in {'"', "'", "`"}:
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    # Convert offset → line.
                    line = 1 + text.count("\n", 0, i + 1)
                    return line
        i += 1
    # Unbalanced — give a reasonable upper bound.
    return min(line_start + 200, text.count("\n") + 1)


def _python_body_end(text: str, line_start: int) -> int:
    """Walk forward from ``line_start`` until we hit a non-indented
    non-blank line at the top level (i.e. the next def/class/import).
    """
    lines = text.splitlines()
    n = len(lines)
    if line_start - 1 >= n:
        return line_start
    # The header line itself.
    header = lines[line_start - 1]
    # Empty body? Step one ahead and treat next-indent as body.
    idx = line_start  # 0-indexed within ``lines[idx]`` from here.
    while idx < n:
        ln = lines[idx]
        stripped = ln.strip()
        if not stripped:
            idx += 1
            continue
        # First non-blank line should be indented (body). If not, we
        # have an empty body — return current line.
        if ln[0] not in (" ", "\t"):
            return idx  # 1-indexed end == previous line
        break
    last_body_line = idx
    while idx < n:
        ln = lines[idx]
        stripped = ln.strip()
        if not stripped:
            idx += 1
            continue
        # Top-level statement reached → body ended on previous nonblank.
        if ln[0] not in (" ", "\t"):
            return last_body_line  # 1-indexed
        last_body_line = idx + 1
        idx += 1
    return last_body_line


def _body_references_any(body: str, tokens: set[str]) -> bool:
    """Return True when ``body`` contains a word-boundary match for any
    of ``tokens``.
    """
    if not tokens:
        return False
    # Cheap pre-check — substring scan; only build regex when needed.
    # The regex is required to avoid false positives like ``cstripe``
    # matching ``stripe``.
    if not any(t in body for t in tokens):
        return False
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b"
    )
    return bool(pattern.search(body))


# ── Symbol-name reverse lookup (schema-source seeding) ──────────────────


def _build_import_name_regex(symbol_names: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a regex matching any import LINE that brings in one of
    ``symbol_names`` as a named import.

    Matches:

      * ``import { User } from "..."``           — ES named import
      * ``import { User as U } from "..."``      — aliased
      * ``import { Foo, User, Bar } from "..."`` — mixed
      * Python: ``from schema import User``      — named import
      * Python: ``from schema import Foo, User`` — comma-separated

    Does NOT match identifier USAGE — only the binding-line itself.
    That distinction is what makes this safe to use for schema-model
    consumer detection (matching every file that mentions ``User``
    would over-match catastrophically).
    """
    if not symbol_names:
        raise ValueError("symbol_names must be non-empty")
    escaped = [re.escape(n) for n in symbol_names]
    alt = "|".join(escaped)
    patterns = [
        # JS named import — name appears inside the {...} of an import.
        rf"\bimport\b[^;]*?\{{[^}}]*?\b(?:{alt})\b[^}}]*?\}}[^;]*?\bfrom\b",
        # Python: from X import ... NAME ...
        rf"^\s*from\s+[\w.]+\s+import\s+[^\n]*\b(?:{alt})\b",
    ]
    return re.compile("|".join(patterns), re.MULTILINE)


def find_consumers_of_symbols(
    symbol_names: list[str] | tuple[str, ...],
    repo_path: Path,
    tracked_files: frozenset[str],
    *,
    scope_prefix: str | None = None,
) -> list[str]:
    """Return tracked files that NAMED-IMPORT any of ``symbol_names``.

    The semantic difference from :func:`find_consumers_of_module`:
    here we don't know the module name. We're looking for files that
    pulled the symbol in from SOMEWHERE — typically a generated
    Prisma client, a Drizzle schema export, a Django ``models``
    module. The regex matches the binding-line shape, never bare
    identifier usage.

    Empty list when ``symbol_names`` is empty or no matches.
    """
    if not symbol_names:
        return []
    names = tuple(sorted(set(symbol_names)))
    regex = _build_import_name_regex(names)
    matches: list[str] = []
    for rel in tracked_files:
        if not rel:
            continue
        if scope_prefix and not rel.startswith(scope_prefix):
            continue
        suffix = Path(rel).suffix.lower()
        if suffix not in _SCANNED_EXTENSIONS:
            continue
        if _is_vendor_or_test(rel):
            continue
        abs_path = repo_path / rel
        try:
            stat = abs_path.stat()
        except OSError:
            continue
        if stat.st_size == 0 or stat.st_size > _MAX_FILE_BYTES:
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if regex.search(text):
            matches.append(rel)
    matches.sort()
    return matches


def find_symbols_in_file_using_symbols(
    file_rel: str,
    symbol_names: list[str] | tuple[str, ...],
    repo_path: Path,
) -> list[tuple[str, int, int]]:
    """Within a consumer file, find top-level symbol bodies that
    reference any of ``symbol_names`` (treating them as imported
    identifiers).

    Reuses :func:`_enumerate_top_level_functions` + body-token scan.
    """
    if not symbol_names:
        return []
    abs_path = repo_path / file_rel
    try:
        text = abs_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    tokens = set(symbol_names)
    spans = _enumerate_top_level_functions(text, file_rel)
    matches: list[tuple[str, int, int]] = []
    for name, ls, le, body in spans:
        if _body_references_any(body, tokens):
            matches.append((name, ls, le))
            if len(matches) >= 25:
                break
    return matches


__all__ = [
    "find_consumers_of_module",
    "find_symbols_in_file_using_module",
    "find_consumers_of_symbols",
    "find_symbols_in_file_using_symbols",
]
