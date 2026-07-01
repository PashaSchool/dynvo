"""Pluggable cache backend for the Faultlines engine.

This module defines the *interface* every cache implementation must
satisfy plus the default on-disk implementation. The engine treats a
backend as a dumb ``(kind, key) -> value`` map with optional TTL — no
pipeline code ever knows whether the bytes hit local disk, a remote
DB, or RAM.

Design intent (spec: ``docs/specs/encrypted-db-cache-backend.md``):

  * The OSS engine ships ONLY this interface + ``FilesystemCacheBackend``
    (byte-for-byte today's per-kind path/TTL semantics). The encrypted
    DB backend lives in a private/worker module that is injected at
    runtime via the env-driven loader in ``faultline.cache`` — see
    ``get_cache_backend``. No boto3 / psycopg / KMS / Neon code ever
    enters this package (``rule-oss-engine-vs-infra-boundary``).

  * Cache KEYS are never computed here. Callers pass the existing
    sha256 / slug strings unchanged so dev filesystem caches stay
    valid across the refactor.

The filesystem backend deliberately preserves each kind's *native*
on-disk JSON shape (the exact bytes the legacy hardcoded call sites
wrote) so an upgraded engine still reads a dev's pre-existing
``~/.faultline`` caches and a fresh write is byte-identical to today.
TTL is enforced per kind exactly as before:

  * ``llm-name``     — 90-day mtime TTL, value is a flat ``{cluster: name}`` map
  * ``marketing``    — 7-day ``fetched_at_epoch`` TTL inside the value
  * ``assignment``   — no TTL, value is ``{file: canonical}``
  * ``flow-verdict`` — no TTL, value carries its own ``version``/hash gates
  * ``flow-symbol``  — no TTL, value carries its own ``version`` gate
  * ``blame``        — handled inside the clone (see spec §5); kind reserved

No LLM. No network. Pure local-disk operations.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class CacheKind(str, Enum):
    """The fixed set of cache namespaces the engine uses.

    ``str``-valued so a ``CacheKind`` is usable anywhere a plain ``str``
    kind is expected (the Protocol accepts ``str``) while giving call
    sites a typo-proof constant. (``str, Enum`` rather than ``StrEnum``
    for 3.10 compatibility — ``CacheKind.X == "x"`` holds either way.)
    """

    ASSIGNMENT = "assignment"
    LLM_NAME = "llm-name"
    # Stage 4 residual fallback: one entry per residual cluster, keyed on
    # the content hash of the cluster's LLM input (system prompt + cluster
    # signature + sample paths + size + canonical model + cache version).
    # Content-keyed (same input → same answer) so this is a deterministic
    # short-circuit, not per-repo memory — compliant with rule-cold-scan.
    LLM_RESIDUAL = "llm-residual"
    # Stage 6.7d journey/product abstraction: one entry per
    # (digest + abstraction model + re-attribution model + cache version)
    # content hash. The cached value is the model's two structured outputs
    # (abstraction specs + dev→capability map) — NOT reconstructed objects — so
    # a re-scan of an unchanged repo replays the SAME LLM answers and produces
    # byte-identical output. Content-keyed (same input → same answer), so this
    # is a deterministic short-circuit, not per-repo memory (rule-cold-scan safe).
    LLM_ABSTRACTION = "llm-abstraction"
    # Stage 3 flow detection: one entry per developer-feature flow-detection
    # unit, keyed on the content hash of that unit's LLM input (system prompt
    # + feature slug + sorted paths + extracted exports/routes + per-file
    # source-content signature + canonical model + cache version). The cached
    # value is the parsed ``flows[]`` array the LLM returned — NOT the raw text
    # or tokens — so a re-scan of an unchanged feature REPLAYS the identical
    # flows and the downstream PF/UF are reproducible. Content-keyed (same
    # input → same answer): a deterministic short-circuit, not per-repo memory
    # (rule-cold-scan safe). Stage 3 is the last uncached main LLM stage; this
    # closes the reproducibility gap Stage 4 (LLM_RESIDUAL) already covered.
    LLM_FLOWS = "llm-flows"
    FLOW_VERDICT = "flow-verdict"
    FLOW_SYMBOL = "flow-symbol"
    MARKETING = "marketing"
    BLAME = "blame"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# Per-kind default TTL (seconds) used by backends that don't embed TTL
# in the value. ``None`` = the kind manages its own expiry (or none).
_NAME_CACHE_TTL_SECONDS = 90 * 24 * 3600
_MARKETING_TTL_SECONDS = 7 * 24 * 3600


@runtime_checkable
class CacheBackend(Protocol):
    """Key/value cache keyed by ``(kind, key)``. Values are JSON-serialisable.

    Implementations decide persistence + encryption. The engine treats
    every backend as a dumb map with TTL.
    """

    def get(self, kind: str, key: str) -> Any | None:
        """Return cached value or ``None`` if missing/expired."""
        ...

    def set(
        self, kind: str, key: str, value: Any, *, ttl_seconds: int | None = None,
    ) -> None:
        """Store ``value`` under ``(kind, key)``. ``ttl=None`` → kind default."""
        ...

    def delete(self, kind: str, key: str) -> None:
        """Remove ``(kind, key)`` if present. No error if absent."""
        ...

    def load_namespace(self, kind: str) -> dict[str, Any]:
        """Return every ``{key: value}`` for a kind (in-memory hydration).

        Workers preload all rows for a scan in ONE round-trip then flush
        writes at the end — critical to keep DB latency off the hot loop.
        """
        ...

    def flush(self) -> None:
        """Persist any buffered writes. No-op for the filesystem backend."""
        ...


# ── Filesystem default ───────────────────────────────────────────────────


def _safe_component(value: str) -> str:
    """Filesystem-safe single path component (mirrors stage_8's sanitiser)."""
    return re.sub(r"[^A-Za-z0-9._\-]", "_", value or "unknown")


class FilesystemCacheBackend:
    """Default backend: byte-for-byte today's on-disk caches under a base dir.

    ``base_dir`` defaults to the resolved Faultlines base dir
    (``FAULTLINES_RUN_DIR`` env, else ``~/.faultline``). Writes are
    immediate; ``flush()`` is a no-op. Each kind keeps its legacy file
    name + JSON body so existing dev caches still hit.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        if base_dir is None:
            from faultline.cache.paths import faultline_base_dir

            base_dir = faultline_base_dir()
        self._base = Path(base_dir)

    # -- path layout (matches legacy call sites byte-for-byte) ---------

    def _path_for(self, kind: str, key: str) -> Path:
        safe_key = _safe_component(key)
        if kind == CacheKind.LLM_NAME.value:
            return self._base / "llm-cache" / f"{safe_key}.json"
        if kind == CacheKind.LLM_RESIDUAL.value:
            return self._base / "llm-cache" / "residual" / f"{safe_key}.json"
        if kind == CacheKind.LLM_ABSTRACTION.value:
            return self._base / "llm-cache" / "abstraction" / f"{safe_key}.json"
        if kind == CacheKind.LLM_FLOWS.value:
            return self._base / "llm-cache" / "flows" / f"{safe_key}.json"
        if kind == CacheKind.MARKETING.value:
            return self._base / "marketing-cache" / f"{safe_key}.json"
        if kind == CacheKind.ASSIGNMENT.value:
            return self._base / f"assignments-{safe_key}.json"
        if kind == CacheKind.FLOW_VERDICT.value:
            return self._base / f"flow-verdicts-{safe_key}.json"
        if kind == CacheKind.FLOW_SYMBOL.value:
            return self._base / f"flow-symbols-{safe_key}.json"
        if kind == CacheKind.BLAME.value:
            return self._base / "blame-cache" / f"{safe_key}.json"
        return self._base / f"cache-{_safe_component(kind)}" / f"{safe_key}.json"

    def _is_subdir_kind(self, kind: str) -> bool:
        """Kinds stored one-file-per-key under a dedicated subdir.

        These are content-keyed (sha / slug) and bulk-loadable. The
        remaining kinds (assignment / flow-*) are one-file-per-repo at
        the base dir and read directly by their call site.
        """
        return kind in {
            CacheKind.LLM_NAME.value,
            CacheKind.LLM_RESIDUAL.value,
            CacheKind.LLM_ABSTRACTION.value,
            CacheKind.LLM_FLOWS.value,
            CacheKind.MARKETING.value,
            CacheKind.BLAME.value,
        }

    def _read_json(self, path: Path) -> Any | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("cache.fs: unreadable %s (%s) — miss", path, exc)
            return None

    def _write_json(self, path: Path, value: Any, *, indent: int | None = None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=indent),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("cache.fs: write failed %s (%s)", path, exc)

    # -- get/set/delete ------------------------------------------------

    def get(self, kind: str, key: str) -> Any | None:
        path = self._path_for(kind, key)
        if kind == CacheKind.LLM_NAME.value:
            # Legacy semantics: 90-day mtime TTL; expired file is deleted.
            if not path.is_file():
                return None
            age_days = (
                datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            ).days
            if age_days > 90:
                try:
                    path.unlink()
                except OSError:
                    pass
                return None
            return self._read_json(path)
        # All other kinds embed their own TTL / version inside the value
        # (marketing: fetched_at_epoch; flow-*: version + hashes), so the
        # backend just returns the stored body and the caller validates.
        return self._read_json(path)

    def set(
        self, kind: str, key: str, value: Any, *, ttl_seconds: int | None = None,
    ) -> None:
        path = self._path_for(kind, key)
        # Preserve the exact indentation each legacy writer used so a
        # fresh write is byte-identical to today.
        if kind == CacheKind.MARKETING.value:
            self._write_json(path, value, indent=2)
        elif kind in {CacheKind.FLOW_VERDICT.value, CacheKind.FLOW_SYMBOL.value}:
            self._write_json(path, value, indent=2)
        else:  # llm-name + assignment + blame: compact (legacy used no indent)
            self._write_json(path, value, indent=None)

    def delete(self, kind: str, key: str) -> None:
        path = self._path_for(kind, key)
        try:
            path.unlink()
        except (OSError, FileNotFoundError):
            pass

    def load_namespace(self, kind: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not self._is_subdir_kind(kind):
            # Flat per-repo kinds are read directly by their call site.
            return out
        directory = self._path_for(kind, "_").parent
        if not directory.is_dir():
            return out
        for f in directory.glob("*.json"):
            value = self.get(kind, f.stem)
            if value is not None:
                out[f.stem] = value
        return out

    def flush(self) -> None:  # writes are immediate
        return None


__all__ = [
    "CacheBackend",
    "CacheKind",
    "FilesystemCacheBackend",
]
