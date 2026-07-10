"""B34 Tier 1 — lazy-import edge collection ($0, deterministic).

Operator exhibit (2026-07-10): Soc0's `Uncovered: EDR — SentinelOne
routes` marker exists because `backend/services/edr/factory.py`
resolves the vendor client via FUNCTION-LEVEL imports inside
if-branches (`from services.edr.sentinelone import SentinelOneClient`
under `if target == "sentinelOne":`). The module-level import graph has
no edge to the connector, so no reachability consumer can see it.

This module collects those edges — `kind="lazy"` — WITHOUT changing any
existing traversal:

  * **Python**: a real ``ast`` walk (not regex). An ``import`` /
    ``from … import`` whose enclosing scope is a function (or any
    non-module scope) is a lazy edge. Imports under
    ``if TYPE_CHECKING:`` are EXCLUDED (type-only, not reachability).
    Imports inside a ``try:`` whose handler catches ``ImportError`` /
    ``ModuleNotFoundError`` are marked ``optional=True`` (vendored /
    optional deps) — edge exists, consumers may ignore.
  * **TS/JS**: literal dynamic specifiers only — ``import('x')``,
    non-top-level ``require('x')`` — no expression evaluation.

Targets resolve to repo-relative files via a deterministic suffix index
built from the tracked-file list (Python dotted-module candidates; TS
path-suffix candidates tolerant of ``./``/``../``/``@/`` prefixes).
Unresolved targets (external packages) are dropped — this is a
REPO-INTERNAL reachability surface.

Emission: side-channel only (stage artifact + in-memory input for the
B34 Tier-2 dispatch-registry stage). Scan JSON is untouched — with
``FAULTLINE_LAZY_IMPORT_EDGES=0`` (default) the collection never runs
unless Tier 2 requests it in memory.

Determinism: tracked files are processed in sorted order; every output
list is sorted; no set iteration on the emitted path.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "LAZY_IMPORT_EDGES_ENV",
    "LazyImportEdge",
    "lazy_import_edges_enabled",
    "collect_lazy_import_edges",
    "build_py_module_index",
    "build_ts_suffix_index",
]

LAZY_IMPORT_EDGES_ENV = "FAULTLINE_LAZY_IMPORT_EDGES"

#: Bounded read — route/service modules are small; this only guards
#: pathological blobs (mirrors the census bound).
_MAX_BYTES = 1_500_000

_PY_EXT = (".py",)
_TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

_TS_DYNAMIC_RE = re.compile(r"\bimport\(\s*['\"]([^'\"]+)['\"]")
_TS_REQUIRE_RE = re.compile(r"^(\s*).*?\brequire\(\s*['\"]([^'\"]+)['\"]")


def lazy_import_edges_enabled() -> bool:
    """Default ON since the 2026-07-10 keyed Soc0 OFF/ON A/B (markers 3->1,
    both exhibit classes dissolved, validator 8->7, gauntlet CLEAR both
    sides). ``=0`` restores the pre-B34 artifact byte-identically."""
    return os.environ.get(LAZY_IMPORT_EDGES_ENV, "1").strip() in {
        "1", "true", "True",
    }


@dataclass(frozen=True)
class LazyImportEdge:
    """One function-level / dynamic import: ``src`` lazily loads ``target``.

    ``target_file`` is the repo-relative resolution (``None`` never
    happens on emitted edges — unresolved targets are dropped).
    """

    src: str
    target: str          # dotted module (py) / specifier (ts), as written
    target_file: str
    lang: str            # "py" | "ts"
    optional: bool = False
    kind: str = "lazy"


# ── target resolution indexes ───────────────────────────────────────────


def build_py_module_index(files: list[str]) -> dict[str, str]:
    """dotted-module-candidate → repo file (shortest-suffix candidates
    included so ``services.edr.sentinelone`` resolves regardless of the
    backend root). First (sorted) writer wins — deterministic."""
    index: dict[str, str] = {}
    for rel in sorted(files):
        if not rel.endswith(".py"):
            continue
        stem = rel[:-3]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        parts = stem.replace("\\", "/").split("/")
        for i in range(len(parts)):
            index.setdefault(".".join(parts[i:]), rel)
    return index


def _ts_norm(spec: str) -> str | None:
    spec = spec.split("?")[0]
    spec = re.sub(r"\.(ts|tsx|js|jsx|mjs|cjs)$", "", spec)
    spec = re.sub(r"^(\.\./)+|^\./", "", spec)
    spec = re.sub(r"^[@~]/", "", spec)
    parts = [p for p in spec.split("/") if p and p != "."]
    if not parts:
        return None
    return "/".join(parts[-3:])


def build_ts_suffix_index(files: list[str]) -> dict[str, str]:
    """path-suffix key → repo file for TS/JS modules (`/index` folded)."""
    index: dict[str, str] = {}
    for rel in sorted(files):
        if not rel.endswith(_TS_EXT):
            continue
        stem = re.sub(r"\.(ts|tsx|js|jsx|mjs|cjs)$", "", rel)
        cands = [stem]
        if stem.endswith("/index"):
            cands.append(stem[: -len("/index")])
        for c in cands:
            parts = c.split("/")
            for k in (3, 2, 1):
                if len(parts) >= k:
                    index.setdefault("/".join(parts[-k:]), rel)
    return index


# ── Python AST walk ─────────────────────────────────────────────────────


def _resolve_relative(src_rel: str, level: int, module: str | None) -> str:
    """Absolute dotted module for a relative ``from . import`` in ``src_rel``."""
    parts = src_rel[:-3].replace("\\", "/").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    hops = max(level - 1, 0)
    if hops:
        parts = parts[:-hops] if hops < len(parts) else []
    tail = module.split(".") if module else []
    return ".".join([*parts, *tail])


def _py_lazy_imports(text: str, src_rel: str) -> list[tuple[str, bool]]:
    """(dotted_module, optional) for every function-scoped import.

    TYPE_CHECKING subtrees are skipped entirely; ``try`` bodies whose
    handlers catch ImportError/ModuleNotFoundError mark ``optional``.
    Relative imports resolve against ``src_rel``'s package path.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    out: list[tuple[str, bool]] = []

    def _is_type_checking(test: ast.expr) -> bool:
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )

    def _handler_catches_import_error(node: ast.Try) -> bool:
        for h in node.handlers:
            types: list[ast.expr] = []
            if h.type is None:
                return True  # bare except swallows ImportError too
            if isinstance(h.type, ast.Tuple):
                types = list(h.type.elts)
            else:
                types = [h.type]
            for t in types:
                name = t.attr if isinstance(t, ast.Attribute) else getattr(
                    t, "id", "",
                )
                if name in ("ImportError", "ModuleNotFoundError"):
                    return True
        return False

    def walk(node: ast.AST, in_func: bool, optional: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                # Type-only imports: skip the body, keep walking orelse.
                for sub in child.orelse:
                    walk(sub, in_func, optional)
                continue
            child_in_func = in_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            )
            child_optional = optional or (
                isinstance(child, ast.Try)
                and _handler_catches_import_error(child)
            )
            if in_func and isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module:
                    out.append((child.module, optional))
                elif child.level > 0:
                    resolved = _resolve_relative(
                        src_rel, child.level, child.module,
                    )
                    if resolved:
                        out.append((resolved, optional))
            elif in_func and isinstance(child, ast.Import):
                for alias in child.names:
                    out.append((alias.name, optional))
            walk(child, child_in_func, child_optional)

    walk(tree, in_func=False, optional=False)
    return out


