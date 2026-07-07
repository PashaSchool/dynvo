"""W6 M1 — symbol-definition spans for TS/JS (spec: w6ast-spec §1/§3.M1).

Walks a :class:`~faultline.pipeline_v2.ts_ast.parse.FileParse` tree and
emits one :class:`DefSpan` per top-level runtime definition (plus class
methods, ``parent``-tagged). This replaces the regex heuristic
"a symbol ends where the next export begins" with EXACT tree-sitter
node boundaries — the root cause of the wrapper/degenerate flow-span
class (10.4% keyed) and of over-wide over-attribution.

Covered (spec §3.M1):
  * ``function``/``function*`` declarations (overload SIGNATURES skipped
    — only the implementation carries a body);
  * arrow-consts / function-expression consts, incl. ``export const X =
    () =>`` and multi-declarator statements (per-declarator spans);
  * classes (+ ``abstract``) with per-METHOD spans (constructor =
    ``kind="method"``, ``name="constructor"``), arrow class fields as
    methods, decorators included in the span;
  * React components through wrappers — ``forwardRef`` / ``memo`` /
    ``styled`` (tagged templates + ``styled(X)`` calls) / generic
    ``with[A-Z]…`` HOCs — stamped in the ``wrapper`` field;
  * plain components: capitalized callable whose body renders JSX;
  * default-export anonymi (``name="default"``), named inner functions
    preferred (``export default memo(function Board…`` → ``Board``);
  * ``enum`` → ``kind="enum"`` (AMENDMENT-1: a runtime value, kept
    distinct so the M4 legacy mapping keeps enums non-flow-eligible);
  * ``export { a, b as c }`` / ``export default ident`` mark existing
    defs exported (no new span).

NOT emitted (documented, deliberate): ``interface`` / ``type`` aliases
(type-space only — DefSpan kinds are frozen to runtime kinds; the M4
adapter decides how legacy type-symbol consumers degrade), ambient
``declare`` statements, ``namespace`` interiors, CJS assignment defs
(``exports.x = fn`` — regex parity: the legacy path missed them too).

Determinism: single walk, sorted output, no set-iteration into results.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from faultline.pipeline_v2.ts_ast.parse import (
    FileParse,
    cached_payload,
    content_hash_of,
    is_active,
    lang_for_path,
    parse_file,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DefSpan",
    "extract_defs",
    "defs_to_payload",
    "defs_from_payload",
    "extract_defs_cached",
]

#: Payload namespace inside CacheKind.AST (never collides with M2+).
_NAMESPACE = "defs"

_DEF_KINDS = ("function", "class", "component", "method", "const", "enum")
_WRAPPERS = ("none", "forwardRef", "memo", "hoc", "styled")

#: React wrapper callees recognised bare (``memo(…)``) or as the LAST
#: member of a callee chain (``React.memo(…)``). Universal React API
#: names — not repo-specific vocabulary.
_REACT_WRAPPER_PROPS = {"memo": "memo", "forwardRef": "forwardRef"}
#: Universal HOC naming convention (``withAuth``, ``withRouter`` …) —
#: an ecosystem-wide lint-enforced pattern, not a per-repo rule.
_HOC_NAME_RE = re.compile(r"^with[A-Z]")

_FUNCTION_VALUE_TYPES = frozenset({
    "arrow_function",
    "function_expression",
    "function",  # older tree-sitter-javascript anonymous fn node
    "generator_function",
    "generator_function_expression",
})
_CLASS_DECL_TYPES = frozenset({
    "class_declaration",
    "abstract_class_declaration",
})
_CLASS_VALUE_TYPES = frozenset({"class"})
_JSX_NODE_TYPES = frozenset({
    "jsx_element",
    "jsx_self_closing_element",
    "jsx_fragment",
})
#: Transparent TS expression wrappers around a declarator value.
_UNWRAP_TYPES = frozenset({
    "parenthesized_expression",
    "satisfies_expression",
    "as_expression",
    "non_null_expression",
    "type_assertion",
})
#: Class members that carry NO body — never spans.
_CLASS_MEMBER_SKIP = frozenset({
    "abstract_method_signature",
    "method_signature",
    "index_signature",
    "property_signature",
    "class_static_block",
})


@dataclass(frozen=True)
class DefSpan:
    """Frozen spec shape (w6ast-spec §1) — do not extend without the
    coordinator amending the spec."""

    file: str
    name: str
    kind: str        # 'function' | 'class' | 'component' | 'method' | 'const' | 'enum'
    start_line: int  # 1-indexed, inclusive
    end_line: int    # 1-indexed, inclusive
    exported: bool
    wrapper: str     # 'none' | 'forwardRef' | 'memo' | 'hoc' | 'styled'
    parent: str | None


# ── Node helpers ─────────────────────────────────────────────────────────


def _text(node: Any, src: bytes) -> str:
    try:
        return src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _lines(node: Any) -> tuple[int, int]:
    return (node.start_point[0] + 1, node.end_point[0] + 1)


def _has_jsx(node: Any) -> bool:
    """True when the subtree renders JSX (early-exit stack walk)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type in _JSX_NODE_TYPES:
            return True
        stack.extend(cur.children)
    return False


