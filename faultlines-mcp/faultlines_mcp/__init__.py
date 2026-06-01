"""Faultlines MCP server.

Thin MCP-protocol surface over a Faultlines feature-map JSON. Lives in
its own top-level folder (separate from the engine package ``faultline``)
so it can be released as a standalone package on PyPI / npm without
touching the OSS engine. Fully engine-independent: there are NO
``faultline`` imports anywhere in this package. Every tool reads the
feature-map JSON directly; optional auto-refresh shells out to the
``faultlines`` CLI via subprocess.
"""

from __future__ import annotations

from faultlines_mcp.server import main

__all__ = ["main"]
