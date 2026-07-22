"""nav_parent — B78 Seg G: display-layer nav-parent grouping (deterministic).

THE PROBLEM (B78 forensics-canon, 2026-07-21): the post-rescue "next
layer" hygiene — homes/names are fixed, but the board is a FLAT list of
product features while the repo itself DECLARES a nav hierarchy. Soc0
``module-registry.ts`` + ``app-nav-config.ts`` group 24/52 PFs under 6
nav categories (SOC / Detector Studio / Network Security / … ) with 3
live categories carrying no PF at all; twenty's
``useSettingsNavigationItems`` files ``Billing`` / ``Members`` /
``Data model`` under ``Workspace``, and its ``objects`` + ``data-model``
PFs are two views of ONE nav position.

THE FIX (display-layer ONLY): a deterministic nav-tree extractor reads
the STATIC exported nav registries (the same legal JSX/TS author-intent
class ``product_strings`` already trusts — NOT a dictionary of file
names, NEVER an i18n VALUE) and attaches a nullable ``nav_parent`` =
``{parent_label, parent_id, source_file, line, via}`` to a PF whose
anchor route matches a nav SUB-item (``via="route"``) — or, for a
route-LESS sub-item only (enum/const-referenced destinations), whose
tokens exactly equal the sub's label (``via="label"``) / enum path
member (``via="path"``). A route-bearing sub-item is judged by the
route channel alone — a bare token guess against it never attaches
(B78-it2 Goal 3, the Soc0 ``api`` → 'Mssp' slug-guess class).
Product features are NEVER merged or moved — the
engineering grain is untouched; only the display field + the
``scan_meta.nav_tree`` telemetry (categories, sub-items, matched /
unmatched / unrepresented / duplicates) are added. Live nav categories
with no PF are reported in ``nav_tree.unrepresented`` and NEVER minted.

MECHANISM (grammar in ``stacks/nav-tree.yaml``, not per-repo constants):
a nav CATEGORY is an exported array/object of nav ITEMS
(``{path|route|href} + {label|title|name|labelKey|…} (+icon)``), with
optional ``children|items|sub|pages`` nesting; the category label comes
from a literal field, else a humanized i18n KEY / ``id`` / const name;
a flat exported array (``SETTINGS_SIDEBAR_ITEMS``) is a category named
by its const, and a ``[{label, items:[…]}]`` return array is one
category per element. Consumption prior (like the ``spa_router``
precedent): only nav-registry files are read, and an item must carry a
label/icon — a random config array of ``{path, size}`` is never a nav
tree.

Flag ``FAULTLINE_NAV_PARENT`` — default **OFF**. Unset/0 → the pass is
never entered: ``nav_parent`` stays ``None`` (omitted from dumps — see
``Feature._omit_unset_spine_fields``) and no ``scan_meta.nav_tree`` key
is written, so the scan is byte-identical to the pre-Seg-G engine. A
nav-less repo (no readable registry) is likewise byte-identical
(inertness). Registered in ``scan_result_cache.ENV_OUTPUT_FLAGS``
WITHOUT a KEY_SCHEMA bump (the bump rides the separate flip commit).

Deterministic, $0 LLM, no network, read-only over tracked files.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.nav_taxonomy import _singular, _tokens
from faultline.pipeline_v2.product_strings import normalize_href, route_path_for_file

if TYPE_CHECKING:  # pragma: no cover
    from faultline.pipeline_v2.stage_0_intake import ScanContext

__all__ = [
    "NAV_PARENT_ENV",
    "NavCategory",
    "NavSubItem",
    "build_nav_tree",
    "nav_parent_enabled",
    "run_nav_parent",
]

NAV_PARENT_ENV = "FAULTLINE_NAV_PARENT"

#: Bounded per-file read — nav registries are small; the cap only guards
#: pathological blobs (mirrors ``spa_router._MAX_BYTES``).
_MAX_BYTES = 1_500_000
#: Code file extensions a nav registry may live in.
_CODE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


def nav_parent_enabled() -> bool:
    """Default **OFF**. ``FAULTLINE_NAV_PARENT`` in ``{1,true,yes,on}`` arms
    the pass; unset / ``0`` / anything else keeps it inert (no field, no
    ``scan_meta`` key → byte-identical to the pre-Seg-G engine)."""
    return os.environ.get(NAV_PARENT_ENV, "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ── grammar config ───────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return load_stack_yaml("nav-tree")


@lru_cache(maxsize=1)
def _label_keys() -> frozenset[str]:
    return frozenset(str(s) for s in (_cfg().get("label_keys") or ()))


@lru_cache(maxsize=1)
def _i18n_label_keys() -> frozenset[str]:
    return frozenset(str(s) for s in (_cfg().get("i18n_label_keys") or ()))


@lru_cache(maxsize=1)
def _href_keys() -> frozenset[str]:
    return frozenset(str(s) for s in (_cfg().get("href_keys") or ()))


@lru_cache(maxsize=1)
def _id_keys() -> tuple[str, ...]:
    return tuple(str(s) for s in (_cfg().get("id_keys") or ()))


@lru_cache(maxsize=1)
def _nest_keys() -> frozenset[str]:
    return frozenset(str(s) for s in (_cfg().get("nest_keys") or ()))


@lru_cache(maxsize=1)
def _icon_keys() -> frozenset[str]:
    return frozenset(str(s) for s in (_cfg().get("icon_keys") or ()))


@lru_cache(maxsize=1)
def _const_suffixes() -> tuple[str, ...]:
    # Longest-first so ``_SIDEBAR_ITEMS`` strips before ``_ITEMS``.
    return tuple(sorted(
        (str(s) for s in (_cfg().get("const_name_suffixes") or ())),
        key=len, reverse=True,
    ))


@lru_cache(maxsize=1)
def _i18n_trailing_generic() -> frozenset[str]:
    return frozenset(str(s).lower() for s in (_cfg().get("i18n_trailing_generic") or ()))


@lru_cache(maxsize=1)
def _nav_basename_markers() -> tuple[str, ...]:
    return tuple(str(s).lower() for s in (_cfg().get("nav_basename_markers") or ()))


@lru_cache(maxsize=1)
def _nav_content_markers() -> tuple[str, ...]:
    return tuple(str(s) for s in (_cfg().get("nav_content_markers") or ()))


# ── humanization / tokenization ──────────────────────────────────────────────

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _humanize(raw: str) -> str:
    """Title-case display label from a code identifier (``detectionsStudio``
    / ``network-security`` / ``NETWORK_SECURITY`` → ``Detections Studio`` /
    ``Network Security``). Pure structure — never an i18n value."""
    spaced = _CAMEL_RE.sub(" ", raw)
    words = [w for w in _SPLIT_RE.split(spaced) if w]
    out: list[str] = []
    for w in words:
        # An ALL-CAPS const-stem word (GLOBAL / SETTINGS) title-cases; a
        # mixed word keeps its interior casing (apiKeys already split).
        w = w.lower() if w.isupper() and len(w) > 1 else w
        out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


def _humanize_i18n_key(dotted: str) -> str:
    """Humanize the meaningful leaf of a dotted i18n KEY, stripping a
    trailing generic segment (``modules.detectionsStudio.name`` →
    ``Detections Studio``). The KEY is a code identifier (legal); the
    VALUE is never read."""
    segs = [s for s in dotted.split(".") if s]
    while len(segs) > 1 and segs[-1].lower() in _i18n_trailing_generic():
        segs.pop()
    return _humanize(segs[-1]) if segs else ""


def _label_slug_tokens(label: str) -> frozenset[str]:
    return _tokens(label)


def _member_token_of_ref(ref: str) -> str:
    """Last dotted member of an enum/const reference (``SettingsPath.Objects``
    → ``Objects``); the whole ident when there is no dot."""
    return ref.rsplit(".", 1)[-1]


# ── nav-file detection (consumption prior) ───────────────────────────────────


def _looks_like_nav_file(path: str, text: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    stem = base.rsplit(".", 1)[0]
    if any(mk in stem for mk in _nav_basename_markers()):
        return True
    return any(mk in text for mk in _nav_content_markers())


# ── object-literal parser (bounded, comment/string-aware) ────────────────────


@dataclass
class _Obj:
    """A parsed object literal: scalar fields + nested object-arrays."""

    line: int
    fields: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (kind, value)
    arrays: dict[str, list["_Obj"]] = field(default_factory=dict)      # key -> [obj, ...]
    array_refs: dict[str, list[str]] = field(default_factory=dict)     # key -> [ident, ...]
    child_obj: dict[str, "_Obj"] = field(default_factory=dict)         # key -> single object value


def _skip_ws_comments(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if s.startswith("/*", i):
            j = s.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        break
    return i


def _read_string(s: str, i: int) -> tuple[str, int]:
    """``s[i]`` is a quote; return (content, index past the closing quote).
    No escape unescaping — nav literals never embed escaped quotes."""
    q = s[i]
    j = s.find(q, i + 1)
    if j < 0:
        return s[i + 1:], len(s)
    return s[i + 1:j], j + 1


def _read_ident(s: str, i: int) -> tuple[str, int]:
    n = len(s)
    j = i
    while j < n and (s[j].isalnum() or s[j] in "_$"):
        j += 1
    return s[i:j], j


def _scan_balanced(s: str, i: int) -> int:
    """From the start of a scalar expression, return the index of the
    depth-0 terminator (``,`` / ``}`` / ``]``) — respecting nested
    ()[]{} and strings. Handles arrow bodies, ternaries, calls."""
    n = len(s)
    depth = 0
    while i < n:
        c = s[i]
        if c in "\"'`":
            _, i = _read_string(s, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif c == "," and depth == 0:
            return i
        i += 1
    return n


def _read_value(s: str, i: int, line_of: Any) -> tuple[str, Any, int]:
    """Parse the value at ``s[i]`` → ``(kind, node, next_i)``.

    kind: ``obj`` (node=_Obj) | ``array`` (node=list[(kind,node)]) |
    ``str`` (node=text) | ``template`` (node=static text or "") |
    ``ref`` (node=dotted ident) | ``other`` (node="")."""
    i = _skip_ws_comments(s, i)
    if i >= len(s):
        return "other", "", i
    c = s[i]
    if c == "{":
        obj, i = _parse_object(s, i, line_of)
        return "obj", obj, i
    if c == "[":
        elems, i = _parse_array(s, i, line_of)
        return "array", elems, i
    if c in "\"'":
        content, i = _read_string(s, i)
        return "str", content, i
    if c == "`":
        content, i = _read_string(s, i)
        return ("template", "" if "${" in content else content, i)
    if c.isalpha() or c in "_$":
        ident, j = _read_ident(s, i)
        k = _skip_ws_comments(s, j)
        # tagged template: ``t`User` `` / ``msg`Documents` ``
        if k < len(s) and s[k] == "`":
            content, k = _read_string(s, k)
            return ("template", "" if "${" in content else content, k)
        # dotted member chain: ``SettingsPath.Objects``
        dotted = ident
        while j < len(s) and s[j] == ".":
            nxt, j = _read_ident(s, j + 1)
            if not nxt:
                break
            dotted += "." + nxt
        # a call / spread / index after the ident → treat as opaque expr
        end = _scan_balanced(s, j)
        tail = s[j:end].strip()
        if tail == "":
            return "ref", dotted, j
        return "other", "", end
    # number / spread / anything else — scan to the terminator
    end = _scan_balanced(s, i)
    return "other", "", end


def _read_key(s: str, i: int) -> tuple[str, int]:
    """Parse an object key at ``s[i]`` (identifier, quoted, or ``[expr]``
    computed). Returns ("", i) when no key is present."""
    i = _skip_ws_comments(s, i)
    if i >= len(s):
        return "", i
    c = s[i]
    if c in "\"'":
        key, i = _read_string(s, i)
        return key, i
    if c == "[":
        # computed key — skip the bracket balanced, no usable key
        depth = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch in "\"'`":
                _, i = _read_string(s, i)
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        return "", i
    if c.isalpha() or c in "_$":
        key, i = _read_ident(s, i)
        return key, i
    return "", i


def _parse_object(s: str, i: int, line_of: Any) -> tuple[_Obj, int]:
    """``s[i] == '{'``. Parse one object literal (top-level members only)."""
    obj = _Obj(line=line_of(i))
    i += 1
    n = len(s)
    while i < n:
        i = _skip_ws_comments(s, i)
        if i >= n or s[i] == "}":
            i += 1 if i < n else 0
            break
        if s[i] == ",":
            i += 1
            continue
        key, i = _read_key(s, i)
        i = _skip_ws_comments(s, i)
        if i < n and s[i] == ":":
            kind, node, i = _read_value(s, i + 1, line_of)
            if key:
                if kind == "array":
                    child_objs = [nd for (k, nd) in node if k == "obj"]
                    child_ids = [nd for (k, nd) in node if k == "ref"]
                    if child_objs:
                        obj.arrays[key] = child_objs
                    if child_ids:
                        obj.array_refs[key] = child_ids
                elif kind == "obj":
                    obj.child_obj[key] = node
                elif kind in ("str", "template", "ref"):
                    obj.fields[key] = (kind, node)
        elif i < n and s[i] in ",}":
            # shorthand field — no value; ignore
            continue
        else:
            # method / getter / malformed — skip its value region
            i = _scan_balanced(s, i)
        i = _skip_ws_comments(s, i)
        if i < n and s[i] == ",":
            i += 1
    return obj, i


def _parse_array(s: str, i: int, line_of: Any) -> tuple[list[tuple[str, Any]], int]:
    """``s[i] == '['``. Parse an array's element (kind, node) list."""
    out: list[tuple[str, Any]] = []
    i += 1
    n = len(s)
    while i < n:
        i = _skip_ws_comments(s, i)
        if i >= n or s[i] == "]":
            i += 1 if i < n else 0
            break
        if s[i] == ",":
            i += 1
            continue
        # spread ``...NAME`` — resolve as a ref to the spread source array
        if s.startswith("...", i):
            ident, j = _read_ident(s, _skip_ws_comments(s, i + 3))
            if ident:
                out.append(("spread", ident))
                i = j
                continue
        kind, node, i = _read_value(s, i, line_of)
        out.append((kind, node))
        i = _skip_ws_comments(s, i)
        if i < n and s[i] == ",":
            i += 1
    return out, i


