"""Local-mode worker — process ONE job then exit.

Used during development to verify the dashboard ↔ engine ↔ pg-boss flow
end-to-end on the developer's laptop, BEFORE paying for a Fly deploy.

Usage
-----
1. Make sure your local dashboard is running (e.g. ``pnpm dev`` on
   port 3001) and reachable from this process.
2. Export the same env vars the production worker uses:

       export DATABASE_URL='postgres://...neon.../neondb?sslmode=require'
       export ANTHROPIC_API_KEY='sk-ant-...'
       export FAULTLINES_WORKER_TOKEN='<same as dashboard .env.local>'
       export FAULTLINES_DASHBOARD_URL='http://localhost:3001'
       export WORKER_QUEUE_NAME='scan-jobs-incremental'
       export SCAN_SCRATCH_DIR="$(mktemp -d)"

3. In the dashboard UI, click "Activate scan" on a repo. This places a
   job on the pg-boss queue.

4. Run this script:

       python -m worker.dev

   It will poll once, claim the job, run a full scan, POST the result
   back, and exit. Exits non-zero if no job is waiting (so you notice).

This deliberately processes a SINGLE job — re-run for each test.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import psycopg

from worker.main import (
    WorkerConfig,
    ack_job,
    fetch_next_job,
    nack_job,
    process_job,
)


def run_once() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    cfg = WorkerConfig.from_env()
    logger = logging.getLogger("faultlines.worker.dev")
    logger.info(
        "dev worker: claiming ONE job from queue=%s, dashboard=%s",
        cfg.queue_name,
        cfg.dashboard_url,
    )
    with psycopg.connect(cfg.database_url, autocommit=False) as conn:
        job = fetch_next_job(conn, cfg.queue_name)
        if not job:
            logger.error("no job waiting on queue %s — enqueue one from the UI first", cfg.queue_name)
            return 2
        job_id = job["id"]
        logger.info("claimed job %s — running scan", job_id)
        started = time.monotonic()
        try:
            output = process_job(cfg, job)
            ack_job(conn, job_id, output)
            elapsed = time.monotonic() - started
            logger.info("job %s ok in %.1fs (%d bytes)", job_id, elapsed, output.get("scan_bytes", 0))
            return 0
        except Exception as e:  # noqa: BLE001 — surface to operator
            logger.exception("job %s failed", job_id)
            nack_job(conn, job_id, repr(e))
            return 1


if __name__ == "__main__":
    sys.exit(run_once())
