"""Scan-time diagnostic artifacts (Sprint 9d).

Write per-stage JSON snapshots so a future scan or investigator can
trace exactly what happened at each pipeline step. Layout:

    <repo_root>/.faultline/logs/<slug>/
        00-stage-extractors.json
        01-stage-primary-scan.json
        02-stage-feature-protection.json
        03-stage-critique-input.json
        03-stage-critique-output.json
        03-stage-critique-raw-llm.json
        04-stage-flow-attribution-critique.json
        05-stage-auto-split.json
        06-stage-display-canonicalizer.json
        99-feature-map-final.json     ← same JSON we ship to landing
        errors.json                   ← chronological warning/error log

Rotation on each new scan:

    if .faultline/logs_prev/<slug>/ exists → rm -rf
    if .faultline/logs/<slug>/ exists      → move to logs_prev/<slug>/
    create empty .faultline/logs/<slug>/

Disable with env var ``FAULTLINE_NO_ARTIFACTS=1``.

Per ``rule-cold-scan``: artifacts are an OUTPUT of the scan, not an
input. The next scan never reads them — they exist purely for
post-mortem analysis and AI agents performing diagnostic queries.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Stage ordering — used as filename prefix for natural sorting in `ls`.
# Linked-list shape: each stage records the previous and next stage so
# the JSON files can be reconstructed into one chronological narrative.
_STAGE_ORDER: dict[str, str] = {
    "scan-init":               "00",
    "extractors":              "01",
    "primary-scan":            "02",
    "feature-protection":      "03",
    "critique-input":          "04a",
    "critique-raw-llm":        "04b",
    "critique-output":         "04c",
    "feature-dedup":           "04d",
    "flow-attribution-critique-input":  "05a",
    "flow-attribution-critique-output": "05b",
    "auto-split":              "06",
    "display-canonicalizer":   "07",
    "post-process":            "08",
    "feature-map-final":       "99",
}

_STAGE_SEQUENCE = [
    "scan-init", "extractors", "primary-scan", "feature-protection",
    "critique-input", "critique-raw-llm", "critique-output",
    "feature-dedup",
    "flow-attribution-critique-input", "flow-attribution-critique-output",
    "auto-split", "display-canonicalizer", "post-process",
    "feature-map-final",
]


def _adjacent_stages(name: str) -> tuple[str | None, str | None]:
    if name not in _STAGE_SEQUENCE:
        return None, None
    i = _STAGE_SEQUENCE.index(name)
    prev_ = _STAGE_SEQUENCE[i - 1] if i > 0 else None
    next_ = _STAGE_SEQUENCE[i + 1] if i + 1 < len(_STAGE_SEQUENCE) else None
    return prev_, next_


def artifacts_enabled() -> bool:
    """True unless the user set ``FAULTLINE_NO_ARTIFACTS=1``."""
    return os.environ.get("FAULTLINE_NO_ARTIFACTS", "").lower() not in {
        "1", "true", "yes", "on",
    }


def _logs_root(repo_root: Path) -> Path:
    return Path(repo_root) / ".faultline" / "logs"


def _logs_prev_root(repo_root: Path) -> Path:
    return Path(repo_root) / ".faultline" / "logs_prev"


def _slug_dir(repo_root: Path, slug: str) -> Path:
    return _logs_root(repo_root) / slug


def _slug_dir_prev(repo_root: Path, slug: str) -> Path:
    return _logs_prev_root(repo_root) / slug


def init_scan(repo_root: Path | str, slug: str) -> Path:
    """Rotate logs (logs/<slug> → logs_prev/<slug>) and prepare a
    fresh empty ``logs/<slug>/`` directory. Returns the fresh dir.

    Idempotent — safe to call from any pipeline entry point.
    """
    if not artifacts_enabled():
        return Path("/dev/null")
    repo = Path(repo_root)
    cur = _slug_dir(repo, slug)
    prev = _slug_dir_prev(repo, slug)
    try:
        if prev.exists():
            shutil.rmtree(prev, ignore_errors=True)
        if cur.exists():
            prev.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cur), str(prev))
        cur.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("artifacts: failed to init scan logs (%s)", exc)
        return Path("/dev/null")
    return cur


# ── ArtifactsLogger ──────────────────────────────────────────────────


@dataclass
class ArtifactsLogger:
    """Per-scan logger that writes one JSON file per stage.

    Construct via ``get_logger(repo_root, slug)``. Use ``stage()``
    to write a snapshot. Errors are appended to ``errors.json`` for
    after-the-fact investigation.
    """

    repo_root: Path
    slug: str
    log_dir: Path
    enabled: bool = True
    errors: list[dict] = field(default_factory=list)
    # Sprint 9d — single ID for one entire scan; threaded into every
    # artifact so they form a verifiable causal chain even if files
    # get reorganised post-hoc.
    scan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Maps stage_name → counts dict (e.g. feature_count_in/out, flow
    # counts, items added/dropped). Helps diff stages.
    _stage_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def stage(self, name: str, payload: Any, *, counts: dict[str, int] | None = None) -> None:
        """Write ``payload`` to ``<log_dir>/<order>-stage-<name>.json``.

        ``counts``: optional dict like ``{"feature_count_in": 27,
        "feature_count_out": 19}`` that becomes part of the linked-list
        chain so a regression-hunter can immediately see "this stage
        dropped 8 features" without parsing the payload.
        """
        if not self.enabled:
            return
        order = _STAGE_ORDER.get(name, "_")
        target = self.log_dir / f"{order}-stage-{name}.json"
        prev_stage, next_stage = _adjacent_stages(name)
        if counts:
            self._stage_counts[name] = counts
        wrapper = {
            "stage": name,
            "scan_id": self.scan_id,
            "slug": self.slug,
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
            "previous_stage": prev_stage,
            "next_stage": next_stage,
            "counts": counts or {},
            "data": payload,
        }
        try:
            target.write_text(
                json.dumps(wrapper, indent=2, default=_json_default,
                           ensure_ascii=False),
            )
        except (OSError, TypeError, ValueError) as exc:
            self.error(f"failed to write {name}.json", str(exc))

    def final_feature_map(self, feature_map_dict: dict) -> None:
        """Write the same JSON the engine ships to the landing — but
        wrapped with the scan_id + a per-stage chain summary so the
        single file is enough to reconstruct the whole causal story
        even if intermediate stage files are missing.
        """
        if not self.enabled:
            return
        target = self.log_dir / "99-feature-map-final.json"
        wrapper = {
            "scan_id": self.scan_id,
            "slug": self.slug,
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
            "stage_chain": _STAGE_SEQUENCE,
            "stage_counts_chain": [
                {"stage": s, "counts": self._stage_counts.get(s, {})}
                for s in _STAGE_SEQUENCE
                if s in self._stage_counts
            ],
            "errors_count": len(self.errors),
            "feature_map": feature_map_dict,
        }
        try:
            target.write_text(
                json.dumps(wrapper, indent=2, default=_json_default,
                           ensure_ascii=False),
            )
        except (OSError, TypeError, ValueError) as exc:
            self.error("failed to write final feature-map", str(exc))

    def error(self, summary: str, detail: str = "") -> None:
        """Append an error entry to errors.json. Best-effort."""
        if not self.enabled:
            return
        entry = {
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
            "summary": summary,
            "detail": detail,
        }
        self.errors.append(entry)
        try:
            (self.log_dir / "errors.json").write_text(
                json.dumps(self.errors, indent=2, ensure_ascii=False),
            )
        except OSError:
            pass


def _json_default(o: Any) -> Any:
    """Coerce common non-JSON-native types to safe representations."""
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, set):
        return sorted(o)
    if hasattr(o, "model_dump"):  # Pydantic v2
        return o.model_dump(mode="json")
    if hasattr(o, "dict"):        # Pydantic v1 / dataclasses-style
        return o.dict()
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items() if not k.startswith("_")}
    return repr(o)


# Module-level cache so independent stages can grab the same logger
# without threading it through every function signature.
_LOGGERS: dict[tuple[str, str], ArtifactsLogger] = {}


def get_logger(repo_root: Path | str, slug: str) -> ArtifactsLogger:
    """Return (or lazily create) the logger for this scan.

    Calling this multiple times during a single scan returns the same
    instance — important because ``init_scan`` should run exactly
    once at scan start, not on every stage write.
    """
    key = (str(repo_root), slug)
    if key in _LOGGERS:
        return _LOGGERS[key]
    log_dir = _slug_dir(Path(repo_root), slug)
    enabled = artifacts_enabled()
    if enabled and not log_dir.exists():
        # Caller forgot to call init_scan — be permissive and create
        # the directory rather than swallow the writes silently.
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            enabled = False
    inst = ArtifactsLogger(
        repo_root=Path(repo_root), slug=slug,
        log_dir=log_dir if enabled else Path("/dev/null"),
        enabled=enabled,
    )
    _LOGGERS[key] = inst
    return inst


@contextmanager
def stage_block(logger_inst: ArtifactsLogger, name: str):
    """Context-manager helper for fenced-stage writes — captures
    exceptions and records them to errors.json without re-raising.
    Use only when you can swallow the failure; otherwise use
    ``try/except`` directly + ``logger.error``.

    Usage::

        with stage_block(art, "auto-split") as out:
            out["features_split"] = stats.features_split
            out["new_features"]   = stats.new_features
    """
    payload: dict = {}
    try:
        yield payload
    except Exception as exc:  # noqa: BLE001 — diagnostic aggregator
        logger_inst.error(f"{name} raised", repr(exc))
        raise
    finally:
        logger_inst.stage(name, payload)


__all__ = [
    "ArtifactsLogger",
    "artifacts_enabled",
    "get_logger",
    "init_scan",
    "stage_block",
]
