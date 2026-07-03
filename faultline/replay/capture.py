"""Per-stage INPUT persistence (replay v2, deterministic-foundation WS1).

Every pipeline-v2 stage persists a deep copy of its exact input — the
feature / flow / context slice it consumes — as
``<run_dir>/NN-stage-<name>-input.json`` next to the existing output
artifact (``NN-stage-<name>.json``). The payload is a NAMED-STATE dict
(``{"features": [...], "ctx": {...}}``) whose keys are the canonical
pipeline state keys the replay registry re-feeds to the stage.

Size guard: when the serialized document exceeds
:data:`GZIP_THRESHOLD_BYTES` (10 MB) it is gzip-compressed and written
as ``…-input.json.gz`` instead. :func:`load_stage_input` transparently
reads either form.

Invariants (determinism traps — see the WS1 spec):

* capture writes NEW files only — it never touches output artifacts,
  cache keys, or the scan result (the Phase-A snapshot gate must pass
  unchanged with capture enabled);
* nothing run-scoped is invented at encode time (no wall-clock, no
  uuid4) — the document contains exactly the encoded object graph;
* a capture failure NEVER breaks a scan (guarded, logged);
* kill-switch: ``FAULTLINE_STAGE_INPUTS=0`` disables capture (default
  ON).

No LLM. No network. Pure local-disk JSON (+ gzip).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from pathlib import Path
from typing import Any

from faultline.replay.serialize import from_jsonable, to_jsonable

logger = logging.getLogger(__name__)

__all__ = [
    "GZIP_THRESHOLD_BYTES",
    "MissingStageInputError",
    "capture_enabled",
    "load_stage_input",
    "stage_input_path",
    "write_stage_input",
]

#: gzip anything above this many serialized bytes (spec: "> N MB", N=10).
GZIP_THRESHOLD_BYTES: int = 10 * 1024 * 1024

_ENV_KILL_SWITCH = "FAULTLINE_STAGE_INPUTS"

#: Schema marker inside every input artifact — bump on breaking layout
#: changes so the loader can fail with a versioned message.
_INPUT_SCHEMA_VERSION = 1


class MissingStageInputError(FileNotFoundError):
    """A replay was requested for a stage whose input artifact is absent."""


def capture_enabled() -> bool:
    """Input capture is ON unless ``FAULTLINE_STAGE_INPUTS=0``."""
    return os.environ.get(_ENV_KILL_SWITCH, "1") != "0"


def _base_name(stage_index: int, stage_name: str) -> str:
    return f"{stage_index:02d}-stage-{stage_name}-input.json"


def stage_input_path(
    run_dir: Path | str,
    stage_index: int,
    stage_name: str,
) -> Path:
    """Resolve the on-disk input artifact for a stage (plain or .gz).

    Returns the plain ``.json`` path when neither exists (callers that
    are about to WRITE use this default; readers should go through
    :func:`load_stage_input` which raises a clear error instead).
    """
    base = Path(run_dir) / _base_name(stage_index, stage_name)
    if base.exists():
        return base
    gz = base.with_name(base.name + ".gz")
    if gz.exists():
        return gz
    return base


def write_stage_input(
    run_dir: Path | str | None,
    stage_index: int,
    stage_name: str,
    state: dict[str, Any],
) -> Path | None:
    """Persist the named-state input slice for one stage.

    Args:
        run_dir: the per-run artifact directory (``ctx.run_dir``).
            ``None`` (CLI planning modes) skips capture silently.
        stage_index: numeric artifact prefix — matches the stage's
            OUTPUT artifact (``NN-stage-<name>.json``).
        stage_name: artifact stage name (same as the output artifact).
        state: named pipeline-state slice this stage consumes. Values
            are encoded via :func:`faultline.replay.serialize.to_jsonable`.

    Returns:
        The written path, or ``None`` when capture is disabled /
        skipped / failed. Failures are logged, never raised — input
        capture must never break a scan.
    """
    if run_dir is None or not capture_enabled():
        return None
    try:
        doc = {
            "input_schema_version": _INPUT_SCHEMA_VERSION,
            "stage_index": stage_index,
            "stage_name": stage_name,
            "state": {key: to_jsonable(value) for key, value in state.items()},
        }
        raw = json.dumps(doc, indent=1).encode("utf-8")
        target_dir = Path(run_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        base = target_dir / _base_name(stage_index, stage_name)
        if len(raw) > GZIP_THRESHOLD_BYTES:
            gz_path = base.with_name(base.name + ".gz")
            # mtime=0 → byte-stable gzip output for identical content.
            with open(gz_path, "wb") as fp:
                with gzip.GzipFile(fileobj=fp, mode="wb", mtime=0) as gz:
                    gz.write(raw)
            # Drop a stale plain twin from an earlier smaller run.
            base.unlink(missing_ok=True)
            return gz_path
        base.write_bytes(raw)
        base.with_name(base.name + ".gz").unlink(missing_ok=True)
        return base
    except Exception as exc:  # noqa: BLE001 — capture must never break a scan
        logger.warning(
            "replay.capture: failed to write %02d-stage-%s-input under %s: %s",
            stage_index, stage_name, run_dir, exc,
        )
        return None


def load_stage_input(
    run_dir: Path | str,
    stage_index: int,
    stage_name: str,
) -> dict[str, Any]:
    """Load + decode one stage's input artifact.

    Raises:
        MissingStageInputError: naming the exact artifact file when it
            does not exist in ``run_dir`` (e.g. a run recorded before
            replay v2 shipped, or a stage that was disabled for that
            run).
    """
    path = stage_input_path(run_dir, stage_index, stage_name)
    if not path.exists():
        raise MissingStageInputError(
            f"stage input artifact not found: {Path(run_dir) / _base_name(stage_index, stage_name)}"
            f"[.gz] — the source run predates replay v2 for this stage, or "
            f"the stage did not run (env-gated) in that scan",
        )
    if path.suffix == ".gz":
        raw = gzip.decompress(path.read_bytes())
    else:
        raw = path.read_bytes()
    doc = json.loads(raw)
    version = doc.get("input_schema_version")
    if version != _INPUT_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: input_schema_version {version!r} != "
            f"{_INPUT_SCHEMA_VERSION} (regenerate the run with the "
            f"current engine)",
        )
    return {k: from_jsonable(v) for k, v in doc.get("state", {}).items()}
