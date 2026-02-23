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
import re
from dataclasses import dataclass, field
from pathlib import Path


_TS_JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

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


@dataclass
class FileSignature:
    path: str
    exports: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)

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
        if Path(rel_path).suffix.lower() not in _TS_JS_EXTENSIONS:
            continue
        abs_path = root / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        sig = _parse_file(rel_path, source)
        if not sig.is_empty():
            result[rel_path] = sig

    return result


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

    # Collect imports (only internal/relative, skip node_modules)
    for match in _RE_IMPORT.finditer(source):
        src = match.group(1)
        if src.startswith(".") or src.startswith("@/") or src.startswith("~/"):
            sig.imports.append(src)

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
