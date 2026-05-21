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

from faultline.pipeline_v2.extractors._rails import (
    is_rails_app,
    load_rails_config,
)
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


class RailsViewsExtractor:
    """View directory → feature anchor (one anchor per resource folder)."""

    name = "rails-views"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else load_rails_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not is_rails_app(ctx):
            return []

        cfg = self._config.get("views") or {}
        if not isinstance(cfg, dict):
            cfg = {}

        base = (cfg.get("base_dir") or _DEFAULT_BASE).rstrip("/") + "/"
        extensions_raw = cfg.get("extensions") or list(_DEFAULT_EXTS)
        extensions = tuple(
            e if isinstance(e, str) and e.startswith(".") else f".{e}"
            for e in extensions_raw
            if isinstance(e, str)
        )
        if not extensions:
            extensions = _DEFAULT_EXTS
        skip_dirs = tuple(
            s for s in (cfg.get("skip_dirs") or list(_DEFAULT_SKIP))
            if isinstance(s, str)
        )
        confidence = float(cfg.get("confidence") or 0.80)

        # Group: resource_dir_slug → list of file paths under it.
        buckets: dict[str, list[str]] = defaultdict(list)

        for raw in ctx.tracked_files:
            p = posix(raw)
            if not p.startswith(base):
                continue
            rest = p[len(base):]
            if "/" not in rest:
                # Files directly in app/views/ — not a resource folder.
                continue
            resource = rest.split("/", 1)[0]
            if resource in skip_dirs:
                continue
            # Require the file extension to match.
            if not any(p.endswith(e) for e in extensions):
                continue
            slug = slugify(resource)
            if not slug or is_noise(slug):
                continue
            buckets[slug].append(p)

        out: list[AnchorCandidate] = []
        for slug, paths in buckets.items():
            unique_paths = tuple(sorted(set(paths)))
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=unique_paths,
                    source=self.name,
                    confidence_self=confidence,
                    rationale=(
                        f"Rails view directory {slug!r} with "
                        f"{len(unique_paths)} template file(s)"
                    ),
                ),
            )
        return out


__all__ = ["RailsViewsExtractor"]