# ── nav tree assembly ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NavSubItem:
    """One navigable leaf under a category."""

    route: str | None            # normalized literal href, else None
    label: str                   # display label (may be "")
    label_tokens: frozenset[str]
    path_tokens: frozenset[str]  # tokens of an enum-referenced path member
    source_file: str
    line: int


@dataclass(frozen=True)
class NavCategory:
    """One vendor-declared nav area (the display parent)."""

    id: str
    label: str
    source_file: str
    line: int
    sub_items: tuple[NavSubItem, ...]


_DECL_RE = re.compile(
    r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
    r"(?::[^=;]+?)?=\s*(?=[\[{])",
)
_RETURN_RE = re.compile(r"\breturn\s*(?=[\[{])")


def _line_of_factory(text: str) -> Any:
    starts = [0]
    for m in re.finditer(r"\n", text):
        starts.append(m.end())

    def line_of(idx: int) -> int:
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= idx:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    return line_of


def _field_str(obj: _Obj, keys: frozenset[str]) -> str | None:
    for key, (kind, val) in obj.fields.items():
        if key in keys and kind in ("str", "template") and val.strip():
            return val.strip()
    return None


def _field_ref_or_str(obj: _Obj, keys: frozenset[str]) -> tuple[str, str] | None:
    """First (kind, value) among ``keys`` that is a str/template/ref."""
    for key, (kind, val) in obj.fields.items():
        if key in keys and val.strip():
            return kind, val.strip()
    return None


