"""Track-B py_ast M2 — Python import/re-export edges (raw, pre-resolution).

The Python mirror of ``ts_ast.imports`` (w6ast-spec §3.M2). Walks the
whole ``ast.Module`` (top-level AND nested — a function-local or
``TYPE_CHECKING``-guarded import is still a real dependency for
provenance) and emits one canonical :class:`ImportEdge` per import
form, plus :class:`ExportEntry` rows for package ``__init__`` files
(the Python "barrel").

Import-form → edge mapping (``raw_target`` is the DOTTED module path;
relative imports keep their leading dots so the resolver honours PEP-328
levels):

    import a                      → named   names=('a',)         raw='a'
    import a.b.c                  → named   names=('a',)         raw='a.b.c'
    import a.b as x               → named   names=('x',)         raw='a.b'
    import a, b                   → two named edges (one per alias)
    from m import x               → named   names=('x',)         raw='m'
    from m import x as y          → named   names=('x as y',)    raw='m'
    from m import x, y            → named   names=('x','y')      raw='m'
    from m import *               → reexport_star names=('*',)   raw='m'
    from . import x               → named   names=('x',)         raw='.'
    from .mod import y            → named   names=('y',)         raw='.mod'
    from ..pkg import z           → named   names=('z',)         raw='..pkg'

``names`` carries the LOCAL binding, renames kept as ``'orig as local'``
(mirrors ts_ast so the provenance view's ``_local_side`` split works
unchanged). Names within a ``from`` edge are sorted (determinism).

ExportEntry (package ``__init__`` re-exports only — the barrel surface
the resolver descends): a ``from .sub import Name`` inside an
``__init__.py`` re-publishes ``Name`` as ``package.Name``, so it yields
``ExportEntry(file=__init__, name=Name, kind='named')``; a
``from .sub import *`` yields ``ExportEntry(name='*', kind='star_from')``.
``origin_file`` is left ``None`` here — M3 fills the resolved source
(post-resolution view, spec §3.M5).
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from faultline.pipeline_v2.py_ast.shapes import ExportEntry, ImportEdge

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.pipeline_v2.py_ast.parse import FileParse

__all__ = ["extract_imports", "imports_payload", "is_package_init"]


def is_package_init(path: str) -> bool:
    """True for a package initialiser (``.../__init__.py`` or root ``__init__.py``)."""
    p = path.replace("\\", "/")
    return p == "__init__.py" or p.endswith("/__init__.py")


def _alias_local(alias: ast.alias) -> str:
    """LOCAL binding name of an ``import`` alias.

    ``import a.b.c`` binds the TOP component ``a``; ``import a.b as x``
    binds ``x``. (For ``from`` imports the caller formats renames as
    ``'orig as local'`` directly.)
    """
    if alias.asname:
        return alias.asname
    return alias.name.split(".", 1)[0]


def extract_imports(
    fp: "FileParse",
) -> tuple[list[ImportEdge], list[ExportEntry]]:
    """All import edges + package re-export entries of one parsed file."""
    file = fp.path.replace("\\", "/")
    in_init = is_package_init(file)
    edges: list[ImportEdge] = []
    exports: list[ExportEntry] = []

    for node in ast.walk(fp.tree):
        line = int(getattr(node, "lineno", 1) or 1)

        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(ImportEdge(
                    src_file=file,
                    kind="named",
                    names=(_alias_local(alias),),
                    raw_target=alias.name,
                    line=line,
                ))

        elif isinstance(node, ast.ImportFrom):
            level = int(node.level or 0)
            module = node.module or ""
            raw_target = ("." * level) + module
            # Star import: pulls all public names into this namespace.
            star = any(a.name == "*" for a in node.names)
            if star:
                edges.append(ImportEdge(
                    src_file=file,
                    kind="reexport_star",
                    names=("*",),
                    raw_target=raw_target,
                    line=line,
                ))
                if in_init:
                    exports.append(ExportEntry(
                        file=file, name="*", kind="star_from",
                        origin_file=None,
                    ))
                continue
            names: list[str] = []
            for a in node.names:
                local = f"{a.name} as {a.asname}" if a.asname else a.name
                names.append(local)
                if in_init:
                    # The name re-published on the package namespace is the
                    # LOCAL binding (asname when renamed, else the name).
                    published = a.asname or a.name
                    exports.append(ExportEntry(
                        file=file, name=published, kind="named",
                        origin_file=None,
                    ))
            edges.append(ImportEdge(
                src_file=file,
                kind="named",
                names=tuple(sorted(names)),
                raw_target=raw_target,
                line=line,
            ))

    edges.sort(key=lambda e: (e.src_file, e.line, e.kind, e.raw_target, e.names))
    exports.sort(key=lambda x: (x.name, x.kind, x.origin_file or ""))
    return edges, exports


def imports_payload(fp: "FileParse") -> dict[str, list[dict]]:
    """JSON-able cache payload for the ``py-imports`` walker namespace."""
    edges, exports = extract_imports(fp)
    return {
        "edges": [e.to_payload() for e in edges],
        "exports": [x.to_payload() for x in exports],
    }
