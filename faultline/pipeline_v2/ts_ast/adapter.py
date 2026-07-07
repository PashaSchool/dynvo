"""W6-AST M4 — the bridge from the symbol graph into existing consumers.

The adapter returns the SAME shapes the regex ``ast_extractor`` path
gives its consumers, so downstream code never learns the extraction
changed (spec §3 M4). Every entry point degrades to ``None`` — meaning
"caller keeps the legacy regex result" — whenever the master flag is
off, the M1-M3 modules are absent, or a file fails to parse
(fallback law, spec §2).

Consumer entry points
=====================

* :func:`ast_symbol_ranges` — per-file drop-in upgrade for
  ``analyzer.ast_extractor.extract_symbol_ranges`` output (Hook A).
  Feeds EVERY span consumer through ``FileSignature.symbol_ranges``:
  flow_expansion/call_graph (FlowNode.lines → W4 flow-span split),
  symbols/extractor (SymbolAttribution.line_ranges), stage_3_flows,
  profiles/_flow_lines, stage_6_55 ``_def_spans_of``.
* :func:`defspans_to_flow_spans` — the same mapping graph-wide:
  ``{file → list[SymbolRange]}`` in exact regex ordering discipline
  (top-level sorted by start line first, methods appended after —
  ``_range_for_symbol`` first-match and ``symbol_lines`` last-write
  semantics both depend on it).
* :func:`provenance_view` / :func:`repo_provenance` /
  :func:`current_provenance` — resolved-import provenance for the W2b
  filter (stage_6_55 ``_resolve_spec`` contract:
  ``(source_file | None, source_kind)``) and the S2 instrument
  detector (``_resolve_one`` contract: tracked file or ``None``;
  ``spec_occurrences`` mirrors the one-entry-per-imported-name
  weighting of ``_SourceCache.imports(rel).values()``).
* :func:`entry_signals` — [FAULTLINE_TS_AST_ENTRY] skeleton only; the
  entry migration is a separate, later decision (spec §4).

Injected pipeline contract (M1-M3)
==================================

:func:`build_symbol_graph` takes the four stage functions by INJECTION
so it can be developed and tested against stubs; the integrator wires
the real modules in :func:`_load_real_fns` (single place):

* ``parse_fn(repo_root: str, rel_path: str, source: bytes | None = None)
  -> FileParse | None`` — M1 ``parse.parse_file`` (owns IO + the
  CacheKind.AST cache; ``source`` is an optional pre-read so callers
  that already hold the bytes avoid double IO; ``None`` = parse fail).
* ``defs_fn(fp: FileParse) -> Sequence[DefSpan]`` — M1
  ``defs.extract_defs``.
* ``imports_fn(fp: FileParse) -> tuple[Sequence[ImportEdge],
  Sequence[ExportEntry]]`` — M2 ``imports.extract_imports``.
* ``resolve_fn(repo_root: str, edges: Sequence[ImportEdge],
  exports_index: Mapping[str, Sequence[ExportEntry]],
  tracked_files: Sequence[str]) -> Sequence[ResolvedEdge]`` — M3
  ``resolve.resolve_edges`` (batch: alias/workspace maps compiled once).

Determinism: inputs are iterated in sorted order, every output list is
canonically sorted (shapes.py keys), and no set iteration reaches any
output (spec §2).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from faultline.pipeline_v2.ts_ast.shapes import (
    DefSpan,
    ExportEntry,
    FileParse,
    ImportEdge,
    ResolvedEdge,
    SymbolGraph,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import SymbolRange

logger = logging.getLogger(__name__)

__all__ = [
    "GRAPH_VER",
    "INTERIOR_KEY_TAG",
    "TS_AST_ENTRY_ENV",
    "TS_AST_ENV",
    "ProvenanceView",
    "ast_symbol_ranges",
    "build_symbol_graph",
    "current_provenance",
    "defspans_to_flow_spans",
    "entry_signals",
    "provenance_view",
    "repo_provenance",
    "reset_ts_ast_state",
    "ts_ast_enabled",
    "ts_ast_entry_enabled",
    "wrapper_channel",
]

#: Master flag (spec §2). Default ON; ``=0`` → every consumer takes
#: EXACTLY the legacy regex path (byte-identical kill-switch).
TS_AST_ENV = "FAULTLINE_TS_AST"
#: Entry-detection migration flag — separate, default OFF (spec §2/§4).
TS_AST_ENTRY_ENV = "FAULTLINE_TS_AST_ENTRY"

#: Adapter/version stamp — participates in graph telemetry and the
#: stage-6.55 INTERIOR cache-key namespace (bump on mapping changes).
GRAPH_VER = "w6ast-m4-1"

#: Prefix mixed into the CacheKind.INTERIOR key by the 6.55 hook while a
#: graph-backed provenance view is ACTIVE, so flag-ON entries never
#: collide with legacy entries (kill-switch byte-identity would break
#: if ``FAULTLINE_TS_AST=0`` re-runs read nodes cached under flag-ON).
INTERIOR_KEY_TAG = GRAPH_VER + ":"

_FALSY = frozenset({"0", "false", "no", "off"})

#: TS/JS suffixes the AST layer owns. ``.d.ts`` (and friends) are
#: declaration files — skipped by design (spec §3 M5 fixture list).
_TS_JS_SUFFIXES = (
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs",
)
_DECL_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")


def ts_ast_enabled() -> bool:
    return (os.environ.get(TS_AST_ENV, "1") or "1").strip().lower() \
        not in _FALSY


def ts_ast_entry_enabled() -> bool:
    return (os.environ.get(TS_AST_ENTRY_ENV, "0") or "0").strip().lower() \
        not in _FALSY | {""}


def _is_ts_js(rel_path: str) -> bool:
    low = rel_path.lower()
    return low.endswith(_TS_JS_SUFFIXES) and not low.endswith(_DECL_SUFFIXES)


# ── Graph assembly (M1-M3 injected) ──────────────────────────────────────


ParseFn = Callable[..., "FileParse | None"]
DefsFn = Callable[[FileParse], Sequence[DefSpan]]
ImportsFn = Callable[
    [FileParse], "tuple[Sequence[ImportEdge], Sequence[ExportEntry]]",
]
ResolveFn = Callable[..., Sequence[ResolvedEdge]]


def build_symbol_graph(
    repo_root: str,
    files: Iterable[str],
    *,
    parse_fn: ParseFn,
    defs_fn: DefsFn,
    imports_fn: ImportsFn,
    resolve_fn: ResolveFn,
) -> SymbolGraph:
    """Assemble the per-repo :class:`SymbolGraph` from injected stages.

    ``files`` is the tracked-file population (any language — non-TS/JS
    and ``.d.ts`` entries are counted and skipped here, but the FULL
    sorted list is still handed to ``resolve_fn`` so relative /
    barrel / workspace probing sees every tracked path).

    A per-file parse failure NEVER fails the build: the file simply
    stays regex-territory (``telemetry["parse_failures"]``++ and it is
    absent from ``parsed_files`` — provenance consumers fall back
    per-file on that basis).
    """
    tracked_sorted = sorted(str(f).replace("\\", "/") for f in files)
    graph = SymbolGraph()
    parsed_files: list[str] = []
    failed_files: list[str] = []
    dts_skipped = 0
    non_ts_skipped = 0

    exports_by_file: dict[str, list[ExportEntry]] = {}
    for rel in tracked_sorted:
        if not _is_ts_js(rel):
            if rel.lower().endswith(_DECL_SUFFIXES):
                dts_skipped += 1
            else:
                non_ts_skipped += 1
            continue
        try:
            fp = parse_fn(repo_root, rel)
        except Exception:  # noqa: BLE001 — parse faults degrade per file
            logger.debug("ts_ast: parse_fn raised for %s", rel, exc_info=True)
            fp = None
        if fp is None:
            failed_files.append(rel)
            continue
        parsed_files.append(rel)
        try:
            graph.defs.extend(defs_fn(fp))
        except Exception:  # noqa: BLE001
            logger.debug("ts_ast: defs_fn raised for %s", rel, exc_info=True)
        try:
            edges, exports = imports_fn(fp)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ts_ast: imports_fn raised for %s", rel, exc_info=True,
            )
            edges, exports = (), ()
        graph.edges.extend(edges)
        for entry in exports:
            exports_by_file.setdefault(entry.file, []).append(entry)

    graph.exports_index = exports_by_file
    graph.sort_canonical()

    try:
        graph.resolved = list(resolve_fn(
            repo_root, graph.edges, graph.exports_index, tracked_sorted,
        ))
    except Exception:  # noqa: BLE001 — resolution faults degrade whole-graph
        logger.debug("ts_ast: resolve_fn raised", exc_info=True)
        graph.resolved = []
    graph.sort_canonical()

    histogram: dict[str, int] = {}
    for r in graph.resolved:
        histogram[r.resolution] = histogram.get(r.resolution, 0) + 1
    graph.telemetry = {
        "graph_ver": GRAPH_VER,
        "files_seen": len(tracked_sorted),
        "files_parsed": len(parsed_files),
        "parse_failures": len(failed_files),
        "dts_skipped": dts_skipped,
        "non_ts_skipped": non_ts_skipped,
        "parsed_files": parsed_files,          # already sorted (input order)
        "failed_files": failed_files,          # already sorted
        "defs": len(graph.defs),
        "edges": len(graph.edges),
        "resolved_total": len(graph.resolved),
        "resolution_histogram": {k: histogram[k] for k in sorted(histogram)},
    }
    return graph


# ── (a) Def-spans → the W4 flow-span consumer shape ──────────────────────


# AMENDMENT-1 §2 — the DOCUMENTED DefSpan.kind → legacy SymbolRange.kind
# table (M4 mapping authority). LAW: flow-eligible semantics
# (symbols/extractor _FLOW_ELIGIBLE_KINDS == {function, class, const})
# MUST match legacy.
#
#   DefSpan.kind  exported  parent  →  SymbolRange.kind
#   ------------  --------  ------     -----------------------------------
#   function      yes       —          function
#   class         yes       —          class
#   const         yes       —          const
#   component     yes       —          the form LEGACY saw for this name:
#                                      regex kind when ∈ {const, function};
#                                      else wrapper≠none → const (wrapped
#                                      components are const-assignments),
#                                      else → function (fn-declaration)
#   enum          any       —          enum   (NEVER flow-eligible)
#   any other     no        —          local  (regex parity: locals stay
#                                      out of flow_symbols; call-graph
#                                      handler resolution understands them)
#   any           any       set        constructor if name=="constructor"
#                                      else method
#
# Wrapper info (forwardRef/memo/hoc/styled) is NOT folded into the kind —
# it travels on the SEPARATE channel :func:`wrapper_channel` for W4-span
# consumers (AMENDMENT-1 §2). The kill-switch byte-law is untouched by
# all of this: flag=0 never reaches this code.
_KIND_MAP = {
    "function": "function",
    "class": "class",
    "const": "const",
    "method": "method",
    "enum": "enum",
}

_LEGACY_COMPONENT_FORMS = frozenset({"const", "function"})


def _symbol_range_of(
    d: DefSpan,
    legacy_kinds: Mapping[str, str] | None = None,
) -> "SymbolRange":
    from faultline.models.types import SymbolRange

    if d.parent is not None:
        kind = "constructor" if d.name == "constructor" else "method"
        return SymbolRange(
            name=d.name, start_line=d.start_line, end_line=d.end_line,
            kind=kind, parent=d.parent,
        )
    if d.kind == "enum":
        kind = "enum"
    elif not d.exported:
        kind = "local"
    elif d.kind == "component":
        legacy = (legacy_kinds or {}).get(d.name)
        if legacy in _LEGACY_COMPONENT_FORMS:
            kind = str(legacy)
        else:
            kind = "const" if d.wrapper != "none" else "function"
    else:
        kind = _KIND_MAP.get(d.kind, d.kind)
    return SymbolRange(
        name=d.name, start_line=d.start_line, end_line=d.end_line, kind=kind,
    )


def wrapper_channel(graph: SymbolGraph) -> dict[str, dict[str, str]]:
    """AMENDMENT-1 §2 — the separate wrapper channel for W4-span work.

    ``{file → {symbol name → wrapper}}`` for every def whose wrapper is
    not ``none`` (forwardRef / memo / hoc / styled). The legacy
    ``SymbolRange`` shape stays untouched — consumers that care about
    wrapper provenance (M5 wrapper-% metrics, W4 span QA) read it here.
    """
    out: dict[str, dict[str, str]] = {}
    for d in sorted(graph.defs, key=_def_wrapper_key):
        if d.wrapper != "none" and d.parent is None:
            out.setdefault(d.file, {})[d.name] = d.wrapper
    return out


def _def_wrapper_key(d: DefSpan) -> tuple[str, str, int]:
    return (d.file, d.name, d.start_line)


def _merge_ranges(
    ast_defs: Sequence[DefSpan],
    regex_ranges: Sequence["SymbolRange"],
) -> list["SymbolRange"]:
    """Merge AST def-spans with the regex ranges of the same file.

    AST wins by name; regex-only entries survive (type / interface /
    enum / reexport spans and anything M1 does not model). Ordering
    discipline mirrors the regex extractor EXACTLY: top-level symbols
    sorted by start line (name-deduped, first wins), THEN method-level
    symbols — ``_range_for_symbol`` first-match must find the top-level
    symbol and ``symbol_lines`` last-write must keep the method range.
    """
    top_ast = sorted(
        (d for d in ast_defs if d.parent is None),
        key=lambda d: (d.start_line, d.end_line, d.name),
    )
    meth_ast = sorted(
        (d for d in ast_defs if d.parent is not None),
        key=lambda d: (d.parent or "", d.start_line, d.end_line, d.name),
    )
    # AMENDMENT-1 §2: components adopt the kind LEGACY used for the same
    # name (arrow-component 'const' vs fn-declaration 'function').
    legacy_kinds = {
        r.name: r.kind for r in regex_ranges
        if not getattr(r, "parent", None)
    }

    top: list["SymbolRange"] = []
    seen_top: set[str] = set()
    for d in top_ast:
        if d.name in seen_top:
            continue
        seen_top.add(d.name)
        top.append(_symbol_range_of(d, legacy_kinds))
    methods: list["SymbolRange"] = []
    seen_meth: set[tuple[str, str]] = set()
    for d in meth_ast:
        key = (d.parent or "", d.name)
        if key in seen_meth:
            continue
        seen_meth.add(key)
        methods.append(_symbol_range_of(d, legacy_kinds))

    for r in regex_ranges:
        parent = getattr(r, "parent", None)
        if parent:
            if (parent, r.name) not in seen_meth:
                seen_meth.add((parent, r.name))
                methods.append(r)
        elif r.name not in seen_top:
            seen_top.add(r.name)
            top.append(r)

    top.sort(key=lambda r: (r.start_line, r.name))
    methods.sort(key=lambda r: (r.parent or "", r.start_line, r.name))
    return top + methods


def defspans_to_flow_spans(
    graph: SymbolGraph,
    regex_ranges_by_file: Mapping[str, Sequence["SymbolRange"]] | None = None,
) -> dict[str, list["SymbolRange"]]:
    """Graph-wide ``{file → list[SymbolRange]}`` in the consumer shape.

    This is the exact shape the W4 flow-span mechanics eat via
    ``FileSignature.symbol_ranges`` (see module docstring for the
    consumer list). ``regex_ranges_by_file`` (when given) contributes
    the regex-only names per :func:`_merge_ranges`.
    """
    by_file: dict[str, list[DefSpan]] = {}
    for d in graph.defs:
        by_file.setdefault(d.file, []).append(d)
    files = set(by_file)
    if regex_ranges_by_file is not None:
        files |= set(regex_ranges_by_file)
    out: dict[str, list["SymbolRange"]] = {}
    for f in sorted(files):
        regex = (regex_ranges_by_file or {}).get(f, ())
        out[f] = _merge_ranges(by_file.get(f, ()), regex)
    return out


def ast_symbol_ranges(
    repo_root: str,
    rel_path: str,
    source: str | bytes | None,
    regex_ranges: Sequence["SymbolRange"],
) -> list["SymbolRange"] | None:
    """Per-file drop-in for Hook A (``extract_signatures``).

    Returns the upgraded ``symbol_ranges`` list, or ``None`` when the
    caller must keep its regex result (flag off / M1 unavailable /
    parse failure / not a TS-JS file). Never raises.
    """
    if not ts_ast_enabled() or not _is_ts_js(rel_path):
        return None
    fns = _load_real_fns()
    if fns is None:
        return None
    try:
        raw = source.encode("utf-8", errors="ignore") \
            if isinstance(source, str) else source
        fp = fns.parse_fn(repo_root, rel_path, raw)
        if fp is None:
            return None
        return _merge_ranges(list(fns.defs_fn(fp)), regex_ranges)
    except Exception:  # noqa: BLE001 — fallback law: regex path survives
        logger.debug(
            "ts_ast: ast_symbol_ranges failed for %s", rel_path,
            exc_info=True,
        )
        return None


# ── (b) Import-provenance view (W2b filter + S2 instrument detector) ─────


#: graph resolution → stage_6_55 ``_resolve_spec`` source_kind vocabulary.
_KIND_6_55 = {
    "relative": "local",
    "tsconfig_alias": "workspace",
    "workspace": "workspace",
    "package_external": "package",
    "unresolved": "unresolved",
}

#: Resolutions whose ``target_file`` satisfies the S2 ``_resolve_one``
#: contract (a tracked repo file). Externals answer ``None`` there —
#: S2 then runs its own external-dep classification on the raw spec.
_IN_REPO_RESOLUTIONS = frozenset({"relative", "tsconfig_alias", "workspace"})


def _runtime_names(names: Sequence[str]) -> tuple[str, ...]:
    """Drop ``type:``-prefixed bindings (erased at runtime — spec §3 M2)."""
    return tuple(n for n in names if not n.startswith("type:"))


@dataclass
class ProvenanceView:
    """Resolved-import map in the shapes the two consumers already eat."""

    tracked_key: frozenset[str]
    files: frozenset[str]
    _by_src: dict[str, dict[str, ResolvedEdge]] = field(default_factory=dict)
    _weights: dict[str, dict[str, int]] = field(default_factory=dict)

    def raw_specs(self, src_file: str) -> list[str]:
        """Sorted UNIQUE raw import specifiers of one parsed file."""
        return sorted(self._by_src.get(src_file, {}))

    def spec_occurrences(self, src_file: str) -> list[str]:
        """Specs with per-imported-name multiplicity (S2 weighting).

        Mirrors ``_SourceCache.imports(rel).values()`` — one occurrence
        per runtime binding name; side-effect / star edges count once.
        """
        weights = self._weights.get(src_file, {})
        out: list[str] = []
        for spec in sorted(weights):
            out.extend([spec] * weights[spec])
        return out

    def resolve(self, src_file: str, spec: str) -> str | None:
        """S2 / ``_resolve_one`` contract: tracked file or ``None``."""
        edge = self._by_src.get(src_file, {}).get(spec)
        if edge is None or edge.resolution not in _IN_REPO_RESOLUTIONS:
            return None
        return edge.target_file

    def lookup(self, src_file: str, spec: str) -> tuple[str | None, str] | None:
        """6.55 ``_resolve_spec`` contract: ``(source_file, source_kind)``.

        ``None`` (file or spec unknown to the graph) → caller falls back
        to the legacy resolver for THAT spec (never lose coverage).
        """
        edge = self._by_src.get(src_file, {}).get(spec)
        if edge is None:
            return None
        kind = _KIND_6_55.get(edge.resolution, "unresolved")
        if edge.target_file is None and kind == "local":
            kind = "unresolved"
        return edge.target_file, kind


def provenance_view(
    graph: SymbolGraph,
    tracked_key: frozenset[str] | None = None,
) -> ProvenanceView:
    """Project the graph into the consumer-facing provenance view.

    Type-only edges (every binding ``type:``-prefixed) are EXCLUDED —
    they are erased at runtime and must not count as tech usage (S2)
    nor as component provenance (W2b). Duplicate ``(src, raw_target)``
    rows merge deterministically: resolved target wins, runtime binding
    names union (canonical graph order makes the merge stable).
    """
    parsed = graph.telemetry.get("parsed_files")
    if isinstance(parsed, list):
        files = frozenset(str(p) for p in parsed)
    else:  # degraded: derive coverage from the rows themselves
        files = frozenset(
            [d.file for d in graph.defs] + [e.src_file for e in graph.edges],
        )
    by_src: dict[str, dict[str, ResolvedEdge]] = {}
    names_acc: dict[str, dict[str, set[str]]] = {}
    bare_seen: dict[str, set[str]] = {}
    graph.sort_canonical()
    for edge in graph.resolved:
        runtime = _runtime_names(edge.names)
        if edge.names and not runtime:
            continue  # purely type-level import — invisible at runtime
        slot = by_src.setdefault(edge.src_file, {})
        prev = slot.get(edge.raw_target)
        if prev is None or (
            prev.target_file is None and edge.target_file is not None
        ):
            slot[edge.raw_target] = edge
        acc = names_acc.setdefault(edge.src_file, {})
        acc.setdefault(edge.raw_target, set()).update(runtime)
        if not edge.names:  # side-effect / star: counts once
            bare_seen.setdefault(edge.src_file, set()).add(edge.raw_target)
    weights: dict[str, dict[str, int]] = {}
    for src in sorted(by_src):
        w: dict[str, int] = {}
        for spec in sorted(by_src[src]):
            named = len(names_acc.get(src, {}).get(spec, ()))
            bare = 1 if spec in bare_seen.get(src, set()) else 0
            w[spec] = max(1, named + bare) if (named or bare) else 1
        weights[src] = w
    return ProvenanceView(
        tracked_key=tracked_key if tracked_key is not None else files,
        files=files,
        _by_src=by_src,
        _weights=weights,
    )


# ── Per-scan registry (hooks B/C reach the view without new plumbing) ────


@dataclass(frozen=True)
class _PipelineFns:
    parse_fn: ParseFn
    defs_fn: DefsFn
    imports_fn: ImportsFn
    resolve_fn: ResolveFn


_FNS_CACHE: list[Any] = [False]  # False = not probed; None = unavailable


def _load_real_fns() -> _PipelineFns | None:
    """Wire the REAL M1-M3 modules — the single integration point.

    Expected names (integrator adjusts here if M1-M3 chose different
    ones): ``parse.parse_file`` / ``defs.extract_defs`` /
    ``imports.extract_imports`` / ``resolve.resolve_edges``. Missing
    modules or attributes → ``None`` → every adapter entry point
    answers ``None`` → consumers stay on the regex path (Phase A).
    Probed ONCE per process (failed imports are not cached by the
    import machinery — per-file re-probing would be pure overhead);
    :func:`reset_ts_ast_state` clears the memo for tests.
    """
    cached: Any = _FNS_CACHE[0]
    if cached is not False:
        return cached
    # importlib keeps this correct in BOTH phases: Phase A (modules
    # absent → ImportError → None) and Phase B (modules merged) without
    # static-analysis knowledge of not-yet-existing files.
    import importlib

    fns: _PipelineFns | None
    try:
        parse_mod = importlib.import_module(
            "faultline.pipeline_v2.ts_ast.parse")
        defs_mod = importlib.import_module(
            "faultline.pipeline_v2.ts_ast.defs")
        imports_mod = importlib.import_module(
            "faultline.pipeline_v2.ts_ast.imports")
        resolve_mod = importlib.import_module(
            "faultline.pipeline_v2.ts_ast.resolve")
        fns = _PipelineFns(
            parse_fn=parse_mod.parse_file,
            defs_fn=defs_mod.extract_defs,
            imports_fn=imports_mod.extract_imports,
            resolve_fn=resolve_mod.resolve_edges,
        )
    except (ImportError, AttributeError):
        fns = None
    _FNS_CACHE[0] = fns
    return fns


_PROV_MEMO: dict[tuple[str, frozenset[str]], "ProvenanceView | None"] = {}
_CURRENT: list["ProvenanceView | None"] = [None]
_PROV_MEMO_CAP = 4  # one scan per process in practice; tests churn a few


def repo_provenance(
    repo_root: str,
    tracked_files: Iterable[str],
) -> ProvenanceView | None:
    """Build (memoised) + REGISTER the provenance view for this scan.

    ``None`` when the flag is off or M1-M3 are unavailable — callers
    keep the legacy path. The successful view becomes the process-
    current one for :func:`current_provenance` consumers that have no
    ``repo_path`` in scope (stage-6.55 ``_parse_page_source``).
    """
    if not ts_ast_enabled():
        return None
    fns = _load_real_fns()
    if fns is None:
        return None
    tracked = frozenset(str(p).replace("\\", "/") for p in tracked_files)
    key = (str(repo_root), tracked)
    if key not in _PROV_MEMO:
        if len(_PROV_MEMO) >= _PROV_MEMO_CAP:
            _PROV_MEMO.clear()
        try:
            graph = build_symbol_graph(
                str(repo_root), sorted(tracked),
                parse_fn=fns.parse_fn, defs_fn=fns.defs_fn,
                imports_fn=fns.imports_fn, resolve_fn=fns.resolve_fn,
            )
            _PROV_MEMO[key] = provenance_view(graph, tracked_key=tracked)
        except Exception:  # noqa: BLE001 — fallback law
            logger.debug("ts_ast: repo_provenance build failed",
                         exc_info=True)
            _PROV_MEMO[key] = None
    view = _PROV_MEMO[key]
    if view is not None:
        _CURRENT[0] = view
    return view


def current_provenance(
    tracked_files: Iterable[str] | frozenset[str],
) -> ProvenanceView | None:
    """The registered view — ONLY if it was built for THIS tracked set.

    The identity check keeps multi-workspace / multi-repo processes
    honest: a stale registration for a different tracked population
    answers ``None`` (legacy path) instead of wrong provenance.
    """
    if not ts_ast_enabled():
        return None
    view = _CURRENT[0]
    if view is None:
        return None
    tracked = tracked_files if isinstance(tracked_files, frozenset) \
        else frozenset(str(p).replace("\\", "/") for p in tracked_files)
    if view.tracked_key != tracked:
        return None
    return view


def reset_ts_ast_state() -> None:
    """Test hook: drop the memos + current registration."""
    _PROV_MEMO.clear()
    _CURRENT[0] = None
    _FNS_CACHE[0] = False


# ── (c) Entry detection — SKELETON ONLY (separate decision, spec §4) ─────


def entry_signals(graph: SymbolGraph) -> list[dict[str, Any]] | None:
    """[FAULTLINE_TS_AST_ENTRY] Entry-detection feed — NOT implemented.

    The entry migration is gated to its own flag and its own decision
    AFTER the (a)/(b) gates pass (spec §4). This skeleton keeps the
    call surface stable: ``None`` = "no AST entry signal, keep the
    existing entry detection" — including when the flag is ON, until
    a real implementation replaces this body.
    """
    if not ts_ast_entry_enabled():
        return None
    logger.debug(
        "ts_ast: entry_signals skeleton called (flag ON, no emission yet; "
        "graph defs=%d)", len(graph.defs),
    )
    return None