def _obj_id(obj: _Obj) -> str | None:
    for key in _id_keys():
        got = obj.fields.get(key)
        if got and got[0] in ("str", "template") and got[1].strip():
            return got[1].strip()
    return None


def _category_label(obj: _Obj | None, const_name: str | None) -> str:
    """Category display label: literal label → i18n-key humanized → id
    humanized → const-name humanized (nav suffixes stripped)."""
    if obj is not None:
        lit = _field_str(obj, _label_keys())
        if lit:
            return lit
        i18n = _field_str(obj, _i18n_label_keys())
        if i18n:
            hz = _humanize_i18n_key(i18n)
            if hz:
                return hz
        oid = _obj_id(obj)
        if oid:
            return _humanize(oid)
    if const_name:
        stem = const_name
        for suf in _const_suffixes():
            if stem.upper().endswith(suf):
                stem = stem[: len(stem) - len(suf)]
                break
        return _humanize(stem)
    return ""


def _category_id(obj: _Obj | None, const_name: str | None, label: str) -> str:
    if obj is not None:
        oid = _obj_id(obj)
        if oid:
            return oid
    if const_name:
        return _slug(const_name)
    return _slug(label)


def _slug(raw: str) -> str:
    spaced = _CAMEL_RE.sub(" ", raw)
    return "-".join(w.lower() for w in _SPLIT_RE.split(spaced) if w)


