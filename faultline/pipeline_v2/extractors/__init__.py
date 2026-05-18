"""Stage 1 deterministic anchor extractors.

Each extractor is a class that implements the :class:`AnchorExtractor`
``Protocol`` (see ``base.py``). The orchestrator in
``faultline.pipeline_v2.stage_1_extractors`` runs all registered
extractors in parallel against a :class:`ScanContext` and collects
:class:`AnchorCandidate` lists per source.

Adding a new extractor is a matter of:

  1. Writing a class with ``name: str`` and ``extract(ctx) -> list``.
  2. Registering it under ``[project.entry-points."faultlines.extractors"]``
     in ``pyproject.toml`` (or dropping a module at
     ``~/.faultline/extractors/<custom>.py``).

The existing extractors must remain untouched when a sixth one is added —
the Protocol contract is FROZEN once Stage 1 ships.
"""

from faultline.pipeline_v2.extractors.base import (
    AnchorCandidate,
    AnchorExtractor,
)

__all__ = ["AnchorCandidate", "AnchorExtractor"]
