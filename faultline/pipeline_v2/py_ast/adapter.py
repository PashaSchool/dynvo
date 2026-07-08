"""Track-B py_ast M4 — the bridge from the Python symbol graph to consumers.

The Python mirror of ``ts_ast.adapter``. It builds a :class:`SymbolGraph`
from Python files and projects it into the SAME
:class:`~faultline.pipeline_v2.ts_ast.adapter.ProvenanceView` shape that
ts_ast returns — so Track A consumes Python provenance through one type,
one accessor surface (``resolve`` / ``lookup`` / ``raw_specs`` /
``spec_occurrences``), regardless of language. To guarantee that
identity, the view CLASS and its projector ``provenance_view`` are
IMPORTED from ts_ast.adapter (read-only reuse; py_ast → ts_ast is a
one-way dependency, ts_ast never imports py_ast → no cycle).

Consumer entry points (mirror ts_ast):

* :func:`repo_provenance` / :func:`current_provenance` — the resolved
  Python import provenance for Track A's file→feature membership
  decision (spec Track-B: "B дає ДАНІ, A їх споживає"). ``None`` when
  the master flag is off or the pipeline is unavailable → caller keeps
  the legacy path.
* :func:`ast_symbol_ranges` — per-file def-span upgrade for the Python
  branch of ``analyzer.ast_extractor`` (Hook A parity). PROVIDED but
  UNWIRED by Track B: py_ast ships as a pure additive DATA layer (zero
  scan-behaviour change → the kill-switch is byte-identical by
  construction); the coordinator/Track A may wire it later behind
  ``FAULTLINE_PY_AST`` with a re-pin.
* :func:`build_symbol_graph` — the repo-level assembler the metrics
  harness + tests drive.

Master flag ``FAULTLINE_PY_AST`` (default ON; ``=0`` → every entry point
answers ``None`` → consumers stay on the legacy path).
Determinism: sorted iteration, canonical graph sort, no set iteration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

# REUSE the canonical shapes + the language-agnostic provenance projector
# and view CLASS from ts_ast so Track A sees ONE provenance type.
from faultline.pipeline_v2.ts_ast.adapter import ProvenanceView, provenance_view
from faultline.pipeline_v2.py_ast.shapes import (
    DefSpan,
    ExportEntry,
    ImportEdge,
    ResolvedEdge,
    SymbolGraph,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import SymbolRange
    from faultline.pipeline_v2.py_ast.parse import FileParse

logger = logging.getLogger(__name__)

__all__ = [
    "GRAPH_VER",
    "PY_AST_ENV",
    "PY_AST_ENTRY_ENV",
    "ProvenanceView",
    "ast_symbol_ranges",
    "build_symbol_graph",
    "current_provenance",
    "provenance_view",
    "py_ast_enabled",
    "repo_provenance",
    "reset_py_ast_state",
]

#: Master flag (spec §2). Default ON; ``=0`` → every consumer answers
#: ``None`` (legacy path) → byte-identical kill-switch.
PY_AST_ENV = "FAULTLINE_PY_AST"
PY_AST_ENTRY_ENV = "FAULTLINE_PY_AST_ENTRY"

#: Adapter/version stamp (participates in graph telemetry).
GRAPH_VER = "py-ast-m4-1"

_FALSY = frozenset({"0", "false", "no", "off"})
_PY_SUFFIXES = (".py",)
_STUB_SUFFIXES = (".pyi",)


def py_ast_enabled() -> bool:
    return (os.environ.get(PY_AST_ENV, "1") or "1").strip().lower() not in _FALSY


def _is_py(rel_path: str) -> bool:
    low = rel_path.lower()
    return low.endswith(_PY_SUFFIXES) and not low.endswith(_STUB_SUFFIXES)


# ── Graph assembly (M1-M3 injected) ──────────────────────────────────────


ParseFn = Callable[..., "FileParse | None"]
DefsFn = Callable[["FileParse"], Sequence[DefSpan]]
ImportsFn = Callable[
    ["FileParse"], "tuple[Sequence[ImportEdge], Sequence[ExportEntry]]",
]
ResolveFn = Callable[
    ...,
    "Sequence[ResolvedEdge] | tuple[Sequence[ResolvedEdge], Mapping[str, int]]",
]


def build_symbol_graph(
    repo_root: str,
    files: Iterable[str],
    *,
    parse_fn: ParseFn,
    defs_fn: DefsFn,
    imports_fn: ImportsFn,
    resolve_fn: ResolveFn,
) -> SymbolGraph:
    """Assemble the per-repo Python :class:`SymbolGraph` from injected stages.

    Mirrors ts_ast's builder but filters for ``.py`` (skipping ``.pyi``
    and non-Python). The FULL sorted tracked list is still handed to
    ``resolve_fn`` so relative / package / src-layout probing sees every
    path. A per-file parse failure never fails the build (fallback law).
    """
    tracked_sorted = sorted(str(f).replace("\\", "/") for f in files)
    graph = SymbolGraph()
    parsed_files: list[str] = []
    failed_files: list[str] = []
    stub_skipped = 0
    non_py_skipped = 0

    exports_by_file: dict[str, list[ExportEntry]] = {}
    for rel in tracked_sorted:
        if not _is_py(rel):
            if rel.lower().endswith(_STUB_SUFFIXES):
                stub_skipped += 1
            else:
                non_py_skipped += 1
            continue
        try:
            fp = parse_fn(repo_root, rel)
        except Exception:  # noqa: BLE001 — parse faults degrade per file
            logger.debug("py_ast: parse_fn raised for %s", rel, exc_info=True)
            fp = None
        if fp is None:
            failed_files.append(rel)
            continue
        parsed_files.append(rel)
        try:
            graph.defs.extend(defs_fn(fp))
        except Exception:  # noqa: BLE001
            logger.debug("py_ast: defs_fn raised for %s", rel, exc_info=True)
        try:
            edges, exports = imports_fn(fp)
        except Exception:  # noqa: BLE001
            logger.debug("py_ast: imports_fn raised for %s", rel, exc_info=True)
            edges, exports = (), ()
        graph.edges.extend(edges)
        for entry in exports:
            exports_by_file.setdefault(entry.file, []).append(entry)

    graph.exports_index = exports_by_file
    graph.sort_canonical()

    resolve_tele: dict[str, int] | None = None
    try:
        res = resolve_fn(
            repo_root, graph.edges, graph.exports_index, tracked_sorted,
        )
        if (isinstance(res, tuple) and len(res) == 2
                and isinstance(res[1], Mapping)):
            graph.resolved = list(res[0])
            resolve_tele = {k: int(res[1][k]) for k in sorted(res[1])}
        else:
            graph.resolved = list(res)
    except Exception:  # noqa: BLE001 — resolution faults degrade whole-graph
        logger.debug("py_ast: resolve_fn raised", exc_info=True)
        graph.resolved = []
    graph.sort_canonical()

    histogram: dict[str, int] = {}
    for r in graph.resolved:
        histogram[r.resolution] = histogram.get(r.resolution, 0) + 1
    graph.telemetry = {
        "graph_ver": GRAPH_VER,
        "lang": "py",
        "files_seen": len(tracked_sorted),
        "files_parsed": len(parsed_files),
        "parse_failures": len(failed_files),
        "stub_skipped": stub_skipped,
        "non_py_skipped": non_py_skipped,
        "parsed_files": parsed_files,          # already sorted (input order)
        "failed_files": failed_files,          # already sorted
        "defs": len(graph.defs),
        "edges": len(graph.edges),
        "resolved_total": len(graph.resolved),
        "resolution_histogram": {k: histogram[k] for k in sorted(histogram)},
    }
    if resolve_tele is not None:
        graph.telemetry["resolve"] = resolve_tele
    return graph


# ── Real-pipeline wiring (single integration point) ──────────────────────


@dataclass(frozen=True)
class _PipelineFns:
    parse_fn: ParseFn
    defs_fn: DefsFn
    imports_fn: ImportsFn
    resolve_fn: ResolveFn


_FNS_CACHE: list[Any] = [False]  # False = not probed; None = unavailable


def _load_real_fns() -> _PipelineFns | None:
    """Wire the REAL py_ast M1-M3 modules. Probed once per process."""
    cached: Any = _FNS_CACHE[0]
    if cached is not False:
        return cached
    import importlib

    fns: _PipelineFns | None
    try:
        parse_mod = importlib.import_module("faultline.pipeline_v2.py_ast.parse")
        defs_mod = importlib.import_module("faultline.pipeline_v2.py_ast.defs")
        imports_mod = importlib.import_module(
            "faultline.pipeline_v2.py_ast.imports")
        resolve_mod = importlib.import_module(
            "faultline.pipeline_v2.py_ast.resolve")
        m1_parse = parse_mod.parse_file
        m1_defs = defs_mod.extract_defs
        m2_imports = imports_mod.extract_imports
        m3_resolve = resolve_mod.resolve_edges

        def parse_fn(repo_root: str, rel: str, source: Any = None) -> Any:
            raw = source
            if raw is None:
                try:
                    raw = Path(repo_root, rel).read_bytes()
                except OSError:
                    return None
            elif isinstance(raw, str):
                raw = raw.encode("utf-8", errors="ignore")
            return m1_parse(rel, raw)

        def defs_fn(fp: Any) -> list[DefSpan]:
            return list(m1_defs(fp))

        def imports_fn(
            fp: Any,
        ) -> tuple[list[ImportEdge], list[ExportEntry]]:
            edges, exports = m2_imports(fp)
            return list(edges), list(exports)

        def resolve_fn(
            repo_root: str, edges: Any, exports_index: Any, tracked: Any,
        ) -> tuple[list[ResolvedEdge], dict[str, int]]:
            resolved, tele = m3_resolve(
                edges, exports_index, repo_root, frozenset(tracked),
            )
            return list(resolved), dict(tele)

        fns = _PipelineFns(
            parse_fn=parse_fn, defs_fn=defs_fn,
            imports_fn=imports_fn, resolve_fn=resolve_fn,
        )
    except (ImportError, AttributeError):
        fns = None
    _FNS_CACHE[0] = fns
    return fns


# ── (a) Def-spans → the legacy SymbolRange consumer shape (Hook A) ────────


def _symbol_range_of(d: DefSpan) -> "SymbolRange | None":
    """Map a Python DefSpan to the legacy SymbolRange (kind parity).

    Mirrors ``_parse_python_file``: module-level ``def`` → ``function``,
    ``class`` → ``class``, method → ``method`` (``__init__`` →
    ``constructor``), module const → ``const``. Non-exported nested
    functions keep ``function`` so the call graph resolves same-file
    local callees.
    """
    from faultline.models.types import SymbolRange

    if d.parent is not None:
        kind = "constructor" if d.name == "__init__" else "method"
        return SymbolRange(
            name=d.name, start_line=d.start_line, end_line=d.end_line,
            kind=kind, parent=d.parent,
        )
    return SymbolRange(
        name=d.name, start_line=d.start_line, end_line=d.end_line, kind=d.kind,
    )


def ast_symbol_ranges(
    repo_root: str,
    rel_path: str,
    source: str | bytes | None,
    regex_ranges: Sequence["SymbolRange"],
) -> list["SymbolRange"] | None:
    """Per-file Python def-span upgrade (Hook A parity — PROVIDED, UNWIRED).

    Returns the merged ``symbol_ranges`` (AST wins by name; legacy-only
    entries survive), or ``None`` when the caller must keep its legacy
    result (flag off / not a ``.py`` file / parse failure). Never raises.
    """
    if not py_ast_enabled() or not _is_py(rel_path):
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
    except Exception:  # noqa: BLE001 — fallback law
        logger.debug("py_ast: ast_symbol_ranges failed for %s", rel_path,
                     exc_info=True)
        return None


def _merge_ranges(
    ast_defs: Sequence[DefSpan],
    regex_ranges: Sequence["SymbolRange"],
) -> list["SymbolRange"]:
    """AST def-spans merged with legacy ranges (AST wins by name/parent)."""
    top: list["SymbolRange"] = []
    seen_top: set[str] = set()
    methods: list["SymbolRange"] = []
    seen_meth: set[tuple[str, str]] = set()

    for d in sorted(ast_defs, key=lambda d: (d.parent or "", d.start_line, d.name)):
        rng = _symbol_range_of(d)
        if rng is None:  # pragma: no cover — Python defs always map
            continue
        if d.parent is not None:
            key = (d.parent, d.name)
            if key not in seen_meth:
                seen_meth.add(key)
                methods.append(rng)
        elif d.name not in seen_top:
            seen_top.add(d.name)
            top.append(rng)

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


# ── (b) Provenance view (Track A membership consumer) ─────────────────────


_PROV_MEMO: dict[tuple[str, frozenset[str]], "ProvenanceView | None"] = {}
_CURRENT: list["ProvenanceView | None"] = [None]
_PROV_MEMO_CAP = 4


def repo_provenance(
    repo_root: str,
    tracked_files: Iterable[str],
) -> ProvenanceView | None:
    """Build (memoised) + REGISTER the Python provenance view for this scan.

    ``None`` when the flag is off or py_ast is unavailable — callers keep
    the legacy path. The successful view becomes the process-current one
    for :func:`current_provenance` consumers without a ``repo_path`` in
    scope. Note this registry is SEPARATE from ts_ast's — a process may
    hold both a TS and a Python current view.
    """
    if not py_ast_enabled():
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
            logger.debug("py_ast: repo_provenance build failed", exc_info=True)
            _PROV_MEMO[key] = None
    view = _PROV_MEMO[key]
    if view is not None:
        _CURRENT[0] = view
    return view


def current_provenance(
    tracked_files: Iterable[str] | frozenset[str],
) -> ProvenanceView | None:
    """The registered Python view — ONLY if built for THIS tracked set."""
    if not py_ast_enabled():
        return None
    view = _CURRENT[0]
    if view is None:
        return None
    tracked = tracked_files if isinstance(tracked_files, frozenset) \
        else frozenset(str(p).replace("\\", "/") for p in tracked_files)
    if view.tracked_key != tracked:
        return None
    return view


def reset_py_ast_state() -> None:
    """Test hook: drop the memos + current registration + fn cache."""
    _PROV_MEMO.clear()
    _CURRENT[0] = None
    _FNS_CACHE[0] = False
