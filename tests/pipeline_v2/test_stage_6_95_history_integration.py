"""Integration tests for Stage 6.95 wiring in the orchestrator.

Mirrors the no-LLM fixture pattern of ``tests/test_run_pipeline_v2.py``
(git fixture + monkey-patched Stage 3/4) and asserts:

  - the history stage runs by default and its telemetry lands in
    ``scan_meta.stage_6_95`` + the on-disk JSON;
  - ``feature_history=False`` (CLI ``--no-feature-history``) skips it;
  - the stage works under a ``--subpath`` (multi-subpath) run, where
    history is computed from the partitioned commit list.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit_files(repo: Path, files: dict[str, str], msg: str, date: str) -> None:
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "commit", "-q", "-m", msg],
        cwd=repo,
        check=True,
        env={
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo.parent),
        },
    )


@pytest.fixture()
def fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo-app"
    _init_repo(repo)
    _commit_files(
        repo,
        {
            "package.json": json.dumps(
                {"name": "demo", "dependencies": {"next": "14.0.0"}},
            ),
            "next.config.js": "module.exports = {};\n",
            "app/billing/page.tsx": "export default function Page() { return null; }\n",
            "app/auth/page.tsx": "export default function Page() { return null; }\n",
        },
        "feat: initial",
        "2026-01-05T12:00:00 +0000",
    )
    _commit_files(
        repo,
        {"app/billing/page.tsx": "export default function Page() { return 1; }\n"},
        "fix: billing rounding bug",
        "2026-02-02T12:00:00 +0000",
    )
    _commit_files(
        repo,
        {"app/billing/page.test.tsx": "test('billing', () => {});\n"},
        "test: billing page",
        "2026-03-02T12:00:00 +0000",
    )

    # Patch Stage 3 + Stage 4 — no network.
    def _fake_stage_3(features, ctx, *, model, cost_tracker, **_kw):
        from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="patched")
                for f in features
            ],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
        )

    def _fake_stage_4(unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
            clusters_total=0,
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
        )

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)
    return repo


def test_history_stage_runs_by_default(fixture_repo: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(fixture_repo, model="haiku", out_path=out_path)

    assert "stage_6_95" in result
    telemetry = result["stage_6_95"]
    assert telemetry.get("skipped") is not True
    assert "product_features_scored" in telemetry
    assert "verdicts" in telemetry

    data = json.loads(out_path.read_text())
    assert data["scan_meta"]["stage_6_95"] == telemetry
    # Every scored product feature carries the additive history shape.
    scored = [
        pf for pf in data.get("product_features", []) if pf.get("history")
    ]
    assert len(scored) == telemetry["product_features_scored"]
    for pf in scored:
        h = pf["history"]
        assert h["birth_week"].count("-W") == 1
        assert isinstance(h["weekly"], list) and h["weekly"]
        assert any(e["kind"] == "birth" for e in h["events"])
        assert h["test_efficacy"]["verdict"] in {
            "improved", "worsened", "no_change", "insufficient_data",
        }


def test_history_stage_skippable(fixture_repo: Path, tmp_path: Path) -> None:
    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(
        fixture_repo, model="haiku", out_path=out_path, feature_history=False,
    )
    assert result["stage_6_95"] == {"skipped": True}
    data = json.loads(out_path.read_text())
    for pf in data.get("product_features", []):
        assert pf.get("history") is None
    for uf in data.get("user_flows", []):
        assert uf.get("history") is None


def test_history_stage_under_subpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-subpath runs compute history from the PARTITIONED commit
    list — commits outside the subpath never reach the stage."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "mono"
    _init_repo(repo)
    _commit_files(
        repo,
        {
            "apps/web/package.json": json.dumps(
                {"name": "web", "dependencies": {"next": "14.0.0"}},
            ),
            "apps/web/next.config.js": "module.exports = {};\n",
            "apps/web/app/billing/page.tsx": "export default function P() { return null; }\n",
            "apps/api/package.json": json.dumps({"name": "api"}),
            "apps/api/src/index.ts": "export {};\n",
        },
        "feat: initial",
        "2026-01-05T12:00:00 +0000",
    )
    _commit_files(
        repo,
        {"apps/api/src/index.ts": "export const x = 1;\n"},
        "fix: api-only bug",
        "2026-02-02T12:00:00 +0000",
    )

    def _fake_stage_3(features, ctx, *, model, cost_tracker, **_kw):
        from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="patched")
                for f in features
            ],
            cost_usd=0.0, llm_calls=0, warnings=[],
        )

    def _fake_stage_4(unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        return Stage4Result(
            residual_features=[], cost_usd=0.0, llm_calls=0, warnings=[],
            clusters_total=0, clusters_processed=0,
            saturation_stopped=False, rejected_names=[],
        )

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)

    out_path = tmp_path / "web-map.json"
    result = run_pipeline_v2(
        repo, model="haiku", out_path=out_path, subpath="apps/web",
    )
    assert "stage_6_95" in result
    assert result["stage_6_95"].get("skipped") is not True

    data = json.loads(out_path.read_text())
    # The api-only fix commit is outside apps/web — no history bucket
    # anywhere may reference its week with a bug fix.
    for pf in data.get("product_features", []):
        h = pf.get("history")
        if not h:
            continue
        for pt in h["weekly"]:
            if pt["week"] == "2026-W06":
                assert pt["bug_fixes"] == 0


# ── Stage 6.96 wiring (impact-over-time, same feature_history gate) ─────


def test_impact_stage_runs_by_default(
    fixture_repo: Path, tmp_path: Path,
) -> None:
    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(fixture_repo, model="haiku", out_path=out_path)

    assert "stage_6_96_impact" in result
    telemetry = result["stage_6_96_impact"]
    assert telemetry.get("skipped") is not True
    assert telemetry["impact_snapshots"] >= 1
    assert telemetry["impact_budget_exceeded"] is False

    data = json.loads(out_path.read_text())
    assert data["scan_meta"]["stage_6_96_impact"] == telemetry
    scored = [
        pf for pf in data.get("product_features", []) if pf.get("history")
    ]
    assert scored
    pf_with_impact = 0
    for pf in scored:
        impact = pf["history"].get("impact", [])
        if impact:
            pf_with_impact += 1
        for point in impact:
            assert point["week"].count("-W") == 1
            assert point["reach"] >= 0
            assert point["members_present"] >= 0
    # entities_with_impact also counts user flows — PFs are a subset.
    assert 1 <= pf_with_impact <= telemetry["entities_with_impact"]
    # The fixture repo must end the run with no leftover worktrees.
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=fixture_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert listing.count("worktree ") == 1


def test_impact_stage_skipped_with_history_flag(
    fixture_repo: Path, tmp_path: Path,
) -> None:
    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(
        fixture_repo, model="haiku", out_path=out_path, feature_history=False,
    )
    assert result["stage_6_96_impact"] == {"skipped": True}
