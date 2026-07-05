"""Stage 6.97 — deterministic feature-level LOC ($0, output layer).

Operator invariant (validate_scan.py I2): «фіча без коду = грубий баг» —
every dev feature with ≥1 real owned source file must carry a nonzero
LOC. Before this stage the ONLY LOC surface was the flow rollup
(``flows[].nodes[].lines``), so features whose files never became flows
(package anchors, flowless route groups, config features) showed "·" on
the dashboard even with dozens of owned paths.

This stage emits a flat ``loc`` field:

* ``developer_features[].loc`` — sum of per-file line counts over the
  feature's OWNED ``paths`` (directories are walked recursively).
* ``product_features[].loc``  — rollup over member developer features,
  each physical file counted ONCE even when shared between members
  (falls back to the PF's own ``paths`` when it has no members).

Counting convention (REUSED, not reinvented):
``faultline.tools.line_completeness.executable_lines`` — the engine's
canonical per-language LOC scanner (comment/blank-aware for the hash- &
C-comment families; non-blank fallback for any other text file, so
YAML/JSON/config-as-product features still get a nonzero count).

Excluded from the COUNT (the paths themselves stay listed on the
feature — this is a metric, not a membership strip):

* test files/dirs      — :func:`stage_6_9_test_strip.is_test_path`
* generated code       — :func:`stage_6_9b_generated_strip.is_generated_path`
* lockfiles            — ``package-lock.json`` / ``pnpm-lock.yaml`` /
  ``yarn.lock`` / ``poetry.lock`` / ``uv.lock`` / ``Cargo.lock`` /
  ``composer.lock`` / ``Gemfile.lock`` / ``bun.lockb`` / ``go.sum``
* minified/bundled     — ``*.min.js`` / ``*.min.css`` / ``*.map``
* binary/media         — extension denylist + NUL-byte sniff on the
  first 8 KiB (covers extensionless binaries)
* files missing on disk (historical paths from the git window)

Guarantee: ``loc > 0`` whenever ``paths`` contains at least one
non-empty, non-test, non-generated, non-binary file.

Per-file counts are cached for the whole scan (shared files and the
PF rollup never re-read a file). Deterministic, no LLM, no network.
Disable via ``FAULTLINE_STAGE_6_97_FEATURE_LOC=0`` (default ON).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path
from faultline.pipeline_v2.stage_6_9b_generated_strip import is_generated_path

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "STAGE_6_97_ENV_FLAG",
    "stage_6_97_enabled",
    "apply_feature_loc",
    "count_file_loc",
]

STAGE_6_97_ENV_FLAG = "FAULTLINE_STAGE_6_97_FEATURE_LOC"

#: Lockfiles — machine-written dependency snapshots, never product code.
_LOCKFILE_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "bun.lockb",
    "go.sum",
    "flake.lock",
}

#: Binary / media / bundle extensions — unreadable or non-authored.
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
    ".svgz", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".avi",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".wasm", ".o", ".a",
    ".jar", ".class", ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3",
    ".lockb", ".node", ".heic", ".psd", ".ai",
}

_SNIFF_BYTES = 8192


def stage_6_97_enabled() -> bool:
    """Default ON; ``FAULTLINE_STAGE_6_97_FEATURE_LOC=0`` disables."""
    return os.environ.get(STAGE_6_97_ENV_FLAG, "1").strip() not in {
        "0", "false", "False",
    }


# ── Per-file counting ───────────────────────────────────────────────────


def _is_excluded_name(path: str) -> bool:
    """Lockfile / minified / sourcemap / binary-extension exclusion."""
    base = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if base in _LOCKFILE_NAMES:
        return True
    if base.endswith((".min.js", ".min.css", ".min.mjs", ".map")):
        return True
    ext = os.path.splitext(base)[1]
    return ext in _BINARY_EXTS


def count_file_loc(abs_path: Path, rel_path: str) -> int:
    """Line count for ONE file per the engine's LOC convention; 0 when
    the file is excluded (test / generated / lockfile / binary / missing
    / empty)."""
    if is_test_path(rel_path) or is_generated_path(rel_path):
        return 0
    if _is_excluded_name(rel_path):
        return 0
    try:
        with open(abs_path, "rb") as fp:
            head = fp.read(_SNIFF_BYTES)
    except OSError:
        return 0
    if b"\x00" in head:  # NUL sniff — extensionless binaries
        return 0
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    # Late import: tools ← pipeline_v2 is the existing dependency
    # direction for the strip predicates; the LOC scanner lives in
    # tools and imports stage_6_9b, so import at call time to keep
    # module-import order cycle-free.
    from faultline.tools.line_completeness import executable_lines

    ext = os.path.splitext(rel_path)[1].lower()
    return len(executable_lines(text, ext))


def _iter_dir_files(root: Path) -> Iterable[Path]:
    """Sorted recursive file walk, skipping VCS/vendor dirs."""
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _path_loc(
    repo_root: Path,
    rel: str,
    cache: dict[str, int],
) -> int:
    """LOC of one feature path (file OR directory), memoised."""
    norm = rel.replace(os.sep, "/").strip("/")
    if norm in cache:
        return cache[norm]
    abs_path = repo_root / norm
    total = 0
    if abs_path.is_dir():
        for child in _iter_dir_files(abs_path):
            child_rel = child.relative_to(repo_root).as_posix()
            if child_rel in cache:
                total += cache[child_rel]
                continue
            n = count_file_loc(child, child_rel)
            cache[child_rel] = n
            total += n
    else:
        total = count_file_loc(abs_path, norm)
    cache[norm] = total
    return total


# ── Stage body ──────────────────────────────────────────────────────────


def apply_feature_loc(
    features: list["Feature"],
    product_features: list["Feature"] | None,
    repo_root: Path | str,
) -> dict[str, Any]:
    """Stamp ``loc`` on every developer + product feature IN PLACE.

    Returns the telemetry dict for ``scan_meta['feature_loc']``.
    """
    root = Path(repo_root)
    cache: dict[str, int] = {}
    # dev-feature name → {path: loc} of its COUNTED paths (PF dedup input).
    dev_counted: dict[str, dict[str, int]] = {}

    zero_loc_with_paths = 0
    for feat in features:
        per_path: dict[str, int] = {}
        for raw in (feat.paths or []):
            rel = str(raw).replace(os.sep, "/").strip("/")
            if not rel:
                continue
            n = _path_loc(root, rel, cache)
            if n > 0:
                per_path[rel] = n
        feat.loc = sum(per_path.values())
        dev_counted[feat.name] = per_path
        if feat.paths and feat.loc == 0:
            zero_loc_with_paths += 1

    pf_total = 0
    for pf in (product_features or []):
        merged: dict[str, int] = {}
        for feat in features:
            if feat.product_feature_id == pf.name:
                merged.update(dev_counted.get(feat.name, {}))
        if not merged:
            # PF with no dev members — fall back to its own paths.
            for raw in (pf.paths or []):
                rel = str(raw).replace(os.sep, "/").strip("/")
                if not rel:
                    continue
                n = _path_loc(root, rel, cache)
                if n > 0:
                    merged[rel] = n
        pf.loc = sum(merged.values())
        pf_total += 1

    counted_files = sum(1 for v in cache.values() if v > 0)
    return {
        "enabled": True,
        "features_total": len(features),
        "features_with_loc": sum(1 for f in features if (f.loc or 0) > 0),
        "features_zero_loc_with_paths": zero_loc_with_paths,
        "product_features_total": pf_total,
        "paths_indexed": len(cache),
        "files_counted": counted_files,
    }
