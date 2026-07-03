"""Tests for Phase 5a — the ``scan --auto-partition`` CLI flag.

Uses Typer's :class:`CliRunner` so the full argument parsing + dispatch
is exercised without spawning real scans (the orchestrators are patched).
Focus:

  - OFF (default): behaviour is byte-identical to today — the SAME
    ``run_pipeline_v2`` call with the same args; the partition planner is
    NOT even consulted.
  - ON + monorepo: ``partition_monorepo`` runs once, its ``subpaths()``
    are fed to ``run_pipeline_multi``, and the assembled monorepo output
    is written.
  - ON + non-monorepo: falls back to the ordinary single whole-repo
    ``run_pipeline_v2`` (no multi, no assembly file).
  - ON + monorepo with no independent unit (subpaths()==[]): same
    whole-repo fallback.
  - ``--auto-partition`` + ``--subpath`` is rejected (exit 2).
  - A failed sub-project still writes the assembly + exits non-zero.

All monkeypatches target the IMPORT-SOURCE modules (the CLI imports
``run_pipeline_multi`` / ``run_pipeline_v2`` / ``partition_monorepo`` /
``stage_0_intake`` locally inside the command, so patching the source
module is what takes effect).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from faultline.cli import app
from faultline.pipeline_v2.stage_0_6_project_classifier import (
    ExcludedProject,
    PartitionPlan,
    ProjectClassification,
    ScanUnit,
)

runner = CliRunner()

# The package __init__ re-exports the ``stage_0_intake`` FUNCTION under the
# same name as the submodule, so attribute access
# ``faultline.pipeline_v2.stage_0_intake`` yields the function and a
# string-target monkeypatch on it fails. Patch the real MODULE object
# (loaded via importlib) instead. (Same gotcha documented in
# test_run_pipeline_multi.py.)
_intake_module = importlib.import_module("faultline.pipeline_v2.stage_0_intake")


def _patch_intake(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(_intake_module, "stage_0_intake", fn)


# ── Shared fakes ─────────────────────────────────────────────────────


def _fake_intake_factory():
    """Return a ``stage_0_intake`` stand-in that yields a mutable ctx."""

    def _fake_intake(repo_path, *a, **kw):
        return SimpleNamespace(repo_path=str(repo_path), run_dir="SET", workspaces=[])

    return _fake_intake


def _monorepo_plan(subpaths: list[str]) -> PartitionPlan:
    units = tuple(
        ScanUnit(subpath=sp, project_type="app", name=sp.split("/")[-1])
        for sp in subpaths
    )
    classifications = tuple(
        ProjectClassification(
            name=sp.split("/")[-1], path=sp, project_type="app",
            confidence=0.95, rationale="x",
        )
        for sp in subpaths
    )
    return PartitionPlan(
        is_monorepo=True,
        units=units,
        excluded=(),
        classifications=classifications,
        rationale=f"Monorepo: {len(units)} scan unit(s).",
    )


def _whole_repo_plan() -> PartitionPlan:
    return PartitionPlan(
        is_monorepo=False,
        units=(ScanUnit(subpath=None, project_type="repo", name="solo"),),
        excluded=(),
        classifications=(),
        rationale="Whole-repo scan (no enumerated workspaces); no partition.",
    )


def _library_monorepo_plan() -> PartitionPlan:
    """is_monorepo True but subpaths() == [] (library-monorepo guard)."""
    return PartitionPlan(
        is_monorepo=True,
        units=(ScanUnit(subpath=None, project_type="repo", name="libs"),),
        excluded=(
            ExcludedProject(path="packages/a", type="lib", reason="rides along"),
        ),
        classifications=(
            ProjectClassification(
                name="a", path="packages/a", project_type="lib",
                confidence=0.8, rationale="lib",
            ),
        ),
        rationale="Monorepo with no app/service unit; single whole-repo scan.",
    )


def _fake_multi_factory(tmp_path: Path, captured: dict, *, fail: str | None = None):
    """A ``run_pipeline_multi`` stand-in that writes a tiny featuremap per
    subpath and records the call."""

    def _fake_multi(repo_path, subpaths, *, on_subpath_start=None,
                    on_subpath_end=None, **kw):
        from faultline.pipeline_v2.multi import MultiScanResult

        captured["subpaths"] = list(subpaths)
        captured["kw"] = kw
        out = []
        for sp in subpaths:
            if on_subpath_start:
                on_subpath_start(sp)
            ok = sp != fail
            fm_path = tmp_path / f"fm-{sp.replace('/', '_')}.json"
            if ok:
                fm_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "2.0",
                            "developer_features": [
                                {"name": "feat", "paths": [f"{sp}/x.ts"]}
                            ],
                            "user_flows": [],
                            "flows": [],
                            "scan_meta": {"subpath": sp},
                        }
                    ),
                    encoding="utf-8",
                )
            entry = MultiScanResult(
                subpath=sp,
                out_path=fm_path if ok else None,
                result={
                    "path": str(fm_path),
                    "run_id": "r1",
                    "subpath": sp,
                    "stack": "next-app-router",
                    "cost_usd": 0.0,
                    "calls": 0,
                    "elapsed_sec": 1.0,
                    "warnings": [],
                    "shared_git_pass": True,
                } if ok else None,
                error=None if ok else "SubpathScopeError: no such dir",
            )
            if on_subpath_end:
                on_subpath_end(entry)
            out.append(entry)
        return out

    return _fake_multi


def _fake_assembly(repo_path, plan, results, **kw):
    """Minimal assembly stand-in — records that it ran + returns a shape."""
    return {
        "is_monorepo": True,
        "partition": "isolated-per-project",
        "projects": [{"subpath": r.subpath} for r in results],
        "cross_project_graph": {"nodes": [], "edges": []},
        "partition_plan": {"units": [], "excluded": [], "rationale": plan.rationale},
        "stats": {
            "project_count": len(results),
            "scanned": sum(1 for r in results if r.error is None),
            "failed": sum(1 for r in results if r.error is not None),
            "edge_count": 0,
            "developer_feature_total": 0,
            "max_project_blob_share": 0.0,
        },
    }


# ── OFF (default) — byte-identical to today ──────────────────────────


def test_auto_partition_off_is_byte_identical_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag AND --no-auto-partition both call run_pipeline_v2 with the
    SAME args, and NEVER consult the partition planner."""
    repo = tmp_path / "demo"
    repo.mkdir()

    calls: list[dict] = []

    def _fake_run(repo_path, *, model, days, out_path, llm_reconcile,
                  run_id=None, max_tree_depth=None, subpath=None, **_kw):
        calls.append(
            {"model": model, "days": days, "subpath": subpath, "out_path": out_path}
        )
        return {
            "path": str(tmp_path / "fm.json"),
            "stack": "next-app-router",
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    # The planner must NOT be called when the flag is off — make it explode.
    def _explode_partition(*a, **kw):
        raise AssertionError("partition_monorepo called with --auto-partition OFF")

    monkeypatch.setattr("faultline.pipeline_v2.run.run_pipeline_v2", _fake_run)
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo",
        _explode_partition,
    )

    # Bare (no flag) — default OFF.
    r1 = runner.invoke(app, ["scan", str(repo)])
    assert r1.exit_code == 0, r1.stdout
    # Explicit --no-auto-partition — identical.
    r2 = runner.invoke(app, ["scan", str(repo), "--no-auto-partition"])
    assert r2.exit_code == 0, r2.stdout

    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0]["subpath"] is None  # whole-repo, exactly as today.


