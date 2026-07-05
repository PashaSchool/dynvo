"""Top-level scan-result cache — full-pipeline reproducibility short-circuit.

Why this exists
===============
``temperature=0`` on Anthropic is **not** bit-exact: the same prompt can
produce a slightly different completion run-to-run. Several LLM stages of
``pipeline_v2`` (Stage 3 flow detection, the 6.7b/6.7c user-flow stages,
and the Stage 8 product clusterer) are therefore non-deterministic across
runs even on an *unchanged* repo — a "fresh" re-scan of fastapi drifts
(48 vs 53 user-flows, 14 vs 16 product-features) although the deterministic
Layer 1 developer features are identical.

Rather than cache every LLM stage individually, this module caches the
**final FeatureMap** keyed on everything that determines it:

  * **repo content identity** — ``git rev-parse HEAD`` plus a hash of any
    dirty/uncommitted state (``git status --porcelain=v1`` + the diff of
    modified tracked files). A clean checkout at commit *X* hashes to *X*;
    a dirty tree hashes distinctly. Non-git dirs fall back to hashing the
    tree's source files.
  * **engine version** — pyproject / installed-distribution version.
  * **scan config signature** — model, days, subpath, max_tree_depth,
    llm_reconcile, feature_history, and the Stage-6.7d abstraction flags.
    Everything that changes output; NOTHING that varies per run
    (``run_id``, timestamps, output path, org/thread identity, cost).

Same ``(repo-state, engine-version, config)`` → the orchestrator replays
the **byte-identical** stored FeatureMap ($0, instant) and skips the whole
pipeline. Because the key excludes per-run values, it is a deterministic
reproducibility cache — not per-repo memory — and is ``rule-cold-scan`` safe.

Design contract
===============
  * **Opt-in.** ``FAULTLINE_SCAN_CACHE`` (default OFF). When off, the
    orchestrator never takes the cache path and behaviour is byte-identical
    to today.
  * **Bypass.** ``FAULTLINE_SCAN_CACHE_BYPASS=1`` forces a fresh scan (skip
    the HIT read) while STILL storing the fresh result — a cheap "refresh".
  * **Byte-exact.** We store and replay the *raw bytes* of the written
    FeatureMap file, never a re-serialised dict, so a HIT reproduces run A
    exactly (including run A's ``scan_meta`` timestamps).
  * **Robust.** Every read/write fault is swallowed (log + fall through to a
    normal scan). A corrupt / unparseable / partial entry is treated as a
    MISS. Writes are atomic (temp file + ``os.replace``) so a crashed write
    never leaves a partial entry that could be served. NEVER crashes a scan.

No LLM. No network. Pure local-disk + git.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from faultline.cache.backend import CacheKind, _safe_component
from faultline.cache.paths import faultline_base_dir

logger = logging.getLogger(__name__)

#: Opt-in gate. Empty / ``"0"`` → cache disabled (default). Any other value
#: (typically ``"1"``) → enabled.
ENV_ENABLE = "FAULTLINE_SCAN_CACHE"
#: Force a fresh scan: skip the HIT read but STILL store the result.
ENV_BYPASS = "FAULTLINE_SCAN_CACHE_BYPASS"
#: Stage-6.7d abstraction env flags — output-affecting, so part of the key.
ENV_6_7D_ABSTRACTION = "FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION"
ENV_6_7D_ABSTRACTION_MODEL = "FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL"

# Every env flag that gates a pipeline stage on/off and thus materially changes
# product_features[] / user_flows[]. Any of these toggled between two scans of
# the same tree MUST miss the cache (audit Bug 2 — else a toggle-and-rescan, the
# exact eval workflow the stale-cache rule forbids, serves a stale result). We
# store the RAW env value for each (unset vs "0" vs "1" all distinct) — safe
# over-invalidation beats a stale serve.
ENV_OUTPUT_FLAGS = (
    "FAULTLINE_SEED_SYSTEM_UFS",
    "FAULTLINE_STAGE_6_3_MEMBER_BACKFILL",
    "FAULTLINE_STAGE_8_6_NONSOURCE_DROP",
    "FAULTLINE_STAGE_8_6_5_SCAFFOLD_FILTER",
    "FAULTLINE_STAGE_8_6_7_DI_ATTRIBUTION",
    "FAULTLINE_STAGE_8_7_DESINK",
    "FAULTLINE_STAGE_8_8_SHARED_MEMBERS",
    "FAULTLINE_STAGE_8_9_SUBDECOMPOSE",
    "FAULTLINE_PF_ANCHOR_NAME_GUARD",
    "FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT",
    "FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP",
    "FAULTLINE_STAGE_6_7D_RESIDUAL_GUARD",
    "FAULTLINE_STAGE_6_7D_UF_RESHARE",
    "FAULTLINE_STAGE_6_7D_SHELL_ABSORB",
)

#: Bump when the KEY composition changes so old entries can't be served
#: against a new key layout (they simply won't match — silent invalidation).
KEY_SCHEMA_VERSION = 2

#: Directory / file-size guards for the non-git tree-hash fallback. Kept
#: scale-invariant (not tuned to any one repo) — they only bound work.
_NONGIT_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".next", "dist", "build", "target", ".turbo", "vendor",
    ".idea", ".vscode", "coverage", ".ruff_cache",
}
_NONGIT_MAX_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MiB


# ── gate helpers ─────────────────────────────────────────────────────────


def _flag(env: str) -> bool:
    return os.environ.get(env, "0").strip() not in ("", "0")


def is_enabled() -> bool:
    """``True`` when the operator opted into the scan-result cache."""
    return _flag(ENV_ENABLE)


def is_bypassed() -> bool:
    """``True`` when a forced-fresh scan was requested (still stores)."""
    return _flag(ENV_BYPASS)


# ── engine version ───────────────────────────────────────────────────────


def _pyproject_version() -> str:
    """Version from the nearest ``pyproject.toml`` (walk up from this file).

    Reflects the *code* version (1.39.0) even when the installed dist
    metadata is stale. Empty string on any failure.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                return ""
            proj = data.get("project")
            if isinstance(proj, dict):
                v = proj.get("version")
                if isinstance(v, str) and v:
                    return v
            return ""
    return ""


