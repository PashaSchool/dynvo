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
    # Stage 6.7b UF-refiner: one entry per per-domain Haiku call (first call
    # AND the optional name-validation retry — the retry's user prompt embeds
    # the prohibition text, so it keys separately). Keyed on the content hash
    # of {cache version, canonical model, system prompt, user prompt}; the
    # user prompt IS the canonical structured input (domain + full UF payload
    # batch, built deterministically). The cached value is the PARSED
    # ``{uf_id: refinement_row}`` mapping — replayed through the SAME
    # validation/apply code as a live call, so an unchanged repo re-scan is
    # byte-identical at $0. Content-keyed (same input → same answer): a
    # deterministic short-circuit, not per-repo memory (rule-cold-scan safe).
    LLM_UF_REFINE = "llm-uf-refine"
    # Stage 6.7c mega-UF splitter: one entry per per-mega-UF partition call
    # (Sonnet). Keyed on {cache version, canonical model, system prompt, user
    # prompt (domain + member flow names)}. The cached value is the PARSED
    # ``journeys[]`` array — replayed through the same ``_split_one`` builder,
    # so sub-UF construction is byte-identical on an unchanged repo.
    LLM_UF_SPLIT = "llm-uf-split"
    # Stage 8 Layer-2 clusterer/analyst: one entry per LLM call on the
    # product-cluster path — the Haiku label-mapper (``cluster_via_haiku``),
    # the Sonnet analyst main + parse-retry calls, and the analyst's
    # name-validator rename retry. All keyed on {cache version, canonical
    # model, system prompt, user prompt}; model+system are in the key, so the
    # two Stage-8 modes never collide. Values are the PARSED structured
    # outputs (mapping / analyst dict / renames) — replayed through the same
    # emission/validation code, making product_feature_id stamps reproducible
    # on an unchanged repo. NOT the marketing-page cache (kind ``marketing``),
    # which stays as-is.
    LLM_PRODUCT_CLUSTER = "llm-product-cluster"
    # Stage 0.5 stack auditor: raw response text keyed on
    # (cache version + model + system + user prompt). The auditor prompt is
    # deterministic for an identical repo state, but Anthropic temp=0 is NOT
    # bit-deterministic — an uncached auditor re-rolled its prose hints every
    # run, and those hints sit inside the stage-8 analyst payload (= its
    # cache key), silently re-rolling all of Layer 2 (supabase, 2026-07-02).
    LLM_AUDITOR = "llm-auditor"
    # Wave-3 personas (§4.7): PM Labeler / Surface Adjudicator / Draft
    # Verifier batch calls. One entry per batch, keyed on {cache version,
    # persona role, canonical model, system prompt, canonical items
    # payload}. The cached value is the PARSED per-item decision mapping —
    # replayed through the same validation/apply code as a live call, so
    # an unchanged repo re-scans byte-identical at $0. Content-keyed (same
    # input → same answer): a deterministic short-circuit, not per-repo
    # memory (rule-cold-scan safe).
    LLM_PERSONA = "llm-persona"
    # Stage 6.7e journey-evidence adjudicator (B57 Seg2): one entry per
    # verdict batch, keyed on {cache version, canonical model, system
    # prompt, canonical batch payload} — the payload embeds each UF's
    # (sorted member set + name + neighbor sets + evidence), so the key is
    # content-derived. The cached value is the PARSED verdicts array —
    # replayed through the SAME deterministic citation verifier + apply
    # code as a live call, so an unchanged repo re-scans byte-identical at
    # $0. Content-keyed (same input → same answer): a deterministic
    # short-circuit, not per-repo memory (rule-cold-scan safe).
    LLM_ADJUDICATOR = "llm-adjudicator"
    # Stage 6.55 page-interior parse (W4, Product-Spine §4.6): one entry
    # per PAGE FILE, keyed on sha256(parser version + tree-sitter grammar
    # versions + file bytes). The cached value is the serialised interior
    # node list (components / headings / provenance / spans) — pure
    # deterministic parse output, $0 LLM. Content-keyed (same bytes →
    # same tree), so this is a deterministic short-circuit, not per-repo
    # memory — rule-cold-scan safe.
    INTERIOR = "interior-parse"
    # W6 ts_ast layer (pipeline_v2/ts_ast): one entry per (walker
    # namespace, file content hash, dialect) — the serialised DERIVED
    # walker payload (definition spans / import edges), never the tree
    # itself. The key embeds grammar + walker versions (ts_ast/parse.py)
    # so grammar upgrades and walker changes re-derive automatically.
    # Pure deterministic parse output, $0 LLM, content-keyed →
    # rule-cold-scan safe.
    AST = "ast-parse"
    # Top-level scan-result cache: one entry per (repo content identity +
    # engine version + scan config) — the full FeatureMap JSON of a
    # completed scan. Because temperature=0 on Anthropic is NOT bit-exact,
    # the several LLM stages (Stage 3 flows + 6.7b/6.7c UF + Stage 8 product
    # clusterer) diverge run-to-run on an unchanged repo. This cache
    # short-circuits the WHOLE pipeline: same input → replay the byte-
    # identical stored FeatureMap ($0, instant). Content-keyed (repo state +
    # version + config, NOT run_id/timestamps) so it is a deterministic
    # reproducibility cache, not per-repo memory — rule-cold-scan safe.
    # Stored under a dedicated ``scan-cache/`` dir (scans are large JSONs,
    # kept out of the small ``llm-cache/``). Opt-in via FAULTLINE_SCAN_CACHE.
    SCAN_RESULT = "scan-result"
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
        if kind == CacheKind.LLM_UF_REFINE.value:
            return self._base / "llm-cache" / "uf-refine" / f"{safe_key}.json"
        if kind == CacheKind.LLM_UF_SPLIT.value:
            return self._base / "llm-cache" / "uf-split" / f"{safe_key}.json"
        if kind == CacheKind.LLM_PRODUCT_CLUSTER.value:
            return self._base / "llm-cache" / "product-cluster" / f"{safe_key}.json"
        if kind == CacheKind.LLM_AUDITOR.value:
            return self._base / "llm-cache" / "auditor" / f"{safe_key}.json"
        if kind == CacheKind.SCAN_RESULT.value:
            # Dedicated dir — scan JSONs are large; keep them out of the
            # small content-keyed ``llm-cache/``.
            return self._base / "scan-cache" / f"{safe_key}.json"
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
            CacheKind.LLM_UF_REFINE.value,
            CacheKind.LLM_UF_SPLIT.value,
            CacheKind.LLM_PRODUCT_CLUSTER.value,
            CacheKind.LLM_AUDITOR.value,
            CacheKind.SCAN_RESULT.value,
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
