"""
Regex-based signature extractor for TypeScript and JavaScript files.

Extracts exports, route definitions, and imports from each file
without any external AST dependencies. This "skeleton" is then
fed to an LLM to identify user-facing flows within each feature.

Supported patterns:
  - Named exports:     export function Foo / export const Foo / export class Foo
  - Default exports:   export default function Foo / export default class Foo
  - Re-exports:        export { Foo, Bar }
  - Next.js routes:    export async function GET/POST/PUT/DELETE/PATCH (App Router)
  - Next.js pages:     getServerSideProps, getStaticProps (Pages Router)
  - Express routes:    router.get('/path', ...) / app.post('/path', ...)
  - ES imports:        import X from 'Y'
"""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from faultline.models.types import SymbolRange


_TS_JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_PYTHON_EXTENSIONS = {".py"}
_GO_EXTENSIONS = {".go"}
_RUST_EXTENSIONS = {".rs"}
_RUBY_EXTENSIONS = {".rb", ".rake"}

# Named function/class/const exports
_RE_NAMED_EXPORT = re.compile(
    r"export\s+(?:async\s+)?(?:function\s*\*?\s*|class\s+|const\s+|let\s+|var\s+)(\w+)"
)
# Default function/class exports with a name
_RE_DEFAULT_EXPORT = re.compile(
    r"export\s+default\s+(?:async\s+)?(?:function|class)\s+(\w+)"
)
# Re-export block: export { Foo, Bar as Baz }
_RE_REEXPORT = re.compile(r"export\s*\{([^}]+)\}")

# Next.js App Router HTTP method handlers
_RE_NEXTJS_ROUTE = re.compile(
    r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b"
)
# Next.js Pages Router data fetchers
_RE_NEXTJS_PAGE = re.compile(
    r"export\s+(?:async\s+)?function\s+(getServerSideProps|getStaticProps|getStaticPaths)\b"
)
# Express/Fastify route definitions: router.get('/path', ...) or app.post('/path')
_RE_EXPRESS_ROUTE = re.compile(
    r"\b(?:router|app|server)\s*\.\s*(get|post|put|delete|patch|head)\s*\(\s*['\"]([^'\"]+)['\"]"
)
# ES6 import paths
_RE_IMPORT = re.compile(r"import\s+.*?from\s+['\"]([^'\"]+)['\"]")

# Top-level LOCAL (non-exported) function/arrow declarations in TS/JS.
# These are NOT exports — they never enter ``FileSignature.exports`` and
# therefore never become feature anchors. They are recorded ONLY in
# ``symbol_ranges`` so the call-graph tracer can resolve same-file
# handler references such as ``const POST = wrap(postHandler)`` where the
# real body lives in a local ``postHandler`` symbol. Anchored to line
# start (MULTILINE) to avoid matching nested / inline closures.
#   function foo(  /  async function foo(
_RE_LOCAL_FUNC_DECL = re.compile(
    r"^(?:async\s+)?function\s*\*?\s*(\w+)\s*[(<]",
    re.MULTILINE,
)
#   const foo = (  /  const foo = async (  /  let foo = function  / arrow
_RE_LOCAL_ARROW_DECL = re.compile(
    r"^(?:const|let|var)\s+(\w+)\s*=\s*"
    r"(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)",
    re.MULTILINE,
)