def _item_href(obj: _Obj) -> tuple[str | None, frozenset[str]]:
    """(normalized literal route | None, path-member tokens). A literal
    ``/x`` normalizes to a route; an enum ref (``SettingsPath.Objects``)
    yields no route but its member token feeds the slug channel."""
    for key, (kind, val) in obj.fields.items():
        if key not in _href_keys():
            continue
        if kind in ("str", "template") and val:
            route = normalize_href(val)
            if route:
                return route, frozenset()
        if kind == "ref" and val:
            member = _member_token_of_ref(val)
            toks = frozenset(
                t for t in (_singular(w.lower()) for w in _SPLIT_RE.split(
                    _CAMEL_RE.sub(" ", member)))
                if len(t) >= 2 and not t.isdigit()
            )
            return None, toks
    return None, frozenset()


def _is_nav_item(obj: _Obj) -> bool:
    """An item is nav-consumed when it carries a real navigation
    DESTINATION *and* a label/icon (the structural 'rendered by
    navigation' proxy). The destination must be an INTERNAL route
    (``/x``) or an enum/const route reference (``SettingsPath.Objects``):
    a bare relative file path (``src/objects/rocket.object.ts`` — a
    terminal-demo file tree) or an external URL (``https://docs…`` — a
    marketing link, off-surface like a README) is NOT a product nav
    destination and never mints a category."""
    route, ptoks = _item_href(obj)
    if route is None and not ptoks:
        return False
    has_label = any(
        k in _label_keys() or k in _i18n_label_keys() for k in obj.fields
    )
    has_icon = any(
        k in _icon_keys() for k in obj.fields
    ) or any(k in _icon_keys() for k in obj.child_obj)
    return has_label or has_icon


