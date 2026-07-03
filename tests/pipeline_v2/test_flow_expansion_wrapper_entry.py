"""Higher-order WRAPPER-entry unwrap for JS/TS route handlers.

Covers the structural hole where a flow's entry symbol is a pure
higher-order wrapper assignment::

    export const POST = wrap(postHandler)

The plain call-pattern finds only ``wrap`` (a cross-file util);
``postHandler`` — a LOCAL function holding the real body — is passed as a
reference argument and so was never traced, collapsing the flow to the
2-LOC wrapper assignment. The fix:

  1. ``extract_symbol_ranges`` records top-level LOCAL (non-exported)
     function / arrow declarations (kind="local") so the handler body has
     a resolvable range — WITHOUT polluting ``FileSignature.exports``
     (feature anchoring stays unchanged).
  2. The call-graph resolves function-REFERENCE arguments at the entry
     node and, when they map to a local function, treats them as the true
     entry body — inheriting the entry's cross-file budget (depth 0).

Neutral synthetic fixtures only — no repo-specific paths or names
(per rule-no-repo-specific-paths). Pure deterministic, NO LLM.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from faultline.analyzer.ast_extractor import extract_signatures, extract_symbol_ranges
from faultline.pipeline_v2.flow_reach import (
    ReachContext,
    build_path_alias_map,
    detect_monorepo_packages,
)
from faultline.pipeline_v2.flow_expansion.call_graph import build_call_graph


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def _reach_context(repo: Path, files: list[str]) -> ReachContext:
    sigs = extract_signatures(files, str(repo))
    return ReachContext(
        repo_path=repo,
        file_set=frozenset(files),
        signatures=sigs,
        alias_map={},
        monorepo_packages=detect_monorepo_packages(str(repo)),
        go_module_prefix=None,
        alias_entries=build_path_alias_map(repo),
    )


# ── Part 1: local-symbol range extraction ───────────────────────────────


def test_local_functions_recorded_in_symbol_ranges() -> None:
    """Non-exported local functions get ranges with kind=local."""
    src = textwrap.dedent(
        """
        function helper(x) {
          return x + 1;
        }

        async function postHandler(req) {
          return helper(req);
        }

        export const POST = wrap(postHandler);
        """
    ).lstrip("\n")
    ranges = {r.name: r for r in extract_symbol_ranges(src)}
    assert "postHandler" in ranges
    assert ranges["postHandler"].kind == "local"
    assert "helper" in ranges
    assert ranges["helper"].kind == "local"
    # exported symbol keeps its const kind, body span is multi-line
    assert ranges["POST"].kind == "const"
    # postHandler span must cover its real body (more than 1 line).
    assert ranges["postHandler"].end_line > ranges["postHandler"].start_line


def test_arrow_local_functions_recorded() -> None:
    """const foo = () => / const foo = async () => are captured."""
    src = textwrap.dedent(
        """
        const validate = (p) => Boolean(p);
        const handler = async (req) => {
          return validate(req);
        };
        export default withErrorHandler(handler);
        """
    ).lstrip("\n")
    ranges = {r.name: r for r in extract_symbol_ranges(src)}
    assert ranges["validate"].kind == "local"
    assert ranges["handler"].kind == "local"


def test_local_symbols_do_not_leak_into_exports(tmp_path: Path) -> None:
    """Local funcs are body-resolution only; never feature anchors."""
    repo = tmp_path / "exports"
    repo.mkdir()
    _write(
        repo,
        "route.ts",
        """
        function postHandler(req) {
          return 1;
        }
        export const POST = wrap(postHandler);
        """,
    )
    sigs = extract_signatures(["route.ts"], str(repo))
    sig = sigs["route.ts"]
    assert sig.exports == ["POST"]
    assert "postHandler" not in sig.exports
    names = {r.name for r in sig.symbol_ranges}
    assert {"POST", "postHandler"} <= names


# ── Part 2: wrapper-entry unwrap in the call graph ──────────────────────


def test_positional_wrapper_arg_resolves_to_local_handler(
    tmp_path: Path,
) -> None:
    """const X = wrap(handler) traces the LOCAL handler body."""
    repo = tmp_path / "wrap_pos"
    repo.mkdir()
    _write(
        repo,
        "route.ts",
        """
        function validateInput(p) {
          return Boolean(p);
        }

        async function postHandler(req) {
          if (!validateInput(req)) {
            return null;
          }
          return req;
        }

        export const POST = defaultResponder(postHandler);
        """,
    )
    rctx = _reach_context(repo, ["route.ts"])
    res = build_call_graph(rctx, "route.ts", "POST", None)
    symbols = {n.symbol for n in res.nodes}
    # The real handler AND its same-file helper are reached.
    assert "postHandler" in symbols
    assert "validateInput" in symbols
    # Flow LOC is now the handler body, not the 2-LOC wrapper line.
    total = sum(
        (n.lines[1] - n.lines[0] + 1)
        for n in res.nodes
        if n.symbol and n.lines
    )
    assert total > 5


def test_object_property_wrapper_arg_resolves(tmp_path: Path) -> None:
    """const X = apiHandler({ GET: getHandler }) traces the handler."""
    repo = tmp_path / "wrap_obj"
    repo.mkdir()
    _write(
        repo,
        "route.ts",
        """
        async function getHandler(req) {
          return req;
        }

        export const GET = apiHandler({ GET: getHandler });
        """,
    )
    rctx = _reach_context(repo, ["route.ts"])
    res = build_call_graph(rctx, "route.ts", "GET", None)
    assert "getHandler" in {n.symbol for n in res.nodes}


def test_wrapper_unwrap_does_not_pull_nonfunction_args(
    tmp_path: Path,
) -> None:
    """A const data object passed as an arg must NOT become a callee."""
    repo = tmp_path / "wrap_data"
    repo.mkdir()
    _write(
        repo,
        "route.ts",
        """
        const schema = { name: "string" };

        async function postHandler(req) {
          return req;
        }

        export const POST = validate(schema, postHandler);
        """,
    )
    rctx = _reach_context(repo, ["route.ts"])
    res = build_call_graph(rctx, "route.ts", "POST", None)
    symbols = {n.symbol for n in res.nodes}
    assert "postHandler" in symbols
    # `schema` is a const object, not a function — never a callee node.
    assert "schema" not in symbols


def test_unwrapped_handler_keeps_entry_cross_file_budget(
    tmp_path: Path,
) -> None:
    """The unwrapped handler's imported callee still fans out (depth-0)."""
    repo = tmp_path / "wrap_xfile"
    repo.mkdir()
    _write(
        repo,
        "service.ts",
        """
        export function doWork(x) {
          return x;
        }
        """,
    )
    _write(
        repo,
        "route.ts",
        """
        import { doWork } from "./service";

        async function postHandler(req) {
          return doWork(req);
        }

        export const POST = wrap(postHandler);
        """,
    )
    rctx = _reach_context(repo, ["route.ts", "service.ts"])
    res = build_call_graph(rctx, "route.ts", "POST", None)
    files = {n.file for n in res.nodes}
    # Cross-file callee of the unwrapped handler is reached → ≥2 files.
    assert "service.ts" in files
    assert "doWork" in {n.symbol for n in res.nodes}