# Python patterns
_RE_PYTHON_CLASS = re.compile(r"^class\s+(\w+)", re.MULTILINE)
_RE_PYTHON_FUNC = re.compile(r"^(?:async\s+)?def\s+([a-zA-Z]\w*)", re.MULTILINE)
_RE_PYTHON_ROUTE = re.compile(
    r"@\w*(?:router|app|blueprint|bp|api)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

# Go: top-level capitalized symbols are exported (idiomatic Go).
# Match func, type, var, const, struct decls.
_RE_GO_FUNC = re.compile(
    r"^func\s+(?:\([^)]*\)\s+)?([A-Z]\w*)\s*\(",
    re.MULTILINE,
)
_RE_GO_TYPE = re.compile(
    r"^type\s+([A-Z]\w*)\b",
    re.MULTILINE,
)
_RE_GO_VAR = re.compile(
    r"^(?:var|const)\s+([A-Z]\w*)\b",
    re.MULTILINE,
)
# Rust: ``pub`` items at any visibility level (we conservatively
# accept ``pub``, ``pub(crate)``, ``pub(super)``).
_RE_RUST_PUB = re.compile(
    r"^\s*pub(?:\([^)]+\))?\s+(?:async\s+)?(?:unsafe\s+)?"
    r"(?:fn|struct|enum|trait|type|const|static|mod)\s+([a-zA-Z_]\w*)",
    re.MULTILINE,
)

# Ruby patterns. Stage 3 only needs symbol *names* (not semantics), so a
# regex parser mirroring _parse_python_file is sufficient — no tree-sitter
# dependency. Without these, every .rb file returns 0 exports and Rails
# repos hit the Stage 3 short-circuit at MIN_EXPORTS_FOR_FLOW_DETECTION=3
# → zero flows emitted (verified on maybe corpus repo).
_RE_RUBY_CLASS = re.compile(r"^\s*class\s+([A-Z]\w*)", re.MULTILINE)
_RE_RUBY_MODULE = re.compile(r"^\s*module\s+([A-Z]\w*)", re.MULTILINE)
_RE_RUBY_DEF = re.compile(
    r"^\s*def\s+(?:self\.)?([a-z_][\w?!=]*)", re.MULTILINE,
)
_RE_RUBY_CONST = re.compile(r"^\s*([A-Z][A-Z0-9_]+)\s*=", re.MULTILINE)
# ActiveRecord / Rails domain declarations — surface as "exports" so
# Stage 3 sees enough symbols and the LLM has Rails-shaped vocabulary.
_RE_RUBY_AR_ASSOC = re.compile(
    r"^\s*(?:has_many|has_one|belongs_to|has_and_belongs_to_many)\s+:(\w+)",
    re.MULTILINE,
)
_RE_RUBY_AR_SCOPE = re.compile(r"^\s*scope\s+:(\w+)", re.MULTILINE)
# Rails routes.rb DSL — get '/path' => 'controller#action' etc.
_RE_RUBY_ROUTE = re.compile(
    r"^\s*(get|post|put|patch|delete)\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE | re.IGNORECASE,
)


# Named import destructuring: import { FOO, BAR as Baz } from './path'
_RE_NAMED_IMPORT = re.compile(
    r"import\s*(?:type\s+)?\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]"
)
# Namespace import: import * as X from './path'
_RE_NAMESPACE_IMPORT = re.compile(
    r"import\s*\*\s*as\s+\w+\s+from\s*['\"]([^'\"]+)['\"]"
)

# TS type/interface/enum exports
_RE_TYPE_EXPORT = re.compile(
    r"export\s+(?:declare\s+)?(?:type|interface|enum)\s+(\w+)"
)

# TS/JS class declaration (named). Group 1 = class name. Matches both
# ``export class Foo`` and bare ``class Foo`` (and ``export default class
# Foo``). Used to locate class bodies for METHOD-LEVEL indexing so a member
# call ``obj.method()`` resolves to the specific method body, not the whole
# class. Anchored at a word boundary; the body span is then recovered by a
# brace-matching scan (regex alone can't balance braces).
_RE_TS_CLASS_DECL = re.compile(
    r"\bclass\s+([A-Za-z_$][\w$]*)\b",
)

# A method / class-field declaration on its OWN line inside a class body.
# Group 1 = method name. Covers:
#   foo(           async foo(        static foo(      *foo(
#   public foo(    private foo(      protected foo(   get foo(   set foo(
#   foo = (args) => {     foo = async (args) => {     (arrow class fields)
# Leading whitespace required (class members are indented). The trailing
# ``(`` or ``=`` is what distinguishes a method/arrow-field from a plain
# data field. STRUCTURAL — no per-repo names. Control-flow keywords that
# could masquerade as a call (``if (``, ``for (`` …) are filtered by the
# caller via _TS_NON_METHOD_KEYWORDS.
_RE_TS_METHOD = re.compile(
    r"^\s+(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+|"
    r"abstract\s+|override\s+|async\s+|get\s+|set\s+|\*\s*)*"
    r"([A-Za-z_$#][\w$]*)\s*"
    r"(?:[(<]|=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>)",
)
# Tokens that _RE_TS_METHOD would otherwise capture as a "method" because
# they are followed by ``(`` — these are control-flow / operators, never
# real callable members. Excluding them keeps phantom method symbols out.
_TS_NON_METHOD_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "await", "yield",
    "typeof", "instanceof", "in", "of", "new", "do", "else", "case",
    "function", "constructor",  # constructor handled separately
})


@dataclass
class FileSignature:
    path: str
    exports: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    symbol_ranges: list[SymbolRange] = field(default_factory=list)
    source: str = field(default="", repr=False)

    def is_empty(self) -> bool:
        return not self.exports and not self.routes and not self.imports

    def to_prompt_line(self) -> str:
        """Formats the signature as a single line for LLM prompts."""
        parts = []
        if self.exports:
            parts.append(f"exports: {', '.join(self.exports[:8])}")
        if self.routes:
            parts.append(f"routes: {', '.join(self.routes[:5])}")
        if not parts:
            return ""
        return f"  {self.path} → {' | '.join(parts)}"