def _unwrap_value(node: Any) -> Any:
    """Strip transparent TS wrappers (``as`` / ``satisfies`` / parens)."""
    cur = node
    while cur is not None and cur.type in _UNWRAP_TYPES:
        nxt = None
        for ch in cur.named_children:
            # The FIRST named child is the wrapped expression; type
            # annotations follow it.
            nxt = ch
            break
        if nxt is None:
            break
        cur = nxt
    return cur


def _is_capitalized(name: str) -> bool:
    return bool(name[:1].isupper())


# ── Wrapper (React) classification ───────────────────────────────────────


def _wrapper_of_call(call: Any, src: bytes, depth: int = 0) -> str | None:
    """``forwardRef`` | ``memo`` | ``styled`` | ``hoc`` | None.

    Resolution walks the callee chain OUTSIDE-IN (``React.memo`` /
    ``styled.div.attrs(…)`` / ``styled(Button)`` / curried calls), then
    falls through to KNOWN React wrappers nested one call deep in the
    arguments (``someWrap(memo(fn))``). Generic ``hoc`` matches only
    the universal ``with[A-Z]…`` callee convention.
    """
    if depth > 3 or call is None or call.type != "call_expression":
        return None
    cur = call.child_by_field_name("function")
    while cur is not None:
        t = cur.type
        if t == "identifier":
            name = _text(cur, src)
            if name in _REACT_WRAPPER_PROPS:
                return _REACT_WRAPPER_PROPS[name]
            if name == "styled":
                return "styled"
            if _HOC_NAME_RE.match(name):
                return "hoc"
            break
        if t == "member_expression":
            prop = cur.child_by_field_name("property")
            prop_name = _text(prop, src) if prop is not None else ""
            if prop_name in _REACT_WRAPPER_PROPS:
                return _REACT_WRAPPER_PROPS[prop_name]
            cur = cur.child_by_field_name("object")
            continue
        if t == "call_expression":
            inner = _wrapper_of_call(cur, src, depth + 1)
            if inner is not None:
                return inner
            break
        break
    args = call.child_by_field_name("arguments")
    if args is not None and args.type == "arguments":
        for a in args.named_children:
            if a.type == "call_expression":
                inner = _wrapper_of_call(a, src, depth + 1)
                if inner is not None and inner != "hoc":
                    return inner
    return None


def _inner_named_function(call: Any, src: bytes, depth: int = 0) -> str | None:
    """Name of the first NAMED function/class inside a call's arguments
    (``memo(function Board() …)`` → ``Board``) — depth-capped."""
    if depth > 3 or call is None:
        return None
    args = call.child_by_field_name("arguments")
    if args is None or args.type != "arguments":
        return None
    for a in args.named_children:
        if a.type in _FUNCTION_VALUE_TYPES or a.type in _CLASS_VALUE_TYPES:
            name_node = a.child_by_field_name("name")
            if name_node is not None:
                return _text(name_node, src)
        elif a.type == "call_expression":
            inner = _inner_named_function(a, src, depth + 1)
            if inner:
                return inner
    return None