def test_non_wrapper_entry_unaffected(tmp_path: Path) -> None:
    """A normal exported handler (no wrapper) behaves exactly as before."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _write(
        repo,
        "route.ts",
        """
        function helper(x) {
          return x;
        }

        export async function POST(req) {
          return helper(req);
        }
        """,
    )
    rctx = _reach_context(repo, ["route.ts"])
    res = build_call_graph(rctx, "route.ts", "POST", None)
    assert "helper" in {n.symbol for n in res.nodes}


# ── D1: page/route entry expands from a 1-line placeholder to the real
#        body span when the entry symbol can't be matched BY NAME ─────────


def test_entry_line_expands_to_containing_body_no_symbol(tmp_path: Path) -> None:
    """D1: a filesystem-routed page whose entry_symbol is None (the
    default-export component name could not be extracted) but with a known
    entry_line must expand to the body range that BRACKETS the line, not
    collapse to a 1-line placeholder span."""
    repo = tmp_path / "d1a"
    repo.mkdir()
    _write(
        repo,
        "app/page.tsx",
        """
        const PageComponent = () => {
          const a = 1;
          const b = 2;
          const c = 3;
          return null;
        };
        export default PageComponent;
        export const meta = { title: "x" };
        """,
    )
    rctx = _reach_context(repo, ["app/page.tsx"])
    # entry_symbol is None (default export name not extracted -> filesystem
    # routing); entry_line points INTO the component body (line 3).
    res = build_call_graph(rctx, "app/page.tsx", None, 3)
    entry = res.nodes[0]
    assert entry.lines is not None
    start, end = entry.lines
    # Expanded to the PageComponent body (1..6), NOT (3, 3).
    assert end > start, f"expected multi-line span, got ({start}, {end})"
    assert start <= 3 <= end


def test_entry_line_arrow_default_export_expands(tmp_path: Path) -> None:
    """D1: arrow default-export page whose entry_symbol the by-name lookup
    missed (wrong/stale name) still expands via the bracketing entry_line."""
    repo = tmp_path / "d1b"
    repo.mkdir()
    _write(
        repo,
        "app/dash/page.tsx",
        """
        const DashPage = () => {
          const x = 1;
          const y = 2;
          return null;
        };
        export default DashPage;
        export const revalidate = 60;
        """,
    )
    rctx = _reach_context(repo, ["app/dash/page.tsx"])
    # Simulate a by-NAME miss: pass a symbol that isn't in the ranges, but
    # an entry_line inside the arrow body (line 3).
    res = build_call_graph(rctx, "app/dash/page.tsx", "NotMatchedName", 3)
    entry = res.nodes[0]
    assert entry.lines is not None
    start, end = entry.lines
    assert end > start, f"expected multi-line span, got ({start}, {end})"
    # Specifically the arrow body range (1..6) brackets line 3.
    assert start <= 3 <= end


def test_entry_by_name_resolution_unchanged_regression_guard(tmp_path: Path) -> None:
    """D1 regression guard: when the entry symbol DOES resolve by name, the
    range is the by-name range — the D1 expansion branch never runs and
    behaviour is byte-identical."""
    repo = tmp_path / "d1c"
    repo.mkdir()
    _write(
        repo,
        "app/page.tsx",
        """
        export default function HomePage() {
          const a = 1;
          const b = 2;
          return null;
        }
        """,
    )
    rctx = _reach_context(repo, ["app/page.tsx"])
    res = build_call_graph(rctx, "app/page.tsx", "HomePage", 1)
    entry = res.nodes[0]
    assert entry.lines is not None
    start, end = entry.lines
    # By-name range: HomePage spans 1..5.
    assert start == 1
    assert end >= 5


def test_entry_no_bracketing_range_falls_back_to_one_line(tmp_path: Path) -> None:
    """D1 graceful degrade: when no symbol range brackets the entry_line
    (e.g. a line in module-scope outside any body), keep the legacy
    1-line placeholder rather than inventing a span."""
    repo = tmp_path / "d1d"
    repo.mkdir()
    _write(
        repo,
        "app/page.tsx",
        """
        const TOP = 1;
        export default function HomePage() {
          return null;
        }
        """,
    )
    rctx = _reach_context(repo, ["app/page.tsx"])
    # entry_line 1 is a module-scope const (single-line range) with no
    # multi-line body bracketing it.
    res = build_call_graph(rctx, "app/page.tsx", None, 1)
    entry = res.nodes[0]
    assert entry.lines == (1, 1)
