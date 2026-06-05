"""Deterministic per-flow test mapper for pipeline_v2 (Gap 2).

``Flow.test_files`` is declared on the model but was NEVER populated in
``pipeline_v2`` — only read (Stage 6.7 / 6.7b / the 6.9 strip). With no
test files, Stage 6.7b's AC drafting has nothing to draft. This module
closes that gap deterministically.

For each flow it finds the test files that EXERCISE it, via three
code-grounded signals (no LLM, no README, no magic numbers):

  1. **Source → test mapping** — reuse the convention-based matcher in
     :mod:`faultline.analyzer.test_mapper` (``_filename_match`` /
     ``_strip_test_suffix``) which already encodes the JS/TS/Py/Go/Rust
     sibling + mirror-tree conventions (the single source of truth for
     "what test covers source file X"). Applied to each of the flow's
     source paths.

  2. **Entry-symbol reference** — a test whose source TEXT names the
     flow's entry symbol (e.g. ``handler`` / ``createDetector``). Catches
     backend unit/integration tests that import or call the handler.

  3. **Route reference** — a test whose source TEXT contains the flow's
     route pattern literal (e.g. ``/api/detectors``). Catches backend
     route tests AND E2E specs that drive the route through HTTP / a page
     URL, neither of which import a source symbol.

The first signal is precise (filename convention). Signals 2/3 are a
content-grep over test files only — bounded, deterministic, and required
because pipeline_v2 has no live ``SymbolGraph`` at this stage (so the
import-based primary in ``analyzer.test_mapper.build_test_map`` is not
available without re-parsing the whole repo).

ADDITIVE: writes only ``Flow.test_files`` + ``Flow.test_file_count``.
Touches no node / edge / symbol-attribution / participant, so the flow's
core-LOC projection is unaffected.

[[rule-no-repo-specific-paths]] / [[rule-no-magic-tuning]]: the only
inputs are universal test-file detection (``is_test_file``), the shared
filename-convention matcher, and literal substring references that come
from the flow's OWN code (its symbols / its route). Nothing is keyed on a
specific repo's folder names.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.test_mapper import _filename_match
from faultline.analyzer.validation import is_test_file

if TYPE_CHECKING:
    from faultline.models.types import Flow
    from faultline.pipeline_v2.flow_reach import ReachContext

logger = logging.getLogger(__name__)


_MAX_FILE_BYTES = 512_000

# Identifier-boundary match for an entry symbol so ``foo`` doesn't match
# ``foobar``. Compiled per-symbol; symbols are short so this is cheap.
def _symbol_pattern(symbol: str) -> re.Pattern[str] | None:
    if not symbol or not symbol.isidentifier():
        return None
    return re.compile(r"\b" + re.escape(symbol) + r"\b")


@dataclass
class FlowTestIndex:
    """Pre-read test-file corpus + the source→test filename map.

    Built ONCE per scan: enumerates every test file, reads its (bounded)
    source for the content-grep signals, and precomputes the
    filename-convention source→test map for the whole repo.
    """

    test_files: list[str] = field(default_factory=list)
    source_set: frozenset[str] = field(default_factory=frozenset)
    # test_file -> source text (lower-cased copy kept only transiently;
    # we keep the original case for symbol matching).
    test_source: dict[str, str] = field(default_factory=dict)
    # source_file -> [test files] via filename convention.
    by_source_file: dict[str, list[str]] = field(default_factory=dict)


def build_flow_test_index(rctx: "ReachContext") -> FlowTestIndex:
    """Enumerate + read test files and build the source→test filename map."""
    all_files = sorted(rctx.file_set)
    test_files = [f for f in all_files if is_test_file(f)]
    source_files = [f for f in all_files if not is_test_file(f)]
    source_set = frozenset(source_files)

    index = FlowTestIndex(
        test_files=test_files,
        source_set=source_set,
    )

    # Read test sources (bounded).
    for tf in test_files:
        src = _read_source(rctx, tf)
        if src:
            index.test_source[tf] = src

    # Filename-convention map: for each test, which source file does it
    # cover? Reuse the shared matcher. Invert into source -> [tests].
    for tf in test_files:
        target = _filename_match(tf, set(source_set))
        if target is not None:
            bucket = index.by_source_file.setdefault(target, [])
            if tf not in bucket:
                bucket.append(tf)

    return index


def _read_source(rctx: "ReachContext", path: str) -> str | None:
    sig = rctx.signatures.get(path)
    src = getattr(sig, "source", None) if sig is not None else None
    if src:
        return src
    try:
        abs_path = Path(rctx.repo_path) / path
        if abs_path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return abs_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return None


def _flow_entry_symbols(flow: "Flow") -> list[str]:
    """The flow's entry + called symbols, for reference-grep signal 2."""
    syms: list[str] = []
    seen: set[str] = set()

    def _add(s: str | None) -> None:
        if s and s != "<file>" and s not in seen:
            seen.add(s)
            syms.append(s)

    entry = flow.entry or {}
    _add(entry.get("symbol") if isinstance(entry, dict) else None)
    for fsa in flow.flow_symbol_attributions or []:
        if getattr(fsa, "role", None) in ("entry", "called"):
            _add(getattr(fsa, "symbol", None))
    return syms


