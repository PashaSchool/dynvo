"""Tests for :mod:`faultline.pipeline_v2.stage_6_6_branch_slicer` (Sprint D2)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.models.types import Feature, FlowSymbolAttribution
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2 import stage_6_6_branch_slicer as branch_slicer
from faultline.pipeline_v2.stage_6_6_branch_slicer import (
    BranchSlice,
    _BRANCH_NODES,
    _MAX_BRANCHES_PER_SYMBOL,
    _MIN_BRANCH_LINES,
    is_active,
    reset_caches,
    run_stage_6_6,
    slice_branches,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo_path=tmp_path,
        tracked_files=tuple(),
        run_dir=None,
        stack="next-app-router",
        audited_stack="next-app-router",
        secondary_stacks=(),
        monorepo=False,
        workspaces=[],
    )


def _new_feature(
    name: str,
    paths: list[str],
    attrs: list[FlowSymbolAttribution] | None = None,
) -> Feature:
    f = Feature(
        name=name,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
    )
    if attrs:
        f.symbol_attributions = list(attrs)
    return f


def _log(tmp_path: Path) -> StageLogger:
    return StageLogger(tmp_path, 6, "branch_slicer_test")


def _write(tmp_path: Path, rel: str, src: str) -> tuple[str, int]:
    """Write source file under tmp_path/rel; return (rel, line_count)."""
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(src, encoding="utf-8")
    return rel, src.count("\n") + 1


# A constant: tree-sitter must be installed for the slicer to do
# anything beyond report active=False. We skip the language-level
# tests when it isn't available, but always run the negative + schema
# tests.
TS_AVAILABLE = branch_slicer.TREE_SITTER_AVAILABLE and is_active()
requires_ts = pytest.mark.skipif(
    not TS_AVAILABLE,
    reason="tree-sitter language bindings not installed",
)


@pytest.fixture(autouse=True)
def _reset_caches():
    """Per-test cache reset so files written under tmp_path are re-parsed."""
    reset_caches()
    yield
    reset_caches()


# ── 1. is_active returns False when tree-sitter import unavailable ──────


def test_is_active_false_when_tree_sitter_unavailable(monkeypatch):
    """Synthetic test: force TREE_SITTER_AVAILABLE=False; is_active → False.

    Verifies the graceful-degradation contract: a fresh-install without
    the ``[ast]`` extras must NOT crash the pipeline.
    """
    monkeypatch.setattr(branch_slicer, "TREE_SITTER_AVAILABLE", False)
    reset_caches()
    assert is_active() is False


def test_run_stage_6_6_inactive_writes_clean_telemetry(tmp_path, monkeypatch):
    """When tree-sitter is missing, the orchestrator gets an inert result."""
    monkeypatch.setattr(branch_slicer, "TREE_SITTER_AVAILABLE", False)
    reset_caches()
    feature = _new_feature("billing", ["src/billing.ts"])
    with _log(tmp_path) as log:
        result = run_stage_6_6(_ctx(tmp_path), [feature], log)
    assert result.active is False
    assert "pip install" in result.reason.lower()
    tel = result.telemetry()
    assert tel == {"active": False, "reason": result.reason}


# ── 2. Schema sanity (always runs) ──────────────────────────────────────


def test_branch_slice_as_attribution_encodes_kind_and_condition():
    sl = BranchSlice(
        file="x.tsx",
        parent_symbol="Foo",
        branch_kind="if",
        condition_text="role === 'admin'",
        line_start=10,
        line_end=12,
    )
    attr = sl.as_attribution(index=2)
    assert attr.role == "branch"
    assert attr.file == "x.tsx"
    assert attr.symbol.startswith("branch:if:Foo__b2")
    assert "role === 'admin'" in attr.symbol
    assert (attr.line_start, attr.line_end) == (10, 12)


def test_branch_slice_truncates_long_condition_in_symbol():
    long_cond = "x === " + ("a" * 200)
    sl = BranchSlice(
        file="x.ts", parent_symbol="F", branch_kind="if",
        condition_text=long_cond, line_start=1, line_end=3,
    )
    attr = sl.as_attribution(index=0)
    # Symbol must be reasonably bounded
    assert len(attr.symbol) < 200
    assert "..." in attr.symbol


# ── 3. TS branch extraction ─────────────────────────────────────────────


TS_IF_SRC = """\
function CreateUserPage() {
  if (isLoading) {
    return <Spinner />
  }
  return null
}
"""


@requires_ts
def test_ts_single_if_yields_one_slice(tmp_path):
    rel, loc = _write(tmp_path, "page.tsx", TS_IF_SRC)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel,
        parent_symbol="CreateUserPage",
        parent_line_start=1,
        parent_line_end=loc,
    )
    kinds = [s.branch_kind for s in slices]
    assert "if" in kinds
    if_slice = next(s for s in slices if s.branch_kind == "if")
    assert "isLoading" in if_slice.condition_text


TS_IF_ELSE_IF_ELSE = """\
function Route() {
  if (role === 'admin') {
    return <AdminForm />
  } else if (role === 'user') {
    return <UserForm />
  } else {
    return <Guest />
  }
}
"""


@requires_ts
def test_ts_if_else_if_else_yields_multiple_slices(tmp_path):
    rel, loc = _write(tmp_path, "route.tsx", TS_IF_ELSE_IF_ELSE)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel,
        parent_symbol="Route",
        parent_line_start=1,
        parent_line_end=loc,
    )
    # Tree-sitter TS exposes the chain as nested if_statements; we
    # expect at least the outer if + one nested if (the else-if). The
    # else block surfaces only when its body spans >=2 lines, which it
    # does here.
    assert sum(1 for s in slices if s.branch_kind == "if") >= 2
    # The 'admin' condition must appear in at least one slice.
    assert any("admin" in s.condition_text for s in slices)


TS_TERNARY = """\
function View({ cond }) {
  return (
    cond
      ? (
        <Big />
      )
      : (
        <Small />
      )
  )
}
"""


@requires_ts
def test_ts_ternary_yields_ternary_slice(tmp_path):
    rel, loc = _write(tmp_path, "view.tsx", TS_TERNARY)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="View",
        parent_line_start=1, parent_line_end=loc,
    )
    kinds = [s.branch_kind for s in slices]
    assert "ternary" in kinds


TS_SWITCH = """\
function Pick(x) {
  switch (x) {
    case 1: {
      doOne()
      return 'one'
    }
    case 2: {
      doTwo()
      return 'two'
    }
    default: {
      doOther()
      return 'other'
    }
  }
}
"""


@requires_ts
def test_ts_switch_yields_case_and_default_slices(tmp_path):
    rel, loc = _write(tmp_path, "pick.ts", TS_SWITCH)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="Pick",
        parent_line_start=1, parent_line_end=loc,
    )
    kinds = {s.branch_kind for s in slices}
    assert "switch" in kinds or "switch_case" in kinds
    assert "switch_case" in kinds
    assert "switch_default" in kinds


TS_TRY_CATCH = """\
async function load() {
  try {
    const r = await fetch('/x')
    const j = await r.json()
    return j
  } catch (e) {
    log.error(e)
    throw e
  } finally {
    metrics.observe()
  }
}
"""


@requires_ts
def test_ts_try_catch_finally_yields_slices(tmp_path):
    rel, loc = _write(tmp_path, "load.ts", TS_TRY_CATCH)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="load",
        parent_line_start=1, parent_line_end=loc,
    )
    kinds = {s.branch_kind for s in slices}
    assert "try" in kinds
    assert "catch" in kinds


# ── 4. Python branches ──────────────────────────────────────────────────


PY_IF_ELIF_ELSE = """\
def classify(x):
    if x > 0:
        result = 'positive'
        return result
    elif x < 0:
        result = 'negative'
        return result
    else:
        result = 'zero'
        return result
