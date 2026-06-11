"""RailsViewsExtractor — walk ``app/views/<resource>/**``.

Rails view convention: each resource gets its own directory under
``app/views/``. The directory NAME is the (plural) resource noun;
files inside (``index.html.erb``, ``show.html.erb``, ``_form.html.erb``)
are views/partials of that resource.

We emit one anchor per resource directory whose slug is the directory
name. The anchor's path list is every view file under that directory.

Layout templates (``app/views/layouts/``) and the shared partials
directory (``app/views/shared/``, ``app/views/application/``) are NOT
resources — they're support templates — so we skip them per the
``skip_dirs`` list in ``rails-app.yaml``.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._rails import RailsPatternExtractor
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


_DEFAULT_BASE = "app/views"
_DEFAULT_EXTS: tuple[str, ...] = (".erb", ".haml", ".slim", ".html.erb")
_DEFAULT_SKIP: tuple[str, ...] = ("layouts", "shared", "application")


class _Compiled:
    """Scalar config for the ``views`` section (no regexes needed)."""

    __slots__ = ("base", "extensions", "skip_dirs", "confidence")

    def __init__(self, config: dict) -> None:
        cfg = config.get("views") or {}
        if not isinstance(cfg, dict):
            cfg = {}

        self.base = (cfg.get("base_dir") or _DEFAULT_BASE).rstrip("/") + "/"
        extensions_raw = cfg.get("extensions") or list(_DEFAULT_EXTS)
        self.extensions = tuple(
            e if isinstance(e, str) and e.startswith(".") else f".{e}"
            for e in extensions_raw
            if isinstance(e, str)
        )
        if not self.extensions:
            self.extensions = _DEFAULT_EXTS
        self.skip_dirs = tuple(
            s for s in (cfg.get("skip_dirs") or list(_DEFAULT_SKIP))
            if isinstance(s, str)
        )
        self.confidence = float(cfg.get("confidence") or 0.80)


class RailsViewsExtractor(RailsPatternExtractor):
    """View directory → feature anchor (one anchor per resource folder)."""

    name = "rails-views"

    def compile_patterns(self, config: dict) -> _Compiled:
        return _Compiled(config)

    def collect(
        self, ctx: "ScanContext", compiled: _Compiled,
    ) -> dict[str, list[str]]:
        # Group: resource_dir_slug → list of file paths under it.
        buckets: dict[str, list[str]] = defaultdict(list)

        for raw in ctx.tracked_files:
            p = posix(raw)
            if not p.startswith(compiled.base):
                continue
            rest = p[len(compiled.base):]
            if "/" not in rest:
                # Files directly in app/views/ — not a resource folder.
                continue
            resource = rest.split("/", 1)[0]
            if resource in compiled.skip_dirs:
                continue
            # Require the file extension to match.
            if not any(p.endswith(e) for e in compiled.extensions):
                continue
            slug = slugify(resource)
            if not slug or is_noise(slug):
                continue
            buckets[slug].append(p)
        return buckets

    def emit(
        self,
        ctx: "ScanContext",
        key: str,
        bucket: list[str],
        compiled: _Compiled,
    ) -> AnchorCandidate:
        unique_paths = tuple(sorted(set(bucket)))
        return AnchorCandidate(
            name=key,
            paths=unique_paths,
            source=self.name,
            confidence_self=compiled.confidence,
            rationale=(
                f"Rails view directory {key!r} with "
                f"{len(unique_paths)} template file(s)"
            ),
        )


__all__ = ["RailsViewsExtractor"]
