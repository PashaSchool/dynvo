"""FAULTLINE_OWNERSHIP_V2 — the single flag gating the four B66-v2 segments.

B66-v2 is an *attribution/extraction* wave: it changes who OWNS which lines
and which python/tRPC entry surfaces are seen — never a journey, never a
member set's membership (Seg A/B are accounting; Seg C/D are extraction).
All four segments read this one flag so an OFF (unset / ``0``) scan is
byte-identical to the B66 (server-api-entries) + B67 (jobs-entries) merged
world.

Segments (each a separate commit, ONE flag):
  * Seg A — module-subtree ownership for entry-mints
    (:mod:`stage_6_97_feature_loc`): a route-anchor dev whose members
    collapsed to ``loc=0`` under primary-owner fan-in recovers exclusive
    owned LOC for the files inside its own module subtree.
  * Seg B — asset/data import-fan-in guard
    (:mod:`stage_6_86_anchored_mint` shared-member pass): a static
    asset/data member (json/svg/lottie/...) reaching a feature ONLY through
    import fan-in neither credits membership nor inflates the file count.
    Genuine shared CODE survives (the documenso ``packages/lib`` anti-case).
  * Seg C — python-module dispatch entry extractor
    (:mod:`extractors.python_dispatch`): registry/handler-map dicts,
    ``entry_points`` (pyproject / setup.cfg), ``__main__`` CLI, celery tasks
    -> ``routes_index`` kind ``py-dispatch``.
  * Seg D — tRPC lazy handler-cache routers
    (:mod:`extractors.server_api_entries` tRPC collector): resolves
    ``UNSTABLE_HANDLER_CACHE`` / ``getHandler`` routers that import
    ``router`` relatively (the ``@trpc/server`` import gate skipped them).

The flag is registered in :data:`scan_result_cache.ENV_OUTPUT_FLAGS`
(append-only, NO ``KEY_SCHEMA`` bump — the bump rides the separate later
flip commit only).
"""

from __future__ import annotations

import os

OWNERSHIP_V2_ENV = "FAULTLINE_OWNERSHIP_V2"

#: Falsy tokens — unset, empty, ``0``, and the usual off-words — keep every
#: B66-v2 segment inert (byte-identical to the merged B66+B67 world).
_FALSY = frozenset({"", "0", "false", "no", "off"})


def ownership_v2_enabled() -> bool:
    """``True`` when ``FAULTLINE_OWNERSHIP_V2`` is set truthy (default OFF)."""
    return os.environ.get(OWNERSHIP_V2_ENV, "0").strip().lower() not in _FALSY
