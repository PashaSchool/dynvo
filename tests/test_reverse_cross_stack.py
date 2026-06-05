"""Unit tests for the reverse cross-stack ui-participant attachment.

Covers Gap 1: backend-seeded flows gain frontend ``ui``-role
participants when a frontend file calls their route. Asserts the pass is
ADDITIVE (no nodes / edges / flow_symbol_attributions touched) and
graceful when no frontend exists.

Uses synthetic neutral fixtures only ([[rule-no-repo-specific-paths]]).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from faultline.models.types import Flow, FlowNode
from faultline.pipeline_v2.flow_expansion.reverse_cross_stack import (
    attach_reverse_cross_stack,
    attach_ui_participants,
    build_reverse_index,
)


# ── Minimal ReachContext fake (only file_set + repo_path + signatures) ──


@dataclass
class _FakeSig:
    source: str


@dataclass
class _FakeRctx:
    repo_path: str
    file_set: frozenset
    signatures: dict = field(default_factory=dict)


def _backend_flow(
    *, name: str, entry_file: str, route_pattern: str, route_method: str = "GET"
) -> Flow:
    """A backend-seeded flow with a cross_stack_server node for its route."""
    server_node = FlowNode(
        id=f"{entry_file}#{route_method}:{route_pattern}",
        kind="route_handler",
        file=entry_file,
        symbol=None,
        lines=None,
        role="cross_stack_server",
        confidence="high",
    )
    entry_node = FlowNode(
        id=f"{entry_file}#handler",
        kind="entry",
        file=entry_file,
        symbol="handler",
        lines=(1, 10),
        role="entry",
        confidence="high",
    )
    return Flow(
        name=name,
        paths=[entry_file],
        entry_point_file=entry_file,
        nodes=[entry_node, server_node],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified="2026-01-01T00:00:00Z",
        health_score=100.0,
    )


def _routes_index(entry_file: str, pattern: str, method: str = "GET"):
    return [{
        "pattern": pattern,
        "method": method,
        "feature_uuid": "feat-1",
        "file": entry_file,
    }]


def test_frontend_caller_attached_as_ui_participant():
    entry_file = "backend/api/detectors.py"
    front_file = "frontend/src/api/detectors.ts"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({entry_file, front_file}),
        signatures={
            front_file: _FakeSig(
                source="export const list = () => fetch('/api/detectors')",
            ),
        },
    )
    routes = _routes_index(entry_file, "/api/detectors")
    flow = _backend_flow(
        name="list-detectors-flow",
        entry_file=entry_file,
        route_pattern="/api/detectors",
    )

    index = build_reverse_index(rctx, routes)
    assert front_file in index.frontend_callers
    assert front_file in index.by_route_file[entry_file]

    attached = attach_ui_participants(flow, index)
    assert attached == 1
    ui = [p for p in flow.participants if p.layer == "ui"]
    assert len(ui) == 1
    assert ui[0].path == front_file
    assert ui[0].role == "ui"


def test_additive_no_core_loc_fields_touched():
    entry_file = "backend/api/orders.py"
    front_file = "frontend/src/orders.tsx"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({entry_file, front_file}),
        signatures={
            front_file: _FakeSig(source="axios.get('/api/orders')"),
        },
    )
    routes = _routes_index(entry_file, "/api/orders")
    flow = _backend_flow(
        name="orders-flow", entry_file=entry_file, route_pattern="/api/orders",
    )
    nodes_before = list(flow.nodes)
    edges_before = list(flow.edges)
    fsa_before = list(flow.flow_symbol_attributions)

    index = build_reverse_index(rctx, routes)
    attach_ui_participants(flow, index)

    # Core-LOC fields untouched (these feed _project_loc_detail).
    assert flow.nodes == nodes_before
    assert flow.edges == edges_before
    assert flow.flow_symbol_attributions == fsa_before
    # Only participants grew.
    assert any(p.layer == "ui" for p in flow.participants)


def test_graceful_when_no_frontend():
    entry_file = "backend/api/users.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({entry_file}),
        signatures={},
    )
    routes = _routes_index(entry_file, "/api/users")
    flow = _backend_flow(
        name="users-flow", entry_file=entry_file, route_pattern="/api/users",
    )
    index = build_reverse_index(rctx, routes)
    attached = attach_ui_participants(flow, index)
    assert attached == 0
    assert not any(p.layer == "ui" for p in flow.participants)


def test_no_match_when_url_differs():
    entry_file = "backend/api/detectors.py"
    front_file = "frontend/src/other.ts"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({entry_file, front_file}),
        signatures={front_file: _FakeSig(source="fetch('/api/unrelated')")},
    )
    routes = _routes_index(entry_file, "/api/detectors")
    flow = _backend_flow(
        name="d-flow", entry_file=entry_file, route_pattern="/api/detectors",
    )
    index = build_reverse_index(rctx, routes)
    assert attach_ui_participants(flow, index) == 0


def test_route_handler_not_treated_as_its_own_caller():
    # A backend route file that itself contains a `requests.get` to ANOTHER
    # route must not be indexed as a frontend caller (it is a route file).
    entry_file = "backend/api/a.py"
    other_route = "backend/api/b.py"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({entry_file, other_route}),
        signatures={
            entry_file: _FakeSig(source="requests.get('/api/b')"),
        },
    )
    routes = [
        {"pattern": "/api/a", "method": "GET", "feature_uuid": "f", "file": entry_file},
        {"pattern": "/api/b", "method": "GET", "feature_uuid": "f", "file": other_route},
    ]
    index = build_reverse_index(rctx, routes)
    # entry_file is a route file -> excluded as a candidate.
    assert entry_file not in index.frontend_callers


def test_idempotent_across_two_flow_lists():
    entry_file = "backend/api/items.py"
    front_file = "frontend/src/items.ts"
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({entry_file, front_file}),
        signatures={front_file: _FakeSig(source="fetch('/api/items')")},
    )
    routes = _routes_index(entry_file, "/api/items")
    flow = _backend_flow(
        name="items-flow", entry_file=entry_file, route_pattern="/api/items",
    )
    index = build_reverse_index(rctx, routes)
    attach_reverse_cross_stack([flow], rctx, routes, index=index)
    attach_reverse_cross_stack([flow], rctx, routes, index=index)
    ui = [p for p in flow.participants if p.layer == "ui"]
    assert len(ui) == 1  # not duplicated


def test_empty_routes_index_is_noop():
    rctx = _FakeRctx(
        repo_path="/repo",
        file_set=frozenset({"frontend/src/x.ts"}),
        signatures={"frontend/src/x.ts": _FakeSig(source="fetch('/api/x')")},
    )
    index = build_reverse_index(rctx, [])
    assert not index.frontend_callers
    assert index.routes_matched == 0
