"""Fan-in gating tests — separate flow CORE from SHARED infrastructure.

Two layers:

  * Unit tests over :mod:`faultline.pipeline_v2.flow_expansion.fan_in`
    (pure, deterministic, no repo) — threshold derivation, the
    structural floor, scale-invariance.
  * Integration tests over :func:`expand_flows` on synthetic micro-repos
    — a symbol called by many flows is demoted to ``role="shared"`` and
    excluded from CORE, while a single-caller symbol never is.

Per [[rule-no-magic-tuning]] the threshold must come from the
distribution, not a constant: the scale-invariance test multiplies every
caller count by a constant factor and asserts the SAME demotion set.
Per [[rule-no-repo-specific-paths]] all fixtures use neutral synthetic
names.
"""

from __future__ import annotations

import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Feature, Flow, FlowSymbolAttribution
from faultline.pipeline_v2.flow_expansion import expand_flows
from faultline.pipeline_v2.flow_expansion.fan_in import (
    FanInAccumulator,
    compute_fan_in,
    symbol_key,
)
from faultline.pipeline_v2.stage_0_intake import stage_0_intake


# ── Unit: threshold derivation ───────────────────────────────────────────


def test_floor_blocks_single_caller_demotion():
    """A symbol called by exactly one flow is never shared, regardless of
    the distribution — the structural floor (>=2) guards it.
    """
    callers = {
        "a.py#only_once": {"f1"},  # fan_in 1 — below floor
    }
    res = compute_fan_in(callers)
    assert res.shared_keys == frozenset()
    assert res.fan_in["a.py#only_once"] == 1


def test_ratio_vs_median_isolates_right_tail():
    """An outlier well above the typical-callee median is demoted; the
    body of the distribution is not.
    """
    # 30 symbols reached by ONE flow (the typical callee) → median 1,
    # threshold = 5*1 = 5. One infra symbol at fan_in 8 clears it; a
    # domain-shared symbol at fan_in 3 does not.
    callers = {
        f"mod.py#leaf{i}": {f"f{i}"} for i in range(30)
    }
    callers["mod.py#domain_shared"] = {f"f{j}" for j in range(3)}
    callers["mod.py#shared_infra"] = {f"f{j}" for j in range(8)}
    res = compute_fan_in(callers)
    assert res.median == 1
    assert res.threshold == 5
    assert res.shared_keys == frozenset({"mod.py#shared_infra"})


def test_flat_distribution_demotes_nothing():
    """When every callee has the same fan-in there is no outlier tail —
    nothing is demoted.
    """
    callers = {
        f"mod.py#util{i}": {f"f{j}" for j in range(3)} for i in range(6)
    }
    res = compute_fan_in(callers)
    # median 3, threshold 15, no symbol exceeds it.
    assert res.shared_keys == frozenset()


def test_scale_invariance_same_demotion_set():
    """Scaling the WHOLE distribution by a constant factor yields the SAME
    demotion set — the threshold is relative (ratio vs median), not an
    absolute count. This is the [[rule-no-magic-tuning]] guarantee.
    """
    base = {f"mod.py#leaf{i}": {f"f{i}"} for i in range(20)}
    base["mod.py#infra"] = {f"f{j}" for j in range(10)}
    res_small = compute_fan_in(base)

    # Scale x3: every symbol called by 3x as many distinct flows.
    scaled = {
        k: {f"{c}_{r}" for c in v for r in range(3)}
        for k, v in base.items()
    }
    res_big = compute_fan_in(scaled)

    # Same keys demoted despite 3x larger absolute fan-in.
    assert res_small.shared_keys == res_big.shared_keys
    assert res_small.shared_keys == frozenset({"mod.py#infra"})
    # Absolute thresholds differ (proof the rule is relative).
    assert res_big.threshold == 3 * res_small.threshold


def test_accumulator_counts_distinct_flows():
    """The accumulator counts DISTINCT flows, not call occurrences — the
    same flow recording a symbol twice contributes fan-in 1.
    """
    acc = FanInAccumulator()
    acc.record("a.py", "helper", "flow-1")
    acc.record("a.py", "helper", "flow-1")  # duplicate — same flow
    acc.record("a.py", "helper", "flow-2")
    res = acc.finalize()
    assert res.fan_in[symbol_key("a.py", "helper")] == 2


# ── Integration fixtures ─────────────────────────────────────────────────


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


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def _make_flow(name: str, entry_file: str, entry_symbol: str,
               entry_line: int) -> Flow:
    now = datetime.now(timezone.utc)
    fl = Flow(
        name=name, entry_point_file=entry_file, entry_point_line=entry_line,
        paths=[entry_file], authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=now, health_score=100.0,
        uuid=f"fl-{name}",
    )
    fl.flow_symbol_attributions = [FlowSymbolAttribution(
        file=entry_file, symbol=entry_symbol,
        line_start=entry_line, line_end=entry_line + 3, role="entry",
    )]
    return fl