def _flow_route_literals(flow: "Flow") -> list[str]:
    """Route pattern literals for reference-grep signal 3.

    A route literal must be specific enough to grep on — we keep only the
    static prefix before the first dynamic segment, and require it to be a
    real path (starts with ``/`` and has length > 1).
    """
    out: list[str] = []
    seen: set[str] = set()
    cands: list[str] = []
    for n in flow.nodes or []:
        if getattr(n, "role", None) == "cross_stack_server":
            nid = getattr(n, "id", "") or ""
            if "#" in nid:
                tail = nid.split("#", 1)[1]
                if ":" in tail:
                    cands.append(tail.split(":", 1)[1])
    for raw in cands:
        # Static prefix before the first dynamic segment marker.
        prefix = re.split(r"[\[{:]", raw, maxsplit=1)[0].rstrip("/")
        if prefix.startswith("/") and len(prefix) > 1 and prefix not in seen:
            seen.add(prefix)
            out.append(prefix)
    return out


def _flow_source_paths(flow: "Flow") -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for p in (flow.paths or []):
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    if flow.entry_point_file and flow.entry_point_file not in seen:
        seen.add(flow.entry_point_file)
        paths.append(flow.entry_point_file)
    return paths


def tests_for_flow(flow: "Flow", index: FlowTestIndex) -> list[str]:
    """All test files exercising ``flow`` via the three signals."""
    tests: set[str] = set()

    # Signal 1 — filename convention over the flow's source paths.
    for sp in _flow_source_paths(flow):
        tests.update(index.by_source_file.get(sp, []))

    # Signal 2 — entry/called symbol reference in test source.
    sym_pats = [
        p for p in (_symbol_pattern(s) for s in _flow_entry_symbols(flow))
        if p is not None
    ]
    # Signal 3 — route literal reference in test source.
    route_literals = _flow_route_literals(flow)

    if sym_pats or route_literals:
        for tf, src in index.test_source.items():
            if tf in tests:
                continue
            if any(lit in src for lit in route_literals):
                tests.add(tf)
                continue
            if any(p.search(src) for p in sym_pats):
                tests.add(tf)

    return sorted(tests)


def attach_flow_test_files(
    flows: list["Flow"],
    rctx: "ReachContext",
    *,
    index: FlowTestIndex | None = None,
) -> dict[str, Any]:
    """Populate ``test_files`` + ``test_file_count`` on each flow (in place).

    ADDITIVE: only the two test fields are written. Idempotent (overwrites
    with the same data on re-run). Graceful when the repo has no tests
    (every flow gets an empty list).

    Returns telemetry for ``scan_meta``.
    """
    if index is None:
        index = build_flow_test_index(rctx)

    flows_with_tests = 0
    test_file_links = 0
    for fl in flows:
        tfs = tests_for_flow(fl, index)
        fl.test_files = tfs
        fl.test_file_count = len(tfs)
        if tfs:
            flows_with_tests += 1
            test_file_links += len(tfs)

    return {
        "test_files_total_in_repo": len(index.test_files),
        "flows_with_test_files": flows_with_tests,
        "flow_test_file_links": test_file_links,
    }


__all__ = [
    "FlowTestIndex",
    "build_flow_test_index",
    "tests_for_flow",
    "attach_flow_test_files",
]
