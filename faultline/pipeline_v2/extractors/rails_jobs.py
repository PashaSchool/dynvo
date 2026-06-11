"""RailsJobsExtractor — walk ``app/jobs/**/*.rb``.

Each background job class (``class Foo < ApplicationJob`` or
``< ActiveJob::Base`` or ``< Sidekiq::Job``) yields one anchor whose
slug is the job classname (with the ``Job`` suffix stripped for
readability — ``WelcomeEmailJob`` → ``welcome-email``).

No LLM. No network. Read-only.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._rails import RailsPatternExtractor
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


_JOB_PREFIX = "app/jobs/"


def _strip_job_suffix(name: str) -> str:
    """Drop a trailing ``Job`` or ``Worker`` from the class name."""
    for suf in ("Job", "Worker"):
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


class _Compiled:
    """Compiled class pattern + confidence for the ``jobs`` section."""

    __slots__ = ("class_re", "confidence")

    def __init__(self, config: dict) -> None:
        cfg = config.get("jobs") or {}
        if not isinstance(cfg, dict):
            cfg = {}

        self.class_re: re.Pattern[str] | None = None
        class_re_raw = cfg.get("class_pattern")
        if isinstance(class_re_raw, str):
            try:
                self.class_re = re.compile(class_re_raw)
            except re.error as exc:
                logger.warning("rails_jobs: bad class regex: %s", exc)

        self.confidence = float(cfg.get("confidence") or 0.85)


class RailsJobsExtractor(RailsPatternExtractor):
    """ActiveJob / Sidekiq class files → feature anchors."""

    name = "rails-jobs"

    def compile_patterns(self, config: dict) -> _Compiled:
        return _Compiled(config)

    def collect(
        self, ctx: "ScanContext", compiled: _Compiled,
    ) -> dict[str, set[str]]:
        class_re = compiled.class_re
        if class_re is None:
            return {}

        files = [
            posix(f) for f in ctx.tracked_files
            if posix(f).startswith(_JOB_PREFIX) and f.endswith(".rb")
        ]
        # ApplicationJob is the base class — not a feature.
        files = [f for f in files if not f.endswith("/application_job.rb")]

        # slug → set of paths
        buckets: dict[str, set[str]] = {}

        for path in files:
            text = read_text(ctx.repo_path / path)
            if not text:
                continue
            class_matches = class_re.findall(text)
            if not class_matches:
                continue
            for classname in class_matches:
                trimmed = _strip_job_suffix(classname)
                slug = slugify(trimmed)
                if not slug or is_noise(slug):
                    continue
                buckets.setdefault(slug, set()).add(path)
        return buckets

    def emit(
        self,
        ctx: "ScanContext",
        key: str,
        bucket: set[str],
        compiled: _Compiled,
    ) -> AnchorCandidate | None:
        paths = tuple(sorted(bucket))
        if not paths:
            return None
        return AnchorCandidate(
            name=key,
            paths=paths,
            source=self.name,
            confidence_self=compiled.confidence,
            rationale=(
                f"Rails background job {key!r} "
                f"from {len(paths)} file(s)"
            ),
        )


__all__ = ["RailsJobsExtractor"]
