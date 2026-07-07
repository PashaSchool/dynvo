"""W6-AST — true tree-sitter AST layer for TS/JS (spec: w6ast-spec v1).

Package layout (module ownership per spec §3 — zero file overlap):

* ``parse.py``   (M1) — shared parse util + cache
* ``defs.py``    (M1) — symbol definitions with line ranges
* ``imports.py`` (M2) — import/export/re-export edges (raw)
* ``resolve.py`` (M3) — edge resolution to files (aliases/workspaces/barrels)
* ``shapes.py``  (M4) — the FROZEN data shapes of spec §1 (canonical module)
* ``adapter.py`` (M4) — bridge into existing consumers

The regex path (``faultline.analyzer.ast_extractor``) is NEVER removed —
it is the forever-fallback. Master flag ``FAULTLINE_TS_AST`` (default ON;
``=0`` → consumers take exactly the legacy regex path, byte-identical).
"""

from faultline.pipeline_v2.ts_ast.shapes import (
    DefSpan,
    ExportEntry,
    FileParse,
    ImportEdge,
    ResolvedEdge,
    SymbolGraph,
)

__all__ = [
    "DefSpan",
    "ExportEntry",
    "FileParse",
    "ImportEdge",
    "ResolvedEdge",
    "SymbolGraph",
]
