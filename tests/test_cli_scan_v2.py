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
from typer.testing import CliRunner

from faultline.cli import app

runner = CliRunner()


def test_scan_v2_help_lists_flags() -> None:
    result = runner.invoke(app, ["scan-v2", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "--model" in out
    assert "--llm-reconcile" in out
    assert "--days" in out
    assert "--output" in out


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

    def _fake_run(repo_path, *, model, days, out_path, llm_reconcile):
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

    def _fake_run(repo_path, *, model, days, out_path, llm_reconcile):
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

    def _fake_run(repo_path, *, model, days, out_path, llm_reconcile):
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
