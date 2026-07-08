"""Track-B py_ast data shapes — the SAME canonical shapes as ts_ast.

py_ast is the Python mirror of the W6 ts_ast layer (w6ast-spec §1). To
guarantee Track A consumes Python provenance through the *identical*
shape it already consumes for TS/JS, py_ast does NOT redefine
``DefSpan`` / ``ImportEdge`` / ``ResolvedEdge`` / ``ExportEntry`` /
``SymbolGraph`` — it re-exports the frozen canonical dataclasses from
``faultline.pipeline_v2.ts_ast.shapes``. One type, one ``to_payload``
contract, one sort discipline for both languages.

The only Python-specific shape is :class:`FileParse` (it holds a live
``ast.Module`` instead of a tree-sitter tree); it lives in
``py_ast.parse`` next to the parser that produces it, mirroring ts_ast.

Python semantics carried on the shared shapes (the truth py_ast encodes):

* ``DefSpan.kind`` uses only ``function`` | ``class`` | ``method`` |
  ``const`` (Python has no React ``component`` and no ``enum`` form we
  model; ``const`` is a MODULE-LEVEL name binding — ``UPPER = ...`` /
  a simple assignment target). ``wrapper`` is always ``"none"`` (the
  React-wrapper channel is inert for Python).
* ``DefSpan`` spans the WHOLE statement including any decorator lines
  and (for ``async def``) the ``async`` keyword — ``start_line`` is the
  first decorator's line when decorated, else the ``def``/``class`` line.
* ``exported`` mirrors the legacy Python contract (analyzer.ast_extractor
  ``_parse_python_file``): MODULE-LEVEL ``def`` / ``async def`` /
  ``class`` are exported; methods and nested defs are ``exported=False``.
  A module-level ``const`` is ``exported`` when the name is not
  dunder-private-only noise (kept parallel to the def rule).
* ``ImportEdge.kind`` maps Python import forms to the shared vocabulary:
  ``import a`` / ``import a.b`` / ``import a as b`` → ``named`` (the
  bound name is the edge's ``names``); ``from m import x, y`` → ``named``;
  ``from m import *`` → ``reexport_star`` when the file is a package
  ``__init__`` re-exporter, else ``side_effect`` semantics with
  ``names=('*',)``; ``from m import x`` inside an ``__init__.py`` that
  also lists ``x`` in ``__all__`` is additionally an ExportEntry
  (re-export). ``raw_target`` is the DOTTED module path, with leading
  dots for relative imports (``.mod`` / ``..pkg.mod``) preserved so the
  resolver can honour PEP-328 relative levels.
"""

from __future__ import annotations

from faultline.pipeline_v2.ts_ast.shapes import (
    DefKind,
    DefSpan,
    ExportEntry,
    ExportKind,
    ImportEdge,
    ImportKind,
    Resolution,
    ResolvedEdge,
    SymbolGraph,
)

__all__ = [
    "DefKind",
    "DefSpan",
    "ExportEntry",
    "ExportKind",
    "ImportEdge",
    "ImportKind",
    "Resolution",
    "ResolvedEdge",
    "SymbolGraph",
]
