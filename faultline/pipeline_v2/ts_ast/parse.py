"""W6 M1 — shared tree-sitter parse layer for TS/JS (spec: w6ast-spec §0/§2/§3.M1).

One parse per file per process: every ts_ast walker (defs M1, imports
M2) consumes the SAME :class:`FileParse` — either passed explicitly or
replayed from the in-process content-hash memo. Derived, JSON-able
walker outputs (never the tree itself) persist through the scan's
``CacheBackend`` under ``CacheKind.AST``, keyed on
``content_hash + GRAMMAR_VER + WALKER_VER`` (+ walker namespace).

Graceful degrade (law §2): tree-sitter is an optional dependency
(``pip install 'faultlines[ast]'``). Missing lib / missing grammar /
per-file parse failure → the caller falls back to the regex path
(``analyzer/ast_extractor.py``), which is never removed. Master
kill-switch: ``FAULTLINE_TS_AST=0`` → :func:`is_active` is False and
every consumer stays byte-identical to the regex engine.

Determinism: pure parse, no fs walks, no set-iteration into outputs;
telemetry counters are observability-only and never feed results.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ENV_FLAG",
    "ENV_FLAG_ENTRY",
    "GRAMMAR_VER",
    "WALKER_VER",
    "TREE_SITTER_AVAILABLE",
    "FileParse",
    "ts_ast_enabled",
    "entry_enabled",
    "is_active",
    "lang_for_path",
    "content_hash_of",
    "parse_file",
    "cache_key",
    "cached_payload",
    "telemetry_snapshot",
    "reset_state",
]

#: Master flag (spec §2): default ON in the ast-* branches; ``=0`` must
#: reproduce the regex engine byte-identically.
ENV_FLAG = "FAULTLINE_TS_AST"
#: Entry-detection migration flag (spec §2): SEPARATE, default OFF —
#: entry migration lands last (M4c), gated on its own switch.
ENV_FLAG_ENTRY = "FAULTLINE_TS_AST_ENTRY"

#: Bump on ANY change to ANY ts_ast walker's output shape or logic
#: (defs.py, imports.py, …). Shared on purpose: a bump re-derives every
#: cached payload — always safe, never stale (rule-cache-invalidation).
WALKER_VER = "m1-1"

_TS_SUFFIXES = {
    ".ts": "ts",
    ".mts": "ts",
    ".cts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".jsx": "jsx",
}

# ── Optional tree-sitter import (pattern mirrors stage_6_55 / 6_6) ───────

try:  # pragma: no cover — import-time branch
    from tree_sitter import Language, Parser

    TREE_SITTER_AVAILABLE = True
except Exception:  # noqa: BLE001 — any failure → degrade gracefully
    TREE_SITTER_AVAILABLE = False
    Language = None  # type: ignore[assignment, misc]
    Parser = None  # type: ignore[assignment, misc]


_LANG_CACHE: dict[str, Any] = {}
_LANG_LOCK = threading.Lock()


def _language(lang: str) -> Any | None:
    """tree-sitter ``Language`` for ``ts`` | ``tsx`` | ``js`` | ``jsx``.

    ``js`` and ``jsx`` share the javascript grammar (it parses JSX
    natively); ``ts``/``tsx`` load the two typescript grammar dialects.
    Any load failure caches ``None`` → those files take the regex path.
    """
    if not TREE_SITTER_AVAILABLE:
        return None
    with _LANG_LOCK:
        if lang in _LANG_CACHE:
            return _LANG_CACHE[lang]
        loaded: Any | None = None
        try:
            if lang in ("ts", "tsx"):
                import tree_sitter_typescript as ts_mod

                capsule = (
                    ts_mod.language_tsx()
                    if lang == "tsx"
                    else ts_mod.language_typescript()
                )
                loaded = Language(capsule)
            elif lang in ("js", "jsx"):
                import tree_sitter_javascript as js_mod

                loaded = Language(js_mod.language())
        except Exception:  # noqa: BLE001 — partial extras install
            loaded = None
        _LANG_CACHE[lang] = loaded
        return loaded


def _compute_grammar_ver() -> str:
    """Installed tree-sitter + grammar versions (cache-key component).

    Computed (not hardcoded) so a grammar upgrade invalidates every
    cached payload automatically — same convention as stage 6.55's
    ``_grammar_signature``.
    """
    parts: list[str] = []
    try:
        import importlib.metadata as _md

        for pkg in (
            "tree-sitter",
            "tree-sitter-typescript",
            "tree-sitter-javascript",
        ):
            try:
                parts.append(f"{pkg}={_md.version(pkg)}")
            except Exception:  # noqa: BLE001
                parts.append(f"{pkg}=?")
    except Exception:  # noqa: BLE001
        parts.append("meta=?")
    return ";".join(parts)


#: Version string of the loaded grammars (spec §2 cache-key law).
GRAMMAR_VER: str = _compute_grammar_ver()


# ── Flags ────────────────────────────────────────────────────────────────


def _flag_on(env: str, default: str) -> bool:
    return (os.environ.get(env, default) or default).strip().lower() not in {
        "0", "false", "no", "off",
    }


def ts_ast_enabled() -> bool:
    """Master flag — default ON (spec §2)."""
    return _flag_on(ENV_FLAG, "1")


def entry_enabled() -> bool:
    """Entry-detection migration flag — default OFF (spec §2)."""
    return _flag_on(ENV_FLAG_ENTRY, "0")


def is_active() -> bool:
    """Flag on + tree-sitter importable + ≥1 grammar loadable."""
    if not ts_ast_enabled():
        return False
    if not TREE_SITTER_AVAILABLE:
        return False
    return any(_language(n) is not None for n in ("tsx", "js", "ts"))


# ── Language + hashing ───────────────────────────────────────────────────


def lang_for_path(path: str) -> str | None:
    """``ts`` | ``tsx`` | ``js`` | ``jsx`` for a TS/JS source path.

    ``None`` for everything else, INCLUDING ``.d.ts`` / ``.d.mts`` /
    ``.d.cts`` ambient-declaration files (spec §3.M5: skipped — they
    hold no runtime definitions and their re-exports mirror the source
    tree the resolver already sees).
    """
    low = path.lower()
    if low.endswith((".d.ts", ".d.mts", ".d.cts")):
        return None
    dot = low.rfind(".")
    if dot < 0:
        return None
    return _TS_SUFFIXES.get(low[dot:])


def content_hash_of(source: bytes) -> str:
    """sha256 hex of the file bytes — THE per-file cache identity."""
    return hashlib.sha256(source).hexdigest()


# ── FileParse + in-process memo ──────────────────────────────────────────


@dataclass
class FileParse:
    """One parsed TS/JS file (spec §1 — NOT serialisable: holds the tree)."""

    path: str
    content_hash: str
    lang: str  # 'ts' | 'tsx' | 'js' | 'jsx'
    tree: Any  # tree_sitter.Tree


#: FIFO memo of recent parses keyed by (lang, content_hash): M1 defs and
#: M2 imports walk the SAME tree without a second parse (spec §2 perf law).
_MEMO: dict[tuple[str, str], FileParse] = {}
_MEMO_ORDER: list[tuple[str, str]] = []
_MEMO_CAP = 256
_MEMO_LOCK = threading.Lock()

_TELEMETRY = {
    "parses": 0,
    "parse_failures": 0,
    "memo_hits": 0,
    "files_with_errors": 0,
    "cache_hits": 0,
    "cache_writes": 0,
}


def parse_file(path: str, source: bytes) -> FileParse | None:
    """Parse one TS/JS file → :class:`FileParse`, or ``None`` to degrade.

    ``None`` covers: layer inactive (flag/lib), non-TS/JS path (incl.
    ``.d.ts``), grammar unavailable for the dialect, parser exception,
    or a root so broken it is not a ``program``. Every ``None`` sends
    THAT FILE down the regex path (spec §2 fallback law); real parse
    faults increment ``telemetry.parse_failures``.

    A tree that parsed with RECOVERED errors (``root.has_error``) is
    still returned — tree-sitter degrades locally and the healthy
    subtrees carry honest spans; such files are counted in
    ``files_with_errors``.
    """
    lang = lang_for_path(path)
    if lang is None or not is_active():
        return None
    chash = content_hash_of(source)
    memo_key = (lang, chash)
    with _MEMO_LOCK:
        hit = _MEMO.get(memo_key)
        if hit is not None:
            _TELEMETRY["memo_hits"] += 1
            return FileParse(
                path=path, content_hash=chash, lang=lang, tree=hit.tree,
            )
    language = _language(lang)
    if language is None:
        return None
    try:
        tree = Parser(language).parse(source)
        root = tree.root_node
    except Exception:  # noqa: BLE001 — grammar/version faults degrade
        with _MEMO_LOCK:
            _TELEMETRY["parse_failures"] += 1
        logger.debug("ts_ast: parse failed for %s", path)
        return None
    if root is None or root.type != "program":
        with _MEMO_LOCK:
            _TELEMETRY["parse_failures"] += 1
        logger.debug("ts_ast: non-program root for %s", path)
        return None
    fp = FileParse(path=path, content_hash=chash, lang=lang, tree=tree)
    with _MEMO_LOCK:
        _TELEMETRY["parses"] += 1
        if root.has_error:
            _TELEMETRY["files_with_errors"] += 1
        if memo_key not in _MEMO:
            _MEMO[memo_key] = fp
            _MEMO_ORDER.append(memo_key)
            while len(_MEMO_ORDER) > _MEMO_CAP:
                _MEMO.pop(_MEMO_ORDER.pop(0), None)
    return fp


# ── Persistent payload cache (CacheKind.AST) ─────────────────────────────


def cache_key(namespace: str, lang: str, content_hash: str) -> str:
    """Spec §2 cache-key law: content hash + grammar + walker versions.

    ``namespace`` separates walker payload families ("defs", "imports",
    …) so they never collide inside the one ``CacheKind.AST`` bucket;
    ``lang`` is included because the same bytes parse differently under
    the ts vs tsx vs js grammars.
    """
    h = hashlib.sha256()
    for part in (namespace, lang, GRAMMAR_VER, WALKER_VER, content_hash):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def cached_payload(
    backend: Any | None,
    namespace: str,
    lang: str,
    content_hash: str,
    compute: Callable[[], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Replay a walker's JSON-able payload from ``CacheKind.AST`` or compute it.

    ``compute`` returning ``None`` (parse failure) is NEVER cached — a
    later run with a healthier environment must get a fresh chance.
    Cache faults never break a scan (house cache law).
    """
    from faultline.cache.backend import CacheKind

    key = cache_key(namespace, lang, content_hash)
    if backend is not None:
        try:
            hit = backend.get(CacheKind.AST, key)
            if isinstance(hit, dict):
                with _MEMO_LOCK:
                    _TELEMETRY["cache_hits"] += 1
                return hit
        except Exception:  # noqa: BLE001 — cache faults never break a scan
            pass
    payload = compute()
    if payload is None:
        return None
    if backend is not None:
        try:
            backend.set(CacheKind.AST, key, payload)
            with _MEMO_LOCK:
                _TELEMETRY["cache_writes"] += 1
        except Exception:  # noqa: BLE001
            pass
    return payload


# ── Telemetry / test hooks ───────────────────────────────────────────────


def telemetry_snapshot() -> dict[str, int]:
    """Copy of the process-lifetime counters (observability only)."""
    with _MEMO_LOCK:
        return dict(_TELEMETRY)


def reset_state() -> None:
    """Drop the memo + zero telemetry (test isolation hook)."""
    with _MEMO_LOCK:
        _MEMO.clear()
        _MEMO_ORDER.clear()
        for k in _TELEMETRY:
            _TELEMETRY[k] = 0
