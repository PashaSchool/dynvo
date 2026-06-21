"""Stage 6.6 — Monorepo Assembly View (cross-project graph + per-project grouping).

The DETERMINISTIC, $0, ADDITIVE output layer that sits on top of the
BRAIN-PARTITIONER (:mod:`faultline.pipeline_v2.stage_0_6_project_classifier`).
It re-projects the ONE flat whole-repo scan into a per-PROJECT structure
WITHOUT re-scanning, and extracts the internal dependency graph between the
monorepo's projects directly from manifests.

Two phases (both pure, manifest-grounded, no LLM, no network):

  Phase 3 — Cross-project dependency graph
  ----------------------------------------
  Extract internal dependency EDGES between the repo's projects from their
  manifests, per ecosystem:

    - JS/TS: a project's ``package.json`` ``dependencies`` /
      ``devDependencies`` / ``peerDependencies`` key that matches another
      workspace's CANONICAL package name (the ``name`` field of that
      workspace's ``package.json`` — the full ``@scope/pkg`` spelling, NOT
      the de-scoped :attr:`Workspace.name`), OR a ``workspace:*`` protocol
      value, yields an edge ``consumer -> dependency``.
    - Go: a ``go.work`` ``use`` directive, OR a ``go.mod`` ``require`` /
      local ``replace ... => ./rel`` referencing another module's declared
      module path / directory.
    - Rust: a ``Cargo.toml`` ``[dependencies]`` entry with a ``path = ...``
      resolving to another crate's directory (or a matching crate name).
    - Python: a ``pyproject.toml`` ``[tool.uv.sources]`` ``{ workspace =
      true }`` / ``{ path = ... }`` entry resolving to another project.

  ``fan_in`` per node = how many DISTINCT projects depend on it. A high
  fan-in surfaces the shared libraries (twenty ``twenty-shared`` / ``twenty-ui``,
  dub ``@dub/utils`` / ``@dub/ui``).

  Phase 4a — Per-project feature grouping
  ---------------------------------------
  Map each ``developer_feature`` to the project that OWNS it: the project
  whose ``subpath`` is the LONGEST path-prefix of the feature's file paths.
  A feature whose files span multiple projects is attributed to the
  DOMINANT project (the one owning the most of its files), and the spanning
  is recorded. A feature whose files match NO project subpath is recorded
  ``unassigned`` (e.g. repo-root tooling). Conservation: every feature is
  assigned to exactly one project OR recorded unassigned — none is lost.

Design tenets (mirror stage_0_6_project_classifier.py)
======================================================

  - Pure functions / value objects. Idempotent: same inputs -> same output.
  - Reuse, don't re-enumerate. Projects come from
    :func:`partition_monorepo` (the classifier's verdict list); manifest
    parsing reuses the classifier's ``_read_json_safe`` / ``_read_text_safe``
    / ``_deps_matching`` helpers and :mod:`tomllib`. We never re-walk the
    workspace tree divergently.
  - Composition over inheritance. Each ecosystem's edge rule is a tiny
    standalone class implementing the :class:`DepEdgeExtractor` Protocol;
    a dispatcher runs them all. No base class, no shared inheritance.
  - Universal, scale-invariant rules. NO magic numbers (longest-prefix and
    most-files-wins are STRUCTURAL, not tuned). NO repo-specific paths
    (only manifest fields + path-prefix arithmetic). See memory
    rule-no-magic-tuning + rule-no-repo-specific-paths.
  - No README parsing (CLAUDE.md). Manifests only.
  - Additive + gated. The assembly is built ONLY when the partition says
    ``is_monorepo``; a single repo gets a trivial (empty) view. The flat
    ``developer_features[]`` are read-only inputs — never mutated.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

from faultline.pipeline_v2.stage_0_6_project_classifier import (
    ProjectClassification,
    partition_monorepo,
)
from faultline.pipeline_v2.stage_0_6_shape import (
    _read_json_safe,
    _read_text_safe,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_0_6_project_classifier import (
        ProjectClassifier,
    )
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Dependency-section keys (industry-standard, not repo-specific) ──────

# package.json sections that declare a dependency on another package. The
# universal npm/yarn/pnpm convention — same set the classifier reads.
_JS_DEP_SECTIONS: tuple[str, ...] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

# The ``workspace:`` protocol prefix (pnpm / yarn-berry / bun). A dep VALUE
# starting with this is unambiguously an internal workspace reference even
# before we resolve the name — a strong corroborating signal.
_WORKSPACE_PROTOCOL: str = "workspace:"

# Cargo manifest dependency sections that may carry a local ``path`` dep.
_CARGO_DEP_SECTIONS: tuple[str, ...] = (
    "dependencies",
    "dev-dependencies",
    "build-dependencies",
)


# ── Value objects ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProjectNode:
    """One node in the cross-project dependency graph = one project.

    Mirrors a :class:`ProjectClassification` but carries the per-node
    ``fan_in`` computed once the edges are known.
    """

    name: str          # the project's display name (de-scoped, from classifier)
    type: str          # app | service | lib | tool | example
    subpath: str       # repo-root-relative dir, e.g. "packages/twenty-front"
    fan_in: int = 0    # number of DISTINCT projects that depend on this one


@dataclass(frozen=True, slots=True)
class ProjectEdge:
    """A directed internal dependency edge: ``from_project`` -> ``to_project``.

    Both ends are project subpaths (the stable identity — names can collide
    across ecosystems, paths cannot). ``via`` records HOW the edge was found
    (the matched dependency token / protocol / path) for telemetry + debug.
    """

    from_project: str  # subpath of the consumer
    to_project: str    # subpath of the dependency
    ecosystem: str     # js | go | rust | python
    via: str           # the matched name / "workspace:*" / resolved path


@dataclass(frozen=True, slots=True)
class _ProjectManifest:
    """Resolved manifest facts for ONE project, collected once.

    Pure structural snapshot used by the edge extractors. Reuses the
    classifier's safe readers; every field degrades to empty/None on a
    missing or malformed manifest (an edge extractor must never crash).
    """

    name: str
    subpath: str
    type: str
    abs_dir: Path
    # JS: the canonical ``name`` field of package.json (full @scope/pkg).
    pkg_canonical_name: str | None
    # JS: {dep_key: dep_value} merged across dep sections.
    pkg_deps: Mapping[str, str]
    # Go: the module path declared by this project's go.mod ``module`` line.
    go_module_path: str | None
    # Go: go.mod ``require`` module paths + local ``replace => ./rel`` targets.
    go_require_paths: tuple[str, ...]
    go_replace_targets: tuple[str, ...]  # resolved repo-root-relative dirs
    # Rust: crate name + {dep_name: resolved_repo_relative_path} path deps.
    cargo_crate_name: str | None
    cargo_path_deps: Mapping[str, str]  # dep_name -> repo-root-relative dir
    # Python: project name + resolved path-dep dirs (uv sources).
    py_project_name: str | None
    py_path_dep_targets: tuple[str, ...]  # repo-root-relative dirs


# ── Edge-extractor Protocol + per-ecosystem strategies ─────────────────


@runtime_checkable
class DepEdgeExtractor(Protocol):
    """Strategy for ONE ecosystem's internal-dependency edges.

    Contract:
      - MUST NOT raise; degrade to ``[]`` on any malformed input.
      - Pure function of the pre-collected manifests + the name/path
        indices. No I/O beyond what the manifests already hold.
    """

    ecosystem: str

    def extract_edges(
        self,
        manifests: Sequence[_ProjectManifest],
        *,
        name_to_subpath: Mapping[str, str],
        path_to_subpath: Mapping[str, str],
    ) -> list[ProjectEdge]:
        """Return the internal edges this ecosystem can see."""


class JsDepEdgeExtractor:
    """JS/TS edges: a dep key matching another workspace's canonical
    package name, OR a ``workspace:`` protocol value.

    Matching uses the canonical ``package.json`` ``name`` field
    (``name_to_subpath`` is keyed on it), NOT the de-scoped workspace name —
    ``@dub/utils`` in ``apps/web``'s deps must resolve to ``packages/utils``.
    """

    ecosystem: str = "js"

    def extract_edges(
        self,
        manifests: Sequence[_ProjectManifest],
        *,
        name_to_subpath: Mapping[str, str],
        path_to_subpath: Mapping[str, str],
    ) -> list[ProjectEdge]:
        edges: list[ProjectEdge] = []
        for m in manifests:
            if not m.pkg_deps:
                continue
            for dep_key, dep_val in m.pkg_deps.items():
                target = name_to_subpath.get(dep_key)
                if target is None:
                    continue
                if target == m.subpath:
                    continue  # never self-edge
                is_workspace_proto = isinstance(dep_val, str) and dep_val.startswith(
                    _WORKSPACE_PROTOCOL
                )
                via = (
                    f"{dep_key} ({dep_val})"
                    if is_workspace_proto
                    else f"{dep_key} (name-match)"
                )
                edges.append(
                    ProjectEdge(
                        from_project=m.subpath,
                        to_project=target,
                        ecosystem=self.ecosystem,
                        via=via,
                    )
                )
        return edges


class GoDepEdgeExtractor:
    """Go edges: ``go.mod`` ``require`` of another module's module path,
    OR a local ``replace ... => ./rel`` targeting another workspace dir.

    ``go.work`` ``use`` directives are handled at enumeration time (each
    becomes a workspace), so here we only connect modules that actually
    reference one another. A require matches when the required module path
    EQUALS or is a path-prefix of another workspace module's declared
    module path (covers ``code.gitea.io/gitea`` requiring
    ``code.gitea.io/gitea/models``).
    """

    ecosystem: str = "go"

    def extract_edges(
        self,
        manifests: Sequence[_ProjectManifest],
        *,
        name_to_subpath: Mapping[str, str],
        path_to_subpath: Mapping[str, str],
    ) -> list[ProjectEdge]:
        # Index: declared module path -> subpath (only projects with a go.mod).
        module_to_subpath: dict[str, str] = {
            m.go_module_path: m.subpath
            for m in manifests
            if m.go_module_path
        }
        edges: list[ProjectEdge] = []
        for m in manifests:
            seen: set[str] = set()
            # require <module-path>  (exact or module-prefix match)
            for req in m.go_require_paths:
                target = _match_go_module(req, module_to_subpath)
                if target is None or target == m.subpath or target in seen:
                    continue
                seen.add(target)
                edges.append(
                    ProjectEdge(m.subpath, target, self.ecosystem, f"require {req}")
                )
            # replace <x> => ./rel  (local path target)
            for rel in m.go_replace_targets:
                target = path_to_subpath.get(rel)
                if target is None or target == m.subpath or target in seen:
                    continue
                seen.add(target)
                edges.append(
                    ProjectEdge(m.subpath, target, self.ecosystem, f"replace => {rel}")
                )
        return edges


class RustDepEdgeExtractor:
    """Rust edges: a ``Cargo.toml`` ``[dependencies]`` entry with a
    ``path = "../crate"`` resolving to another crate's directory.

    Path resolution is the robust signal (crate names can be renamed via
    ``package = ...``); we additionally accept a bare crate-name match when
    the dep key equals another crate's ``[package].name``.
    """

    ecosystem: str = "rust"

    def extract_edges(
        self,
        manifests: Sequence[_ProjectManifest],
        *,
        name_to_subpath: Mapping[str, str],
        path_to_subpath: Mapping[str, str],
    ) -> list[ProjectEdge]:
        crate_name_to_subpath: dict[str, str] = {
            m.cargo_crate_name: m.subpath
            for m in manifests
            if m.cargo_crate_name
        }
        edges: list[ProjectEdge] = []
        for m in manifests:
            seen: set[str] = set()
            for dep_name, rel_dir in m.cargo_path_deps.items():
                target = path_to_subpath.get(rel_dir) or crate_name_to_subpath.get(
                    dep_name
                )
                if target is None or target == m.subpath or target in seen:
                    continue
                seen.add(target)
                edges.append(
                    ProjectEdge(
                        m.subpath, target, self.ecosystem, f"{dep_name} (path={rel_dir})"
                    )
                )
        return edges


class PythonDepEdgeExtractor:
    """Python edges: ``pyproject.toml`` ``[tool.uv.sources]`` entries with
    ``{ workspace = true }`` or ``{ path = "..." }`` resolving to another
    project (by resolved path, else by project name).
    """

    ecosystem: str = "python"

    def extract_edges(
        self,
        manifests: Sequence[_ProjectManifest],
        *,
        name_to_subpath: Mapping[str, str],
        path_to_subpath: Mapping[str, str],
    ) -> list[ProjectEdge]:
        project_name_to_subpath: dict[str, str] = {
            m.py_project_name: m.subpath
            for m in manifests
            if m.py_project_name
        }
        edges: list[ProjectEdge] = []
        for m in manifests:
            seen: set[str] = set()
            for rel_dir in m.py_path_dep_targets:
                target = path_to_subpath.get(rel_dir) or project_name_to_subpath.get(
                    rel_dir
                )
                if target is None or target == m.subpath or target in seen:
                    continue
                seen.add(target)
                edges.append(
                    ProjectEdge(m.subpath, target, self.ecosystem, f"uv-source {rel_dir}")
                )
        return edges


# Registry — single source of truth, deterministically ordered by ecosystem.
_DEFAULT_EDGE_EXTRACTORS: tuple[DepEdgeExtractor, ...] = (
    JsDepEdgeExtractor(),
    GoDepEdgeExtractor(),
    RustDepEdgeExtractor(),
    PythonDepEdgeExtractor(),
)


# ── Manifest collection (reuse classifier readers) ─────────────────────


def _collect_manifest(
    repo_root: Path,
    clf: ProjectClassification,
) -> _ProjectManifest:
    """Collect the manifest facts for ONE project. Never raises."""
    abs_dir = (repo_root / clf.path).resolve()

    # ── JS ──
    pkg = _read_json_safe(abs_dir / "package.json")
    pkg_name: str | None = None
    pkg_deps: dict[str, str] = {}
    if isinstance(pkg, dict):
        nm = pkg.get("name")
        pkg_name = nm if isinstance(nm, str) and nm else None
        for sec in _JS_DEP_SECTIONS:
            block = pkg.get(sec)
            if isinstance(block, dict):
                for k, v in block.items():
                    if isinstance(k, str) and k not in pkg_deps:
                        pkg_deps[k] = v if isinstance(v, str) else ""

    # ── Go ──
    go_text = _read_text_safe(abs_dir / "go.mod")
    go_module, go_requires, go_replaces = _parse_go_mod(go_text, clf.path)

    # ── Rust ──
    cargo_text = _read_text_safe(abs_dir / "Cargo.toml")
    cargo_name, cargo_paths = _parse_cargo(cargo_text, clf.path)

    # ── Python ──
    py_text = _read_text_safe(abs_dir / "pyproject.toml")
    py_name, py_paths = _parse_pyproject(py_text, clf.path)

    return _ProjectManifest(
        name=clf.name,
        subpath=clf.path,
        type=clf.project_type,
        abs_dir=abs_dir,
        pkg_canonical_name=pkg_name,
        pkg_deps=pkg_deps,
        go_module_path=go_module,
        go_require_paths=go_requires,
        go_replace_targets=go_replaces,
        cargo_crate_name=cargo_name,
        cargo_path_deps=cargo_paths,
        py_project_name=py_name,
        py_path_dep_targets=py_paths,
    )


def _parse_go_mod(
    text: str | None, project_subpath: str
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Parse a go.mod: module path, required module paths, local replace dirs.

    Hand-parsed line-wise (go.mod is not TOML/JSON). Defensive: any line that
    doesn't fit a directive is ignored. Local ``replace => ./rel`` targets are
    resolved to repo-root-relative dirs against ``project_subpath``.
    """
    if not text:
        return None, (), ()
    module: str | None = None
    requires: list[str] = []
    replaces: list[str] = []
    in_require_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()  # strip line comments
        if not line:
            continue
        if module is None and line.startswith("module "):
            module = line[len("module ") :].strip()
            continue
        # require ( ... ) block
        if line.startswith("require (") or line == "require (":
            in_require_block = True
            continue
        if in_require_block:
            if line == ")":
                in_require_block = False
                continue
            mod = line.split()[0]
            if mod:
                requires.append(mod)
            continue
        if line.startswith("require "):
            rest = line[len("require ") :].strip()
            if rest and not rest.startswith("("):
                requires.append(rest.split()[0])
            continue
        # replace <old> => <new>   (only local ./ or ../ targets matter)
        if line.startswith("replace ") and "=>" in line:
            target = line.split("=>", 1)[1].strip().split()[0]
            if target.startswith(".") or target.startswith("/"):
                resolved = _resolve_relative(project_subpath, target)
                if resolved is not None:
                    replaces.append(resolved)
            continue
    return module, tuple(requires), tuple(replaces)


