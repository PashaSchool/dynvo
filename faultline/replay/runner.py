"""Replay executor — run one pipeline_v2 stage (or a chain) from a
recorded run's input artifacts, with the CURRENT code.

Semantics (deterministic-foundation WS1):

* ``replay(run, stage)`` — reconstruct the stage's input from
  ``NN-stage-<name>-input.json`` in the source run dir, execute the
  stage's runner (current code), write the stage's output artifact
  into a NEW run dir under the same slug.
* ``--through <name>`` — chain downstream stages in pipeline order.
  Each downstream stage starts from ITS OWN recorded input, overlaid
  with any state keys the replayed upstream stages produced. A code
  change in stage N therefore propagates exactly the way it would in a
  live scan, while state that N does not influence stays pinned to the
  recorded run — deltas appear ONLY downstream of N (the mutation
  ship-gate).
* LLM stages replay against the content-keyed llm-cache by default;
  ``fresh_llm=True`` clears ONLY the target stage kind's llm-cache
  subdir (``~/.faultline/llm-cache/<kind>/`` — the align-v2 protocol,
  formalized).
* The new run dir carries ``replay-meta.json`` and, when the chain
  reaches Stage 7, ``scan_meta.replayed_from = <source run id>``.

Missing input artifacts fail LOUD with the artifact name — except
``optional`` (env-gated) stages mid-chain, which are skipped exactly
like the live pipeline skips them when their gate is off.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultline.replay.capture import (
    MissingStageInputError,
    load_stage_input,
    write_stage_input,
)
from faultline.replay.registry import (
    ReplayEnv,
    StageSpec,
    pipeline_slice,
    relink_bipartite,
)

logger = logging.getLogger(__name__)

__all__ = ["ReplayReport", "resolve_run_dir", "replay"]


@dataclass
class ReplayReport:
    source_run_dir: Path
    new_run_dir: Path
    new_run_id: str
    stages_run: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    out_path: Path | None = None


def resolve_run_dir(run: str) -> Path:
    """Resolve ``--run`` into an existing run directory.

    Accepts an absolute/relative path, or ``<slug>/<run_id>`` /
    ``<slug>/latest`` under ``~/.faultline/logs/``.
    """
    p = Path(run).expanduser()
    if p.is_dir():
        return p.resolve()
    from faultline.cache.paths import faultline_base_dir

    candidate = faultline_base_dir() / "logs" / run
    if candidate.is_dir():
        return candidate.resolve()
    raise FileNotFoundError(
        f"--run {run!r} is neither a directory nor "
        f"<slug>/<run_id> under {faultline_base_dir() / 'logs'}",
    )


def _new_run_id(source_run_dir: Path, stage: str) -> str:
    """Sequence-numbered replay id — no wall-clock, collision-free."""
    slug_dir = source_run_dir.parent
    base = f"replay-{source_run_dir.name}-{stage}"
    seq = 1
    while (slug_dir / f"{base}-{seq}").exists():
        seq += 1
    return f"{base}-{seq}"


def _fresh_llm_bust(spec: StageSpec) -> str | None:
    """Clear ONLY the target stage kind's llm-cache subdir."""
    if spec.llm_cache_dir is None:
        return None
    from faultline.cache.paths import faultline_base_dir

    target = faultline_base_dir() / "llm-cache" / spec.llm_cache_dir
    if target.is_dir():
        shutil.rmtree(target)
        return str(target)
    return None


def _prepare_state(state: dict[str, Any], env: ReplayEnv) -> None:
    """Post-overlay fixups: point ctx at the replay run + restore flow
    object identity between features and the bipartite projection."""
    ctx = state.get("ctx")
    if ctx is not None:
        ctx.run_id = env.run_id
        ctx.run_dir = env.run_dir
        ctx.cache_backend = env.cache_backend()
    if "bipartite_flows" in state and "features" in state:
        state["bipartite_flows"] = relink_bipartite(
            state["bipartite_flows"], state["features"],
        )


def replay(
    run: str,
    stage: str,
    *,
    through: str | None = None,
    env_overrides: dict[str, str] | None = None,
    fresh_llm: bool = False,
) -> ReplayReport:
    """Replay ``stage`` (optionally chaining ``--through``) from ``run``."""
    source_run_dir = resolve_run_dir(run)
    specs = pipeline_slice(stage, through)
    target = specs[0]

    new_run_id = _new_run_id(source_run_dir, target.key)
    new_run_dir = source_run_dir.parent / new_run_id
    new_run_dir.mkdir(parents=True, exist_ok=False)

    env_backup: dict[str, str | None] = {}
    for k, v in (env_overrides or {}).items():
        env_backup[k] = os.environ.get(k)
        os.environ[k] = v

    busted = None
    if fresh_llm:
        busted = _fresh_llm_bust(target)

    env = ReplayEnv(run_dir=new_run_dir, run_id=new_run_id)
    report = ReplayReport(
        source_run_dir=source_run_dir,
        new_run_dir=new_run_dir,
        new_run_id=new_run_id,
    )

    meta = {
        "replayed_from": source_run_dir.name,
        "source_run_dir": str(source_run_dir),
        "stage": target.key,
        "through": specs[-1].key if through else None,
        "env_overrides": dict(env_overrides or {}),
        "fresh_llm": fresh_llm,
        "fresh_llm_busted_dir": busted,
    }
    (new_run_dir / "replay-meta.json").write_text(json.dumps(meta, indent=2))

    overrides: dict[str, Any] = {}
    try:
        for spec in specs:
            try:
                state = load_stage_input(source_run_dir, spec.index, spec.key)
            except MissingStageInputError:
                if spec.key == target.key:
                    raise
                if spec.optional:
                    report.stages_skipped.append(spec.key)
                    logger.info(
                        "replay: skipping %s (no input artifact — stage "
                        "was gated off in the source run)", spec.key,
                    )
                    continue
                raise
            # Overlay the state produced by upstream replayed stages.
            for k, v in overrides.items():
                if k in state:
                    state[k] = v
            _prepare_state(state, env)
            state["_replayed_from"] = source_run_dir.name
            # Mirror-capture: the replay run dir is itself replayable.
            write_stage_input(
                new_run_dir, spec.index, spec.key,
                {k: v for k, v in state.items() if not k.startswith("_")},
            )
            outputs = spec.run(env, state)
            overrides.update(outputs)
            report.stages_run.append(spec.key)
            if "out_path" in outputs:
                report.out_path = Path(outputs["out_path"])
    finally:
        for k, old in env_backup.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    return report