# ── Extraction walk ──────────────────────────────────────────────────────


class _Collector:
    def __init__(self, file: str, src: bytes) -> None:
        self.file = file
        self.src = src
        self.rows: list[tuple[str, str, int, int, bool, str, str | None]] = []
        self.exported_names: set[str] = set()
        self._seen: set[tuple[str, str | None, int]] = set()

    def add(
        self,
        name: str,
        kind: str,
        start: int,
        end: int,
        exported: bool,
        wrapper: str = "none",
        parent: str | None = None,
    ) -> None:
        if not name:
            return
        key = (name, parent, start)
        if key in self._seen:
            return
        self._seen.add(key)
        self.rows.append(
            (name, kind, start, max(start, end), exported, wrapper, parent),
        )

    def finish(self) -> list[DefSpan]:
        out: list[DefSpan] = []
        for name, kind, start, end, exported, wrapper, parent in self.rows:
            if (
                not exported
                and parent is None
                and name in self.exported_names
            ):
                exported = True
            out.append(DefSpan(
                file=self.file, name=name, kind=kind,
                start_line=start, end_line=end,
                exported=exported, wrapper=wrapper, parent=parent,
            ))
        out.sort(key=lambda d: (
            d.start_line, d.parent or "", d.name, d.end_line,
        ))
        return out


def _callable_kind(name: str, body_node: Any) -> str:
    """``component`` for a capitalized callable that renders JSX."""
    if _is_capitalized(name) and _has_jsx(body_node):
        return "component"
    return "function"


def _classify_value(
    name: str, value: Any, src: bytes,
) -> tuple[str, str]:
    """(kind, wrapper) for a declarator / default-export VALUE node."""
    value = _unwrap_value(value)
    if value is None:
        return ("const", "none")
    t = value.type
    if t in _FUNCTION_VALUE_TYPES:
        return (_callable_kind(name, value), "none")
    if t in _CLASS_VALUE_TYPES or t in _CLASS_DECL_TYPES:
        return ("class", "none")
    if t == "call_expression":
        wrapper = _wrapper_of_call(value, src)
        if wrapper in ("forwardRef", "memo", "styled"):
            return ("component", wrapper)
        if wrapper == "hoc":
            return (
                ("component", "hoc") if _is_capitalized(name)
                else ("const", "hoc")
            )
        # Unknown factory that visibly renders JSX (component factories
        # outside the with*/React.* conventions).
        if _is_capitalized(name) and _has_jsx(value):
            return ("component", "hoc")
        return ("const", "none")
    return ("const", "none")


def _walk_class_body(
    col: _Collector, class_name: str, class_node: Any,
) -> None:
    body = class_node.child_by_field_name("body")
    if body is None or body.type != "class_body":
        return
    for member in body.named_children:
        t = member.type
        if t in _CLASS_MEMBER_SKIP:
            continue
        if t == "method_definition":
            name_node = member.child_by_field_name("name")
            if name_node is None or name_node.type == "computed_property_name":
                continue  # `[Symbol.iterator]()` — no stable member name
            name = _text(name_node, col.src)
            if not name:
                continue
            start, end = _lines(member)
            col.add(name, "method", start, end, False, "none", class_name)
        elif t in ("public_field_definition", "field_definition"):
            # TS names the field "name"; JS names it "property".
            name_node = (
                member.child_by_field_name("name")
                or member.child_by_field_name("property")
            )
            value = member.child_by_field_name("value")
            if name_node is None or value is None:
                continue
            if name_node.type == "computed_property_name":
                continue
            if _unwrap_value(value).type not in _FUNCTION_VALUE_TYPES:
                continue  # data fields are not callable members
            name = _text(name_node, col.src)
            start, end = _lines(member)
            col.add(name, "method", start, end, False, "none", class_name)


