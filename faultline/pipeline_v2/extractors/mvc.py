"""MVCControllerExtractor — recognises traditional MVC controllers.

Per the ``mvc-controller-extractor`` skill, the pattern recurs across:

  - Rails    : ``app/controllers/*_controller.rb``
  - Laravel  : ``app/Http/Controllers/*.php`` (``*Controller.php``)
  - Phoenix  : ``lib/*_web/controllers/*_controller.ex``
  - Spring   : Java files with ``@Controller`` / ``@RestController``
  - ASP.NET  : ``*Controller.cs``
  - Django CBV : ``views.py`` classes that inherit from ``View``

Convention is filename- or annotation-based (not file-system routing
like :mod:`route`). Each controller file → one anchor whose slug is
the controller's resource noun (``UsersController`` → ``users``).

We avoid scanning file *contents* for annotation languages (Spring,
Django CBV) in this first pass — that requires a parser. The filename
convention is universal enough across Rails/Laravel/Phoenix/ASP.NET to
ship now. A future revision can layer in content-scanning when the
``stack-pattern-library`` YAML grows annotation patterns.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# (suffix, strip_suffix) — file matches if its basename ends with
# ``suffix``. ``strip_suffix`` is removed from the basename to produce
# the slug source. The table lives in ``eval/stacks/mvc-controllers.yaml``
# (runtime mirror packaged under ``faultline/pipeline_v2/data/stacks/``);
# order matters — the FIRST suffix that matches wins.
_PATTERNS_CACHE: tuple[tuple[str, str], ...] | None = None


def _load_controller_patterns() -> tuple[tuple[str, str], ...]:
    """Parse mvc-controllers.yaml once into the historical tuple shape.

    Hermetic: resolves via ``importlib.resources`` (see
    ``faultline.pipeline_v2.data``).
    """
    global _PATTERNS_CACHE
    if _PATTERNS_CACHE is not None:
        return _PATTERNS_CACHE

    config = load_stack_yaml("mvc-controllers")
    out: list[tuple[str, str]] = []
    for entry in config.get("controller_patterns") or []:
        if not isinstance(entry, dict):
            continue
        suffix = entry.get("suffix")
        strip = entry.get("strip")
        if isinstance(suffix, str) and isinstance(strip, str) and suffix:
            out.append((suffix, strip))
    _PATTERNS_CACHE = tuple(out)
    return _PATTERNS_CACHE


def _controller_slug_from(basename: str) -> str | None:
    """Apply each known suffix; return the stripped slug or ``None``."""
    for suf, strip in _load_controller_patterns():
        if basename.endswith(suf):
            stem = basename[: -len(strip)]
            if not stem or is_noise(stem):
                return None
            return slugify(stem)
    return None


class MVCControllerExtractor:
    """Filename-convention MVC controllers → anchors."""

    name = "mvc"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        files = list(ctx.tracked_files)
        buckets: dict[str, list[str]] = defaultdict(list)

        for raw in files:
            p = posix(raw)
            basename = p.rsplit("/", 1)[-1]
            slug = _controller_slug_from(basename)
            if slug is None:
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
                    # Controllers are explicit author intent; high
                    # baseline confidence with mild evidence boost.
                    confidence_self=min(0.7 + 0.05 * len(unique_paths), 0.95),
                    rationale=f"MVC controller {slug!r} "
                              f"from {len(unique_paths)} controller file(s)",
                ),
            )
        return out


__all__ = ["MVCControllerExtractor"]