def extract_signatures(
    files: list[str],
    repo_path: str,
) -> dict[str, FileSignature]:
    """
    Extracts function/route/import signatures from TypeScript and JavaScript files.

    Args:
        files: List of relative file paths (relative to repo_path).
        repo_path: Absolute path to the repository root.

    Returns:
        Dict mapping relative file path → FileSignature.
        Non-TS/JS files are skipped and not included in the result.
    """
    result: dict[str, FileSignature] = {}
    root = Path(repo_path)

    for rel_path in files:
        suffix = Path(rel_path).suffix.lower()
        if (
            suffix not in _TS_JS_EXTENSIONS
            and suffix not in _PYTHON_EXTENSIONS
            and suffix not in _GO_EXTENSIONS
            and suffix not in _RUST_EXTENSIONS
            and suffix not in _RUBY_EXTENSIONS
        ):
            continue
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if suffix in _PYTHON_EXTENSIONS:
            sig = _parse_python_file(rel_path, source)
        elif suffix in _GO_EXTENSIONS:
            sig = _parse_go_file(rel_path, source)
        elif suffix in _RUST_EXTENSIONS:
            sig = _parse_rust_file(rel_path, source)
        elif suffix in _RUBY_EXTENSIONS:
            sig = _parse_ruby_file(rel_path, source)
        else:
            sig = _parse_file(rel_path, source)
            sig.symbol_ranges = extract_symbol_ranges(source)

        # The T1 call-graph (flow_expansion/call_graph.py) scans a
        # symbol's body for callee identifiers via ``sig.source``. This
        # was previously populated ONLY for TS/JS, so every Python / Go /
        # Rust / Ruby flow collapsed to a single entry node (0 edges).
        # Populate it for every language so same-file + imported callees
        # resolve uniformly.
        sig.source = source

        if not sig.is_empty():
            result[rel_path] = sig

    return result


def _parse_go_file(rel_path: str, source: str) -> FileSignature:
    """Extract exports + symbol ranges from a Go source file.

    Go convention: identifiers starting with an uppercase letter are
    package-exported. Receiver methods on exported types
    (``func (s *Server) HandleX(...)``) count too.
    """
    sig = FileSignature(path=rel_path)
    seen: set[str] = set()
    raw: list[tuple[int, str, str]] = []  # (start_line, name, kind)

    for match in _RE_GO_FUNC.finditer(source):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        sig.exports.append(name)
        line = source.count("\n", 0, match.start()) + 1
        raw.append((line, name, "function"))

    for match in _RE_GO_TYPE.finditer(source):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        sig.exports.append(name)
        line = source.count("\n", 0, match.start()) + 1
        raw.append((line, name, "class"))

    for match in _RE_GO_VAR.finditer(source):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        sig.exports.append(name)
        line = source.count("\n", 0, match.start()) + 1
        raw.append((line, name, "const"))

    sig.symbol_ranges = _ranges_from_raw(raw, source)
    return sig


def _parse_rust_file(rel_path: str, source: str) -> FileSignature:
    """Extract ``pub`` items and their line ranges from a Rust source file."""
    sig = FileSignature(path=rel_path)
    seen: set[str] = set()
    raw: list[tuple[int, str, str]] = []

    for match in _RE_RUST_PUB.finditer(source):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        sig.exports.append(name)
        line = source.count("\n", 0, match.start()) + 1
        # Crude kind detection from the matched item keyword
        text = match.group(0)
        if "fn " in text:
            kind = "function"
        elif "struct " in text or "enum " in text or "trait " in text:
            kind = "class"
        elif "type " in text:
            kind = "type"
        else:
            kind = "const"
        raw.append((line, name, kind))

    sig.symbol_ranges = _ranges_from_raw(raw, source)
    return sig


def _ranges_from_raw(
    raw: list[tuple[int, str, str]], source: str,
) -> list[SymbolRange]:
    """Convert (start_line, name, kind) tuples to ``SymbolRange`` with end_line.

    End-of-symbol heuristic: extends until the start of the next top-
    level symbol (or EOF). Good enough for symbol-scoped queries
    where exact boundaries matter less than catching the right
    function body.
    """
    if not raw:
        return []
    raw_sorted = sorted(raw, key=lambda x: x[0])
    total_lines = source.count("\n") + 1
    out: list[SymbolRange] = []
    for i, (line, name, kind) in enumerate(raw_sorted):
        end_line = (
            raw_sorted[i + 1][0] - 1
            if i + 1 < len(raw_sorted)
            else total_lines
        )
        if end_line < line:
            end_line = line
        out.append(SymbolRange(
            name=name, start_line=line, end_line=end_line, kind=kind,
        ))
    return out


