"""Track-B py_ast — true stdlib-``ast`` import-graph layer for Python.

The Python mirror of the W6 ts_ast package (same architecture + shapes,
w6ast-spec). Module ownership (zero file overlap):

* ``parse.py``   (M1) — stdlib ``ast`` parse util + cache
* ``defs.py``    (M1) — symbol definitions with exact line ranges
* ``imports.py`` (M2) — import / re-export edges (raw)
* ``resolve.py`` (M3) — edge resolution to files (relative / packages /
                        PEP-420 namespace / src-layout / workspace roots)
* ``shapes.py``  — re-exports the FROZEN canonical shapes from ts_ast
* ``adapter.py`` (M4) — bridge into consumers (Track A provenance)

The legacy regex/stdlib Python path (``faultline.analyzer.ast_extractor``
``_parse_python_file``) is NEVER removed — it is the forever-fallback.
Master flag ``FAULTLINE_PY_AST`` (default ON; ``=0`` → consumers take the
legacy path, byte-identical kill-switch).
"""

from faultline.pipeline_v2.py_ast.shapes import (
    DefSpan,
    ExportEntry,
    ImportEdge,
    ResolvedEdge,
    SymbolGraph,
)

__all__ = [
    "DefSpan",
    "ExportEntry",
    "ImportEdge",
    "ResolvedEdge",
    "SymbolGraph",
]
