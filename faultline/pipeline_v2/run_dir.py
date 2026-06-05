"""Run-isolation helpers for pipeline-v2.

Each scan run is given a unique ``run_id`` and writes all stage
artifacts + logs under ``~/.faultline/logs/<slug>/<run_id>/``. A
``latest`` symlink in the slug directory is atomically swapped to
point at the most recent run after every scan, so debug commands
can always resolve ``~/.faultline/logs/<slug>/latest/03-stage-flows.json``
without knowing the timestamp.

Run-id format::

    <UTC-timestamp>-<git-head-sha[:8]>

E.g. ``20260519T103045Z-9565cffa``. When the repo has no git HEAD
(fixture repos, freshly-initialised dirs without commits), the SHA
component falls back to ``nogit``. When the caller passes an
explicit ``run_id`` (CLI ``--run-id custom``), we use it verbatim
as long as it's filesystem-safe.

No LLM. No network. Pure local-disk operations.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def repo_slug(repo_path: Path | str) -> str:
    """Kebab-cased dirname — matches ``stage_7_output._repo_slug``."""
    name = Path(repo_path).name
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "repo"


def slug_log_dir(repo_path: Path | str) -> Path:
    """Return ``<base>/logs/<slug>/``, creating it if needed.

    ``<base>`` resolves to ``$FAULTLINES_RUN_DIR`` when the worker sets
    it (job-scoped temp dir), else ``~/.faultline`` (dev). See
    ``faultline.cache.paths.faultline_base_dir``.
    """
    from faultline.cache.paths import faultline_base_dir

    target = faultline_base_dir() / "logs" / repo_slug(repo_path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _utc_compact_timestamp() -> str:
    """``YYYYMMDDTHHMMSSZ`` — filesystem-safe, sorts chronologically."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_head_short(repo_path: Path) -> str:
    """Return the first 8 chars of ``git HEAD``, or ``"nogit"``.

    Never raises — fixture repos without commits silently fall back.
    """
    try:
        from faultline.analyzer.git import load_repo

        repo = load_repo(str(repo_path))
        sha = repo.head.commit.hexsha
        return sha[:8] if sha else "nogit"
    except Exception as exc:  # noqa: BLE001 — best-effort tag, never fatal
        logger.debug("run_dir: no git HEAD for %s (%s)", repo_path, exc)
        return "nogit"


def generate_run_id(repo_path: Path | str) -> str:
    """Generate ``<ts>-<sha8>`` for this run.

    Format is stable per ``(timestamp, sha)`` — two scans of the
    same repo at the same wall-clock second on the same commit
    produce the same id (and would collide; the orchestrator is
    expected not to launch concurrent runs of the same repo).
    """
    return f"{_utc_compact_timestamp()}-{_git_head_short(Path(repo_path))}"


def sanitize_run_id(run_id: str) -> str:
    """Reject obviously-unsafe ``--run-id`` values from the CLI.

    Allows letters, digits, ``.``, ``_``, ``-``. Anything else is a
    bug — the run-id is used as a directory name and a symlink target.
    """
    if not run_id or not _SAFE_RUN_ID_RE.match(run_id):
        raise ValueError(
            f"run_id must match [A-Za-z0-9_.-]+, got: {run_id!r}",
        )
    return run_id


def run_artifact_dir(repo_path: Path | str, run_id: str) -> Path:
    """Return ``~/.faultline/logs/<slug>/<run_id>/``, creating it."""
    target = slug_log_dir(repo_path) / run_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def update_latest_symlink(repo_path: Path | str, run_id: str) -> Path | None:
    """Atomically point ``<slug>/latest`` at ``<run_id>``.

    Uses ``os.symlink`` to a temporary name then ``os.replace`` so
    the swap is atomic on POSIX. On platforms where symlinks aren't
    available (Windows without dev-mode), we log and return None
    rather than failing the scan.

    Returns the symlink path on success, or None if creation failed.
    """
    slug_dir = slug_log_dir(repo_path)
    link = slug_dir / "latest"
    tmp = slug_dir / f".latest.{os.getpid()}.tmp"
    # Remove any stale tmp from a crashed prior run.
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
    except OSError:
        pass
    try:
        os.symlink(run_id, tmp)
        os.replace(tmp, link)
        return link
    except (OSError, NotImplementedError) as exc:
        logger.warning(
            "run_dir: failed to update latest symlink for %s: %s",
            slug_dir, exc,
        )
        # Clean up tmp if it survived.
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None


__all__ = [
    "generate_run_id",
    "repo_slug",
    "run_artifact_dir",
    "sanitize_run_id",
    "slug_log_dir",
    "update_latest_symlink",
]
