"""Faultlines MCP server.

Thin MCP-protocol surface over a Faultlines feature-map JSON. Lives in
its own top-level folder (separate from the engine package ``faultline``)
so it can later be released as a standalone package on PyPI / npm without
touching the OSS engine. Engine-boundary today is intentionally tiny —
only one engine call (``faultline.impact.risk.predict_impact``); the rest
of MCP reads the feature-map JSON directly.
"""

from __future__ import annotations

from faultlines_mcp.server import main

__all__ = ["main"]
