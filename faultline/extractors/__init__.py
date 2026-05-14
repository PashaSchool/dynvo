"""Composable signal extractors.

Per docs/ROADMAP-90-RECALL.md (faultlines-app repo). Each module under
``faultline.extractors`` is a self-contained Extractor implementation
(satisfying the ``Extractor`` Protocol from ``faultline.protocols``).
Extractors do NOT import each other — they communicate exclusively via
``Signal`` objects through the orchestrator.

Phase 3a (initial): only ``dependency_context`` lives here. Phase 2+
migrates bucketizer, import_graph, symbol_graph, llm_features, etc.,
each becoming its own module.
"""

from faultline.extractors.dependency_context import (
    DependencyContextInjector,
    build_dependency_context_block,
)

__all__ = ["DependencyContextInjector", "build_dependency_context_block"]