def _parse_file(rel_path: str, source: str) -> FileSignature:
    sig = FileSignature(path=rel_path)

    # Collect named exports
    seen_exports: set[str] = set()

    for match in _RE_NAMED_EXPORT.finditer(source):
        name = match.group(1)
        if name not in seen_exports:
            seen_exports.add(name)
            sig.exports.append(name)

    for match in _RE_DEFAULT_EXPORT.finditer(source):
        name = match.group(1)
        if name not in seen_exports:
            seen_exports.add(name)
            sig.exports.append(name)

    for match in _RE_REEXPORT.finditer(source):
        for token in match.group(1).split(","):
            # Handle "Foo as Bar" → take the exported name "Bar"
            parts = token.strip().split(" as ")
            name = parts[-1].strip()
            if name and name not in seen_exports:
                seen_exports.add(name)
                sig.exports.append(name)

    # Collect route definitions
    for match in _RE_NEXTJS_ROUTE.finditer(source):
        method = match.group(1)
        # Infer path from the file path for App Router (files live at the route path)
        route_path = _infer_nextjs_route_path(rel_path)
        sig.routes.append(f"{method} {route_path}")

    for match in _RE_NEXTJS_PAGE.finditer(source):
        sig.routes.append(match.group(1))

    for match in _RE_EXPRESS_ROUTE.finditer(source):
        method = match.group(1).upper()
        path = match.group(2)
        sig.routes.append(f"{method} {path}")

    # Collect imports. We keep:
    #   * relative ('./x', '../x') and builtin aliases ('@/x', '~/x', '#/x')
    #     — always internal;
    #   * SCOPED bare specifiers ('@scope/pkg[/subpath]') — these are the
    #     workspace cross-package imports ('@calcom/lib', '@calcom/features/…')
    #     that the downstream resolver maps to real files via the
    #     package.json#name → dir map. A scoped specifier that is genuinely
    #     third-party (e.g. '@sentry/node') simply fails resolution and adds
    #     no edge, so retaining it here is safe — the resolver is the single
    #     gate. (Previously these were dropped at parse time, which made the
    #     cross-package callee bodies unreachable for flow tracing.)
    # We still skip UNSCOPED bare specifiers ('react', 'next/server') to keep
    # the import list lean; unscoped workspace package names are far rarer and
    # the directory-name monorepo resolver already covers the common case.
    for match in _RE_IMPORT.finditer(source):
        src = match.group(1)
        if (
            src.startswith(".")
            or src.startswith("@/")
            or src.startswith("~/")
            or src.startswith("#/")
            or src.startswith("@")  # scoped workspace/3p — resolver gates it
        ):
            sig.imports.append(src)

    return sig


