"""Stage 6.55 — page-interior structure (Product-Spine §4.6, Wave 4).

Deterministic tree-sitter parse of PAGE/route entry files (TS/TSX/JSX
first) into their **interior render tree**: which components a page
renders, where each is defined, whether it is a PRODUCT component or a
design-system primitive, and which author-visible labels (headings,
``title=``/``label=`` props) the page carries.

Why this exists (rootcause RC6): the engine knew a page EXISTS (route
anchor) but nothing about what it HOSTS, so shell pages became opaque
one-file capabilities, flows on them carried degenerate 1-2-line spans
(the supabase 21.1% wrapper-span class), and journeys could not cite
in-PF interior evidence. This stage is the designed upgrade over the
regex "AST" (``analyzer/ast_extractor.py``) for the page-interior
surface only — the regex extractor keeps every other duty.

Consumers
=========

  * ``refine_flow_spans``   — pre-Stage-3.5: page-entry flows gain
    ``role="interior"`` symbol attributions (component DEFINITION spans,
    1-hop into the imported product module).
  * ``inject_interior_nodes`` — post-Stage-3.5: the same spans become
    ``FlowNode``s so ``line_ranges`` / LOC accounting see them; support
    nodes whose whole-file span covers a resolved component source are
    TIGHTENED to the definition span.
  * ``spine_anchors._build_interior_anchors`` — ≥2-page repeated product
    component families become ``source="interior"`` sub-anchors (never
    minting by default; the existing Stage-6.86 mint bar adjudicates).
  * Stage 6.7d digest — per-PF interior section labels extend the
    constrained Call-1 citation vocabulary (``from_sections``).

Determinism + cost
==================

Pure file IO + tree-sitter walk. NO LLM, NO network. Output sorted
everywhere; per-file parse cached by **content hash** (sha256 of file
bytes + parser version + grammar versions) through the scan's
``CacheBackend`` (kind ``interior-parse``) plus an in-process memo.

Graceful degrade: tree-sitter is an optional dependency (``pip install
'faultlines[ast]'``). When missing, :func:`is_active` returns False,
the stage records telemetry and every consumer no-ops — scans stay
byte-identical to the pre-W4 engine. Kill-switch:
``FAULTLINE_STAGE_6_55=0``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.cache.backend import CacheKind

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)

__all__ = [
    "ENV_FLAG",
    "InteriorNode",
    "PageInterior",
    "InteriorFamily",
    "InteriorResult",
    "is_active",
    "stage_6_55_enabled",
    "get_page_interiors",
    "run_stage_6_55",
    "refine_flow_spans",
    "inject_interior_nodes",
    "build_interior_evidence",
    "degenerate_span_stats",
]

ENV_FLAG = "FAULTLINE_STAGE_6_55"

#: Bump on ANY change to the parse walk / node shape / classification —
#: the content-hash cache key includes it (rule-cache-invalidation).
PARSER_VERSION = "w4-interior-1"

#: Per-page node cap (sorted, deterministic truncation) — keeps monster
#: dashboard pages from blowing the artifact/digest up.
MAX_NODES_PER_PAGE = 120
#: Per-flow cap for ADDED interior attributions (mirrors
#: flow_symbols.DEFAULT_MAX_SYMBOLS_PER_FLOW).
MAX_INTERIOR_PER_FLOW = 12
#: Files above this size are skipped (bundles / generated pages).
MAX_PAGE_BYTES = 512_000
#: A repeated interior family needs this many DISTINCT pages.
MIN_FAMILY_PAGES = 2

_TS_EXTS = (".tsx", ".ts")
_JS_EXTS = (".jsx", ".js", ".mjs", ".cjs")
_PAGE_EXTS = _TS_EXTS + _JS_EXTS

# Label-bearing JSX attributes (author-visible strings, product intent).
_LABEL_ATTRS = frozenset({"title", "label", "heading", "aria-label", "alt"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3"})

# Design-system / primitive vocabulary — REUSED from the UF-rollup
# Filter A (single source of truth for "rendering infra, not product").
from faultline.pipeline_v2.stage_6_7_user_flows import (  # noqa: E402
    _PRIMITIVE_DIR_SEGMENTS,
    _PRIMITIVE_FILE_VOCAB,
)

# ── Optional tree-sitter import (pattern mirrors stage_6_6) ──────────────

try:  # pragma: no cover — import-time branch
    from tree_sitter import Language, Parser

    TREE_SITTER_AVAILABLE = True
except Exception:  # noqa: BLE001 — any failure → degrade gracefully
    TREE_SITTER_AVAILABLE = False
    Language = None  # type: ignore[assignment, misc]
    Parser = None  # type: ignore[assignment, misc]


_LANG_CACHE: dict[str, Any] = {}


def _language(name: str) -> Any | None:
    """tree-sitter Language for ``tsx`` | ``typescript`` | ``javascript``."""
    if not TREE_SITTER_AVAILABLE:
        return None
    if name in _LANG_CACHE:
        return _LANG_CACHE[name]
    lang = None
    try:
        if name in ("tsx", "typescript"):
            import tree_sitter_typescript as ts_mod

            capsule = (ts_mod.language_tsx() if name == "tsx"
                       else ts_mod.language_typescript())
            lang = Language(capsule)
        elif name == "javascript":
            import tree_sitter_javascript as js_mod

            lang = Language(js_mod.language())
    except Exception:  # noqa: BLE001 — partial extras install
        lang = None
    _LANG_CACHE[name] = lang
    return lang


def _lang_for_ext(path: str) -> str | None:
    low = path.lower()
    if low.endswith(".tsx"):
        return "tsx"
    if low.endswith(".ts"):
        return "typescript"
    if low.endswith(_JS_EXTS):
        return "javascript"
    return None


def _grammar_signature() -> str:
    """Version string of tree-sitter + loaded grammars (cache key part)."""
    parts: list[str] = []
    try:
        import importlib.metadata as _md

        for pkg in ("tree-sitter", "tree-sitter-typescript",
                    "tree-sitter-javascript"):
            try:
                parts.append(f"{pkg}={_md.version(pkg)}")
            except Exception:  # noqa: BLE001
                parts.append(f"{pkg}=?")
    except Exception:  # noqa: BLE001
        parts.append("meta=?")
    return ";".join(parts)


def is_active(ctx: "ScanContext | None" = None) -> bool:
    """Tree-sitter importable + ≥1 web grammar loadable + env flag on."""
    if not stage_6_55_enabled():
        return False
    if not TREE_SITTER_AVAILABLE:
        return False
    return any(_language(n) is not None
               for n in ("tsx", "javascript", "typescript"))


def stage_6_55_enabled() -> bool:
    return (os.environ.get(ENV_FLAG, "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ── Result shapes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InteriorNode:
    """One interior element of a page's render tree."""

    kind: str                       # "component" | "heading"
    name: str                       # component name / heading text
    label: str | None               # author-visible label candidate
    usage_line_start: int           # 1-indexed, in the PAGE file
    usage_line_end: int
    source_kind: str                # local | workspace | package | unresolved
    provenance: str                 # "product" | "design_system"
    source_file: str | None = None  # repo-relative, when resolved
    def_line_start: int | None = None
    def_line_end: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "name": self.name, "label": self.label,
            "usage": [self.usage_line_start, self.usage_line_end],
            "source_kind": self.source_kind, "provenance": self.provenance,
            "source_file": self.source_file,
            "def": ([self.def_line_start, self.def_line_end]
                    if self.def_line_start is not None else None),
        }


