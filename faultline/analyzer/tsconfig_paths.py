"""Multi-workspace tsconfig path-alias resolver (Sprint C3, deterministic).

Why this exists
===============

The legacy :func:`faultline.analyzer.import_graph.load_tsconfig_paths`
picks the FIRST tsconfig it finds (root or ``src/``) and returns the
``compilerOptions.paths`` entries from that file alone. On real
monorepos this is structurally insufficient:

  - Root ``tsconfig.json`` is often a near-empty shell that only
    holds ``strictNullChecks: true``. It has no ``paths``.
  - Each workspace (``apps/web``, ``apps/admin``) has its own
    ``tsconfig.json`` with the real ``paths`` aliases (``@/* → ./*``).
  - Workspaces ``extends`` a shared base config from a sibling
    package (``packages/tsconfig/nextjs.json``). The base contributes
    additional aliases that must be merged.

Result: Stage 3 flow_reach BFS resolves zero imports for files inside
``apps/web/...`` because the aliased ``@/utils/foo`` import never
makes it into the alias map. Sprint C3 fixes this by walking every
tsconfig under the repo, following each ``extends`` chain, and
emitting a per-workspace alias table.

Output shape
============

A flat dict keyed by the alias PREFIX (the leading segment up to the
trailing ``/`` of the alias pattern), where each value is a tuple
``(workspace_root, target_prefix)``:

    {
        "@/": ("apps/web", "apps/web/"),
        "@/utils/": ("apps/web", "apps/web/utils/"),
        "@org/shared": ("packages/shared", "packages/shared/src/"),
    }

The keys preserve the trailing ``/`` from the original pattern so
``startswith`` matches in the resolver are unambiguous (``@/`` vs
``@/utils/``). Resolution tries the LONGEST matching prefix first
to honour the tsconfig precedence rule.

Caching
=======

The alias map is built ONCE per scan and cached on the orchestrator
(passed through as a function argument — we do NOT mutate
:class:`ScanContext`). Building involves a bounded fs walk + JSON5
parse per file; cost is O(workspaces) and < 200ms on every repo we
have benchmarked.

NO LLM. Pure file parsing.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────────

# Extensions to try when resolving a bare import (no extension given).
# Order matters: TS variants first because TypeScript projects with
# ``moduleResolution: bundler`` resolve ``import "./foo"`` to ``foo.ts``
# in preference to ``foo.js``.
_EXTENSIONS_TO_TRY: tuple[str, ...] = (
    ".ts", ".tsx", ".mts", ".cts",
    ".js", ".jsx", ".mjs", ".cjs",
)

# When a bare import resolves to a directory, try these index files.
_INDEX_FILES: tuple[str, ...] = (
    "index.ts", "index.tsx", "index.mts", "index.cts",
    "index.js", "index.jsx", "index.mjs", "index.cjs",
)

# JS → TS swap candidates for projects that import with .js but the
# file on disk is .ts (NodeNext + bundler ESM convention).
_JS_TO_TS_SWAPS: dict[str, tuple[str, ...]] = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx", ".ts"),
    ".mjs": (".mts", ".ts"),
    ".cjs": (".cts", ".ts"),
}

# Skip vendor / build dirs when looking for tsconfigs.
_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    "node_modules", ".next", ".turbo", "dist", "build", "out",
    ".venv", "venv", "__pycache__", "target", ".git",
})

# Maximum depth into the tree we walk for tsconfigs. Workspaces in
# real repos live within 3-4 levels (apps/web, packages/api/src) so
# 6 is generous. Bounds runtime in pathological deeply-nested repos.
_MAX_WALK_DEPTH = 6

# Maximum number of ``extends`` chain hops before we bail.
# Circular / pathological chains are extremely rare but cheap to guard.
_MAX_EXTENDS_DEPTH = 8


# ── Result shape ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AliasEntry:
    """One alias entry: prefix → workspace + target dir.

    Attributes:
        prefix: alias prefix INCLUDING trailing ``/`` if the original
            pattern was ``@foo/*``. For non-wildcard aliases (rare in
            modern tsconfigs) the prefix is the bare token.
        workspace_root: repo-relative directory of the tsconfig that
            DECLARED this alias (resolution is rooted here).
        target_prefix: repo-relative dir the alias resolves to,
            normalised to a trailing ``/`` for wildcard aliases.
    """

    prefix: str
    workspace_root: str
    target_prefix: str


# ── JSON5 loader ─────────────────────────────────────────────────────────


#: Perf wave R6 (2026-07-07): content-hash memo for JSONC/JSON5 parses.
#: ``build_path_alias_map`` runs from EIGHT call sites per scan (Stage
#: 2.6 closure, 6.3 import tree, 6.86 anchored mint ×2, 8.8 shared
#: members, flow_reach, snapshots (per 6.96 worktree snapshot!),
#: symbol_graph, plus the server-actions linker), each re-parsing the
#: same tsconfigs with the pure-Python json5 PEG parser — 6.5s profiled
#: on documenso. The memo is IN-PROCESS only (no on-disk state), keyed
#: by the sha256 of the file text, so identical content parses once —
#: including across 6.96's git-worktree snapshots when a tsconfig is
#: unchanged. Values are deep-copied on the way in AND out so caller
#: mutation can never poison the cache. Entries are tiny (parsed
#: tsconfigs); unbounded growth is not a concern for a scan process.
#: Thread-safety: plain dict get/set are atomic under the GIL; a race
#: costs one duplicate parse of identical content — same result.
_PARSE_CACHE: dict[str, dict | None] = {}


def _parse_jsonc(text: str) -> dict | None:
    """Content-hash-memoized :func:`_parse_jsonc_uncached` (see above)."""
    key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    if key in _PARSE_CACHE:
        cached = _PARSE_CACHE[key]
        return copy.deepcopy(cached) if cached is not None else None
    result = _parse_jsonc_uncached(text)
    _PARSE_CACHE[key] = copy.deepcopy(result) if result is not None else None
    return result


def _parse_jsonc_uncached(text: str) -> dict | None:
    """Parse a tsconfig that may carry comments / trailing commas / JSON5.

    Tries ``json5`` first (handles every form tsconfig allows). Falls
    back to a regex-strip + stdlib ``json`` so the module degrades
    gracefully when ``json5`` isn't installed.

    Returns ``None`` on any parse failure — the caller logs telemetry.
    """
    try:
        import json5  # type: ignore[import-not-found]
        return json5.loads(text)
    except ImportError:
        pass
    except Exception:
        # Fall through to the regex fallback — sometimes json5 chokes
        # on truly malformed input but the simpler stripped parser
        # still works.
        pass

    # Stdlib fallback — strip comments + trailing commas then parse.
    # Sprint C3b regression fix: the original regex stripped `//...`
    # and `/*...*/` blindly, which corrupted paths-block strings such
    # as `"@/*": ["./*"]` (the `/*` inside the JSON value was treated
    # as a block-comment opener). Walk char-by-char with awareness of
    # string-literal state so comments inside strings are preserved.
    import json as _json
    import re as _re

    def _strip_jsonc(src: str) -> str:
        out: list[str] = []
        i = 0
        n = len(src)
        in_str: str | None = None
        while i < n:
            ch = src[i]
            nxt = src[i + 1] if i + 1 < n else ""
            if in_str is not None:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(nxt)
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch in ('"', "'"):
                in_str = ch
                out.append(ch)
                i += 1
                continue
            if ch == "/" and nxt == "/":
                # Line comment — skip to newline.
                while i < n and src[i] != "\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                # Block comment — skip to closing */.
                i += 2
                while i < n - 1 and not (src[i] == "*" and src[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    stripped = _strip_jsonc(text)
    stripped = _re.sub(r",\s*([}\]])", r"\1", stripped)
    try:
        return _json.loads(stripped)
    except Exception:
        return None


# ── ``extends`` resolution ───────────────────────────────────────────────


def _resolve_extends(
    extends_ref: str,
    importer_tsconfig: Path,
    repo_root: Path,
) -> Path | None:
    """Resolve a tsconfig ``extends`` reference to an absolute path.

    Handles all four shapes the TS compiler accepts:

      * relative file (``"./base.json"`` / ``"../tsconfig.base.json"``)
      * bare relative without extension (``"tsconfig/nextjs"``)
      * npm package reference (``"tsconfig/nextjs.json"`` resolved via
        the nearest ``node_modules`` walking upward from the
        importer's directory)
      * sibling workspace package
        (``"@org/tsconfig/nextjs.json"`` resolved either via
        ``node_modules`` or by mapping ``@org/tsconfig`` → a
        ``packages/`` workspace directory).

    Returns ``None`` when the reference does not resolve to a file
    inside ``repo_root`` — the caller skips the chain step.
    """
    importer_dir = importer_tsconfig.parent

    # ── Relative file references ──
    if extends_ref.startswith("./") or extends_ref.startswith("../"):
        candidates = [
            importer_dir / extends_ref,
            (importer_dir / extends_ref).with_suffix(".json"),
        ]
        for c in candidates:
            try:
                if c.is_file():
                    resolved = c.resolve()
                    try:
                        resolved.relative_to(repo_root.resolve())
                    except ValueError:
                        return None
                    return resolved
            except OSError:
                continue
        return None

    # ── Package-style references ──
    # Walk upward from the importer's directory looking for a
    # ``node_modules/<extends_ref>`` entry. If we find one, that's our
    # tsconfig. Bounded by repo_root to avoid escaping the scan.
    rel_path = extends_ref
    if not rel_path.endswith(".json"):
        rel_path = rel_path + ".json"
    cursor = importer_dir
    repo_root_abs = repo_root.resolve()
    for _ in range(_MAX_WALK_DEPTH * 2):
        candidate = cursor / "node_modules" / rel_path
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            pass
        if cursor == repo_root_abs:
            break
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent

    # ── Sibling-workspace mapping ──
    # ``tsconfig/nextjs.json`` → ``packages/tsconfig/nextjs.json``
    # ``@org/tsconfig/nextjs.json`` → ``packages/tsconfig/nextjs.json``
    parts = rel_path.split("/")
    # Drop a leading @org/ scope (we map to the package name only).
    if parts and parts[0].startswith("@") and len(parts) >= 2:
        parts = parts[1:]
    if parts:
        for container in ("packages", "apps", "tooling"):
            candidate = repo_root / container / "/".join(parts)
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue

    return None


# ── Compiler-options merge ───────────────────────────────────────────────


def _load_compiler_options(
    tsconfig_path: Path,
    repo_root: Path,
    *,
    seen: set[Path] | None = None,
    depth: int = 0,
) -> tuple[dict, str]:
    """Read ``compilerOptions`` from ``tsconfig_path``, merging the
    ``extends`` chain.

    Returns a tuple ``(options, base_dir)`` where ``base_dir`` is the
    directory the inheritance resolved ``baseUrl`` against — that's
    the IMPORTER's directory by spec (each link in the chain inherits
    options but ``baseUrl`` always resolves against the FINAL config's
    location). We carry the importer dir explicitly to satisfy that.

    Cycle / depth-cap safe.
    """
    if seen is None:
        seen = set()
    try:
        resolved = tsconfig_path.resolve()
    except OSError:
        return {}, str(tsconfig_path.parent)
    if resolved in seen or depth > _MAX_EXTENDS_DEPTH:
        return {}, str(tsconfig_path.parent)
    seen.add(resolved)

    try:
        text = tsconfig_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}, str(tsconfig_path.parent)

    config = _parse_jsonc(text)
    if not isinstance(config, dict):
        return {}, str(tsconfig_path.parent)

    base_options: dict = {}

    # Recurse into ``extends`` first so child keys override parent.
    extends_ref = config.get("extends")
    if isinstance(extends_ref, str) and extends_ref:
        parent_path = _resolve_extends(extends_ref, tsconfig_path, repo_root)
        if parent_path is not None:
            parent_options, _ = _load_compiler_options(
                parent_path, repo_root, seen=seen, depth=depth + 1,
            )
            base_options = dict(parent_options)

    own_options = config.get("compilerOptions")
    if isinstance(own_options, dict):
        # Merge: own ``paths`` wins entirely (TS spec — paths is not
        # additive), other options shallow-merge.
        for k, v in own_options.items():
            if k == "paths" and isinstance(v, dict):
                base_options["paths"] = dict(v)
            else:
                base_options[k] = v

    return base_options, str(tsconfig_path.parent)


# ── Per-tsconfig alias extraction ────────────────────────────────────────


def _alias_entries_for(
    tsconfig_path: Path,
    repo_root: Path,
) -> list[AliasEntry]:
    """Build :class:`AliasEntry` records for one tsconfig file."""
    options, ts_dir = _load_compiler_options(tsconfig_path, repo_root)
    paths_block = options.get("paths") if isinstance(options, dict) else None
    if not isinstance(paths_block, dict) or not paths_block:
        return []
    base_url = (
        options.get("baseUrl") if isinstance(options.get("baseUrl"), str) else "."
    )

    repo_root_abs = repo_root.resolve()
    ts_dir_abs = Path(ts_dir).resolve()

    # Compute workspace_root = tsconfig directory relative to repo root,
    # using POSIX separators so it joins cleanly with import targets.
    try:
        workspace_root = ts_dir_abs.relative_to(repo_root_abs).as_posix()
    except ValueError:
        return []
    if workspace_root in ("", "."):
        workspace_root = ""

    entries: list[AliasEntry] = []
    for pattern, targets in paths_block.items():
        if not isinstance(pattern, str) or not isinstance(targets, list):
            continue
        if not targets:
            continue
        # Emit one entry per target, in DECLARED order (TS spec — the
        # resolver tries each target in order until one resolves).
        for target in targets:
            if not isinstance(target, str):
                continue

            # Normalise the prefix.
            if pattern.endswith("/*"):
                prefix = pattern[:-1]  # ``@/*`` → ``@/``
                target_clean = target[:-1] if target.endswith("/*") else target
            else:
                prefix = pattern
                target_clean = target

            # Resolve target against (ts_dir, baseUrl). Combine and
            # normalise to repo-relative POSIX.
            absolute_target = (ts_dir_abs / base_url / target_clean).resolve()
            try:
                target_rel = absolute_target.relative_to(repo_root_abs).as_posix()
            except ValueError:
                # Target escapes repo root — skip (e.g. ``../../node_modules/...``).
                continue

            # Normalise the "same directory" sentinel from pathlib to an
            # empty string so prefix concatenation produces clean paths.
            if target_rel == ".":
                target_rel = ""

            # Wildcard aliases get a trailing ``/`` for unambiguous prefix
            # matching in the resolver — UNLESS the target is the repo
            # root, in which case the empty string ``""`` is the correct
            # prefix (avoids producing ``"/foo.ts"`` lookups).
            if (
                pattern.endswith("/*")
                and target_rel
                and not target_rel.endswith("/")
            ):
                target_rel += "/"

            entries.append(AliasEntry(
                prefix=prefix,
                workspace_root=workspace_root,
                target_prefix=target_rel,
            ))

    return entries


# ── Repo-wide alias map build ────────────────────────────────────────────


def _walk_tsconfigs(repo_root: Path) -> list[Path]:
    """Enumerate every ``tsconfig*.json`` under ``repo_root``, skipping
    vendor / build dirs and bounded by :data:`_MAX_WALK_DEPTH`.
    """
    found: list[Path] = []
    repo_root = repo_root.resolve()

    def _walk(current: Path, depth: int) -> None:
        if depth > _MAX_WALK_DEPTH:
            return
        try:
            # SORTED for deterministic discovery order — iterdir() order
            # is filesystem-dependent and would leak into alias-entry
            # ordering (and thus resolution tie-breaks) between machines.
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name in _SKIP_DIR_NAMES or name.startswith("."):
                    # Allow dotted entries that aren't in the skip set?
                    # Keep simple: skip everything dotted (no real
                    # tsconfigs live in ``.something`` dirs in scope).
                    continue
                _walk(entry, depth + 1)
            elif entry.is_file():
                if name == "tsconfig.json" or name.startswith("tsconfig."):
                    if name.endswith(".json"):
                        found.append(entry)

    _walk(repo_root, depth=0)
    return found


def build_path_alias_map(
    repo_root: Path | str,
) -> list[AliasEntry]:
    """Build the repo-wide tsconfig path-alias map.

    Walks every ``tsconfig*.json`` under ``repo_root`` (skipping vendor
    dirs), follows each file's ``extends`` chain, merges
    ``compilerOptions.paths`` per workspace, and returns a flat list
    of :class:`AliasEntry` records sorted LONGEST-prefix-first so the
    resolver always picks the most specific match.

    Returns an empty list on any error or when no tsconfigs are found
    (non-TS repos).
    """
    repo_path = Path(repo_root)
    try:
        if not repo_path.is_dir():
            return []
    except OSError:
        return []

    tsconfigs = _walk_tsconfigs(repo_path)
    all_entries: list[AliasEntry] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for ts in tsconfigs:
        for entry in _alias_entries_for(ts, repo_path):
            key = (entry.prefix, entry.workspace_root, entry.target_prefix)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_entries.append(entry)

    # Longest-prefix-first so resolution honours the tsconfig
    # precedence rule (``@/utils/foo`` matches ``@/utils/`` before
    # ``@/``).
    all_entries.sort(key=lambda e: (-len(e.prefix), e.prefix))
    return all_entries


# ── Resolver ─────────────────────────────────────────────────────────────


def _try_extensions(base: str, tracked_files: AbstractSet[str]) -> str | None:
    """Try ``base`` with TS/JS extensions + index files; return the
    first match found in ``tracked_files``."""
    # Already has an extension that resolves directly.
    if base in tracked_files:
        return base

    # .js → .ts swap (NodeNext / bundler).
    for js_ext, ts_candidates in _JS_TO_TS_SWAPS.items():
        if base.endswith(js_ext):
            stem = base[: -len(js_ext)]
            for ts_ext in ts_candidates:
                candidate = stem + ts_ext
                if candidate in tracked_files:
                    return candidate

    # Bare import without extension.
    for ext in _EXTENSIONS_TO_TRY:
        candidate = base + ext
        if candidate in tracked_files:
            return candidate

    # Directory index import.
    for idx in _INDEX_FILES:
        candidate = f"{base}/{idx}"
        if candidate in tracked_files:
            return candidate

    return None


def _workspace_encloses(workspace_root: str, importer_file: str) -> bool:
    """True when ``importer_file`` lives inside ``workspace_root``.

    The repo-root workspace (``""``) encloses every file. Paths are
    repo-relative POSIX on both sides.
    """
    if not workspace_root:
        return True
    return importer_file.startswith(workspace_root + "/")


def resolve_alias_import(
    importer_file: str,
    import_spec: str,
    alias_map: list[AliasEntry],
    tracked_files: AbstractSet[str],
) -> str | None:
    """Resolve ``import_spec`` through tsconfig path aliases, honouring
    the NEAREST ENCLOSING workspace's mapping.

    TypeScript resolves a file's imports against exactly ONE tsconfig —
    the nearest one walking up from the file (its project). A monorepo
    routinely declares the SAME alias prefix per app with different
    targets (``~/* → ./src/*`` in ``apps/marketing`` AND ``apps/web``),
    so matching by prefix alone can cross app boundaries. This resolver:

      1. Considers only alias entries whose ``workspace_root`` encloses
         the importer (repo-root entries always apply). Entries from a
         sibling workspace are NEVER used — that would fabricate edges
         the compiler wouldn't make.
      2. Tries the nearest (deepest) enclosing workspace first, walking
         up toward the repo root when nothing resolves.
      3. Within one workspace, tries longest alias prefix first
         (tsconfig precedence), then each target in declared order.

    Returns ``None`` when no enclosing alias resolves to a tracked file.
    """
    if not import_spec:
        return None

    # Collect matching entries from enclosing workspaces only, keeping
    # the alias_map's stable order as the final tie-break (declared
    # target order within one pattern).
    candidates = [
        (idx, entry)
        for idx, entry in enumerate(alias_map)
        if import_spec.startswith(entry.prefix)
        and _workspace_encloses(entry.workspace_root, importer_file)
    ]
    if not candidates:
        return None
    # Nearest workspace first; longest prefix next; declared order last.
    candidates.sort(key=lambda pair: (
        -len(pair[1].workspace_root),
        -len(pair[1].prefix),
        pair[0],
    ))

    for _, entry in candidates:
        remainder = import_spec[len(entry.prefix):]
        base = entry.target_prefix + remainder
        # Normalise — target_prefix already ends with ``/`` for
        # wildcard aliases; for exact aliases (rare) the remainder
        # is empty and ``base == target_prefix``.
        resolved = _try_extensions(base, tracked_files)
        if resolved is not None:
            return resolved

    return None


def resolve_ts_import(
    importer_file: str,
    import_spec: str,
    *,
    alias_map: list[AliasEntry],
    tracked_files: AbstractSet[str],
) -> str | None:
    """Resolve a TS/JS import to a tracked file path.

    Tries (in order):

      1. Relative imports (``./foo`` / ``../bar``) against the
         importer's directory.
      2. Path aliases — nearest enclosing workspace wins, then longest
         matching prefix (see :func:`resolve_alias_import`).
      3. Bare imports are treated as external (``node_modules`` /
         stdlib) and return ``None``.

    Returns ``None`` when the import is external or the target file
    is not in ``tracked_files``.
    """
    if not import_spec:
        return None

    # 1. Relative
    if import_spec.startswith("./") or import_spec.startswith("../"):
        importer_dir = str(Path(importer_file).parent)
        # Normalise to forward slashes for tracked_files matching.
        import os as _os
        raw = _os.path.normpath(_os.path.join(importer_dir, import_spec))
        base = raw.replace("\\", "/").lstrip("/")
        if base.startswith(".."):
            return None
        return _try_extensions(base, tracked_files)

    # 2. Path aliases — nearest enclosing workspace, longest prefix.
    resolved = resolve_alias_import(
        importer_file, import_spec, alias_map, tracked_files,
    )
    if resolved is not None:
        return resolved

    # 3. Bare — external.
    return None


# ── Public API ───────────────────────────────────────────────────────────


__all__ = [
    "AliasEntry",
    "build_path_alias_map",
    "resolve_alias_import",
    "resolve_ts_import",
]