# ── ON + monorepo — feeds subpaths to multi + assembles ──────────────


def test_auto_partition_on_monorepo_feeds_subpaths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()

    captured: dict = {}

    _patch_intake(monkeypatch, _fake_intake_factory())
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo",
        lambda ctx, **kw: _monorepo_plan(["apps/web", "apps/api"]),
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.multi.run_pipeline_multi",
        _fake_multi_factory(tmp_path, captured),
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.auto_partition.build_partition_assembly",
        _fake_assembly,
    )

    # run_pipeline_v2 must NOT run — auto-partition handles a monorepo.
    monkeypatch.setattr(
        "faultline.pipeline_v2.run.run_pipeline_v2",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("single-scan run_pipeline_v2 should not run for a monorepo")
        ),
    )

    out_json = tmp_path / "assembly.json"
    result = runner.invoke(
        app,
        ["scan", str(repo), "--auto-partition", "--partition-output", str(out_json)],
    )
    assert result.exit_code == 0, result.stdout
    # The planner's subpaths reached run_pipeline_multi.
    assert captured["subpaths"] == ["apps/web", "apps/api"]
    # The assembled output was written to --partition-output.
    assert out_json.exists()
    doc = json.loads(out_json.read_text())
    assert doc["is_monorepo"] is True
    assert [p["subpath"] for p in doc["projects"]] == ["apps/web", "apps/api"]
    assert "auto-partition" in result.stdout
    assert "monorepo assembly" in result.stdout


