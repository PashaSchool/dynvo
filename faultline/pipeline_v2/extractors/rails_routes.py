"""RailsRoutesExtractor — parse ``config/routes.rb`` for declared routes.

Rails route declarations are the most authoritative feature signal in
a Rails app: every customer-facing URL transit passes through this
file. We use a regex-based reader (NOT a Ruby AST) because we cannot
assume the user has a Ruby toolchain installed, and the four idioms
below cover the vast majority of real-world routes.rb files.

Patterns are loaded from ``eval/stacks/rails-app.yaml``; this module
just iterates them and emits anchors. Activation is gated by
:func:`is_rails_app` — no work happens on non-Rails repos.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
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


def _compile_patterns(cfg_patterns: dict) -> dict[str, re.Pattern[str]]:
    """Compile YAML-declared regex patterns. Bad patterns are dropped
    with a warning so a typo in YAML never kills the extractor."""
    out: dict[str, re.Pattern[str]] = {}
    if not isinstance(cfg_patterns, dict):
        return out
    for key, raw in cfg_patterns.items():
        if not isinstance(raw, str):
            continue
        try:
            out[key] = re.compile(raw)
        except re.error as exc:
            logger.warning(
                "rails_routes: bad regex for %s (%s): %s", key, raw, exc,
            )
    return out


class _Compiled:
    """Compiled patterns + scalar config for one rails-app.yaml dict."""

    __slots__ = ("route_file", "patterns", "confidence")

    def __init__(self, config: dict) -> None:
        routes_cfg = config.get("routes") or {}
        if not isinstance(routes_cfg, dict):
            routes_cfg = {}
            self.patterns: dict[str, re.Pattern[str]] = {}
        else:
            self.patterns = _compile_patterns(routes_cfg.get("patterns") or {})
        self.route_file = routes_cfg.get("file") or "config/routes.rb"
        self.confidence = float(routes_cfg.get("confidence") or 0.95)


class RailsRoutesExtractor(RailsPatternExtractor):
    """Parse ``config/routes.rb`` → one anchor per declared resource/path."""

    name = "rails-routes"

    def compile_patterns(self, config: dict) -> _Compiled:
        return _Compiled(config)

    def collect(
        self, ctx: "ScanContext", compiled: _Compiled,
    ) -> dict[str, set[str]]:
        if not compiled.patterns:
            return {}

        full_path = ctx.repo_path / compiled.route_file
        text = read_text(full_path)
        if not text:
            return {}

        # Bucket: slug → set of pattern keys that fired (for rationale).
        buckets: dict[str, set[str]] = defaultdict(set)

        for line in text.splitlines():
            # Skip Ruby comments.
            stripped = line.split("#", 1)[0]
            if not stripped.strip():
                continue
            for key, pattern in compiled.patterns.items():
                for m in pattern.finditer(stripped):
                    if not m.groups():
                        continue
                    raw = m.group(1)
                    if not raw:
                        continue
                    # For paths like "/admin/users" take the FIRST
                    # segment after the leading slash — that's the
                    # feature root.
                    head = raw.lstrip("/").split("/", 1)[0]
                    slug = slugify(head)
                    if not slug or is_noise(slug):
                        continue
                    buckets[slug].add(key)
        return buckets

    def emit(
        self,
        ctx: "ScanContext",
        key: str,
        bucket: set[str],
        compiled: _Compiled,
    ) -> AnchorCandidate:
        route_file = compiled.route_file
        return AnchorCandidate(
            name=key,
            paths=(posix(route_file),),
            source=self.name,
            confidence_self=compiled.confidence,
            rationale=(
                f"Rails route {key!r} declared in {route_file} "
                f"via {sorted(bucket)}"
            ),
        )


__all__ = ["RailsRoutesExtractor"]