def _parse_cargo(
    text: str | None, project_subpath: str
) -> tuple[str | None, dict[str, str]]:
    """Parse a Cargo manifest: crate name + {dep_name: resolved repo-rel dir}.

    Uses :mod:`tomllib` (NOT regex). Only ``[package]`` crates (not the root
    ``[workspace]`` virtual manifest) contribute a crate name + path deps.
    Path-dep values (``{ path = "../x" }``) are resolved to repo-root-relative
    dirs against ``project_subpath``.
    """
    if not text or "[package]" not in text:
        return None, {}
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None, {}
    pkg = data.get("package")
    crate_name = None
    if isinstance(pkg, dict):
        nm = pkg.get("name")
        crate_name = nm if isinstance(nm, str) and nm else None
    path_deps: dict[str, str] = {}
    for sec in _CARGO_DEP_SECTIONS:
        block = data.get(sec)
        if not isinstance(block, dict):
            continue
        for dep_name, spec in block.items():
            if not isinstance(dep_name, str) or not isinstance(spec, dict):
                continue
            rel = spec.get("path")
            if isinstance(rel, str) and rel:
                resolved = _resolve_relative(project_subpath, rel)
                if resolved is not None:
                    path_deps[dep_name] = resolved
    return crate_name, path_deps


def _parse_pyproject(
    text: str | None, project_subpath: str
) -> tuple[str | None, tuple[str, ...]]:
    """Parse a pyproject: PEP621 project name + uv-workspace path-dep dirs.

    Uses :mod:`tomllib`. ``[tool.uv.sources]`` entries shaped
    ``{ workspace = true }`` or ``{ path = "..." }`` are internal deps. A
    ``workspace = true`` source names a sibling project by its dependency
    key (resolved later by project name); a ``path`` source resolves to a
    repo-root-relative dir.
    """
    if not text or "[project]" not in text:
        return None, ()
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None, ()
    proj = data.get("project")
    proj_name = None
    if isinstance(proj, dict):
        nm = proj.get("name")
        proj_name = nm if isinstance(nm, str) and nm else None
    targets: list[str] = []
    tool = data.get("tool")
    sources = (
        tool.get("uv", {}).get("sources")
        if isinstance(tool, dict) and isinstance(tool.get("uv"), dict)
        else None
    )
    if isinstance(sources, dict):
        for dep_name, spec in sources.items():
            if not isinstance(dep_name, str) or not isinstance(spec, dict):
                continue
            rel = spec.get("path")
            if isinstance(rel, str) and rel:
                resolved = _resolve_relative(project_subpath, rel)
                if resolved is not None:
                    targets.append(resolved)
            elif spec.get("workspace") is True:
                # Internal workspace member named by its dependency key —
                # resolved against the project-name index downstream.
                targets.append(dep_name)
    return proj_name, tuple(targets)


