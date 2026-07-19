"""WS1 ship-gate (b): mutation test on the pinned formbricks clone.

Monkeypatch ONE stage's constants (Stage 6.9 test-strip marker sets →
the strip becomes a no-op), replay the FULL chain (intake → output),
and assert the deltas vs the baseline appear ONLY at/downstream of the
mutated stage:

* every artifact of a stage ordered BEFORE test_strip is byte-identical
  (normalized) to the baseline;
* the mutated stage's own artifact differs (726 test paths kept);
* the final Stage 7 artifact differs (stripped features survive).

Same baseline + $0 preconditions as test_replay_identity_formbricks.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from faultline.replay.compare import diff_summary, load_artifact
from faultline.replay.registry import STAGES, stage_by_key

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


@pytest.fixture(autouse=True)
def _pin_ws1_baseline_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """MECHANICAL flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): the
    ws1-baseline was recorded with DEFAULT env BEFORE the flip — mutation
    replay must run in the baseline's recorded world, so the flipped
    defaults are pinned back to the recorded (unset≡OFF) semantics via the
    kill-switch. Lift these pins when the baseline is re-recorded under
    the new default world."""
    monkeypatch.setenv("FAULTLINE_UF_DET_AGGREGATION", "0")
    monkeypatch.setenv("FAULTLINE_UF_REFINE_TOKEN_SCALE", "0")
    monkeypatch.setenv("FAULTLINE_LLM_BATCH_CANON", "0")
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "0")


MUTATED_STAGE = "test_strip"


def test_mutation_deltas_only_downstream(monkeypatch):
    import faultline.pipeline_v2.stage_6_9_test_strip as ts
    from faultline.replay.runner import replay

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-replay-cache-only")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    # The mutation: no path ever classifies as a test file.
    monkeypatch.setattr(ts, "_TEST_DIR_SEGMENTS", frozenset())
    monkeypatch.setattr(ts, "_TEST_MARKER_TOKENS", frozenset())

    report = replay(str(BASELINE), "intake", through="output")

    mutated_order = stage_by_key(MUTATED_STAGE).order
    order_by_artifact = {
        f"{s.index:02d}-stage-{s.key}.json": s.order for s in STAGES
    }

    upstream_diffs: dict[str, list[str]] = {}
    downstream_changed: set[str] = set()
    for original in sorted(BASELINE.glob("*-stage-*.json")):
        if "-input" in original.name:
            continue
        order = order_by_artifact.get(original.name)
        assert order is not None, f"unregistered artifact {original.name}"
        twin = report.new_run_dir / original.name
        assert twin.exists(), f"chain replay wrote no {original.name}"
        diffs = diff_summary(load_artifact(original), load_artifact(twin))
        if order < mutated_order and diffs:
            upstream_diffs[original.name] = diffs
        if order >= mutated_order and diffs:
            downstream_changed.add(original.name)

    assert not upstream_diffs, (
        f"mutation of {MUTATED_STAGE} leaked UPSTREAM: {upstream_diffs} "
        f"(replay dir kept at {report.new_run_dir})"
    )
    assert f"06-stage-{MUTATED_STAGE}.json" in downstream_changed, (
        "the mutated stage's own artifact did not change — mutation inert?"
    )
    assert "07-stage-output.json" in downstream_changed, (
        "the mutation did not propagate to the final output artifact"
    )
    shutil.rmtree(report.new_run_dir, ignore_errors=True)