def engine_version() -> str:
    """Best-effort engine version for the cache key.

    Prefers pyproject (source-of-truth code version), then installed-dist
    metadata, then the module ``__version__``. Returns ``"0"`` only when
    everything fails — the key stays stable, it just loses version
    granularity (never crashes).
    """
    v = _pyproject_version()
    if v:
        return v
    try:
        import importlib.metadata as md

        for dist in ("dynvo", "faultlines", "faultline"):
            try:
                return md.version(dist)
            except md.PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001 — metadata is best-effort
        pass
    try:
        from faultline import __version__

        return __version__ or "0"
    except Exception:  # noqa: BLE001
        return "0"


# ── repo content identity ────────────────────────────────────────────────


def _git(repo_path: Path, *args: str, binary: bool = False) -> Any | None:
    """Run a git command scoped to ``repo_path``; ``None`` on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            check=True,
            timeout=30,
            text=not binary,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("scan_result_cache: git %s failed (%s)", args, exc)
        return None
    return out.stdout


def _is_git_repo(repo_path: Path) -> bool:
    res = _git(repo_path, "rev-parse", "--is-inside-work-tree")
    return isinstance(res, str) and res.strip() == "true"


def _dirty_hash(repo_path: Path) -> str:
    """Hash of uncommitted state; empty string for a clean tree.

    Combines ``git status --porcelain=v1`` (untracked + modified + staged
    status lines) with ``git diff HEAD`` (the actual content diff of tracked
    modifications, staged + unstaged). A clean checkout → both empty → ``""``.
    """
    # ``--untracked-files=all`` expands untracked DIRECTORIES into individual
    # file entries so each new file gets its own ``?? path`` line.
    status = _git(repo_path, "status", "--porcelain=v1", "--untracked-files=all")
    diff = _git(repo_path, "diff", "HEAD")
    status_s = status or ""
    diff_s = diff or ""
    # Untracked NEW files appear as "?? path" in status but contribute NOTHING to
    # ``git diff HEAD`` — so their CONTENT is invisible to the key unless hashed
    # explicitly (audit Bug 1): editing an untracked file would keep the key
    # stable and serve a stale result.
    untracked_s = _untracked_content_hash(repo_path, status_s)
    if not status_s.strip() and not diff_s.strip() and not untracked_s:
        return ""
    h = hashlib.sha256()
    h.update(b"status\0")
    h.update(status_s.encode("utf-8", "replace"))
    h.update(b"\0diff\0")
    h.update(diff_s.encode("utf-8", "replace"))
    h.update(b"\0untracked\0")
    h.update(untracked_s.encode("utf-8"))
    return h.hexdigest()


def _untracked_content_hash(repo_path: Path, status_s: str) -> str:
    """Content hash of untracked (``?? path``) files listed in ``status_s``.
    Deterministic (sorted), bounded (2 MiB/file; huge files hashed by size)."""
    paths: list[str] = []
    for line in status_s.splitlines():
        if line.startswith("?? "):
            rel = line[3:].strip()
            if rel.startswith('"') and rel.endswith('"'):  # git quotes special names
                rel = rel[1:-1]
            paths.append(rel)
    if not paths:
        return ""
    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        fp = repo_path / rel
        try:
            if fp.is_file():
                size = fp.stat().st_size
                if size <= 2 * 1024 * 1024:
                    h.update(fp.read_bytes())
                else:
                    h.update(f"size:{size}".encode())
        except OSError:
            h.update(b"\0unreadable")
        h.update(b"\0")
    return h.hexdigest()


def _nongit_tree_hash(repo_path: Path) -> str:
    """Deterministic hash of a non-git tree's source files.

    Walks regular files (skipping heavy build/vendor dirs and oversized
    files), hashing sorted ``(relpath, sha256(content))`` pairs. Stable for
    an unchanged tree, distinct when any hashed file changes.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repo_path)
        if any(part in _NONGIT_SKIP_DIRS for part in rel.parts):
            continue
        try:
            if path.stat().st_size > _NONGIT_MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        entries.append((rel.as_posix(), hashlib.sha256(data).hexdigest()))
    h = hashlib.sha256()
    for rel, digest in entries:
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def repo_content_identity(repo_path: Path | str) -> dict[str, str]:
    """Return a stable identity for the repo's current content.

    Git tree: ``{"vcs": "git", "head": <sha>, "dirty": <hash-or-empty>}`` —
    a clean checkout at commit X always maps to ``(X, "")``. Non-git tree:
    ``{"vcs": "none", "head": "", "dirty": <tree-hash>}``.
    """
    repo_path = Path(repo_path)
    if _is_git_repo(repo_path):
        head = _git(repo_path, "rev-parse", "HEAD")
        return {
            "vcs": "git",
            "head": (head or "").strip(),
            "dirty": _dirty_hash(repo_path),
        }
    return {"vcs": "none", "head": "", "dirty": _nongit_tree_hash(repo_path)}