def _resolve_relative(base_subpath: str, rel: str) -> str | None:
    """Resolve a manifest-relative path to a repo-root-relative POSIX dir.

    ``base_subpath`` is the consuming project's repo-root-relative dir;
    ``rel`` is a ``./`` or ``../`` path from THAT dir. Returns the
    normalised repo-root-relative target, or ``None`` if it escapes the
    repo root (a guard against ``../../`` walking out of the tree).
    """
    try:
        base = Path(base_subpath)
        combined = (base / rel) if rel not in ("", ".") else base
        # Normalise .. / . without touching the filesystem (paths may be
        # synthetic in tests). PurePosix-style resolution.
        parts: list[str] = []
        for seg in combined.as_posix().split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    return None  # escaped the repo root
                parts.pop()
            else:
                parts.append(seg)
        return "/".join(parts) if parts else None
    except (ValueError, OSError):
        return None


def _match_go_module(
    required: str, module_to_subpath: Mapping[str, str]
) -> str | None:
    """Return the subpath of the workspace module a ``require`` references.

    Exact match first; otherwise the LONGEST declared module path that is a
    path-prefix of ``required`` (so ``code.gitea.io/gitea/models`` matches a
    module declaring ``code.gitea.io/gitea``). Prefix is segment-aware to
    avoid ``foo/bar`` matching ``foo/barbaz``.
    """
    if required in module_to_subpath:
        return module_to_subpath[required]
    best: tuple[int, str] | None = None
    for mod, sub in module_to_subpath.items():
        if required == mod or required.startswith(mod + "/"):
            depth = mod.count("/")
            if best is None or depth > best[0]:
                best = (depth, sub)
    return best[1] if best is not None else None


