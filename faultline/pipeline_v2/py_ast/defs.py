"""Track-B py_ast M1 — Python symbol definitions with exact line ranges.

The Python mirror of ``ts_ast.defs`` (w6ast-spec §3.M1). Walks the
``ast.Module`` of a :class:`~faultline.pipeline_v2.py_ast.parse.FileParse`
and emits one canonical :class:`DefSpan` per definition:

* module-level ``def`` / ``async def``        → kind ``function``, exported
* module-level ``class``                      → kind ``class``, exported
* ``def`` inside a ``class`` body             → kind ``method``
  (``__init__`` stays ``method`` — parity with the legacy Python path,
  which tags it ``constructor``; the shared DefSpan enum has no
  ``constructor`` so the adapter re-derives that legacy kind by name),
  ``parent`` = enclosing class, exported=False
* ``def`` nested inside a function            → kind ``function``,
  exported=False (a closure/local — reachable as a same-file callee)
* module-level name binding (``X = ...`` /
  ``x: T = ...`` / ``a = b = ...`` / ``a, b = …``) → kind ``const``,
  exported (the module's public data surface: routers, url lists,
  registries — the Django ``urlpatterns``/``router`` idiom)

``start_line`` spans the WHOLE statement including decorator lines and
the ``async`` keyword (``ast`` reports ``node.lineno`` at the ``def`` /
``class`` keyword, so a decorated symbol's span is widened to the first
decorator). ``end_line`` = ``node.end_lineno``. Both are 1-based
inclusive — the exact boundaries the T1 call graph slices for callee
identifiers (a too-wide range over-attributes, a too-narrow one drops
real callees), which is why ``ast`` beats the regex heuristic.

Determinism: output is sorted by the canonical DefSpan key; no
set-iteration reaches the result.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from faultline.pipeline_v2.py_ast.shapes import DefSpan

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.pipeline_v2.py_ast.parse import FileParse

__all__ = ["extract_defs", "defs_payload"]


def _stmt_start(node: ast.AST) -> int:
    """First source line of a def/class STATEMENT, decorators included.

    ``ast`` puts ``lineno`` on the ``def``/``class`` keyword; decorators
    precede it, so the statement truly starts at the topmost decorator.
    """
    line = int(getattr(node, "lineno", 1) or 1)
    decos = getattr(node, "decorator_list", None) or ()
    for d in decos:
        dl = getattr(d, "lineno", None)
        if dl is not None:
            line = min(line, int(dl))
    return max(1, line)


def _end_line(node: ast.AST, start: int) -> int:
    end = getattr(node, "end_lineno", None)
    return max(start, int(end)) if end is not None else start


def _const_names(node: ast.AST) -> list[str]:
    """Module-level BINDING names for an assignment statement.

    Handles ``x = ...``, ``x: T = ...`` (AnnAssign), chained ``a = b =
    ...``, and tuple/list unpacking ``a, b = ...``. Only bare ``Name``
    targets are surfaced — ``obj.attr = …`` / ``d[k] = …`` bind no
    module symbol. Order follows source (left-to-right) and is de-duped
    stably by the caller's sort.
    """
    names: list[str] = []

    def _from_target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for elt in t.elts:
                _from_target(elt)
        # Starred (``a, *rest = …``) binds ``rest`` as a Name via .value
        elif isinstance(t, ast.Starred):
            _from_target(t.value)

    if isinstance(node, ast.Assign):
        for tgt in node.targets:
            _from_target(tgt)
    elif isinstance(node, ast.AnnAssign):
        # Only annotated assignments WITH a value bind a runtime name;
        # a bare ``x: int`` declaration binds nothing.
        if node.value is not None:
            _from_target(node.target)
    return names


def extract_defs(fp: "FileParse") -> list[DefSpan]:
    """All :class:`DefSpan`s of one parsed Python file (sorted, canonical)."""
    file = fp.path.replace("\\", "/")
    tree = fp.tree
    out: list[DefSpan] = []

    def _emit(
        name: str,
        node: ast.AST,
        kind: str,
        *,
        exported: bool,
        parent: str | None,
    ) -> None:
        start = _stmt_start(node)
        out.append(DefSpan(
            file=file,
            name=name,
            kind=kind,  # type: ignore[arg-type]  # Python subset of DefKind
            start_line=start,
            end_line=_end_line(node, start),
            exported=exported,
            wrapper="none",
            parent=parent,
        ))

    def _walk(node: ast.AST, parent_class: str | None, top: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                _emit(
                    child.name, child, "class",
                    exported=top, parent=None,
                )
                # Descend into the class body: its ``def``s become methods.
                _walk(child, child.name, top=False)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if parent_class is not None:
                    _emit(
                        child.name, child, "method",
                        exported=False, parent=parent_class,
                    )
                else:
                    _emit(
                        child.name, child, "function",
                        exported=top, parent=None,
                    )
                # Recurse into the body: nested defs are local functions,
                # nested classes get their own parent (kind='class').
                _walk(child, None, top=False)
            elif top and isinstance(child, (ast.Assign, ast.AnnAssign)):
                # Module-level data bindings only (top-level statements).
                for cname in _const_names(child):
                    _emit(
                        cname, child, "const",
                        exported=True, parent=None,
                    )
            else:
                # Recurse into every other node preserving scope (mirrors
                # the legacy ``_parse_python_file`` deep walk). Module-level
                # block statements (If / Try / ExceptHandler / With / For /
                # Match) keep ``top`` so a conditionally-defined name
                # (``try: from x import V / except: class V``) still counts
                # as module-level; inside a function/class body the flags
                # carry down unchanged (function bodies are handled above
                # with top=False, so their nested defs stay non-exported).
                _walk(child, parent_class, top=top)

    _walk(tree, None, top=True)
    out.sort(key=lambda d: (d.file, d.start_line, d.end_line, d.parent or "", d.name))
    return out


def defs_payload(fp: "FileParse") -> dict[str, list[dict]]:
    """JSON-able cache payload for the ``py-defs`` walker namespace."""
    return {"defs": [d.to_payload() for d in extract_defs(fp)]}
