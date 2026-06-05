"""Base-directory resolution for engine-local state.

The engine writes scan artifacts, run logs, and (for the default
filesystem cache backend) cache files under a single base directory.

Historically this was hardcoded to ``Path.home() / ".faultline"``.
On scale-to-zero hosted workers ``$HOME`` is shared / ephemeral and
per-tenant state on a shared host is a hazard, so the base dir is now
resolved from the ``FAULTLINES_RUN_DIR`` environment variable when the
worker sets it (typically ``tempfile.mkdtemp()`` per job), falling back
to ``~/.faultline`` only when the env is absent (dev / CLI / OSS).

This is the single source of truth — output writers, run-dir helpers,
and the filesystem cache backend all call ``faultline_base_dir()`` so
"where does the engine write" is decided in exactly one place.

No LLM. No network. Pure path resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Env var the hosted worker sets to a job-scoped temp dir so nothing
#: lands under ``$HOME``. Absent in dev → fall back to ``~/.faultline``.
RUN_DIR_ENV = "FAULTLINES_RUN_DIR"


def faultline_base_dir() -> Path:
    """Return the engine's base state directory.

    ``$FAULTLINES_RUN_DIR`` when set + non-empty, else
    ``~/.faultline``. The directory is NOT created here — callers
    create the specific subpaths they need.
    """
    override = os.environ.get(RUN_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".faultline"


__all__ = ["RUN_DIR_ENV", "faultline_base_dir"]
