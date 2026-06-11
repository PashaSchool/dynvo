"""Shared helpers for Rails-app extractors and the Stage 2 Rails merger.

Includes:
  - ``is_rails_app``: activation gate shared by all 5 Rails extractors.
  - ``singularize`` / ``pluralize`` / ``rails_canonical_noun``: minimal
    English inflector covering the common Rails resource conventions.
    NOT a full ``inflection`` library port — Rails has dozens of
    irregular forms; we cover the universally-correct ones and let
    edge cases (octopi / oxen / quizzes) fall through to a no-op.
  - YAML loader cached on first read.

The inflector is intentionally conservative: when we are unsure
whether a token is singular or plural we return it unchanged. Stage 2
treats slugs that singularize identically as the same canonical
resource, so an over-conservative inflector loses merges but never
makes a false merge.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._pattern_base import PatternExtractor
from faultline.pipeline_v2.extractors._util import is_audited_stack

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


_CONFIG_CACHE: dict | None = None


def load_rails_config() -> dict:
    """Read ``rails-app.yaml`` from the packaged data tree once; cache it.

    Hermetic: resolves via ``importlib.resources`` so it works identically
    from the dev repo, an installed wheel, or the Fly worker image.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    _CONFIG_CACHE = load_stack_yaml("rails-app")
    return _CONFIG_CACHE


def is_rails_app(ctx: "ScanContext") -> bool:
    """Activation gate — True iff the Stage 0.5 auditor declared rails-app.

    We do not fall back to heuristics: the auditor has read manifest
    files and structural signals already, and a false positive here
    floods 5 extractors with empty work on non-Rails repos.
    """
    return is_audited_stack(ctx, "rails-app")


class RailsPatternExtractor(PatternExtractor):
    """Shared scaffold for the five rails-app extractors.

    All five gate on :func:`is_rails_app` and read sections of the same
    ``rails-app.yaml``; subclasses only implement ``compile_patterns`` /
    ``collect`` / ``emit``.
    """

    def load_config(self) -> dict:
        return load_rails_config()

    def is_active(self, ctx: "ScanContext") -> bool:
        return is_rails_app(ctx)


# ── Inflector (singular ↔ plural) ─────────────────────────────────────────


# Irregular forms — pairs (singular, plural) that are not rule-derivable.
# Keep the list minimal: things that ACTUALLY appear as Rails resources.
_IRREGULAR: dict[str, str] = {
    "person": "people",
    "child": "children",
    "man": "men",
    "woman": "women",
    "mouse": "mice",
    "goose": "geese",
    "datum": "data",
    "medium": "media",
    "analysis": "analyses",
    "criterion": "criteria",
    "phenomenon": "phenomena",
}
_IRREGULAR_REVERSE: dict[str, str] = {v: k for k, v in _IRREGULAR.items()}

# Words that are the same in singular and plural — leave untouched.
_UNCOUNTABLE: frozenset[str] = frozenset({
    "equipment", "information", "rice", "money", "species",
    "series", "fish", "sheep", "deer", "news", "metadata",
})


def singularize(word: str) -> str:
    """Best-effort English singularizer for Rails resource nouns.

    Rules applied (in order):
      1. lowercase the token
      2. uncountable / empty → return as-is
      3. irregular reverse lookup
      4. ``ies`` → ``y`` (categories → category)
      5. ``sses`` / ``shes`` / ``ches`` / ``xes`` / ``zes`` → strip ``es``
      6. ``ves`` → ``f`` (knives → knife)
      7. final ``s`` (not ``ss``) → drop

    Anything that doesn't match leaves the word unchanged. We do NOT
    invent fancy rules for ``-us``, ``-um``, ``-on`` because Rails
    apps rarely model classical-Latin nouns and a wrong reverse
    would split a feature ("statuses" → "statu" is a worse outcome
    than not merging).
    """
    if not word:
        return word
    w = word.lower()
    if w in _UNCOUNTABLE:
        return w
    if w in _IRREGULAR_REVERSE:
        return _IRREGULAR_REVERSE[w]
    # Rule 4 — `ies` → `y`
    if len(w) > 3 and w.endswith("ies") and w[-4] not in "aeiou":
        return w[:-3] + "y"
    # Rule 5 — `ches`, `shes`, `xes`, `zes`, `sses`
    for suf in ("sses", "shes", "ches", "xes", "zes"):
        if w.endswith(suf) and len(w) > len(suf):
            return w[:-2]  # drop the trailing `es`
    # Rule 6 — `ves` → `f` (knives → knife; leaves → leaf)
    if w.endswith("ves") and len(w) > 3:
        return w[:-3] + "f"
    # Rule 7 — bare `s` (but not `ss`)
    if w.endswith("s") and not w.endswith("ss") and len(w) > 1:
        return w[:-1]
    return w


def pluralize(word: str) -> str:
    """Best-effort English pluralizer. Inverse of :func:`singularize`.

    Used only by tests + downstream label rendering — Stage 2 merging
    uses :func:`singularize` to compute the canonical noun.
    """
    if not word:
        return word
    w = word.lower()
    if w in _UNCOUNTABLE:
        return w
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    # consonant + y → ies
    if len(w) > 1 and w.endswith("y") and w[-2] not in "aeiou":
        return w[:-1] + "ies"
    # sibilants
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"
    # default: append s
    return w + "s"


def rails_canonical_noun(slug: str) -> str:
    """Reduce a kebab-case slug to its canonical Rails resource noun.

    The slug is split on ``-`` and each token is singularized. We then
    re-join with ``-`` so multi-word resources (``project-membership``
    ↔ ``project-memberships``) collapse correctly.

    Stage 2 keys on the result; two slugs collapse iff
    ``rails_canonical_noun(a) == rails_canonical_noun(b)``.

    Also strips known Rails suffixes that arise from filename
    conventions: ``users-controller`` → ``user`` (controller suffix
    dropped before singularization).
    """
    if not slug:
        return slug
    tokens = [t for t in slug.lower().split("-") if t]
    if not tokens:
        return slug
    # Strip trailing rails-noise tokens — `controller`, `job`, `worker`,
    # `mailer`. These appear in slugs derived from filenames and would
    # block the singular-vs-plural match between an MVC controller
    # anchor and a model anchor.
    _TRAILING_NOISE = {"controller", "job", "worker", "mailer", "channel"}
    while len(tokens) > 1 and tokens[-1] in _TRAILING_NOISE:
        tokens.pop()
    # Singularize each remaining token.
    tokens = [singularize(t) for t in tokens]
    return "-".join(tokens)


__all__ = [
    "RailsPatternExtractor",
    "is_rails_app",
    "load_rails_config",
    "singularize",
    "pluralize",
    "rails_canonical_noun",
]
