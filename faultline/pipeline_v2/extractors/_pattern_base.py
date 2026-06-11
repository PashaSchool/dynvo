"""PatternExtractor — shared scaffold for regex-driven Stage 1 extractors.

About ten Stage 1 extractors follow the exact same shape:

    activation gate → load YAML config → compile regex patterns →
    walk tracked files → match → slugify / noise-filter → bucket →
    emit one AnchorCandidate per bucket

Each used to hand-roll that scaffold with its own pattern-cache idiom
(``_Compiled`` class keyed by ``id(config)``, ``_compiled`` tuples,
inline ``re.compile``). This base class extracts ONLY the skeleton —
it is an implementation helper, not a framework. Subclasses still
satisfy the frozen :class:`~faultline.pipeline_v2.extractors.base.AnchorExtractor`
Protocol exactly as before (``name`` attribute + ``extract``); the
orchestrator never sees this class.

Overridable steps
=================

  - :meth:`load_config`      — default config when none injected
                               (typically ``load_stack_yaml(...)``).
  - :meth:`is_active`        — activation gate; ``False`` → ``[]``
                               with no further work.
  - :meth:`compile_patterns` — turn the raw config dict into whatever
                               compiled bundle the extractor needs.
                               Cached per ``(subclass, id(config))``
                               so repeated extracts don't recompile —
                               the same idiom the extractors used
                               individually.
  - :meth:`collect`          — walk the repo, match patterns, return
                               an ordered ``bucket-key → bucket`` dict.
  - :meth:`emit`             — turn one bucket into an
                               :class:`AnchorCandidate` (or ``None``
                               to drop it).

``extract`` wires the steps together and never needs overriding for
extractors that fit the shape; extractors that don't fit (multi-pass
residual emission like django) simply shouldn't subclass this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


class PatternExtractor:
    """Walk-match-bucket-emit skeleton. See module docstring."""

    #: source slug — subclasses MUST set this (Protocol requirement).
    name: str = ""

    # Compiled-pattern cache shared across instances, keyed by
    # (concrete subclass, id(config)). Keying by ``id`` mirrors the
    # historical per-module ``_COMPILED_CACHE`` idiom: tests that
    # inject a literal config dict get their own compile, while the
    # module-level YAML config (a singleton via ``lru_cache``) compiles
    # exactly once per subclass.
    _COMPILED_CACHE: ClassVar[dict[tuple[type, int], Any]] = {}

    def __init__(self, config: dict | None = None) -> None:
        # ``config=None`` → load from YAML. Tests may pass a literal
        # dict to keep the unit hermetic.
        self._config = config if config is not None else self.load_config()

    # ── overridable steps ──────────────────────────────────────────────

    def load_config(self) -> dict:
        """Default config when the constructor received none."""
        return {}

    def is_active(self, ctx: "ScanContext") -> bool:
        """Activation gate. ``False`` short-circuits to ``[]``."""
        return True

    def compile_patterns(self, config: dict) -> Any:
        """Build the compiled bundle (regexes + scalars) for ``config``.

        Called at most once per ``(subclass, id(config))``; the result
        is cached. Return ``None`` when nothing needs compiling.
        """
        return None

    def collect(self, ctx: "ScanContext", compiled: Any) -> dict[str, Any]:
        """Walk + match + bucket. Returns an insertion-ordered mapping
        of bucket key (usually the anchor slug) → bucket payload."""
        raise NotImplementedError

    def emit(
        self, ctx: "ScanContext", key: str, bucket: Any, compiled: Any,
    ) -> AnchorCandidate | None:
        """One bucket → one candidate. Return ``None`` to drop."""
        raise NotImplementedError

    # ── skeleton ───────────────────────────────────────────────────────

    def compiled(self) -> Any:
        """Cached :meth:`compile_patterns` result for this config."""
        cache_key = (type(self), id(self._config))
        cache = PatternExtractor._COMPILED_CACHE
        if cache_key not in cache:
            cache[cache_key] = self.compile_patterns(self._config)
        return cache[cache_key]

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not self.is_active(ctx):
            return []
        compiled = self.compiled()
        out: list[AnchorCandidate] = []
        for key, bucket in self.collect(ctx, compiled).items():
            candidate = self.emit(ctx, key, bucket, compiled)
            if candidate is not None:
                out.append(candidate)
        return out


__all__ = ["PatternExtractor"]
