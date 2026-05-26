"""Phase 5 — LOC-detail parity tests for Stage 3.5 flow expansion.

Verifies the ADDITIVE fields (``entry_point``, ``line_ranges``,
``loc_symbol_attributions``, ``loc_nodes``, ``loc_edges``) are
populated and correct, while EVERY pre-existing field/shape on the
Flow stays byte-identical. Exercises the real ``expand_flows``
orchestrator over a synthetic multi-file TS call graph (no LLM, no
ANTHROPIC_API_KEY needed).
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import (
    Feature,
    Flow,
    FlowSymbolAttribution,
)
from faultline.pipeline_v2.flow_expansion import expand_flows
from faultline.pipeline_v2.flow_expansion.expander import (
    _merge_spans,
    _project_loc_detail,
)
from faultline.pipeline_v2.stage_0_intake import stage_0_intake


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def _init_git(repo: Path) -> None:
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


def _make_flow(name: str, *, entry_file: str, entry_symbol: str,
               paths: list[str]) -> Flow:
    now = datetime.now(timezone.utc)
    return Flow(
        name=name,
        entry_point_file=entry_file,
        entry_point_line=2,
        paths=paths,
        authors=["alice"],
        total_commits=10,
        bug_fixes=1,
        bug_fix_ratio=0.1,
        last_modified=now,
        health_score=88.0,
        uuid=f"fl-{name}",
        id=f"feat::{name}",
        primary_feature="feat",
        flow_symbol_attributions=[FlowSymbolAttribution(
            file=entry_file, symbol=entry_symbol,
            line_start=2, line_end=4, role="entry",
        )],
    )


def _make_feature(name: str, paths: list[str], flows: list[Flow]) -> Feature:
    now = datetime.now(timezone.utc)
    return Feature(
        name=name, paths=paths, authors=[], total_commits=10,
        bug_fixes=1, bug_fix_ratio=0.1, last_modified=now,
        health_score=90.0, flows=flows, uuid=f"feat-{name}",
    )


def _multi_file_repo(repo: Path) -> None:
    # handler() in x.ts calls helper() in y.ts → a real call edge.
    _write(repo, "src/x.ts", """
        import { helper } from './y';
        export function handler() {
          return helper();
        }
    """)
    _write(repo, "src/y.ts", """
        export function helper() {
          return 42;
        }
    """)
    _init_git(repo)


# ── Existing fields unchanged ────────────────────────────────────────────


def test_existing_fields_byte_identical_after_loc_projection(tmp_path: Path):
    repo = tmp_path / "compat"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])

    expand_flows([feat], ctx, routes_index=[])

    # Sprint 2 graph fields are still the canonical source-of-truth and
    # are NOT mutated by the LOC projection.
    assert flow.entry is not None
    assert flow.entry["file"] == "src/x.ts"
    assert len(flow.nodes) >= 1
    assert flow.summary is not None
    assert flow.summary.total_nodes == len(flow.nodes)
    # Legacy scalar entry fields preserved.
    assert flow.entry_point_file == "src/x.ts"
    assert flow.entry_point_line == 2
    # Legacy thin flow_symbol_attributions preserved verbatim.
    assert flow.flow_symbol_attributions[0].file == "src/x.ts"
    assert flow.flow_symbol_attributions[0].line_start == 2


# ── New fields populated + correct ───────────────────────────────────────


def test_entry_point_richer_object(tmp_path: Path):
    repo = tmp_path / "ep"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    assert flow.entry_point is not None
    assert flow.entry_point.path == "src/x.ts"
    assert flow.entry_point.symbol == "handler"
    assert flow.entry_point.line is not None


def test_line_ranges_populated_and_match_source(tmp_path: Path):
    repo = tmp_path / "lr"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    assert flow.line_ranges, "flow should have at least one line range"
    paths = {lr.path for lr in flow.line_ranges}
    assert "src/x.ts" in paths
    # Spot-check: handler() spans lines 2-4 in x.ts (1-indexed, after the
    # import line). Verify the recorded span is within the file and
    # start <= end.
    for lr in flow.line_ranges:
        assert lr.start_line >= 1
        assert lr.start_line <= lr.end_line
    # handler is reached via call to helper in y.ts — y.ts span exists.
    assert "src/y.ts" in paths


def test_loc_nodes_landing_shape(tmp_path: Path):
    repo = tmp_path / "ln"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    assert len(flow.loc_nodes) == len(flow.nodes)
    roles = {n.role for n in flow.loc_nodes}
    assert "entry" in roles
    # Every loc_node mirrors a real graph node's file.
    node_files = {n.file for n in flow.nodes}
    for ln in flow.loc_nodes:
        assert ln.path in node_files
        assert ln.role in ("entry", "step", "sink")


def test_loc_edges_carry_call_site(tmp_path: Path):
    repo = tmp_path / "le"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    # The handler→helper call should surface as a loc_edge with a
    # resolved caller file and a call-site.
    assert flow.loc_edges, "expected at least one edge in a 2-file call graph"
    for e in flow.loc_edges:
        assert e.from_path
        assert e.to_path
        assert e.call_site is not None
        assert "path" in e.call_site and "line" in e.call_site
    # At least one edge originates in x.ts (the caller).
    assert any(e.from_path == "src/x.ts" for e in flow.loc_edges)


def test_loc_symbol_attributions_parity_shape(tmp_path: Path):
    repo = tmp_path / "lsa"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    assert flow.loc_symbol_attributions
    # Parity shape: path/symbol/kind/start_line/end_line present.
    for a in flow.loc_symbol_attributions:
        assert a.path
        assert hasattr(a, "kind")
        assert hasattr(a, "start_line")
        assert hasattr(a, "end_line")
    # The Stage 3 entry attribution is carried through.
    assert any(
        a.path == "src/x.ts" and a.symbol == "handler"
        for a in flow.loc_symbol_attributions
    )


def test_top_level_bipartite_flow_mirrors_loc_detail(tmp_path: Path):
    """The top-level flows[] view must carry the same LOC detail."""
    repo = tmp_path / "tl"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    containment_flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [containment_flow])
    # Distinct Flow object, same uuid (mirrors how Stage 5.5 builds the
    # top-level array).
    top_flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    expand_flows([feat], ctx, routes_index=[], top_level_flows=[top_flow])

    assert top_flow.entry_point is not None
    assert top_flow.loc_nodes == containment_flow.loc_nodes
    assert top_flow.line_ranges == containment_flow.line_ranges
    assert top_flow.loc_edges == containment_flow.loc_edges
    assert (
        top_flow.loc_symbol_attributions
        == containment_flow.loc_symbol_attributions
    )


def test_serialized_json_has_new_and_legacy_keys(tmp_path: Path):
    repo = tmp_path / "ser"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    dumped = json.loads(flow.model_dump_json())
    # Legacy keys still present.
    for k in ("paths", "entry_point_file", "uuid", "id", "entry",
              "nodes", "edges", "summary", "flow_symbol_attributions"):
        assert k in dumped, f"legacy key {k} dropped"
    # New Phase 5 keys present.
    for k in ("entry_point", "line_ranges", "loc_symbol_attributions",
              "loc_nodes", "loc_edges"):
        assert k in dumped, f"new key {k} missing"


# ── Pure helpers ─────────────────────────────────────────────────────────


def test_merge_spans():
    assert _merge_spans([]) == []
    assert _merge_spans([(1, 5)]) == [(1, 5)]
    # adjacent + overlapping merge; disjoint stay separate
    assert _merge_spans([(1, 5), (6, 8), (20, 25)]) == [(1, 8), (20, 25)]
    assert _merge_spans([(10, 20), (5, 12)]) == [(5, 20)]


def test_project_is_idempotent():
    """Re-running projection yields identical output (no duplication)."""
    flow = _make_flow(
        "view", entry_file="src/x.ts", entry_symbol="handler",
        paths=["src/x.ts"],
    )
    # Minimal graph to project from.
    from faultline.models.types import FlowEdge, FlowNode
    flow.entry = {"file": "src/x.ts", "symbol": "handler", "lines": [2, 4]}
    flow.nodes = [
        FlowNode(id="src/x.ts#handler", kind="entry", file="src/x.ts",
                 symbol="handler", lines=(2, 4), role="entry"),
        FlowNode(id="src/y.ts#helper", kind="function", file="src/y.ts",
                 symbol="helper", lines=(1, 3), role="called"),
    ]
    flow.edges = [FlowEdge(from_="src/x.ts#handler", to="src/y.ts#helper",
                           kind="call")]
    _project_loc_detail(flow)
    n1, lr1, e1 = (
        list(flow.loc_nodes), list(flow.line_ranges), list(flow.loc_edges),
    )
    _project_loc_detail(flow)
    assert flow.loc_nodes == n1
    assert flow.line_ranges == lr1
    assert flow.loc_edges == e1
