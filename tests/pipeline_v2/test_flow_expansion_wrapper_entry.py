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
    detect_monorepo_packages,
    load_tsconfig_paths,
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
        alias_map=load_tsconfig_paths(str(repo)),
        monorepo_packages=detect_monorepo_packages(str(repo)),
        go_module_prefix=None,
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
