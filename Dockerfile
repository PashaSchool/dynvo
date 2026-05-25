# syntax=docker/dockerfile:1.7
# Faultlines scan worker — multi-stage build
# Stage 1 builds the wheel; Stage 2 is the slim runtime image deployed to Fly Machines.
#
# Build:   docker build -t faultlines-engine:dev .
# Run:     docker run --rm -e DATABASE_URL=... -e ANTHROPIC_API_KEY=... \
#                  -e FAULTLINES_WORKER_TOKEN=... -e FAULTLINES_DASHBOARD_URL=... \
#                  faultlines-engine:dev

############################
# Stage 1 — build the wheel
############################
FROM python:3.13-slim AS builder

WORKDIR /build

# Only the bits needed for `python -m build`. Keeping this layer
# narrow improves cache hit rate on engine-code-only changes.
COPY pyproject.toml README.md LICENSE ./
COPY faultline ./faultline

RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /build/dist

############################
# Stage 2 — runtime
############################
FROM python:3.13-slim AS runtime

# System deps:
#   git        — `git clone` + per-file `git log` used by Stage 6 enrichment
#   curl       — diagnostics, health probes
#   libgit2    — gitpython falls back to libgit2 for some operations
#   ca-certs   — required for HTTPS clones from GitHub
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        curl \
        libgit2-dev \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the engine wheel + worker-only runtime deps.
# psycopg[binary] is pulled in here (not in pyproject) because it is a
# WORKER concern, not an engine concern — the engine itself never
# touches Postgres.
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir \
        /tmp/*.whl \
        'psycopg[binary]>=3.2,<4' \
        'httpx>=0.27,<1' \
 && rm /tmp/*.whl

# Worker entrypoint code lives outside the published wheel —
# it is a deployment concern, not part of the OSS engine surface.
COPY worker /app/worker

# Scan scratch space. fly.toml mounts a volume here.
RUN mkdir -p /tmp/scans \
 && useradd -m -u 1001 faultlines \
 && chown -R faultlines:faultlines /app /tmp/scans

USER faultlines

# Healthcheck — verifies the wheel actually imports.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import faultline; print('ok')" || exit 1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FAULTLINES_PRODUCTION=1 \
    SCAN_SCRATCH_DIR=/tmp/scans

CMD ["python", "-m", "worker.main"]