def _parse_python_file(rel_path: str, source: str) -> FileSignature:
    """Extract exports, routes, and symbol ranges from a Python file.

    Prefers the stdlib :mod:`ast` module for symbol ranges: it gives
    EXACT ``(node.lineno, node.end_lineno)`` boundaries for every
    ``def`` / ``async def`` / ``class`` — including nested methods —
    which the regex heuristic (next-symbol-start-minus-one) could only
    approximate. Precise ranges matter for the T1 call graph, which
    scans a symbol's body line-slice for callee identifiers: a too-wide
    range pulls in sibling functions' calls (over-attribution), a
    too-narrow one misses real callees.

    ``exports`` keeps MODULE-LEVEL names only (unchanged contract for
    Stage 3's MIN_EXPORTS_FOR_FLOW_DETECTION gate). Nested methods get
    symbol *ranges* (so same-file ``self._helper()`` callees resolve)
    but are NOT added to ``exports`` — that would inflate the export
    list with private internals and shift flow-detection vocabulary.

    Falls back to the regex scanner when the source is not valid Python
    (partial files, syntax errors, templating) so we never regress to
    zero symbols on a parse failure.
    """
    sig = FileSignature(path=rel_path)

    # Routes are regex-derived in both paths (decorators are awkward to
    # match structurally and the regex already generalises across
    # FastAPI / Flask / blueprint styles via the stack-agnostic pattern).
    for match in _RE_PYTHON_ROUTE.finditer(source):
        method = match.group(1).upper()
        path = match.group(2)
        sig.routes.append(f"{method} {path}")

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        _python_symbols_via_regex(sig, source)
        return sig

    total_lines = source.count("\n") + 1
    seen_exports: set[str] = set()
    seen_range_keys: set[tuple[str, int]] = set()

    # Class-aware walk: a ``def`` whose nearest enclosing scope is a
    # ``class`` is a METHOD (tag kind="method"/"constructor" + parent) so a
    # cross-file member call ``instance.find_by_id()`` resolves to that
    # method's OWN exact range, never the whole class. Module-level ``def``
    # stays ``function``; classes stay ``class`` (kept for anchoring).
    # ``__init__`` is tagged ``constructor`` to mirror the TS path (so a
    # ``Class(...)`` construction attributes the initialiser body only).
    def _walk(node: ast.AST, parent_class: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                start = child.lineno
                end = getattr(child, "end_lineno", None) or start
                key = (child.name, start)
                if key not in seen_range_keys:
                    seen_range_keys.add(key)
                    sig.symbol_ranges.append(SymbolRange(
                        name=child.name, start_line=start,
                        end_line=max(start, end), kind="class",
                    ))
                _walk(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = child.lineno
                end = getattr(child, "end_lineno", None) or start
                if parent_class is not None:
                    kind = (
                        "constructor"
                        if child.name == "__init__"
                        else "method"
                    )
                else:
                    kind = "function"
                key = (child.name, start)
                if key not in seen_range_keys:
                    seen_range_keys.add(key)
                    sig.symbol_ranges.append(SymbolRange(
                        name=child.name, start_line=start,
                        end_line=max(start, end), kind=kind,
                        parent=parent_class,
                    ))
                # Recurse into the function body too (nested defs / closures
                # keep kind="function"; nested classes get their own parent).
                _walk(child, None)
            else:
                _walk(child, parent_class)

    _walk(tree, None)

    # Module-level names → exports (stable Stage-3 contract).
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            if node.name not in seen_exports:
                seen_exports.add(node.name)
                sig.exports.append(node.name)

    if not sig.symbol_ranges and total_lines:
        # No defs/classes at all (e.g. a script of bare statements) —
        # nothing to attribute; leave ranges empty.
        pass

    return sig


def _python_symbols_via_regex(sig: FileSignature, source: str) -> None:
    """Regex fallback for Python files that don't parse via ``ast``.

    Module-level ``class`` / ``def`` only (the ``^`` anchors require
    column-0). End-of-symbol = next symbol's start - 1 (EOF for last).
    """
    seen: set[str] = set()
    raw_symbols: list[tuple[int, str, str]] = []

    for match in _RE_PYTHON_CLASS.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)
            line = source.count("\n", 0, match.start()) + 1
            raw_symbols.append((line, name, "class"))

    for match in _RE_PYTHON_FUNC.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)
            line = source.count("\n", 0, match.start()) + 1
            raw_symbols.append((line, name, "function"))

    raw_symbols.sort(key=lambda x: x[0])
    total_lines = source.count("\n") + 1
    for i, (start, name, kind) in enumerate(raw_symbols):
        end = (
            raw_symbols[i + 1][0] - 1
            if i + 1 < len(raw_symbols) else total_lines
        )
        sig.symbol_ranges.append(SymbolRange(
            name=name, start_line=start, end_line=max(start, end),
            kind=kind,
        ))


def _parse_ruby_file(rel_path: str, source: str) -> FileSignature:
    """Extract exports + symbol ranges from a Ruby source file.

    Treats classes, modules, top-level methods, and uppercase constants
    as "exports" — enough for Stage 3's MIN_EXPORTS_FOR_FLOW_DETECTION
    gate. Also surfaces ActiveRecord associations (``has_many :foos``)
    and scopes (``scope :recent``) as exports because in a Rails domain
    these ARE the user-visible vocabulary; surfacing them gives the
    flow-detection LLM richer ground to name flows.

    Routes (``get '/path' => 'controller#action'`` in config/routes.rb)
    populate ``sig.routes``.
    """
    sig = FileSignature(path=rel_path)
    seen: set[str] = set()
    raw_symbols: list[tuple[int, str, str]] = []

    for match in _RE_RUBY_CLASS.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)
            line = source.count("\n", 0, match.start()) + 1
            raw_symbols.append((line, name, "class"))

    for match in _RE_RUBY_MODULE.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)
            line = source.count("\n", 0, match.start()) + 1
            raw_symbols.append((line, name, "module"))

    for match in _RE_RUBY_DEF.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)
            line = source.count("\n", 0, match.start()) + 1
            raw_symbols.append((line, name, "method"))

    for match in _RE_RUBY_CONST.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)
            line = source.count("\n", 0, match.start()) + 1
            raw_symbols.append((line, name, "constant"))

    for match in _RE_RUBY_AR_ASSOC.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)

    for match in _RE_RUBY_AR_SCOPE.finditer(source):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            sig.exports.append(name)

    for match in _RE_RUBY_ROUTE.finditer(source):
        method = match.group(1).upper()
        path = match.group(2)
        sig.routes.append(f"{method} {path}")

    raw_symbols.sort(key=lambda x: x[0])
    total_lines = source.count("\n") + 1
    for i, (start, name, kind) in enumerate(raw_symbols):
        end = (
            raw_symbols[i + 1][0] - 1
            if i + 1 < len(raw_symbols) else total_lines
        )
        sig.symbol_ranges.append(SymbolRange(
            name=name, start_line=start, end_line=max(start, end),
            kind=kind,
        ))

    return sig


