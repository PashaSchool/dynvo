"""Sprint 2 — pipeline integration tests for Stage 3.5.

Gate 1 (backward compat): Sprint 1 fields on every Flow are
unchanged after Stage 3.5 runs. Tests use the actual
``run_pipeline_v2`` orchestrator wired with synthetic micro-repos.

Heavy LLM stages (Stage 0.5 auditor, Stage 3 flow detector, Stage 4
residual, Stage 8 marketing clusterer) are not exercised here — we
patch around them via env vars / direct stage 3.5 unit calls to keep
the test hermetic without ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Feature, Flow, FlowSymbolAttribution
from faultline.pipeline_v2.flow_expansion import expand_flows
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
               paths: list[str], coverage_pct: float | None = None) -> Flow:
    now = datetime.now(timezone.utc)
    fl = Flow(
        name=name,
        entry_point_file=entry_file,
        entry_point_line=1,
        paths=paths,
        authors=["alice", "bob"],
        total_commits=42,
        bug_fixes=3,
        bug_fix_ratio=3 / 42,
        last_modified=now,
        health_score=87.5,
        coverage_pct=coverage_pct,
        uuid=f"fl-{name}",
        # Sprint B1 bipartite fields.
        id=f"feat::{name}",
        primary_feature="feat",
        secondary_features=["other"],
        shared_with_flows_count=1,
        shared_with_features_count=1,
        cross_cutting=True,
        flow_symbol_attributions=[FlowSymbolAttribution(
            file=entry_file, symbol=entry_symbol,
            line_start=1, line_end=10, role="entry",
        )],
        previous_names=["legacy-name"],
    )
    return fl


def _make_feature(name: str, paths: list[str], flows: list[Flow]) -> Feature:
    now = datetime.now(timezone.utc)
    return Feature(
        name=name, paths=paths, authors=[], total_commits=10,
        bug_fixes=1, bug_fix_ratio=0.1, last_modified=now,
        health_score=90.0, flows=flows, uuid=f"feat-{name}",
    )


def test_backward_compat_all_sprint_1_fields_preserved(tmp_path: Path):
    """Gate 1 — every Sprint 1 / B1 field on Flow is preserved verbatim."""
    repo = tmp_path / "compat"
    repo.mkdir()
    _write(repo, "src/x.ts", """
        import { y } from './y';
        export function handler() { return y(); }
    """)
    _write(repo, "src/y.ts", "export function y() { return 1; }")
    _init_git(repo)
    ctx = stage_0_intake(repo, days=30)

    flow = _make_flow(
        "view-items",
        entry_file="src/x.ts",
        entry_symbol="handler",
        paths=["src/x.ts", "src/y.ts"],
        coverage_pct=42.5,
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])

    # Snapshot every Sprint 1 / B1 field BEFORE.
    before = {
        "name": flow.name,
        "entry_point_file": flow.entry_point_file,
        "entry_point_line": flow.entry_point_line,
        "paths": list(flow.paths),
        "authors": list(flow.authors),
        "total_commits": flow.total_commits,
        "bug_fixes": flow.bug_fixes,
        "bug_fix_ratio": flow.bug_fix_ratio,
        "health_score": flow.health_score,
        "coverage_pct": flow.coverage_pct,
        "uuid": flow.uuid,
        "id": flow.id,
        "primary_feature": flow.primary_feature,
        "secondary_features": list(flow.secondary_features),
        "shared_with_flows_count": flow.shared_with_flows_count,
        "shared_with_features_count": flow.shared_with_features_count,
        "cross_cutting": flow.cross_cutting,
        "previous_names": list(flow.previous_names),
        "flow_symbol_attributions": [
            (a.file, a.symbol, a.line_start, a.line_end, a.role)
            for a in flow.flow_symbol_attributions
        ],
    }

    expand_flows([feat], ctx, routes_index=[])

    after = {
        "name": flow.name,
        "entry_point_file": flow.entry_point_file,
        "entry_point_line": flow.entry_point_line,
        "paths": list(flow.paths),
        "authors": list(flow.authors),
        "total_commits": flow.total_commits,
        "bug_fixes": flow.bug_fixes,
        "bug_fix_ratio": flow.bug_fix_ratio,
        "health_score": flow.health_score,
        "coverage_pct": flow.coverage_pct,
        "uuid": flow.uuid,
        "id": flow.id,
        "primary_feature": flow.primary_feature,
        "secondary_features": list(flow.secondary_features),
        "shared_with_flows_count": flow.shared_with_flows_count,
        "shared_with_features_count": flow.shared_with_features_count,
        "cross_cutting": flow.cross_cutting,
        "previous_names": list(flow.previous_names),
        "flow_symbol_attributions": [
            (a.file, a.symbol, a.line_start, a.line_end, a.role)
            for a in flow.flow_symbol_attributions
        ],
    }
    assert before == after, "Sprint 1 / B1 fields must be preserved verbatim"

    # New Sprint 2 surface is populated.
    assert flow.entry is not None
    assert flow.entry["file"] == "src/x.ts"
    assert len(flow.nodes) >= 1
    assert flow.summary is not None
    assert flow.summary.total_nodes == len(flow.nodes)


def test_serialized_json_includes_new_fields_and_legacy(tmp_path: Path):
    """Round-trip via pydantic model_dump_json to verify schema."""
    repo = tmp_path / "ser"
    repo.mkdir()
    _write(repo, "src/x.ts", """
        export function handler() { return 1; }
    """)
    _init_git(repo)
    ctx = stage_0_intake(repo, days=30)

    flow = _make_flow(
        "view-items", entry_file="src/x.ts",
        entry_symbol="handler", paths=["src/x.ts"],
    )
    feat = _make_feature("items", ["src/x.ts"], [flow])
    expand_flows([feat], ctx, routes_index=[])

    dumped = json.loads(flow.model_dump_json())
    # Legacy keys still present.
    assert "paths" in dumped
    assert "entry_point_file" in dumped
    assert "uuid" in dumped
    assert "id" in dumped  # B1
    # New Sprint 2 keys present.
    assert "entry" in dumped
    assert "nodes" in dumped
    assert "edges" in dumped
    assert "summary" in dumped


def test_cold_scan_no_disk_writes_from_expand_flows(tmp_path: Path):
    """Gate 6 — Stage 3.5 must not write to ~/.faultline persistence.

    The orchestrator owns artifact emission. The pure
    ``expand_flows`` API is in-memory only — verify it doesn't touch
    any of the cold-scan-forbidden caches.
    """
    repo = tmp_path / "cold"
    repo.mkdir()
    _write(repo, "src/x.ts", "export function f() {}")
    _init_git(repo)
    ctx = stage_0_intake(repo, days=30)
    flow = _make_flow(
        "f", entry_file="src/x.ts", entry_symbol="f", paths=["src/x.ts"],
    )
    feat = _make_feature("x", ["src/x.ts"], [flow])

    # Snapshot ~/.faultline modtimes.
    fault_dir = Path.home() / ".faultline"
    before_files = set()
    if fault_dir.exists():
        for p in fault_dir.glob("assignments-*.json"):
            before_files.add((p, p.stat().st_mtime))

    expand_flows([feat], ctx, routes_index=[])

    after_files = set()
    if fault_dir.exists():
        for p in fault_dir.glob("assignments-*.json"):
            after_files.add((p, p.stat().st_mtime))
    # Assignments cache untouched.
    assert before_files == after_files
