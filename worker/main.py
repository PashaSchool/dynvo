"""Faultlines scan worker.

Long-running container that polls a ``pg-boss`` job queue for scan
requests, runs ``faultlines scan-v2`` against the requested commit,
and POSTs the result back to the dashboard backend, which performs
all KMS-encrypted writes to the customer-data store.

Two-phase design
----------------
1. ``poll_and_process`` blocks on the next pg-boss job (raw SQL — pg-boss
   is a JS library but its schema is plain Postgres so any client works).
2. For each job we clone the repo into ``$SCAN_SCRATCH_DIR`` (tmpfs-backed
   on Fly), run pipeline_v2, and POST the JSON blob to the dashboard.
   The worker NEVER writes to the customer-data DB directly — that is
   the dashboard's job, behind the worker-token auth boundary.

Cold-scan invariant
-------------------
The worker holds no scan state across iterations. The scratch dir is
wiped between jobs, the engine runs against a freshly cloned working
tree, and incremental scans receive their ``base-scan-path`` over the
wire from the dashboard. This respects the cold-scan rule
(``rule-cold-scan``) — every scan is a fresh X-ray.

Queue payload contract (camelCase, matches ``apps/dashboard/src/lib/queue.ts``)
-----------------------------------------------------------------------------
The ``data`` jsonb of each pg-boss job is the ``ScanJobPayload`` shape:

    {
      "jobId":              "<uuid?>",
      "orgId":              "<uuid>",
      "repoId":             "<uuid>",
      "commitSha":          "<sha | 'HEAD'>",
      "sinceCommit":        "<sha | null>",
      "isFullScan":         true | false,
      "branch":             "<string>",
      "prNumber":           <int | null>,
      "installationToken":  "<gh installation token | null>",
      "repoFullName":       "owner/name",
      "enqueuedAt":         "<iso datetime>"
    }

If ``commitSha == 'HEAD'`` we resolve to a real SHA via ``git rev-parse HEAD``
inside the cloned worktree before scanning, then echo the resolved SHA back
to the dashboard as ``commit_sha`` in the POST body.

Environment
-----------
DATABASE_URL                Postgres URL — used ONLY for pg-boss queue ops.
ANTHROPIC_API_KEY           Forwarded into the engine subprocess.
FAULTLINES_WORKER_TOKEN     Shared secret presented to the dashboard on each
                            scan-complete POST.
FAULTLINES_DASHBOARD_URL    e.g. ``https://app.faultlines.dev``.
SCAN_SCRATCH_DIR            Defaults to ``/tmp/scans`` (matches Dockerfile).
WORKER_TIMEOUT_SEC          Per-job hard limit. Default 1800 (30 min).
WORKER_POLL_INTERVAL_SEC    Sleep between empty polls. Default 5.
WORKER_QUEUE_NAME           pg-boss queue name. Default ``scan-jobs-incremental``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("faultlines.worker")

# Engine version is read from the installed wheel at startup — see ``_engine_version``.
ENGINE_VERSION_UNKNOWN = "unknown"


# --------------------------------------------------------------------- config


@dataclass(frozen=True)
class WorkerConfig:
    """All env-derived config, resolved once at startup."""

    database_url: str
    dashboard_url: str
    worker_token: str
    anthropic_api_key: str
    scan_scratch_dir: Path
    queue_name: str
    timeout_sec: int
    poll_interval_sec: int

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        missing = [
            k
            for k in (
                "DATABASE_URL",
                "FAULTLINES_DASHBOARD_URL",
                "FAULTLINES_WORKER_TOKEN",
                "ANTHROPIC_API_KEY",
            )
            if not os.environ.get(k)
        ]
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
        return cls(
            database_url=os.environ["DATABASE_URL"],
            dashboard_url=os.environ["FAULTLINES_DASHBOARD_URL"].rstrip("/"),
            worker_token=os.environ["FAULTLINES_WORKER_TOKEN"],
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            scan_scratch_dir=Path(os.environ.get("SCAN_SCRATCH_DIR", "/tmp/scans")),
            queue_name=os.environ.get("WORKER_QUEUE_NAME", "scan-jobs-incremental"),
            timeout_sec=int(os.environ.get("WORKER_TIMEOUT_SEC", "1800")),
            poll_interval_sec=int(os.environ.get("WORKER_POLL_INTERVAL_SEC", "5")),
        )


# ------------------------------------------------------------------ pg-boss IO
#
# pg-boss schema lives in the ``pgboss`` schema by default. The columns we
# touch are stable across pg-boss 9.x and 10.x:
#
#     id            uuid
#     name          text         -- queue name
#     data          jsonb        -- our payload (see ScanJobPayload above)
#     state         text         -- 'created' | 'active' | 'completed' | 'failed' | ...
#     retry_count   int
#     started_on    timestamptz
#     completed_on  timestamptz
#     output        jsonb        -- result blob OR error info
#
# We deliberately use SKIP LOCKED so multiple worker replicas can poll the
# same queue concurrently without stepping on each other.

_FETCH_NEXT_SQL = """
UPDATE pgboss.job
   SET state = 'active',
       started_on = now(),
       retry_count = retry_count + (CASE WHEN state = 'retry' THEN 1 ELSE 0 END)
 WHERE id = (
   SELECT id FROM pgboss.job
    WHERE name = %s
      AND state < 'active'
      AND start_after < now()
    ORDER BY priority DESC, created_on ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
 )