def _infer_nextjs_route_path(rel_path: str) -> str:
    """
    Infers the Next.js API route path from the file's relative path.

    Examples:
        app/api/auth/login/route.ts → /api/auth/login
        pages/api/auth.ts           → /api/auth
        src/app/api/users/route.ts  → /api/users
    """
    p = Path(rel_path)
    parts = p.parts

    # Drop leading src/, app/ wrappers
    skip = {"src", "app"}
    start = 0
    for i, part in enumerate(parts):
        if part not in skip:
            start = i
            break

    trimmed = parts[start:]

    # Drop trailing "route.ts" filename
    if trimmed and Path(trimmed[-1]).stem == "route":
        trimmed = trimmed[:-1]
    else:
        # Drop the filename extension for pages/api style
        trimmed = trimmed[:-1] + (Path(trimmed[-1]).stem,) if trimmed else trimmed

    return "/" + "/".join(trimmed) if trimmed else "/"


def _find_class_body_span(
    source: str, class_decl_offset: int,
) -> tuple[int, int] | None:
    """Return ``(body_open_offset, body_close_offset)`` for a class body.

    Starting at the class declaration char offset, find the first ``{``
    (the class body open brace) and scan forward tracking brace depth
    (skipping braces inside strings / template literals / line + block
    comments) until the matching close brace. Returns char offsets of the
    open and close brace, or ``None`` if unbalanced (truncated / malformed
    source) — the caller then simply skips method indexing for that class
    and keeps the whole-class symbol as before (graceful degrade).
    """
    n = len(source)
    i = source.find("{", class_decl_offset)
    if i < 0:
        return None
    open_off = i
    depth = 0
    in_str: str | None = None       # current string/template delimiter
    in_line_comment = False
    in_block_comment = False
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        # not in string/comment
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
                return (open_off, i)
        i += 1
    return None


def _extract_class_methods(
    source: str,
    class_name: str,
    body_open_off: int,
    body_close_off: int,
) -> list[SymbolRange]:
    """Index each method / arrow-field of a class body as its own symbol.

    The method body span is approximated the same scale-invariant way the
    top-level extractor uses: a method runs until the NEXT method's start
    line minus one (or the class body close for the last one). This gives a
    tight per-method line range so a member call resolves to ONE method's
    lines, not the whole 1800-line class. Constructors are tagged
    ``kind="constructor"`` so the call-graph resolver can attribute them on
    ``new Class()`` separately.

    Only DIRECT members of THIS class body are indexed — declarations that
    live in a nested block (deeper brace depth than the class body) are
    skipped, so inner-function locals never masquerade as methods.
    """
    # Identify the line numbers spanned by the class body.
    body_open_line = source.count("\n", 0, body_open_off) + 1
    body_close_line = source.count("\n", 0, body_close_off) + 1
    lines = source.splitlines()

    # Pre-compute, per line index, the brace depth RELATIVE to the class
    # body open, so we only accept method declarations at the body's
    # immediate level (depth 1 inside the class). Track string/comment
    # state across lines to avoid counting braces in literals.
    raw: list[tuple[int, str, str]] = []  # (start_line, name, kind)
    depth = 0
    in_str: str | None = None
    in_block_comment = False
    # Walk char-by-char from body_open_off to body_close_off, but record a
    # candidate only when we are exactly at depth 1 (immediate member) and
    # at the START of a line's first significant token.
    # Simpler + robust: iterate lines, maintain depth at line START.
    # Compute depth at the start of each line by scanning the body once.
    line_start_depth: dict[int, int] = {}
    i = body_open_off
    cur_line = body_open_line
    line_start_depth[cur_line] = 0
    n = body_close_off
    while i <= n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if c == "\n":
            cur_line += 1
            line_start_depth[cur_line] = depth
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c == "/" and nxt == "/":
            # line comment — skip to EOL
            j = source.find("\n", i)
            if j < 0:
                break
            i = j
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
        i += 1

    seen: set[str] = set()
    for ln in range(body_open_line, body_close_line + 1):
        idx = ln - 1
        if idx < 0 or idx >= len(lines):
            continue
        # Only immediate members: the line must START at class-body depth 1.
        if line_start_depth.get(ln, 99) != 1:
            continue
        line_txt = lines[idx]
        stripped = line_txt.strip()
        if stripped.startswith("constructor"):
            if "constructor" not in seen and re.match(
                r"constructor\s*\(", stripped,
            ):
                seen.add("constructor")
                raw.append((ln, "constructor", "constructor"))
            continue
        m = _RE_TS_METHOD.match(line_txt)
        if not m:
            continue
        name = m.group(1)
        if name in _TS_NON_METHOD_KEYWORDS or name in seen:
            continue
        seen.add(name)
        raw.append((ln, name, "method"))

    if not raw:
        return []
    raw.sort(key=lambda x: x[0])
    out: list[SymbolRange] = []
    for k, (start, name, kind) in enumerate(raw):
        end = (
            raw[k + 1][0] - 1
            if k + 1 < len(raw)
            else body_close_line
        )
        out.append(SymbolRange(
            name=name,
            start_line=start,
            end_line=max(start, end),
            kind=kind,
            parent=class_name,
        ))
    return out


