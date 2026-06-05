"""Unit tests for the deterministic per-flow test mapper (Gap 2).

Covers: a test exercising a flow is attached via (1) filename convention,
(2) entry-symbol reference, (3) route-literal reference; additive (only
test_files / test_file_count written); graceful when no tests exist.

Synthetic neutral fixtures only ([[rule-no-repo-specific-paths]]).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from faultline.models.types import Flow, FlowNode, FlowSymbolAttribution
from faultline.pipeline_v2.flow_test_mapper import (
    attach_flow_test_files,
    build_flow_test_index,
)
from faultline.pipeline_v2.flow_test_mapper import (
    tests_for_flow as _tests_for_flow,
)


@dataclass
class _FakeSig:
    source: str


@dataclass
class _FakeRctx:
    repo_path: str
    file_set: frozenset
    signatures: dict = field(default_factory=dict)


def _flow(
    *,
    name: str,
    paths: list[str],
    entry_file: str | None = None,
    entry_symbol: str | None = None,
    route_pattern: str | None = None,
) -> Flow:
    nodes: list[FlowNode] = []
    fsa: list[FlowSymbolAttribution] = []
    entry: dict | None = None
    if entry_symbol and entry_file:
        entry = {"file": entry_file, "symbol": entry_symbol, "lines": [1, 5]}
        fsa.append(FlowSymbolAttribution(
            file=entry_file, symbol=entry_symbol, role="entry",
            line_start=1, line_end=5,
        ))
    if route_pattern and entry_file:
        nodes.append(FlowNode(
            id=f"{entry_file}#GET:{route_pattern}",
            kind="route_handler", file=entry_file, symbol=None,
            lines=None, role="cross_stack_server", confidence="high",
        ))
    return Flow(
        name=name,
        paths=paths,
        entry_point_file=entry_file,
        entry=entry,
        nodes=nodes,
        flow_symbol_attributions=fsa,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified="2026-01-01T00:00:00Z",
        health_score=100.0,
    )


def test_filename_convention_attaches_sibling_test():
    src = "backend/api/detectors.py"
    test = "backend/tests/test_detectors.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src, test}),
        signatures={test: _FakeSig(source="def test_x(): pass")},
    )
    flow = _flow(name="detectors-flow", paths=[src], entry_file=src)
    index = build_flow_test_index(rctx)
    tfs = _tests_for_flow(flow, index)
    assert test in tfs


def test_entry_symbol_reference_attaches_test():
    src = "backend/svc/orders.py"
    # Test does NOT follow a filename convention for `src`, but references
    # the flow's entry symbol.
    test = "backend/tests/test_order_api.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src, test}),
        signatures={
            test: _FakeSig(
                source="from svc.orders import create_order\n"
                       "def test_it(): create_order()",
            ),
        },
    )
    flow = _flow(
        name="create-order-flow", paths=[src],
        entry_file=src, entry_symbol="create_order",
    )
    index = build_flow_test_index(rctx)
    assert test in _tests_for_flow(flow, index)


def test_route_literal_reference_attaches_e2e_spec():
    src = "backend/api/detectors.py"
    e2e = "frontend/tests/e2e/detectors.spec.ts"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src, e2e}),
        signatures={
            e2e: _FakeSig(
                source="test('list', async ({page}) => {"
                       " await page.goto('/api/detectors'); })",
            ),
        },
    )
    flow = _flow(
        name="list-detectors-flow", paths=[src],
        entry_file=src, route_pattern="/api/detectors",
    )
    index = build_flow_test_index(rctx)
    assert e2e in _tests_for_flow(flow, index)


def test_symbol_match_respects_word_boundary():
    src = "backend/svc/foo.py"
    test = "backend/tests/test_unrelated.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src, test}),
        # contains "foobar" but NOT the symbol "foo" on a boundary
        signatures={test: _FakeSig(source="x = foobar_helper()")},
    )
    flow = _flow(
        name="foo-flow", paths=[src], entry_file=src, entry_symbol="foo",
    )
    index = build_flow_test_index(rctx)
    assert test not in _tests_for_flow(flow, index)


def test_attach_is_additive_and_sets_count():
    src = "backend/api/users.py"
    test = "backend/tests/test_users.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src, test}),
        signatures={test: _FakeSig(source="def test_u(): pass")},
    )
    flow = _flow(name="users-flow", paths=[src], entry_file=src)
    nodes_before = list(flow.nodes)
    tel = attach_flow_test_files([flow], rctx)
    assert flow.test_files == [test]
    assert flow.test_file_count == 1
    assert flow.nodes == nodes_before  # core LOC untouched
    assert tel["flows_with_test_files"] == 1


def test_graceful_when_no_tests():
    src = "backend/api/x.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src}),
        signatures={},
    )
    flow = _flow(name="x-flow", paths=[src], entry_file=src)
    tel = attach_flow_test_files([flow], rctx)
    assert flow.test_files == []
    assert flow.test_file_count == 0
    assert tel["flows_with_test_files"] == 0


def test_idempotent():
    src = "backend/api/y.py"
    test = "backend/tests/test_y.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({src, test}),
        signatures={test: _FakeSig(source="def test_y(): pass")},
    )
    flow = _flow(name="y-flow", paths=[src], entry_file=src)
    index = build_flow_test_index(rctx)
    attach_flow_test_files([flow], rctx, index=index)
    attach_flow_test_files([flow], rctx, index=index)
    assert flow.test_files == [test]
    assert flow.test_file_count == 1
