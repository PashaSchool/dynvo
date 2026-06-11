"""Tests for the ``faultline scan-v2`` CLI subcommand.

Uses Typer's :class:`CliRunner` so we exercise the full argument
parsing without spawning a subprocess. The orchestrator is patched
out (we tested it directly in ``test_run_pipeline_v2.py``) so these
tests focus on the CLI surface: flag parsing, model alias resolution
at the CLI boundary, error path, output formatting.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from faultline.cli import app

runner = CliRunner()


def test_scan_v2_exposes_expected_flags() -> None:
    # Introspect the registered Click options directly instead of asserting
    # on `--help` output. Rich-rendered help truncates long option names at
    # narrow widths and wraps differently across platforms/terminals, which
    # made the prior text-scraping assertion flaky in headless CI. The flags
    # a command exposes is what we actually care about — read it from source.
    scan_v2 = get_command(app).commands["scan-v2"]
    flags = {opt for param in scan_v2.params for opt in param.opts}
    assert {"--model", "--llm-reconcile", "--days", "--output", "--run-id"} <= flags


def test_scan_v2_nonexistent_dir_exits_with_2(tmp_path: Path) -> None:
    bad = tmp_path / "does-not-exist"
    result = runner.invoke(app, ["scan-v2", str(bad)])
    assert result.exit_code == 2
    assert "not a directory" in result.stdout


def test_scan_v2_invokes_orchestrator_with_resolved_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: --model haiku resolves to the fully-qualified id
    before reaching the orchestrator.
    """
    repo = tmp_path / "demo"
    repo.mkdir()

    captured: dict[str, object] = {}

    def _fake_run(
        repo_path, *, model, days, out_path, llm_reconcile,
        run_id=None, max_tree_depth=None, **_kw,
    ):
        captured["model"] = model
        captured["days"] = days
        captured["llm_reconcile"] = llm_reconcile
        captured["out_path"] = out_path
        return {
            "path": str(tmp_path / "fm.json"),
            "stack": "next-app-router",
            "cost_usd": 0.0123,
            "calls": 5,
            "elapsed_sec": 1.2,
            "warnings": [],
        }

    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2", _fake_run,
    )
    result = runner.invoke(
        app,
        ["scan-v2", str(repo), "--model", "haiku", "--llm-reconcile", "--days", "90"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["model"] == "claude-haiku-4-5-20251001"
    assert captured["days"] == 90
    assert captured["llm_reconcile"] is True
    assert captured["out_path"] is None
    assert "scan-v2" in result.stdout
    assert "next-app-router" in result.stdout
    assert "$0.0123" in result.stdout


def test_scan_v2_propagates_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()

    def _fake_run(
        repo_path, *, model, days, out_path, llm_reconcile,
        run_id=None, max_tree_depth=None, **_kw,
    ):
        return {
            "path": str(tmp_path / "fm.json"),
            "stack": "fastapi",
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.5,
            "warnings": ["LLM-fallback handled 80% of features"],
        }

    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2", _fake_run,
    )
    result = runner.invoke(app, ["scan-v2", str(repo)])
    assert result.exit_code == 0
    assert "LLM-fallback handled 80%" in result.stdout


def test_scan_v2_explicit_output_path_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    target = tmp_path / "explicit.json"

    captured: dict[str, object] = {}

    def _fake_run(
        repo_path, *, model, days, out_path, llm_reconcile,
        run_id=None, max_tree_depth=None, **_kw,
    ):
        captured["out_path"] = out_path
        return {
            "path": str(target),
            "stack": None,
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2", _fake_run,
    )
    result = runner.invoke(app, ["scan-v2", str(repo), "--output", str(target)])
    assert result.exit_code == 0
    assert captured["out_path"] == target.resolve()


def test_scan_v2_run_id_flag_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--run-id baseline`` is forwarded to the orchestrator verbatim."""
    repo = tmp_path / "demo"
    repo.mkdir()

    captured: dict[str, object] = {}

    def _fake_run(
        repo_path, *, model, days, out_path, llm_reconcile,
        run_id=None, max_tree_depth=None, **_kw,
    ):
        captured["run_id"] = run_id
        return {
            "path": str(tmp_path / "fm.json"),
            "run_id": run_id,
            "stack": None,
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2", _fake_run,
    )
    result = runner.invoke(
        app, ["scan-v2", str(repo), "--run-id", "baseline"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["run_id"] == "baseline"
    assert "baseline" in result.stdout


def test_scan_v2_max_tree_depth_flag_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sprint C3b — ``--max-tree-depth N`` reaches run_pipeline_v2."""
    repo = tmp_path / "demo"
    repo.mkdir()

    captured: dict[str, object] = {}

    def _fake_run(
        repo_path, *, model, days, out_path, llm_reconcile,
        run_id=None, max_tree_depth=None, **_kw,
    ):
        captured["max_tree_depth"] = max_tree_depth
        return {
            "path": str(tmp_path / "fm.json"),
            "stack": None,
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2", _fake_run,
    )
    # Explicit override → captured value matches.
    result = runner.invoke(
        app, ["scan-v2", str(repo), "--max-tree-depth", "4"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["max_tree_depth"] == 4
    assert "max_tree_depth=4" in result.stdout

    # Default (no flag) → 8.
    captured.clear()
    result = runner.invoke(app, ["scan-v2", str(repo)])
    assert result.exit_code == 0, result.stdout
    assert captured["max_tree_depth"] == 8


# ── Multi-subpath routing (shared single git pass) ──────────────────


def test_scan_v2_multi_subpath_routes_through_run_pipeline_multi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """>1 --subpath calls run_pipeline_multi once (not the per-scope loop)."""
    repo = tmp_path / "mono"
    repo.mkdir()

    captured: dict[str, object] = {}

    def _fake_multi(repo_path, subpaths, *, on_subpath_start=None,
                    on_subpath_end=None, **kw):
        from faultline.pipeline_v2.multi import MultiScanResult
        captured["subpaths"] = list(subpaths)
        captured["kw"] = kw
        out = []
        for sp in subpaths:
            if on_subpath_start:
                on_subpath_start(sp)
            entry = MultiScanResult(
                subpath=sp,
                out_path=tmp_path / f"fm-{sp.replace('/', '_')}.json",
                result={
                    "path": str(tmp_path / f"fm-{sp.replace('/', '_')}.json"),
                    "run_id": "r1",
                    "subpath": sp,
                    "stack": "next-app-router",
                    "cost_usd": 0.0,
                    "calls": 0,
                    "elapsed_sec": 1.0,
                    "warnings": [],
                    "shared_git_pass": True,
                },
                error=None,
            )
            if on_subpath_end:
                on_subpath_end(entry)
            out.append(entry)
        return out

    def _fail_single(*a, **kw):  # the single-scope path must NOT run
        raise AssertionError("run_pipeline_v2 called for a multi-subpath scan")

    monkeypatch.setattr("faultline.pipeline_v2.multi.run_pipeline_multi", _fake_multi)
    monkeypatch.setattr("faultline.pipeline_v2.run.run_pipeline_v2", _fail_single)

    result = runner.invoke(
        app,
        ["scan-v2", str(repo), "--subpath", "apps/web", "--subpath", "apps/worker"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["subpaths"] == ["apps/web", "apps/worker"]
    # Per-scope progress + success lines preserved.
    assert result.stdout.count("scan-v2") >= 2
    assert "subpath=apps/web" in result.stdout
    assert "subpath=apps/worker" in result.stdout


def test_scan_v2_multi_subpath_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()

    def _fake_multi(repo_path, subpaths, *, on_subpath_start=None,
                    on_subpath_end=None, **kw):
        from faultline.pipeline_v2.multi import MultiScanResult
        out = []
        for sp in subpaths:
            if on_subpath_start:
                on_subpath_start(sp)
            ok = sp != "apps/nope"
            entry = MultiScanResult(
                subpath=sp,
                out_path=(tmp_path / "fm.json") if ok else None,
                result={
                    "path": str(tmp_path / "fm.json"),
                    "run_id": "r1",
                    "subpath": sp,
                    "stack": "go",
                    "cost_usd": 0.0,
                    "calls": 0,
                    "elapsed_sec": 1.0,
                    "warnings": [],
                } if ok else None,
                error=None if ok else "SubpathScopeError: no such dir",
            )
            if on_subpath_end:
                on_subpath_end(entry)
            out.append(entry)
        return out

    monkeypatch.setattr("faultline.pipeline_v2.multi.run_pipeline_multi", _fake_multi)

    result = runner.invoke(
        app,
        ["scan-v2", str(repo), "--subpath", "apps/web", "--subpath", "apps/nope"],
    )
    assert result.exit_code == 1
    assert "Scan failed subpath=apps/nope" in result.stdout
    assert "SubpathScopeError" in result.stdout


def test_scan_v2_multi_subpath_rejects_output_flag(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()
    result = runner.invoke(
        app,
        [
            "scan-v2", str(repo),
            "--subpath", "apps/web", "--subpath", "apps/worker",
            "--output", str(tmp_path / "out.json"),
        ],
    )
    assert result.exit_code == 2
    assert "--output cannot be combined" in result.stdout


# ── --max-cost flag ─────────────────────────────────────────────────


def test_scan_v2_max_cost_flag_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--max-cost 2.50`` reaches run_pipeline_v2; default is None."""
    repo = tmp_path / "demo"
    repo.mkdir()

    captured: dict[str, object] = {}

    def _fake_run(
        repo_path, *, model, days, out_path, llm_reconcile,
        run_id=None, max_tree_depth=None, max_cost=None, **_kw,
    ):
        captured["max_cost"] = max_cost
        return {
            "path": str(tmp_path / "fm.json"),
            "stack": None,
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2", _fake_run,
    )
    result = runner.invoke(
        app, ["scan-v2", str(repo), "--max-cost", "2.50"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["max_cost"] == 2.50

    # Default (no flag) → None (enforcement disabled).
    captured.clear()
    result = runner.invoke(app, ["scan-v2", str(repo)])
    assert result.exit_code == 0, result.stdout
    assert captured["max_cost"] is None
