"""Deepen flow detection: Python symbol-range population (Gap A),
same-file callee resolution (Gap B), and callees written into
``flow_symbol_attributions`` with role=``called`` (Gap C).

Neutral synthetic fixtures only — no repo-specific paths or names
(per rule-no-repo-specific-paths). Pure deterministic trace-flows
work, NO LLM, NO token cost.
"""

from __future__ import annotations

import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.analyzer.ast_extractor import extract_signatures
from faultline.models.types import Feature, Flow, FlowSymbolAttribution
from faultline.pipeline_v2.flow_expansion import expand_flows
from faultline.pipeline_v2.flow_expansion.call_graph import build_call_graph
from faultline.pipeline_v2.flow_reach import (
    ReachContext,
    detect_monorepo_packages,
    load_tsconfig_paths,
)
from faultline.pipeline_v2.stage_0_intake import stage_0_intake


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.local"], cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed", "--allow-empty"],
        cwd=repo, check=True,
    )


# ── Gap A: Python symbol ranges + source populated ──────────────────────


def test_python_symbol_ranges_via_ast(tmp_path: Path) -> None:
    """``extract_signatures`` populates exact AST ranges + source for .py."""
    repo = tmp_path / "py_ranges"
    repo.mkdir()
    _write(repo, "svc.py", """
        import os


        class Service:
            def run(self):
                return helper()


        def helper():
            return 1


        async def amain():
            return helper()
    """)
    sigs = extract_signatures(["svc.py"], str(repo))
    sig = sigs["svc.py"]

    # Module-level names are exports.
    assert "Service" in sig.exports
    assert "helper" in sig.exports
    assert "amain" in sig.exports

    # Source is now populated (was empty for Python before the fix → the
    # call graph could never scan a symbol body).
    assert sig.source, "Python signature must carry source for body scan"

    names = {r.name for r in sig.symbol_ranges}
    assert {"Service", "helper", "amain"} <= names
    # Nested method gets a range too (AST walk), enabling same-file
    # method-callee resolution.
    assert "run" in names

    helper_rng = next(r for r in sig.symbol_ranges if r.name == "helper")
    # Precise AST boundaries — helper is a small 2-line function, not the
    # whole rest-of-file the old next-symbol-minus-one heuristic gave.
    assert helper_rng.end_line - helper_rng.start_line <= 3


def test_python_regex_fallback_on_syntax_error(tmp_path: Path) -> None:
    """Invalid Python still yields module-level symbols via regex."""
    repo = tmp_path / "py_broken"
    repo.mkdir()
    _write(repo, "broken.py", """
        def good():
            return 1

        def bad(:   # syntax error below this point
    """)
    sigs = extract_signatures(["broken.py"], str(repo))
    sig = sigs["broken.py"]
    assert "good" in sig.exports
    assert sig.source


# ── Gap B: same-file callees resolve ────────────────────────────────────


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


def test_same_file_callee_resolves(tmp_path: Path) -> None:
    """Handler → private helper in the SAME module yields a call edge."""
    repo = tmp_path / "same_file"
    repo.mkdir()
    _write(repo, "api.py", """
        def _validate(payload):
            return bool(payload)


        def _persist(row):
            return row


        def create_thing(payload):
            if not _validate(payload):
                return None
            return _persist(payload)
    """)
    rctx = _reach_context(repo, ["api.py"])
    res = build_call_graph(rctx, "api.py", "create_thing", None)

    callee_syms = {n.symbol for n in res.nodes if n.symbol}
    assert "_validate" in callee_syms
    assert "_persist" in callee_syms
    # Edges are labelled same_file (medium per confidence_for_call).
    call_edges = [e for e in res.edges if e.kind == "call"]
    assert call_edges, "must emit same-file call edges"
    # No self-loop on the entry symbol.
    assert all(
        not (e.from_id == e.to_id) for e in res.edges
    )


def test_cross_file_callee_still_resolves(tmp_path: Path) -> None:
    """Regression guard: imported callees still resolve after Gap B."""
    repo = tmp_path / "cross_file"
    repo.mkdir()
    _write(repo, "handler.py", """
        from .service import find_user


        def handle(uid):
            return find_user(uid)
    """)
    _write(repo, "service.py", """
        def find_user(uid):
            return {"id": uid}
    """)
    rctx = _reach_context(repo, ["handler.py", "service.py"])
    res = build_call_graph(rctx, "handler.py", "handle", None)
    resolved = {(n.file, n.symbol) for n in res.nodes if n.symbol}
    assert ("service.py", "find_user") in resolved


# ── Gap C: callees land in flow_symbol_attributions role=called ─────────


def _make_flow(entry_file: str, entry_symbol: str) -> Flow:
    now = datetime.now(timezone.utc)
    fl = Flow(
        name="create-thing-flow",
        entry_point_file=entry_file,
        entry_point_line=None,
        paths=[entry_file],
        authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=now, health_score=100.0,
        uuid="fl-create-thing",
    )
    fl.flow_symbol_attributions = [FlowSymbolAttribution(
        file=entry_file, symbol=entry_symbol,
        line_start=1, line_end=1, role="entry",
    )]
    return fl


def test_callees_written_into_flow_symbol_attributions(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "fsa"
    repo.mkdir()
    _write(repo, "api.py", """
        def _validate(payload):
            return bool(payload)


        def _persist(row):
            return row


        def create_thing(payload):
            if not _validate(payload):
                return None
            return _persist(payload)
    """)
    _init_git_repo(repo)
    ctx = stage_0_intake(str(repo))

    flow = _make_flow("api.py", "create_thing")
    feat = Feature(
        name="api", paths=["api.py"], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0, flows=[flow], uuid="feat-api",
    )

    expand_flows([feat], ctx, routes_index=[])

    by_role: dict[str, set[str]] = {}
    for a in flow.flow_symbol_attributions:
        by_role.setdefault(a.role, set()).add(a.symbol)

    # Entry preserved.
    assert "create_thing" in by_role.get("entry", set())
    # Callees now attributed with role=called.
    called = by_role.get("called", set())
    assert "_validate" in called
    assert "_persist" in called


def test_expand_flows_idempotent_on_attributions(tmp_path: Path) -> None:
    """Re-expanding an already-expanded flow doesn't duplicate records."""
    repo = tmp_path / "idem"
    repo.mkdir()
    _write(repo, "api.py", """
        def _h():
            return 1


        def entry():
            return _h()
    """)
    _init_git_repo(repo)
    ctx = stage_0_intake(str(repo))
    flow = _make_flow("api.py", "entry")
    feat = Feature(
        name="api", paths=["api.py"], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0, flows=[flow], uuid="feat-api",
    )
    expand_flows([feat], ctx, routes_index=[])
    first = [
        (a.file, a.symbol, a.line_start, a.line_end, a.role)
        for a in flow.flow_symbol_attributions
    ]
    # Second pass is a no-op on the graph (nodes already populated); the
    # attribution set must not grow or duplicate.
    expand_flows([feat], ctx, routes_index=[])
    second = [
        (a.file, a.symbol, a.line_start, a.line_end, a.role)
        for a in flow.flow_symbol_attributions
    ]
    assert first == second