RETURNING id, name, data, retry_count;
"""

_ACK_SQL = """
UPDATE pgboss.job
   SET state = 'completed',
       completed_on = now(),
       output = %s::jsonb
 WHERE id = %s;
"""

_NACK_SQL = """
UPDATE pgboss.job
   SET state = CASE WHEN retry_count >= retry_limit THEN 'failed' ELSE 'retry' END,
       completed_on = CASE WHEN retry_count >= retry_limit THEN now() ELSE NULL END,
       start_after = now() + (retry_delay || ' seconds')::interval,
       output = %s::jsonb
 WHERE id = %s;
"""


def fetch_next_job(conn: psycopg.Connection, queue_name: str) -> Optional[dict[str, Any]]:
    """Atomically claim the next pending job for ``queue_name``."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FETCH_NEXT_SQL, (queue_name,))
        row = cur.fetchone()
        conn.commit()
        return row


def ack_job(conn: psycopg.Connection, job_id: str, output: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(_ACK_SQL, (json.dumps(output), job_id))
        conn.commit()


def nack_job(conn: psycopg.Connection, job_id: str, error: str) -> None:
    payload = {"error": error}
    with conn.cursor() as cur:
        cur.execute(_NACK_SQL, (json.dumps(payload), job_id))
        conn.commit()


# ----------------------------------------------------------------- dashboard IO


def post_scan_to_dashboard(
    cfg: WorkerConfig,
    *,
    job_id: str,
    org_id: str,
    repo_id: str,
    commit_sha: str,
    parent_commit_sha: Optional[str],
    branch: str,
    is_full_scan: bool,
    engine_version: str,
    scan_meta: dict[str, Any],
    features_summary: list[dict[str, Any]],
    scan_blob: bytes,
    scan_duration_ms: int,
    llm_cost_cents: int,
) -> dict[str, Any]:
    """POST scan results to the dashboard's internal ingest endpoint.

    Matches the JSON body shape declared in
    ``apps/dashboard/src/app/api/internal/scan-complete/route.ts``:
    ``scan_blob`` is base64-encoded inside a JSON envelope, NOT raw
    octet-stream. We swapped from octet-stream to JSON during D3 audit
    because the dashboard zod schema requires all metadata in the body.
    """
    url = f"{cfg.dashboard_url}/api/internal/scan-complete"
    body = {
        "job_id": job_id,
        "org_id": org_id,
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "parent_commit_sha": parent_commit_sha,
        "branch": branch,
        "is_full_scan": is_full_scan,
        "engine_version": engine_version,
        "scan_meta": scan_meta,
        "features_summary": features_summary,
        "scan_duration_ms": scan_duration_ms,
        "llm_cost_cents": llm_cost_cents,
        "scan_blob": base64.b64encode(scan_blob).decode("ascii"),
    }
    headers = {
        "Authorization": f"Bearer {cfg.worker_token}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()


def fetch_base_scan(
    cfg: WorkerConfig,
    *,
    org_id: str,
    repo_id: str,
    base_commit_sha: str,
    dest: Path,
) -> Path:
    """Fetch the scan JSON for ``base_commit_sha`` from the dashboard.

    Uses query params (NOT headers) to match the scan-blob route handler.
    Response is JSON whose ``scan_data`` field holds the actual engine
    output; we write just that subtree to disk so the engine sees the
    same shape it produced last time.
    """
    url = f"{cfg.dashboard_url}/api/internal/scan-blob"
    headers = {"Authorization": f"Bearer {cfg.worker_token}"}
    params = {
        "org_id": org_id,
        "repo_id": repo_id,
        "commit_sha": base_commit_sha,
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
    # ``scan_data`` is the original engine JSON; everything else is dashboard
    # metadata. Engine expects to be handed only the engine-shaped subtree.
    scan_data = payload.get("scan_data") if isinstance(payload, dict) else None
    if scan_data is None:
        # Older dashboards may return the engine JSON directly — accept both.
        scan_data = payload
    dest.write_text(json.dumps(scan_data))
    return dest


# ----------------------------------------------------------------- job runner


def _resolve_clone_url(data: dict[str, Any]) -> str:
    """Build an authenticated HTTPS clone URL from queue payload.

    Prefers ``installationToken`` + ``repoFullName`` (the
    dashboard-canonical pair). Falls back to ``repoCloneUrl`` if provided
    raw (used by local dev to bypass GitHub).
    """
    raw = data.get("repoCloneUrl") or data.get("repo_clone_url")
    if isinstance(raw, str) and raw:
        return raw
    token = data.get("installationToken")
    full_name = data.get("repoFullName")
    if not token or not full_name:
        raise ValueError(
            "queue payload missing both repoCloneUrl and "
            "(installationToken + repoFullName)"
        )
    return f"https://x-access-token:{token}@github.com/{full_name}.git"


def process_job(cfg: WorkerConfig, job: dict[str, Any]) -> dict[str, Any]:
    """Clone, scan, POST. Returns the dashboard's ack payload."""
    data = job["data"] or {}
    # Queue payload is camelCase per ScanJobPayload zod schema.
    org_id: str = data["orgId"]
    repo_id: str = data["repoId"]
    branch: str = data["branch"]
    is_full: bool = bool(data.get("isFullScan", True))
    since_commit: Optional[str] = data.get("sinceCommit")
    requested_sha: str = data["commitSha"]

    clone_url = _resolve_clone_url(data)

    cfg.scan_scratch_dir.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix=f"job-{job['id']}-", dir=cfg.scan_scratch_dir))
    started_at = time.monotonic()
    try:
        repo_dir = job_dir / "repo"
        scan_output = job_dir / "scan.json"

        # Depth 1000 gives Stage 6 + behavioral coverage enough history
        # without paying full-history clone cost on monorepos.
        _run(
            ["git", "clone", "--depth", "1000", clone_url, str(repo_dir)],
            timeout=300,
        )

        if requested_sha == "HEAD":
            # Resolve placeholder to a concrete SHA AFTER cloning the default
            # branch so the dashboard can store the real commit.
            commit_sha = subprocess.check_output(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                timeout=30,
            ).decode().strip()
        else:
            _run(
                ["git", "-C", str(repo_dir), "checkout", requested_sha],
                timeout=60,
            )
            commit_sha = requested_sha

        cmd = [
            "faultlines",
            "scan-v2",
            str(repo_dir),
            "--output",
            str(scan_output),
            "--run-id",
            f"job-{job['id']}",
            # Per [[rule-full-flag-scans]] — landing-grade scans need the
            # complete signal surface so coverage, participants, and
            # classifications all land in the JSON.
            "--llm",
            "--flows",
            "--symbols",
            "--trace-flows",
            "--tool-flows",
            "--smart-aggregators",
            "--critique",
        ]
        if not is_full and since_commit:
            base_path = job_dir / "base-scan.json"
            fetch_base_scan(
                cfg,
                org_id=org_id,
                repo_id=repo_id,
                base_commit_sha=since_commit,
                dest=base_path,
            )
            cmd.extend(
                ["--since", since_commit, "--base-scan-path", str(base_path)]
            )

        env = {
            **os.environ,
            "ANTHROPIC_API_KEY": cfg.anthropic_api_key,
            "FAULTLINES_PRODUCTION": "1",
        }
        _run(cmd, env=env, timeout=cfg.timeout_sec)

        scan_blob = scan_output.read_bytes()
        scan_json = json.loads(scan_blob)
        scan_meta = scan_json.get("scan_meta") or {}
        features_summary = _summarize_features(scan_json)
        engine_version = scan_meta.get("engine_version") or _engine_version()
        llm_cost_cents = int(round(_extract_llm_cost_usd(scan_meta) * 100))
        scan_duration_ms = int((time.monotonic() - started_at) * 1000)

        parent_commit_sha = since_commit if not is_full else None

        ack = post_scan_to_dashboard(
            cfg,
            job_id=str(job["id"]),
            org_id=org_id,
            repo_id=repo_id,
            commit_sha=commit_sha,
            parent_commit_sha=parent_commit_sha,
            branch=branch,
            is_full_scan=is_full,
            engine_version=engine_version,
            scan_meta=scan_meta,
            features_summary=features_summary,
            scan_blob=scan_blob,
            scan_duration_ms=scan_duration_ms,
            llm_cost_cents=llm_cost_cents,
        )
        return {
            "ok": True,
            "scan_bytes": len(scan_blob),
            "scan_duration_ms": scan_duration_ms,
            "dashboard_ack": ack,
        }
    finally:
        # Cold-scan rule: never leave scan artefacts on disk between jobs.
        shutil.rmtree(job_dir, ignore_errors=True)


def _summarize_features(scan: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a tiny, indexable summary of developer features for the dashboard.

    The dashboard stores the full scan in encrypted blob storage and keeps
    only this projection in plaintext for browsing UIs. Keep the shape
    minimal — anything richer should go through the encrypted blob.
    """
    features = scan.get("developer_features") or []
    out: list[dict[str, Any]] = []
    for f in features:
        if not isinstance(f, dict):
            continue
        out.append({
            "name": f.get("name"),
            "paths_count": len(f.get("paths") or []),
            "flows_count": len(f.get("flows") or []),
            "impact_score": f.get("impact_score"),
        })
    return out


def _extract_llm_cost_usd(scan_meta: dict[str, Any]) -> float:
    """Best-effort extraction of LLM cost in USD from scan_meta.

    Different pipeline versions stash it under different keys. We accept
    any of the well-known ones and default to 0 (over-reporting is worse
    than under-reporting for usage meter purposes — under can be reconciled).
    """
    for key in ("llm_cost_usd", "total_llm_cost_usd", "cost_usd"):
        v = scan_meta.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _engine_version() -> str:
    """Resolve installed engine wheel version; fall back gracefully."""
    try:
        from importlib.metadata import version

        return version("faultline")
    except Exception:  # noqa: BLE001 — metadata lookup must never fail the job
        return ENGINE_VERSION_UNKNOWN


def _run(
    cmd: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    """``subprocess.run`` with consistent logging + ``check=True``."""
    logger.info("exec: %s", " ".join(_redact(c) for c in cmd))
    return subprocess.run(cmd, env=env, check=True, timeout=timeout)


def _redact(token: str) -> str:
    """Strip embedded ``x-access-token:<TOKEN>@`` segments from log lines."""
    if "x-access-token:" in token and "@" in token:
        head, _, tail = token.partition("x-access-token:")
        _, _, rest = tail.partition("@")
        return f"{head}x-access-token:***@{rest}"
    return token


# --------------------------------------------------------------------- loop


_shutdown = False


def _install_signal_handlers() -> None:
    def _handle(signum: int, _frame: Any) -> None:
        global _shutdown
        logger.info("received signal %s — finishing current job then exiting", signum)
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def poll_and_process(cfg: WorkerConfig) -> None:
    """Main loop. Blocks on next job; exits cleanly on SIGTERM/SIGINT."""
    logger.info(
        "worker starting (queue=%s, scratch=%s, dashboard=%s)",
        cfg.queue_name,
        cfg.scan_scratch_dir,
        cfg.dashboard_url,
    )
    while not _shutdown:
        try:
            with psycopg.connect(cfg.database_url, autocommit=False) as conn:
                while not _shutdown:
                    job = fetch_next_job(conn, cfg.queue_name)
                    if not job:
                        time.sleep(cfg.poll_interval_sec)
                        continue
                    job_id = job["id"]
                    logger.info("claimed job %s", job_id)
                    started = time.monotonic()
                    try:
                        output = process_job(cfg, job)
                        ack_job(conn, job_id, output)
                        logger.info(
                            "job %s ok in %.1fs", job_id, time.monotonic() - started
                        )
                    except subprocess.TimeoutExpired as e:
                        logger.exception("job %s timed out", job_id)
                        nack_job(conn, job_id, f"timeout: {e}")
                    except subprocess.CalledProcessError as e:
                        logger.exception("job %s subprocess failed", job_id)
                        nack_job(conn, job_id, f"subprocess exit {e.returncode}")
                    except Exception as e:  # noqa: BLE001 — last-resort handler
                        logger.exception("job %s failed", job_id)
                        nack_job(conn, job_id, repr(e))
        except psycopg.OperationalError:
            logger.exception("DB connection lost; reconnecting in 5s")
            time.sleep(5)
    logger.info("worker shutdown complete")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    _install_signal_handlers()
    cfg = WorkerConfig.from_env()
    poll_and_process(cfg)


if __name__ == "__main__":
    main()