"""


@requires_ts
def test_python_if_elif_else_yields_slices(tmp_path):
    rel, loc = _write(tmp_path, "clf.py", PY_IF_ELIF_ELSE)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="classify",
        parent_line_start=1, parent_line_end=loc,
    )
    kinds = {s.branch_kind for s in slices}
    assert "if" in kinds
    assert "elif" in kinds
    assert "else" in kinds


# ── 5. Go branches ──────────────────────────────────────────────────────


GO_SWITCH = """\
package main
func g(x int) string {
    if x > 0 {
        a := compute(x)
        return a
    } else if x < 0 {
        b := compute(-x)
        return b
    } else {
        return "zero"
    }
}
func h(x int) string {
    switch x {
    case 1:
        doOne()
        return "one"
    case 2:
        doTwo()
        return "two"
    default:
        doOther()
        return "other"
    }
}
"""


@requires_ts
def test_go_if_else_and_switch_yield_slices(tmp_path):
    rel, loc = _write(tmp_path, "x.go", GO_SWITCH)
    # Slice the whole file as if it were one parent — both functions
    # contribute.
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="<file>",
        parent_line_start=1, parent_line_end=loc,
    )
    kinds = {s.branch_kind for s in slices}
    assert "if" in kinds
    assert "switch" in kinds
    assert "switch_case" in kinds
    assert "switch_default" in kinds


# ── 6. Rust match ───────────────────────────────────────────────────────


RUST_MATCH = """\
fn classify(x: i32) -> &'static str {
    match x {
        1 => {
            let a = compute(1);
            return "one";
        },
        2 => {
            let b = compute(2);
            return "two";
        },
        _ => {
            let c = compute(0);
            return "other";
        },
    }
}
"""


@requires_ts
def test_rust_match_yields_match_arm_slices(tmp_path):
    rel, loc = _write(tmp_path, "x.rs", RUST_MATCH)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="classify",
        parent_line_start=1, parent_line_end=loc,
    )
    kinds = {s.branch_kind for s in slices}
    assert "match" in kinds
    assert "match_arm" in kinds


# ── 7. Tiny-body filter ─────────────────────────────────────────────────


TS_TINY_IF = """\
function tiny(x) { if (x) return 1; return 0; }
"""


@requires_ts
def test_branches_with_body_under_min_lines_are_dropped(tmp_path):
    rel, loc = _write(tmp_path, "tiny.ts", TS_TINY_IF)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="tiny",
        parent_line_start=1, parent_line_end=loc,
    )
    # Every if body fits on a single line → filtered out by _MIN_BRANCH_LINES.
    assert all(
        (s.line_end - s.line_start + 1) >= _MIN_BRANCH_LINES for s in slices
    )


# ── 8. Per-symbol cap ───────────────────────────────────────────────────


@requires_ts
def test_per_symbol_cap_enforced(tmp_path):
    # Generate a function with way more than _MAX_BRANCHES_PER_SYMBOL if-blocks.
    blocks = "\n".join(
        f"  if (x === {i}) {{\n    doStuff_{i}()\n    return {i}\n  }}"
        for i in range(_MAX_BRANCHES_PER_SYMBOL + 10)
    )
    src = f"function many(x) {{\n{blocks}\n  return -1\n}}\n"
    rel, loc = _write(tmp_path, "many.ts", src)
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="many",
        parent_line_start=1, parent_line_end=loc,
    )
    assert len(slices) <= _MAX_BRANCHES_PER_SYMBOL


# ── 9. Unknown extension: silent skip ───────────────────────────────────


def test_unknown_extension_returns_empty(tmp_path):
    rel, loc = _write(tmp_path, "x.cobol", "PROCEDURE DIVISION.")
    slices = slice_branches(
        _ctx(tmp_path),
        file_path=rel, parent_symbol="<file>",
        parent_line_start=1, parent_line_end=loc,
    )
    assert slices == []


def test_missing_file_returns_empty(tmp_path):
    slices = slice_branches(
        _ctx(tmp_path),
        file_path="does/not/exist.ts",
        parent_symbol="ghost",
        parent_line_start=1, parent_line_end=5,
    )
    assert slices == []


# ── 10. Integration: run_stage_6_6 extends feature.symbol_attributions ──


@requires_ts
def test_run_stage_6_6_extends_feature_symbol_attributions(tmp_path):
    rel, loc = _write(tmp_path, "page.tsx", TS_IF_ELSE_IF_ELSE)
    feature = _new_feature(
        "users",
        paths=[rel],
        attrs=[FlowSymbolAttribution(
            file=rel,
            symbol="Route",
            line_start=1,
            line_end=loc,
            role="entry",
        )],
    )
    pre_count = len(feature.symbol_attributions)

    with _log(tmp_path) as log:
        result = run_stage_6_6(_ctx(tmp_path), [feature], log)

    assert result.active is True
    assert result.symbols_analyzed >= 1
    assert result.branches_emitted >= 1
    # Original attributions preserved + branch attributions appended.
    assert len(feature.symbol_attributions) > pre_count
    branch_attrs = [
        a for a in feature.symbol_attributions if a.role == "branch"
    ]
    assert len(branch_attrs) == result.branches_emitted
    for a in branch_attrs:
        assert a.symbol.startswith("branch:")
        assert a.line_start >= 1
        assert a.line_end >= a.line_start


@requires_ts
def test_run_stage_6_6_skips_support_role_attributions(tmp_path):
    """Only entry+called attributions get sliced; support spans whole
    files and would yield meaningless slices."""
    rel, loc = _write(tmp_path, "page.tsx", TS_IF_ELSE_IF_ELSE)
    feature = _new_feature(
        "users",
        paths=[rel],
        attrs=[FlowSymbolAttribution(
            file=rel,
            symbol="<file>",
            line_start=1, line_end=loc,
            role="support",
        )],
    )
    with _log(tmp_path) as log:
        result = run_stage_6_6(_ctx(tmp_path), [feature], log)
    # No branch attributions emitted because the only attribution had
    # an ineligible role.
    assert result.symbols_analyzed == 0
    branch_attrs = [
        a for a in feature.symbol_attributions if a.role == "branch"
    ]
    assert branch_attrs == []


@requires_ts
def test_run_stage_6_6_slices_flow_symbol_attributions(tmp_path):
    """Stages 3/C1 attach attributions to ``flow.flow_symbol_attributions``
    for non-JS stacks (Go/Python-lib/Rust). The slicer MUST also walk
    those — otherwise non-JS scans get no branch enrichment."""
    from faultline.models.types import Flow

    rel, loc = _write(tmp_path, "x.go", GO_SWITCH)
    feature = _new_feature("svc", paths=[rel])
    flow = Flow(
        name="handle-x",
        display_name="Handle X",
        description="",
        paths=[rel],
        authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        entry_point_file=rel,
        entry_point_line=2,
        flow_symbol_attributions=[FlowSymbolAttribution(
            file=rel, symbol="g", line_start=1, line_end=loc, role="entry",
        )],
    )
    feature.flows = [flow]

    with _log(tmp_path) as log:
        result = run_stage_6_6(_ctx(tmp_path), [feature], log)

    assert result.active is True
    assert result.symbols_analyzed >= 1
    branch_attrs = [
        a for a in flow.flow_symbol_attributions if a.role == "branch"
    ]
    assert len(branch_attrs) >= 2  # at least if + switch_case
    # feature-level attributions list is untouched (no eligible entries).
    feat_branch_attrs = [
        a for a in feature.symbol_attributions if a.role == "branch"
    ]
    assert feat_branch_attrs == []


@requires_ts
def test_run_stage_6_6_telemetry_shape(tmp_path):
    rel, loc = _write(tmp_path, "page.tsx", TS_IF_ELSE_IF_ELSE)
    feature = _new_feature(
        "users", paths=[rel],
        attrs=[FlowSymbolAttribution(
            file=rel, symbol="Route", line_start=1, line_end=loc, role="entry",
        )],
    )
    with _log(tmp_path) as log:
        result = run_stage_6_6(_ctx(tmp_path), [feature], log)
    tel = result.telemetry()
    assert tel["active"] is True
    assert "tree_sitter_version" in tel
    assert "symbols_analyzed" in tel
    assert "branches_emitted" in tel
    assert isinstance(tel["branch_kinds"], dict)
    assert isinstance(tel["sample_slices"], list)
    assert "elapsed_sec" in tel
