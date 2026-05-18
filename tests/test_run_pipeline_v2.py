"""Integration test for the pipeline-v2 orchestrator.

Uses a lightweight git fixture (``git init`` + one commit) so Stage 0
can load real git history, then monkey-patches Stage 3 + Stage 4 to
skip the network calls. Verifies that the assembled FeatureMap on disk
carries layered features, scan_meta, and that stage artifacts were
written under ``~/.faultline/logs/<slug>/``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.run import (
    DEFAULT_MODEL,
    MODEL_ALIASES,
    resolve_model,
    run_pipeline_v2,
)
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result


def test_resolve_model_aliases() -> None:
    assert resolve_model("haiku") == MODEL_ALIASES["haiku"]
    assert resolve_model("sonnet") == MODEL_ALIASES["sonnet"]
    # Fully qualified ids pass through unchanged.
    assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
    assert resolve_model("") == DEFAULT_MODEL


def _git_init_with_one_commit(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: initial"], cwd=repo, check=True,
    )


def test_run_pipeline_v2_end_to_end_no_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end without hitting Anthropic.

    Builds a minimal Next-App-Router repo, monkey-patches Stage 3 +
    Stage 4 to return canned empty results (no LLM calls), and asserts
    the orchestrator wires everything together correctly.
    """
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo-app"
    _git_init_with_one_commit(
        repo,
        {
            "package.json": json.dumps(
                {"name": "demo", "dependencies": {"next": "14.0.0"}},
            ),
            "app/billing/page.tsx": "export default function Page() { return null; }\n",
            "app/auth/page.tsx": "export default function Page() { return null; }\n",
            "next.config.js": "module.exports = {};\n",
        },
    )

    # Patch Stage 3 + Stage 4 so we don't hit the network.
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
            chunks_processed=0,
            rejected_names=[],
        )

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)

    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)

    # Returned dict carries the path + scan_meta.
    assert result["path"] == str(out_path)
    assert result["pipeline_version"] == "v2"
    assert result["model"] == MODEL_ALIASES["haiku"]
    assert result["stack"] == "next-app-router"
    assert "extractor_hits" in result
    assert isinstance(result["warnings"], list)
    assert result["cost_usd"] == 0.0
    assert result["calls"] == 0
    assert result["llm_reconcile"] is False
    # No fallback features → llm_fallback_pct == 0.0 and no warn.
    assert result["llm_fallback_pct"] == 0.0
    assert not any("LLM-fallback handled" in w for w in result["warnings"])

    # File on disk is a valid FeatureMap.
    data = json.loads(out_path.read_text())
    assert "features" in data
    assert "developer_features" in data
    assert "product_features" in data
    assert data["product_features"] == []
    assert data["scan_meta"]["pipeline_version"] == "v2"
    assert data["scan_meta"]["stack"] == "next-app-router"

    # Stage artifacts were written for stages 0..7.
    artifact_dir = fake_home / ".faultline" / "logs" / "demo-app"
    assert artifact_dir.is_dir()
    expected = {
        "00-stage-intake.json",
        "01-stage-extractors.json",
        "02-stage-reconcile.json",
        "03-stage-flows.json",
        "04-stage-residual.json",
        "05-stage-postprocess.json",
        "06-stage-metrics.json",
        "07-stage-output.json",
    }
    found = {p.name for p in artifact_dir.iterdir()}
    assert expected.issubset(found), f"missing: {expected - found}"


def test_run_pipeline_v2_emits_high_fallback_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM-fallback > 30% of features, warnings carries the nudge."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "tiny-repo"
    _git_init_with_one_commit(
        repo,
        {
            "package.json": json.dumps({"name": "tiny"}),
            "src/random.ts": "export const x = 1;\n",
        },
    )

    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

    def _fake_stage_3(features, ctx, *, model, cost_tracker, **_kw):
        from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="")
                for f in features
            ],
            cost_usd=0.0, llm_calls=0, warnings=[],
        )

    def _fake_stage_4(unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        # Synthesise five residuals so they dominate the feature set.
        residuals = [
            DeveloperFeature(
                name=f"residual-{i}",
                paths=("src/random.ts",),
                sources=["llm-fallback"],
                confidence="low",
                rationale="test",
            )
            for i in range(5)
        ]
        return Stage4Result(
            residual_features=residuals,
            cost_usd=0.10, llm_calls=1, warnings=[],
            chunks_processed=1, rejected_names=[],
        )

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)

    out_path = tmp_path / "fm.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)
    # 5 residual + 0 deterministic → 100% fallback share
    assert result["llm_fallback_pct"] == 1.0
    assert any("LLM-fallback handled" in w for w in result["warnings"])