def _handle_declaration(
    col: _Collector, decl: Any, exported: bool, span_node: Any,
) -> None:
    """One top-level declaration node (possibly export-wrapped)."""
    t = decl.type
    src = col.src
    if t in ("function_declaration", "generator_function_declaration"):
        name_node = decl.child_by_field_name("name")
        name = _text(name_node, src) if name_node is not None else ""
        if not name:
            return
        start, end = _lines(span_node)
        col.add(name, _callable_kind(name, decl), start, end, exported)
        return
    if t in _CLASS_DECL_TYPES:
        name_node = decl.child_by_field_name("name")
        name = _text(name_node, src) if name_node is not None else ""
        if not name:
            return
        start, end = _lines(span_node)
        col.add(name, "class", start, end, exported)
        _walk_class_body(col, name, decl)
        return
    if t in ("lexical_declaration", "variable_declaration"):
        declarators = [
            ch for ch in decl.named_children
            if ch.type == "variable_declarator"
        ]
        single = len(declarators) == 1
        for d in declarators:
            name_node = d.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                continue  # destructuring patterns carry no single def
            value = d.child_by_field_name("value")
            if value is None:
                continue  # bare `let x;` — no body, no span
            name = _text(name_node, src)
            kind, wrapper = _classify_value(name, value, src)
            # Single-declarator: span the whole statement (covers the
            # `export const` prefix + decorators); multi: per-declarator.
            start, end = _lines(span_node if single else d)
            col.add(name, kind, start, end, exported, wrapper)
            unwrapped = _unwrap_value(value)
            if unwrapped is not None and unwrapped.type in _CLASS_VALUE_TYPES:
                _walk_class_body(col, name, unwrapped)
        return
    if t == "enum_declaration":
        name_node = decl.child_by_field_name("name")
        name = _text(name_node, src) if name_node is not None else ""
        if name:
            start, end = _lines(span_node)
            # AMENDMENT-1: honest kind='enum' — a runtime value, but NOT
            # flow-eligible; the legacy SymbolRange mapping is M4's table.
            col.add(name, "enum", start, end, exported)
        return
    # interface / type_alias / ambient / signatures / imports → no spans.


def _handle_default_value(col: _Collector, value: Any, span_node: Any) -> None:
    """``export default <expression>`` — anonymi become ``default``."""
    src = col.src
    value = _unwrap_value(value)
    if value is None:
        return
    t = value.type
    start, end = _lines(span_node)
    if t == "identifier":
        # `export default foo` — foo's own def (elsewhere) turns exported.
        col.exported_names.add(_text(value, src))
        return
    if t in _FUNCTION_VALUE_TYPES:
        name_node = value.child_by_field_name("name")
        name = _text(name_node, src) if name_node is not None else "default"
        kind = (
            "component" if _has_jsx(value) and (
                name == "default" or _is_capitalized(name)
            ) else "function"
        )
        col.add(name, kind, start, end, True)
        return
    if t in _CLASS_VALUE_TYPES or t in _CLASS_DECL_TYPES:
        name_node = value.child_by_field_name("name")
        name = _text(name_node, src) if name_node is not None else "default"
        col.add(name, "class", start, end, True)
        _walk_class_body(col, name, value)
        return
    if t == "call_expression":
        inner = _inner_named_function(value, src)
        name = inner or "default"
        kind, wrapper = _classify_value(name, value, src)
        if kind == "const" and wrapper == "hoc":
            # `export default withX(Thing)` — a default-exported HOC wrap
            # IS the component (the capitalization gate can't see the
            # anonymous name).
            kind = "component"
        elif kind == "const" and _has_jsx(value):
            kind = "component"  # anonymous wrapped component factories
        col.add(name, kind, start, end, True, wrapper)
        return
    # Any other value (`export default {…}` / literal) — a real runtime
    # export whose span is the statement.
    col.add("default", "const", start, end, True)


