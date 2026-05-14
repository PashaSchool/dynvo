"""Tests for the dependency-context window builder (Phase 3a).

Verifies:
- Empty block for trivial chunks (< 2 files, no edges).
- Imports + callers correctly classified.
- Ranking prefers neighbours touching multiple chunk files.
- Budget enforcement truncates with a marker.
- Env-var-driven opt-in works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from faultline.analyzer.symbol_graph import ImportEdge, SymbolGraph
from faultline.extractors.dependency_context import (
    DEFAULT_BUDGET_CHARS,
    DependencyContextInjector,
    build_dependency_context_block,
    is_enabled,
)


# ── Helpers to fabricate a SymbolGraph quickly ────────────────────────


@dataclass
class _Rng:
    name: str
    start_line: int = 1
    end_line: int = 10
    kind: str = "function"


def _graph(*, forward: dict[str, list[ImportEdge]], exports: dict[str, list]) -> SymbolGraph:
    g = SymbolGraph()
    g.forward = forward
    # Build reverse from forward
    rev: dict[str, list[ImportEdge]] = {}
    for caller, edges in forward.items():
        for e in edges:
            rev.setdefault(e.target_file, []).append(
                ImportEdge(target_file=caller, target_symbol=e.target_symbol)
            )
    g.reverse = rev
    g.exports = exports
    return g


# ── Tests ─────────────────────────────────────────────────────────────


def test_empty_block_for_single_file_chunk():
    g = SymbolGraph()
    assert build_dependency_context_block(chunk_files=["a.ts"], graph=g) == ""


def test_empty_block_when_no_edges():
    g = SymbolGraph()
    out = build_dependency_context_block(
        chunk_files=["a.ts", "b.ts"], graph=g,
    )
    assert out == ""


def test_block_lists_imports_with_exports():
    g = _graph(
        forward={
            "src/auth/login.ts": [
                ImportEdge(target_file="src/db/users.ts", target_symbol="User"),
                ImportEdge(target_file="src/db/users.ts", target_symbol="getUser"),
            ],
        },
        exports={
            "src/db/users.ts": [
                _Rng("User", 1, 30),
                _Rng("getUser", 31, 50),
                _Rng("createUser", 51, 70),
            ],
        },
    )
    block = build_dependency_context_block(
        chunk_files=["src/auth/login.ts", "src/auth/jwt.ts"], graph=g,
    )
    assert "DEPENDENCY CONTEXT" in block
    assert "src/auth/login.ts" in block
    assert "src/auth/jwt.ts" in block
    assert "This chunk IMPORTS:" in block
    assert "src/db/users.ts" in block
    assert "User" in block        # used symbol
    assert "createUser" in block  # in exports list of neighbour


def test_block_lists_callers_outside_chunk():
    g = _graph(
        forward={
            "src/api/routes.ts": [
                ImportEdge(target_file="src/auth/login.ts", target_symbol="login"),
            ],
        },
        exports={
            "src/api/routes.ts": [_Rng("registerRoutes", 1, 20)],
        },
    )
    block = build_dependency_context_block(
        chunk_files=["src/auth/login.ts", "src/auth/jwt.ts"], graph=g,
    )
    assert "IMPORTED BY" in block
    assert "src/api/routes.ts" in block


def test_neighbour_touching_more_chunk_files_ranks_higher():
    g = _graph(
        forward={
            "src/auth/a.ts": [
                ImportEdge(target_file="src/db/users.ts", target_symbol="User"),
                ImportEdge(target_file="src/util/log.ts", target_symbol="log"),
            ],
            "src/auth/b.ts": [
                ImportEdge(target_file="src/db/users.ts", target_symbol="User"),
            ],
        },
        exports={
            "src/db/users.ts": [_Rng("User", 1, 10)],
            "src/util/log.ts": [_Rng("log", 1, 5)],
        },
    )
    block = build_dependency_context_block(
        chunk_files=["src/auth/a.ts", "src/auth/b.ts"], graph=g,
    )
    # users.ts (touches both chunk files) should appear before log.ts
    users_pos = block.find("src/db/users.ts")
    log_pos = block.find("src/util/log.ts")
    assert users_pos != -1 and log_pos != -1
    assert users_pos < log_pos


def test_synthetic_http_edge_is_skipped():
    g = _graph(
        forward={
            "src/web/client.ts": [
                ImportEdge(target_file="src/auth/login.ts", target_symbol="@http"),
            ],
        },
        exports={"src/auth/login.ts": [_Rng("login", 1, 10)]},
    )
    block = build_dependency_context_block(
        chunk_files=["src/auth/login.ts", "src/auth/jwt.ts"], graph=g,
    )
    # @http edge should not have produced a caller entry
    assert "@http" not in block


def test_budget_enforcement_truncates_with_marker():
    big_forward: dict[str, list[ImportEdge]] = {}
    big_exports: dict[str, list] = {}
    for i in range(60):
        caller = f"caller_{i}.ts"
        big_forward[caller] = [
            ImportEdge(target_file="src/auth/login.ts", target_symbol=f"sym{i}")
        ]
        big_exports[caller] = [_Rng(f"export{i}", 1, 10)]
    g = _graph(forward=big_forward, exports=big_exports)
    block = build_dependency_context_block(
        chunk_files=["src/auth/login.ts", "src/auth/jwt.ts"],
        graph=g, budget_chars=500,
    )
    assert len(block) <= 500 + 200    # close to budget, allows footer slack
    assert "more omitted for budget" in block


def test_third_party_omitted_note_present():
    g = _graph(
        forward={
            "src/x.ts": [ImportEdge(target_file="src/y.ts", target_symbol="Y")],
        },
        exports={"src/y.ts": [_Rng("Y", 1, 10)]},
    )
    block = build_dependency_context_block(
        chunk_files=["src/x.ts", "src/z.ts"], graph=g,
    )
    assert "third-party imports omitted" in block


def test_is_enabled_env_var_off_by_default(monkeypatch):
    monkeypatch.delenv("FAULTLINE_DEP_CONTEXT", raising=False)
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_enabled_env_var_on(monkeypatch, val):
    monkeypatch.setenv("FAULTLINE_DEP_CONTEXT", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_is_enabled_env_var_off(monkeypatch, val):
    monkeypatch.setenv("FAULTLINE_DEP_CONTEXT", val)
    assert is_enabled() is False


def test_injector_class_wraps_function():
    g = _graph(
        forward={"a.ts": [ImportEdge(target_file="b.ts", target_symbol="X")]},
        exports={"b.ts": [_Rng("X", 1, 5)]},
    )
    injector = DependencyContextInjector(graph=g)
    assert injector.name == "dependency-context-injector"
    out = injector.context_for(["a.ts", "c.ts"])
    assert "b.ts" in out


def test_default_budget_is_reasonable():
    assert 800 <= DEFAULT_BUDGET_CHARS <= 3000
