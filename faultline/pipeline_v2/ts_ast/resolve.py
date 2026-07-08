"""W6-AST M3 — resolve raw TS/JS import edges to concrete repo files.

Input: the raw :class:`ImportEdge` stream produced by M2 (``imports.py``)
plus M2's per-file :class:`ExportEntry` index. Output: a deterministic,
sorted list of :class:`ResolvedEdge` — each raw specifier classified as
``relative`` / ``tsconfig_alias`` / ``workspace`` / ``package_external``
/ ``unresolved`` and, where possible, pinned to a repo-relative target
file. Transitive re-export (barrel) chains are followed through the
exports index so ``target_file`` points at the file that actually
*defines* the imported symbol, with the traversed barrels recorded in
``via_barrels``.

Resolution pipeline per edge (first hop):

  1. Relative specifiers (``./x``, ``../x``, ``.``, ``..``) against the
     importer's directory — extension try-list, ``.js``→``.ts`` swaps
     and ``index.*`` descent (mirrors the engine's canonical
     ``_try_extensions`` semantics).
  2. tsconfig ``paths`` aliases — reuses the engine's public
     :func:`faultline.analyzer.tsconfig_paths.build_path_alias_map` /
     :func:`~faultline.analyzer.tsconfig_paths.resolve_alias_import`
     (nearest enclosing workspace, longest prefix). An explicit
     ``baseUrl`` additionally contributes a synthetic empty-prefix
     alias entry so bare ``baseUrl``-anchored specifiers resolve too
     (compiled once per ``repo_root``).
  3. Workspace packages — ``pnpm-workspace.yaml`` / ``package.json``
     ``workspaces`` globs via the engine's public
     :func:`faultline.analyzer.import_graph.detect_workspace_package_map`,
     then the matched package's ``exports`` map (``"."``, ``"./sub"``,
     single-``*`` wildcards, condition objects), ``source``/``module``/
     ``main`` fields, and ``index`` / ``src/`` descent fallbacks.
  4. Anything else bare that looks like an npm/builtin specifier →
     ``package_external`` (``target_file=None``); the rest →
     ``unresolved``.

Barrel chase (second phase, ``named`` / ``default`` / ``reexport_named``
edges only): each imported name is walked through the exports index —
cycle-safe via a path-based visited set, hop-capped at
:data:`MAX_REEXPORT_DEPTH`. Renames (``export {x as y} from './a'``)
anchor on the declared origin when the name trail goes cold (the frozen
:class:`ExportEntry` shape carries no source name). Chains that hit an
external origin anchor on the last in-repo file. Names carrying the
M2 ``type:`` prefix are skipped for provenance (type-only edges drop
out entirely).

Filesystem policy: the *file list* is never taken from disk — every
target must be a member of the caller-supplied ``file_set``. Disk reads
are limited to tsconfig / package.json / pnpm-workspace.yaml config
files, compiled once and cached per ``repo_root``
(:func:`clear_resolver_caches` resets, e.g. between test fixtures).

Determinism laws: output list fully sorted, names/via tuples sorted or
chain-ordered, telemetry dict keys fixed and alphabetical, no set
iteration reaches the output.

NO LLM. Pure config parsing + graph walking.
"""

from __future__ import annotations

import json
import os
import posixpath
from collections.abc import Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from faultline.analyzer.import_graph import detect_workspace_package_map

#: Track-A (W6-AST provenance): opt the resolver's workspace-map build into
#: the recursive ``**`` glob expansion (STANDARD pnpm ``packages/**`` layout).
#: Default ON; ``FAULTLINE_WS_DEEP_GLOB=0`` restores the legacy ``**``-skip so
#: the resolver — and therefore every ts_ast provenance consumer — is
#: byte-identical to pre-Track-A. Only THIS caller opts in; flow_reach /
#: snapshots / symbol_graph keep the shallow map (contained blast radius).
_WS_DEEP_GLOB_ENV = "FAULTLINE_WS_DEEP_GLOB"
_WS_DEEP_FALSY = frozenset({"0", "false", "no", "off"})


def _ws_deep_glob_enabled() -> bool:
    return (os.environ.get(_WS_DEEP_GLOB_ENV, "1") or "1").strip().lower() \
        not in _WS_DEEP_FALSY
