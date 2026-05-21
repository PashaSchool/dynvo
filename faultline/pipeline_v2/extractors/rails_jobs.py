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

from faultline.pipeline_v2.extractors._rails import (
    is_rails_app,
    load_rails_config,
)
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


class RailsJobsExtractor:
    """ActiveJob / Sidekiq class files → feature anchors."""

    name = "rails-jobs"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else load_rails_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not is_rails_app(ctx):
            return []

        cfg = self._config.get("jobs") or {}
        if not isinstance(cfg, dict):
            return []

        class_re_raw = cfg.get("class_pattern")
        if not isinstance(class_re_raw, str):
            return []
        try:
            class_re = re.compile(class_re_raw)
        except re.error as exc:
            logger.warning("rails_jobs: bad class regex: %s", exc)
            return []

        confidence = float(cfg.get("confidence") or 0.85)

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

        out: list[AnchorCandidate] = []
        for slug, paths_set in buckets.items():
            paths = tuple(sorted(paths_set))
            if not paths:
                continue
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=self.name,
                    confidence_self=confidence,
                    rationale=(
                        f"Rails background job {slug!r} "
                        f"from {len(paths)} file(s)"
                    ),
                ),
            )
        return out


__all__ = ["RailsJobsExtractor"]
