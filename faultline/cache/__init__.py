"""Cache and incremental refresh for feature maps.

Keeps the feature-map JSON in sync with the repo without a full
re-scan. Uses git SHA tracking + content hashing to identify the
minimum set of features/flows/symbols that need re-analysis.

Layers:
  - freshness.py: detect staleness (commits behind, file diff)
  - hashing.py: content + symbol hashing helpers
  - refresh.py: orchestrator that runs incremental re-scan

The existing analyzer/incremental.py handles the actual feature
metric recomputation. This module wraps it with SHA tracking and
file-hash invalidation.

Pluggable cache backend (spec: encrypted-db-cache-backend)
----------------------------------------------------------
This package also exposes the engine's pluggable key/value cache:

  * :class:`~faultline.cache.backend.CacheBackend` — the Protocol.
  * :class:`~faultline.cache.backend.FilesystemCacheBackend` — default.
  * :class:`MemoryCacheBackend` — in-process test double.
  * :func:`get_cache_backend` — env-driven selector + lazy injection.

The OSS engine ships only the interface + filesystem default. A hosted
worker injects an encrypted DB backend via
``FAULTLINES_CACHE_BACKEND="module.path:factory"`` — imported lazily so
this package never hard-depends on boto3 / psycopg / KMS / Neon
(``rule-oss-engine-vs-infra-boundary``).
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from faultline.cache.backend import (
    CacheBackend,
    CacheKind,
    FilesystemCacheBackend,
)

#: Env var selecting the cache backend. Empty / ``"fs"`` → filesystem.
#: ``"module.path:factory"`` → lazy-imported injection point.
CACHE_BACKEND_ENV = "FAULTLINES_CACHE_BACKEND"


class MemoryCacheBackend:
    """In-process :class:`CacheBackend` — a dict keyed by ``(kind, key)``.

    No persistence, no TTL enforcement (values never expire), no
    network. Used as a test double to prove the pipeline reads/writes
    EXCLUSIVELY through the backend (nothing touches the filesystem) and
    to assert warm-cache hits across two scans against one instance.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}
        self.flush_count = 0

    def get(self, kind: str, key: str) -> Any | None:
        return self._store.get((str(kind), key))

    def set(
        self, kind: str, key: str, value: Any, *, ttl_seconds: int | None = None,
    ) -> None:
        self._store[(str(kind), key)] = value

    def delete(self, kind: str, key: str) -> None:
        self._store.pop((str(kind), key), None)

    def load_namespace(self, kind: str) -> dict[str, Any]:
        k = str(kind)
        return {key: val for (kk, key), val in self._store.items() if kk == k}

    def flush(self) -> None:
        self.flush_count += 1


def get_cache_backend(*, org_id: str | None = None) -> CacheBackend:
    """Return the cache backend selected by ``$FAULTLINES_CACHE_BACKEND``.

    * unset / ``""`` / ``"fs"`` → :class:`FilesystemCacheBackend`.
    * ``"module.path:factory"`` → lazily import ``module.path`` and call
      ``factory(org_id=org_id)`` (defaults to ``build_backend`` when the
      ``:factory`` part is omitted). This is the boundary-safe injection
      point: the private/worker DB backend ships its own module and the
      OSS engine never imports it by name.

    ``org_id`` is forwarded to the injected factory so a DB backend can
    scope rows per tenant. The filesystem default ignores it.
    """
    spec = os.environ.get(CACHE_BACKEND_ENV, "").strip()
    if not spec or spec == "fs":
        return FilesystemCacheBackend()
    module_path, _, factory_name = spec.partition(":")
    mod = importlib.import_module(module_path)
    factory = getattr(mod, factory_name or "build_backend")
    return factory(org_id=org_id)


__all__ = [
    "CACHE_BACKEND_ENV",
    "CacheBackend",
    "CacheKind",
    "FilesystemCacheBackend",
    "MemoryCacheBackend",
    "get_cache_backend",
]