def _extract_all_class_methods(source: str) -> list[SymbolRange]:
    """Index methods for EVERY class declared in a TS/JS source file."""
    out: list[SymbolRange] = []
    for m in _RE_TS_CLASS_DECL.finditer(source):
        class_name = m.group(1)
        span = _find_class_body_span(source, m.start())
        if span is None:
            continue
        out.extend(
            _extract_class_methods(source, class_name, span[0], span[1])
        )
    return out


def extract_symbol_ranges(source: str) -> list[SymbolRange]:
    """Extracts line ranges for each exported symbol in TS/JS source.

    MVP heuristic: each export's end_line = next export's start_line - 1,
    or EOF for the last export. This avoids complex brace-balancing but
    gives reasonable line attribution for most files.

    Additionally indexes class METHODS / constructors / arrow class-fields
    as their own ``SymbolRange`` (kind ``method`` / ``constructor``,
    ``parent`` = class name) so a member call ``obj.findById()`` resolves
    to the specific method body rather than the whole enclosing class. The
    class symbol itself is KEPT (feature anchoring depends on it).
    """
    total_lines = source.count("\n") + 1
    # Collect all export positions with their symbol names and kinds
    exports: list[tuple[int, str, str]] = []  # (start_line, name, kind)

    for match in _RE_NAMED_EXPORT.finditer(source):
        line = source[:match.start()].count("\n") + 1
        name = match.group(1)
        # Determine kind from the keyword before the name
        text = source[match.start():match.end()]
        if "function" in text:
            kind = "function"
        elif "class" in text:
            kind = "class"
        else:
            kind = "const"
        exports.append((line, name, kind))

    for match in _RE_DEFAULT_EXPORT.finditer(source):
        line = source[:match.start()].count("\n") + 1
        name = match.group(1)
        text = source[match.start():match.end()]
        kind = "class" if "class" in text else "function"
        exports.append((line, name, kind))

    for match in _RE_TYPE_EXPORT.finditer(source):
        line = source[:match.start()].count("\n") + 1
        name = match.group(1)
        text = source[match.start():match.end()]
        if "enum" in text:
            kind = "enum"
        elif "interface" in text:
            kind = "type"
        else:
            kind = "type"
        exports.append((line, name, kind))

    for match in _RE_REEXPORT.finditer(source):
        line = source[:match.start()].count("\n") + 1
        for token in match.group(1).split(","):
            parts = token.strip().split(" as ")
            name = parts[-1].strip()
            if name:
                exports.append((line, name, "reexport"))

    # Top-level LOCAL function / arrow declarations. Recorded with
    # kind="local" so callers can tell them apart from exports. They are
    # appended AFTER exports so that a name which is BOTH exported and
    # locally declared keeps its export kind (first-occurrence dedup
    # below). This is what lets the call-graph tracer resolve handler
    # references like ``const POST = wrap(postHandler)`` whose body lives
    # in a non-exported ``postHandler`` symbol. STRUCTURAL + stack-neutral
    # (no hardcoded wrapper or handler names).
    for match in _RE_LOCAL_FUNC_DECL.finditer(source):
        line = source[:match.start()].count("\n") + 1
        exports.append((line, match.group(1), "local"))
    for match in _RE_LOCAL_ARROW_DECL.finditer(source):
        line = source[:match.start()].count("\n") + 1
        exports.append((line, match.group(1), "local"))

    # Class METHODS / constructors as their own tight ranges (computed by a
    # brace-balanced scan, independent of the top-level next-export
    # heuristic). Appended below AFTER top-level dedup so a method named the
    # same as a top-level symbol never shadows the top-level one — methods
    # are namespaced by ``parent`` and resolved member-aware downstream.
    method_ranges = _extract_all_class_methods(source)

    if not exports and not method_ranges:
        return []

    # Sort by start_line, deduplicate by name (keep first occurrence so an
    # exported symbol's kind wins over a later same-name local match).
    exports.sort(key=lambda x: x[0])
    seen: set[str] = set()
    unique: list[tuple[int, str, str]] = []
    for start, name, kind in exports:
        if name not in seen:
            seen.add(name)
            unique.append((start, name, kind))

    # Assign end_line: next export's start_line - 1, or EOF for last
    ranges = []
    for i, (start, name, kind) in enumerate(unique):
        if i + 1 < len(unique):
            end = unique[i + 1][0] - 1
        else:
            end = total_lines
        ranges.append(SymbolRange(
            name=name, start_line=start, end_line=max(start, end), kind=kind,
        ))

    # Append method-level symbols (kept distinct from top-level symbols via
    # their ``parent`` field; a member call resolves to these tight ranges).
    ranges.extend(method_ranges)

    return ranges


