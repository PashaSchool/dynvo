"""Track-B py_ast M3 — resolve Python import edges to repo files.

The Python mirror of ``ts_ast.resolve`` (w6ast-spec §3.M3). Turns each
raw :class:`ImportEdge` into one or more :class:`ResolvedEdge`, deciding
the target file (or classifying it external) with a config- AND
data-derived model of the repo's import surface:

* **Source roots** — where a dotted module resolves. Always the repo
  root ``""``; plus ``src/`` (src-layout); plus poetry/setuptools
  ``package-dir`` / ``from=`` / ``where=`` roots read from
  ``pyproject.toml`` / ``setup.cfg``; plus **data-derived roots**: a
  first component that is not a repo top-level module but resolves under
  exactly-best directory ``D`` (e.g. ``onyx`` living at ``backend/onyx``
  ⇒ ``backend`` is a source root; ``routers`` at ``backend/routers`` ⇒
  ``backend``). No hard-coded directory names — the roots are discovered
  from the imports themselves.
* **Module index** — every tracked ``.py`` mapped to its dotted name(s)
  under each root (``a/b.py`` → ``a.b``; ``a/b/__init__.py`` → ``a.b``).
  Collisions prefer the shallowest root, then the shortest dotted name.
* **PEP-420 namespace packages** — a directory with ``.py`` descendants
  but no ``__init__.py`` still resolves (``ns.mod`` → ``ns/mod.py``).
* **Relative imports** (PEP-328) — ``.mod`` / ``..pkg.mod`` resolved
  against the importing file's package, honouring the leading-dot level.
* **``from M import n1, n2``** — split into one :class:`ResolvedEdge`
  per distinct target: a name that is a SUBMODULE of package ``M``
  (``M/n.py``) resolves finer than one that is an attribute of ``M``
  (the package/module file); ``__init__`` re-exports are descended
  (barrel, cycle-safe, depth cap 8).

Resolution buckets (metric ruler mirrors ts_ast; non-external = the
denominator of resolution-%): ``relative`` (dotted in-repo import),
``workspace`` (absolute import resolved to a first-party repo file),
``package_external`` (stdlib / third-party — first component is not a
repo package), ``unresolved`` (in-repo intent, no file found).

Signature is AMENDMENT-2: ``resolve_edges(...) -> (resolved, telemetry)``.
Determinism: indices built from sorted inputs, output canonically
sorted, no set-iteration reaches the result.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from faultline.pipeline_v2.py_ast.shapes import ImportEdge, ResolvedEdge

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.pipeline_v2.py_ast.shapes import ExportEntry

logger = logging.getLogger(__name__)

__all__ = ["resolve_edges", "PyResolver"]

_INIT = "__init__.py"
_BARREL_DEPTH_CAP = 8


def _posix(p: str) -> str:
    return p.replace("\\", "/")


def _module_of(rel: str, root: str) -> str | None:
    """Dotted module name of ``rel`` under source ``root`` (or ``None``)."""
    rel = _posix(rel)
    if root:
        prefix = root.rstrip("/") + "/"
        if not rel.startswith(prefix):
            return None
        rel = rel[len(prefix):]
    if not rel.endswith(".py"):
        return None
    stem = rel[:-3]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    elif stem == "__init__":
        return None  # root package initialiser — no dotted name
    return stem.replace("/", ".") if stem else None


class PyResolver:
    """Builds the repo's import indices once; resolves edges against them."""

    def __init__(
        self,
        repo_root: str,
        tracked: Iterable[str],
        edges: Sequence[ImportEdge],
        exports_index: Mapping[str, Sequence["ExportEntry"]] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.tracked = frozenset(_posix(f) for f in tracked)
        self._py = sorted(f for f in self.tracked if f.endswith(".py"))
        self._dirs = self._dir_set(self._py)
        self.roots = self._discover_roots(edges)
        self.module_to_file = self._build_module_index()
        self._pkg_modules = self._build_package_set()
        self._top_level = self._build_top_level()
        # per-__init__ re-export map: name -> raw relative target (barrel)
        self._reexports = self._build_reexport_map(edges)

    # ── index construction ────────────────────────────────────────────

    @staticmethod
    def _dir_set(files: Sequence[str]) -> frozenset[str]:
        dirs: set[str] = set()
        for f in files:
            segs = f.split("/")[:-1]
            for i in range(1, len(segs) + 1):
                dirs.add("/".join(segs[:i]))
        return frozenset(dirs)

    def _read_pyproject_roots(self) -> list[str]:
        """Explicit src roots from pyproject.toml / setup.cfg (best-effort)."""
        roots: list[str] = []
        for name in ("pyproject.toml", "setup.cfg"):
            path = os.path.join(self.repo_root, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            # poetry: packages = [{ include = "x", from = "src" }]
            for m in re.finditer(r"from\s*=\s*['\"]([^'\"]+)['\"]", text):
                roots.append(m.group(1).strip("/"))
            # setuptools: package_dir = {"": "src"}  /  where = ["src"]
            for m in re.finditer(
                r"package[_-]dir\s*=\s*\{[^}]*['\"]{2}\s*[:=]\s*['\"]([^'\"]+)['\"]",
                text,
            ):
                roots.append(m.group(1).strip("/"))
            for m in re.finditer(r"where\s*=\s*\[?\s*['\"]([^'\"]+)['\"]", text):
                roots.append(m.group(1).strip("/"))
        return roots

    def _resolves_under(self, comp: str, root: str) -> bool:
        base = (root.rstrip("/") + "/") if root else ""
        return (
            f"{base}{comp}.py" in self.tracked
            or f"{base}{comp}/{_INIT}" in self.tracked
            or f"{base}{comp}" in self._dirs
        )

    def _discover_roots(self, edges: Sequence[ImportEdge]) -> list[str]:
        roots: set[str] = {""}
        if any(f.startswith("src/") for f in self._py):
            roots.add("src")
        for r in self._read_pyproject_roots():
            if r and (r in self._dirs or r == "src"):
                roots.add(r)

        # data-derived: first components of ABSOLUTE imports that do not
        # resolve under the current roots → find the best directory that
        # makes the most of them resolve (greedy, deterministic).
        first_comps: set[str] = set()
        for e in edges:
            if e.raw_target.startswith("."):
                continue
            head = e.raw_target.split(".", 1)[0]
            if head:
                first_comps.add(head)
        unresolved = sorted(
            c for c in first_comps
            if not any(self._resolves_under(c, r) for r in roots)
        )
        # Candidate source-root dirs. A source root is a CONTAINER of
        # packages, not a package itself, so exclude any dir that is a
        # regular package (has its own ``__init__.py``) — that guard
        # alone kills the false positives (an app sub-dir like ``base``
        # or ``attendance/views`` is a package, never a root). Keep only
        # shallow dirs (roots are ``src`` / ``backend`` / ``apps/api``,
        # depth ≤ 2).
        cand_dirs = [
            d for d in sorted(self._dirs, key=lambda d: (d.count("/"), d))
            if d.count("/") <= 2
            and f"{d}/{_INIT}" not in self.tracked
        ]
        # A real source root hosts MULTIPLE first-party packages — require
        # ≥2 distinct unresolved first-components resolve under it, so a
        # lone external-name/subdir collision never mints a root.
        _MIN_ROOT_SUPPORT = 2
        guard = 0
        while unresolved and guard < 8:
            guard += 1
            best: tuple[int, int, str] | None = None  # (-hits, depth, dir)
            for d in cand_dirs:
                if d in roots:
                    continue
                hits = sum(1 for c in unresolved if self._resolves_under(c, d))
                if hits < _MIN_ROOT_SUPPORT:
                    continue
                key = (-hits, d.count("/"), d)
                if best is None or key < best:
                    best = key
            if best is None:
                break
            roots.add(best[2])
            unresolved = [
                c for c in unresolved
                if not any(self._resolves_under(c, r) for r in roots)
            ]
        return sorted(roots, key=lambda r: (r.count("/"), len(r), r))

    def _build_module_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for f in self._py:
            for root in self.roots:
                mod = _module_of(f, root)
                if mod is None:
                    continue
                prev = index.get(mod)
                if prev is None or self._better(f, prev, mod):
                    index[mod] = f
        return index

    @staticmethod
    def _better(new: str, old: str, mod: str) -> bool:
        """Collision tie-break: fewer path segments, then lexicographic.

        (A module reachable via a shallower root has a shorter file path;
        ``__init__`` packages and plain modules with the same dotted name
        are ordered deterministically.)
        """
        return (new.count("/"), new) < (old.count("/"), old)

    def _build_package_set(self) -> frozenset[str]:
        pkgs: set[str] = set()
        for mod, f in self.module_to_file.items():
            if f.endswith("/" + _INIT) or f == _INIT:
                pkgs.add(mod)
        # PEP-420 namespace packages: a dotted dir under a root with no
        # __init__ but with .py descendants is still a package for the
        # purpose of "M.sub is a submodule".
        for root in self.roots:
            base = (root.rstrip("/") + "/") if root else ""
            for d in self._dirs:
                if base and not d.startswith(base):
                    continue
                rel = d[len(base):] if base else d
                if rel:
                    pkgs.add(rel.replace("/", "."))
        return frozenset(pkgs)

    def _build_top_level(self) -> frozenset[str]:
        tops: set[str] = set()
        for mod in self.module_to_file:
            tops.add(mod.split(".", 1)[0])
        for pkg in self._pkg_modules:
            tops.add(pkg.split(".", 1)[0])
        return frozenset(t for t in tops if t)

    def _build_reexport_map(
        self, edges: Sequence[ImportEdge],
    ) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = defaultdict(dict)
        for e in edges:
            src = e.src_file
            if not (src == _INIT or src.endswith("/" + _INIT)):
                continue
            if not e.raw_target.startswith("."):
                continue  # only RELATIVE re-exports form a barrel
            for n in e.names:
                if n == "*":
                    continue
                local = n.rsplit(" as ", 1)[-1].strip()
                if local:
                    out[src].setdefault(local, e.raw_target)
        return {k: dict(v) for k, v in out.items()}

    # ── module lookup ─────────────────────────────────────────────────

    def _lookup_module(self, dotted: str) -> str | None:
        """Absolute dotted module → tracked file (``.py`` or ``__init__``)."""
        f = self.module_to_file.get(dotted)
        if f is not None:
            return f
        # namespace package with no __init__ → no single file target.
        return None

    def _is_package(self, dotted: str) -> bool:
        return dotted in self._pkg_modules

    def _package_dir_of(self, importer: str) -> str:
        """The dotted package that OWNS ``importer`` (its directory)."""
        segs = importer.split("/")[:-1]  # drop filename
        # find the deepest root the importer lives under → strip it
        best = ""
        for root in self.roots:
            base = root.split("/") if root else []
            if base == segs[: len(base)] and len(base) >= len(best.split("/") if best else []):
                best = root
        stripped = segs[len(best.split("/")) if best else 0:]
        return ".".join(stripped)

    def _resolve_relative(self, importer: str, raw: str) -> tuple[str, str] | None:
        """``(module_part, base_dotted_package)`` for a relative ``raw``.

        level = number of leading dots; base = importer's package walked
        up ``level-1`` times.
        """
        level = len(raw) - len(raw.lstrip("."))
        module_part = raw[level:]
        pkg = self._package_dir_of(importer)
        base_segs = pkg.split(".") if pkg else []
        # A module file `a/b/foo.py` is in package `a.b`; level=1 → `a.b`.
        up = level - 1
        if up > len(base_segs):
            return None
        base = base_segs[: len(base_segs) - up] if up else base_segs
        base_dotted = ".".join(base)
        return module_part, base_dotted

    # ── edge resolution ───────────────────────────────────────────────

    def resolve_one(self, edge: ImportEdge) -> list[ResolvedEdge]:
        raw = edge.raw_target
        names = edge.names
        if raw.startswith("."):
            return self._resolve_relative_edge(edge)
        # absolute
        head = raw.split(".", 1)[0]
        if head not in self._top_level:
            return [self._edge(edge, None, "package_external", raw)]
        return self._resolve_module_edge(edge, raw, names, "workspace")

    def _resolve_relative_edge(self, edge: ImportEdge) -> list[ResolvedEdge]:
        parsed = self._resolve_relative(edge.src_file, edge.raw_target)
        if parsed is None:
            return [self._edge(edge, None, "unresolved", edge.raw_target)]
        module_part, base_dotted = parsed
        if module_part:
            full = f"{base_dotted}.{module_part}" if base_dotted else module_part
        else:
            full = base_dotted
        return self._resolve_module_edge(
            edge, full, edge.names, "relative",
            empty_module=not module_part,
        )

    def _resolve_module_edge(
        self,
        edge: ImportEdge,
        dotted: str,
        names: tuple[str, ...],
        resolution: str,
        empty_module: bool = False,
    ) -> list[ResolvedEdge]:
        """Resolve ``from <dotted> import <names>`` (or ``import <dotted>``)."""
        # ``from . import x`` (empty module part): each name is a submodule
        # of the base package, or an attribute of its __init__.
        if empty_module:
            return self._split_by_name(edge, dotted, names, resolution, base_is_pkg=True)

        file = self._lookup_module(dotted)
        is_pkg = self._is_package(dotted)

        if file is None and not is_pkg:
            return [self._edge(edge, None, "unresolved", edge.raw_target)]

        # A plain module (not a package): all names live inside it.
        if not is_pkg:
            return [self._edge(edge, file, resolution, edge.raw_target, names)]

        # A package: names may be submodules, re-exports, or attributes.
        return self._split_by_name(edge, dotted, names, resolution, base_is_pkg=True, pkg_file=file)

    def _split_by_name(
        self,
        edge: ImportEdge,
        base_dotted: str,
        names: tuple[str, ...],
        resolution: str,
        *,
        base_is_pkg: bool,
        pkg_file: str | None = None,
    ) -> list[ResolvedEdge]:
        """One ResolvedEdge per distinct target (submodule vs package attr)."""
        pkg_file = pkg_file if pkg_file is not None else self._lookup_module(base_dotted)
        by_target: dict[str | None, list[str]] = defaultdict(list)
        # ``import a.b.c`` (names is the local binding, not import members):
        # treat as importing the module itself.
        member_names = [n for n in names if n and n != "*"]
        if not member_names or not base_is_pkg:
            tgt = pkg_file if base_is_pkg else self._lookup_module(base_dotted)
            return [self._edge(edge, tgt, resolution, edge.raw_target, names)]

        for n in member_names:
            local = n.rsplit(" as ", 1)[-1].strip()
            orig = n.split(" as ", 1)[0].strip()
            sub_dotted = f"{base_dotted}.{orig}" if base_dotted else orig
            sub_file = self._lookup_module(sub_dotted)
            if sub_file is not None:
                by_target[sub_file].append(n)
                continue
            # barrel descent through a package __init__ re-export
            descended = self._descend_barrel(pkg_file, orig, 0)
            by_target[descended if descended is not None else pkg_file].append(n)

        out: list[ResolvedEdge] = []
        for tgt in sorted(by_target, key=lambda t: (t is None, t or "")):
            out.append(self._edge(
                edge, tgt, resolution, edge.raw_target,
                tuple(sorted(by_target[tgt])),
            ))
        return out

    def _descend_barrel(
        self, pkg_file: str | None, name: str, depth: int,
    ) -> str | None:
        if pkg_file is None or depth > _BARREL_DEPTH_CAP:
            return pkg_file
        reexports = self._reexports.get(pkg_file)
        if not reexports or name not in reexports:
            return pkg_file
        raw = reexports[name]  # relative target, e.g. '.sub'
        parsed = self._resolve_relative(pkg_file, raw)
        if parsed is None:
            return pkg_file
        module_part, base_dotted = parsed
        full = (
            f"{base_dotted}.{module_part}" if (base_dotted and module_part)
            else (module_part or base_dotted)
        )
        target = self._lookup_module(full)
        if target is None:
            # maybe the name is a submodule of the re-export target package
            target = self._lookup_module(f"{full}.{name}") if full else None
        if target is None:
            return pkg_file
        if target.endswith("/" + _INIT) or target == _INIT:
            return self._descend_barrel(target, name, depth + 1)
        return target

    def _edge(
        self,
        edge: ImportEdge,
        target: str | None,
        resolution: str,
        raw: str,
        names: tuple[str, ...] | None = None,
    ) -> ResolvedEdge:
        return ResolvedEdge(
            src_file=edge.src_file,
            raw_target=raw,
            target_file=target,
            resolution=resolution,  # type: ignore[arg-type]
            via_barrels=(),
            names=names if names is not None else edge.names,
            kind=edge.kind,
        )


def resolve_edges(
    edges: Sequence[ImportEdge],
    exports_index: Mapping[str, Sequence["ExportEntry"]] | None,
    repo_root: str,
    tracked_files: Iterable[str],
) -> tuple[list[ResolvedEdge], dict[str, int]]:
    """Resolve every import edge (AMENDMENT-2 shape: ``(resolved, telemetry)``)."""
    resolver = PyResolver(repo_root, tracked_files, edges, exports_index)
    resolved: list[ResolvedEdge] = []
    for e in sorted(edges, key=lambda x: (x.src_file, x.line, x.raw_target, x.names)):
        try:
            resolved.extend(resolver.resolve_one(e))
        except Exception:  # noqa: BLE001 — one bad edge never fails the graph
            logger.debug("py_ast: resolve failed for %r", e, exc_info=True)
            resolved.append(ResolvedEdge(
                src_file=e.src_file, raw_target=e.raw_target, target_file=None,
                resolution="unresolved", via_barrels=(), names=e.names, kind=e.kind,
            ))
    resolved.sort(key=lambda r: (
        r.src_file, r.raw_target, r.kind, r.target_file or "", r.names,
    ))
    tele: dict[str, int] = defaultdict(int)
    tele["edges_in"] = len(edges)
    tele["resolved_out"] = len(resolved)
    tele["source_roots"] = len(resolver.roots)
    for r in resolved:
        tele[f"res_{r.resolution}"] += 1
        if r.target_file is not None:
            tele["with_target"] += 1
    return resolved, dict(tele)