# ── config signature + key ───────────────────────────────────────────────


def scan_config_signature(
    *,
    model: str,
    days: int,
    subpath: str | None,
    max_tree_depth: int | None,
    llm_reconcile: bool,
    feature_history: bool,
) -> dict[str, Any]:
    """Everything about the run configuration that changes scan output.

    ``model`` should be the RESOLVED model id (so two aliases for the same
    model share a cache entry). The Stage-6.7d abstraction env flags are
    read here — they materially change ``product_features`` / ``user_flows``.
    Deliberately EXCLUDES run-varying values (run_id, out_path, timestamps,
    org_id, thread identity, cost caps).
    """
    return {
        "model": model or "",
        "days": int(days),
        "subpath": subpath or "",
        "max_tree_depth": (
            int(max_tree_depth) if max_tree_depth is not None else None
        ),
        "llm_reconcile": bool(llm_reconcile),
        "feature_history": bool(feature_history),
        "stage_6_7d_abstraction": _flag(ENV_6_7D_ABSTRACTION),
        "stage_6_7d_abstraction_model": os.environ.get(
            ENV_6_7D_ABSTRACTION_MODEL, "",
        ).strip(),
        # All stage-gating env flags (raw values) — see ENV_OUTPUT_FLAGS.
        "stage_flags": {
            f: os.environ.get(f, "").strip() for f in ENV_OUTPUT_FLAGS
        },
    }


