"""W6-AST M2 — import/export/re-export edge extraction for TS/JS (tree-sitter).

Extracts the *raw* module graph edges of a single parsed file:

* ``ImportEdge`` — every construct that pulls another module in:
  ``import`` statements (named / default / namespace / side-effect),
  dynamic ``import()``, CommonJS ``require()`` (``.js`` / ``.jsx`` only,
  plus the explicit TS ``import x = require('...')`` form), and re-export
  statements (``export * from`` / ``export {x} from``).
* ``ExportEntry`` — the per-file export index consumed by M3 barrel
  descent: one entry per name this file exposes, with ``origin_file``
  set to the *raw* (unresolved) target for re-exports.

Resolution of ``raw_target`` → file paths is M3's job (``resolve.py``);
this module never touches the filesystem.

``names`` semantics on ``ImportEdge`` (frozen contract for M3/M4):

* ``named`` / ``reexport_named`` — the *original* names in the target
  module (i.e. left-hand side of ``as``), so barrel descent can look
  them up in the target's export index.
* ``default`` — the local binding (the kind already says the target-side
  name is ``default``).
* ``namespace`` — the local binding (``ns`` in ``import * as ns``).
* ``reexport_star`` — ``()`` for ``export * from``; the exposed name for
  ``export * as ns from``.
* ``require`` — object-pattern keys (original CommonJS member names) for
  ``const {a, b: c} = require(...)``; the local binding for
  ``const x = require(...)`` (whole-module, namespace-like); ``()``
  otherwise (bare call / non-declarator context).
* ``dynamic`` / ``side_effect`` — always ``()``.

Type-only imports/exports (``import type``, ``export type``, inline
``{ type X }`` specifiers) keep their names prefixed with ``type:`` —
M3 resolution SKIPS those names for provenance (spec §3.M2).

``ExportEntry.name`` is the *exposed* name (right-hand side of ``as``);
``export {x as default}`` and ``export default`` both yield
``name='default', kind='default'``.  ``export * from './m'`` yields
``name='*', kind='star_from'``; ``export * as ns from './m'`` yields
``name='ns', kind='star_from'`` (descend into ``origin_file`` either way).
Type-only declarations (``interface``, ``type`` aliases, type-only
specifiers) are indexed with the same ``type:`` prefix.

Determinism laws (spec §2): every returned list is sorted on a total
canonical key and deduplicated via ``dict.fromkeys`` on the sorted
list — no set iteration anywhere near the output.

Dynamic ``import()`` / ``require()`` with a non-literal argument
(template substitution, variable) has no static target and is skipped.

The module deliberately imports tree-sitter only under
``TYPE_CHECKING``: callers hand in an already-parsed tree (M1's
``parse.py`` once it lands; tests use a private helper), so importing
this module stays dependency-free at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from faultline.pipeline_v2.ts_ast.shapes import ExportEntry, ImportEdge

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from tree_sitter import Node, Tree

# Languages accepted by extract_imports (mirrors FileParse.lang, spec §1).
_LANGS = ("ts", "tsx", "js", "jsx")

# CommonJS bare require() calls are extracted only for these languages
# (spec §3.M2: "require() у .js/.jsx").  The explicit TS statement form
# `import x = require('...')` is an import *statement* and is always kept.
_REQUIRE_LANGS = ("js", "jsx")

# Prefix carried by type-only names; M3 skips these for provenance.
TYPE_NAME_PREFIX = "type:"

# ImportEdge / ExportEntry are imported from shapes.py — M4's canonical single
# source for the frozen §1 shapes (coordinator ast-chain canonicalisation;
# semantics frozen, fields/order identical to the former local copies).


def extract_imports(
    path: str, lang: str, tree: "Tree", text: bytes
) -> tuple[list[ImportEdge], list[ExportEntry]]:
    """Extract all import edges + the export index from one parsed file.

    Args:
        path: repo-relative file path; stamped into every edge/entry.
        lang: one of ``ts`` / ``tsx`` / ``js`` / ``jsx`` (FileParse.lang).
        tree: tree-sitter tree parsed from ``text`` (M1 parse util or any
            equivalent parser output for the matching grammar).
        text: exact source bytes the tree was parsed from.

    Returns:
        ``(edges, exports)`` — both sorted on a canonical key and
        deduplicated; safe to diff/serialize.
    """
    if lang not in _LANGS:
        raise ValueError(f"unsupported lang {lang!r}; expected one of {_LANGS}")

    edges: list[ImportEdge] = []
    exports: list[ExportEntry] = []

    for node in _iter_nodes(tree.root_node):
        if node.type == "import_statement":
            edges.extend(_import_statement_edges(path, node, text))
        elif node.type == "export_statement":
            if _inside_namespace(node):
                # `export const q` inside `export namespace N {}` is N.q,
                # not a file-level export; the namespace itself is indexed.
                continue
            stmt_edges, stmt_exports = _export_statement_parts(path, node, text)
            edges.extend(stmt_edges)
            exports.extend(stmt_exports)
        elif node.type == "call_expression":
            call_edge = _call_edge(path, lang, node, text)
            if call_edge is not None:
                edges.append(call_edge)

    edges_sorted = sorted(
        edges, key=lambda e: (e.line, e.kind, e.raw_target, e.names)
    )
    exports_sorted = sorted(
        exports, key=lambda x: (x.name, x.kind, x.origin_file or "")
    )
    # Deduplicate exact duplicates only; dict preserves the sorted order.
    return list(dict.fromkeys(edges_sorted)), list(dict.fromkeys(exports_sorted))


# ---------------------------------------------------------------------------
# tree walking + small node utilities
# ---------------------------------------------------------------------------


def _iter_nodes(root: "Node") -> Iterator["Node"]:
    """Yield every node depth-first in document order (iterative, no recursion)."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _node_text(text: bytes, node: "Node") -> str:
    return text[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line(node: "Node") -> int:
    return node.start_point[0] + 1  # tree-sitter rows are 0-based


def _anon_types(node: "Node") -> list[str]:
    """Types of anonymous (keyword/punctuation) children, in order."""
    return [c.type for c in node.children if not c.is_named]


def _static_string(text: bytes, node: "Node | None") -> str | None:
    """Literal value of a ``string`` / substitution-free ``template_string``.

    Returns None when the node is missing or has no static value
    (e.g. a template literal with ``${...}`` substitutions).
    """
    if node is None:
        return None
    if node.type == "string":
        return "".join(
            _node_text(text, c)
            for c in node.named_children
            if c.type in ("string_fragment", "escape_sequence")
        )
    if node.type == "template_string":
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        return "".join(
            _node_text(text, c)
            for c in node.named_children
            if c.type in ("string_fragment", "escape_sequence")
        )
    return None


def _specifier_name(text: bytes, node: "Node") -> str:
    """Name of an import/export specifier part; unquotes string names.

    Handles the ES2022 arbitrary-namespace form ``import {"x y" as z}``.
    """
    if node.type in ("string", "template_string"):
        return _static_string(text, node) or ""
    return _node_text(text, node)


def _has_type_keyword(node: "Node") -> bool:
    """True when the node carries an anonymous ``type`` keyword child."""
    return any(c.type == "type" and not c.is_named for c in node.children)


def _type_prefixed(name: str, is_type: bool) -> str:
    return TYPE_NAME_PREFIX + name if is_type else name


# ---------------------------------------------------------------------------
# import statements
# ---------------------------------------------------------------------------


def _import_statement_edges(
    path: str, stmt: "Node", text: bytes
) -> list[ImportEdge]:
    line = _line(stmt)

    # TS interop form: `import x = require('./y')` → kind='require'.
    require_clause = _first_named_child(stmt, "import_require_clause")
    if require_clause is not None:
        target = _static_string(text, require_clause.child_by_field_name("source"))
        if target is None:
            return []
        binding = _first_named_child(require_clause, "identifier")
        names: tuple[str, ...] = (
            (_node_text(text, binding),) if binding is not None else ()
        )
        return [ImportEdge(path, "require", names, target, line)]

    target = _static_string(text, stmt.child_by_field_name("source"))
    if target is None:
        return []

    clause = _first_named_child(stmt, "import_clause")
    if clause is None:
        return [ImportEdge(path, "side_effect", (), target, line)]

    stmt_is_type = _has_type_keyword(stmt)  # `import type ... from`
    edges: list[ImportEdge] = []
    for part in clause.named_children:
        if part.type == "identifier":  # default import binding
            name = _type_prefixed(_node_text(text, part), stmt_is_type)
            edges.append(ImportEdge(path, "default", (name,), target, line))
        elif part.type == "namespace_import":
            ident = _first_named_child(part, "identifier")
            if ident is None:
                continue
            name = _type_prefixed(_node_text(text, ident), stmt_is_type)
            edges.append(ImportEdge(path, "namespace", (name,), target, line))
        elif part.type == "named_imports":
            names = _named_import_names(part, text, stmt_is_type)
            edges.append(ImportEdge(path, "named", names, target, line))
    return edges


def _named_import_names(
    named_imports: "Node", text: bytes, stmt_is_type: bool
) -> tuple[str, ...]:
    """Original (target-side) names of ``{a, b as c, type T}`` — sorted."""
    names: list[str] = []
    for spec in named_imports.named_children:
        if spec.type != "import_specifier":  # skips comments inside the braces
            continue
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            continue
        is_type = stmt_is_type or _has_type_keyword(spec)
        names.append(_type_prefixed(_specifier_name(text, name_node), is_type))
    return tuple(sorted(names))


# ---------------------------------------------------------------------------
# export statements
# ---------------------------------------------------------------------------


def _export_statement_parts(
    path: str, stmt: "Node", text: bytes
) -> tuple[list[ImportEdge], list[ExportEntry]]:
    line = _line(stmt)
    anon = _anon_types(stmt)
    if "=" in anon:  # legacy `export = X` interop — out of scope
        return [], []

    target = _static_string(text, stmt.child_by_field_name("source"))
    stmt_is_type = "type" in anon  # `export type {...}`

    # --- star re-exports -----------------------------------------------
    # NB: for `export * as ns from` the `*` token is a child of the
    # namespace_export node, not of the statement itself.
    namespace_export = _first_named_child(stmt, "namespace_export")
    if target is not None and (namespace_export is not None or "*" in anon):
        if namespace_export is not None:  # export * as ns from './m'
            ident = _first_named_child(namespace_export, "identifier")
            exposed = _node_text(text, ident) if ident is not None else "*"
            exposed = _type_prefixed(exposed, stmt_is_type)
            return (
                [ImportEdge(path, "reexport_star", (exposed,), target, line)],
                [ExportEntry(path, exposed, "star_from", target)],
            )
        return (  # export * from './m'
            [ImportEdge(path, "reexport_star", (), target, line)],
            [ExportEntry(path, "*", "star_from", target)],
        )

    # --- clause exports: export {a, b as c} [from './m'] ----------------
    clause = _first_named_child(stmt, "export_clause")
    if clause is not None:
        return _export_clause_parts(path, clause, text, target, stmt_is_type, line)

    # --- export default --------------------------------------------------
    if "default" in anon:
        return [], [ExportEntry(path, "default", "default", None)]

    # --- export <declaration> --------------------------------------------
    declaration = stmt.child_by_field_name("declaration")
    if declaration is not None:
        entries = [
            ExportEntry(path, name, "named", None)
            for name in _declaration_names(declaration, text)
        ]
        return [], entries

    return [], []


def _export_clause_parts(
    path: str,
    clause: "Node",
    text: bytes,
    target: str | None,
    stmt_is_type: bool,
    line: int,
) -> tuple[list[ImportEdge], list[ExportEntry]]:
    original_names: list[str] = []
    entries: list[ExportEntry] = []
    for spec in clause.named_children:
        if spec.type != "export_specifier":  # skips comments inside the braces
            continue
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            continue
        is_type = stmt_is_type or _has_type_keyword(spec)
        original = _type_prefixed(_specifier_name(text, name_node), is_type)
        alias_node = spec.child_by_field_name("alias")
        exposed = (
            _specifier_name(text, alias_node)
            if alias_node is not None
            else _specifier_name(text, name_node)
        )
        original_names.append(original)
        if exposed == "default":
            entries.append(ExportEntry(path, "default", "default", target))
        else:
            entries.append(
                ExportEntry(path, _type_prefixed(exposed, is_type), "named", target)
            )
    edges: list[ImportEdge] = []
    if target is not None:
        edges.append(
            ImportEdge(
                path, "reexport_named", tuple(sorted(original_names)), target, line
            )
        )
    return edges, entries


_TYPE_ONLY_DECLARATIONS = ("interface_declaration", "type_alias_declaration")

_NAMED_DECLARATIONS = (
    "function_declaration",
    "generator_function_declaration",
    "class_declaration",
    "abstract_class_declaration",
    "enum_declaration",
    "internal_module",  # `export namespace N {}`
    "module",  # `export module N {}` (legacy TS spelling)
)

_VARIABLE_DECLARATIONS = ("lexical_declaration", "variable_declaration")


def _declaration_names(declaration: "Node", text: bytes) -> list[str]:
    """Exposed names of ``export <declaration>`` (document order)."""
    if declaration.type == "ambient_declaration":  # `export declare ...`
        inner = declaration.named_children
        return _declaration_names(inner[0], text) if inner else []
    if declaration.type in _TYPE_ONLY_DECLARATIONS:
        name_node = declaration.child_by_field_name("name")
        if name_node is None:
            return []
        return [TYPE_NAME_PREFIX + _node_text(text, name_node)]
    if declaration.type in _NAMED_DECLARATIONS:
        name_node = declaration.child_by_field_name("name")
        return [] if name_node is None else [_node_text(text, name_node)]
    if declaration.type in _VARIABLE_DECLARATIONS:
        names: list[str] = []
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is not None:
                names.extend(_pattern_bindings(name_node, text))
        return names
    return []


def _pattern_bindings(node: "Node", text: bytes) -> list[str]:
    """Local bindings introduced by a declarator name / destructuring pattern."""
    if node.type == "identifier":
        return [_node_text(text, node)]
    if node.type in ("object_pattern", "array_pattern"):
        names: list[str] = []
        for child in node.named_children:
            names.extend(_pattern_bindings(child, text))
        return names
    if node.type in (
        "shorthand_property_identifier_pattern",
        "shorthand_property_identifier",
    ):
        return [_node_text(text, node)]
    if node.type == "pair_pattern":
        value = node.child_by_field_name("value")
        return _pattern_bindings(value, text) if value is not None else []
    if node.type in ("rest_pattern", "assignment_pattern"):
        # rest: binding is the inner identifier; assignment: left side.
        inner = node.child_by_field_name("left") or _first_named_child(
            node, "identifier"
        )
        if inner is None:
            named = node.named_children
            inner = named[0] if named else None
        return _pattern_bindings(inner, text) if inner is not None else []
    return []


# ---------------------------------------------------------------------------
# call expressions: dynamic import() + CommonJS require()
# ---------------------------------------------------------------------------


def _call_edge(
    path: str, lang: str, call: "Node", text: bytes
) -> ImportEdge | None:
    fn = call.child_by_field_name("function")
    if fn is None:
        return None

    if fn.type == "import":  # dynamic import('./x')
        target = _first_call_argument_string(call, text)
        if target is None:
            return None
        return ImportEdge(path, "dynamic", (), target, _line(call))

    if (
        fn.type == "identifier"
        and _node_text(text, fn) == "require"
        and lang in _REQUIRE_LANGS
    ):
        target = _first_call_argument_string(call, text)
        if target is None:
            return None
        return ImportEdge(
            path, "require", _require_binding_names(call, text), target, _line(call)
        )

    return None


def _first_call_argument_string(call: "Node", text: bytes) -> str | None:
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return None
    named = arguments.named_children
    if not named:
        return None
    return _static_string(text, named[0])


def _require_binding_names(call: "Node", text: bytes) -> tuple[str, ...]:
    """Names bound by ``const <pattern> = require(...)`` (sorted).

    * ``const x = require('./m')`` → ``('x',)`` — whole module, namespace-like.
    * ``const {a, b: c} = require('./m')`` → ``('a', 'b')`` — original
      CommonJS member names (consistent with ``named`` semantics).
    * anything else (bare call, member access, nested expression) → ``()``.
    """
    parent = call.parent
    if parent is None or parent.type != "variable_declarator":
        return ()
    value = parent.child_by_field_name("value")
    if value is None or value.id != call.id:
        return ()
    name_node = parent.child_by_field_name("name")
    if name_node is None:
        return ()
    if name_node.type == "identifier":
        return (_node_text(text, name_node),)
    if name_node.type == "object_pattern":
        names: list[str] = []
        for child in name_node.named_children:
            if child.type in (
                "shorthand_property_identifier_pattern",
                "shorthand_property_identifier",
            ):
                names.append(_node_text(text, child))
            elif child.type == "pair_pattern":
                key = child.child_by_field_name("key")
                if key is not None:
                    names.append(_specifier_name(text, key))
        return tuple(sorted(names))
    return ()


def _first_named_child(node: "Node", child_type: str) -> "Node | None":
    for child in node.named_children:
        if child.type == child_type:
            return child
    return None


def _inside_namespace(node: "Node") -> bool:
    """True when the node sits inside a TS namespace / (ambient) module body."""
    parent = node.parent
    while parent is not None:
        if parent.type in ("internal_module", "module", "ambient_declaration"):
            return True
        parent = parent.parent
    return False
