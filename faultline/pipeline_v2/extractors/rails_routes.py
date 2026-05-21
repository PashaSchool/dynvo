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


class RailsRoutesExtractor:
    """Parse ``config/routes.rb`` → one anchor per declared resource/path."""

    name = "rails-routes"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else load_rails_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not is_rails_app(ctx):
            return []

        routes_cfg = self._config.get("routes") or {}
        if not isinstance(routes_cfg, dict):
            return []

        route_file = routes_cfg.get("file") or "config/routes.rb"
        patterns = _compile_patterns(routes_cfg.get("patterns") or {})
        confidence = float(routes_cfg.get("confidence") or 0.95)

        if not patterns:
            return []

        full_path = ctx.repo_path / route_file
        text = read_text(full_path)
        if not text:
            return []

        # Bucket: slug → set of pattern keys that fired (for rationale).
        buckets: dict[str, set[str]] = defaultdict(set)

        for line in text.splitlines():
            # Skip Ruby comments.
            stripped = line.split("#", 1)[0]
            if not stripped.strip():
                continue
            for key, pattern in patterns.items():
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

        out: list[AnchorCandidate] = []
        route_file_posix = posix(route_file)
        for slug, keys in buckets.items():
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=(route_file_posix,),
                    source=self.name,
                    confidence_self=confidence,
                    rationale=(
                        f"Rails route {slug!r} declared in {route_file} "
                        f"via {sorted(keys)}"
                    ),
                ),
            )
        return out


__all__ = ["RailsRoutesExtractor"]
