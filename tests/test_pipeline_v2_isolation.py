"""Sprint A0 invariant tests for pipeline-v2 run isolation.

Covers:

  - Deep-copy boundary between stages — a misbehaving stage that
    mutates its input cannot taint the artifact captured for the
    upstream stage.
  - ``latest`` symlink swaps between two sequential scans of the
    same repo.
  - The ``--run-id`` override path produces a directory of that name.

The full end-to-end orchestrator behaviour is in
``test_run_pipeline_v2.py`` — this file is intentionally narrow,
focused on the new invariants Sprint A0 introduced.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result


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


def _patch_llm_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace Stage 3 + Stage 4 with no-op fakes to skip Anthropic."""

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


def _build_next_repo(repo: Path) -> None:
    _git_init_with_one_commit(
        repo,
        {
            "package.json": json.dumps(
                {"name": "demo", "dependencies": {"next": "14.0.0"}},
            ),
            "app/billing/page.tsx": "export default function P() { return null; }\n",
            "app/auth/page.tsx": "export default function P() { return null; }\n",
            "next.config.js": "module.exports = {};\n",
        },
    )


# ── Deep-copy invariant ─────────────────────────────────────────────────


def test_stage_mutation_does_not_taint_upstream_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Stage 2 mutates the Stage 1 output dict, the artifact written
    for Stage 1 must remain unaffected.

    We swap in a Stage 2 fake that aggressively mutates its input
    (clears the extractor candidates lists). Without the deep-copy
    boundary in ``run_pipeline_v2`` this would zero out the Stage 1
    artifact too, because both pointers would refer to the same dict.
    """
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo"
    _build_next_repo(repo)
    _patch_llm_stages(monkeypatch)

    real_stage_2 = run_module.stage_2_reconcile

    def _mutating_stage_2(stage1_out, ctx, **kw):
        # Mutate the input — clear every extractor's candidate list.
        # In a buggy pipeline this would also clear the artifact dict
        # already captured for Stage 1.
        for name in list(stage1_out.keys()):
            if name != "_errors" and isinstance(stage1_out[name], list):
                stage1_out[name].clear()
        return real_stage_2(stage1_out, ctx, **kw)

    monkeypatch.setattr(run_module, "stage_2_reconcile", _mutating_stage_2)

    out_path = tmp_path / "fm.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)
    run_id = result["run_id"]
    run_dir = fake_home / ".faultline" / "logs" / "demo" / run_id

    stage_1 = json.loads((run_dir / "01-stage-extractors.json").read_text())
    # The route extractor should have found app/billing + app/auth.
    # If the deep-copy boundary failed, the mutating Stage 2 would
    # have cleared them and this count would be 0.
    total_hits = sum(stage_1["extractor_hits"].values())
    assert total_hits > 0, (
        f"Stage 1 artifact was tainted by Stage 2 mutation: {stage_1}"
    )


# ── Latest-symlink swap ─────────────────────────────────────────────────


def test_latest_symlink_swaps_to_newest_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two scans → ``latest`` points at the most recent ``run_id``."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo"
    _build_next_repo(repo)
    _patch_llm_stages(monkeypatch)

    out_path = tmp_path / "fm.json"
    first = run_pipeline_v2(
        repo, model="haiku", out_path=out_path, run_id="baseline",
    )
    second = run_pipeline_v2(
        repo, model="haiku", out_path=out_path, run_id="with-clustering",
    )

    assert first["run_id"] == "baseline"
    assert second["run_id"] == "with-clustering"

    slug_dir = fake_home / ".faultline" / "logs" / "demo"
    link = slug_dir / "latest"
    assert link.is_symlink()
    assert os.readlink(link) == "with-clustering"

    # Both run dirs exist side-by-side.
    assert (slug_dir / "baseline" / "00-stage-intake.json").is_file()
    assert (slug_dir / "with-clustering" / "00-stage-intake.json").is_file()


# ── --run-id override is propagated ────────────────────────────────────


def test_explicit_run_id_overrides_auto_generated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo"
    _build_next_repo(repo)
    _patch_llm_stages(monkeypatch)

    out_path = tmp_path / "fm.json"
    result = run_pipeline_v2(
        repo, model="haiku", out_path=out_path, run_id="experiment-7",
    )
    assert result["run_id"] == "experiment-7"
    run_dir = fake_home / ".faultline" / "logs" / "demo" / "experiment-7"
    assert run_dir.is_dir()
    # FeatureMap on disk also has the run_id pinned in scan_meta.
    data = json.loads(out_path.read_text())
    assert data["scan_meta"]["run_id"] == "experiment-7"


# ── Per-stage log files written ────────────────────────────────────────


def test_every_stage_emits_its_jsonl_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 8 stages must drop a ``NN-stage-<name>.log`` next to their
    JSON artifact.
    """
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo"
    _build_next_repo(repo)
    _patch_llm_stages(monkeypatch)

    out_path = tmp_path / "fm.json"
    result = run_pipeline_v2(
        repo, model="haiku", out_path=out_path, run_id="logtest",
    )
    run_dir = fake_home / ".faultline" / "logs" / "demo" / result["run_id"]

    for stage_num, stage_name in (
        (0, "intake"), (1, "extractors"), (2, "reconcile"),
        (3, "flows"), (4, "residual"), (5, "postprocess"),
        (6, "metrics"), (7, "output"),
    ):
        path = run_dir / f"{stage_num:02d}-stage-{stage_name}.log"
        assert path.is_file(), f"missing: {path}"
        # Each line is a valid JSON object with the expected schema.
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            assert "ts" in rec and "stage" in rec and "event" in rec
            assert rec["stage"] == stage_num
            assert rec["stage_name"] == stage_name


# ── run_id surfaces everywhere downstream ──────────────────────────────


def test_scan_meta_carries_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo"
    _build_next_repo(repo)
    _patch_llm_stages(monkeypatch)

    out_path = tmp_path / "fm.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)
    assert result["run_id"] is not None

    # FeatureMap JSON has it pinned in scan_meta.
    data = json.loads(out_path.read_text())
    assert data["scan_meta"]["run_id"] == result["run_id"]

    # stage_artifact_dir in scan_meta points at the RUN dir, not just
    # the slug dir.
    assert result["run_id"] in data["scan_meta"]["stage_artifact_dir"]