def _collect_sub_items(obj: _Obj, source_file: str) -> list[NavSubItem]:
    """Every nav leaf under ``obj`` — its own nesting arrays, flattened
    (deep ``subItems`` land on the top category), deterministic order."""
    out: list[NavSubItem] = []
    seen: set[tuple[str | None, str, int]] = set()

    def _walk(node: _Obj) -> None:
        # nested arrays first (deterministic key order)
        for key in sorted(node.arrays):
            if key in _nest_keys():
                for child in node.arrays[key]:
                    if _is_nav_item(child):
                        _emit(child)
                    _walk(child)
        for key in sorted(node.child_obj):
            if key in _nest_keys():
                child = node.child_obj[key]
                if _is_nav_item(child):
                    _emit(child)
                _walk(child)

    def _emit(item: _Obj) -> None:
        route, ptoks = _item_href(item)
        label = (
            _field_str(item, _label_keys())
            or _humanize_i18n_key(_field_str(item, _i18n_label_keys()) or "")
        )
        key = (route, label, item.line)
        if key in seen:
            return
        seen.add(key)
        out.append(NavSubItem(
            route=route,
            label=label,
            label_tokens=_label_slug_tokens(label),
            path_tokens=ptoks,
            source_file=source_file,
            line=item.line,
        ))

    _walk(obj)
    return out


def _has_nested_nav_array(obj: _Obj) -> bool:
    for key in _nest_keys():
        for child in obj.arrays.get(key, []):
            if _is_nav_item(child):
                return True
        c = obj.child_obj.get(key)
        if c is not None and _is_nav_item(c):
            return True
    return False


