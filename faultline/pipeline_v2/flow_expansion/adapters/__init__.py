"""Framework-specific adapters for Stage 3.5 flow expansion.

Each adapter is a thin module that contributes additional edge / node
detection on top of the universal T1 + T2 pipeline. The current
v1 ships only the Next.js adapter as functional; the rest are stubs
that expose the same interface for future expansion (entry-points wire-
up planned for Sprint 3 — out of scope here).
"""

from faultline.pipeline_v2.flow_expansion.adapters import (
    express,
    fastapi,
    nextjs,
    react,
)

__all__ = ["nextjs", "react", "express", "fastapi"]
