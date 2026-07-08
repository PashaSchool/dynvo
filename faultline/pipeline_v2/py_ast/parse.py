"""Track-B py_ast M1 — shared stdlib-``ast`` parse layer for Python.

The Python mirror of ``ts_ast.parse`` (w6ast-spec §0/§2/§3.M1). One parse
per file per process: the defs walker (M1) and the imports walker (M2)
consume the SAME :class:`FileParse` (an ``ast.Module``) — either passed
explicitly or replayed from the in-process content-hash memo. Derived,
JSON-able walker outputs (never the tree) persist through the scan's
``CacheBackend`` under ``CacheKind.AST``, keyed on
``content_hash + GRAMMAR_VER + WALKER_VER`` (+ walker namespace).

Unlike ts_ast, the "grammar" is the stdlib ``ast`` module: it is ALWAYS
available (no optional extra), so :data:`TREE_SITTER_AVAILABLE`-style
gating collapses to the master flag alone. ``GRAMMAR_VER`` folds the
running CPython version because ``ast`` node shapes + ``end_lineno``
semantics are tied to the interpreter version — a Python upgrade
invalidates cached payloads automatically.

Graceful degrade (law §2): a per-file ``SyntaxError`` / ``ValueError``
(partial file, template, py2 source) → the caller falls back to the
regex path (``analyzer/ast_extractor.py`` ``_python_symbols_via_regex``),
which is never removed. Master kill-switch: ``FAULTLINE_PY_AST=0`` →
:func:`is_active` is False and every consumer stays byte-identical to
the legacy engine.

Determinism: pure parse, no fs walks, no set-iteration into outputs;
telemetry counters are observability-only and never feed results.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ENV_FLAG",
    "ENV_FLAG_ENTRY",
    "GRAMMAR_VER",
    "WALKER_VER",
    "AST_AVAILABLE",
    "FileParse",
    "py_ast_enabled",
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

#: Master flag (spec §2): default ON in the track-b branch; ``=0`` must
#: reproduce the legacy regex engine byte-identically.
ENV_FLAG = "FAULTLINE_PY_AST"
#: Provenance/entry migration flag (spec §2): SEPARATE, default OFF —
#: reserved so a later Track-A entry-on-AST decision has its own switch.
ENV_FLAG_ENTRY = "FAULTLINE_PY_AST_ENTRY"

#: Bump on ANY change to ANY py_ast walker's output shape or logic
#: (defs.py, imports.py, …). Shared on purpose: a bump re-derives every
#: cached payload — always safe, never stale (rule-cache-invalidation).
WALKER_VER = "py-m1-1"

#: stdlib ``ast`` is always importable; the constant is kept for shape
#: parity with ts_ast (``TREE_SITTER_AVAILABLE``) and to let tests assert
#: on it. It is never False in practice.
AST_AVAILABLE = True

_PY_SUFFIXES = {".py"}
#: ``.pyi`` type-stub files are the Python analogue of ``.d.ts`` — they
#: hold no runtime definitions and their re-exports mirror the source
#: tree the resolver already sees. Skipped (spec §3.M5 declaration rule).
_STUB_SUFFIXES = (".pyi",)


def _compute_grammar_ver() -> str:
    """CPython version string (cache-key component).

    ``ast`` node structure + ``end_lineno``/``end_col_offset`` semantics
    are interpreter-version-bound, so folding the version invalidates
    every cached payload on a Python upgrade automatically — the same
    convention ts_ast uses for a grammar bump.
    """
    v = sys.version_info
    return f"cpython={v.major}.{v.minor}.{v.micro}"


#: Version string of the running interpreter (spec §2 cache-key law).
GRAMMAR_VER: str = _compute_grammar_ver()


# ── Flags ────────────────────────────────────────────────────────────────


def _flag_on(env: str, default: str) -> bool:
    return (os.environ.get(env, default) or default).strip().lower() not in {
        "0", "false", "no", "off",
    }


def py_ast_enabled() -> bool:
    """Master flag — default ON (spec §2)."""
    return _flag_on(ENV_FLAG, "1")


def entry_enabled() -> bool:
    """Provenance/entry migration flag — default OFF (spec §2)."""
    return _flag_on(ENV_FLAG_ENTRY, "0")


def is_active() -> bool:
    """Flag on + stdlib ast importable (always true in CPython)."""
    return py_ast_enabled() and AST_AVAILABLE


# ── Language + hashing ───────────────────────────────────────────────────


def lang_for_path(path: str) -> str | None:
    """``'py'`` for a Python source path, else ``None``.

    ``None`` for everything else INCLUDING ``.pyi`` type-stub files
    (skipped by design — the declaration-file rule, spec §3.M5).
    """
    low = path.lower()
    if low.endswith(_STUB_SUFFIXES):
        return None
    dot = low.rfind(".")
    if dot < 0:
        return None
    return "py" if low[dot:] in _PY_SUFFIXES else None


def content_hash_of(source: bytes) -> str:
    """sha256 hex of the file bytes — THE per-file cache identity."""
    return hashlib.sha256(source).hexdigest()


# ── FileParse + in-process memo ──────────────────────────────────────────


@dataclass
class FileParse:
    """One parsed Python file (spec §1 — NOT serialisable: holds the tree)."""

    path: str
    content_hash: str
    lang: str  # always 'py' here (parity with ts_ast's Lang slot)
    tree: Any  # ast.Module


#: FIFO memo of recent parses keyed by (lang, content_hash): defs and
#: imports walk the SAME tree without a second parse (spec §2 perf law).
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
    """Parse one Python file → :class:`FileParse`, or ``None`` to degrade.

    ``None`` covers: layer inactive (flag), non-Python path (incl.
    ``.pyi``), or a ``SyntaxError`` / ``ValueError`` (partial file,
    py2 source, template). Every ``None`` sends THAT FILE down the regex
    path (spec §2 fallback law); real parse faults increment
    ``telemetry.parse_failures``.

    ``ast.parse`` accepts bytes and honours a PEP-263 coding cookie, so
    the raw file bytes are parsed directly (no lossy pre-decode). Line
    numbers are 1-based regardless of encoding.
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
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        # ValueError covers null-byte / bad source; RecursionError guards
        # pathologically deep expressions — all degrade to the regex path.
        with _MEMO_LOCK:
            _TELEMETRY["parse_failures"] += 1
        logger.debug("py_ast: parse failed for %s", path)
        return None
    if not isinstance(tree, ast.Module):  # pragma: no cover — defensive
        with _MEMO_LOCK:
            _TELEMETRY["parse_failures"] += 1
        return None
    fp = FileParse(path=path, content_hash=chash, lang=lang, tree=tree)
    with _MEMO_LOCK:
        _TELEMETRY["parses"] += 1
        if memo_key not in _MEMO:
            _MEMO[memo_key] = fp
            _MEMO_ORDER.append(memo_key)
            while len(_MEMO_ORDER) > _MEMO_CAP:
                _MEMO.pop(_MEMO_ORDER.pop(0), None)
    return fp


# ── Persistent payload cache (CacheKind.AST) ─────────────────────────────


def cache_key(namespace: str, lang: str, content_hash: str) -> str:
    """Spec §2 cache-key law: content hash + grammar + walker versions.

    ``namespace`` separates walker payload families ("py-defs",
    "py-imports", …) so they never collide inside the one
    ``CacheKind.AST`` bucket — and never collide with the ts_ast
    walkers' payloads (whose namespaces are "defs" / "imports").
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
    """Replay a walker's JSON-able payload from ``CacheKind.AST`` or compute.

    ``compute`` returning ``None`` (parse failure) is NEVER cached — a
    later run in a healthier environment must get a fresh chance. Cache
    faults never break a scan (house cache law).
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