# ── public entry point ──────────────────────────────────────────────────


def collect_lazy_import_edges(
    repo_path: Path | str,
    tracked_files: list[str],
) -> list[LazyImportEdge]:
    """Collect resolved repo-internal lazy-import edges, sorted."""
    root = Path(repo_path)
    files = [str(f).replace("\\", "/") for f in tracked_files]
    py_index = build_py_module_index(files)
    ts_index = build_ts_suffix_index(files)

    edges: list[LazyImportEdge] = []
    for rel in sorted(files):
        if not rel.endswith(_PY_EXT + _TS_EXT):
            continue
        p = root / rel
        try:
            if p.stat().st_size > _MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if rel.endswith(".py"):
            for module, optional in _py_lazy_imports(text, rel):
                target = py_index.get(module)
                if target and target != rel:
                    edges.append(LazyImportEdge(
                        src=rel, target=module, target_file=target,
                        lang="py", optional=optional,
                    ))
        else:
            for line in text.splitlines():
                for spec in _TS_DYNAMIC_RE.findall(line):
                    key = _ts_norm(spec)
                    target = ts_index.get(key) if key else None
                    if target and target != rel:
                        edges.append(LazyImportEdge(
                            src=rel, target=spec, target_file=target,
                            lang="ts",
                        ))
                m = _TS_REQUIRE_RE.match(line)
                if m and m.group(1) != "":
                    key = _ts_norm(m.group(2))
                    target = ts_index.get(key) if key else None
                    if target and target != rel:
                        edges.append(LazyImportEdge(
                            src=rel, target=m.group(2), target_file=target,
                            lang="ts",
                        ))
    # Deterministic, deduplicated.
    seen: set[tuple[str, str, str]] = set()
    unique: list[LazyImportEdge] = []
    for e in sorted(edges, key=lambda e: (e.src, e.target_file, e.target)):
        k = (e.src, e.target_file, e.target)
        if k in seen:
            continue
        seen.add(k)
        unique.append(e)
    return unique
