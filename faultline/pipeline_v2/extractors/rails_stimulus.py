"""RailsStimulusExtractor — walk ``app/javascript/controllers/*_controller.js``.

Hotwire's Stimulus convention: every JS controller lives in a
predictable directory and follows ``foo_controller.js`` naming with
``extends Controller`` inheritance. We match BOTH the filename
convention AND the inheritance line (loose: either is sufficient,
because some apps export anonymous controllers).

Each matched file yields one anchor whose slug is the filename stem
(``users_controller.js`` → ``users``).

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


_DEFAULT_BASE_DIRS: tuple[str, ...] = (
    "app/javascript/controllers",
    "app/javascript/src/controllers",
)
_DEFAULT_SUFFIX = "_controller.js"


class RailsStimulusExtractor:
    """Stimulus controller files → feature anchors."""

    name = "rails-stimulus"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else load_rails_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not is_rails_app(ctx):
            return []

        cfg = self._config.get("stimulus") or {}
        if not isinstance(cfg, dict):
            cfg = {}

        base_dirs_raw = cfg.get("base_dirs") or list(_DEFAULT_BASE_DIRS)
        base_dirs = tuple(
            (d.rstrip("/") + "/")
            for d in base_dirs_raw
            if isinstance(d, str)
        )
        if not base_dirs:
            base_dirs = tuple(d + "/" for d in _DEFAULT_BASE_DIRS)

        suffix = cfg.get("filename_suffix") or _DEFAULT_SUFFIX
        if not isinstance(suffix, str):
            suffix = _DEFAULT_SUFFIX

        content_re_raw = cfg.get("content_pattern")
        content_re: re.Pattern[str] | None = None
        if isinstance(content_re_raw, str):
            try:
                content_re = re.compile(content_re_raw)
            except re.error:
                content_re = None

        confidence = float(cfg.get("confidence") or 0.80)

        # slug → set of paths
        buckets: dict[str, set[str]] = {}

        for raw in ctx.tracked_files:
            p = posix(raw)
            if not any(p.startswith(b) for b in base_dirs):
                continue
            basename = p.rsplit("/", 1)[-1]
            if not basename.endswith(suffix):
                # Allow content-pattern fallback: a controller file
                # named non-conventionally is still admissible if it
                # contains ``extends Controller``.
                if content_re is None:
                    continue
                text = read_text(ctx.repo_path / p)
                if not text or not content_re.search(text):
                    continue
                # Use the basename minus .js as stem.
                stem = basename[:-3] if basename.endswith(".js") else basename
            else:
                stem = basename[: -len(suffix)]

            slug = slugify(stem)
            if not slug or is_noise(slug):
                continue
            buckets.setdefault(slug, set()).add(p)

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
                        f"Stimulus controller {slug!r} "
                        f"from {len(paths)} file(s)"
                    ),
                ),
            )
        return out


__all__ = ["RailsStimulusExtractor"]
