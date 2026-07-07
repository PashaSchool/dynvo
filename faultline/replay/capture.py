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
  ON);
* capture cost stays off the hot path (perf wave R1, 2026-07-07):
  documents serialize COMPACT — every reader goes through ``json.load``
  (:func:`load_stage_input`); nothing byte-compares capture files, and
  ``indent=1`` forced the pure-Python encoder (80% of papermark's
  profiled scan time). During a pipeline run the dumps+gzip+write also
  moves to ONE background writer thread with a bounded queue
  (:func:`install_async_writer` / :func:`drain_async_writer`, drained
  at scan end); ``to_jsonable`` stays synchronous so the captured state
  is snapshotted BEFORE any later mutation.

No LLM. No network. Pure local-disk JSON (+ gzip).
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any

from faultline.replay.serialize import from_jsonable, to_jsonable

logger = logging.getLogger(__name__)

__all__ = [
    "GZIP_THRESHOLD_BYTES",
    "MissingStageInputError",
    "capture_enabled",
    "drain_async_writer",
    "install_async_writer",
    "load_stage_input",
    "stage_input_path",
    "uninstall_async_writer",
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


# ── Background writer (perf wave R1) ────────────────────────────────────

_QUEUE_MAXSIZE = 4  # bounded: backpressure beats RAM growth (state trees are big)


class _AsyncCaptureWriter:
    """Single FIFO background thread that serializes + writes capture docs.

    * ORDER: one queue, one thread — artifacts land in submit order
      (identical to the sync path's per-stage write order).
    * BOUNDED: ``maxsize=4`` — a slow disk applies backpressure to the
      pipeline instead of accumulating encoded state trees in memory.
    * FAILURES: logged per item (the capture contract is "never break a
      scan"), counted, and re-summarized loudly at drain time.
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[tuple[dict, Path, int, str] | None]" = (
            queue.Queue(maxsize=_QUEUE_MAXSIZE)
        )
        self.failures = 0
        self._thread = threading.Thread(
            target=self._loop, name="replay-capture-writer", daemon=True,
        )
        self._thread.start()

    def submit(
        self, doc: dict, run_dir: Path, stage_index: int, stage_name: str,
    ) -> None:
        self._queue.put((doc, run_dir, stage_index, stage_name))

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            doc, run_dir, stage_index, stage_name = item
            try:
                _serialize_and_write(doc, run_dir, stage_index, stage_name)
            except Exception as exc:  # noqa: BLE001 — never break a scan
                self.failures += 1
                logger.warning(
                    "replay.capture(async): failed to write "
                    "%02d-stage-%s-input under %s: %s",
                    stage_index, stage_name, run_dir, exc,
                )
            finally:
                self._queue.task_done()

    def drain(self) -> None:
        """Block until every submitted capture document is on disk."""
        self._queue.join()
        if self.failures:
            logger.warning(
                "replay.capture(async): %d capture write(s) failed this run "
                "(see earlier warnings) — scan output is unaffected, but the "
                "affected stages are not replayable from this run dir",
                self.failures,
            )
            self.failures = 0

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join()


_async_writer: _AsyncCaptureWriter | None = None
_async_lock = threading.Lock()


def install_async_writer() -> None:
    """Route capture writes through the background writer (idempotent).

    Called by ``run_pipeline_v2`` at scan start; paired with
    :func:`drain_async_writer` at scan end. An ``atexit`` drain guards
    abnormal exits so an aborted scan still flushes what it queued.
    The writer thread is shared across scans in one process — FIFO
    order makes a later drain cover earlier leftovers too.
    """
    global _async_writer
    with _async_lock:
        if _async_writer is None:
            _async_writer = _AsyncCaptureWriter()
            atexit.register(drain_async_writer)


def drain_async_writer() -> None:
    """Block until all queued capture writes hit disk (no-op when sync)."""
    writer = _async_writer
    if writer is not None:
        writer.drain()


def uninstall_async_writer() -> None:
    """Drain + remove the background writer (test hygiene; scans keep it)."""
    global _async_writer
    with _async_lock:
        writer = _async_writer
        _async_writer = None
    if writer is not None:
        writer.drain()
        writer.stop()


def _serialize_and_write(
    doc: dict, target_dir: Path, stage_index: int, stage_name: str,
) -> Path:
    """dumps + (gzip when large) + write. Runs on the writer thread when
    the async writer is installed, inline otherwise. COMPACT JSON — see
    the module docstring's R1 invariant."""
    raw = json.dumps(doc, separators=(",", ":")).encode("utf-8")
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
            # SYNCHRONOUS encode on purpose: to_jsonable builds a fresh
            # tree, snapshotting the state slice BEFORE the stage (or a
            # later stage) mutates the live objects.
            "state": {key: to_jsonable(value) for key, value in state.items()},
        }
        writer = _async_writer
        if writer is not None:
            target_dir = Path(run_dir)
            writer.submit(doc, target_dir, stage_index, stage_name)
            # The artifact lands as .json or .json.gz depending on size,
            # decided on the writer thread; ``stage_input_path`` resolves
            # either form. Return the base path as the canonical handle.
            return target_dir / _base_name(stage_index, stage_name)
        return _serialize_and_write(doc, Path(run_dir), stage_index, stage_name)
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