def _make_feature(name: str, flows: list[Flow]) -> Feature:
    now = datetime.now(timezone.utc)
    return Feature(
        name=name, participants=[], paths=[], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=now,
        health_score=100.0, flows=flows,
    )


def _shared_infra_repo(tmp_path: Path) -> Path:
    """N handler flows, each calling its own private helper PLUS one shared
    ``open_session`` utility imported from a common module. ``open_session``
    has high fan-in; the per-handler helpers have fan-in 1.
    """
    repo = tmp_path / "fanin_repo"
    repo.mkdir()
    _write(repo, "common/db.py", """
        def open_session():
            return {"db": True}
    """)
    # 6 handlers, each with a unique helper + the shared session opener.
    for i in range(6):
        _write(repo, f"handlers/h{i}.py", f"""
            from common.db import open_session
            def handle_{i}(req):
                session = open_session()
                return helper_{i}(session)
            def helper_{i}(session):
                return {{"i": {i}}}
        """)
    _init_git_repo(repo)
    return repo


# ── Integration tests ────────────────────────────────────────────────────


def test_high_fan_in_symbol_demoted_to_shared(tmp_path: Path):
    """``open_session`` (called by every flow) is demoted to role=shared
    and EXCLUDED from each flow's core; the per-handler ``helper_i``
    (single caller) stays core.
    """
    repo = _shared_infra_repo(tmp_path)
    ctx = stage_0_intake(repo, days=30)
    flows = [
        _make_flow(f"handle-{i}", f"handlers/h{i}.py", f"handle_{i}", 2)
        for i in range(6)
    ]
    feat = _make_feature("handlers", flows)
    fx = expand_flows([feat], ctx, routes_index=[], max_depth=1)

    # open_session classified shared globally.
    assert fx.telemetry["fanin_shared_symbols"] >= 1
    assert fx.telemetry["shared_attributions_total"] >= 6

    for i, fl in enumerate(flows):
        roles = {
            (a.symbol): a.role
            for a in (fl.flow_symbol_attributions or [])
        }
        # Shared infra demoted.
        assert roles.get("open_session") == "shared", roles
        # The flow's own helper stays core.
        assert roles.get(f"helper_{i}") == "called", roles

        core = [
            a for a in (fl.flow_symbol_attributions or [])
            if a.role in ("entry", "called")
        ]
        core_symbols = {a.symbol for a in core}
        # open_session excluded from CORE.
        assert "open_session" not in core_symbols
        # Entry + own helper are in CORE.
        assert f"handle_{i}" in core_symbols
        assert f"helper_{i}" in core_symbols


def test_shared_node_carries_fan_in_badge(tmp_path: Path):
    """The demoted graph node carries its ``fan_in`` count for the
    dashboard shared-dependency badge.
    """
    repo = _shared_infra_repo(tmp_path)
    ctx = stage_0_intake(repo, days=30)
    flows = [
        _make_flow(f"handle-{i}", f"handlers/h{i}.py", f"handle_{i}", 2)
        for i in range(6)
    ]
    feat = _make_feature("handlers", flows)
    expand_flows([feat], ctx, routes_index=[], max_depth=1)

    shared_nodes = [
        n for fl in flows for n in fl.nodes if n.role == "shared"
    ]
    assert shared_nodes, "expected at least one shared node"
    for n in shared_nodes:
        assert n.symbol == "open_session"
        assert n.fan_in is not None and n.fan_in >= 2


def test_no_shared_when_no_overlap(tmp_path: Path):
    """When no symbol is reached by 2+ flows, nothing is demoted — every
    callee stays core.
    """
    repo = tmp_path / "disjoint_repo"
    repo.mkdir()
    for i in range(4):
        _write(repo, f"handlers/h{i}.py", f"""
            def handle_{i}(req):
                return helper_{i}(req)
            def helper_{i}(req):
                return {{"i": {i}}}
        """)
    _init_git_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    flows = [
        _make_flow(f"handle-{i}", f"handlers/h{i}.py", f"handle_{i}", 1)
        for i in range(4)
    ]
    feat = _make_feature("handlers", flows)
    fx = expand_flows([feat], ctx, routes_index=[], max_depth=1)

    assert fx.telemetry["fanin_shared_symbols"] == 0
    assert fx.telemetry["shared_attributions_total"] == 0
    for fl in flows:
        assert all(
            a.role != "shared"
            for a in (fl.flow_symbol_attributions or [])
        )
