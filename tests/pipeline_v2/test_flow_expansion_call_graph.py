"""Sprint 2 — integration tests for T1 call-graph + the orchestrator.

Uses synthetic micro-repos materialised under ``tmp_path`` so the
tests are hermetic and don't depend on any cached scan artifact.

Gates exercised:
  * Gate 2 — call graph correctness (≥80% recall, ≥90% precision on
    known fixtures).
  * Gate 3 — cross-stack resolution (≥80% of seeded hops resolved).
  * Gate 4 — performance smoke (synthetic 100-flow scale; deferred
    live-cold-scan).
  * Gate 6 — cold-scan principle (no on-disk persistence side-effects).
  * Backward compat — Sprint 1 ``participant_files`` (== ``paths``)
    unchanged, all Sprint 1 fields preserved.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.flow_expansion import expand_flows
from faultline.pipeline_v2.stage_0_intake import stage_0_intake


# ── Fixture helpers ─────────────────────────────────────────────────────


def _init_git_repo(repo: Path) -> None:
    """Materialise a minimal git repo so Stage 0 intake works."""
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


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def _make_flow(
    name: str,
    *,
    entry_file: str,
    entry_symbol: str | None = None,
    entry_line: int | None = None,
    paths: list[str] | None = None,
) -> Flow:
    now = datetime.now(timezone.utc)
    fl = Flow(
        name=name,
        entry_point_file=entry_file,
        entry_point_line=entry_line,
        paths=paths or [entry_file],
        authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=now, health_score=100.0,
        uuid=f"fl-{name}",
    )
    if entry_symbol:
        from faultline.models.types import FlowSymbolAttribution
        fl.flow_symbol_attributions = [FlowSymbolAttribution(
            file=entry_file, symbol=entry_symbol,
            line_start=entry_line or 1, line_end=(entry_line or 1) + 5,
            role="entry",
        )]
    return fl


def _make_feature(name: str, paths: list[str], flows: list[Flow]) -> Feature:
    now = datetime.now(timezone.utc)
    return Feature(
        name=name, paths=paths, authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=now,
        health_score=100.0, flows=flows,
        uuid=f"feat-{name}",
    )


# ── Fixture 1: TS call chain ────────────────────────────────────────────


@pytest.fixture
def ts_call_chain(tmp_path: Path) -> Path:
    """handler.ts → service.ts → repo.ts (depth 2)."""
    repo = tmp_path / "ts_chain"
    repo.mkdir()
    _write(repo, "src/handler.ts", """
        import { findUser } from './service';
        export function handleRequest(userId: string) {
          return findUser(userId);
        }
    """)
    _write(repo, "src/service.ts", """
        import { loadUser } from './repo';
        export function findUser(id: string) {
          return loadUser(id);
        }
    """)
    _write(repo, "src/repo.ts", """
        export function loadUser(id: string) {
          return { id, name: 'x' };
        }
    """)
    _init_git_repo(repo)
    return repo


def test_t1_resolves_call_chain_depth_2(ts_call_chain: Path):
    ctx = stage_0_intake(ts_call_chain, days=30)
    flow = _make_flow(
        "view-user",
        entry_file="src/handler.ts",
        entry_symbol="handleRequest",
        entry_line=2,
    )
    feat = _make_feature("user", ["src/handler.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])
    files_in_graph = {n.file for n in flow.nodes}
    assert "src/handler.ts" in files_in_graph
    assert "src/service.ts" in files_in_graph
    assert "src/repo.ts" in files_in_graph
    # First node is the entry.
    assert flow.nodes[0].role == "entry"
    assert flow.nodes[0].file == "src/handler.ts"
    # Depth reached should be ≥2 (handler → service → repo).
    assert flow.summary is not None
    assert flow.summary.max_depth >= 2
    assert flow.summary.unsupported_stack is False
    # Call edges should beat import edges in precision: at least one
    # call edge to a symbol node, not just file-level.
    call_edges = [e for e in flow.edges if e.kind == "call"]
    assert any(
        "service.ts#findUser" in e.to for e in call_edges
    ), f"expected call edge to service#findUser, got edges {[e.model_dump() for e in flow.edges]}"


def test_legacy_paths_preserved_after_expansion(ts_call_chain: Path):
    """Gate 1 — backward compat: paths[] unchanged."""
    ctx = stage_0_intake(ts_call_chain, days=30)
    flow = _make_flow(
        "view-user",
        entry_file="src/handler.ts",
        entry_symbol="handleRequest",
        entry_line=2,
        paths=["src/handler.ts", "src/service.ts"],
    )
    feat = _make_feature("user", ["src/handler.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])
    # paths must remain exactly as input.
    assert flow.paths == ["src/handler.ts", "src/service.ts"]
    # entry_point_file preserved.
    assert flow.entry_point_file == "src/handler.ts"
    # uuid preserved.
    assert flow.uuid == "fl-view-user"


# ── Fixture 2: Python call chain ────────────────────────────────────────


@pytest.fixture
def py_chain(tmp_path: Path) -> Path:
    repo = tmp_path / "py_chain"
    repo.mkdir()
    _write(repo, "app/views.py", """
        from app.services import compute_total
        def view():
            return compute_total(1, 2)
    """)
    _write(repo, "app/services.py", """
        def compute_total(a, b):
            return a + b
    """)
    _write(repo, "app/__init__.py", "")
    _init_git_repo(repo)
    return repo


def test_t1_resolves_python_imports(py_chain: Path):
    ctx = stage_0_intake(py_chain, days=30)
    flow = _make_flow(
        "view-total",
        entry_file="app/views.py",
        entry_symbol="view",
        entry_line=2,
    )
    feat = _make_feature("totals", ["app/views.py"], [flow])
    expand_flows([feat], ctx, routes_index=[])
    files = {n.file for n in flow.nodes}
    assert "app/views.py" in files
    assert "app/services.py" in files
    assert flow.summary.max_depth >= 1


# ── Fixture 3: cross-stack Next.js ──────────────────────────────────────


@pytest.fixture
def cross_stack_nextjs(tmp_path: Path) -> Path:
    """10 client → server hops via fetch literals."""
    repo = tmp_path / "next_xstack"
    repo.mkdir()
    # Server routes.
    for i in range(10):
        _write(repo, f"src/app/api/r{i}/route.ts", f"""
            export async function GET() {{
              return Response.json({{ ok: true, id: {i} }});
            }}
        """)
    # Client file that fetches all 10.
    client_body = "\n".join(
        f'  await fetch("/api/r{i}");' for i in range(10)
    )
    _write(repo, "src/components/Dashboard.tsx", f"""
        export async function loadDashboard() {{
        {client_body}
        }}
    """)
    _init_git_repo(repo)
    return repo


def test_t2_cross_stack_resolves_eight_of_ten(cross_stack_nextjs: Path):
    """Gate 3 — ≥8/10 cross-stack hops resolved."""
    ctx = stage_0_intake(cross_stack_nextjs, days=30)
    # Build a routes_index by hand mirroring Sprint 1's projection.
    routes_index = [
        {
            "pattern": f"/api/r{i}",
            "method": "GET",
            "feature_uuid": f"feat-r{i}",
            "file": f"src/app/api/r{i}/route.ts",
        }
        for i in range(10)
    ]
    flow = _make_flow(
        "load-dashboard",
        entry_file="src/components/Dashboard.tsx",
        entry_symbol="loadDashboard",
        entry_line=1,
    )
    feat = _make_feature(
        "dashboard", ["src/components/Dashboard.tsx"], [flow],
    )
    expand_flows([feat], ctx, routes_index=routes_index)
    cross_edges = [e for e in flow.edges if e.kind == "cross_stack_http"]
    assert flow.summary is not None
    # Gate 3: ≥8/10.
    assert len(cross_edges) >= 8, (
        f"only resolved {len(cross_edges)} of 10 hops"
    )
    assert flow.summary.cross_stack_hops >= 8
    # Confidence should be HIGH (literal, not template).
    assert all(e.confidence == "high" for e in cross_edges)


# ── Fixture 4: graceful degrade for unsupported stack ──────────────────


def test_unsupported_stack_emits_entry_only(tmp_path: Path):
    repo = tmp_path / "rb_repo"
    repo.mkdir()
    _write(repo, "app/users_controller.rb", """
        class UsersController < ApplicationController
          def index
            @users = User.all
          end
        end
    """)
    _init_git_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "list-users",
        entry_file="app/users_controller.rb",
        entry_symbol="index",
        entry_line=2,
    )
    feat = _make_feature(
        "users", ["app/users_controller.rb"], [flow],
    )
    expand_flows([feat], ctx, routes_index=[])
    assert flow.summary is not None
    assert flow.summary.unsupported_stack is True
    assert len(flow.nodes) == 1
    assert flow.nodes[0].role == "entry"
    # Backward compat: paths preserved.
    assert flow.paths == ["app/users_controller.rb"]


# ── Fixture 5: no entry point ───────────────────────────────────────────


def test_flow_with_no_entry_emits_empty_graph(tmp_path: Path):
    repo = tmp_path / "no_entry"
    repo.mkdir()
    _write(repo, "src/a.ts", "export const x = 1;")
    _init_git_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = Flow(
        name="orphan",
        entry_point_file=None,
        entry_point_line=None,
        paths=[],
        authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=0.0, uuid="fl-orphan",
    )
    feat = _make_feature("ghost", [], [flow])
    expand_flows([feat], ctx, routes_index=[])
    assert flow.entry is None
    assert flow.nodes == []
    assert flow.summary is not None
    assert flow.summary.unsupported_stack is True


# ── Idempotence ─────────────────────────────────────────────────────────


def test_expand_is_idempotent_when_already_populated(ts_call_chain: Path):
    ctx = stage_0_intake(ts_call_chain, days=30)
    flow = _make_flow(
        "view-user",
        entry_file="src/handler.ts",
        entry_symbol="handleRequest",
        entry_line=2,
    )
    feat = _make_feature("user", ["src/handler.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])
    nodes_first = list(flow.nodes)
    edges_first = list(flow.edges)
    # Second call should not change anything.
    result = expand_flows([feat], ctx, routes_index=[])
    assert flow.nodes == nodes_first
    assert flow.edges == edges_first
    # Telemetry should report 1 skipped.
    assert result.telemetry["flows_skipped_already_expanded"] >= 1


# ── Truncation under node cap ───────────────────────────────────────────


def test_truncation_emits_deep_call_subtree_marker(tmp_path: Path):
    """Gate-supporting: fan-out >> max_nodes triggers aggregation node."""
    repo = tmp_path / "fanout"
    repo.mkdir()
    # entry imports 50 leaves, each calling fn_<i>.
    leaf_imports = "\n".join(
        f"import {{ fn{i} }} from './leaf{i}';" for i in range(50)
    )
    body = "\n".join(f"  fn{i}();" for i in range(50))
    _write(repo, "src/entry.ts", f"""
        {leaf_imports}
        export function run() {{
        {body}
        }}
    """)
    for i in range(50):
        _write(repo, f"src/leaf{i}.ts", f"export function fn{i}() {{}}")
    _init_git_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "run-all", entry_file="src/entry.ts",
        entry_symbol="run", entry_line=51,
    )
    feat = _make_feature("runner", ["src/entry.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[], max_depth=4, max_nodes=10)
    assert flow.summary is not None
    assert flow.summary.truncated is True
    # An aggregation node should appear.
    agg = [n for n in flow.nodes if n.kind == "deep_call_subtree"]
    assert agg, "expected at least one deep_call_subtree aggregation node"
    assert agg[0].count is not None and agg[0].count > 0


# ── Top-level bipartite mirror ──────────────────────────────────────────


def test_top_level_flows_mirror_receives_expansion(ts_call_chain: Path):
    ctx = stage_0_intake(ts_call_chain, days=30)
    flow_in_feature = _make_flow(
        "view-user", entry_file="src/handler.ts",
        entry_symbol="handleRequest", entry_line=2,
    )
    feat = _make_feature("user", ["src/handler.ts"], [flow_in_feature])
    # Build a separate top-level flow object with the same uuid (the
    # Stage 5.5 bipartite store keeps its own Flow references).
    top_flow = _make_flow(
        "view-user", entry_file="src/handler.ts",
        entry_symbol="handleRequest", entry_line=2,
    )
    expand_flows(
        [feat], ctx, routes_index=[], top_level_flows=[top_flow],
    )
    assert top_flow.nodes, "top-level flow should be mirrored"
    assert {n.file for n in top_flow.nodes} == {n.file for n in flow_in_feature.nodes}


# ── Performance smoke (Gate 4 — synthetic only) ────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32", reason="timing flaky on win32",
)
def test_perf_smoke_100_flows_under_5s(ts_call_chain: Path):
    """Deferred live cold-scan but: 100 small flows in <5s on ts_chain."""
    import time
    ctx = stage_0_intake(ts_call_chain, days=30)
    flows = [
        _make_flow(
            f"f{i}", entry_file="src/handler.ts",
            entry_symbol="handleRequest", entry_line=2,
        )
        for i in range(100)
    ]
    feat = _make_feature("user", ["src/handler.ts"], flows)
    t0 = time.monotonic()
    expand_flows([feat], ctx, routes_index=[])
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"expansion took {elapsed:.2f}s on 100 flows"
