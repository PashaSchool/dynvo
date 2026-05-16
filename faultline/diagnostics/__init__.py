"""Per-stage JSON artifacts for scan diagnostics (Sprint 9d).

Every key pipeline stage writes a small JSON snapshot under
``<repo_root>/.faultline/logs/<slug>/<stage>.json``. The previous
scan's artifacts are rotated to ``logs_prev/<slug>/`` so a regression
can be diff'd against the last good run.

Default ON. Disable with ``FAULTLINE_NO_ARTIFACTS=1``.
"""

from faultline.diagnostics.artifacts import (
    ArtifactsLogger,
    artifacts_enabled,
    get_logger,
    init_scan,
)

__all__ = [
    "ArtifactsLogger",
    "artifacts_enabled",
    "get_logger",
    "init_scan",
]
