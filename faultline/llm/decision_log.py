"""Phase-0 LLM decision logging — the training-dataset tap.

Product-Spine Wave 2a rider (approved LLM-training spec, Phase 0): every
LLM call the scan pipeline makes appends one JSONL record to
``<dir>/decisions-<scan_id>.jsonl`` so the decisions the pipeline
delegates to a model become an inspectable, replayable dataset — BEFORE
any training work starts, the tap must exist.

Mechanism — one chokepoint plus optional richness:

* :class:`faultline.llm.cost.CostTracker` records every LLM call in the
  pipeline (that is the standing cost law); its ``record()`` now forwards
  each call here (``kind="llm_call"``) with the stage marker from
  ``faultline.llm.stage_context``, the role (the cost label), model and
  token counts. Call sites that have the prompt in scope pass
  ``decision_meta`` with an ``input_digest_hash`` (sha256 of the prompt
  payload — NEVER the payload itself).
* Stages whose calls ARE membership/naming decisions (6.7d Call 1 / Call
  2 — the RC1 oracle the training spec targets) additionally append a
  parsed ``kind="decision"`` record via :func:`log_decision` with the
  candidate set and the decision (names/labels only).

Privacy contract: no secrets, no env, no repo content beyond names,
paths and labels; prompts are logged as HASHES only. Cache-replayed
stages make no LLM call and log nothing (the original live call was
logged when it happened).

Scope: a record is written ONLY between :func:`begin_scan` /
:func:`end_scan` — ``run_pipeline_v2`` brackets the scan with them, so
unit tests and ad-hoc ``CostTracker`` use never touch the filesystem.

Env:

* ``FAULTLINE_DECISION_LOG`` — default ON; ``0``/``false`` disables.
* ``FAULTLINE_DECISION_LOG_DIR`` — target directory override. Default:
  ``<faultline base>/training`` (i.e. ``~/.faultline/training``, or under
  ``$FAULTLINES_RUN_DIR`` when the worker sets it). Smokes/evals point
  this into their scratch dir.

Thread-safe (LLM stages fan out over ThreadPoolExecutor — same
module-global + lock pattern as ``stage_context``). Best-effort by
contract: any I/O fault disables logging for the rest of the scan and
never raises into the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from faultline.llm.stage_context import current_stage

__all__ = [
    "DECISION_LOG_ENV",
    "DECISION_LOG_DIR_ENV",
    "decision_log_enabled",
    "decision_log_dir",
    "begin_scan",
    "end_scan",
    "current_scan_id",
    "digest_hash",
    "log_llm_call",
    "log_decision",
]

DECISION_LOG_ENV = "FAULTLINE_DECISION_LOG"
DECISION_LOG_DIR_ENV = "FAULTLINE_DECISION_LOG_DIR"

_LOCK = threading.Lock()
_SCAN: dict[str, Any] = {"id": None, "path": None, "failed": False}

#: Bound on serialized candidates/decision payloads per record — the tap
#: stores decisions (name→label maps, candidate name lists), never bulk
#: content; a pathological payload is truncated to a summary marker.
_MAX_FIELD_BYTES = 65536


def decision_log_enabled() -> bool:
    """Default ON; ``FAULTLINE_DECISION_LOG=0`` disables."""
    return os.environ.get(DECISION_LOG_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def decision_log_dir() -> Path:
    """Target directory (created lazily on first write).

    ``FAULTLINE_DECISION_LOG_DIR`` wins; else ``<faultline base>/training``
    — which itself respects ``$FAULTLINES_RUN_DIR`` (worker job dirs), so
    hosted scans never write to a shared home dir.
    """
    override = os.environ.get(DECISION_LOG_DIR_ENV, "").strip()
    if override:
        return Path(override)
    from faultline.cache.paths import faultline_base_dir

    return faultline_base_dir() / "training"


def begin_scan(scan_id: str) -> None:
    """Open the decision log for *scan_id* (called by ``run_pipeline_v2``).

    Never raises; a bad id degrades to disabled logging.
    """
    safe = "".join(
        ch for ch in str(scan_id or "") if ch.isalnum() or ch in "._-"
    )
    with _LOCK:
        _SCAN["id"] = safe or None
        _SCAN["path"] = None
        _SCAN["failed"] = False


def end_scan() -> None:
    """Close the scan bracket — subsequent calls stop logging."""
    with _LOCK:
        _SCAN["id"] = None
        _SCAN["path"] = None
        _SCAN["failed"] = False


def current_scan_id() -> str | None:
    with _LOCK:
        return _SCAN["id"]


def digest_hash(*parts: str | None) -> str:
    """sha256 over the given prompt parts — the ONLY form a prompt may
    take in the log (privacy contract)."""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def _bounded(value: Any) -> Any:
    """JSON-encodable, size-bounded copy of a candidates/decision field."""
    try:
        blob = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"unserializable": str(type(value).__name__)}
    if len(blob.encode("utf-8", "replace")) > _MAX_FIELD_BYTES:
        return {"truncated": True, "bytes": len(blob)}
    return json.loads(blob)


def _append(entry: dict[str, Any]) -> None:
    """Best-effort JSONL append; the first fault disables the scan's log."""
    with _LOCK:
        scan_id = _SCAN["id"]
        if scan_id is None or _SCAN["failed"]:
            return
        path: Path | None = _SCAN["path"]
        if path is None:
            try:
                d = decision_log_dir()
                d.mkdir(parents=True, exist_ok=True)
                path = d / f"decisions-{scan_id}.jsonl"
                _SCAN["path"] = path
            except OSError:
                _SCAN["failed"] = True
                return
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str))
                fh.write("\n")
        except OSError:
            _SCAN["failed"] = True


def _base_entry(kind: str, role: str | None, model: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "scan_id": current_scan_id(),
        "role": role or "unlabeled",
        "model": model,
    }
    stage = current_stage()
    if stage:
        entry["stage"] = str(stage.get("name", ""))
        entry["stage_num"] = stage.get("num")
    return entry


def log_llm_call(
    *,
    model: str,
    label: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    decision_meta: Mapping[str, Any] | None = None,
) -> None:
    """One record per LLM call — wired from ``CostTracker.record``.

    ``decision_meta`` optional keys: ``role`` (overrides the label),
    ``input_digest_hash``, ``candidates``, ``decision``.
    """
    if not decision_log_enabled() or current_scan_id() is None:
        return
    meta = decision_meta or {}
    entry = _base_entry("llm_call", str(meta.get("role") or label), model)
    entry["input_tokens"] = input_tokens
    entry["output_tokens"] = output_tokens
    if meta.get("input_digest_hash"):
        entry["input_digest_hash"] = str(meta["input_digest_hash"])
    if meta.get("candidates") is not None:
        entry["candidates"] = _bounded(meta["candidates"])
    if meta.get("decision") is not None:
        entry["decision"] = _bounded(meta["decision"])
    _append(entry)


def log_decision(
    *,
    role: str,
    decision: Any,
    model: str | None = None,
    candidates: Any = None,
    input_digest_hash: str | None = None,
) -> None:
    """One PARSED decision record (kind="decision") — the rich tap for
    stages whose LLM output moves membership/naming (6.7d Call 1/2)."""
    if not decision_log_enabled() or current_scan_id() is None:
        return
    entry = _base_entry("decision", role, model)
    if input_digest_hash:
        entry["input_digest_hash"] = input_digest_hash
    if candidates is not None:
        entry["candidates"] = _bounded(candidates)
    entry["decision"] = _bounded(decision)
    _append(entry)