# ── Phase 3 — graph builder ────────────────────────────────────────────


def build_cross_project_graph(
    repo_root: Path,
    classifications: Sequence[ProjectClassification],
    *,
    extractors: Sequence[DepEdgeExtractor] | None = None,
) -> tuple[list[ProjectNode], list[ProjectEdge]]:
    """Build the internal dependency graph over ALL classified projects.

    Nodes = every project (app/service/lib/tool/example) — shared libs are
    nodes so their ``fan_in`` is visible. Edges = internal dependencies
    discovered by the per-ecosystem extractors, deduplicated by
    (from, to). ``fan_in`` counts DISTINCT consumers of each node.

    Pure + deterministic. Returns ``([], [])`` for an empty project list.
    """
    pipeline = extractors if extractors is not None else _DEFAULT_EDGE_EXTRACTORS
    manifests = [_collect_manifest(repo_root, c) for c in classifications]

    # Indices the extractors match against (canonical name + subpath).
    name_to_subpath: dict[str, str] = {}
    for m in manifests:
        if m.pkg_canonical_name:
            name_to_subpath.setdefault(m.pkg_canonical_name, m.subpath)
    path_to_subpath: dict[str, str] = {m.subpath: m.subpath for m in manifests}

    raw_edges: list[ProjectEdge] = []
    for ex in pipeline:
        try:
            raw_edges.extend(
                ex.extract_edges(
                    manifests,
                    name_to_subpath=name_to_subpath,
                    path_to_subpath=path_to_subpath,
                )
            )
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "dep-edge extractor %s raised (%s); skipping",
                getattr(ex, "ecosystem", "?"),
                exc,
            )

    # Dedup edges by (from, to); keep the first ``via`` deterministically.
    deduped: dict[tuple[str, str], ProjectEdge] = {}
    for e in sorted(raw_edges, key=lambda x: (x.from_project, x.to_project, x.ecosystem, x.via)):
        deduped.setdefault((e.from_project, e.to_project), e)
    edges = list(deduped.values())

    # fan_in = number of distinct consumers per dependency subpath.
    fan_in: dict[str, set[str]] = {}
    for e in edges:
        fan_in.setdefault(e.to_project, set()).add(e.from_project)

    nodes = [
        ProjectNode(
            name=c.name,
            type=c.project_type,
            subpath=c.path,
            fan_in=len(fan_in.get(c.path, ())),
        )
        for c in classifications
    ]
    nodes.sort(key=lambda n: (-n.fan_in, n.subpath))
    edges.sort(key=lambda e: (e.from_project, e.to_project))
    return nodes, edges


