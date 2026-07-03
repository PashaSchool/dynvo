"""WS1 ship-gate (a): identity replay on the pinned formbricks clone.

For EVERY stage recorded in a baseline run, replaying that single
stage with unchanged code must produce an output artifact
byte-identical (after :mod:`faultline.replay.compare` normalization,
which reuses the Phase-A ``normalize_scan`` catalogue) to the
original run's ``NN-stage-<name>.json``.

Requirements to run (skipped otherwise — CI boxes without the pinned
corpus / baseline skip cleanly):

* the pinned clone at the snapshot-gate path;
* a baseline run recorded with input capture, default env, and the
  warm llm-cache (see faultline/replay/README.md):

      dynvo scan-v2 <formbricks-clone> --run-id ws1-baseline

$0: the dummy API key below never authenticates; every LLM unit is
served from the content-keyed llm-cache exactly as in the baseline.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from faultline.replay.compare import diff_summary, load_artifact
from faultline.replay.registry import STAGES

BASELINE = Path(
    os.environ.get(
        "FAULTLINE_REPLAY_BASELINE",
        str(Path.home() / ".faultline/logs/formbricks/ws1-baseline"),
    ),
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not BASELINE.is_dir(),
        reason=f"replay baseline run not found at {BASELINE}",
    ),
]


def _identity_specs():
    """Stages with BOTH a recorded input and an output artifact."""
    if not BASELINE.is_dir():
        return []
    out = []
    for spec in STAGES:
        has_input = (
            (BASELINE / f"{spec.index:02d}-stage-{spec.key}-input.json").exists()
            or (BASELINE / f"{spec.index:02d}-stage-{spec.key}-input.json.gz").exists()
        )
        has_output = (BASELINE / f"{spec.index:02d}-stage-{spec.key}.json").exists()
        if has_input and has_output:
            out.append(spec)
    return out


@pytest.fixture(autouse=True)
def _cache_only_key(monkeypatch):
    # Force the never-authenticating key: LLM clients get constructed
    # (so cache lookups happen) but any cache MISS costs $0 (401).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-replay-cache-only")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


@pytest.mark.parametrize(
    "spec", _identity_specs(), ids=lambda s: f"{s.index:02d}-{s.key}",
)
def test_identity_replay(spec):
    from faultline.replay.runner import replay

    report = replay(str(BASELINE), spec.key)
    artifact = f"{spec.index:02d}-stage-{spec.key}.json"
    original = load_artifact(BASELINE / artifact)
    replayed_path = report.new_run_dir / artifact
    assert replayed_path.exists(), (
        f"replay of {spec.key} wrote no {artifact} in {report.new_run_dir}"
    )
    replayed = load_artifact(replayed_path)
    diffs = diff_summary(original, replayed)
    assert not diffs, (
        f"identity replay of {spec.key} diverged: {diffs} "
        f"(replay dir kept at {report.new_run_dir})"
    )
    shutil.rmtree(report.new_run_dir, ignore_errors=True)


def test_missing_input_artifact_fails_with_artifact_name(tmp_path):
    from faultline.replay.capture import MissingStageInputError
    from faultline.replay.runner import replay

    empty_run = tmp_path / "logs" / "x" / "empty-run"
    empty_run.mkdir(parents=True)
    with pytest.raises(MissingStageInputError, match="03-stage-flows-input.json"):
        replay(str(empty_run), "flows")