def test_auto_partition_on_monorepo_default_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --partition-output the assembly lands at the default
    ~/.faultline/monorepo-<slug>-<ts>.json path."""
    repo = tmp_path / "mono"
    repo.mkdir()
    captured: dict = {}

    written: dict = {}

    def _spy_default_path(repo_path):
        p = tmp_path / "default-monorepo.json"
        written["path"] = p
        return p

    _patch_intake(monkeypatch, _fake_intake_factory())
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo",
        lambda ctx, **kw: _monorepo_plan(["apps/web"]),
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.multi.run_pipeline_multi",
        _fake_multi_factory(tmp_path, captured),
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.auto_partition.build_partition_assembly", _fake_assembly
    )
    monkeypatch.setattr("faultline.cli._default_monorepo_out_path", _spy_default_path)

    result = runner.invoke(app, ["scan", str(repo), "--auto-partition"])
    assert result.exit_code == 0, result.stdout
    assert written["path"].exists()


# ── ON + non-monorepo — whole-repo fallback ──────────────────────────


def test_auto_partition_on_single_repo_falls_back_to_whole_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "solo"
    repo.mkdir()

    captured: dict = {}

    def _fake_run(repo_path, *, model, days, out_path, llm_reconcile,
                  run_id=None, max_tree_depth=None, subpath=None, **_kw):
        captured["ran"] = True
        captured["subpath"] = subpath
        return {
            "path": str(tmp_path / "fm.json"),
            "stack": "next-app-router",
            "cost_usd": 0.0,
            "calls": 0,
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    _patch_intake(monkeypatch, _fake_intake_factory())
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo",
        lambda ctx, **kw: _whole_repo_plan(),
    )
    monkeypatch.setattr("faultline.pipeline_v2.run.run_pipeline_v2", _fake_run)
    # Multi must NOT run for a non-monorepo.
    monkeypatch.setattr(
        "faultline.pipeline_v2.multi.run_pipeline_multi",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_pipeline_multi should not run for a non-monorepo")
        ),
    )

    result = runner.invoke(app, ["scan", str(repo), "--auto-partition"])
    assert result.exit_code == 0, result.stdout
    assert captured.get("ran") is True
    assert captured["subpath"] is None  # whole-repo scan.
    assert "whole-repo scan" in result.stdout


def test_auto_partition_library_monorepo_falls_back_to_whole_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_monorepo True but subpaths()==[] (library-monorepo) → whole-repo."""
    repo = tmp_path / "libs"
    repo.mkdir()
    captured: dict = {}

    def _fake_run(repo_path, *, subpath=None, **_kw):
        captured["ran"] = True
        captured["subpath"] = subpath
        return {
            "path": str(tmp_path / "fm.json"), "stack": None,
            "cost_usd": 0.0, "calls": 0, "elapsed_sec": 0.0, "warnings": [],
        }

    _patch_intake(monkeypatch, _fake_intake_factory())
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo",
        lambda ctx, **kw: _library_monorepo_plan(),
    )
    monkeypatch.setattr("faultline.pipeline_v2.run.run_pipeline_v2", _fake_run)
    monkeypatch.setattr(
        "faultline.pipeline_v2.multi.run_pipeline_multi",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("run_pipeline_multi should not run when subpaths()==[]")
        ),
    )

    result = runner.invoke(app, ["scan", str(repo), "--auto-partition"])
    assert result.exit_code == 0, result.stdout
    assert captured.get("ran") is True
    assert captured["subpath"] is None


# ── Mutual exclusion + failure exit ──────────────────────────────────


def test_auto_partition_with_subpath_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()
    result = runner.invoke(
        app,
        ["scan", str(repo), "--auto-partition", "--subpath", "apps/web"],
    )
    assert result.exit_code == 2
    assert "cannot be combined with" in result.stdout


def test_auto_partition_failed_subproject_writes_assembly_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "mono"
    repo.mkdir()
    captured: dict = {}
    out_json = tmp_path / "assembly.json"

    _patch_intake(monkeypatch, _fake_intake_factory())
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_0_6_project_classifier.partition_monorepo",
        lambda ctx, **kw: _monorepo_plan(["apps/web", "apps/api"]),
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.multi.run_pipeline_multi",
        _fake_multi_factory(tmp_path, captured, fail="apps/api"),
    )
    # Use the REAL assembly here so a failed entry is reflected in stats.
    result = runner.invoke(
        app,
        ["scan", str(repo), "--auto-partition", "--partition-output", str(out_json)],
    )
    assert result.exit_code == 1, result.stdout
    assert "Scan failed subpath=apps/api" in result.stdout
    # The assembly still got written (the failure is recorded, not fatal).
    assert out_json.exists()
    doc = json.loads(out_json.read_text())
    assert doc["stats"]["failed"] == 1
    assert doc["stats"]["scanned"] == 1
