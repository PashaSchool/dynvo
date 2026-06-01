"""Faultlines MCP — HTTP service mode.

Serves the SAME 13 tools as the local stdio server, but as a tiny stateless
HTTP API the hosted dashboard can proxy to. The dashboard authenticates the
end user, resolves + decrypts the org's latest scan, optionally pre-fetches
runtime (Sentry/PostHog) arrays, then POSTs ``{tool, args, scan, runtime?}``
here. This service holds NO secrets and NO scans of its own — every request
carries the data it needs.

The tool LOGIC is not duplicated here: every endpoint dispatches through
:data:`faultlines_mcp.core.TOOLS`, the single source of truth shared with the
stdio server.

This module imports FastAPI/uvicorn, which are an OPTIONAL extra
(``faultlines-mcp[http]``). The stdio path never imports it, so a plain
``pip install faultlines-mcp`` stays lean.

Endpoints:
    POST /call    {tool, args, scan, runtime?} -> 200 {summary, details} | 4xx {error}
    GET  /tools   -> {tools: [{name, description, inputSchema}]}  (the 13)
    GET  /health  -> 200 {status: "ok"}

Auth:
    If ``MCP_SERVICE_TOKEN`` is set in the environment, ``/call`` and ``/tools``
    require ``Authorization: Bearer <token>``. If unset, auth is skipped (local
    dev only — production always sets the secret).

Run:
    faultlines-mcp-serve            # uvicorn on $PORT (default 8080), host 0.0.0.0
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from faultlines_mcp import core


class CallRequest(BaseModel):
    """Body for ``POST /call``.

    ``scan`` is the already-decrypted feature-map / Scan dict; ``runtime`` (if
    present) carries pre-fetched Sentry/PostHog arrays the runtime tools read.
    """

    tool: str = Field(..., description="One of the 13 registered tool names.")
    args: dict[str, Any] = Field(default_factory=dict, description="Tool arguments.")
    scan: dict[str, Any] = Field(..., description="Already-decrypted feature-map / Scan dict.")
    runtime: dict[str, Any] | None = Field(
        default=None,
        description="Optional pre-fetched runtime arrays (errors / pageviews).",
    )


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency enforcing the shared bearer token.

    No-op when ``MCP_SERVICE_TOKEN`` is unset (local dev). When set, the
    ``Authorization`` header must be exactly ``Bearer <token>``.
    """
    token = os.environ.get("MCP_SERVICE_TOKEN")
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


def create_app() -> FastAPI:
    """Build the FastAPI app. Factored out so tests can instantiate it without
    starting uvicorn."""
    app = FastAPI(
        title="faultlines-mcp-service",
        description="HTTP proxy target for the unified 13-tool Faultlines MCP.",
        version="0.1.0",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe — no auth, used by the Fly health check."""
        return {"status": "ok"}

    @app.get("/tools")
    def tools(_: None = Depends(_require_auth)) -> dict[str, Any]:
        """List the 13 tools with their descriptions + input schemas.

        Mirrors the stdio server's ``tools/list``; the dashboard proxies this
        (or a static mirror) for its own ``tools/list``.
        """
        return {"tools": core.list_tool_specs()}

    @app.post("/call")
    def call(req: CallRequest, _: None = Depends(_require_auth)) -> dict[str, Any]:
        """Dispatch one tool call through the shared registry.

        Returns the pure fn's ``{summary, details}``. Unknown tool names →
        400; an unexpected fn error → 500 (with the message, no traceback).
        """
        if req.tool not in core.TOOLS:
            raise HTTPException(status_code=400, detail=f"Unknown tool '{req.tool}'")
        try:
            return core.call_tool(req.tool, req.scan, req.args, req.runtime)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as a clean 500
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


# Module-level app so ``uvicorn faultlines_mcp.http_service:app`` works too.
app = create_app()


def main() -> None:
    """Entry point for the ``faultlines-mcp-serve`` console script.

    Starts uvicorn on ``$PORT`` (default 8080), binding ``0.0.0.0`` so it's
    reachable inside a container / Fly machine.
    """
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
