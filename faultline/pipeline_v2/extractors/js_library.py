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
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

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


_YAML_PATH = (
    Path(__file__).resolve().parents[3]
    / "eval"
    / "stacks"
    / "js-library.yaml"
)


def _load_config() -> dict:
    text = read_text(_YAML_PATH)
    if not text:
        logger.debug("js-library.yaml not readable at %s", _YAML_PATH)
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        logger.warning("js-library.yaml parse failed: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


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
    if audited in _LIBRARY_STACK_TAGS:
        # Strong signal from Stack Auditor. Honour it but still
        # disqualify on direct app-framework deps to be safe.
        pkg = _read_package_json(ctx.repo_path)
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

    pkg = _read_package_json(ctx.repo_path)
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
            # Prefer source paths over bundled CJS.
            for cond in ("default", "import", "module", "node", "browser", "require", "types"):
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

        # 1) Discover source root.
        source_root_candidates = self._config.get("source_root_candidates") or [
            "lib", "src", "source",
        ]
        source_root = _pick_source_root(repo, source_root_candidates)

        # 2) Parse package.json#exports for explicit public-API subpaths.
        pkg = _read_package_json(repo)
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
            if target_norm not in tracked_set:
                continue
            if _excluded(target_norm):
                continue
            anchors.append(
                AnchorCandidate(
                    name=slug,
                    paths=(target_norm,),
                    source=self.name,
                    confidence_self=export_conf,
                    rationale=(
                        f"js-library package.json exports {subpath!r} → "
                        f"{target_norm!r}"
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
            sub_files: dict[str, str] = {}
            prefix = f"{source_root}/"
            for t in tracked:
                if not t.startswith(prefix):
                    continue
                if _excluded(t):
                    continue
                rest = t[len(prefix):]
                if "/" in rest:
                    top = rest.split("/", 1)[0]
                    sub_dirs.setdefault(top, []).append(t)
                else:
                    if re.search(r"\.(m?[jt]s|cts|cjs)$", rest):
                        name = re.sub(r"\.(m?[jt]s|cts|cjs)$", "", rest)
                        if name and not is_noise(name):
                            sub_files.setdefault(name, t)

            # Submodule anchors.
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

            # Top-level single-file modules — only when explicitly
            # re-exported from the entry file. Avoid spamming on
            # internal utility files.
            entry_candidates = self._config.get("entry_file_candidates") or [
                "index.js", "index.ts", "index.mjs", "src/index.ts", "src/index.js",
            ]
            entry_file = _find_entry_file(repo, entry_candidates)
            reexported = _parse_entry_reexports(read_text(entry_file) or "") if entry_file else set()
            for name, file_path in sorted(sub_files.items()):
                if name in reexported or name.lower() in {s.lower() for s in reexported}:
                    slug = slugify(name)
                    if not slug or is_noise(slug) or slug in emitted_slugs:
                        continue
                    anchors.append(
                        AnchorCandidate(
                            name=slug,
                            paths=(file_path,),
                            source=self.name,
                            confidence_self=sub_conf,
                            rationale=(
                                f"js-library single-file module {file_path!r} "
                                f"re-exported from entry"
                            ),
                        ),
                    )
                    emitted_slugs.add(slug)

        # 4) Symbol-only anchors from entry-file re-exports. Low
        # confidence; Stage 2 may merge into the right submodule.
        entry_candidates = self._config.get("entry_file_candidates") or [
            "index.js", "index.ts", "index.mjs", "src/index.ts", "src/index.js",
        ]
        entry_file = _find_entry_file(repo, entry_candidates)
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
