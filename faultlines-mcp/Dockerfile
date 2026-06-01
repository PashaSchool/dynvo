# Faultlines MCP HTTP service — the proxy target the hosted dashboard calls.
# Installs the package WITH the `http` extra (fastapi + uvicorn) and runs the
# `faultlines-mcp-serve` console script (uvicorn on $PORT, default 8080).
FROM python:3.13-slim

WORKDIR /app

# Copy package sources + metadata, then install with the HTTP extra. Pip reads
# pyproject.toml; the `.[http]` selector pulls fastapi + uvicorn[standard].
COPY pyproject.toml README.md ./
COPY faultlines_mcp ./faultlines_mcp
RUN pip install --no-cache-dir ".[http]"

# Fly routes to this internal port; main() honours $PORT (default 8080).
ENV PORT=8080
EXPOSE 8080

CMD ["faultlines-mcp-serve"]