# ── Phase 4a — feature -> project grouping ─────────────────────────────


@dataclass(frozen=True, slots=True)
class FeatureAssignment:
    """One feature's project assignment (Phase 4a)."""

    feature_uuid: str
    feature_name: str
    project_subpath: str | None  # None = unassigned (no project owns its files)
    file_count: int              # total distinct files considered
    # When the feature's files span >1 project: {project_subpath: file_count}.
    spanning: Mapping[str, int] = field(default_factory=dict)


def _feature_files(feature: "Feature") -> list[str]:
    """Distinct repo-root-relative files for a feature.

    Union of ``paths`` (the 1:1 owned-file / blame surface) and
    ``member_files[].path`` (the additive N:M claim ledger). Order-stable,
    de-duplicated. ``paths`` is primary; member_files only ADD coverage.
    """
    seen: set[str] = set()
    out: list[str] = []
    for p in feature.paths:
        if isinstance(p, str) and p and p not in seen:
            seen.add(p)
            out.append(p)
    for mf in getattr(feature, "member_files", ()) or ():
        mf_path = getattr(mf, "path", None)
        if isinstance(mf_path, str) and mf_path and mf_path not in seen:
            seen.add(mf_path)
            out.append(mf_path)
    return out


def _longest_prefix_project(
    file_path: str, project_subpaths: Sequence[str]
) -> str | None:
    """Return the project subpath that is the LONGEST path-prefix of a file.

    Segment-aware: ``apps/web`` is a prefix of ``apps/web/app/page.tsx`` but
    NOT of ``apps/web-admin/x.ts``. The longest (deepest) matching subpath
    wins, so a file under a nested unit attributes to the nearest project.
    """
    file_segs = file_path.split("/")
    best: tuple[int, str] | None = None
    for sub in project_subpaths:
        sub_segs = sub.split("/")
        if len(sub_segs) <= len(file_segs) and file_segs[: len(sub_segs)] == sub_segs:
            depth = len(sub_segs)
            if best is None or depth > best[0]:
                best = (depth, sub)
    return best[1] if best is not None else None


