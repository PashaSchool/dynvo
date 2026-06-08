"""JsLibraryExtractor — package.json#exports + lib/ layout for JS/TS libs.

For JavaScript / TypeScript LIBRARY repos (no Next / Express / Nuxt
app entry), the feature map is encoded in:

  - ``package.json#exports`` — the maintainer's explicit public API
    surface map. Each exported subpath that points to a real source
    file is a feature anchor.
  - The top-level source directory (``lib/`` or ``src/``) — each
    subdirectory with files is a submodule feature anchor.
  - The entry file (``index.js`` / ``src/index.ts``) — named re-exports
    name additional public symbols.

This shape is distinct from a JS APP (Next / Express / Nuxt) whose
features come from URL routes — those are handled by
:class:`RouteFileExtractor`. The activation gate disqualifies any repo
whose ``package.json`` directly depends on an app framework so we
don't double-count.

Patterns live in ``eval/stacks/js-library.yaml``. The Python code
just loads + applies them. Strictly deterministic: no LLM, no network,
read-only.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Median-outlier flat-leaf collapse (scale-invariant, no magic counts) ────
#
# A library whose source root holds MANY flat one-file modules (yup's
# ``src/*.ts`` = 18 files) must not explode into one feature-per-file.
# A *directory* whose count of flat single-file leaves is an outlier
# relative to the median FILE-COUNT of the sibling folder-modules is
# collapsed to its folder anchor. Folder-modules and explicit
# package.json#exports are ALWAYS kept (real, author-declared
# boundaries); only flat-file leaves collapse, and only when there is a
# folder anchor to fold into.
#
# Both constants are scale-invariant comparators, not corpus-tuned
# absolutes (per memory/rule-no-magic-tuning):
#   * _FLAT_OUTLIER_MULT — a flat-leaf group is an outlier when its size
#     exceeds MULT × the median substantive-anchor file-count.
#   * _FLAT_OUTLIER_MEDIAN_FLOOR — below this many flat leaves in a dir
#     we never collapse (a handful of flat modules IS the feature map
#     for a small lib; collapsing would erase signal).
#
# NOTE: this copy is intentionally LOCAL to js_library.py. An equivalent
# guard lives in the Rust module extractor (rust_packages.py) which is on
# the still-unmerged PR #25 branch — importing it here would create a
# cross-PR dependency. TODO: DRY into a shared util once #25 merges.
_FLAT_OUTLIER_MULT = 2.0
_FLAT_OUTLIER_MEDIAN_FLOOR = 3


def _collapse_oversplit_flat(
    flat_by_dir: dict[str, dict[str, str]],
    folder_file_counts: list[int],
    foldable_dirs: set[str],
) -> set[str]:
    """Return the set of dir-keys whose flat-leaf set should collapse.

    Scale-invariant outlier detection. A directory's flat single-file
    modules "explode" relative to the repo's OWN sense of feature size,
    given by the median file-count of the sibling folder-modules. A dir
    whose flat-leaf count exceeds ``_FLAT_OUTLIER_MULT`` × that median
    (and clears ``_FLAT_OUTLIER_MEDIAN_FLOOR``) is an outlier.

    Critical: we only collapse a dir that has a FOLD TARGET — a real
    folder-module anchor (``foldable_dirs``). A noise-named module dir
    with no folder anchor (e.g. a public ``core/`` whose leaves ARE the
    feature surface) keeps its leaves rather than silently dropping them
    back to the LLM. With no folder-modules to reference we fall back to
    the floor alone for the genuine giant-flat-dir explosion.
    """
    if not flat_by_dir:
        return set()

    counts = [c for c in folder_file_counts if c > 0]
    median_folder = (
        statistics.median(counts) if counts else float(_FLAT_OUTLIER_MEDIAN_FLOOR)
    )
    threshold = max(
        _FLAT_OUTLIER_MEDIAN_FLOOR,
        _FLAT_OUTLIER_MULT * median_folder,
    )

    collapsed: set[str] = set()
    for dir_key, modules in flat_by_dir.items():
        if len(modules) <= threshold:
            continue
        if dir_key in foldable_dirs:
            collapsed.add(dir_key)
    return collapsed


def _load_config() -> dict:
    """Load js-library.yaml from the packaged data tree (hermetic)."""
    return load_stack_yaml("js-library")


# ── Activation gate ────────────────────────────────────────────────────────


_LIBRARY_STACK_TAGS = (
    "js-client-library",
    "js-library",
    "ts-library",
    "js-node-library",
)


_APP_FRAMEWORK_DEPS = (
    # Frontend frameworks
    "next",
    "nuxt",
    "@remix-run/react",
    "@sveltejs/kit",
    "astro",
    # Server frameworks — when these are DIRECT deps, the repo is a
    # server app, not a library. We do NOT disqualify on having an
    # adapter shim that mentions these (adapters/http.js).
    "express",
    "fastify",
    "@nestjs/core",
    "koa",
    "hapi",
)


def _read_package_json(repo_path: Path) -> dict:
    """Read root ``package.json`` if present; return ``{}`` on any error."""
    text = read_text(repo_path / "package.json")
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _effective_manifest(ctx: "ScanContext") -> tuple[dict, str]:
    """Pick the package.json that this extractor should reason about.

    Returns ``(pkg_dict, source_root_prefix)`` where ``source_root_prefix``
    is "" for repo-root mode and ``"<workspace_path>/"`` for per-workspace
    mode. Per-workspace dispatch (see stage_1_per_workspace) passes a
    single-workspace scoped ctx with the parsed manifest already on
    ``ctx.workspaces[0].package_json`` — using that lets a monorepo
    library (e.g. better-auth's packages/better-auth/) activate as a
    library even when the root package.json is private/empty.
    """
    if (
        ctx.monorepo
        and ctx.workspaces
        and len(ctx.workspaces) == 1
        and isinstance(ctx.workspaces[0].package_json, dict)
    ):
        ws = ctx.workspaces[0]
        ws_path = (ws.path or "").rstrip("/")
        prefix = f"{ws_path}/" if ws_path else ""
        return ws.package_json, prefix  # type: ignore[return-value]
    return _read_package_json(ctx.repo_path), ""


def _has_app_framework_dep(pkg: dict) -> bool:
    """``True`` when any direct dep / peerDep / devDep is an app framework.

    We check ``dependencies`` and ``peerDependencies`` (the strong
    signals). ``devDependencies`` is intentionally NOT consulted —
    a library will often dev-depend on Next to run sandbox examples
    or on Express to run its own tests.
    """
    for key in ("dependencies", "peerDependencies"):
        deps = pkg.get(key) or {}
        if not isinstance(deps, dict):
            continue
        for name in deps:
            if name in _APP_FRAMEWORK_DEPS:
                return True
    return False


def _is_js_library(ctx: "ScanContext") -> bool:
    """``True`` when the repo should be treated as a JS/TS library."""
    audited = (ctx.audited_stack or "").lower()
    pkg, _prefix = _effective_manifest(ctx)
    if audited in _LIBRARY_STACK_TAGS:
        # Strong signal from Stack Auditor. Honour it but still
        # disqualify on direct app-framework deps to be safe.
        if _has_app_framework_dep(pkg):
            return False
        return True

    # Heuristic fallback for repos the auditor didn't tag explicitly:
    # JS-shaped repo with package.json#main or #module or #exports AND
    # no app-framework dep.
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    is_jsish = (
        (ctx.stack or "").lower() in ("js", "js-generic", "ts", "node", "vite")
        or audited.startswith("js")
        or audited.startswith("ts")
        or any(s.startswith("js") or s.startswith("ts") for s in secondaries)
    )
    if not is_jsish:
        return False

    if not pkg:
        return False
    if _has_app_framework_dep(pkg):
        return False

    # Library-shape signal: at least one of main / module / exports / types.
    if not any(k in pkg for k in ("main", "module", "exports", "types")):
        return False
    return True


# ── Source root + package.json exports parsing ─────────────────────────────


def _pick_source_root(repo_path: Path, candidates: list[str]) -> str | None:
    """Return first candidate that exists with at least one file under it.

    Conservative: just checks the directory exists. The caller will
    confirm by walking ``ctx.tracked_files`` later.
    """
    for c in candidates:
        p = repo_path / c
        if p.is_dir():
            return c
    return None


def _resolve_exports_to_paths(
    exports_field: object,
) -> dict[str, str]:
    """Walk ``package.json#exports`` and yield ``{subpath: source_file}``.

    Handles three shapes:
      - ``"./x": "./lib/x.js"``                    (string value)
      - ``"./x": {"default": "./lib/x.js", ...}``  (conditional)
      - ``"./x": {"./y": "./lib/x/y.js", ...}``    (nested subpaths)

    Skips: dist/* targets, ./package.json, ./* glob wildcards (without
    a concrete pair). Returns the FIRST plausible source path per
    subpath (we prefer ``default``/``import`` over ``require`` so we
    point at source, not bundled CJS).
    """
    out: dict[str, str] = {}
    if not isinstance(exports_field, dict):
        # Top-level string export — rare but valid for tiny libraries.
        if isinstance(exports_field, str):
            out["."] = exports_field
        return out

    # Walk conditional / nested shapes.
    def _resolve_value(v: object) -> str | None:
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            # Prefer source paths over bundled CJS/ESM. ``dev-source`` and
            # ``source`` are emerging conventions (tsdown, tsup, custom)
            # that point at the actual TS/JS source file; without them
            # ``default`` points at ``dist/*`` which is gitignored and
            # therefore never tracked — collapsing all anchors silently.
            for cond in (
                "dev-source",
                "source",
                "default",
                "import",
                "module",
                "node",
                "browser",
                "require",
                "types",
            ):
                if cond in v:
                    nested = _resolve_value(v[cond])
                    if nested:
                        return nested
            # Fallback: first scalar found.
            for val in v.values():
                nested = _resolve_value(val)
                if nested:
                    return nested
        return None

    for subpath, value in exports_field.items():
        if not isinstance(subpath, str):
            continue
        if subpath in ("./package.json",):
            continue
        if subpath.endswith("/*") or "*" in subpath:
            # Glob exports re-expose the source dir — covered by source
            # root walk; skip here to avoid duplicate / glob entries.
            continue
        resolved = _resolve_value(value)
        if not resolved:
            continue
        # Normalise leading "./".
        norm = resolved.lstrip("./") if resolved.startswith("./") else resolved
        out[subpath] = norm

    return out


# ── Entry file re-export parsing ───────────────────────────────────────────


_REEXPORT_NAMED = re.compile(
    r"export\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]",
)
_REEXPORT_STAR = re.compile(
    r"export\s*\*\s*from\s*['\"]([^'\"]+)['\"]",
)
_NAMED_EXPORT_LIST = re.compile(
    r"export\s*\{([^}]+)\}\s*;",
)
_IMPORT_DEFAULT = re.compile(
    r"^import\s+(\w+)\s+from\s*['\"](\.[^'\"]+)['\"]",
    re.MULTILINE,
)


def _parse_entry_star_reexports(text: str) -> list[str]:
    """Return relative paths re-exported via ``export * from "./X"``.

    These are submodule signals — when an entry file does ``export *
    from "./plugins/admin"`` we treat ``plugins/admin`` (or its closest
    directory) as a public-API submodule of the library, regardless of
    whether the named symbols are listed individually.

    Returns the raw relative target string (without the leading ``./``).
    Caller normalises against the source root.
    """
    out: list[str] = []
    if not text:
        return out
    for m in _REEXPORT_STAR.finditer(text):
        target = (m.group(1) or "").strip()
        if not target:
            continue
        # Drop leading "./" and trailing extensions; keep the relative
        # subpath. We do NOT resolve "../" parents — those reach into
        # sibling source dirs which are handled by the source-root walk.
        if target.startswith("../"):
            continue
        cleaned = target.lstrip("./")
        cleaned = re.sub(r"\.(m?[jt]s|cts|cjs|d\.ts)$", "", cleaned)
        if cleaned:
            out.append(cleaned)
    return out


def _parse_entry_reexports(text: str) -> set[str]:
    """Return the set of public symbol names re-exported from an entry file."""
    symbols: set[str] = set()
    if not text:
        return symbols

    # Symbols we never want as feature anchors — these are JS keywords
    # used in re-export syntax, not real public API names.
    _SYMBOL_BLOCKLIST = frozenset({"default", "as", "from"})

    for m in _REEXPORT_NAMED.finditer(text):
        for raw in m.group(1).split(","):
            # `as` re-binds — prefer the rebound name when present.
            if " as " in raw:
                name = raw.split(" as ")[-1].strip()
            else:
                name = raw.strip()
            if name and re.match(r"^[A-Za-z_$][\w$]*$", name) and name not in _SYMBOL_BLOCKLIST:
                symbols.add(name)

    for m in _NAMED_EXPORT_LIST.finditer(text):
        for raw in m.group(1).split(","):
            if " as " in raw:
                name = raw.split(" as ")[-1].strip()
            else:
                name = raw.strip()
            if name and re.match(r"^[A-Za-z_$][\w$]*$", name) and name not in _SYMBOL_BLOCKLIST:
                symbols.add(name)

    return symbols


def _find_entry_file(repo_path: Path, candidates: list[str]) -> Path | None:
    for c in candidates:
        p = repo_path / c
        if p.is_file():
            return p
    return None


# ── Extractor ──────────────────────────────────────────────────────────────


class JsLibraryExtractor:
    """JS/TS package.json#exports + ``lib/`` layout → feature anchors."""

    name = "js-library"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else _load_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_js_library(ctx):
            return []

        repo = ctx.repo_path
        tracked = [posix(f) for f in ctx.tracked_files]
        tracked_set = set(tracked)

        excludes = tuple(
            e for e in (self._config.get("excludes") or [])
            if isinstance(e, str)
        )

        def _excluded(p: str) -> bool:
            return any(p.startswith(ex) or f"/{ex}" in f"/{p}" for ex in excludes)

        # Pick the effective manifest. ``ws_prefix`` is "" for repo-root
        # mode and "<workspace_path>/" for per-workspace dispatch — used
        # to scope source-root discovery + path joins so monorepo
        # libraries (better-auth / sst / others) actually fire.
        pkg, ws_prefix = _effective_manifest(ctx)

        # 1) Discover source root. In workspace mode look under
        # ``<workspace_path>/``; in repo-root mode look at the root.
        source_root_candidates = self._config.get("source_root_candidates") or [
            "lib", "src", "source",
        ]
        ws_base = repo / ws_prefix.rstrip("/") if ws_prefix else repo
        source_root = _pick_source_root(ws_base, source_root_candidates)

        # 2) Parse package.json#exports for explicit public-API subpaths.
        exports_map = _resolve_exports_to_paths(pkg.get("exports"))

        confidence = self._config.get("confidence") or {}
        sub_conf = float(confidence.get("submodule", 0.85))
        export_conf = float(confidence.get("package_exports_subpath", 0.85))
        sym_conf = float(confidence.get("symbol_only", 0.6))

        anchors: list[AnchorCandidate] = []
        emitted_slugs: set[str] = set()

        # 2a) Anchors from package.json#exports (high signal: maintainer
        # explicitly declared this surface).
        for subpath, target in sorted(exports_map.items()):
            # Slug from subpath (skip the bare "." root export — that's
            # the default symbol surface, covered by entry re-exports).
            if subpath == ".":
                continue
            slug_src = subpath.lstrip("./").replace("/", "-")
            # Strip trailing extensions.
            slug_src = re.sub(r"\.(m?[jt]s|cts|cjs|d\.ts)$", "", slug_src)
            slug = slugify(slug_src)
            if not slug or is_noise(slug) or slug in emitted_slugs:
                continue
            # Only emit if the target file is tracked + not excluded.
            target_norm = target.lstrip("./")
            # Workspace-scoped manifests reference paths relative to the
            # workspace root — re-anchor against the repo for tracked
            # lookup. We also keep the bare form as a fallback because
            # some monorepos symlink packages and tracked_files may carry
            # the un-prefixed path.
            candidate_paths: list[str] = []
            if ws_prefix:
                candidate_paths.append(f"{ws_prefix}{target_norm}")
            candidate_paths.append(target_norm)
            tracked_path = next(
                (p for p in candidate_paths if p in tracked_set), None,
            )
            if tracked_path is None:
                continue
            if _excluded(tracked_path):
                continue
            anchors.append(
                AnchorCandidate(
                    name=slug,
                    paths=(tracked_path,),
                    source=self.name,
                    confidence_self=export_conf,
                    rationale=(
                        f"js-library package.json exports {subpath!r} → "
                        f"{tracked_path!r}"
                    ),
                ),
            )
            emitted_slugs.add(slug)

        # 3) Walk source root for first-level submodules + single-file
        # modules. Each subdirectory of <source_root>/ becomes an anchor;
        # each <source_root>/<name>.{js,ts,mjs} top-level file becomes
        # an anchor.
        if source_root:
            sub_dirs: dict[str, list[str]] = {}
            # Flat module files keyed by the directory that holds them.
            # ``flat_by_dir[<dir>]`` maps module-name → tracked path. The
            # holding dir is the source root itself (root-level flat
            # modules) or a first-level subdir (nested flat modules like
            # ``source/core/options.ts``). Keying by dir lets the median-
            # collapse decide per-directory whether the flat-leaf set has
            # exploded.
            flat_by_dir: dict[str, dict[str, str]] = {}
            # Full path prefix in tracked-file terms. In workspace mode
            # tracked_files carry the workspace-relative paths
            # (e.g. ``packages/better-auth/src/...``); in root mode just
            # ``src/...``.
            prefix = f"{ws_prefix}{source_root}/"

            def _is_src_module_file(rest: str) -> str | None:
                if not re.search(r"\.(m?[jt]s|cts|cjs)$", rest):
                    return None
                base = rest.rsplit("/", 1)[-1]
                # ``*.d.ts`` declaration files are not feature modules.
                if base.endswith(".d.ts"):
                    return None
                name = re.sub(r"\.(m?[jt]s|cts|cjs)$", "", base)
                if not name or is_noise(name):
                    return None
                return name

            for t in tracked:
                if not t.startswith(prefix):
                    continue
                if _excluded(t):
                    continue
                rest = t[len(prefix):]
                depth = rest.count("/")
                if depth == 0:
                    # Root-level flat module file: <source_root>/<m>.ts
                    name = _is_src_module_file(rest)
                    if name:
                        flat_by_dir.setdefault(source_root, {}).setdefault(name, t)
                else:
                    top = rest.split("/", 1)[0]
                    sub_dirs.setdefault(top, []).append(t)
                    if depth == 1:
                        # First-level nested flat module:
                        # <source_root>/<sub>/<m>.ts
                        name = _is_src_module_file(rest)
                        if name:
                            dir_key = f"{source_root}/{top}"
                            flat_by_dir.setdefault(dir_key, {}).setdefault(name, t)

            # Folder-module anchors — ALWAYS kept (author-declared dir
            # boundary). These also become the collapse target for an
            # exploding flat-leaf set inside that dir.
            folder_slugs: dict[str, str] = {}  # dir_key -> slug
            for sub, files in sorted(sub_dirs.items()):
                slug = slugify(sub)
                if not slug or is_noise(slug) or slug in emitted_slugs:
                    continue
                anchors.append(
                    AnchorCandidate(
                        name=slug,
                        paths=tuple(sorted(files)),
                        source=self.name,
                        confidence_self=sub_conf,
                        rationale=(
                            f"js-library submodule {source_root}/{sub}/ "
                            f"({len(files)} files)"
                        ),
                    ),
                )
                emitted_slugs.add(slug)
                folder_slugs[f"{source_root}/{sub}"] = slug

            # Public-API reachability for NESTED dirs. A nested noise-named
            # module dir (e.g. ``source/core/``) is a public feature
            # surface only when the source-root barrel re-exports through
            # it (``export * from './core/...'`` or ``export {...} from
            # './core/x'``). An internal helper dir (e.g. ``src/util/``,
            # reached only by ``import`` statements) is NOT public — its
            # flat helpers must not become per-file phantom features
            # (memory/next-sprint-structural-folder-phantoms). Root-level
            # flat modules need no gate: they sit on the public source root
            # itself.
            #
            # KEY: the library's public entry is its source-root barrel
            # (``<source_root>/index.{ts,js,...}``). A narrow configured
            # entry-candidate list can miss it (e.g. configured
            # ``src/index.ts`` won't find ``source/index.ts``). We read the
            # source-root barrel DIRECTLY and union with any configured
            # entry file.
            entry_texts: list[str] = []
            for cand in ("index.ts", "index.js", "index.mjs", "index.cts", "index.cjs"):
                barrel_path = repo / f"{ws_prefix}{source_root}/{cand}"
                barrel_text = read_text(barrel_path)
                if barrel_text:
                    entry_texts.append(barrel_text)
                    break
            cfg_entry = _find_entry_file(
                ws_base,
                self._config.get("entry_file_candidates")
                or ["index.ts", "src/index.ts"],
            )
            if cfg_entry:
                cfg_text = read_text(cfg_entry)
                if cfg_text:
                    entry_texts.append(cfg_text)

            public_nested_dirs: set[str] = set()
            for entry_text in entry_texts:
                for tgt in _parse_entry_star_reexports(entry_text):
                    seg = tgt.strip("/").split("/")[0]
                    if seg:
                        public_nested_dirs.add(f"{source_root}/{seg}")
                for m in _REEXPORT_NAMED.finditer(entry_text):
                    rel = (m.group(2) or "").lstrip("./")
                    seg = rel.split("/")[0]
                    if seg and "/" in rel:
                        public_nested_dirs.add(f"{source_root}/{seg}")

            # Flat single-file modules — ROOT-level files emit
            # deterministically; NESTED dirs only when publicly reachable.
            # Guarded by the median-outlier collapse so a huge flat source
            # dir doesn't explode into one feature per file.
            folder_file_counts = [len(files) for files in sub_dirs.values()]
            foldable_dirs = set(folder_slugs.keys())
            collapsed_dirs = _collapse_oversplit_flat(
                flat_by_dir, folder_file_counts, foldable_dirs,
            )
            for dir_key, modules in sorted(flat_by_dir.items()):
                if dir_key != source_root and dir_key not in public_nested_dirs:
                    # Internal helper dir (util/, internal/) — not a public
                    # feature surface. Leave to Stage 2/4.
                    continue
                if dir_key in collapsed_dirs:
                    # Exploding flat set with a folder anchor to fold into —
                    # don't emit per-file leaves (the folder-module anchor
                    # already covers the dir). A root-level flat set with no
                    # fold target is NOT in collapsed_dirs (floor-only path
                    # in _collapse_oversplit_flat) and still emits below.
                    continue
                for name, file_path in sorted(modules.items()):
                    slug = slugify(name)
                    if not slug or is_noise(slug) or slug in emitted_slugs:
                        continue
                    anchors.append(
                        AnchorCandidate(
                            name=slug,
                            paths=(file_path,),
                            source=self.name,
                            confidence_self=sub_conf,
                            rationale=f"js-library source module {file_path!r}",
                        ),
                    )
                    emitted_slugs.add(slug)

        # 3a) ``export * from "./X"`` submodule anchors — including
        # plugin barrels like ``src/plugins/index.ts``. Library authors
        # use this pattern when a directory hosts many sibling
        # capabilities (auth providers, plugins) that the entry file
        # re-exposes wholesale. Each star-reexport target becomes its
        # own feature anchor when the target directory or file is
        # tracked. We harvest from ANY entry file under the workspace's
        # source root (not just the package root entry), so a nested
        # ``plugins/index.ts`` barrel contributes anchors too.
        if source_root:
            # Scan-tracked barrel files (any ``index.{ts,js,mjs}`` under
            # the source root) for star re-exports — these are the
            # documented sub-API surfaces.
            barrel_files: list[str] = []
            for t in tracked:
                if not t.startswith(prefix):
                    continue
                if _excluded(t):
                    continue
                if re.search(r"(?:^|/)index\.(?:m?[jt]s|cts|cjs)$", t):
                    barrel_files.append(t)

            for barrel_rel in barrel_files:
                barrel_text = read_text(repo / barrel_rel) or ""
                star_targets = _parse_entry_star_reexports(barrel_text)
                if not star_targets:
                    continue
                # Directory of the barrel file (relative to repo).
                barrel_dir = barrel_rel.rsplit("/", 1)[0] if "/" in barrel_rel else ""
                for target in star_targets:
                    # Resolve target relative to the barrel file's dir.
                    if barrel_dir:
                        resolved_dir = f"{barrel_dir}/{target}"
                    else:
                        resolved_dir = target
                    # Confirm the target exists as a tracked dir or file.
                    dir_prefix = f"{resolved_dir}/"
                    file_candidates = (
                        f"{resolved_dir}.ts",
                        f"{resolved_dir}.tsx",
                        f"{resolved_dir}.mjs",
                        f"{resolved_dir}.js",
                        f"{resolved_dir}/index.ts",
                        f"{resolved_dir}/index.tsx",
                        f"{resolved_dir}/index.mjs",
                        f"{resolved_dir}/index.js",
                    )
                    matched_paths: list[str] = []
                    if any(t.startswith(dir_prefix) for t in tracked):
                        matched_paths = [
                            t for t in tracked
                            if t.startswith(dir_prefix) and not _excluded(t)
                        ]
                    else:
                        for f in file_candidates:
                            if f in tracked_set and not _excluded(f):
                                matched_paths = [f]
                                break
                    if not matched_paths:
                        continue
                    # Use the LAST path component of the target as the
                    # feature name (``plugins/admin`` → ``admin``).
                    leaf = target.rstrip("/").split("/")[-1]
                    slug = slugify(leaf)
                    if not slug or is_noise(slug) or slug in emitted_slugs:
                        continue
                    anchors.append(
                        AnchorCandidate(
                            name=slug,
                            paths=tuple(sorted(matched_paths)),
                            source=self.name,
                            confidence_self=sub_conf,
                            rationale=(
                                f"js-library star re-export {target!r} "
                                f"from {barrel_rel!r}"
                            ),
                        ),
                    )
                    emitted_slugs.add(slug)

        # 4) Symbol-only anchors from entry-file re-exports. Low
        # confidence; Stage 2 may merge into the right submodule.
        entry_candidates = self._config.get("entry_file_candidates") or [
            "index.js", "index.ts", "index.mjs", "src/index.ts", "src/index.js",
        ]
        entry_file = _find_entry_file(ws_base, entry_candidates)
        if entry_file:
            entry_text = read_text(entry_file) or ""
            reexported = _parse_entry_reexports(entry_text)
            # Entry file path relative to repo (for rationale + anchor).
            try:
                entry_rel = posix(str(entry_file.relative_to(repo)))
            except ValueError:
                entry_rel = posix(str(entry_file))
            for sym in sorted(reexported):
                slug = slugify(sym)
                if not slug or is_noise(slug) or slug in emitted_slugs:
                    continue
                anchors.append(
                    AnchorCandidate(
                        name=slug,
                        paths=(entry_rel,),
                        source=self.name,
                        confidence_self=sym_conf,
                        rationale=f"js-library entry-file re-export {sym!r}",
                    ),
                )
                emitted_slugs.add(slug)

        return anchors


__all__ = ["JsLibraryExtractor"]