@dataclass(frozen=True)
class PageInterior:
    file: str                        # repo-relative page path
    page_kind: str                   # "page" | "layout"
    nodes: tuple[InteriorNode, ...]  # sorted (usage line, name)
    parse_ok: bool = True


@dataclass(frozen=True)
class InteriorFamily:
    """A product component family rendered by ≥2 distinct pages.

    ``dir_owned=True`` — the component OWNS its directory (its source is
    an ``index.*`` file), so the family may claim the dir subtree.
    ``dir_owned=False`` — a component file living directly inside a
    bigger container dir: the family claims ONLY its own file(s). This
    is the container-sink guard (supabase ``components/interfaces``
    smoke, 2026-07-07: a prefix claim at the container minted a
    253K-LOC PF — exactly the D1 sink class this arc kills).
    """

    family_dir: str                  # grouping/labeling dir
    component_names: tuple[str, ...]
    page_files: tuple[str, ...]
    source_files: tuple[str, ...]
    label: str                       # display candidate (best heading/name)
    dir_owned: bool = False


@dataclass
class InteriorResult:
    active: bool
    reason: str = ""
    pages: dict[str, PageInterior] = field(default_factory=dict)
    families: tuple[InteriorFamily, ...] = ()
    telemetry: dict[str, Any] = field(default_factory=dict)

    def product_sections_by_source_prefix(
        self, prefixes: tuple[str, ...], files: frozenset[str] = frozenset(),
        cap: int = 8,
    ) -> list[str]:
        """Distinct product-component/section labels whose PAGE lives
        inside the given subtree (anchor scope) — the 6.7d digest feed."""
        labels: list[str] = []
        seen: set[str] = set()
        for page in sorted(self.pages):
            inside = page in files or any(
                page == p or page.startswith(p + "/") for p in prefixes
            )
            if not inside:
                continue
            for n in self.pages[page].nodes:
                if n.provenance != "product":
                    continue
                text = (n.label or n.name or "").strip()
                key = text.lower()
                if not text or key in seen:
                    continue
                seen.add(key)
                labels.append(text)
                if len(labels) >= cap:
                    return labels
        return labels


# ── Import extraction + provenance (workspace-aware) ─────────────────────


_WS_UI_KEYS = frozenset({
    "ui", "design-system", "design_system", "components", "component",
    "primitives", "icons", "theme", "styles",
})


def _norm_join(base_dir: str, rel: str) -> str:
    """POSIX-normalise ``base_dir / rel`` without touching the fs."""
    segs: list[str] = []
    for part in (base_dir + "/" + rel).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if segs:
                segs.pop()
            continue
        segs.append(part)
    return "/".join(segs)


_CAND_SUFFIXES = (
    "", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs",
    "/index.tsx", "/index.ts", "/index.jsx", "/index.js",
)


