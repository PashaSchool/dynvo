"""Stage 3 — flow-reach enrichment (deterministic, NO LLM).

Walks the import / call graph from a flow's entry point outward via
BFS and returns the set of reachable files. The result feeds
``Flow.paths`` so Stage 5.5's bipartite store can detect cross-cutting
flows (flows whose reachable file set spans more than one feature's
primary path attribution).

Why this is a separate module
=============================

Stage 3's LLM-driven flow detector emits one entry-point file per
flow. The bipartite store in Stage 5.5 derives ``secondary_features``
from path overlap — but when every flow has exactly one path and that
path is attributed to exactly one feature, the secondary set is
ALWAYS empty by construction. Sprint B1 shipped the store but the
signal was zero on all six validation repos (see commit ``ea0bb0f``).

This module fixes that by enriching each flow with its transitive
file set. Pure structural: AST + regex over imports, capped at
``max_depth=3`` hops and ``max_paths=8`` total files. No LLM.

Caps + safety
=============

  - ``max_depth = 3`` — handler → service → repository captures the
    dominant Layer 1 pattern. Deeper walks hit shared infra (logging,
    DB clients) that aren't part of the flow's narrative.
  - ``max_paths = 8`` — caps payload size; trigger.dev with 510 flows
    × 8 paths = 4080 path entries, still tractable for landing JSON.
  - Same caps for EVERY language (per [[rule-no-magic-tuning]]). The
    caps are scale-invariant — depth/breadth bounds applied identically.
  - Cycle detection via visited set per call.
  - Test files, vendor dirs, and generated files are filtered.

Multi-language reach
====================

  - TS / JS / TSX / JSX / MJS / CJS: reuse the existing
    :func:`faultline.analyzer.import_graph._resolve_import` resolver,
    which handles ``./foo``, ``../bar``, ``@/foo``, tsconfig path
    aliases, and monorepo bare imports.
  - Python (``.py``): regex over ``from X import Y`` and ``import X``;
    resolve dotted modules to files under repo root (e.g.
    ``from foo.bar.baz import x`` → ``foo/bar/baz.py`` or
    ``foo/bar/baz/__init__.py``).
  - Go (``.go``): regex over ``import (...)`` blocks; resolve internal
    module-path prefixes only (anything matching the repo's go.mod
    module path).
  - Rust (``.rs``): regex over ``use crate::X::Y`` and ``mod X``; map
    to ``src/X/Y.rs`` / ``X/mod.rs`` candidates.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.analyzer.ast_extractor import (
    FileSignature,
    extract_signatures,
)
from faultline.analyzer.import_graph import (
    _resolve_import,
    detect_monorepo_packages,
    detect_workspace_package_map,
    load_tsconfig_paths,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Caps (scale-invariant; identical for every language) ─────────────────

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_PATHS = 8

# Test / vendor / generated paths to exclude from reach.
# (Lists are explicit and short; we never want to mis-classify a real
# source file. Wide test detection lives in test-pattern-library.)
_TEST_PATH_MARKERS = (
    "/tests/", "/test/", "/__tests__/", "/spec/",
    ".test.", ".spec.", "_test.go",
)
_VENDOR_PATH_MARKERS = (
    "/node_modules/", "/vendor/", "/venv/", "/.venv/",
    "/dist/", "/build/", "/out/", "/.next/", "/.turbo/",
    "/target/", "/__pycache__/", "/.pytest_cache/",
)
_GENERATED_PATH_MARKERS = (
    ".generated.", "/generated/", "/.generated/",
    ".pb.go", ".pb.cc", ".pb.h",
    ".d.ts",  # type declarations — not first-class source for reach
)

_TS_JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_PYTHON_EXTENSION = ".py"
_GO_EXTENSION = ".go"
_RUST_EXTENSION = ".rs"


# ── Public result shape ──────────────────────────────────────────────────


@dataclass(frozen=True)
class FlowReach:
    """Reachable file set for one flow, plus the depth walked.

    Attributes:
        entry_file: starting file (always first in ``reached_paths``).
        entry_line: 1-indexed start line of the flow's entry symbol.
        reached_paths: entry + transitive callees (BFS), capped at
            ``max_paths``. Stable order: entry first, then BFS-visit
            order.
        depth_reached: deepest BFS layer the walk actually reached
            (0 = only the entry file; 1 = entry + direct imports; …).
    """

    entry_file: str
    entry_line: int
    reached_paths: tuple[str, ...]
    depth_reached: int


# ── Python import resolver ───────────────────────────────────────────────

# Catches both:  from foo.bar import baz   AND   import foo.bar
_RE_PY_FROM_IMPORT = re.compile(
    r"^\s*from\s+([.\w]+)\s+import\s+", re.MULTILINE,
)
_RE_PY_IMPORT = re.compile(
    r"^\s*import\s+([\w.]+)(?:\s+as\s+\w+)?\s*$", re.MULTILINE,
)


def compute_python_source_roots(file_set: frozenset[str]) -> tuple[str, ...]:
    """Deterministically discover Python *source-root* directories.

    A "source root" is a directory that gets prepended to ``sys.path``
    at runtime, so absolute imports inside the repo are written relative
    to it rather than to the repo root. The dominant cases:

        * repo root itself (``src``-less layout) — always included as
          ``""``.
        * a ``src/`` layout (``src/<pkg>/__init__.py``) → ``src``.
        * a service / app sub-directory that is itself NOT a package
          but contains top-level packages (``backend/agent/__init__.py``
          with no ``backend/__init__.py``) → ``backend``. This is the
          standard FastAPI / Django / monorepo-service convention.

    We infer roots structurally (no per-repo paths, no config): a
    directory ``D`` is a source root iff some immediate child directory
    of ``D`` is a Python *package* (contains ``__init__.py``) while
    ``D`` itself is NOT a package. Walking up stops at the first
    packageless ancestor, which is exactly the importable base.

    Returns a stable, de-duplicated tuple including ``""`` (repo root)
    first. Pure function of ``file_set`` — cheap to compute once and
    cache on :class:`ReachContext`.
    """
    package_dirs: set[str] = set()
    for path in file_set:
        if path.endswith("/__init__.py"):
            package_dirs.add(path[: -len("/__init__.py")])
        elif path == "__init__.py":
            package_dirs.add("")

    roots: set[str] = {""}
    for pkg in package_dirs:
        # The source root for this package is its nearest ancestor that
        # is itself NOT a package. Walk up while the parent is a package.
        parent = pkg.rsplit("/", 1)[0] if "/" in pkg else ""
        # Climb until ``parent`` is not a package dir.
        while parent and parent in package_dirs:
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        roots.add(parent)

    # Stable ordering: repo root first, then shallow → deep, then lexical.
    ordered = sorted(roots, key=lambda r: (r.count("/") if r else -1, r))
    return tuple(ordered)


def _resolve_python_module(
    importer: str,
    module: str,
    file_set: frozenset[str],
    *,
    source_roots: tuple[str, ...] = ("",),
) -> str | None:
    """Resolve a Python dotted module path to a file in ``file_set``.

    Handles:
        * absolute: ``foo.bar.baz`` → ``foo/bar/baz.py`` or
          ``foo/bar/baz/__init__.py``, tried under every directory in
          ``source_roots`` (repo root + any inferred ``sys.path`` base
          such as ``src`` or a ``backend`` service dir). This is what
          makes ``from agent.detector_tools import x`` resolve when the
          file actually lives at ``backend/agent/detector_tools.py``.
        * relative: ``.sibling`` / ``..parent.child`` resolved against
          the importer's package directory (source-root-independent).

    Returns ``None`` when the module resolves outside ``file_set``
    (third-party / stdlib).
    """
    if not module:
        return None

    # Relative imports — count leading dots.
    leading_dots = 0
    while leading_dots < len(module) and module[leading_dots] == ".":
        leading_dots += 1
    rest = module[leading_dots:]

    if leading_dots > 0:
        # Walk up from the importer's directory.
        importer_dir = Path(importer).parent
        # 1 dot = same package, 2 = parent, etc.
        parts = importer_dir.parts
        # leading_dots-1 levels up; if we exceed root, bail.
        up = leading_dots - 1
        if up > len(parts):
            return None
        base_parts = parts[: len(parts) - up] if up > 0 else parts
        base = "/".join(base_parts)
        bases: tuple[str, ...] = (base,)
    else:
        # Absolute import: try each candidate source root as the base.
        bases = source_roots or ("",)

    if rest:
        rest_path = rest.replace(".", "/")
    else:
        rest_path = ""

    for base in bases:
        if rest_path:
            candidate_stem = f"{base}/{rest_path}" if base else rest_path
        else:
            candidate_stem = base
        candidate_stem = candidate_stem.lstrip("/")
        if not candidate_stem:
            continue
        # Try foo/bar/baz.py first, then foo/bar/baz/__init__.py.
        for candidate in (
            f"{candidate_stem}.py",
            f"{candidate_stem}/__init__.py",
        ):
            if candidate in file_set:
                return candidate

    return None


# ── Go import resolver ───────────────────────────────────────────────────

# Catches both:  import "path/to/pkg"  AND grouped  import ( "a" "b" )
_RE_GO_IMPORT_BLOCK = re.compile(
    r"import\s*\(\s*((?:.|\n)*?)\)", re.MULTILINE,
)
_RE_GO_IMPORT_SINGLE = re.compile(
    r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', re.MULTILINE,
)
_RE_GO_IMPORT_LINE = re.compile(r'(?:\w+\s+)?"([^"]+)"')


def _go_module_path(repo_root: Path) -> str | None:
    """Read the ``module`` directive from ``go.mod`` if present."""
    gomod = repo_root / "go.mod"
    if not gomod.exists():
        return None
    try:
        for line in gomod.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("module "):
                return line.removeprefix("module ").strip()
    except OSError:
        return None
    return None


def _extract_go_imports(source: str) -> list[str]:
    """Pull all import paths from a Go file (grouped + single)."""
    paths: list[str] = []
    for block_match in _RE_GO_IMPORT_BLOCK.finditer(source):
        block = block_match.group(1)
        for m in _RE_GO_IMPORT_LINE.finditer(block):
            paths.append(m.group(1))
    for m in _RE_GO_IMPORT_SINGLE.finditer(source):
        paths.append(m.group(1))
    return paths


def _resolve_go_import(
    import_path: str,
    file_set: frozenset[str],
    module_prefix: str | None,
) -> str | None:
    """Resolve a Go import to an internal file.

    Only INTERNAL imports (matching the repo's ``go.mod`` module
    prefix) resolve. ``import "github.com/other/pkg"`` returns None.
    Returns the first ``.go`` file found under the package directory
    (Go packages are directories; we pick a stable representative).
    """
    if not module_prefix or not import_path.startswith(module_prefix):
        return None
    sub = import_path.removeprefix(module_prefix).lstrip("/")
    if not sub:
        return None
    # Find any .go file under sub/ (non-test). Pick lexicographically
    # smallest for stability.
    prefix = f"{sub}/"
    matches = sorted(
        p for p in file_set
        if p.startswith(prefix) and p.endswith(".go")
        and not p.endswith("_test.go")
    )
    return matches[0] if matches else None


# ── Rust import resolver ─────────────────────────────────────────────────

_RE_RUST_USE_CRATE = re.compile(
    r"\buse\s+crate::([\w:]+)", re.MULTILINE,
)
_RE_RUST_MOD = re.compile(
    r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", re.MULTILINE,
)


def _resolve_rust_use(
    importer: str,
    use_path: str,
    file_set: frozenset[str],
) -> str | None:
    """Resolve ``use crate::foo::bar`` to ``src/foo/bar.rs`` or
    ``src/foo/bar/mod.rs``. ``use_path`` is the ``::``-joined segment
    after ``crate::``.
    """
    if not use_path:
        return None
    segments = use_path.split("::")
    # Drop the trailing item name (could be a function/struct, not a file).
    # Try both with and without it.
    candidates = []
    for depth in (len(segments), max(1, len(segments) - 1)):
        stem = "/".join(segments[:depth])
        # Find the crate root — the importer's nearest src/ ancestor.
        parts = Path(importer).parts
        if "src" in parts:
            i = parts.index("src")
            crate_src = "/".join(parts[: i + 1])
            candidates.append(f"{crate_src}/{stem}.rs")
            candidates.append(f"{crate_src}/{stem}/mod.rs")
        # Also try top-level src/ for single-crate repos.
        candidates.append(f"src/{stem}.rs")
        candidates.append(f"src/{stem}/mod.rs")
    for c in candidates:
        if c in file_set:
            return c
    return None


def _resolve_rust_mod(
    importer: str,
    mod_name: str,
    file_set: frozenset[str],
) -> str | None:
    """Resolve ``mod foo;`` to a sibling file ``foo.rs`` or
    ``foo/mod.rs`` under the importer's directory.
    """
    importer_dir = str(Path(importer).parent)
    for c in (
        f"{importer_dir}/{mod_name}.rs",
        f"{importer_dir}/{mod_name}/mod.rs",
    ):
        if c in file_set:
            return c
    return None


# ── Per-file edge extraction ─────────────────────────────────────────────


def _file_edges(
    rel_path: str,
    signatures: dict[str, FileSignature],
    file_set: frozenset[str],
    *,
    alias_map: dict[str, str],
    monorepo_packages: set[str] | None,
    go_module_prefix: str | None,
    repo_path: Path,
    python_source_roots: tuple[str, ...] = ("",),
    workspace_package_map: dict[str, str] | None = None,
) -> list[str]:
    """Return the set of files ``rel_path`` reaches via one hop.

    Centralises the language switch so callers (the BFS) stay clean.
    """
    suffix = Path(rel_path).suffix.lower()
    # INSERTION-ORDERED dedup (dict-as-ordered-set), NOT a set: the BFS
    # truncates the frontier at max_paths, and set iteration order is
    # PYTHONHASHSEED-randomised per process — a plain set made WHICH
    # neighbors survive the cap differ between two runs of the same scan
    # (Flow.paths drift → bipartite shared_with/secondary + testmap drift;
    # diagnosed on supabase 2026-07-02). Neighbors now keep deterministic
    # source-import order.
    out: dict[str, None] = {}

    # TS/JS: use the existing ast_extractor signatures + resolver.
    if suffix in _TS_JS_EXTENSIONS:
        sig = signatures.get(rel_path)
        if sig is None:
            return []
        for imp in sig.imports:
            resolved = _resolve_import(
                rel_path, imp, file_set,
                alias_map=alias_map,
                monorepo_packages=monorepo_packages,
                workspace_package_map=workspace_package_map,
                repo_root=str(repo_path),
            )
            if resolved and resolved != rel_path:
                out[resolved] = None
        return list(out)

    # The other languages don't have imports populated on FileSignature
    # today — read the source directly. Cheap (only ever called on
    # files that landed in BFS frontier; bounded by max_paths).
    abs_path = repo_path / rel_path
    try:
        source = abs_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    if suffix == _PYTHON_EXTENSION:
        for match in _RE_PY_FROM_IMPORT.finditer(source):
            resolved = _resolve_python_module(
                rel_path, match.group(1), file_set,
                source_roots=python_source_roots,
            )
            if resolved and resolved != rel_path:
                out[resolved] = None
        for match in _RE_PY_IMPORT.finditer(source):
            resolved = _resolve_python_module(
                rel_path, match.group(1), file_set,
                source_roots=python_source_roots,
            )
            if resolved and resolved != rel_path:
                out[resolved] = None
        return list(out)

    if suffix == _GO_EXTENSION:
        for imp in _extract_go_imports(source):
            resolved = _resolve_go_import(imp, file_set, go_module_prefix)
            if resolved and resolved != rel_path:
                out[resolved] = None
        return list(out)

    if suffix == _RUST_EXTENSION:
        for match in _RE_RUST_USE_CRATE.finditer(source):
            resolved = _resolve_rust_use(rel_path, match.group(1), file_set)
            if resolved and resolved != rel_path:
                out[resolved] = None
        for match in _RE_RUST_MOD.finditer(source):
            resolved = _resolve_rust_mod(rel_path, match.group(1), file_set)
            if resolved and resolved != rel_path:
                out[resolved] = None
        return list(out)

    return []


# ── Filter helpers ───────────────────────────────────────────────────────


def _is_test_or_vendor_or_generated(path: str) -> bool:
    """True if ``path`` matches any test / vendor / generated marker."""
    # Normalise leading slash for substring checks (markers include "/").
    needle = "/" + path
    for marker in _TEST_PATH_MARKERS:
        if marker in needle:
            return True
    for marker in _VENDOR_PATH_MARKERS:
        if marker in needle:
            return True
    for marker in _GENERATED_PATH_MARKERS:
        if marker in needle:
            return True
    return False


# ── Reach context (built ONCE per scan, reused for every flow) ───────────


@dataclass
class ReachContext:
    """Pre-computed resolver state shared across all flows in a scan.

    Building this is non-trivial (extract_signatures over the whole
    repo can take 1-3s on large monorepos); the BFS itself is O(reach
    × max_paths) which is bounded by ``max_depth`` × ``max_paths`` ≤ 24.
    Building once and passing here keeps the per-flow cost flat.
    """

    repo_path: Path
    file_set: frozenset[str]
    signatures: dict[str, FileSignature]
    alias_map: dict[str, str]
    monorepo_packages: set[str]
    go_module_prefix: str | None
    python_source_roots: tuple[str, ...] = ("",)
    # package.json#name → dir map for scoped workspace import resolution
    # ('@calcom/lib' → 'packages/lib'). Empty for non-workspace repos.
    workspace_package_map: dict[str, str] = field(default_factory=dict)


def build_reach_context(ctx: "ScanContext") -> ReachContext:
    """Construct a :class:`ReachContext` from a Stage 0 ``ScanContext``.

    Walks the tracked-file list ONCE through :func:`extract_signatures`
    so TS/JS imports are pre-parsed. Loads tsconfig path aliases +
    monorepo package names so cross-workspace imports resolve. Reads
    ``go.mod`` for the internal-module prefix.
    """
    repo_path = Path(ctx.repo_path)
    file_set = frozenset(ctx.tracked_files)
    signatures = extract_signatures(list(ctx.tracked_files), str(repo_path))
    alias_map = load_tsconfig_paths(str(repo_path))
    monorepo_packages = detect_monorepo_packages(str(repo_path))
    go_module_prefix = _go_module_path(repo_path)
    python_source_roots = compute_python_source_roots(file_set)
    workspace_package_map = detect_workspace_package_map(str(repo_path))
    return ReachContext(
        repo_path=repo_path,
        file_set=file_set,
        signatures=signatures,
        alias_map=alias_map,
        monorepo_packages=monorepo_packages,
        go_module_prefix=go_module_prefix,
        python_source_roots=python_source_roots,
        workspace_package_map=workspace_package_map,
    )


# ── Public BFS entry point ───────────────────────────────────────────────


def compute_flow_reach(
    rctx: ReachContext,
    entry_file: str,
    entry_line: int,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_paths: int = DEFAULT_MAX_PATHS,
) -> FlowReach:
    """BFS from ``entry_file`` outward over import/call edges.

    Stops when EITHER:
      * depth == ``max_depth`` (no further expansion beyond this layer);
      * ``len(reached) >= max_paths`` (payload cap reached);
      * no new files added in a layer (frontier exhausted).

    Filters out test, vendor, and generated files at frontier
    expansion time (the entry file itself is always kept, even if it
    matches a filter — Stage 3 chose it).
    """
    if max_depth < 1:
        max_depth = 1
    if max_paths < 1:
        max_paths = 1

    visited: list[str] = [entry_file]
    seen: set[str] = {entry_file}
    depth_reached = 0

    # BFS layer-by-layer so ``depth_reached`` tracks the actual depth.
    frontier: deque[str] = deque([entry_file])
    for depth in range(1, max_depth + 1):
        if len(visited) >= max_paths or not frontier:
            break
        next_frontier: deque[str] = deque()
        added_this_layer = 0
        while frontier:
            current = frontier.popleft()
            try:
                neighbors = _file_edges(
                    current,
                    rctx.signatures,
                    rctx.file_set,
                    alias_map=rctx.alias_map,
                    monorepo_packages=rctx.monorepo_packages,
                    go_module_prefix=rctx.go_module_prefix,
                    repo_path=rctx.repo_path,
                    python_source_roots=rctx.python_source_roots,
                    workspace_package_map=rctx.workspace_package_map,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug("flow_reach: edge extraction failed for %s: %s",
                             current, exc)
                neighbors = []
            for n in neighbors:
                if n in seen:
                    continue
                if _is_test_or_vendor_or_generated(n):
                    continue
                seen.add(n)
                visited.append(n)
                next_frontier.append(n)
                added_this_layer += 1
                if len(visited) >= max_paths:
                    break
            if len(visited) >= max_paths:
                break
        if added_this_layer > 0:
            depth_reached = depth
        frontier = next_frontier

    return FlowReach(
        entry_file=entry_file,
        entry_line=entry_line,
        reached_paths=tuple(visited),
        depth_reached=depth_reached,
    )


__all__ = [
    "FlowReach",
    "ReachContext",
    "build_reach_context",
    "compute_flow_reach",
    "compute_python_source_roots",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PATHS",
]
