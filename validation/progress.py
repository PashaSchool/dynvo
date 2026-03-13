"""Thread-safe progress tracker with atomic JSON writes."""

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .config import PROGRESS_FILE
from .models import (
    OverallProgress,
    OverallSummary,
    PhaseStatus,
    RepoProgress,
    RepoTarget,
    ValidationResult,
)


class ProgressTracker:
    """Manages pipeline progress, writes atomic JSON updates."""

    def __init__(self, output_path: Path | None = None) -> None:
        self._path = output_path or PROGRESS_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._progress = OverallProgress()
        self._on_update: list = []
        self._flush()

    @property
    def progress(self) -> OverallProgress:
        return self._progress

    def on_update(self, callback) -> None:
        self._on_update.append(callback)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._progress.phase = phase
            self._flush()

    def set_repos(self, repos: list[RepoTarget]) -> None:
        with self._lock:
            self._progress.repos = [RepoProgress(repo=r) for r in repos]
            self._flush()

    def update_repo(
        self,
        repo_name: str,
        *,
        clone_status: PhaseStatus | None = None,
        analyze_status: PhaseStatus | None = None,
        validate_status: PhaseStatus | None = None,
        feature_map_path: str | None = None,
        validation_result: ValidationResult | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            for rp in self._progress.repos:
                if rp.repo.name != repo_name:
                    continue
                if clone_status is not None:
                    rp.clone_status = clone_status
                if analyze_status is not None:
                    rp.analyze_status = analyze_status
                if validate_status is not None:
                    rp.validate_status = validate_status
                if feature_map_path is not None:
                    rp.feature_map_path = feature_map_path
                if validation_result is not None:
                    rp.validation_result = validation_result
                if error is not None:
                    rp.error = error
                if clone_status == PhaseStatus.running and rp.started_at is None:
                    rp.started_at = datetime.now()
                if validate_status in (PhaseStatus.completed, PhaseStatus.failed):
                    rp.completed_at = datetime.now()
                break
            self._flush()

    def set_summary(self, summary: OverallSummary) -> None:
        with self._lock:
            self._progress.summary = summary
            self._progress.phase = "done"
            self._flush()

    def _flush(self) -> None:
        self._progress.updated_at = datetime.now()
        data = self._progress.model_dump(mode="json")
        # Atomic write: tmp file then rename
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        for cb in self._on_update:
            cb(self._progress)