def _clause_is_type_only(node: Any, src: bytes) -> bool:
    """``export type { … }`` — type-space only, never marks value defs."""
    for ch in node.children:
        if ch.type == "type":
            return True
        if ch.type == "export_clause":
            break
    return False


def _handle_export_statement(col: _Collector, node: Any) -> None:
    src = col.src
    if node.child_by_field_name("source") is not None:
        return  # re-export (`export … from`) — M2's edge, no local def
    decl = node.child_by_field_name("declaration")
    if decl is not None:
        _handle_declaration(col, decl, True, node)
        return
    value = node.child_by_field_name("value")
    if value is not None:
        _handle_default_value(col, value, node)
        return
    if _clause_is_type_only(node, src):
        return
    for ch in node.named_children:
        if ch.type != "export_clause":
            continue
        for spec in ch.named_children:
            if spec.type != "export_specifier":
                continue
            # Inline type specifier (`export { type Foo, bar }`).
            if any(c.type == "type" for c in spec.children):
                continue
            name_node = spec.child_by_field_name("name")
            if name_node is not None:
                col.exported_names.add(_text(name_node, src))


def extract_defs(fp: FileParse, source: bytes) -> list[DefSpan]:
    """All top-level runtime definitions of one parsed file, sorted."""
    col = _Collector(fp.path, source)
    root = fp.tree.root_node
    for node in root.named_children:
        t = node.type
        if t == "export_statement":
            _handle_export_statement(col, node)
        else:
            _handle_declaration(col, node, False, node)
    return col.finish()


# ── Serialisation (CacheKind.AST payload) ────────────────────────────────


def defs_to_payload(defs: list[DefSpan]) -> list[dict[str, Any]]:
    """JSON-able rows, sorted + file-independent (content-addressed)."""
    return [
        {
            "name": d.name,
            "kind": d.kind,
            "start_line": d.start_line,
            "end_line": d.end_line,
            "exported": d.exported,
            "wrapper": d.wrapper,
            "parent": d.parent,
        }
        for d in sorted(defs, key=lambda d: (
            d.start_line, d.parent or "", d.name, d.end_line,
        ))
    ]


def defs_from_payload(rows: list[Any], file: str) -> list[DefSpan]:
    """Rehydrate cached rows; malformed rows are dropped defensively."""
    out: list[DefSpan] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            kind = str(row["kind"])
            wrapper = str(row.get("wrapper") or "none")
            if kind not in _DEF_KINDS or wrapper not in _WRAPPERS:
                continue
            parent = row.get("parent")
            out.append(DefSpan(
                file=file,
                name=str(row["name"]),
                kind=kind,
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                exported=bool(row["exported"]),
                wrapper=wrapper,
                parent=str(parent) if parent is not None else None,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda d: (d.start_line, d.parent or "", d.name, d.end_line))
    return out


def extract_defs_cached(
    path: str,
    source: bytes,
    backend: Any | None = None,
) -> list[DefSpan] | None:
    """Parse + extract + cache in one call — the M4 adapter entrypoint.

    ``None`` = this file must take the regex path (layer inactive,
    non-TS/JS path, or parse failure). Never raises.
    """
    lang = lang_for_path(path)
    if lang is None or not is_active():
        return None

    def _compute() -> dict[str, Any] | None:
        fp = parse_file(path, source)
        if fp is None:
            return None
        try:
            return {"defs": defs_to_payload(extract_defs(fp, source))}
        except Exception:  # noqa: BLE001 — walker fault → regex fallback
            logger.debug("ts_ast: defs walk failed for %s", path,
                         exc_info=True)
            return None

    payload = cached_payload(
        backend, _NAMESPACE, lang, content_hash_of(source), _compute,
    )
    if payload is None:
        return None
    rows = payload.get("defs")
    if not isinstance(rows, list):
        return None
    return defs_from_payload(rows, path)