from faultline.analyzer.tsconfig_paths import (
    AliasEntry,
    build_path_alias_map,
    resolve_alias_import,
)
from faultline.pipeline_v2.ts_ast.shapes import (
    ExportEntry,
    ImportEdge,
    Resolution,
    ResolvedEdge,
)

# ── Spec §1 shapes ────────────────────────────────────────────────────────
# ImportEdge / ExportEntry / ResolvedEdge / Resolution are imported from
# shapes.py — M4's canonical single source for the frozen §1 shapes
# (coordinator ast-chain canonicalisation; semantics frozen, fields/order
# identical to the former local copies).


# ── Tunables (mirroring the engine's canonical resolver conventions) ─────

#: Names carrying this M2 prefix are type-only — skipped for provenance.
TYPE_NAME_PREFIX = "type:"

#: Maximum transitive re-export hops before a chain anchors where it is.
MAX_REEXPORT_DEPTH = 8

# Extension try-order for bare specifiers. Superset of the spec's
# minimum (.ts/.tsx/.js/.jsx/.mjs/.cjs) matching the engine resolver
# (analyzer/tsconfig_paths.py) so ts_ast and legacy paths agree.
_EXTENSIONS_TO_TRY: tuple[str, ...] = (
    ".ts", ".tsx", ".mts", ".cts",
    ".js", ".jsx", ".mjs", ".cjs",
)

_INDEX_FILES: tuple[str, ...] = tuple(f"index{e}" for e in _EXTENSIONS_TO_TRY)

# Import says `./foo.js` but the file on disk is `foo.ts` (NodeNext /
# bundler ESM convention).
_JS_TO_TS_SWAPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".js", (".ts", ".tsx")),
    (".jsx", (".tsx", ".ts")),
    (".mjs", (".mts", ".ts")),
    (".cjs", (".cts", ".ts")),
)

# package.json "exports" condition names tried first, in this order;
# any remaining (custom) condition keys follow in sorted order.
_CONDITION_PRIORITY: tuple[str, ...] = (
    "source", "import", "module", "default", "require", "node", "browser",
    "types",
)

# package.json entry fields for a bare workspace-package import when the
# exports map yields nothing. "source" (microbundle et al.) points at
# the src entry — best for provenance — before dist-leaning fields.
_PKG_ENTRY_FIELDS: tuple[str, ...] = ("source", "module", "main")

# Directories skipped while walking for tsconfig files (baseUrl pass).
_TSCONFIG_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".next", ".turbo", "dist", "build", "out",
    ".venv", "venv", "__pycache__", "target", ".git",
})
_MAX_TSCONFIG_WALK_DEPTH = 6

# Edge kinds whose names are module symbols we chase through barrels.
# namespace/side_effect/reexport_star bind whole modules; dynamic/require
# names have ambiguous semantics (destructure vs module binding) so they
# anchor on the directly resolved file.
_CHASE_KINDS: frozenset[str] = frozenset({"named", "default", "reexport_named"})

#: Fixed telemetry counter keys (alphabetical — dict is built in this
#: order so serialisation is canonical). All values are ints, always
#: present, zero-initialised.
TELEMETRY_KEYS: tuple[str, ...] = (
    "edges_in",
    "edges_out",
    "edges_type_only_skipped",
    "names_type_skipped",
    "reexport_cycle_hits",
    "reexport_depth_cap_hits",
    "reexport_external_stops",
    "reexport_hops",
    "reexport_name_misses",
    "resolution_package_external",
    "resolution_relative",
    "resolution_tsconfig_alias",
    "resolution_unresolved",
    "resolution_workspace",
    "tsconfig_candidate_misses",
    "workspace_file_misses",
)


# ── File-existence probing (file_set only — never the filesystem) ────────