def assign_features_to_projects(
    features: Sequence["Feature"],
    project_subpaths: Sequence[str],
) -> list[FeatureAssignment]:
    """Assign every feature to the project owning the MOST of its files.

    Per feature:
      - Map each file to its longest-prefix project.
      - The DOMINANT project (most files) wins the assignment.
      - If files span >1 project, record the per-project counts in
        ``spanning``.
      - If NO file matches any project subpath, the feature is unassigned
        (``project_subpath=None``).

    Deterministic tie-break: when two projects own an equal number of a
    feature's files, the lexicographically-smallest subpath wins (stable,
    not magic). Conservation is the caller's invariant: this returns exactly
    one :class:`FeatureAssignment` per input feature.
    """
    assignments: list[FeatureAssignment] = []
    subpaths = list(project_subpaths)
    for f in features:
        files = _feature_files(f)
        counts: dict[str, int] = {}
        for fp in files:
            owner = _longest_prefix_project(fp, subpaths)
            if owner is not None:
                counts[owner] = counts.get(owner, 0) + 1
        if not counts:
            assignments.append(
                FeatureAssignment(
                    feature_uuid=f.uuid,
                    feature_name=f.name,
                    project_subpath=None,
                    file_count=len(files),
                    spanning={},
                )
            )
            continue
        # Dominant: max file-count, tie-break on lexicographic subpath.
        dominant = max(counts.items(), key=lambda kv: (kv[1], _neg_lex(kv[0])))[0]
        spanning = dict(sorted(counts.items())) if len(counts) > 1 else {}
        assignments.append(
            FeatureAssignment(
                feature_uuid=f.uuid,
                feature_name=f.name,
                project_subpath=dominant,
                file_count=len(files),
                spanning=spanning,
            )
        )
    return assignments


