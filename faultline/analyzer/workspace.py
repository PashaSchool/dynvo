"""Workspace / monorepo package detection.

Detects package manager workspace configurations and enumerates
sub-packages so each can be analyzed independently for better
feature detection accuracy on large monorepos.

Supported:
  - pnpm (pnpm-workspace.yaml)
  - npm/yarn (package.json workspaces)
  - Turborepo (turbo.json)
  - Nx (nx.json + project.json)
  - Lerna (lerna.json)
  - Cargo (Cargo.toml [workspace])
  - Go (go.work)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path


@dataclass
class WorkspacePackage:
    """A single package/app within a monorepo."""

    name: str
    path: str  # relative to repo root, e.g. "packages/auth"
    files: list[str] = field(default_factory=list)


@dataclass
class WorkspaceInfo:
    """Result of workspace detection."""

    detected: bool
    manager: str  # "pnpm" | "npm" | "yarn" | "turbo" | "nx" | "lerna" | "cargo" | "go" | "none"
    packages: list[WorkspacePackage] = field(default_factory=list)
    root_files: list[str] = field(default_factory=list)  # files not in any package


def detect_workspace(repo_root: str, files: list[str]) -> WorkspaceInfo:
    """Detect workspace configuration and enumerate packages.

    Args:
        repo_root: Absolute path to the repository root.
        files: List of file paths relative to analysis root.

    Returns:
        WorkspaceInfo with detected packages and their files.
    """
    root = Path(repo_root)

    # Try each workspace type in priority order
    for detector in [
        _detect_pnpm,
        _detect_npm_yarn,
        _detect_turbo,
        _detect_nx,
        _detect_lerna,
        _detect_cargo,
        _detect_go,
    ]:
        info = detector(root, files)
        if info and info.detected:
            # Assign files to packages
            _assign_files_to_packages(info, files)
            return info

    return WorkspaceInfo(detected=False, manager="none")


def _assign_files_to_packages(info: WorkspaceInfo, files: list[str]) -> None:
    """Assign each file to its package, or to root_files if unmatched."""
    # Sort packages by path length descending so deeper paths match first
    sorted_pkgs = sorted(info.packages, key=lambda p: len(p.path), reverse=True)

    for f in files:
        matched = False
        for pkg in sorted_pkgs:
            prefix = pkg.path + "/"
            if f.startswith(prefix) or f == pkg.path:
                pkg.files.append(f)
                matched = True
                break
        if not matched:
            info.root_files.append(f)

    # Remove empty packages
    info.packages = [p for p in info.packages if p.files]


# Directory names that are never workspace candidates. Walked at every
# nesting level when expanding ``**`` globs so a runaway descent into
# ``node_modules/`` or ``target/`` can't fabricate hundreds of phantom
# workspaces. Mirrors the exclusion list used by Stage 0's filesystem
# walker (``_walk_tracked_files``) and the auditor.
_GLOB_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "vendor", "target", "dist", "build", "out",
    ".next", ".turbo", "__pycache__", ".venv", "venv", ".pytest_cache",
    ".mypy_cache",
})

# Cap recursive glob descent. 6 directory segments below the literal
# prefix is enough for `packages/db/src/schema/<x>/<y>` and similar
# real-world layouts without risking a runaway walk on weird repos.
_GLOB_MAX_DEPTH: int = 6

# Manifests that prove a directory is a real package (and therefore a
# legitimate workspace). When ``**`` expands we only emit dirs that
# contain one of these — bare source dirs don't become workspaces.
_WORKSPACE_MANIFEST_FILENAMES: tuple[str, ...] = (
    "package.json",
    "Cargo.toml",
)


def _expand_double_star(
    root: Path,
    pattern: str,
    seen: set[str],
) -> list[WorkspacePackage]:
    """Recursively expand a workspace glob containing ``**``.

    A ``packages/**`` or ``packages/**/*`` pattern from pnpm-workspace.yaml
    is supposed to enumerate EVERY descendant directory that hosts a
    package — not just immediate children. The previous single-level
    ``iterdir()`` implementation missed nested layouts like
    ``packages/db/src/schema/users/``.

    We walk the prefix dir (the path segments before the first ``*``),
    then descend up to :data:`_GLOB_MAX_DEPTH` levels, emitting one
    workspace per directory that:

      - matches the glob via :func:`fnmatch`
      - contains one of the :data:`_WORKSPACE_MANIFEST_FILENAMES`
        (so source-only directories don't fabricate workspaces)
      - is not under a noise dir (``node_modules/``, ``target/``, …)
    """
    out: list[WorkspacePackage] = []
    # The literal prefix is everything before the first ``*``/``?``.
    # e.g. ``packages/**`` → prefix ``packages``, ``a/b/**/c`` → ``a/b``.
    prefix = pattern.split("*")[0].rstrip("/")
    base = root / prefix if prefix else root
    if not base.is_dir():
        return out

    # Depth is measured from ``base`` so a 6-deep cap genuinely means
    # 6 directory segments below the prefix root.
    base_depth = len(base.parts)

    for dirpath, dirnames, _filenames in __import__("os").walk(base):
        # Prune noise dirs in-place so os.walk skips them.
        dirnames[:] = [
            d for d in dirnames
            if d not in _GLOB_SKIP_DIRS and not d.startswith(".")
        ]
        cur = Path(dirpath)
        depth = len(cur.parts) - base_depth
        if depth > _GLOB_MAX_DEPTH:
            # Don't recurse further; clearing dirnames stops descent.
            dirnames[:] = []
            continue

        try:
            rel = str(cur.relative_to(root))
        except ValueError:
            continue
        if not rel or rel in seen:
            continue
        if not fnmatch(rel, pattern):
            continue
        # Require a manifest — otherwise it's a source dir, not a pkg.
        if not any((cur / m).is_file() for m in _WORKSPACE_MANIFEST_FILENAMES):
            continue
        name = _package_name(cur)
        out.append(WorkspacePackage(name=name, path=rel))
        seen.add(rel)

    return out


def _resolve_globs(root: Path, patterns: list[str]) -> list[WorkspacePackage]:
    """Resolve workspace glob patterns to actual directories.

    Handles three glob shapes:
      1. ``**`` patterns (``packages/**`` or ``packages/**/*``) →
         recursive expansion via :func:`_expand_double_star` with a
         depth cap and noise-dir skiplist. Requires each emitted dir
         to contain a package manifest.
      2. Single-level globs (``packages/*``) → ``iterdir()`` of the
         prefix dir, original behaviour preserved.
      3. Exact paths → checked for existence and emitted as-is.
    """
    packages: list[WorkspacePackage] = []
    seen: set[str] = set()

    for pattern in patterns:
        pattern = pattern.rstrip("/")

        if "**" in pattern:
            # Recursive expansion path. Handles ``packages/**`` and
            # ``packages/**/*`` uniformly — the trailing ``/*`` is
            # equivalent to the bare ``**`` for our purposes since we
            # filter by manifest presence anyway.
            packages.extend(_expand_double_star(root, pattern, seen))
            continue

        if "*" in pattern or "?" in pattern:
            # Single-level glob like "packages/*" — preserve original
            # behaviour (no manifest requirement here so we don't break
            # back-compat for the many repos that rely on it).
            parent = pattern.split("*")[0].rstrip("/")
            parent_path = root / parent
            if not parent_path.is_dir():
                continue

            for child in sorted(parent_path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    if child.name in _GLOB_SKIP_DIRS:
                        continue
                    rel = str(child.relative_to(root))
                    if fnmatch(rel, pattern) and rel not in seen:
                        name = _package_name(child)
                        packages.append(WorkspacePackage(name=name, path=rel))
                        seen.add(rel)
        else:
            # Exact path
            exact = root / pattern
            if exact.is_dir() and pattern not in seen:
                name = _package_name(exact)
                packages.append(WorkspacePackage(name=name, path=pattern))
                seen.add(pattern)

    return packages


def _package_name(pkg_dir: Path) -> str:
    """Extract a human-readable package name."""
    # Try reading package.json name
    pkg_json = pkg_dir / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text())
            name = data.get("name", "")
            if name:
                # Strip scope: @myorg/auth → auth
                return name.split("/")[-1]
        except (json.JSONDecodeError, OSError):
            pass

    # Try reading Cargo.toml name
    cargo = pkg_dir / "Cargo.toml"
    if cargo.exists():
        try:
            for line in cargo.read_text().splitlines():
                m = re.match(r'^name\s*=\s*"(.+)"', line)
                if m:
                    return m.group(1)
        except OSError:
            pass

    # Fallback to directory name
    return pkg_dir.name


# ── Workspace type detectors ──


def _detect_pnpm(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect pnpm workspace (pnpm-workspace.yaml)."""
    ws_file = root / "pnpm-workspace.yaml"
    if not ws_file.exists():
        return None

    try:
        content = ws_file.read_text()
        patterns = _parse_yaml_list(content, "packages")
        if not patterns:
            return None

        packages = _resolve_globs(root, patterns)
        return WorkspaceInfo(detected=True, manager="pnpm", packages=packages)
    except OSError:
        return None


def _detect_npm_yarn(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect npm/yarn workspaces (package.json workspaces field)."""
    pkg_json = root / "package.json"
    if not pkg_json.exists():
        return None

    try:
        data = json.loads(pkg_json.read_text())
        workspaces = data.get("workspaces")
        if not workspaces:
            return None

        # yarn can have { packages: [...] } or [...]
        if isinstance(workspaces, dict):
            patterns = workspaces.get("packages", [])
        elif isinstance(workspaces, list):
            patterns = workspaces
        else:
            return None

        if not patterns:
            return None

        manager = "yarn" if (root / "yarn.lock").exists() else "npm"
        packages = _resolve_globs(root, patterns)
        return WorkspaceInfo(detected=True, manager=manager, packages=packages)
    except (json.JSONDecodeError, OSError):
        return None


def _detect_turbo(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect Turborepo — uses npm/yarn/pnpm workspaces underneath."""
    if not (root / "turbo.json").exists():
        return None

    # Turbo relies on the package manager's workspace config
    info = _detect_pnpm(root, files) or _detect_npm_yarn(root, files)
    if info and info.detected:
        info.manager = "turbo"
    return info


def _detect_nx(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect Nx workspace."""
    if not (root / "nx.json").exists():
        return None

    packages: list[WorkspacePackage] = []

    # Nx projects can be in apps/, libs/, packages/ directories
    for search_dir in ["apps", "libs", "packages", "modules"]:
        search_path = root / search_dir
        if search_path.is_dir():
            for child in sorted(search_path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    # Verify it's an Nx project (has project.json or package.json)
                    if (child / "project.json").exists() or (child / "package.json").exists():
                        name = _package_name(child)
                        packages.append(WorkspacePackage(
                            name=name,
                            path=str(child.relative_to(root)),
                        ))

    if not packages:
        return None

    return WorkspaceInfo(detected=True, manager="nx", packages=packages)


def _detect_lerna(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect Lerna monorepo."""
    lerna_file = root / "lerna.json"
    if not lerna_file.exists():
        return None

    try:
        data = json.loads(lerna_file.read_text())
        patterns = data.get("packages", ["packages/*"])
        packages = _resolve_globs(root, patterns)
        return WorkspaceInfo(detected=True, manager="lerna", packages=packages)
    except (json.JSONDecodeError, OSError):
        return None


def _detect_cargo(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect Cargo workspace (Cargo.toml [workspace])."""
    cargo = root / "Cargo.toml"
    if not cargo.exists():
        return None

    try:
        content = cargo.read_text()
        if "[workspace]" not in content:
            return None

        # Parse members from [workspace] section
        patterns: list[str] = []
        in_members = False
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("members"):
                in_members = True
                # Handle inline: members = ["crate-a", "crate-b"]
                m = re.search(r'\[(.+)\]', line)
                if m:
                    for item in m.group(1).split(","):
                        item = item.strip().strip('"').strip("'")
                        if item:
                            patterns.append(item)
                    in_members = False
                continue
            if in_members:
                if line == "]":
                    in_members = False
                    continue
                item = line.strip(",").strip().strip('"').strip("'")
                if item:
                    patterns.append(item)

        if not patterns:
            return None

        packages = _resolve_globs(root, patterns)
        return WorkspaceInfo(detected=True, manager="cargo", packages=packages)
    except OSError:
        return None


def _detect_go(root: Path, files: list[str]) -> WorkspaceInfo | None:
    """Detect Go workspace (go.work)."""
    go_work = root / "go.work"
    if not go_work.exists():
        return None

    try:
        content = go_work.read_text()
        patterns: list[str] = []
        in_use = False
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("use ("):
                in_use = True
                continue
            if line == ")" and in_use:
                in_use = False
                continue
            if line.startswith("use ") and not in_use:
                patterns.append(line.split()[1])
            elif in_use and line:
                patterns.append(line)

        if not patterns:
            return None

        # Go workspace paths are relative dirs
        packages: list[WorkspacePackage] = []
        for p in patterns:
            p = p.strip().rstrip("/")
            pkg_path = root / p
            if pkg_path.is_dir():
                packages.append(WorkspacePackage(name=pkg_path.name, path=p))

        return WorkspaceInfo(detected=True, manager="go", packages=packages)
    except OSError:
        return None


# ── Utility ──


def _parse_yaml_list(content: str, key: str) -> list[str]:
    """Minimal YAML parser — extracts a list under a given key.

    Avoids requiring PyYAML as a dependency. Handles:
        packages:
          - "apps/*"
          - "packages/*"
    """
    items: list[str] = []
    in_key = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            in_key = True
            continue
        if in_key:
            if stripped.startswith("- "):
                item = stripped[2:].strip().strip('"').strip("'")
                if item:
                    items.append(item)
            elif stripped and not stripped.startswith("#"):
                break  # new key started
    return items