def _try_extensions(base: str, file_set: AbstractSet[str]) -> str | None:
    """Resolve ``base`` against ``file_set``: exact → js→ts swap →
    extension append → ``index.*`` descent. Mirrors the engine resolver.
    """
    if not base or base in (".", ".."):
        # Root-index import (`import "."` from a root file) — descend.
        for idx in _INDEX_FILES:
            if idx in file_set:
                return idx
        return None
    if base in file_set:
        return base
    for js_ext, ts_candidates in _JS_TO_TS_SWAPS:
        if base.endswith(js_ext):
            stem = base[: -len(js_ext)]
            for ts_ext in ts_candidates:
                candidate = stem + ts_ext
                if candidate in file_set:
                    return candidate
    for ext in _EXTENSIONS_TO_TRY:
        candidate = base + ext
        if candidate in file_set:
            return candidate
    for idx in _INDEX_FILES:
        candidate = f"{base}/{idx}"
        if candidate in file_set:
            return candidate
    return None


def _norm_join(base_dir: str, rel: str) -> str | None:
    """Join ``rel`` under ``base_dir`` (both repo-relative POSIX) and
    normalise. Returns ``None`` when the result escapes the repo root
    or ``rel`` is absolute."""
    if rel.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, rel)) if rel else base_dir
    if joined.startswith("../") or joined == "..":
        return None
    return joined


# ── Light JSONC (tsconfig baseUrl pass) ──────────────────────────────────


def _strip_jsonc(src: str) -> str:
    """Remove ``//`` and ``/* */`` comments outside string literals."""
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
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i < n - 1 and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_jsonc_light(text: str) -> dict[str, object] | None:
    """Tolerant tsconfig parse: comment strip + trailing-comma removal +
    stdlib ``json``. Used only for the baseUrl extraction pass (the
    ``paths`` pass reuses the engine's full json5-backed parser via
    :func:`build_path_alias_map`)."""
    import re as _re

    stripped = _re.sub(r",\s*([}\]])", r"\1", _strip_jsonc(text))
    try:
        data: object = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): v for k, v in data.items()}


# ── tsconfig baseUrl → synthetic alias entries ───────────────────────────


def _iter_tsconfig_files(repo_root: Path) -> list[Path]:
    """Bounded, sorted walk for ``tsconfig*.json`` files."""
    found: list[Path] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > _MAX_TSCONFIG_WALK_DEPTH:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                if name in _TSCONFIG_SKIP_DIRS or name.startswith("."):
                    continue
                _walk(entry, depth + 1)
            elif entry.is_file() and name.endswith(".json") and (
                name == "tsconfig.json" or name.startswith("tsconfig.")
            ):
                found.append(entry)

    _walk(repo_root, 0)
    return found


def _base_url_alias_entries(repo_root: Path) -> list[AliasEntry]:
    """Synthesise one empty-prefix :class:`AliasEntry` per tsconfig that
    explicitly declares ``compilerOptions.baseUrl`` — TypeScript resolves
    ANY bare specifier against ``baseUrl``, so the empty prefix matches
    everything and (being shortest) sorts after every real ``paths``
    alias inside :func:`resolve_alias_import`.

    ``extends``-inherited baseUrl is intentionally not followed here
    (rare; the ``paths`` pass — which dominates in practice — does
    follow extends chains via the engine's compiler-options merge).
    """
    entries: list[AliasEntry] = []
    seen: set[tuple[str, str]] = set()
    for ts_path in _iter_tsconfig_files(repo_root):
        try:
            text = ts_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        config = _parse_jsonc_light(text)
        if config is None:
            continue
        options = config.get("compilerOptions")
        if not isinstance(options, dict):
            continue
        base_url = options.get("baseUrl")
        if not isinstance(base_url, str) or not base_url:
            continue
        try:
            ws_root = ts_path.parent.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if ws_root == ".":
            ws_root = ""
        target = _norm_join(ws_root, base_url.rstrip("/"))
        if target is None:
            continue
        if target == ".":
            target = ""
        target_prefix = f"{target}/" if target else ""
        key = (ws_root, target_prefix)
        if key in seen:
            continue
        seen.add(key)
        entries.append(AliasEntry(
            prefix="", workspace_root=ws_root, target_prefix=target_prefix,
        ))
    entries.sort(key=lambda e: (e.workspace_root, e.target_prefix))
    return entries


# ── Per-repo compiled context (cached on repo_root) ──────────────────────