def _resolve_spec(
    spec: str,
    page_file: str,
    tracked: frozenset[str],
    ws_by_name: dict[str, str],
    alias_roots: tuple[tuple[str, str], ...],
) -> tuple[str | None, str]:
    """Resolve one import specifier → (repo-relative file | None, source_kind).

    Resolution order: relative → tsconfig-style alias roots → workspace
    package names → unresolved package. Extension probing over the
    TRACKED file set only (no fs IO) keeps this deterministic and fast.
    """

    def _probe(base: str) -> str | None:
        for suf in _CAND_SUFFIXES:
            cand = base + suf
            if cand in tracked:
                return cand
        return None

    if spec.startswith("."):
        base_dir = page_file.rsplit("/", 1)[0] if "/" in page_file else ""
        hit = _probe(_norm_join(base_dir, spec))
        return hit, ("local" if hit else "unresolved")

    # Alias roots: ("@/", "apps/web/src/") style pairs, longest first.
    for prefix, root in alias_roots:
        if spec == prefix.rstrip("/") or spec.startswith(prefix):
            rel = spec[len(prefix):].lstrip("/")
            hit = _probe(root.rstrip("/") + ("/" + rel if rel else ""))
            if hit:
                return hit, "workspace"

    # Workspace package import: exact name or name-prefixed subpath.
    for name in sorted(ws_by_name, key=len, reverse=True):
        if spec == name or spec.startswith(name + "/"):
            root = ws_by_name[name].strip("/")
            sub = spec[len(name):].lstrip("/")
            for base in ((f"{root}/src/{sub}" if sub else f"{root}/src"),
                         (f"{root}/{sub}" if sub else root)):
                hit = _probe(base)
                if hit:
                    return hit, "workspace"
            return None, "workspace"
    return None, "package"


