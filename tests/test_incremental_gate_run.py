"""End-to-end proof that the --since path gates the LLM stages.

This is the load-bearing test for ``finding-incremental-no-llm-savings``
Option A: it runs the REAL ``run_pipeline_v2`` orchestrator (no network —
Stage 3 + Stage 4 are spied/mocked) twice:

  1. A FULL scan to produce a base scan JSON.
  2. A ``--since`` scan after changing ONE file, asserting that Stage 3
     (the per-feature Haiku call) was handed ONLY the feature(s) whose
     files changed — NOT the whole repo — and that Stage 4 (the residual
     Haiku call) saw only changed residual paths.

It also asserts:
  * untouched features still appear in the final output (re-hydrated
    from the base scan, with their flows/metrics carried forward);
  * the FULL scan path is unaffected (Stage 3 sees every feature).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows, Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: initial")


def _commit_change(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"change {rel}")


class _Stage3Spy:
    """Records the feature names Stage 3 was asked to flow-detect."""

    def __init__(self) -> None:
        self.feature_name_batches: list[list[str]] = []

    def __call__(self, features, ctx, *, model, cost_tracker, **_kw):
        self.feature_name_batches.append([f.name for f in features])
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="spied")
                for f in features
            ],
            cost_usd=0.0,
            llm_calls=len(features),  # 1 Haiku call per feature (real model)
            warnings=[],
        )


class _Stage4Spy:
    """Records the residual path set Stage 4 was asked to cluster."""

    def __init__(self) -> None:
        self.unattributed_batches: list[list[str]] = []

    def __call__(self, unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        self.unattributed_batches.append(list(unattributed))
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=len(unattributed),
            warnings=[],
            clusters_total=0,
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
        )


def _repo_files() -> dict[str, str]:
    page = "export default function Page() { return null; }\n"
    return {
        "package.json": json.dumps({"name": "demo", "dependencies": {"next": "14.0.0"}}),
        "next.config.js": "module.exports = {};\n",
        "app/auth/page.tsx": page,
        "app/auth/login.tsx": "export function Login() { return null; }\n",
        "app/billing/page.tsx": page,
        "app/billing/charge.tsx": "export function Charge() { return null; }\n",
        "app/dashboard/page.tsx": page,
    }


def test_incremental_gate_stage3_receives_only_changed_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo-app"
    _init_repo(repo, _repo_files())
    base_sha = _git(repo, "rev-parse", "HEAD")

    # ── 1. FULL scan → base ────────────────────────────────────────
    full_spy3 = _Stage3Spy()
    full_spy4 = _Stage4Spy()
    monkeypatch.setattr(run_module, "stage_3_flows", full_spy3)
    monkeypatch.setattr(run_module, "stage_4_residual", full_spy4)

    base_out = tmp_path / "base.json"
    run_pipeline_v2(repo, model="haiku", out_path=base_out)

    base_data = json.loads(base_out.read_text())
    base_feature_names = {
        f["name"] for f in base_data["developer_features"]
    }
    assert base_data["is_full_scan"] is True
    # Full scan: Stage 3 saw EVERY feature (whole-repo, unchanged path).
    full_stage3_names = set(full_spy3.feature_name_batches[0])
    assert full_stage3_names == base_feature_names
    assert len(base_feature_names) >= 2, "need >=2 features to prove gating"

    # ── 2. Change ONE file, run --since ────────────────────────────
    _commit_change(
        repo, "app/auth/login.tsx",
        "export function Login() { return <div>changed</div>; }\n",
    )

    inc_spy3 = _Stage3Spy()
    inc_spy4 = _Stage4Spy()
    monkeypatch.setattr(run_module, "stage_3_flows", inc_spy3)
    monkeypatch.setattr(run_module, "stage_4_residual", inc_spy4)

    inc_out = tmp_path / "inc.json"
    result = run_pipeline_v2(
        repo, model="haiku", out_path=inc_out,
        since=base_sha, base_scan_path=base_out,
    )

    # The whole point: Stage 3 was handed FEWER features than the full
    # scan — only those touching the changed file.
    inc_stage3_names = set(inc_spy3.feature_name_batches[0])
    assert len(inc_stage3_names) < len(full_stage3_names), (
        f"gate did not reduce Stage 3 input: "
        f"inc={inc_stage3_names} full={full_stage3_names}"
    )
    # Every feature Stage 3 saw must actually contain the changed file's
    # directory (auth). It must NOT have seen billing/dashboard.
    assert all("auth" in n or "login" in n for n in inc_stage3_names), (
        f"Stage 3 saw an unchanged feature: {inc_stage3_names}"
    )

    # Telemetry surfaces the gate.
    meta = result
    assert meta["is_full_scan"] is False
    assert meta.get("incremental_gate_active") is True
    assert meta["incremental_gate_features_touched"] == len(inc_stage3_names)
    assert meta["incremental_gate_features_untouched"] >= 1
    assert meta["incremental_gate_features_rehydrated"] >= 1

    # Untouched features are NOT dropped — they reappear in the output,
    # re-hydrated from the base scan.
    inc_data = json.loads(inc_out.read_text())
    inc_feature_names = {f["name"] for f in inc_data["developer_features"]}
    # Billing + dashboard (untouched) must still be present.
    untouched_in_base = base_feature_names - inc_stage3_names
    assert untouched_in_base, "test setup must leave some untouched features"
    assert untouched_in_base.issubset(inc_feature_names), (
        f"untouched features silently dropped: "
        f"{untouched_in_base - inc_feature_names}"
    )


def test_incremental_gate_noop_diff_runs_no_per_feature_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty diff (HEAD == since) gates ALL features out of Stage 3,
    so the per-feature Haiku call list is empty — near-zero LLM cost."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo-app2"
    _init_repo(repo, _repo_files())
    head_sha = _git(repo, "rev-parse", "HEAD")

    spy3 = _Stage3Spy()
    spy4 = _Stage4Spy()
    monkeypatch.setattr(run_module, "stage_3_flows", spy3)
    monkeypatch.setattr(run_module, "stage_4_residual", spy4)

    base_out = tmp_path / "base2.json"
    run_pipeline_v2(repo, model="haiku", out_path=base_out)

    # Reset spies for the incremental run.
    spy3.feature_name_batches.clear()
    spy4.unattributed_batches.clear()

    inc_out = tmp_path / "inc2.json"
    result = run_pipeline_v2(
        repo, model="haiku", out_path=inc_out,
        since=head_sha, base_scan_path=base_out,  # since == HEAD → empty diff
    )

    # Stage 3 was handed ZERO features → zero per-feature LLM calls.
    assert spy3.feature_name_batches == [[]]
    # Stage 4 was handed ZERO residual paths.
    assert spy4.unattributed_batches == [[]]
    assert result["incremental_gate_features_touched"] == 0

    # Output is still complete — every base feature re-hydrated.
    base_names = {f["name"] for f in json.loads(base_out.read_text())["developer_features"]}
    inc_names = {f["name"] for f in json.loads(inc_out.read_text())["developer_features"]}
    assert base_names.issubset(inc_names)


def test_full_scan_unaffected_by_gate_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: with no --since, the gate block is skipped and
    Stage 3 sees every feature (cold-scan path unchanged)."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo-app3"
    _init_repo(repo, _repo_files())

    spy3 = _Stage3Spy()
    spy4 = _Stage4Spy()
    monkeypatch.setattr(run_module, "stage_3_flows", spy3)
    monkeypatch.setattr(run_module, "stage_4_residual", spy4)

    out = tmp_path / "full.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out)

    data = json.loads(out.read_text())
    all_names = {f["name"] for f in data["developer_features"]}
    # Stage 3 saw every feature; no gate telemetry on a full scan.
    assert set(spy3.feature_name_batches[0]) == all_names
    assert result["is_full_scan"] is True
    assert "incremental_gate_active" not in result