@dataclass
class _RepoContext:
    """Config compiled once per ``repo_root``: tsconfig alias entries
    (paths + synthetic baseUrl), the workspace package-name → dir map,
    and a package.json parse cache."""

    repo_root: str
    alias_entries: list[AliasEntry]
    workspace_packages: dict[str, str]
    _pkg_json_cache: dict[str, dict[str, object] | None]

    def package_json(self, pkg_dir: str) -> dict[str, object] | None:
        if pkg_dir in self._pkg_json_cache:
            return self._pkg_json_cache[pkg_dir]
        parsed: dict[str, object] | None = None
        pj = Path(self.repo_root) / pkg_dir / "package.json"
        try:
            data: object = json.loads(pj.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                parsed = {str(k): v for k, v in data.items()}
        except (OSError, ValueError):
            parsed = None
        self._pkg_json_cache[pkg_dir] = parsed
        return parsed


_CTX_CACHE: dict[str, _RepoContext] = {}


def _context_for(repo_root: str) -> _RepoContext:
    ctx = _CTX_CACHE.get(repo_root)
    if ctx is not None:
        return ctx
    root_path = Path(repo_root)
    alias_entries = list(build_path_alias_map(root_path))
    alias_entries.extend(_base_url_alias_entries(root_path))
    ctx = _RepoContext(
        repo_root=repo_root,
        alias_entries=alias_entries,
        workspace_packages=detect_workspace_package_map(
            repo_root, deep=_ws_deep_glob_enabled()),
        _pkg_json_cache={},
    )
    _CTX_CACHE[repo_root] = ctx
    return ctx


def clear_resolver_caches() -> None:
    """Drop all per-repo compiled config (tests / long-lived processes)."""
    _CTX_CACHE.clear()


# ── package.json "exports" map (simplified per spec) ─────────────────────


def _collect_leaves(value: object, out: list[str]) -> None:
    """Flatten an exports value (string / array / condition object) into
    candidate relative paths, condition-priority first, then remaining
    condition keys in sorted order. ``null`` blocks are skipped."""
    if isinstance(value, str):
        if value:
            out.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_leaves(item, out)
        return
    if isinstance(value, dict):
        str_keys = [k for k in value.keys() if isinstance(k, str)]
        ordered = [c for c in _CONDITION_PRIORITY if c in value]
        ordered.extend(
            k for k in sorted(str_keys)
            if k not in ordered and not k.startswith(".")
        )
        for cond in ordered:
            _collect_leaves(value[cond], out)


def _exports_candidates(exports_value: object, subpath: str) -> list[str]:
    """Candidate package-relative paths for ``subpath`` (``""`` = bare)
    from a package.json ``exports`` value. Handles the spec-simplified
    surface: ``"."``, ``"./sub"`` exact keys, single-``*`` wildcard
    keys, condition objects, arrays."""
    key = "." if not subpath else f"./{subpath}"
    raw: list[str] = []
    if isinstance(exports_value, (str, list)):
        if key == ".":
            _collect_leaves(exports_value, raw)
    elif isinstance(exports_value, dict):
        str_keys = [k for k in exports_value.keys() if isinstance(k, str)]
        if any(k.startswith(".") for k in str_keys):
            if key in exports_value:
                _collect_leaves(exports_value[key], raw)
            else:
                # Wildcard subpath patterns — most-specific match wins.
                best: tuple[int, int, str] | None = None
                for k in str_keys:
                    if k.count("*") != 1:
                        continue
                    pre, post = k.split("*", 1)
                    if (
                        key.startswith(pre)
                        and key.endswith(post)
                        and len(key) >= len(pre) + len(post)
                    ):
                        rank = (-len(pre), -len(post), k)
                        if best is None or rank < best:
                            best = rank
                if best is not None:
                    pattern = best[2]
                    pre, post = pattern.split("*", 1)
                    mid = key[len(pre): len(key) - len(post)] if post else key[len(pre):]
                    leaves: list[str] = []
                    _collect_leaves(exports_value[pattern], leaves)
                    raw.extend(leaf.replace("*", mid) for leaf in leaves)
        elif key == ".":
            # Top-level condition object applies to the bare entry.
            _collect_leaves(exports_value, raw)
    # De-dup preserving priority order.
    seen: set[str] = set()
    out: list[str] = []
    for cand in raw:
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


# ── Workspace-package resolution ─────────────────────────────────────────


def _resolve_workspace(
    ctx: _RepoContext,
    spec: str,
    file_set: AbstractSet[str],
) -> tuple[bool, str | None]:
    """Resolve ``spec`` through the workspace package map.

    Returns ``(matched, target)`` — ``matched`` is True when a workspace
    package NAME matched (even if no file was found, so the caller can
    report ``workspace_file_misses`` instead of misclassifying the
    specifier as external)."""
    if not ctx.workspace_packages:
        return False, None
    # Longest name first; lexicographic tie-break for determinism.
    for name in sorted(ctx.workspace_packages, key=lambda n: (-len(n), n)):
        if spec == name:
            subpath = ""
        elif spec.startswith(name + "/"):
            subpath = spec[len(name) + 1:]
        else:
            continue
        pkg_dir = ctx.workspace_packages[name]
        bases: list[str] = []

        pkg = ctx.package_json(pkg_dir)
        if pkg is not None and "exports" in pkg:
            for cand in _exports_candidates(pkg["exports"], subpath):
                base = _norm_join(pkg_dir, cand[2:] if cand.startswith("./") else cand)
                if base is not None:
                    bases.append(base)
        if subpath:
            direct = _norm_join(pkg_dir, subpath)
            if direct is not None:
                bases.append(direct)
                # Packages routinely expose subpaths through their src/
                # root (engine-canonical fallback).
                bases.append(f"{pkg_dir}/src/{subpath}")
        else:
            if pkg is not None:
                for field in _PKG_ENTRY_FIELDS:
                    value = pkg.get(field)
                    if isinstance(value, str) and value:
                        base = _norm_join(
                            pkg_dir, value[2:] if value.startswith("./") else value,
                        )
                        if base is not None:
                            bases.append(base)
            bases.append(f"{pkg_dir}/index")
            bases.append(f"{pkg_dir}/src/index")

        for base in bases:
            resolved = _try_extensions(base, file_set)
            if resolved is not None:
                return True, resolved
        return True, None
    return False, None


# ── Specifier classification (first hop) ─────────────────────────────────


def _looks_like_package(spec: str) -> bool:
    """Plausible npm package / node builtin specifier."""
    if spec.startswith("node:"):
        return True
    if spec.startswith((".", "/", "#", "~")):
        return False
    parts = spec.split("/")
    head = parts[0]
    head_names: tuple[str, ...]
    if head.startswith("@"):
        if len(parts) < 2 or len(head) < 2 or not parts[1]:
            return False
        head_names = (head[1:], parts[1])
    else:
        head_names = (head,)
    for token in head_names:
        if not token or not all(
            c.isalnum() or c in "._-" for c in token
        ):
            return False
    return True


def _workspace_encloses(workspace_root: str, importer_file: str) -> bool:
    if not workspace_root:
        return True
    return importer_file.startswith(workspace_root + "/")


def _resolve_specifier(
    ctx: _RepoContext,
    src_file: str,
    spec: str,
    file_set: AbstractSet[str],
    telemetry: dict[str, int],
) -> tuple[str | None, Resolution]:
    """First-hop resolution of ``spec`` imported from ``src_file``."""
    if not spec:
        return None, "unresolved"

    # 1. Relative.
    if spec in (".", "..") or spec.startswith(("./", "../")):
        base = _norm_join(posixpath.dirname(src_file), spec)
        if base is None:
            return None, "unresolved"
        resolved = _try_extensions("" if base == "." else base, file_set)
        if resolved is not None:
            return resolved, "relative"
        return None, "unresolved"

    # 1b. Root-absolute (Vite-style `/src/x` project-root imports) —
    # honoured only when the file actually exists in the repo set.
    if spec.startswith("/"):
        base = posixpath.normpath(spec.lstrip("/"))
        if base and not base.startswith(".."):
            resolved = _try_extensions(base, file_set)
            if resolved is not None:
                return resolved, "relative"
        return None, "unresolved"

    # 2. tsconfig paths + baseUrl (nearest workspace, longest prefix).
    if ctx.alias_entries:
        resolved = resolve_alias_import(
            src_file, spec, ctx.alias_entries, file_set,
        )
        if resolved is not None:
            return resolved, "tsconfig_alias"
        # Diagnostic: a real (non-baseUrl) alias pattern matched the
        # specifier but no file existed behind any of its targets.
        if any(
            entry.prefix
            and spec.startswith(entry.prefix)
            and _workspace_encloses(entry.workspace_root, src_file)
            for entry in ctx.alias_entries
        ):
            telemetry["tsconfig_candidate_misses"] += 1

    # 3. Workspace packages.
    matched, resolved = _resolve_workspace(ctx, spec, file_set)
    if matched:
        if resolved is not None:
            return resolved, "workspace"
        telemetry["workspace_file_misses"] += 1
        return None, "unresolved"

    # 4. External package vs noise.
    if _looks_like_package(spec):
        return None, "package_external"
    return None, "unresolved"


# ── Transitive re-export (barrel) chase ──────────────────────────────────


def _resolve_origin(
    ctx: _RepoContext,
    barrel_file: str,
    origin: str,
    file_set: AbstractSet[str],
    telemetry: dict[str, int],
) -> tuple[str | None, Resolution]:
    """Resolve an :class:`ExportEntry` ``origin_file`` reference, which
    M2 may provide either as an already repo-relative path or as the raw
    specifier from the ``export ... from`` clause."""
    if origin in file_set:
        return origin, "relative"
    return _resolve_specifier(ctx, barrel_file, origin, file_set, telemetry)


def _entry_matches(entry: ExportEntry, wanted: str) -> bool:
    if wanted == "default":
        return entry.kind == "default" or (
            entry.kind == "named" and entry.name == "default"
        )
    return entry.kind == "named" and entry.name == wanted


def _lookup(
    ctx: _RepoContext,
    exports_index: Mapping[str, Sequence[ExportEntry]],
    file: str,
    wanted: str,
    depth: int,
    visited: frozenset[str],
    file_set: AbstractSet[str],
    telemetry: dict[str, int],
) -> tuple[str, tuple[str, ...]] | None:
    """Find where ``wanted`` (an exported name, or ``"default"``) is
    ultimately defined, starting at ``file``.

    Returns ``(origin_file, via_barrels)`` or ``None`` when the name is
    not provably exported from ``file`` (caller anchors on ``file``).
    Cycle-safe (path-based visited set) and hop-capped."""
    if file in visited:
        telemetry["reexport_cycle_hits"] += 1
        return None
    if depth >= MAX_REEXPORT_DEPTH:
        telemetry["reexport_depth_cap_hits"] += 1
        return file, ()
    visited = visited | {file}

    entries = exports_index.get(file) or ()
    named = sorted(
        (e for e in entries if _entry_matches(e, wanted)),
        key=lambda e: (e.origin_file or "", e.kind, e.name),
    )
    if named:
        entry = named[0]
        if not entry.origin_file:
            return file, ()
        origin, label = _resolve_origin(
            ctx, file, entry.origin_file, file_set, telemetry,
        )
        if origin is None:
            if label == "package_external":
                # `export {z} from 'zod'` — provenance leaves the repo;
                # anchor on the last in-repo surface.
                telemetry["reexport_external_stops"] += 1
            return file, ()
        sub = _lookup(
            ctx, exports_index, origin, wanted,
            depth + 1, visited, file_set, telemetry,
        )
        if sub is None:
            # Name trail went cold in the origin (rename hop such as
            # `export {x as y} from './a'`, or unindexed file) — the
            # re-export declaration itself pins provenance to `origin`.
            return origin, (file,)
        final, via = sub
        return final, (file, *via)

    stars = sorted(
        (e for e in entries if e.kind == "star_from" and e.origin_file),
        key=lambda e: (e.origin_file or "", e.name),
    )
    for entry in stars:
        origin, _label = _resolve_origin(
            ctx, file, entry.origin_file or "", file_set, telemetry,
        )
        if origin is None:
            continue
        sub = _lookup(
            ctx, exports_index, origin, wanted,
            depth + 1, visited, file_set, telemetry,
        )
        if sub is not None:
            final, via = sub
            return final, (file, *via)
    return None


# ── Public API ───────────────────────────────────────────────────────────


def resolve_edges(
    edges: Sequence[ImportEdge],
    exports_index: Mapping[str, Sequence[ExportEntry]],
    repo_root: str,
    file_set: frozenset[str],
) -> tuple[list[ResolvedEdge], dict[str, int]]:
    """Resolve raw M2 edges to repo files (module entry point).

    Args:
        edges: raw import/export edges from M2 (any order).
        exports_index: M2's per-file export entries (barrel chase input).
        repo_root: absolute repo path — used ONLY to read tsconfig /
            package.json / pnpm-workspace.yaml config (cached per root).
        file_set: repo-relative POSIX paths of all candidate files;
            every ``target_file`` is guaranteed to be a member.

    Returns:
        ``(resolved, telemetry)`` — ``resolved`` fully sorted and
        de-duplicated (identical logical edges from repeated import
        statements collapse; per-name barrel origins split into one
        edge per distinct ``(target_file, via_barrels)``);
        ``telemetry`` has the fixed :data:`TELEMETRY_KEYS` counters.
    """
    ctx = _context_for(repo_root)
    telemetry: dict[str, int] = {key: 0 for key in TELEMETRY_KEYS}
    telemetry["edges_in"] = len(edges)

    # (src, raw, kind, resolution, target, via) → union of names.
    merged: dict[
        tuple[str, str, str, Resolution, str | None, tuple[str, ...]],
        set[str],
    ] = {}

    def _accumulate(
        edge: ImportEdge,
        target: str | None,
        resolution: Resolution,
        via: tuple[str, ...],
        names: tuple[str, ...],
    ) -> None:
        key = (edge.src_file, edge.raw_target, edge.kind, resolution, target, via)
        merged.setdefault(key, set()).update(names)

    for edge in edges:
        clean_names = tuple(
            n for n in edge.names if not n.startswith(TYPE_NAME_PREFIX)
        )
        telemetry["names_type_skipped"] += len(edge.names) - len(clean_names)
        if edge.names and not clean_names:
            telemetry["edges_type_only_skipped"] += 1
            continue

        target, resolution = _resolve_specifier(
            ctx, edge.src_file, edge.raw_target, file_set, telemetry,
        )
        if target is None or edge.kind not in _CHASE_KINDS or not clean_names:
            _accumulate(edge, target, resolution, (), clean_names)
            continue

        # Barrel chase — group names by their true origin.
        groups: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        for name in sorted(set(clean_names)):
            wanted = "default" if edge.kind == "default" else name
            found = _lookup(
                ctx, exports_index, target, wanted,
                0, frozenset(), file_set, telemetry,
            )
            if found is None:
                if exports_index.get(target):
                    telemetry["reexport_name_misses"] += 1
                found = (target, ())
            groups.setdefault(found, set()).add(name)
        for (final, via), group_names in groups.items():
            _accumulate(edge, final, resolution, via, tuple(group_names))

    resolved_edges = [
        ResolvedEdge(
            src_file=src,
            raw_target=raw,
            target_file=target,
            resolution=resolution,
            via_barrels=via,
            names=tuple(sorted(names)),
            kind=kind,
        )
        for (src, raw, kind, resolution, target, via), names in merged.items()
    ]
    resolved_edges.sort(key=lambda e: (
        e.src_file, e.raw_target, e.kind, e.resolution,
        e.target_file or "", e.via_barrels, e.names,
    ))

    telemetry["edges_out"] = len(resolved_edges)
    for edge_out in resolved_edges:
        telemetry[f"resolution_{edge_out.resolution}"] += 1
        telemetry["reexport_hops"] += len(edge_out.via_barrels)
    return resolved_edges, telemetry


__all__ = [
    "ExportEntry",
    "ImportEdge",
    "MAX_REEXPORT_DEPTH",
    "ResolvedEdge",
    "TELEMETRY_KEYS",
    "TYPE_NAME_PREFIX",
    "clear_resolver_caches",
    "resolve_edges",
]