def _neg_lex(s: str) -> tuple[int, ...]:
    """Key that makes ``max`` prefer the lexicographically-SMALLEST string.

    ``max(..., key=lambda kv:(count, _neg_lex(sub)))`` thus picks the
    highest count and, on a tie, the smallest subpath — a stable,
    documented tie-break (not a magic numeric threshold).
    """
    return tuple(-ord(c) for c in s)


# ── Top-level assembly ─────────────────────────────────────────────────


def build_monorepo_assembly(
    ctx: "ScanContext",
    features: Sequence["Feature"],
    *,
    classifiers: Sequence["ProjectClassifier"] | None = None,
    edge_extractors: Sequence[DepEdgeExtractor] | None = None,
) -> dict[str, Any]:
    """Build the additive ``monorepo`` output field. Deterministic, $0.

    Returns ``{"is_monorepo": False}`` (a trivial back-compat view) when the
    partition says the repo is NOT a monorepo — single repos get no project
    structure. Otherwise returns the full assembly:

        {
          "is_monorepo": True,
          "projects": [
            {name, type, subpath, fan_in, feature_uuids, feature_count,
             file_count, spanning_count, spanning_ratio},
            ...
          ],
          "cross_project_graph": {"nodes": [...], "edges": [...]},
          "unassigned_features": [{uuid, name, file_count}, ...],
          "spanning_features": [{uuid, name, dominant, spanning}, ...],
          "stats": {assigned_pct, feature_total, assigned, unassigned, spanning, edges},
          "rationale": "...",
        }

    The flat ``developer_features[]`` are read-only inputs — this function
    never mutates them.
    """
    plan = partition_monorepo(ctx, classifiers=classifiers)
    if not plan.is_monorepo:
        return {"is_monorepo": False}

    repo_root = Path(ctx.repo_path).resolve()

    # ── Phase 3 — graph over every classified project ──
    nodes, edges = build_cross_project_graph(
        repo_root, plan.classifications, extractors=edge_extractors
    )

    # ── Phase 4a — feature -> project grouping ──
    # Group ALL projects (not just scan UNITS): a feature physically living
    # in a lib package must attribute to that lib, not to an unrelated app.
    project_subpaths = [c.path for c in plan.classifications]
    assignments = assign_features_to_projects(features, project_subpaths)

    # Index features by uuid for loc lookup.
    feat_by_uuid: dict[str, "Feature"] = {f.uuid: f for f in features}

    # Aggregate per project.
    per_project_uuids: dict[str, list[str]] = {c.path: [] for c in plan.classifications}
    unassigned: list[dict[str, Any]] = []
    spanning: list[dict[str, Any]] = []
    for a in assignments:
        if a.project_subpath is None:
            unassigned.append(
                {"uuid": a.feature_uuid, "name": a.feature_name, "file_count": a.file_count}
            )
            continue
        per_project_uuids.setdefault(a.project_subpath, []).append(a.feature_uuid)
        if a.spanning:
            spanning.append(
                {
                    "uuid": a.feature_uuid,
                    "name": a.feature_name,
                    "dominant": a.project_subpath,
                    "spanning": dict(a.spanning),
                }
            )

    # Per-project spanning count: features homed HERE whose files actually
    # span >1 project (assigned by the dominant-share heuristic). A high
    # spanning_ratio means this project's feature list is mostly cross-cutting
    # blobs (the flat scan's blob-bug leaking through) — a trustworthiness
    # signal so a consumer can suppress low-confidence per-project ownership
    # (e.g. cal-com homes many features to a lib at 6% dominant share).
    spanning_by_project: dict[str, int] = {}
    for s in spanning:
        spanning_by_project[s["dominant"]] = spanning_by_project.get(s["dominant"], 0) + 1

    node_by_subpath = {n.subpath: n for n in nodes}
    projects: list[dict[str, Any]] = []
    for c in plan.classifications:
        uuids = per_project_uuids.get(c.path, [])
        file_count = _project_loc(uuids, feat_by_uuid)
        node = node_by_subpath.get(c.path)
        span_n = spanning_by_project.get(c.path, 0)
        projects.append(
            {
                "name": c.name,
                "type": c.project_type,
                "subpath": c.path,
                "fan_in": node.fan_in if node is not None else 0,
                "feature_uuids": uuids,
                "feature_count": len(uuids),
                "file_count": file_count,
                "spanning_count": span_n,
                "spanning_ratio": round(span_n / len(uuids), 3) if uuids else 0.0,
            }
        )
    # Order projects: scan-worthy first (app/service), then by feature count.
    _unit_rank = {"app": 0, "service": 0}
    projects.sort(
        key=lambda p: (_unit_rank.get(p["type"], 1), -p["feature_count"], p["subpath"])
    )

    feature_total = len(features)
    assigned = feature_total - len(unassigned)
    assigned_pct = round(assigned / feature_total, 4) if feature_total else 1.0

    return {
        "is_monorepo": True,
        "projects": projects,
        "cross_project_graph": {
            "nodes": [
                {"name": n.name, "type": n.type, "subpath": n.subpath, "fan_in": n.fan_in}
                for n in nodes
            ],
            "edges": [
                {
                    "from": e.from_project,
                    "to": e.to_project,
                    "ecosystem": e.ecosystem,
                    "via": e.via,
                }
                for e in edges
            ],
        },
        "unassigned_features": unassigned,
        "spanning_features": spanning,
        "stats": {
            "feature_total": feature_total,
            "assigned": assigned,
            "unassigned": len(unassigned),
            "spanning": len(spanning),
            "assigned_pct": assigned_pct,
            "project_count": len(projects),
            "edge_count": len(edges),
        },
        "rationale": plan.rationale,
    }


def _project_loc(
    feature_uuids: Sequence[str], feat_by_uuid: Mapping[str, "Feature"]
) -> int:
    """Distinct FILE count for a project (emitted as the ``file_count`` field).

    NOT lines-of-code: we do not re-read files. It is the number of distinct
    files attributed to the project's features — deterministic + cheap.
    Callers that need true LOC can join on ``path_index``. Distinct across
    the project's features so a shared file isn't double-counted. (The field
    was renamed from the misleading ``loc`` per independent audit 2026-06-21.)
    """
    files: set[str] = set()
    for uid in feature_uuids:
        f = feat_by_uuid.get(uid)
        if f is None:
            continue
        for p in _feature_files(f):
            files.add(p)
    return len(files)


__all__ = [
    "ProjectNode",
    "ProjectEdge",
    "DepEdgeExtractor",
    "JsDepEdgeExtractor",
    "GoDepEdgeExtractor",
    "RustDepEdgeExtractor",
    "PythonDepEdgeExtractor",
    "FeatureAssignment",
    "build_cross_project_graph",
    "assign_features_to_projects",
    "build_monorepo_assembly",
]