def compute_scan_cache_key(
    repo_path: Path | str,
    *,
    engine_version: str,
    config_signature: dict[str, Any],
) -> str:
    """sha256 over repo identity + engine version + config signature.

    Stable for identical inputs; changes when any tracked file, the config,
    or the engine version changes.
    """
    identity = repo_content_identity(repo_path)
    payload = {
        "key_schema": KEY_SCHEMA_VERSION,
        "identity": identity,
        "engine_version": engine_version,
        "config": config_signature,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── storage (raw, byte-exact) ────────────────────────────────────────────


def _scan_cache_path(key: str, *, base_dir: Path | None = None) -> Path:
    """Resolve ``<base>/scan-cache/<safe-key>.json`` (matches CacheKind)."""
    base = base_dir if base_dir is not None else faultline_base_dir()
    return Path(base) / "scan-cache" / f"{_safe_component(key)}.json"


def load_cached_scan(key: str, *, base_dir: Path | None = None) -> str | None:
    """Return the raw stored FeatureMap TEXT, or ``None`` on miss/fault.

    A missing file, an OS error, or an unparseable (corrupt / partial) body
    all count as a MISS — we validate the JSON parses before returning so a
    truncated entry is NEVER served. The raw text (not the parsed dict) is
    returned so the caller can reproduce byte-identical output.
    """
    path = _scan_cache_path(key, base_dir=base_dir)
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("scan_result_cache: read failed %s (%s) — miss", path, exc)
        return None
    try:
        json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "scan_result_cache: corrupt entry %s (%s) — treating as miss",
            path, exc,
        )
        return None
    return raw


def store_scan_result(
    key: str, featuremap_path: Path | str, *, base_dir: Path | None = None,
) -> bool:
    """Copy the written FeatureMap file verbatim into the cache.

    Reads the raw bytes of ``featuremap_path`` and writes them atomically
    (temp file + ``os.replace``) so a crashed write never leaves a partial
    entry. Returns ``True`` on success, ``False`` on any fault (never raises).
    """
    dst = _scan_cache_path(key, base_dir=base_dir)
    try:
        raw = Path(featuremap_path).read_bytes()
    except OSError as exc:
        logger.warning(
            "scan_result_cache: cannot read result %s (%s) — not cached",
            featuremap_path, exc,
        )
        return False
    tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        os.replace(tmp, dst)
    except OSError as exc:
        logger.warning("scan_result_cache: write failed %s (%s)", dst, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


# ── HIT serving ──────────────────────────────────────────────────────────


def _default_out_path(repo_path: Path | str) -> Path:
    """Mirror ``output.writer.write_feature_map``'s default naming."""
    slug = re.sub(r"[^a-z0-9]+", "-", Path(repo_path).name.lower()).strip("-")
    slug = slug or "repo"
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    return faultline_base_dir() / f"feature-map-{slug}-{ts}.json"


def serve_from_cache(
    raw_text: str,
    *,
    key: str,
    repo_path: Path | str,
    out_path: Path | str | None,
) -> dict[str, Any] | None:
    """Write the cached bytes to the requested path and build the return dict.

    Returns the same ``{"path": ..., **scan_meta}`` shape ``run_pipeline_v2``
    yields, with a ``scan_cache`` marker flagging the HIT. Returns ``None`` on
    any fault so the orchestrator falls through to a normal scan.
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return None
    target = Path(out_path).resolve() if out_path else _default_out_path(repo_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Byte-exact replay of run A's file.
        target.write_text(raw_text, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "scan_result_cache: could not write served result to %s (%s) "
            "— falling through to a normal scan", target, exc,
        )
        return None
    meta = dict(data.get("scan_meta") or {})
    meta["scan_cache"] = {
        "enabled": True,
        "served_from_cache": True,
        "stored": False,
        "key": key,
    }
    logger.info(
        "scan_result_cache: HIT — scan served from cache (key=%s) → %s ($0)",
        key[:12], target,
    )
    return {"path": str(target), **meta}


__all__ = [
    "ENV_ENABLE",
    "ENV_BYPASS",
    "KEY_SCHEMA_VERSION",
    "is_enabled",
    "is_bypassed",
    "engine_version",
    "repo_content_identity",
    "scan_config_signature",
    "compute_scan_cache_key",
    "load_cached_scan",
    "store_scan_result",
    "serve_from_cache",
]
