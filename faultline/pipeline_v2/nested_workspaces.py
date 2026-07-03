"""Nested workspace-manifest discovery (StackProfile Phase B+).

Stage 0's :func:`faultline.analyzer.workspace.detect_workspace` reads
workspace manifests at the REPO ROOT only. A class of hybrid repos
(the polar / dispatch shape) keeps its frontend under a nested tree:

  * **polar** — ``pnpm-workspace.yaml`` lives at ``clients/``, not the
    root, so Stage 0 enumerates ZERO workspaces, the package extractor
    emits no workspace anchors, and the entire 190k-LOC Next frontend
    stays unattributed.
  * **dispatch** — a Vue SPA is colocated at
    ``src/dispatch/static/dispatch/`` (its own ``package.json`` +
    ``vite.config.js``), invisible to every root-level convention.

This module discovers those scopes deterministically, in two tiers:

  Tier A — nested workspace MANIFESTS (unambiguous)
    A ``pnpm-workspace.yaml`` or a ``package.json`` with a
    ``workspaces`` field found at directory depth 1..3 below the root.
    The manifest's own glob patterns are resolved rooted at ITS
    directory (reusing the analyzer's glob machinery — never
    re-implemented) and each resolved package path is re-prefixed to
    repo-root-relative form.

  Tier B — colocated framework app roots (structural markers)
    A directory at depth **>= 2** that hosts its own ``package.json``
    PLUS a framework/bundler config marker (``vite.config.*`` /
    ``next.config.*`` / ``nuxt.config.*`` / ``astro.config.*`` /
    ``svelte.config.*`` — or an ``index.html`` host page next to a
    ``src/`` tree). This is the "buried SPA" class: an app root nested
    INSIDE another project's source tree.

    The depth >= 2 floor is structural, not tuned: depth-1 app dirs
    (``frontend/``, ``webui/``, ``client/``) are the split-fullstack /
    synthetic-workspace territory that existing conventions already
    own (``stage_0_6_project_classifier`` split rescue,
    ``stage_1_per_workspace`` synthesis) — re-claiming them here would
    change every pinned repo with a top-level asset build (traefik
    ``webui/``, weblate ``client/``). Tier B exists precisely for the
    scopes NO top-level convention can reach.

Activation contract (G4 inertness): the Stage 0 hook calls this ONLY
when root-level ``detect_workspace`` found nothing. Repos with a root
workspace manifest (formbricks, dub, plane, dittofeed, openpanel) are
byte-for-byte untouched, as is every repo where discovery finds
nothing (traefik, saleor, weblate, litestar, fastapi-template).

Deterministic — NO LLM, NO network, sorted traversal, tracked-files
only (git already excludes node_modules etc.; a defensive noise-segment
skiplist is applied anyway).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath

from faultline.analyzer.workspace import (
    WorkspaceInfo,
    WorkspacePackage,
    _assign_files_to_packages,
    _package_name,
    _parse_yaml_list,
    _resolve_globs,
)

logger = logging.getLogger(__name__)

# ── structural constants (conventions, not tuned numbers) ───────────────

#: Maximum directory depth (path segments) at which a nested WORKSPACE
#: MANIFEST is honoured. Task-specified bound: a workspace manifest
#: deeper than 3 levels is not a product-scoping decision anyone makes
#: in practice, and the bound keeps the scan O(tracked_files).
_MAX_MANIFEST_DEPTH = 3

#: Tier B bounds: a colocated app root must be nested (depth >= 2 — see
#: module docstring for why depth-1 dirs are out of scope) and not
#: absurdly deep (<= 5 keeps ``src/<pkg>/static/<app>/`` shapes in
#: range without walking generated trees).
_MIN_APP_ROOT_DEPTH = 2
_MAX_APP_ROOT_DEPTH = 5

#: Framework / bundler config filename prefixes that mark a directory
#: as a real app root (ecosystem conventions, not corpus paths).
_APP_CONFIG_PREFIXES = (
    "vite.config.",
    "next.config.",
    "nuxt.config.",
    "astro.config.",
    "svelte.config.",
    "remix.config.",
    "react-router.config.",
)

#: Path segments that never host a product workspace (defensive — git
#: tracked files exclude most of these already; sample/doc trees are
#: excluded on principle: an example app is not a scan scope).
_NOISE_SEGMENTS = frozenset({
    "node_modules", "vendor", "dist", "build", "out", "target",
    ".git", ".next", ".turbo", "__pycache__",
    "docs", "doc", "examples", "example", "samples", "sample",
    "fixtures", "__fixtures__", "test", "tests", "e2e",
    "templates", "template",
})


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _dir_of(rel_file: str) -> str:
    p = _posix(rel_file)
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _depth(rel_dir: str) -> int:
    """Directory depth in segments (``""`` → 0, ``a/b`` → 2)."""
    return len(rel_dir.split("/")) if rel_dir else 0


def _has_noise_segment(rel_dir: str) -> bool:
    return any(seg.lower() in _NOISE_SEGMENTS for seg in rel_dir.split("/") if seg)


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ── Tier A — nested workspace manifests ─────────────────────────────────


def _workspace_patterns_from_package_json(path: Path) -> list[str]:
    doc = _read_json(path)
    if doc is None:
        return []
    workspaces = doc.get("workspaces")
    if isinstance(workspaces, dict):
        packages = workspaces.get("packages")
        return [p for p in packages if isinstance(p, str)] if isinstance(packages, list) else []
    if isinstance(workspaces, list):
        return [p for p in workspaces if isinstance(p, str)]
    return []


def _nested_manifest_packages(
    repo_root: Path,
    tracked_files: list[str],
) -> tuple[list[WorkspacePackage], list[str]]:
    """Tier A: packages declared by nested workspace manifests.

    Returns ``(packages, signals)``. Packages are repo-root-relative;
    manifests nested under an already-claimed workspace are skipped
    (the shallowest manifest wins — deterministic via sorted-by-depth
    traversal).
    """
    manifest_dirs: list[tuple[str, str]] = []  # (dir, kind)
    for f in tracked_files:
        p = _posix(f)
        base = p.rsplit("/", 1)[-1]
        d = _dir_of(p)
        depth = _depth(d)
        if depth < 1 or depth > _MAX_MANIFEST_DEPTH or _has_noise_segment(d):
            continue
        if base == "pnpm-workspace.yaml":
            manifest_dirs.append((d, "pnpm"))
        elif base == "package.json":
            manifest_dirs.append((d, "package.json"))

    packages: list[WorkspacePackage] = []
    signals: list[str] = []
    claimed: list[str] = []  # workspace path prefixes already resolved
    for d, kind in sorted(manifest_dirs, key=lambda t: (_depth(t[0]), t[0])):
        if any(d == c or d.startswith(c + "/") for c in claimed):
            continue  # nested under an already-discovered workspace
        base_dir = repo_root / d
        if kind == "pnpm":
            try:
                content = (base_dir / "pnpm-workspace.yaml").read_text(
                    encoding="utf-8",
                )
            except OSError:
                continue
            patterns = _parse_yaml_list(content, "packages")
        else:
            patterns = _workspace_patterns_from_package_json(
                base_dir / "package.json",
            )
        # pnpm negations ("!apps/api") are exclusions, not packages.
        patterns = [p for p in patterns if p and not p.startswith("!")]
        if not patterns:
            continue
        resolved = _resolve_globs(base_dir, patterns)
        if not resolved:
            continue
        for pkg in resolved:
            pkg_path = f"{d}/{pkg.path}"
            packages.append(WorkspacePackage(name=pkg.name, path=pkg_path))
            claimed.append(pkg_path)
        signals.append(
            f"nested workspace manifest at {d}/ "
            f"({kind}, {len(resolved)} packages)"
        )
    return packages, signals


# ── Tier B — colocated framework app roots ──────────────────────────────


def _colocated_app_roots(
    repo_root: Path,
    tracked_files: list[str],
    claimed: list[str],
) -> tuple[list[WorkspacePackage], list[str]]:
    """Tier B: nested app roots proven by their own manifest + config.

    ``claimed`` — path prefixes already owned by Tier A workspaces;
    a Tier B candidate under (or equal to) one is skipped.
    """
    tracked = [_posix(f) for f in tracked_files]
    tracked_set = frozenset(tracked)
    by_dir: dict[str, set[str]] = {}
    for p in tracked:
        by_dir.setdefault(_dir_of(p), set()).add(p.rsplit("/", 1)[-1])

    packages: list[WorkspacePackage] = []
    signals: list[str] = []
    accepted: list[str] = []
    for d in sorted(by_dir, key=lambda x: (_depth(x), x)):
        depth = _depth(d)
        if depth < _MIN_APP_ROOT_DEPTH or depth > _MAX_APP_ROOT_DEPTH:
            continue
        if _has_noise_segment(d):
            continue
        names = by_dir[d]
        if "package.json" not in names:
            continue
        if any(d == c or d.startswith(c + "/") for c in claimed):
            continue
        if any(d == a or d.startswith(a + "/") for a in accepted):
            continue  # nested under an already-accepted app root
        has_config = any(
            n.startswith(_APP_CONFIG_PREFIXES) for n in sorted(names)
        )
        has_spa_host = "index.html" in names and any(
            f.startswith(d + "/src/") for f in tracked_set
        )
        if not (has_config or has_spa_host):
            continue
        name = _package_name(repo_root / d)
        packages.append(WorkspacePackage(name=name, path=d))
        accepted.append(d)
        signals.append(f"colocated app root at {d}/")
    return packages, signals


# ── public entry point ───────────────────────────────────────────────────


def discover_nested_workspaces(
    repo_root: Path,
    tracked_files: list[str],
) -> tuple[WorkspaceInfo, list[str]]:
    """Discover nested workspace scopes for a repo with NO root manifest.

    Returns ``(info, signals)``. ``info.detected`` is False when
    nothing is found — the caller then proceeds exactly as before
    (single-package path, byte-for-byte).

    File assignment reuses the analyzer's
    :func:`_assign_files_to_packages` (deepest-prefix-wins, empty
    packages dropped) so ``Workspace.files`` semantics match the
    root-manifest path exactly.
    """
    tier_a, signals_a = _nested_manifest_packages(repo_root, tracked_files)
    claimed = [p.path for p in tier_a]
    tier_b, signals_b = _colocated_app_roots(repo_root, tracked_files, claimed)

    packages = sorted(tier_a + tier_b, key=lambda p: p.path)
    if not packages:
        return WorkspaceInfo(detected=False, manager="none"), []

    manager = "pnpm" if any("pnpm" in s for s in signals_a) else (
        "npm" if tier_a else "colocated"
    )
    info = WorkspaceInfo(detected=True, manager=manager, packages=packages)
    _assign_files_to_packages(info, [_posix(f) for f in tracked_files])
    return info, signals_a + signals_b


__all__ = ["discover_nested_workspaces"]