def _alias_roots_for(
    page_file: str, tracked: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Universal ``@/`` alias heuristic — NO tsconfig parse (deterministic,
    IO-free): ``@/x`` resolves against the page's enclosing ``src/`` (or
    app root) directory, the dominant convention across Next/Vite
    templates. Returns ((prefix, root), …) longest-prefix-first."""
    segs = page_file.split("/")
    roots: list[tuple[str, str]] = []
    for i in range(len(segs) - 1, 0, -1):
        if segs[i - 1] == "src":
            roots.append(("@/", "/".join(segs[:i])))
            break
    if not roots:
        # No src/ ancestor: fall back to the routing-root's parent
        # (app/… under apps/web → apps/web).
        for i, s in enumerate(segs[:-1]):
            if s in ("app", "pages", "routes"):
                roots.append(("@/", "/".join(segs[:i])))
                break
    roots.append(("~/", roots[0][1] if roots else ""))
    return tuple((p, r) for p, r in roots if r)


def _classify_provenance(
    component: str,
    source_kind: str,
    source_file: str | None,
    ws_by_path: dict[str, str],
) -> str:
    """"product" vs "design_system" — the W2b import-provenance filter
    generalised: an element only counts as PRODUCT interior when it is
    NOT a design-system primitive by (a) unresolved external package,
    (b) UI-class workspace package, (c) primitive dir segment in its
    resolved path, or (d) primitive widget name."""
    if source_kind == "package":
        return "design_system"
    if component.lower() in _PRIMITIVE_FILE_VOCAB:
        return "design_system"
    if source_file:
        segs = [s.lower() for s in source_file.split("/")[:-1]]
        if any(s in _PRIMITIVE_DIR_SEGMENTS for s in segs[-2:]):
            # Only the two INNERMOST dirs: features/billing/components/X
            # is still product; packages/ui/src/button is design-system.
            if not any(
                s not in _PRIMITIVE_DIR_SEGMENTS and s not in ("src", "lib")
                for s in segs[-2:]
            ):
                return "design_system"
        for ws_path, ws_key in ws_by_path.items():
            if source_file.startswith(ws_path + "/"):
                if ws_key in _WS_UI_KEYS:
                    return "design_system"
                break
    return "product"


# ── Tree-sitter walk ─────────────────────────────────────────────────────


def _node_text(node: Any, src: bytes) -> str:
    try:
        return src[node.start_byte:node.end_byte].decode(
            "utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _walk_imports(root: Any, src: bytes) -> dict[str, str]:
    """{imported local name → specifier} from ES import statements."""
    out: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            src_node = node.child_by_field_name("source")
            spec = _node_text(src_node, src).strip("'\"") if src_node else ""
            if spec:
                for ch in node.children:
                    if ch.type != "import_clause":
                        continue
                    for c in ch.children:
                        if c.type == "identifier":       # default import
                            out.setdefault(_node_text(c, src), spec)
                        elif c.type == "namespace_import":
                            for cc in c.children:
                                if cc.type == "identifier":
                                    out.setdefault(_node_text(cc, src), spec)
                        elif c.type == "named_imports":
                            for spec_node in c.named_children:
                                if spec_node.type != "import_specifier":
                                    continue
                                alias = spec_node.child_by_field_name("alias")
                                name = spec_node.child_by_field_name("name")
                                local = alias if alias is not None else name
                                if local is not None:
                                    out.setdefault(
                                        _node_text(local, src), spec)
            continue
        for ch in node.children:
            if ch.type in ("program", "import_statement",
                           "export_statement"):
                stack.append(ch)
            elif node.type == "program":
                stack.append(ch)
    return out


_COMPONENT_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
_WS_RE = re.compile(r"\s+")


def _jsx_elements(root: Any) -> list[Any]:
    """Every jsx_(self_closing_/opening_)element under ``root`` (iterative)."""
    hits: list[Any] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("jsx_self_closing_element", "jsx_opening_element"):
            hits.append(node)
        stack.extend(node.children)
    hits.sort(key=lambda n: (n.start_point[0], n.start_point[1]))
    return hits


def _element_name(el: Any, src: bytes) -> str:
    name_node = el.child_by_field_name("name")
    if name_node is None:
        for ch in el.children:
            if ch.type in ("identifier", "member_expression",
                           "jsx_namespace_name", "nested_identifier"):
                name_node = ch
                break
    return _node_text(name_node, src) if name_node is not None else ""


def _element_label(el: Any, src: bytes) -> str | None:
    """First label-bearing string attribute of a JSX element."""
    for ch in el.children:
        if ch.type != "jsx_attribute":
            continue
        parts = ch.children
        if not parts:
            continue
        attr = _node_text(parts[0], src).lower()
        if attr not in _LABEL_ATTRS:
            continue
        for v in parts[1:]:
            if v.type == "string":
                text = _node_text(v, src).strip("'\"").strip()
                if text:
                    return text[:80]
    return None


def _heading_text(el: Any, src: bytes) -> str | None:
    """Concatenated jsx_text of the heading ELEMENT wrapping ``el``."""
    parent = el.parent
    if parent is None or parent.type != "jsx_element":
        return None
    texts: list[str] = []
    for ch in parent.children:
        if ch.type == "jsx_text":
            t = _WS_RE.sub(" ", _node_text(ch, src)).strip()
            if t:
                texts.append(t)
    joined = " ".join(texts).strip()
    return joined[:80] if joined else None


def _parse_page_source(
    rel_path: str,
    source: bytes,
    tracked: frozenset[str],
    ws_by_name: dict[str, str],
    ws_by_path: dict[str, str],
) -> list[dict[str, Any]]:
    """Parse ONE page file → serialisable interior node dicts (sorted)."""
    lang_name = _lang_for_ext(rel_path)
    lang = _language(lang_name) if lang_name else None
    if lang is None:
        return []
    try:
        parser = Parser(lang)
        tree = parser.parse(source)
    except Exception:  # noqa: BLE001 — grammar/version faults degrade
        logger.debug("stage_6_55: parse failed for %s", rel_path)
        return []

    imports = _walk_imports(tree.root_node, source)
    alias_roots = _alias_roots_for(rel_path, tracked)

    raw: list[dict[str, Any]] = []
    seen_components: set[str] = set()
    for el in _jsx_elements(tree.root_node):
        name = _element_name(el, source)
        if not name:
            continue
        line_start = el.start_point[0] + 1
        line_end = el.end_point[0] + 1
        low = name.lower()
        if low in _HEADING_TAGS:
            text = _heading_text(el, source)
            if text:
                raw.append({
                    "kind": "heading", "name": text, "label": text,
                    "usage": [line_start, line_end],
                    "source_kind": "local", "provenance": "product",
                    "source_file": None, "def": None,
                })
            continue
        head = name.split(".", 1)[0]
        if not _COMPONENT_RE.match(head):
            continue  # host elements / lowercase tags
        if head in seen_components:
            continue  # first usage wins (deterministic)
        seen_components.add(head)
        spec = imports.get(head)
        if spec is None:
            source_kind, source_file = "local", None
        else:
            source_file, source_kind = _resolve_spec(
                spec, rel_path, tracked, ws_by_name, alias_roots)
        provenance = _classify_provenance(
            head, source_kind, source_file, ws_by_path)
        raw.append({
            "kind": "component", "name": name, "label": _element_label(el, source),
            "usage": [line_start, line_end],
            "source_kind": source_kind, "provenance": provenance,
            "source_file": source_file, "def": None,
        })
        if len(raw) >= MAX_NODES_PER_PAGE:
            break
    raw.sort(key=lambda d: (d["usage"][0], d["name"]))
    return raw


# ── Definition-span resolution (1-hop, regex AST reuse) ──────────────────


_DEF_SPAN_CACHE: dict[str, dict[str, tuple[int, int]]] = {}


def _def_spans_of(repo_path: Path, rel: str) -> dict[str, tuple[int, int]]:
    """{symbol → (start, end)} for one source file via the house regex
    AST (``extract_signatures``) — cached per (scan-process, file)."""
    if rel in _DEF_SPAN_CACHE:
        return _DEF_SPAN_CACHE[rel]
    spans: dict[str, tuple[int, int]] = {}
    try:
        from faultline.analyzer.ast_extractor import extract_signatures

        sig = extract_signatures([rel], str(repo_path)).get(rel)
        for sr in (getattr(sig, "symbol_ranges", None) or []):
            name = getattr(sr, "name", "")
            if name and name not in spans:
                spans[name] = (int(sr.start_line), int(sr.end_line))
    except Exception:  # noqa: BLE001 — resolution is best-effort
        logger.debug("stage_6_55: def-span resolution failed for %s",
                     rel, exc_info=True)
    _DEF_SPAN_CACHE[rel] = spans
    return spans


# ── 1-hop barrel following (debt-pack, w4-report residual 4) ─────────────
# typebot-class: builder pages import blocks via deep workspace-package
# paths that resolve to package roots (index barrels re-exporting) — the
# def-span probe then misses (the barrel holds no definition) and the
# node degrades to a whole-file span. ONE hop through the barrel's own
# re-export statements recovers the real definition file. Regex over the
# barrel source (house regex-AST convention, no new parser); resolution
# probes the TRACKED set only (deterministic, no fs walks); bounded
# per-file and cached per scan process.

_BARREL_EXPORT_CACHE: dict[str, list[tuple[str, dict[str, str] | None]]] = {}
_BARREL_MAX_BYTES = 262_144  # a barrel is small by nature; cap the read
_BARREL_MAX_TARGETS = 24     # bound the star-export fan-out per symbol

_RE_EXPORT_NAMED = re.compile(
    r"export\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]")
_RE_EXPORT_STAR = re.compile(
    r"export\s+\*\s+from\s*['\"]([^'\"]+)['\"]")


def _barrel_exports(repo_path: Path, rel: str) -> list[
        tuple[str, dict[str, str] | None]]:
    """Parsed re-export statements of *rel*, in source order.

    Each entry is ``(spec, names)`` where ``names`` maps the EXPORTED
    name → the name inside the target (``export { A as B }`` → {"B":
    "A"}), and ``None`` means ``export * from`` (every name passes
    through unchanged).
    """
    if rel in _BARREL_EXPORT_CACHE:
        return _BARREL_EXPORT_CACHE[rel]
    out: list[tuple[str, dict[str, str] | None]] = []
    try:
        data = (repo_path / rel).read_bytes()
        if len(data) <= _BARREL_MAX_BYTES:
            text = data.decode("utf-8", errors="replace")
            events: list[tuple[int, str, dict[str, str] | None]] = []
            for m in _RE_EXPORT_NAMED.finditer(text):
                names: dict[str, str] = {}
                for item in m.group(1).split(","):
                    item = item.strip()
                    if not item or item.startswith("type "):
                        item = item.removeprefix("type ").strip()
                        if not item:
                            continue
                    if " as " in item:
                        orig, _, exported = item.partition(" as ")
                        names[exported.strip()] = orig.strip()
                    else:
                        names[item] = item
                if names:
                    events.append((m.start(), m.group(2), names))
            for m in _RE_EXPORT_STAR.finditer(text):
                events.append((m.start(), m.group(1), None))
            events.sort(key=lambda e: e[0])
            out = [(spec, names) for _, spec, names in events]
    except OSError:
        pass
    _BARREL_EXPORT_CACHE[rel] = out
    return out


def _def_span_via_barrel(
    repo_path: Path,
    barrel_rel: str,
    symbol: str,
    tracked: frozenset[str],
) -> tuple[str, tuple[int, int]] | None:
    """Resolve *symbol* through ONE hop of *barrel_rel*'s re-exports.

    Returns ``(real_source_file, def_span)`` when a target file defines
    the symbol, else ``None``. Named re-exports (exact match, alias
    honored) are tried before ``export *`` fan-out; targets resolve
    against the barrel's directory with the same suffix probing as
    imports (tracked set only).
    """
    base_dir = barrel_rel.rsplit("/", 1)[0] if "/" in barrel_rel else ""

    def _probe(spec: str) -> str | None:
        if not spec.startswith("."):
            return None  # one hop stays inside the barrel's package
        base = _norm_join(base_dir, spec)
        for suf in _CAND_SUFFIXES:
            cand = base + suf
            if cand in tracked and cand != barrel_rel:
                return cand
        return None

    candidates: list[tuple[str, str]] = []  # (target_rel, target_symbol)
    for spec, names in _barrel_exports(repo_path, barrel_rel):
        if names is not None:
            if symbol not in names:
                continue
            target = _probe(spec)
            if target:
                candidates.append((target, names[symbol]))
        else:
            target = _probe(spec)
            if target:
                candidates.append((target, symbol))
        if len(candidates) >= _BARREL_MAX_TARGETS:
            break
    for target, target_symbol in candidates:
        span = _def_spans_of(repo_path, target).get(target_symbol)
        if span:
            return target, span
    return None


# ── Content-hash parse cache ─────────────────────────────────────────────


def _cache_key(source: bytes) -> str:
    h = hashlib.sha256()
    h.update(PARSER_VERSION.encode())
    h.update(b"\x00")
    h.update(_grammar_signature().encode())
    h.update(b"\x00")
    h.update(source)
    return h.hexdigest()


def _parse_cached(
    ctx: "ScanContext",
    rel_path: str,
    source: bytes,
    tracked: frozenset[str],
    ws_by_name: dict[str, str],
    ws_by_path: dict[str, str],
    stats: dict[str, int],
) -> list[dict[str, Any]]:
    backend = getattr(ctx, "cache_backend", None)
    key = _cache_key(source)
    if backend is not None:
        try:
            hit = backend.get(CacheKind.INTERIOR, key)
            if isinstance(hit, dict) and isinstance(hit.get("nodes"), list):
                stats["cache_hits"] += 1
                return hit["nodes"]
        except Exception:  # noqa: BLE001 — cache faults never break a scan
            pass
    nodes = _parse_page_source(
        rel_path, source, tracked, ws_by_name, ws_by_path)
    stats["parsed"] += 1
    if backend is not None:
        try:
            backend.set(CacheKind.INTERIOR, key, {"nodes": nodes})
        except Exception:  # noqa: BLE001
            pass
    return nodes


# ── Page enumeration ─────────────────────────────────────────────────────


def _enumerate_pages(
    routes_index: list[dict[str, Any]],
    tracked: frozenset[str],
) -> list[tuple[str, str]]:
    """[(repo-relative file, "page"|"layout")] — PAGE routes plus their
    sibling layout files, TS/TSX/JSX only, sorted."""
    pages: dict[str, str] = {}
    for entry in routes_index or []:
        if str(entry.get("method") or "").upper() != "PAGE":
            continue
        f = str(entry.get("file") or "")
        if f and f.lower().endswith(_PAGE_EXTS):
            pages.setdefault(f, "page")
    for f in sorted(pages):
        d = f.rsplit("/", 1)[0] if "/" in f else ""
        for ext in _PAGE_EXTS:
            layout = (d + "/" if d else "") + "layout" + ext
            if layout in tracked:
                pages.setdefault(layout, "layout")
    return sorted(pages.items())


# ── Result assembly ──────────────────────────────────────────────────────


_MEMO: dict[str, InteriorResult] = {}
_MEMO_ORDER: list[str] = []
_MEMO_CAP = 4


def _family_label(comp_names: list[str], labels: list[str]) -> str:
    for lab in labels:
        if lab:
            return lab
    if comp_names:
        # De-camel the first component name ("DatabaseBackups" →
        # "Database Backups").
        return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", comp_names[0])
    return ""


def _build_families(pages: dict[str, PageInterior]) -> tuple[InteriorFamily, ...]:
    """Product components grouped into family UNITS; families span ≥2 pages.

    Unit grain (container-sink guard):
      * ``index.*`` source — the component owns its dir → DIR unit
        (subtree claim legal);
      * any other source — FILE unit (the file lives inside a bigger
        container; claiming the container is the D1 sink class).
    A DIR unit that contains ANOTHER family's claim is dropped (barrel
    ``index.ts`` at a container level must not swallow its children —
    nested families collapse to the FINEST grain, the opposite of the
    hub rule's shallowest-dir on purpose: hubs are author-declared
    containers, interior families are evidence units).
    """
    units: dict[tuple[str, str], dict[str, Any]] = {}
    for page_file in sorted(pages):
        for n in pages[page_file].nodes:
            if (n.kind != "component" or n.provenance != "product"
                    or not n.source_file):
                continue
            src = n.source_file
            d = src.rsplit("/", 1)[0] if "/" in src else ""
            if not d or len(d.split("/")) < 2:
                continue  # repo-top dirs are never families
            base = src.rsplit("/", 1)[-1]
            dir_owned = base.rsplit(".", 1)[0].lower() == "index"
            key = ("dir", d) if dir_owned else ("file", src)
            slot = units.setdefault(key, {
                "components": set(), "pages": set(),
                "sources": set(), "labels": [],
            })
            slot["components"].add(n.name.split(".", 1)[0])
            slot["pages"].add(page_file)
            slot["sources"].add(src)
            if n.label:
                slot["labels"].append(n.label)

    qualified = {
        key: slot for key, slot in units.items()
        if len(slot["pages"]) >= MIN_FAMILY_PAGES
    }
    # Finest-grain collapse: a dir unit containing another qualified
    # unit's claim is a container — drop it.
    claim_paths = [
        (key[1] if key[0] == "dir" else key[1]) for key in qualified
    ]
    out: list[InteriorFamily] = []
    for kind, path in sorted(qualified):
        if kind == "dir" and any(
            c != path and c.startswith(path + "/") for c in claim_paths
        ):
            continue
        slot = qualified[(kind, path)]
        comps = sorted(slot["components"])
        out.append(InteriorFamily(
            family_dir=(path if kind == "dir"
                        else path.rsplit("/", 1)[0]),
            component_names=tuple(comps),
            page_files=tuple(sorted(slot["pages"])),
            source_files=tuple(sorted(slot["sources"])),
            label=_family_label(comps, sorted(slot["labels"])),
            dir_owned=(kind == "dir"),
        ))
    return tuple(out)


def get_page_interiors(
    ctx: "ScanContext",
    routes_index: list[dict[str, Any]] | None,
) -> InteriorResult:
    """Parse (or replay from memo/cache) every page interior for this scan.

    Deterministic + idempotent: safe to call from BOTH the stage runner
    and ``spine_anchors`` — the second call is a memo hit.
    """
    if not stage_6_55_enabled():
        return InteriorResult(active=False, reason="disabled via FAULTLINE_STAGE_6_55")
    if not is_active(ctx):
        return InteriorResult(
            active=False,
            reason="tree-sitter (or web grammars) not installed — "
                   "pip install 'faultlines[ast]'",
        )

    tracked = frozenset(
        str(p).replace("\\", "/")
        for p in (getattr(ctx, "tracked_files", None) or [])
    )
    pages_list = _enumerate_pages(routes_index or [], tracked)

    memo_key = hashlib.sha256(json.dumps(
        [str(getattr(ctx, "repo_path", "")), pages_list, PARSER_VERSION],
        sort_keys=True).encode()).hexdigest()
    if memo_key in _MEMO:
        return _MEMO[memo_key]

    ws_by_name: dict[str, str] = {}
    ws_by_path: dict[str, str] = {}
    for w in (getattr(ctx, "workspaces", None) or []):
        name = str(getattr(w, "name", "") or "")
        path = str(getattr(w, "path", "") or "").strip("/")
        if path:
            base = path.rsplit("/", 1)[-1].lower()
            ws_by_path[path] = base
            if name:
                ws_by_name[name] = path

    repo_path = Path(getattr(ctx, "repo_path", "."))
    stats = {"parsed": 0, "cache_hits": 0, "skipped_big": 0,
             "read_errors": 0}
    pages: dict[str, PageInterior] = {}
    for rel, page_kind in pages_list:
        try:
            data = (repo_path / rel).read_bytes()
        except OSError:
            stats["read_errors"] += 1
            continue
        if len(data) > MAX_PAGE_BYTES:
            stats["skipped_big"] += 1
            continue
        node_dicts = _parse_cached(
            ctx, rel, data, tracked, ws_by_name, ws_by_path, stats)
        nodes: list[InteriorNode] = []
        for d in node_dicts:
            source_file = d.get("source_file")
            def_span: tuple[int, int] | None = None
            if (d["kind"] == "component" and source_file
                    and d["provenance"] == "product"):
                head = d["name"].split(".", 1)[0]
                spans = _def_spans_of(repo_path, source_file)
                def_span = spans.get(head)
                if def_span is None:
                    # 1-hop barrel follow (debt-pack): index re-exports
                    # hold no definition — chase the real source file so
                    # the node carries a definition span instead of
                    # degrading to a whole-file claim on the barrel.
                    hop = _def_span_via_barrel(
                        repo_path, source_file, head, tracked)
                    if hop is not None:
                        source_file, def_span = hop
            nodes.append(InteriorNode(
                kind=d["kind"], name=d["name"], label=d.get("label"),
                usage_line_start=int(d["usage"][0]),
                usage_line_end=int(d["usage"][1]),
                source_kind=d["source_kind"], provenance=d["provenance"],
                source_file=source_file,
                def_line_start=def_span[0] if def_span else None,
                def_line_end=def_span[1] if def_span else None,
            ))
        pages[rel] = PageInterior(
            file=rel, page_kind=page_kind, nodes=tuple(nodes))

    families = _build_families(pages)
    n_nodes = sum(len(p.nodes) for p in pages.values())
    n_product = sum(
        1 for p in pages.values() for n in p.nodes
        if n.kind == "component" and n.provenance == "product")
    n_design = sum(
        1 for p in pages.values() for n in p.nodes
        if n.kind == "component" and n.provenance == "design_system")
    result = InteriorResult(
        active=True,
        pages=pages,
        families=families,
        telemetry={
            "pages_seen": len(pages_list),
            "pages_parsed": len(pages),
            "interior_nodes": n_nodes,
            "product_components": n_product,
            "design_system_components": n_design,
            "families": len(families),
            "family_sample": [f.family_dir for f in families[:8]],
            **stats,
        },
    )
    _MEMO[memo_key] = result
    _MEMO_ORDER.append(memo_key)
    while len(_MEMO_ORDER) > _MEMO_CAP:
        _MEMO.pop(_MEMO_ORDER.pop(0), None)
    return result


# ── Stage runner (telemetry wrapper) ─────────────────────────────────────


def run_stage_6_55(
    ctx: "ScanContext",
    routes_index: list[dict[str, Any]] | None,
    log: "StageLogger | None" = None,
) -> InteriorResult:
    result = get_page_interiors(ctx, routes_index)
    if log is not None:
        if result.active:
            log.info(
                "stage_6_55: pages=%d nodes=%d product=%d design=%d "
                "families=%d cache_hits=%d" % (
                    result.telemetry.get("pages_parsed", 0),
                    result.telemetry.get("interior_nodes", 0),
                    result.telemetry.get("product_components", 0),
                    result.telemetry.get("design_system_components", 0),
                    result.telemetry.get("families", 0),
                    result.telemetry.get("cache_hits", 0),
                ),
            )
        else:
            log.info(f"stage_6_55 inactive: {result.reason}")
    return result


# ── Flow-span consumers ──────────────────────────────────────────────────


def _iter_flows(features: list["Feature"]):
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        for flow in getattr(f, "flows", None) or []:
            yield flow


def refine_flow_spans(
    features: list["Feature"],
    interior: InteriorResult,
) -> dict[str, Any]:
    """Pre-Stage-3.5 span refinement (§4.6 scope 3, first half).

    For every flow whose entry file is a parsed page: append ONE
    ``role="interior"`` :class:`FlowSymbolAttribution` per resolved
    PRODUCT component the page renders (definition span in the
    component's OWN file — the 1-hop imported product-module span).
    Deterministic order, capped, deduped against existing attributions.
    """
    from faultline.models.types import FlowSymbolAttribution

    tele = {"flows_touched": 0, "interior_attributions": 0}
    if not interior.active or not interior.pages:
        return tele
    for flow in _iter_flows(features):
        entry = getattr(flow, "entry_point_file", None)
        page = interior.pages.get(entry or "")
        if page is None:
            continue
        existing = {
            (a.file, a.symbol)
            for a in (getattr(flow, "flow_symbol_attributions", None) or [])
        }
        added = 0
        for n in page.nodes:
            if added >= MAX_INTERIOR_PER_FLOW:
                break
            if (n.kind != "component" or n.provenance != "product"
                    or not n.source_file or n.def_line_start is None):
                continue
            symbol = n.name.split(".", 1)[0]
            if (n.source_file, symbol) in existing:
                continue
            existing.add((n.source_file, symbol))
            flow.flow_symbol_attributions = list(
                getattr(flow, "flow_symbol_attributions", None) or []
            ) + [FlowSymbolAttribution(
                file=n.source_file, symbol=symbol,
                line_start=int(n.def_line_start),
                line_end=int(n.def_line_end or n.def_line_start),
                role="interior",
            )]
            added += 1
        if added:
            tele["flows_touched"] += 1
            tele["interior_attributions"] += added
    return tele


def inject_interior_nodes(
    features: list["Feature"],
    interior: InteriorResult,
) -> dict[str, Any]:
    """Post-Stage-3.5 node surface (§4.6 scope 3, second half).

    (a) every ``role="interior"`` attribution becomes a ``FlowNode`` so
    ``line_ranges`` / LOC accounting see the real component spans;
    (b) whole-file ``kind="file"`` support nodes whose file is a resolved
    interior component source are TIGHTENED to the definition span.
    Re-projects the Phase-5 LOC views afterwards (idempotent projection).
    """
    from faultline.models.types import FlowNode
    from faultline.pipeline_v2.flow_expansion.expander import (
        _project_loc_detail,
    )

    tele = {"nodes_added": 0, "support_tightened": 0, "flows_touched": 0}
    if not interior.active or not interior.pages:
        return tele

    # (file → {symbol → def span}) over every resolved product component.
    def_spans: dict[str, dict[str, tuple[int, int]]] = {}
    for p in interior.pages.values():
        for n in p.nodes:
            if (n.kind == "component" and n.provenance == "product"
                    and n.source_file and n.def_line_start is not None):
                def_spans.setdefault(n.source_file, {})[
                    n.name.split(".", 1)[0]
                ] = (int(n.def_line_start), int(n.def_line_end or n.def_line_start))

    for flow in _iter_flows(features):
        touched = False
        nodes = list(getattr(flow, "nodes", None) or [])
        node_ids = {n.id for n in nodes}

        for a in (getattr(flow, "flow_symbol_attributions", None) or []):
            if getattr(a, "role", None) != "interior":
                continue
            nid = f"{a.file}#{a.symbol}"
            if nid in node_ids:
                continue
            node_ids.add(nid)
            nodes.append(FlowNode(
                id=nid, kind="function", file=a.file, symbol=a.symbol,
                lines=(int(a.line_start), int(a.line_end)),
                role="interior", confidence="high",
            ))
            tele["nodes_added"] += 1
            touched = True

        for i, n in enumerate(nodes):
            if n.kind != "file" or n.role not in ("support", "shared"):
                continue
            spans = def_spans.get(n.file)
            if not spans:
                continue
            # The tightened span = union of the component definitions this
            # scan actually resolved in that file (real evidence), only
            # when it is STRICTLY narrower than the current whole-file span.
            start = min(s for s, _ in spans.values())
            end = max(e for _, e in spans.values())
            cur = n.lines
            if cur and (end - start) < (cur[1] - cur[0]):
                nodes[i] = n.model_copy(update={
                    "lines": (start, end),
                    "symbol": n.symbol or "+".join(sorted(spans)),
                    "confidence": "high",
                })
                tele["support_tightened"] += 1
                touched = True

        if touched:
            flow.nodes = nodes
            if flow.summary is not None:
                total_lines = sum(
                    max(0, n.lines[1] - n.lines[0] + 1)
                    for n in nodes if n.lines is not None
                )
                flow.summary = flow.summary.model_copy(update={
                    "total_nodes": len(nodes),
                    "total_files": len({n.file for n in nodes}),
                    "total_lines_touched": total_lines,
                })
            try:
                _project_loc_detail(flow)
            except Exception:  # noqa: BLE001 — projection is additive
                logger.debug("stage_6_55: loc re-projection failed",
                             exc_info=True)
            tele["flows_touched"] += 1
    return tele


# ── 6.7d evidence feed (W4 §4.6 — journey citation vocabulary) ───────────

_MAX_SECTIONS_PER_PAGE = 8
_MAX_SECTIONS_PER_PF = 8


def build_interior_evidence(
    interior: InteriorResult,
    features: list["Feature"],
    product_features: list["Feature"],
) -> dict[str, Any] | None:
    """Per-PF interior sections for the constrained 6.7d Call-1.

    ``{"by_pf": {pf_display: [section labels]}, "pages": {page: {"pf":
    pf_display, "sections": [labels]}}}`` — a page contributes ONLY to
    the PF that owns it (primary dev ownership → PF stamp), so cited
    sections are verifiable against ONE PF's subtree. ``None`` when the
    stage was inactive or nothing resolved (the digest/prompt/cache
    stay byte-identical to pre-W4 then).
    """
    if not interior.active or not interior.pages:
        return None
    from faultline.pipeline_v2.flow_span_split import _owner_map

    owner = _owner_map(features)
    pf_display: dict[str, str] = {}
    for pf in product_features or []:
        k = getattr(pf, "id", None) or getattr(pf, "name", None)
        if k:
            pf_display[k] = (getattr(pf, "display_name", None)
                             or getattr(pf, "name", "") or str(k))
    pages: dict[str, dict[str, Any]] = {}
    by_pf: dict[str, list[str]] = {}
    for page in sorted(interior.pages):
        disp = pf_display.get(owner.get(page) or "")
        if not disp:
            continue
        secs: list[str] = []
        seen: set[str] = set()
        for n in interior.pages[page].nodes:
            if n.provenance != "product":
                continue
            text = (n.label or n.name or "").strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            secs.append(text)
            if len(secs) >= _MAX_SECTIONS_PER_PAGE:
                break
        if not secs:
            continue
        pages[page] = {"pf": disp, "sections": secs}
        slot = by_pf.setdefault(disp, [])
        for text in secs:
            if text not in slot and len(slot) < _MAX_SECTIONS_PER_PF:
                slot.append(text)
    if not pages:
        return None
    return {"by_pf": by_pf, "pages": pages}


# ── Degenerate-span ruler (before/after telemetry) ───────────────────────


def degenerate_span_stats(features: list["Feature"]) -> dict[str, Any]:
    """% of flows whose TOTAL merged span is ≤ 2 lines (the wrapper-span
    class; supabase baseline 21.1%). Span source mirrors the validator's
    on-flow ruler: ``nodes[].lines`` merged per file; flows with no lined
    nodes fall back to attribution spans; flows with neither are counted
    separately (``no_span``)."""
    total = degenerate = no_span = 0
    for flow in _iter_flows(features):
        spans_by_file: dict[str, list[tuple[int, int]]] = {}
        for n in (getattr(flow, "nodes", None) or []):
            lines = getattr(n, "lines", None)
            if lines:
                spans_by_file.setdefault(n.file, []).append(
                    (int(lines[0]), int(lines[1])))
        if not spans_by_file:
            for a in (getattr(flow, "flow_symbol_attributions", None) or []):
                spans_by_file.setdefault(a.file, []).append(
                    (int(a.line_start), int(a.line_end)))
        total += 1
        if not spans_by_file:
            no_span += 1
            continue
        loc = 0
        for file_spans in spans_by_file.values():
            merged: list[tuple[int, int]] = []
            for s, e in sorted(file_spans):
                if merged and s <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            loc += sum(e - s + 1 for s, e in merged)
        if loc <= 2:
            degenerate += 1
    share = round(degenerate / total, 4) if total else 0.0
    return {"flows": total, "degenerate": degenerate,
            "no_span": no_span, "degenerate_share": share}