def _parse_nav_file(path: str, text: str) -> list[NavCategory]:
    line_of = _line_of_factory(text)

    # const-name -> parsed value node (object) or element list (array),
    # for ref / spread resolution.
    const_obj: dict[str, _Obj] = {}
    const_arr: dict[str, list[tuple[str, Any]]] = {}
    roots: list[tuple[str | None, str, Any, int]] = []  # (name, kind, node, decl_line)

    for m in _DECL_RE.finditer(text):
        name = m.group(1)
        decl_line = line_of(m.start())
        kind, node, _ = _read_value(text, m.end(), line_of)
        if kind == "obj":
            const_obj[name] = node
            roots.append((name, "obj", node, decl_line))
        elif kind == "array":
            const_arr[name] = node
            roots.append((name, "array", node, decl_line))
    for m in _RETURN_RE.finditer(text):
        decl_line = line_of(m.start())
        kind, node, _ = _read_value(text, m.end(), line_of)
        if kind in ("obj", "array"):
            roots.append((None, kind, node, decl_line))

    def _resolve_ref(ident: str) -> _Obj | None:
        return const_obj.get(ident)

    def _array_elems(node: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Flatten spreads (``...MODULES``) one level into element list."""
        out: list[tuple[str, Any]] = []
        for kind, nd in node:
            if kind == "spread" and isinstance(nd, str) and nd in const_arr:
                out.extend(const_arr[nd])
            else:
                out.append((kind, nd))
        return out

    cats: list[NavCategory] = []

    def _add_category(obj: _Obj | None, const_name: str | None,
                      sub_source: _Obj, source_file: str, line: int) -> None:
        subs = _collect_sub_items(sub_source, source_file)
        if not subs:
            return
        label = _category_label(obj, const_name)
        if not label.strip():
            # A nav area must have a NAME. An anonymous ``return [...]``
            # array's top-level flat leaves (no object label, no const
            # name) are not a category — dropping them avoids the empty
            # '' bucket (twenty marketing Menu return-array leaves).
            return
        cid = _category_id(obj, const_name, label)
        cats.append(NavCategory(
            id=cid, label=label, source_file=source_file, line=line,
            sub_items=tuple(subs),
        ))

    for const_name, kind, node, decl_line in roots:
        if kind == "obj":
            obj: _Obj = node
            if _has_nested_nav_array(obj):
                _add_category(obj, const_name, obj, path, obj.line)
            # A bare single nav-item object (no nesting, not an array —
            # e.g. ADMIN_SIDEBAR_ITEM) is a top-level sidebar ENTRY, not a
            # category/group; it never synthesizes a self-parent category
            # (a nav_parent is for a SUB-item under a real area).
        else:  # array
            elems = _array_elems(node)
            direct_objs = [nd for (k, nd) in elems if k == "obj"]
            ref_names = [nd for (k, nd) in elems if k == "ref"]
            # (a) elements are categories themselves (nested nav arrays)
            elem_cats = [o for o in direct_objs if _has_nested_nav_array(o)]
            if elem_cats:
                for o in elem_cats:
                    _add_category(o, None, o, path, o.line)
            # (b) elements are flat nav leaves → the CONST is the category
            #     (declared at ``decl_line`` — never a synthetic line 1).
            flat_leaves = [
                o for o in direct_objs
                if _is_nav_item(o) and not _has_nested_nav_array(o)
            ]
            if flat_leaves:
                synthetic = _Obj(line=decl_line)
                synthetic.arrays["items"] = flat_leaves
                _add_category(None, const_name, synthetic, path, decl_line)
            # (c) elements are refs to category-defining consts
            for ident in ref_names:
                ref_obj = _resolve_ref(ident)
                if ref_obj is not None and _has_nested_nav_array(ref_obj):
                    _add_category(ref_obj, ident, ref_obj, path, ref_obj.line)

    # Dedupe categories by (source_file, line, id) — a def reachable via
    # several arrays (MODULES + ALL_MODULES spread) yields ONE category.
    deduped: dict[tuple[str, int, str], NavCategory] = {}
    for c in cats:
        deduped.setdefault((c.source_file, c.line, c.id), c)
    return sorted(deduped.values(), key=lambda c: (c.source_file, c.line, c.id))


def build_nav_tree(ctx: "ScanContext") -> list[NavCategory]:
    """Every nav category the repo declares, across its nav-registry files.
    Deterministic (sorted tracked order). Empty when the repo has no
    readable nav registry — the inertness path."""
    repo_root = Path(ctx.repo_path)
    cats: list[NavCategory] = []
    for rel in sorted(str(p).replace("\\", "/") for p in ctx.tracked_files):
        if not rel.endswith(_CODE_EXTS):
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeError):
            continue
        if not text or len(text) > _MAX_BYTES:
            continue
        if not _looks_like_nav_file(rel, text):
            continue
        cats.extend(_parse_nav_file(rel, text))
    return cats


# ── PF ↔ nav matching ────────────────────────────────────────────────────────


def _pf_routes(pf: Any, routes_by_file: dict[str, set[str]]) -> set[str]:
    """Normalized routes a PF serves — anchor-file routes (routes_index +
    file-system routing)."""
    out: set[str] = set()
    anchors = [
        str(mf.path) for mf in (getattr(pf, "member_files", None) or [])
        if getattr(mf, "role", None) == "anchor"
    ]
    paths = anchors or [str(p) for p in (getattr(pf, "paths", None) or [])]
    for p in paths:
        p = p.replace("\\", "/")
        for r in routes_by_file.get(p, ()):  # routes_index patterns
            nr = normalize_href(r)
            if nr:
                out.add(nr)
        fsr = route_path_for_file(p)
        if fsr and fsr != "/":
            out.add(fsr.lower())
    return out


def _pf_tokens(pf: Any) -> frozenset[str]:
    toks = set(_tokens(str(getattr(pf, "name", "") or "")))
    dn = getattr(pf, "display_name", None)
    if dn:
        toks |= set(_tokens(str(dn)))
    return frozenset(toks)


def _route_match(pf_routes: set[str], sub: NavSubItem) -> int:
    """Route-match strength of a PF against a nav SUB-item:
      ``2`` — an EXACT route (a PF route equals the sub destination);
      ``1`` — NESTED-under (a PF route is a deeper page below the sub);
      ``0`` — no match.
    The broad direction (a PF ABOVE the sub — an umbrella route claiming a
    specific child leaf) is deliberately NOT a match. Exact beats nested so
    a PF that serves several routes homes on its OWN nav position, not the
    longest ancestor (the ``findings`` vs ``mitre-framework`` trap)."""
    if not sub.route or not pf_routes:
        return 0
    sr = sub.route
    if sr in pf_routes:
        return 2
    if any(r.startswith(sr + "/") for r in pf_routes):
        return 1
    return 0


def _token_match(pf_tokens: frozenset[str], sub: NavSubItem) -> str | None:
    """B78-it2 (Goal 3) — the token-identity channel, legal ONLY for a
    route-LESS nav sub-item. An enum/const-referenced destination (the
    twenty ``SettingsPath.Objects`` class) has no literal href to
    route-match, so exact token identity against its label / enum member
    is the honest remaining evidence. A sub-item that DECLARES a literal
    route is judged by the route channel alone: a PF that does not serve
    that route may not token-guess its way under the area (the Soc0
    ``api`` → 'Mssp' exhibit — the PF's ``{api}`` token equalled the
    humanized i18n label of ``/tenants/api``, a route the PF never
    serves). Returns the honest via channel (``"label"`` when the match
    is the sub-item's display label, ``"path"`` when it is the enum path
    member) or ``None``."""
    if not pf_tokens or sub.route is not None:
        return None
    if sub.label_tokens and pf_tokens == sub.label_tokens:
        return "label"
    if sub.path_tokens and pf_tokens == sub.path_tokens:
        return "path"
    return None


def _routes_by_file(routes_index: list[dict[str, Any]] | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in routes_index or ():
        if not isinstance(e, dict):
            continue
        fpath = str(e.get("file") or "").replace("\\", "/")
        pat = str(e.get("pattern") or "")
        if fpath and pat:
            out.setdefault(fpath, set()).add(pat)
    return out


def run_nav_parent(
    product_features: list[Any],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    user_flows: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Attach ``nav_parent`` to product features (in place) and return the
    ``scan_meta.nav_tree`` telemetry. Returns ``None`` — no field, no
    telemetry key — when the flag is off OR the repo declares no nav tree
    (byte-identical inertness). PFs are never merged, moved, or minted."""
    if not nav_parent_enabled():
        return None
    if not product_features:
        return None
    try:
        categories = build_nav_tree(ctx)
    except Exception:  # noqa: BLE001 — extraction never breaks a scan
        categories = []
    if not categories:
        return None

    routes_by_file = _routes_by_file(routes_index)
    # UF routes per PF (union of members' router paths) enrich the route set.
    uf_routes_by_pf: dict[str, set[str]] = {}
    for uf in user_flows or ():
        pid = getattr(uf, "product_feature_id", None)
        if not pid:
            continue
        bucket = uf_routes_by_pf.setdefault(str(pid), set())
        for r in (getattr(uf, "routes", None) or ()):
            nr = normalize_href(str(r))
            if nr:
                bucket.add(nr)

    # Ordered (category, sub, sub-index, identity-tokens) tuples → a stable
    # "nav position" list. The identity tokens (category label + sub label
    # + sub route leaf + enum-member) disambiguate a keyless PF that bundles
    # several routes: among equal route-strength matches it homes on the
    # position it most RESEMBLES, not an arbitrary earliest one.
    positions: list[tuple[NavCategory, NavSubItem, int, frozenset[str]]] = []
    for c in categories:
        ctoks = _tokens(c.label)
        for si, sub in enumerate(c.sub_items):
            leaf_toks: frozenset[str] = frozenset()
            if sub.route:
                leaf_toks = _tokens(sub.route.rsplit("/", 1)[-1])
            ident = ctoks | sub.label_tokens | sub.path_tokens | leaf_toks
            positions.append((c, sub, si, ident))

    # Honest per-channel counters (B78-it2 Goal 3): ``route`` (literal /
    # nested href), ``label`` (route-less sub display label), ``path``
    # (route-less enum path member). The old ``slug`` guess — a token hit
    # against a ROUTE-BEARING sub-item — no longer matches at all.
    matched_by_via: dict[str, int] = {"route": 0, "label": 0, "path": 0}

    def _catkey(c: NavCategory) -> tuple[str, int, str]:
        return (c.source_file, c.line, c.id)

    # nav-position key = (unique category key, sub index) — so two
    # categories that happen to share a decl line + sub index never
    # collide (the flat-const line-1 trap).
    pos_to_pfs: dict[tuple[tuple[str, int, str], int], list[str]] = {}
    pos_meta: dict[tuple[tuple[str, int, str], int], tuple[str, str]] = {}
    cat_matched: dict[tuple[str, int, str], int] = {
        _catkey(c): 0 for c in categories
    }

    products = [
        pf for pf in product_features
        if getattr(pf, "layer", "product") == "product"
    ] or list(product_features)

    for pf in products:
        proutes = _pf_routes(pf, routes_by_file)
        proutes |= uf_routes_by_pf.get(str(getattr(pf, "name", "")), set())
        ptoks = _pf_tokens(pf)
        best: tuple[tuple[int, int, int], NavCategory, NavSubItem, int, str] | None = None
        for pos_idx, (c, sub, si, ident) in enumerate(positions):
            via = ""
            # exact route (10) > nested-under route (5) > route-less token
            # identity (1). Exact beats nested so a multi-route PF homes on
            # its OWN position, not the longest ancestor leaf. The token
            # channel never fires against a route-bearing sub-item (B78-it2
            # Goal 3 — the ``api`` → 'Mssp' slug-guess class).
            rm = _route_match(proutes, sub)
            if rm == 2:
                via, score = "route", 10
            elif rm == 1:
                via, score = "route", 5
            else:
                tv = _token_match(ptoks, sub)
                if tv is None:
                    continue
                via, score = tv, 1
            # tie-break: how much the PF RESEMBLES this position (identity
            # token overlap), then earliest nav order — so a bundled PF's
            # route hit on a foreign area loses to the area it names.
            overlap = len(ptoks & ident) if ptoks else 0
            cand = ((score, overlap, -pos_idx), c, sub, si, via)
            if best is None or cand[0] > best[0]:
                best = cand
        if best is None:
            continue
        _rank, c, sub, si, via = best
        pf.nav_parent = {
            "parent_label": c.label,
            "parent_id": c.id,
            "source_file": c.source_file,
            "line": c.line,
            "via": via,
        }
        matched_by_via[via] += 1
        ck = _catkey(c)
        pos_to_pfs.setdefault((ck, si), []).append(
            str(getattr(pf, "name", "")))
        pos_meta[(ck, si)] = (c.label, sub.label)
        cat_matched[ck] += 1

    sub_total = sum(len(c.sub_items) for c in categories)
    matched_positions = {k for k, pfs in pos_to_pfs.items() if pfs}
    duplicates = [
        {
            "category_id": ck[2], "source_file": ck[0], "line": ck[1],
            "sub_index": si,
            "category_label": pos_meta[(ck, si)][0],
            "sub_label": pos_meta[(ck, si)][1],
            "pfs": sorted(pfs),
        }
        for (ck, si), pfs in sorted(pos_to_pfs.items())
        if len(pfs) > 1
    ]
    unrepresented = [
        {"id": c.id, "label": c.label,
         "source_file": c.source_file, "line": c.line}
        for c in categories
        if cat_matched[_catkey(c)] == 0
    ]

    return {
        "enabled": True,
        "source_files": sorted({c.source_file for c in categories}),
        "category_count": len(categories),
        "sub_item_count": sub_total,
        "categories": [
            {"id": c.id, "label": c.label, "source_file": c.source_file,
             "line": c.line, "sub_items": len(c.sub_items),
             "matched_pfs": cat_matched[(c.source_file, c.line, c.id)]}
            for c in categories
        ],
        "matched": sum(matched_by_via.values()),
        "matched_via": dict(matched_by_via),
        "matched_positions": len(matched_positions),
        "unmatched_sub_items": sub_total - len(matched_positions),
        "unrepresented": unrepresented,
        "duplicates": duplicates,
    }
