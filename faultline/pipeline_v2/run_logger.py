"""Per-stage structured logging for pipeline-v2 runs.

Each stage gets its own :class:`StageLogger` instance that buffers
line-delimited JSON records and flushes them to
``<run_dir>/NN-stage-<name>.log`` at :meth:`close` time.

The schema is deliberately tiny — one record per decision:

    {"ts": ISO8601, "stage": int, "stage_name": str,
     "event": "emit"|"drop"|"warn"|"cluster",
     "feature": str|None, "reason": str, ...extra}

Buffering matters: in a hot loop (Stage 4 may iterate over hundreds
of residual paths) we don't want a write() syscall per call. We flush
on :meth:`close`, which the orchestrator invokes via a context manager
right after each stage returns.

No LLM. No network. Pure local-disk JSONL.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

logger = logging.getLogger(__name__)


# ── Structured stage-lifecycle events (local dev observability) ─────────
# Besides the per-stage .log artifacts, emit one JSON line per stage start/end
# to an append-only stream the local watcher tails (scripts/llm-subscription-
# proxy/watcher.py in faultlines-app). Pure local disk, fully guarded — a
# failure here NEVER affects a scan. Path from FAULTLINE_STAGE_EVENTS (default
# ~/.faultline/stage-events.jsonl); set it to "0" or "" to disable.
def _emit_stage_event(record: dict[str, Any]) -> None:
    raw = os.environ.get("FAULTLINE_STAGE_EVENTS", "~/.faultline/stage-events.jsonl")
    if not raw or raw == "0":
        return
    try:
        path = Path(raw).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", datetime.now(tz=timezone.utc).isoformat())
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass  # observability must never break a scan


_VALID_EVENTS = frozenset({"emit", "drop", "warn", "cluster", "info"})


@dataclass(frozen=True)
class _LogRecord:
    ts: str
    stage: int
    stage_name: str
    event: str
    feature: str | None
    reason: str
    extra: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "ts": self.ts,
            "stage": self.stage,
            "stage_name": self.stage_name,
            "event": self.event,
            "feature": self.feature,
            "reason": self.reason,
        }
        if self.extra:
            base.update(self.extra)
        return base


class StageLogger:
    """Buffered JSONL writer for one stage of a pipeline run.

    Use as a context manager so flush + close happen even on error:

        with StageLogger(run_dir, 4, "residual") as log:
            log.emit("billing", "clustered from 12 paths")
            log.drop("trpc-router", "name in JUNK_NAMES")
    """

    def __init__(
        self,
        run_dir: Path,
        stage_num: int,
        stage_name: str,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._stage_num = stage_num
        self._stage_name = stage_name
        self._buffer: list[_LogRecord] = []
        self._closed = False
        self._t0 = time.monotonic()
        self._stage_ctx_token: int | None = None
        # Ensure the directory exists; cheap idempotent call.
        self._run_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """Absolute path the log file will be written to on close."""
        fname = f"{self._stage_num:02d}-stage-{self._stage_name}.log"
        return self._run_dir / fname

    def emit(self, feature: str | None, reason: str, **extra: Any) -> None:
        """Record an emitted feature (the stage decided to keep it)."""
        self._append("emit", feature, reason, extra)

    def drop(self, feature: str | None, reason: str, **extra: Any) -> None:
        """Record a dropped feature with the reason it was filtered."""
        self._append("drop", feature, reason, extra)

    def warn(self, reason: str, feature: str | None = None, **extra: Any) -> None:
        """Record a non-fatal warning (e.g. high LLM fallback share)."""
        self._append("warn", feature, reason, extra)

    def cluster(self, reason: str, **extra: Any) -> None:
        """Record a clustering decision (typically without a feature name)."""
        self._append("cluster", None, reason, extra)

    def info(self, reason: str, feature: str | None = None, **extra: Any) -> None:
        """Record an informational event (timing, counts, etc.)."""
        self._append("info", feature, reason, extra)

    def close(self) -> None:
        """Flush all buffered records to disk and prevent further writes."""
        if self._closed:
            return
        self._closed = True
        try:
            with self.path.open("w", encoding="utf-8") as fp:
                for rec in self._buffer:
                    fp.write(json.dumps(rec.to_dict(), default=str))
                    fp.write("\n")
        except OSError as exc:
            # Debug artifact — never break a scan because logging failed.
            logger.warning(
                "StageLogger: failed to write %s: %s", self.path, exc,
            )

    # ── Context manager ─────────────────────────────────────────────

    def __enter__(self) -> "StageLogger":
        self._t0 = time.monotonic()
        self._lifecycle("stage_start")
        # Mark this stage current so LLM call sites can tag their requests.
        try:
            from faultline.llm.stage_context import push_stage
            self._stage_ctx_token = push_stage(self._stage_num, self._stage_name)
        except Exception:  # noqa: BLE001 — marker is best-effort, never break a scan
            self._stage_ctx_token = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._lifecycle(
            "stage_end",
            dur_s=round(time.monotonic() - self._t0, 3),
            status="error" if exc_type else "ok",
            error=(str(exc_val)[:300] if exc_val else None),
        )
        if self._stage_ctx_token is not None:
            try:
                from faultline.llm.stage_context import pop_stage
                pop_stage(self._stage_ctx_token)
            except Exception:  # noqa: BLE001
                pass
        self.close()

    def _lifecycle(self, event: str, **extra: Any) -> None:
        """Emit a structured stage-lifecycle event to the watcher stream."""
        rd = self._run_dir
        _emit_stage_event({
            "event": event,
            "stage": self._stage_num,
            "stage_name": self._stage_name,
            "slug": rd.parent.name,
            "run_id": rd.name,
            "run_dir": str(rd),
            **extra,
        })

    # ── Internal ────────────────────────────────────────────────────

    def _append(
        self,
        event: str,
        feature: str | None,
        reason: str,
        extra: dict[str, Any],
    ) -> None:
        if self._closed:
            # Late-write guard — should never happen with proper context-manager use.
            logger.warning(
                "StageLogger(%s): write after close ignored: %s/%s",
                self._stage_name, event, reason,
            )
            return
        if event not in _VALID_EVENTS:
            raise ValueError(f"unknown StageLogger event: {event!r}")
        self._buffer.append(
            _LogRecord(
                ts=datetime.now(tz=timezone.utc).isoformat(),
                stage=self._stage_num,
                stage_name=self._stage_name,
                event=event,
                feature=feature,
                reason=reason,
                extra=dict(extra),
            ),
        )


__all__ = ["StageLogger"]