def get_symbol_range(
    rel_path: str,
    source: str,
    symbol_name: str,
) -> SymbolRange | None:
    """Return the line range of a single named symbol in ``source``.

    Sprint 12 Day 4 — used by the per-flow symbol picker to resolve
    a Haiku-selected ``(file, symbol)`` pair into a concrete
    ``SymbolRange`` with start_line / end_line. Returns ``None`` if
    the symbol cannot be located.

    Routes through the appropriate language extractor based on file
    extension:
        - ``.py`` → Python class / function regex
        - ``.ts`` / ``.tsx`` / ``.js`` / ``.jsx`` / ``.mts`` / ``.cts``
          → TS/JS export regex
        - other → ``None`` (not yet supported)
    """
    if not symbol_name:
        return None
    ext = ""
    dot = rel_path.rfind(".")
    if dot >= 0:
        ext = rel_path[dot:].lower()

    if ext == ".py":
        sig = _parse_python_file(rel_path, source)
        ranges = sig.symbol_ranges
    elif ext in _RUBY_EXTENSIONS:
        sig = _parse_ruby_file(rel_path, source)
        ranges = sig.symbol_ranges
    elif ext in {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}:
        ranges = extract_symbol_ranges(source)
    else:
        return None

    for r in ranges:
        if r.name == symbol_name:
            return r
    return None


def list_exported_symbols(rel_path: str, source: str) -> list[SymbolRange]:
    """All exported / top-level symbols in a file with their ranges.

    Sprint 12 Day 4 — used by the symbol picker to feed the candidate
    list into Haiku. Empty list when language is unsupported or the
    file has no symbols.
    """
    ext = ""
    dot = rel_path.rfind(".")
    if dot >= 0:
        ext = rel_path[dot:].lower()
    if ext == ".py":
        return _parse_python_file(rel_path, source).symbol_ranges
    if ext in _RUBY_EXTENSIONS:
        return _parse_ruby_file(rel_path, source).symbol_ranges
    if ext in {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}:
        return extract_symbol_ranges(source)
    return []


def extract_named_imports(source: str) -> dict[str, set[str]]:
    """Extracts named imports from TS/JS source.

    Returns:
        Dict mapping module path → set of imported symbol names.
        For namespace imports (import * as X), returns {"*"} as the symbol set.
    """
    result: dict[str, set[str]] = {}

    # NO module-prefix filter here: the old ``./`` / ``@/`` / ``~/``
    # guard dropped workspace-scoped modules (``@scope/pkg/...``) and
    # custom aliases before resolution ever ran (WS3 oracle, 2026-07-03).
    # Callers resolve each module against the tracked file set — external
    # packages simply resolve to ``None`` and stay silent.
    for match in _RE_NAMED_IMPORT.finditer(source):
        names_str = match.group(1)
        module = match.group(2)
        names = set()
        for token in names_str.split(","):
            parts = token.strip().split(" as ")
            original = parts[0].strip()
            # Inline type modifier: ``import { type Foo, Bar } from …``.
            if original.startswith("type "):
                original = original[len("type "):].strip()
            if original:
                names.add(original)
        if names:
            result.setdefault(module, set()).update(names)

    for match in _RE_NAMESPACE_IMPORT.finditer(source):
        module = match.group(1)
        result.setdefault(module, set()).add("*")

    return result
