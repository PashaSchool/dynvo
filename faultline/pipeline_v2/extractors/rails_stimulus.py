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


_DEFAULT_BASE_DIRS: tuple[str, ...] = (
    "app/javascript/controllers",
    "app/javascript/src/controllers",
)
_DEFAULT_SUFFIX = "_controller.js"


class _Compiled:
    """Compiled content pattern + scalars for the ``stimulus`` section."""

    __slots__ = ("base_dirs", "suffix", "content_re", "confidence")

    def __init__(self, config: dict) -> None:
        cfg = config.get("stimulus") or {}
        if not isinstance(cfg, dict):
            cfg = {}

        base_dirs_raw = cfg.get("base_dirs") or list(_DEFAULT_BASE_DIRS)
        self.base_dirs = tuple(
            (d.rstrip("/") + "/")
            for d in base_dirs_raw
            if isinstance(d, str)
        )
        if not self.base_dirs:
            self.base_dirs = tuple(d + "/" for d in _DEFAULT_BASE_DIRS)

        suffix = cfg.get("filename_suffix") or _DEFAULT_SUFFIX
        if not isinstance(suffix, str):
            suffix = _DEFAULT_SUFFIX
        self.suffix = suffix

        content_re_raw = cfg.get("content_pattern")
        self.content_re: re.Pattern[str] | None = None
        if isinstance(content_re_raw, str):
            try:
                self.content_re = re.compile(content_re_raw)
            except re.error:
                self.content_re = None

        self.confidence = float(cfg.get("confidence") or 0.80)


class RailsStimulusExtractor(RailsPatternExtractor):
    """Stimulus controller files → feature anchors."""

    name = "rails-stimulus"

    def compile_patterns(self, config: dict) -> _Compiled:
        return _Compiled(config)

    def collect(
        self, ctx: "ScanContext", compiled: _Compiled,
    ) -> dict[str, set[str]]:
        suffix = compiled.suffix
        content_re = compiled.content_re

        # slug → set of paths
        buckets: dict[str, set[str]] = {}

        for raw in ctx.tracked_files:
            p = posix(raw)
            if not any(p.startswith(b) for b in compiled.base_dirs):
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
                f"Stimulus controller {key!r} "
                f"from {len(paths)} file(s)"
            ),
        )


__all__ = ["RailsStimulusExtractor"]
